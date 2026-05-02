"""self-hosted tempmail backend (/opt/code-server/project/tempmail).

Auth: Authorization: Bearer <api_key>.
Mailbox ID is a UUID (not a numeric int). Server extracts OTP server-side via
GET /api/mailboxes/:id/otp/latest, which we use as a fast-path before falling
back to the generic regex pipeline in base.py.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

import requests

from autofree.config import EMAIL_POLL_INTERVAL, EMAIL_POLL_TIMEOUT
from autofree.mail.base import MailProvider, normalize_email_addr
from autofree.settings import get_mail_config

logger = logging.getLogger(__name__)


class TempmailClient(MailProvider):
    provider_name = "tempmail"

    def __init__(self):
        cfg = get_mail_config("tempmail")
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.api_key = cfg.get("api_key") or ""
        self.default_domain = (cfg.get("domain") or "").lstrip("@").strip()
        self.session = requests.Session()
        # email (lower) -> mailbox UUID, so callers can pass email and we know the id.
        self._mailbox_ids: dict[str, str] = {}

    # ---- helpers

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _get(self, path, params=None):
        return self.session.get(self._url(path), headers=self._headers(), params=params, timeout=30)

    def _post(self, path, body=None):
        return self.session.post(self._url(path), headers=self._headers(), json=body or {}, timeout=30)

    def _put(self, path, body=None):
        return self.session.put(self._url(path), headers=self._headers(), json=body or {}, timeout=30)

    def _delete(self, path):
        return self.session.delete(self._url(path), headers=self._headers(), timeout=30)

    @staticmethod
    def _sanitize_prefix(prefix: str | None) -> str:
        if not prefix:
            return uuid.uuid4().hex[:10]
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "", str(prefix)).strip(".-_")
        return cleaned[:60] or uuid.uuid4().hex[:10]

    @staticmethod
    def _parse_or_raise(r: requests.Response, what: str) -> dict:
        if r.status_code == 401 or r.status_code == 403:
            raise Exception(f"tempmail {what}: {r.status_code} 鉴权失败,检查 api_key")
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or (r.text or "")[:200]
            except Exception:
                msg = (r.text or "")[:200]
            raise Exception(f"tempmail {what}: HTTP {r.status_code} {msg}")
        try:
            return r.json() or {}
        except Exception as e:
            raise Exception(f"tempmail {what} 响应非 JSON: {e}")

    # ---- auth

    def login(self) -> str:
        if not self.base_url:
            raise Exception("tempmail 未配置 base_url")
        if not self.api_key:
            raise Exception("tempmail 未配置 api_key")
        # /api/me requires auth and is cheap.
        r = self._get("/api/me")
        if r.status_code in (401, 403):
            raise Exception(f"tempmail api_key 无效 (HTTP {r.status_code})")
        if r.status_code != 200:
            raise Exception(f"tempmail login 失败: HTTP {r.status_code} {(r.text or '')[:200]}")
        return f"key-{self.api_key[:6]}"

    # ---- accounts (mailboxes)

    def create_temp_email(self, prefix=None, domain=None):
        domain = (domain or self.default_domain).lstrip("@").strip()
        body: dict = {"source": "api"}
        if prefix:
            body["address"] = self._sanitize_prefix(prefix)
        if domain:
            body["domain"] = domain
        r = self._post("/api/mailboxes", body)
        if r.status_code in (200, 201):
            mailbox = (r.json() or {}).get("mailbox") or {}
        else:
            # surface server message
            self._parse_or_raise(r, "create_temp_email")
            return None, None  # unreachable
        mid = mailbox.get("id") or mailbox.get("uuid")
        full = mailbox.get("full_address") or mailbox.get("fullAddress") or ""
        if not mid or not full:
            raise Exception(f"tempmail 创建邮箱响应缺字段: {mailbox!r}")
        self._mailbox_ids[normalize_email_addr(full)] = str(mid)
        logger.info("[tempmail] 临时邮箱: %s id=%s", full, mid)
        return mid, full

    def list_accounts(self, size=200):
        r = self._get("/api/mailboxes", params={"size": min(size, 100)})
        data = self._parse_or_raise(r, "list_accounts")
        # tempmail Mailbox model field names: id / full_address / is_favorite / created_at / expires_at
        out = []
        for row in data.get("data") or []:
            out.append(
                {
                    "accountId": row.get("id"),
                    "email": row.get("full_address"),
                    "createTime": row.get("created_at"),
                    "expiresAt": row.get("expires_at"),
                    "favorite": row.get("is_favorite"),
                }
            )
            email = out[-1]["email"]
            if email:
                self._mailbox_ids[normalize_email_addr(email)] = str(out[-1]["accountId"])
        return out

    def delete_account(self, account_id):
        real_id = self._resolve_id(account_id)
        if not real_id:
            return {"code": 404, "message": "mailbox not found"}
        r = self._delete(f"/api/mailboxes/{real_id}")
        if r.status_code in (200, 204):
            return {"code": 200}
        return {"code": r.status_code, "message": (r.text or "")[:200]}

    # ---- emails

    def _resolve_id(self, value) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        # Already a UUID-ish id (contains dashes, no @).
        if "@" not in s:
            return s
        cached = self._mailbox_ids.get(normalize_email_addr(s))
        if cached:
            return cached
        for row in self.list_accounts(size=500):
            if normalize_email_addr(row.get("email")) == normalize_email_addr(s):
                return str(row.get("accountId"))
        return None

    @staticmethod
    def _normalize_email(row, recipient: str | None = None):
        # tempmail Email model: id / sender / subject / body_text / body_html /
        # received_at. The recipient lives on the parent Mailbox, not the Email
        # itself — caller can pass it in if known.
        return {
            "emailId": row.get("id"),
            "accountEmail": recipient,
            "toEmail": recipient,
            "sendEmail": row.get("sender"),
            "sender": row.get("sender"),
            "subject": row.get("subject") or "",
            "text": row.get("body_text") or "",
            "content": row.get("body_html") or "",
            "messageId": "",
            "createTime": row.get("received_at"),
        }

    def list_emails(self, account_id, size=10, recipient=None):
        real_id = self._resolve_id(account_id)
        if not real_id:
            return []
        r = self._get(f"/api/mailboxes/{real_id}/emails", params={"size": min(size, 100)})
        if r.status_code != 200:
            return []
        data = r.json() or {}
        return [self._normalize_email(row, recipient=recipient) for row in (data.get("data") or [])]

    def search_emails_by_recipient(self, to_email, size=10, account_id=None):
        # tempmail scopes emails by mailbox id, not recipient string.
        real_id = self._resolve_id(account_id) if account_id else self._resolve_id(to_email)
        if not real_id:
            return []
        return self.list_emails(real_id, size=size, recipient=to_email)

    def delete_emails_for(self, to_email):
        real_id = self._resolve_id(to_email)
        if not real_id:
            return 0
        rows = self.list_emails(real_id, size=100)
        deleted = 0
        for row in rows:
            eid = row.get("emailId")
            if not eid:
                continue
            try:
                if self._delete(f"/api/mailboxes/{real_id}/emails/{eid}").status_code in (200, 204):
                    deleted += 1
            except Exception:
                pass
        return deleted

    # ---- fast OTP path: tempmail does server-side extraction

    def wait_for_otp(self, to_email, timeout=None, sender_keyword="openai", account_id=None):
        timeout = timeout or EMAIL_POLL_TIMEOUT
        real_id = self._resolve_id(account_id) if account_id else self._resolve_id(to_email)
        if not real_id:
            return super().wait_for_otp(to_email, timeout=timeout, sender_keyword=sender_keyword, account_id=account_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self._get(f"/api/mailboxes/{real_id}/otp/latest")
            except Exception as exc:
                logger.warning("[tempmail] otp/latest 异常: %s", exc)
                r = None
            if r is not None and r.status_code == 200:
                otp = ((r.json() or {}).get("otp") or {})
                code = otp.get("code")
                sender = str(otp.get("sender") or "").lower()
                if code and (not sender_keyword or sender_keyword.lower() in sender):
                    logger.info("[tempmail] OTP 命中: %s", code)
                    return code
            time.sleep(EMAIL_POLL_INTERVAL)
        # fallback to base regex pipeline if server endpoint never matched
        return super().wait_for_otp(to_email, timeout=5, sender_keyword=sender_keyword, account_id=real_id)
