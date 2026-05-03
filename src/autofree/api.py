"""FastAPI HTTP layer.

Endpoints fall into 6 groups, all under /api/* unless noted:
  /api/health                     unauth — liveness
  /api/settings (GET/PATCH)       proxy / mail / cpa config (deep-merged)
  /api/mail/probe                 verify mail backend connectivity
  /api/cpa/probe                  verify CPA reachability
  /api/master/*                   session import + auto_provision + members + kick
  /api/runs (CRUD)                start / list / inspect / cancel a multi-round run
  /api/auths (CRUD + push)        list / push / delete saved free-account tokens

Static files served at /  ← src/autofree/web/dist (Vue build output).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from autofree import admin_state, cpa_push, master, runner, storage
from autofree.config import get_api_key
from autofree.mail import get_mail_client
from autofree.settings import get_all as settings_get_all
from autofree.settings import update as settings_update

logger = logging.getLogger(__name__)


# ============================================================ app + auth


app = FastAPI(title="AutoFree", version="0.1.0", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


def require_api_key(request: Request) -> None:
    expected = get_api_key()
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="AUTOFREE_API_KEY 未配置 (.env 必须设置)",
        )
    auth = request.headers.get("authorization") or ""
    token = auth.removeprefix("Bearer ").strip() or request.query_params.get("api_key", "")
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid api key")


# ============================================================ health (no auth)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": "0.1.0"}


# ============================================================ settings


class SettingsPatch(BaseModel):
    """Partial update — fields omitted are kept untouched. Deep-merged into JSON."""

    proxy: str | None = None
    mail: dict[str, Any] | None = None
    cpa: dict[str, Any] | None = None


@app.get("/api/settings", dependencies=[Depends(require_api_key)])
def settings_get() -> dict[str, Any]:
    raw = settings_get_all()
    # Mask sensitive fields in the response so the panel never re-renders them.
    masked = _mask_settings(raw)
    return masked


@app.patch("/api/settings", dependencies=[Depends(require_api_key)])
def settings_patch(patch: SettingsPatch) -> dict[str, Any]:
    update_payload: dict[str, Any] = {}
    if patch.proxy is not None:
        update_payload["proxy"] = patch.proxy
    if patch.mail is not None:
        update_payload["mail"] = patch.mail
    if patch.cpa is not None:
        update_payload["cpa"] = patch.cpa
    if not update_payload:
        return _mask_settings(settings_get_all())
    settings_update(update_payload)
    return _mask_settings(settings_get_all())


def _mask_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with secrets replaced by `<set:N>` so the UI shows
    `is configured` without ever round-tripping the raw value."""

    def mask(v: Any) -> Any:
        if isinstance(v, str) and v:
            return f"<set:{len(v)}>"
        return v

    out = {
        "proxy": data.get("proxy") or "",
        "mail": {"provider": (data.get("mail") or {}).get("provider") or "tempmail"},
        "cpa": {},
    }
    mail = data.get("mail") or {}
    for backend in ("cf_temp_email", "maillab", "tempmail"):
        cfg = (mail.get(backend) or {}).copy()
        for secret_key in ("password", "api_key"):
            if secret_key in cfg:
                cfg[secret_key] = mask(cfg[secret_key])
        out["mail"][backend] = cfg
    cpa = data.get("cpa") or {}
    out["cpa"] = {
        "base_url": cpa.get("base_url") or "",
        "key": mask(cpa.get("key") or ""),
    }
    return out


# ============================================================ mail / cpa probes


class MailProbeReq(BaseModel):
    provider: str | None = None


@app.post("/api/mail/probe", dependencies=[Depends(require_api_key)])
def mail_probe(req: MailProbeReq) -> dict[str, Any]:
    try:
        client = get_mail_client(req.provider)
        token = client.login()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "provider": client.provider_name, "token_hint": token}


@app.post("/api/cpa/probe", dependencies=[Depends(require_api_key)])
def cpa_probe() -> dict[str, Any]:
    try:
        return cpa_push.probe()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ============================================================ master


class ImportTokenReq(BaseModel):
    session_token: str = Field(..., min_length=10)
    account_id: str | None = None
    email: str | None = None
    access_token: str | None = None  # optional Bearer override


class SetAccountReq(BaseModel):
    account_id: str = Field(..., min_length=1)


class SetAccessTokenReq(BaseModel):
    access_token: str  # empty string clears it


class AutoProvisionReq(BaseModel):
    value: bool


class KickReq(BaseModel):
    email: str | None = None
    user_id: str | None = None


@app.get("/api/master/state", dependencies=[Depends(require_api_key)])
def master_state() -> dict[str, Any]:
    summary = admin_state.get_summary()
    summary["proxy"] = (settings_get_all().get("proxy") or "")
    return summary


