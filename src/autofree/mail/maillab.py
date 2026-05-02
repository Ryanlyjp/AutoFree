"""maillab/cloud-mail backend.

Auth: POST /login {email,password} → {data:{token}}, then Authorization: <jwt>.
Routes: /account/list, /account/add, /account/delete, /email/list, /email/delete.
"""

from __future__ import annotations

import functools
import logging
import re
import threading
import uuid
from datetime import datetime, timezone

import requests

from autofree.mail.base import MailProvider, normalize_email_addr
from autofree.settings import get_mail_config

logger = logging.getLogger(__name__)


class MaillabAuthFailed(Exception):
    pass


_GUARD = threading.local()


def _retry_on_401(method):
    @functools.wraps(method)
    def wrapper(self, path, *args, **kwargs):
        resp = method(self, path, *args, **kwargs)
        if not (isinstance(resp, dict) and resp.get("code") == 401):
            return resp
        if getattr(_GUARD, "in_login", False):
            return resp
        if getattr(_GUARD, "retried", False):
            raise MaillabAuthFailed(f"maillab {path} re-login 后仍 401")
        self.token = None
        try:
            _GUARD.retried = True
            self.login()
            resp = method(self, path, *args, **kwargs)
            if isinstance(resp, dict) and resp.get("code") == 401:
                raise MaillabAuthFailed(f"maillab {path} re-login 后仍 401")
        finally:
            _GUARD.retried = False
        return resp

    return wrapper


