"""Master account operations against chatgpt.com /backend-api.

Captured-traffic reference (`muhao.har`):
    GET    /backend-api/accounts/{account_id}/identity
    POST   /backend-api/accounts/{account_id}/settings/auto_provision  body {"value": bool}
    GET    /backend-api/accounts/{account_id}/users
    DELETE /backend-api/accounts/{account_id}/users/{user_id}

Auth model: session cookie `__Secure-next-auth.session-token` + headers
`chatgpt-account-id` and a browser-like `user-agent`. We optionally pull a
Bearer access_token from `/api/auth/session` and attach it for endpoints that
require it; identity/settings/users work with the cookie alone.

Playwright-based email+password login lives in flow.py (it shares the browser
machinery there). This module only handles `import_session_token` for the
fast path used by CTF.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

from autofree import admin_state
from autofree.proxy import build_requests_proxy_map, normalize_proxy_url
from autofree.settings import get_proxy

logger = logging.getLogger(__name__)


CHATGPT_BASE = "https://chatgpt.com"
SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


class MasterAuthError(Exception):
    """Session token rejected — user needs to re-import."""


class MasterCloudflareError(Exception):
    """Cloudflare challenge encountered — needs the Playwright path."""


class MasterClient:
    """Lightweight requests-based client for the master ChatGPT Team workspace."""

    def __init__(
        self,
        session_token: str | None = None,
        account_id: str | None = None,
        *,
        device_id: str | None = None,
        proxy: str | None = None,
        access_token: str | None = None,
    ):
        self.session_token = session_token or admin_state.get_session_token()
        self.account_id = account_id or admin_state.get_account_id()
        self.device_id = device_id or str(uuid.uuid4())
        # Pre-seeded access_token wins over /api/auth/session-derived one.
        # Saved value lives in admin_state (`access_token` field). User may paste
        # it on the Setup page when /api/auth/session can't be made to work
        # (e.g. cookie too restrictive, Cloudflare interference).
        self._user_access_token: str | None = (
            access_token or admin_state.get_state().get("access_token") or None
        )
        self._access_token: str | None = None
        self._access_token_fetched_at: float = 0.0

        self.session = requests.Session()
        self.session.trust_env = False
        proxy = proxy if proxy is not None else get_proxy()
        self.proxy = normalize_proxy_url(proxy)
        proxies = build_requests_proxy_map(self.proxy)
        if proxies:
            self.session.proxies = proxies

    # ------------------------------------------------------------ headers / cookies

    def _cookie_jar(self) -> dict[str, str]:
        # Send the raw session_token under one cookie name only. Don't try to
        # auto-split into .0/.1 — getting the wrong split point yields a cookie
        # NextAuth refuses to decrypt and the response becomes a logged-out
        # session (200 with empty body). If your token comes from chunked
        # cookies, paste the *concatenated* value (.0 + .1) into session_token.
        token = self.session_token or ""
        if not token:
            return {}
        return {SESSION_COOKIE_NAME: token}

    def _base_headers(self, *, referer: str | None = None) -> dict[str, str]:
        h = {
            "user-agent": DEFAULT_USER_AGENT,
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "oai-device-id": self.device_id,
            "oai-language": "en-US",
            "origin": CHATGPT_BASE,
            "referer": referer or f"{CHATGPT_BASE}/admin/identity",
        }
        if self.account_id:
            h["chatgpt-account-id"] = self.account_id
        token = self._get_access_token(silent=True)
        if token:
            h["authorization"] = f"Bearer {token}"
        return h

    # ------------------------------------------------------------ low-level request

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        referer: str | None = None,
        timeout: float = 30.0,
        retries: int = 1,
    ) -> requests.Response:
        url = f"{CHATGPT_BASE}{path}"
        cookies = self._cookie_jar()
        if not cookies:
            raise MasterAuthError("master session_token 未导入")
        headers = self._base_headers(referer=referer)
        if json_body is not None:
            headers["content-type"] = "application/json"

        for attempt in range(retries + 1):
            r = self.session.request(
                method, url,
                headers=headers, cookies=cookies, json=json_body, timeout=timeout,
            )
            if r.status_code == 403 and "challenge-platform" in (r.text or "")[:5000]:
                raise MasterCloudflareError(
                    f"{method} {path}: Cloudflare 拦截。需要在浏览器内重新导出 cf_clearance + session_token"
                )
            if r.status_code in (401, 403):
                raise MasterAuthError(f"{method} {path}: HTTP {r.status_code} {(r.text or '')[:200]}")
            if r.status_code >= 500 and attempt < retries:
                time.sleep(0.6 * (attempt + 1))
                continue
            return r
        return r  # not reached

    # ------------------------------------------------------------ access token

    def _get_access_token(self, *, silent: bool = False) -> str | None:
        """Resolve a Bearer access_token for /backend-api/* calls.

        Priority:
          1. User-pasted access_token (admin_state.access_token) — never expires
             from our point of view; we just trust it until chatgpt rejects.
          2. Cached JWT from a recent /api/auth/session call (10-min TTL).
          3. Fresh /api/auth/session fetch — only works when chatgpt accepts
             our session cookie.

        When step 3 fails or returns an empty/logged-out session, returns None
        unless `silent=False`, in which case it logs a warning. The caller is
        expected to surface a clearer error to the user.
        """
        if self._user_access_token:
            return self._user_access_token
        if self._access_token and time.time() - self._access_token_fetched_at < 600:
            return self._access_token
        try:
            r = self.session.get(
                f"{CHATGPT_BASE}/api/auth/session",
                cookies=self._cookie_jar(),
                headers={
                    "user-agent": DEFAULT_USER_AGENT,
                    "accept": "application/json",
                    "referer": f"{CHATGPT_BASE}/",
                },
                timeout=15,
            )
        except Exception as exc:
            if not silent:
                logger.warning("[master] /api/auth/session 取 access_token 失败: %s", exc)
            return None
        if r.status_code != 200:
            if not silent:
                logger.warning("[master] /api/auth/session HTTP %d body=%s", r.status_code, (r.text or "")[:200])
            return None
        try:
            data = r.json() or {}
        except Exception:
            return None
        token = data.get("accessToken") or data.get("access_token")
        if token:
            self._access_token = token
            self._access_token_fetched_at = time.time()
        return token

    # ------------------------------------------------------------ public ops

    def verify_session(self) -> dict[str, Any]:
        """Probe /api/auth/session + /backend-api/accounts/{id}/settings to
        confirm credentials. Returns dict {email, account_id, workspace_name}.

        If the session cookie alone can't produce a logged-in session AND the
        user did not provide a separate access_token, raises a clear error
        explaining the two recovery paths.
        """
        # /api/auth/session works without an account_id and gives us email.
        r = self.session.get(
            f"{CHATGPT_BASE}/api/auth/session",
            cookies=self._cookie_jar(),
            headers={
                "user-agent": DEFAULT_USER_AGENT,
                "accept": "application/json",
                "referer": f"{CHATGPT_BASE}/",
            },
            timeout=15,
        )
        if r.status_code != 200:
            raise MasterAuthError(
                f"/api/auth/session HTTP {r.status_code} — chatgpt 拒绝了 session_token cookie。"
                " 重新从浏览器复制完整 session_token (chunked 形式记得拼接 .0 + .1)。"
            )
        try:
            sess = r.json() or {}
        except Exception:
            sess = {}
        user = (sess.get("user") or {})
        email = user.get("email") or ""
        token = sess.get("accessToken") or sess.get("access_token")
        if token:
            self._access_token = token
            self._access_token_fetched_at = time.time()

        # /api/auth/session returned a logged-out shell? Cookie isn't being
        # accepted by NextAuth. Two valid paths forward — surface both.
        if not email and not token and not self._user_access_token:
            preview = (r.text or "")[:200]
            raise MasterAuthError(
                "/api/auth/session 返回了空 session(cookie 无效或被截断)。两种修法,任选一种:\n"
                "  ① 从浏览器 DevTools 复制完整 __Secure-next-auth.session-token "
                "(分 .0 / .1 段时按顺序拼接,不要漏字符), 重新导入。\n"
                "  ② 同时粘贴 access_token: DevTools → Network → 任意一次 "
                "/api/auth/session 请求 → Response 里复制 accessToken 的值,"
                "在 Setup 页 access_token 框粘贴。\n"
                f"原始响应片段: {preview!r}"
            )

        # If we know the account_id, also fetch /settings to confirm membership and pick up workspace_name.
        info: dict[str, Any] = {"email": email}
        if self.account_id:
            try:
                r2 = self._request("GET", f"/backend-api/accounts/{self.account_id}/settings",
                                   referer=f"{CHATGPT_BASE}/admin")
            except MasterAuthError as exc:
                # 401/403 here is almost certainly "Access token is missing" —
                # rethrow with actionable hint.
                raise MasterAuthError(
                    f"{exc}。已拿到 session 但 access_token 缺失或被拒。"
                    " 请在 Setup 页 access_token 框粘贴 (从浏览器 /api/auth/session 响应里复制)。"
                )
            if r2.status_code == 200:
                try:
                    body = r2.json() or {}
                    info["workspace_name"] = body.get("workspace_name") or body.get("name") or ""
                except Exception:
                    pass
        info["account_id"] = self.account_id
        return info

    # ---- identity / auto_provision ----

    def get_identity(self) -> dict[str, Any]:
        """GET /backend-api/accounts/{account_id}/identity — returns the
        Identity & Access settings block."""
        self._require_account()
        r = self._request("GET", f"/backend-api/accounts/{self.account_id}/identity")
        if r.status_code != 200:
            raise Exception(f"get_identity HTTP {r.status_code}: {(r.text or '')[:200]}")
        return r.json() or {}

    def get_auto_provision(self) -> bool | None:
        """Return current auto_provision toggle; None if unknown."""
        ident = self.get_identity()
        # Known fields seen in production: top-level `auto_provision` or nested in settings.
        for key_path in (("auto_provision",), ("settings", "auto_provision"), ("automatic_account_creation",)):
            cur: Any = ident
            for k in key_path:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(k)
            if isinstance(cur, bool):
                return cur
            if isinstance(cur, dict) and "value" in cur and isinstance(cur["value"], bool):
                return cur["value"]
        return None

    def set_auto_provision(self, value: bool) -> dict[str, Any]:
        """POST /backend-api/accounts/{account_id}/settings/auto_provision body {"value": bool}.
        Returns the updated settings document.
        """
        self._require_account()
        path = f"/backend-api/accounts/{self.account_id}/settings/auto_provision"
        r = self._request("POST", path, json_body={"value": bool(value)},
                          referer=f"{CHATGPT_BASE}/admin/identity")
        if r.status_code != 200:
            raise Exception(f"set_auto_provision HTTP {r.status_code}: {(r.text or '')[:200]}")
        logger.info("[master] auto_provision -> %s", value)
        return r.json() or {}

    # ---- members ----

    # chatgpt admin /users uses offset/limit pagination (verified via kick.har:
    # GET /backend-api/accounts/{id}/users?offset=0&limit=25&query=).
    # Default UI limit is 25; we use 100 to need fewer round-trips.
    _MEMBER_PAGE_SIZE = 100

    def _normalize_member_row(self, row: dict) -> dict[str, Any] | None:
        """Map an API row to {user_id, email, name, role, status, raw}.
        Returns None if user_id is missing (skip)."""
        if not isinstance(row, dict):
            return None
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        # Prefer top-level user_id (the "user-xxx" format used in DELETE),
        # then nested user.id, then row.id, then member_id.
        uid = (
            row.get("user_id")
            or user.get("id")
            or row.get("id")
            or row.get("member_id")
        )
        if not uid:
            return None
        email = (row.get("email") or user.get("email") or "").lower().strip()
        return {
            "user_id": uid,
            "email": email,
            "name": row.get("name") or user.get("name") or "",
            "role": row.get("role") or user.get("role") or "",
            "status": row.get("status") or user.get("status") or "",
            "raw": row,
        }

    def _list_members_page(self, *, offset: int, limit: int, query: str = "") -> tuple[list[dict], dict]:
        """Single page of /users. Returns (rows, raw_body)."""
        self._require_account()
        params = f"offset={offset}&limit={limit}&query={quote(query)}"
        path = f"/backend-api/accounts/{self.account_id}/users?{params}"
        r = self._request("GET", path, referer=f"{CHATGPT_BASE}/admin/members")
        if r.status_code != 200:
            raise Exception(f"list_members HTTP {r.status_code}: {(r.text or '')[:200]}")
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        rows_raw: Any = (
            body.get("items")
            or body.get("users")
            or body.get("members")
            or body.get("data")
            or (body if isinstance(body, list) else [])
        )
        if not isinstance(rows_raw, list):
            rows_raw = []
        rows = [m for m in (self._normalize_member_row(r) for r in rows_raw) if m]
        return rows, body if isinstance(body, dict) else {}

    def list_members(self, *, query: str = "") -> list[dict[str, Any]]:
        """List members of the master workspace, paging through all results.

        Pass `query` to filter server-side by email/name (used by the fast-path
        `find_user_id_by_email`). Without `query`, walks every offset until a
        page returns fewer rows than `limit` (no more pages).
        """
        out: list[dict[str, Any]] = []
        seen_ids: set = set()
        offset = 0
        max_pages = 30  # safety: 100 * 30 = 3000 ceiling

        for page in range(max_pages):
            rows, body = self._list_members_page(
                offset=offset, limit=self._MEMBER_PAGE_SIZE, query=query,
            )
            if not rows:
                break
            new_in_page = 0
            for m in rows:
                if m["user_id"] in seen_ids:
                    continue
                seen_ids.add(m["user_id"])
                out.append(m)
                new_in_page += 1
            # Stop conditions: short page, or server-reported total reached.
            total = body.get("total") if isinstance(body, dict) else None
            if len(rows) < self._MEMBER_PAGE_SIZE:
                break
            if isinstance(total, int) and len(out) >= total:
                break
            if new_in_page == 0:
                break
            offset += self._MEMBER_PAGE_SIZE

        logger.info(
            "[master] list_members(query=%r): 共 %d 个成员 (跨 %d 页)",
            query, len(out), page + 1,
        )
        return out

    def find_user_id_by_email(self, email: str) -> str | None:
        """Email → user_id, case-insensitive. None if not found.

        Three-phase lookup:
          1. Fast path: `?query=<email>` — chatgpt admin API filters server-side
             (chatgpt-admin UI uses `query` for the "按姓名筛选" search box;
             it also matches against email).
          2. Slow path: full pagination then exact email match.
          3. Last resort: substring match on local-part — handles cases where
             the server normalises email differently (rare but cheap).

        On miss, dumps the **raw** first row so the user can see exactly what
        chatgpt returns vs. what they expect. This is the kick debug net.
        """
        target = (email or "").strip().lower()
        if not target:
            return None

        # ---- fast path: ?query=email ----
        try:
            rows, body = self._list_members_page(offset=0, limit=25, query=target)
        except Exception as exc:
            logger.warning("[master] query 快路径失败,fallback 全量分页: %s", exc)
            rows, body = [], {}
        for m in rows:
            if m.get("email") == target:
                logger.info("[master] find_user_id_by_email: query 快路径命中 %s -> %s",
                            target, m["user_id"])
                return m["user_id"]
        if rows:
            logger.info(
                "[master] query=%s 返回 %d 行但无精确匹配 (前几行 email: %s) — 进入全量",
                target, len(rows), [m.get("email") for m in rows[:5]],
            )

        # ---- slow path: full pagination ----
        members = self.list_members()
        for m in members:
            if m.get("email") == target:
                return m["user_id"]

        # ---- last resort: substring match on local-part ----
        local = target.split("@", 1)[0]
        if local:
            for m in members:
                if local in (m.get("email") or ""):
                    logger.warning(
                        "[master] find_user_id_by_email: 精确匹配未中,但 local-part %r 在 %r 里匹配",
                        local, m.get("email"),
                    )
                    return m["user_id"]

        # Still not found — dump the raw row of the first member so the user
        # can see what fields chatgpt actually returns (in case the API
        # changed shape and we're parsing wrong keys).
        sample = [
            {"email": m.get("email"), "status": m.get("status"),
             "role": m.get("role"), "name": m.get("name"), "user_id": m.get("user_id")}
            for m in members[:20]
        ]
        raw_first = members[0].get("raw") if members else None
        logger.warning(
            "[master] find_user_id_by_email: 没找到 %s; 当前共 %d 个成员, 前 20 个 normalised: %s",
            target, len(members), sample,
        )
        if raw_first:
            logger.warning("[master] 第 1 行原始 (debug): %s", raw_first)
        return None

    def kick_user_by_id(self, user_id: str) -> bool:
        """DELETE /backend-api/accounts/{account_id}/users/{user_id} → {success: true}."""
        self._require_account()
        if not user_id:
            return False
        path = f"/backend-api/accounts/{self.account_id}/users/{quote(user_id)}"
        r = self._request("DELETE", path, referer=f"{CHATGPT_BASE}/admin/members")
        if r.status_code not in (200, 204):
            raise Exception(f"kick_user HTTP {r.status_code}: {(r.text or '')[:200]}")
        try:
            ok = bool((r.json() or {}).get("success", True))
        except Exception:
            ok = True
        logger.info("[master] kick user_id=%s ok=%s", user_id, ok)
        return ok

    def kick_user_by_email(
        self, email: str, *, lookup_retries: int = 3, retry_interval: float = 3.0
    ) -> tuple[bool, str]:
        """Returns (success, reason). reason is non-empty when success=False.

        Retries the member-list lookup up to `lookup_retries` times with
        `retry_interval` seconds between attempts because OpenAI has a sync
        delay: a user who just joined via auto_provision may not appear in
        /users immediately. Only after all retries miss do we declare absent.
        """
        target = (email or "").strip().lower()
        total_attempts = max(1, lookup_retries + 1)
        uid: str | None = None

        for attempt in range(total_attempts):
            uid = self.find_user_id_by_email(target)
            if uid:
                break
            if attempt < total_attempts - 1:
                logger.info(
                    "[master] kick: %s 暂未在成员列表中 (OpenAI 同步延迟?), %.1fs 后重试 (%d/%d)",
                    target, retry_interval, attempt + 1, total_attempts - 1,
                )
                time.sleep(retry_interval)

        if not uid:
            logger.info(
                "[master] kick: 重试 %d 次后仍未找到 %s, 视为已不在 Team (already_absent)",
                total_attempts, target,
            )
            return True, "already_absent"

        try:
            ok = self.kick_user_by_id(uid)
        except Exception as exc:
            return False, f"DELETE 调用失败: {exc}"
        return (ok, "" if ok else "DELETE 返回 success=false")

    # ------------------------------------------------------------ helpers

    def _require_account(self) -> None:
        if not self.account_id:
            raise MasterAuthError("master account_id 未设置")


# ============================================================ session import


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def import_session_token(
    session_token: str,
    *,
    account_id: str | None = None,
    email: str | None = None,
    access_token: str | None = None,
) -> dict[str, Any]:
    """Verify a pasted session_token (+ optional access_token), persist to admin_state.

    Pass `access_token` when chatgpt's `/api/auth/session` won't yield one (the
    most common reason for the "Access token is missing" 401). Copy it from
    DevTools → Network → /api/auth/session response → `accessToken` field.
    """
    token = (session_token or "").strip()
    if not token:
        raise ValueError("session_token 为空")
    at = (access_token or "").strip() or None

    client = MasterClient(session_token=token, account_id=account_id, access_token=at)
    info = client.verify_session()
    final_email = (email or info.get("email") or "").strip().lower()
    final_account = account_id or info.get("account_id") or ""
    state = admin_state.update_state(
        session_token=token,
        access_token=at,  # None → admin_state.update_state will pop the field
        account_id=final_account or None,
        email=final_email or None,
        workspace_name=info.get("workspace_name") or None,
        updated_at=_now_iso(),
    )
    logger.info("[master] session_token 已导入 email=%s account_id=%s access_token=%s",
                final_email, final_account, "set" if at else "from-session")
    return {
        "ok": True,
        "email": final_email,
        "account_id": final_account,
        "workspace_name": state.get("workspace_name") or "",
        "access_token_source": "user-provided" if at else "from-session",
    }


def set_access_token(access_token: str) -> dict[str, Any]:
    """Add/replace the master access_token without touching session_token.

    Useful when /api/auth/session refuses our cookie but the user can still
    grab a Bearer token from a working browser tab.
    """
    at = (access_token or "").strip()
    if not at:
        # Empty value clears it.
        admin_state.update_state(access_token=None, updated_at=_now_iso())
        return {"ok": True, "cleared": True}
    token = admin_state.get_session_token()
    aid = admin_state.get_account_id()
    if not token:
        # Allow access_token-only mode. session_token strictly speaking is no
        # longer needed for /backend-api/* once we have Bearer; we still keep
        # session_token in the model for /api/auth/session and consent flows.
        admin_state.update_state(access_token=at, updated_at=_now_iso())
        return {"ok": True, "warning": "session_token 未设置,只有 access_token 可能不够"}
    client = MasterClient(session_token=token, account_id=aid, access_token=at)
    info = client.verify_session()
    admin_state.update_state(
        access_token=at,
        workspace_name=info.get("workspace_name") or None,
        updated_at=_now_iso(),
    )
    return {
        "ok": True,
        "account_id": aid,
        "workspace_name": info.get("workspace_name") or "",
    }


def set_account_id(account_id: str) -> dict[str, Any]:
    """Manually set/override the master account_id and re-verify."""
    aid = (account_id or "").strip()
    if not aid:
        raise ValueError("account_id 为空")
    token = admin_state.get_session_token()
    if not token:
        raise MasterAuthError("session_token 未导入,请先 import_session_token")
    saved_at = admin_state.get_state().get("access_token") or None
    client = MasterClient(session_token=token, account_id=aid, access_token=saved_at)
    info = client.verify_session()
    state = admin_state.update_state(
        account_id=aid,
        workspace_name=info.get("workspace_name") or None,
        updated_at=_now_iso(),
    )
    return {
        "ok": True,
        "account_id": aid,
        "email": admin_state.get_email(),
        "workspace_name": state.get("workspace_name") or "",
    }


def get_default_client() -> MasterClient:
    """Build a MasterClient from saved admin_state. Raises if session missing."""
    token = admin_state.get_session_token()
    if not token:
        raise MasterAuthError("session_token 未导入")
    aid = admin_state.get_account_id()
    saved_at = admin_state.get_state().get("access_token") or None
    return MasterClient(session_token=token, account_id=aid, access_token=saved_at)


def diagnose() -> dict[str, Any]:
    """Run a non-throwing self-test against /api/auth/session + (if account_id
    set) /backend-api/accounts/{id}/settings. Returns a dict the UI can render."""
    token = admin_state.get_session_token()
    aid = admin_state.get_account_id()
    saved_at = admin_state.get_state().get("access_token") or None
    out: dict[str, Any] = {
        "session_token_set": bool(token),
        "access_token_set": bool(saved_at),
        "account_id_set": bool(aid),
    }
    if not token:
        out["session"] = {"ok": False, "error": "session_token 未导入"}
        return out
    client = MasterClient(session_token=token, account_id=aid, access_token=saved_at)
    # session probe
    try:
        r = client.session.get(
            f"{CHATGPT_BASE}/api/auth/session",
            cookies=client._cookie_jar(),
            headers={"user-agent": DEFAULT_USER_AGENT, "accept": "application/json",
                     "referer": f"{CHATGPT_BASE}/"},
            timeout=15,
        )
        body = {}
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        out["session"] = {
            "ok": r.status_code == 200,
            "status": r.status_code,
            "has_user": bool((body.get("user") or {}).get("email")),
            "has_access_token": bool(body.get("accessToken") or body.get("access_token")),
            "preview": (r.text or "")[:200] if r.status_code != 200 else None,
        }
    except Exception as exc:
        out["session"] = {"ok": False, "error": str(exc)}
    # backend-api settings probe (only if account_id set)
    if aid:
        try:
            r2 = client.session.get(
                f"{CHATGPT_BASE}/backend-api/accounts/{aid}/settings",
                cookies=client._cookie_jar(),
                headers=client._base_headers(),
                timeout=15,
            )
            out["backend_settings"] = {
                "ok": r2.status_code == 200,
                "status": r2.status_code,
                "preview": (r2.text or "")[:200] if r2.status_code != 200 else None,
            }
        except Exception as exc:
            out["backend_settings"] = {"ok": False, "error": str(exc)}
    return out