@app.post("/api/master/import-token", dependencies=[Depends(require_api_key)])
def master_import_token(req: ImportTokenReq) -> dict[str, Any]:
    try:
        return master.import_session_token(
            req.session_token,
            account_id=req.account_id,
            email=req.email,
            access_token=req.access_token,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/master/set-account-id", dependencies=[Depends(require_api_key)])
def master_set_account_id(req: SetAccountReq) -> dict[str, Any]:
    try:
        return master.set_account_id(req.account_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/master/set-access-token", dependencies=[Depends(require_api_key)])
def master_set_access_token(req: SetAccessTokenReq) -> dict[str, Any]:
    try:
        return master.set_access_token(req.access_token)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/master/diagnose", dependencies=[Depends(require_api_key)])
def master_diagnose() -> dict[str, Any]:
    return master.diagnose()


@app.delete("/api/master/state", dependencies=[Depends(require_api_key)])
def master_clear() -> dict[str, Any]:
    admin_state.clear_state()
    return {"ok": True}


@app.get("/api/master/identity", dependencies=[Depends(require_api_key)])
def master_identity() -> dict[str, Any]:
    try:
        client = master.get_default_client()
    except master.MasterAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        ident = client.get_identity()
        ap = client.get_auto_provision()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"identity 拉取失败: {exc}")
    return {"ok": True, "auto_provision": ap, "identity": ident}


@app.post("/api/master/auto-provision", dependencies=[Depends(require_api_key)])
def master_set_ap(req: AutoProvisionReq) -> dict[str, Any]:
    try:
        client = master.get_default_client()
        client.set_auto_provision(req.value)
        return {"ok": True, "auto_provision": req.value}
    except master.MasterAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/master/members", dependencies=[Depends(require_api_key)])
def master_members() -> dict[str, Any]:
    try:
        client = master.get_default_client()
        members = client.list_members()
        return {"ok": True, "count": len(members), "members": members}
    except master.MasterAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/master/kick", dependencies=[Depends(require_api_key)])
def master_kick(req: KickReq) -> dict[str, Any]:
    if not req.email and not req.user_id:
        raise HTTPException(status_code=400, detail="必须提供 email 或 user_id")
    try:
        client = master.get_default_client()
        if req.user_id:
            ok = client.kick_user_by_id(req.user_id)
            return {"ok": ok, "reason": "" if ok else "kick_user_by_id 返回 success=false"}
        else:
            ok, reason = client.kick_user_by_email(req.email or "")
            return {"ok": ok, "reason": reason}
    except master.MasterAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ============================================================ runs


class StartRunReq(BaseModel):
    rounds: int = Field(..., ge=1, le=20)
    per_round: int = Field(..., ge=1, le=10)
    mail_provider: str | None = None


@app.get("/api/runs", dependencies=[Depends(require_api_key)])
def runs_list(limit: int = 50) -> dict[str, Any]:
    return {"runs": storage.list_runs(limit=limit)}


@app.post("/api/runs", dependencies=[Depends(require_api_key)], status_code=202)
def runs_start(req: StartRunReq) -> dict[str, Any]:
    if not admin_state.get_session_token():
        raise HTTPException(status_code=400, detail="尚未导入母号 session_token")
    if not admin_state.get_account_id():
        raise HTTPException(status_code=400, detail="母号 account_id 未设置")
    try:
        record = runner.start_run(req.rounds, req.per_round, mail_provider=req.mail_provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return record


@app.get("/api/runs/{run_id}", dependencies=[Depends(require_api_key)])
def runs_get(run_id: str) -> dict[str, Any]:
    rec = storage.get_run(run_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return rec


@app.post("/api/runs/{run_id}/cancel", dependencies=[Depends(require_api_key)])
def runs_cancel(run_id: str) -> dict[str, Any]:
    if not storage.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    cancelled = runner.cancel_run(run_id)
    return {"ok": cancelled, "running": runner.is_running(run_id)}


# ============================================================ auths


class PushOneReq(BaseModel):
    force: bool = False


class PushManyReq(BaseModel):
    emails: list[str] | None = None  # None → push everything un-pushed
    force: bool = False


@app.get("/api/auths", dependencies=[Depends(require_api_key)])
def auths_list() -> dict[str, Any]:
    return {"auths": storage.list_auths()}


@app.get("/api/auths/{email}", dependencies=[Depends(require_api_key)])
def auths_get(email: str) -> dict[str, Any]:
    data = storage.load_auth(email)
    if not data:
        raise HTTPException(status_code=404, detail=f"auth {email} not found")
    # Mask the actual tokens.
    masked = data.copy()
    for k in ("access_token", "refresh_token", "id_token"):
        if masked.get(k):
            masked[k] = f"<set:{len(masked[k])}>"
    return masked


@app.delete("/api/auths/{email}", dependencies=[Depends(require_api_key)])
def auths_delete(email: str) -> dict[str, Any]:
    ok = storage.delete_auth(email)
    if not ok:
        raise HTTPException(status_code=404, detail=f"auth {email} not found")
    return {"ok": True}


@app.post("/api/auths/{email}/push", dependencies=[Depends(require_api_key)])
def auths_push_one(email: str, req: PushOneReq) -> dict[str, Any]:
    try:
        return cpa_push.push_one(email, overwrite=req.force)
    except cpa_push.CPAError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/auths/push-all", dependencies=[Depends(require_api_key)])
def auths_push_all(req: PushManyReq) -> dict[str, Any]:
    if req.emails is not None:
        emails = list(req.emails)
    else:
        emails = [
            row["email"]
            for row in storage.list_auths()
            if not row.get("pushed_to_cpa_at") or req.force
        ]
    if not emails:
        return {"pushed": 0, "skipped": 0, "failed": 0, "total": 0, "results": []}
    try:
        return cpa_push.push_many(emails, overwrite=req.force)
    except cpa_push.CPAError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ============================================================ static frontend

_DIST = Path(__file__).parent / "web" / "dist"

if _DIST.is_dir() and (_DIST / "index.html").is_file():
    # Mount under /assets so /api/* still wins.
    if (_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(_DIST / "index.html"))

    @app.get("/{path:path}")
    def spa_fallback(path: str) -> FileResponse:
        # Don't shadow API routes.
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = _DIST / path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_DIST / "index.html"))
else:

    @app.get("/")
    def index_placeholder() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "ui": "not built — run `cd web && npm install && npm run build`",
                "docs": "/api/docs",
                "version": "0.1.0",
            }
        )