def _parse_ts(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 1e12 else int(value)
    text = str(value).strip()
    if not text:
        return None
    iso = text.replace(" ", "T") if "T" not in text and " " in text else text
    iso = iso.rstrip("Z")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        try:
            return int(float(text))
        except Exception:
            return None


class MaillabClient(MailProvider):
    provider_name = "maillab"

    def __init__(self):
        cfg = get_mail_config("maillab")
        self.base_url = (cfg.get("api_url") or "").rstrip("/")
        self.username = cfg.get("username") or ""
        self.password = cfg.get("password") or ""
        self.default_domain = (cfg.get("domain") or "").lstrip("@").strip()
        self.session = requests.Session()
        self.token: str | None = None

    # ---- http

    def _url(self, p):
        return f"{self.base_url}{p if p.startswith('/') else '/' + p}"

    def _h(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = self.token  # bare JWT, no Bearer
        return h

    @_retry_on_401
    def _get(self, path, params=None):
        return self._parse(self.session.get(self._url(path), headers=self._h(), params=params, timeout=30), path)

    @_retry_on_401
    def _post(self, path, body=None):
        return self._parse(self.session.post(self._url(path), headers=self._h(), json=body, timeout=30), path)

    @_retry_on_401
    def _del(self, path, params=None):
        return self._parse(self.session.delete(self._url(path), headers=self._h(), params=params, timeout=30), path)

    @staticmethod
    def _parse(r: requests.Response, path: str) -> dict:
        if r.status_code == 404:
            raise Exception(f"maillab {path} 404 — base_url 可能错配")
        if r.status_code != 200:
            raise Exception(f"maillab {path} HTTP {r.status_code}: {(r.text or '')[:200]}")
        try:
            return r.json() or {}
        except Exception as e:
            raise Exception(f"maillab {path} 响应非 JSON: {e}")

    # ---- auth

    def login(self) -> str:
        if not self.base_url:
            raise Exception("maillab 未配置 api_url")
        if not self.username or not self.password:
            raise Exception("maillab 未配置 username / password")
        _GUARD.in_login = True
        try:
            resp = self._post("/login", {"email": self.username, "password": self.password})
        finally:
            _GUARD.in_login = False
        if resp.get("code") != 200:
            raise Exception(f"maillab 登录失败: {resp.get('message') or resp}")
        token = (resp.get("data") or {}).get("token")
        if not token:
            raise Exception("maillab 登录响应缺 token")
        self.token = token
        return token

    def _ensure(self):
        if not self.token:
            self.login()

    # ---- accounts

    def _build(self, prefix, domain) -> str:
        domain = (domain or self.default_domain).lstrip("@").strip()
        if not domain:
            raise Exception("maillab 创建邮箱失败: 未配置域名")
        if not prefix:
            cleaned = uuid.uuid4().hex[:10]
        else:
            cleaned = re.sub(r"[^A-Za-z0-9._-]", "", str(prefix)).strip(".-_")
            cleaned = cleaned[:60] or uuid.uuid4().hex[:10]
        return f"{cleaned}@{domain}"

    def create_temp_email(self, prefix=None, domain=None):
        self._ensure()
        full = self._build(prefix, domain)
        resp = self._post("/account/add", {"email": full})
        if resp.get("code") != 200:
            raise Exception(f"maillab 创建失败: {resp.get('message') or resp}")
        data = resp.get("data") or {}
        aid = data.get("accountId") or data.get("id")
        if not aid:
            raise Exception(f"maillab 创建响应缺 accountId: {data!r}")
        return aid, data.get("email") or full

    _PAGE_CAP = 30

    def list_accounts(self, size=200):
        self._ensure()
        target = int(size) if size else 0
        out, last_sort, last_id = [], 0, 0
        seen: set = set()
        max_pages = max(1, (target // self._PAGE_CAP) + 2) if target else 50
        for _ in range(max_pages):
            params = {"size": self._PAGE_CAP}
            if last_sort or last_id:
                params["lastSort"] = last_sort
                params["accountId"] = last_id
            resp = self._get("/account/list", params=params)
            if resp.get("code") != 200:
                break
            rows = resp.get("data") or []
            if not rows:
                break
            new_in_page = 0
            for row in rows:
                aid = row.get("accountId") or row.get("id")
                if aid is None or aid in seen:
                    continue
                seen.add(aid)
                new_in_page += 1
                out.append(
                    {
                        "accountId": aid,
                        "email": row.get("email"),
                        "createTime": _parse_ts(row.get("createTime")),
                        "latestEmailTime": _parse_ts(row.get("latestEmailTime")),
                    }
                )
                if target and len(out) >= target:
                    return out
            if not new_in_page:
                break
            tail = rows[-1]
            last_sort = tail.get("sort") or 0
            last_id = tail.get("accountId") or tail.get("id") or 0
        return out

    def delete_account(self, account_id):
        self._ensure()
        real_id = self._resolve_id(account_id)
        if not real_id:
            return {"code": 404, "message": "account not found"}
        resp = self._del("/account/delete", params={"accountId": real_id})
        return {"code": resp.get("code", 200), "message": resp.get("message")}

    # ---- emails

    def _resolve_id(self, value):
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
        s = normalize_email_addr(value)
        if "@" not in s:
            return None
        for row in self.list_accounts(size=500):
            if normalize_email_addr(row.get("email")) == s:
                return row.get("accountId")
        return None

    def list_emails(self, account_id, size=10):
        self._ensure()
        real_id = self._resolve_id(account_id)
        if not real_id:
            return []
        resp = self._get(
            "/email/list",
            params={"accountId": real_id, "size": size, "emailId": 0, "allReceive": 0, "timeSort": 0},
        )
        if resp.get("code") != 200:
            return []
        rows = ((resp.get("data") or {}).get("list")) or []
        return [self._normalize_email(row) for row in rows]

    def search_emails_by_recipient(self, to_email, size=10, account_id=None):
        target = normalize_email_addr(to_email)
        if not target:
            return []
        if account_id is None:
            account_id = self._resolve_id(target)
            if not account_id:
                return []
        return self.list_emails(account_id, size=size)

    @staticmethod
    def _normalize_email(row):
        return {
            "emailId": row.get("emailId") or row.get("id"),
            "accountEmail": row.get("toEmail"),
            "toEmail": row.get("toEmail"),
            "sendEmail": row.get("sendEmail"),
            "sender": row.get("name") or row.get("sendEmail"),
            "subject": row.get("subject") or "",
            "text": row.get("text") or "",
            "content": row.get("content") or "",
            "messageId": row.get("messageId"),
            "createTime": _parse_ts(row.get("createTime")),
        }

    def delete_emails_for(self, to_email):
        self._ensure()
        target = normalize_email_addr(to_email)
        rows = self.search_emails_by_recipient(target, size=100)
        ids = [str(r.get("emailId")) for r in rows if r.get("emailId")]
        if not ids:
            return 0
        resp = self._del("/email/delete", params={"emailIds": ",".join(ids)})
        return len(ids) if resp.get("code") == 200 else 0
