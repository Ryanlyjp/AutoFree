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
    ):
        self.session_token = session_token or admin_state.get_session_token()
        self.account_id = account_id or admin_state.get_account_id()
        self.device_id = device_id or str(uuid.uuid4())
        self._access_token: str | None = None
        self._access_token_fetched_at: float = 0.0

        self.session = requests.Session()
        proxy = proxy if proxy is not None else get_proxy()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    # ------------------------------------------------------------ headers / cookies

    def _cookie_jar(self) -> dict[str, str]:
        # Long session_tokens are split across __Secure-next-auth.session-token.0/.1
        # by the chatgpt.com login page. We accept either: a raw concatenated value
        # we save as `session-token`, or the user already pasted segmented values.
        token = self.session_token or ""
        if not token:
            return {}
        jar = {SESSION_COOKIE_NAME: token}
        # If the token is very long, also expose segmented form (some endpoints check it).
        if len(token) > 3800:
            mid = len(token) // 2
            jar[f"{SESSION_COOKIE_NAME}.0"] = token[:mid]
            jar[f"{SESSION_COOKIE_NAME}.1"] = token[mid:]
        return jar

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
        # Cache for 10 min — token JWT is valid much longer but session can
        # still rotate so we refresh periodically.
        if self._access_token and time.time() - self._access_token_fetched_at < 600:
            return self._access_token
        try:
            r = self.session.get(
                f"{CHATGPT_BASE}/api/auth/session",
                cookies=self._cookie_jar(),
                headers={"user-agent": DEFAULT_USER_AGENT, "accept": "application/json"},
                timeout=15,
            )
        except Exception as exc:
            if not silent:
                logger.warning("[master] /api/auth/session 取 access_token 失败: %s", exc)
            return None
        if r.status_code != 200:
            if not silent:
                logger.warning("[master] /api/auth/session HTTP %d", r.status_code)
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
        """Probe /api/auth/session + /backend-api/me to confirm the cookie works.
        Returns a dict with email, account_id, workspace_name when known.
        """
        # /api/auth/session works without an account_id and gives us email.
        r = self.session.get(
            f"{CHATGPT_BASE}/api/auth/session",
            cookies=self._cookie_jar(),
            headers={"user-agent": DEFAULT_USER_AGENT, "accept": "application/json"},
            timeout=15,
        )
        if r.status_code != 200:
            raise MasterAuthError(f"/api/auth/session HTTP {r.status_code}")
        sess = r.json() or {}
        user = (sess.get("user") or {})
        email = user.get("email") or ""
        token = sess.get("accessToken") or sess.get("access_token")
        if token:
            self._access_token = token
            self._access_token_fetched_at = time.time()

        # If we know the account_id, also fetch /accounts to confirm membership and pick up workspace_name.
        info: dict[str, Any] = {"email": email}
        if self.account_id:
            r2 = self._request("GET", f"/backend-api/accounts/{self.account_id}/settings",
                               referer=f"{CHATGPT_BASE}/admin")
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

    def list_members(self) -> list[dict[str, Any]]:
        """GET /backend-api/accounts/{account_id}/users — returns flat list of members."""
        self._require_account()
        r = self._request("GET", f"/backend-api/accounts/{self.account_id}/users",
                          referer=f"{CHATGPT_BASE}/admin/members")
        if r.status_code != 200:
            raise Exception(f"list_members HTTP {r.status_code}: {(r.text or '')[:200]}")
        body = r.json() or {}
        # Account-Owner endpoints typically wrap rows in `items` or `users`.
        rows = body.get("items") or body.get("users") or body.get("data") or body
        if not isinstance(rows, list):
            rows = []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            user = row.get("user") or row
            out.append(
                {
                    "user_id": user.get("id") or row.get("id") or row.get("user_id"),
                    "email": (user.get("email") or row.get("email") or "").lower(),
                    "name": user.get("name") or row.get("name") or "",
                    "role": row.get("role") or user.get("role") or "",
                    "status": row.get("status") or user.get("status") or "",
                    "raw": row,
                }
            )
        return out

    def find_user_id_by_email(self, email: str) -> str | None:
        target = (email or "").strip().lower()
        if not target:
            return None
        for m in self.list_members():
            if m.get("email") == target:
                return m.get("user_id")
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

    def kick_user_by_email(self, email: str) -> bool:
        uid = self.find_user_id_by_email(email)
        if not uid:
            logger.warning("[master] kick_by_email: 找不到 %s 对应的 user_id", email)
            return False
        return self.kick_user_by_id(uid)

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
) -> dict[str, Any]:
    """Verify a pasted session_token, then persist it to data/admin_state.json.

    `account_id` is optional — if omitted, this function tries to infer it
    from /api/auth/session's user payload (when ChatGPT exposes it). If still
    unknown, the caller must set it explicitly via set_account_id later.
    """
    token = (session_token or "").strip()
    if not token:
        raise ValueError("session_token 为空")

    client = MasterClient(session_token=token, account_id=account_id)
    info = client.verify_session()
    final_email = (email or info.get("email") or "").strip().lower()
    final_account = account_id or info.get("account_id") or ""
    state = admin_state.update_state(
        session_token=token,
        account_id=final_account or None,
        email=final_email or None,
        workspace_name=info.get("workspace_name") or None,
        updated_at=_now_iso(),
    )
    logger.info("[master] session_token 已导入 email=%s account_id=%s", final_email, final_account)
    return {
        "ok": True,
        "email": final_email,
        "account_id": final_account,
        "workspace_name": state.get("workspace_name") or "",
    }


def set_account_id(account_id: str) -> dict[str, Any]:
    """Manually set/override the master account_id and re-verify."""
    aid = (account_id or "").strip()
    if not aid:
        raise ValueError("account_id 为空")
    token = admin_state.get_session_token()
    if not token:
        raise MasterAuthError("session_token 未导入,请先 import_session_token")
    client = MasterClient(session_token=token, account_id=aid)
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
    return MasterClient(session_token=token, account_id=aid)
