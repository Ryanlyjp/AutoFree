"""dreamhunter2333/cloudflare_temp_email backend.

Auth: x-admin-auth header.
Routes: /admin/new_address, /admin/delete_address/{id}, /admin/address,
        /admin/mails, /admin/mails/{id}, /admin/clear_inbox/{email}.
Mail body comes back as raw MIME in the `raw` field.
"""

from __future__ import annotations

import logging
import re
import uuid

import requests

from autofree.mail.base import MailProvider, decode_jwt_payload, normalize_email_addr, parse_mime
from autofree.settings import get_mail_config

logger = logging.getLogger(__name__)


class CfTempEmailClient(MailProvider):
    provider_name = "cf_temp_email"

    def __init__(self):
        cfg = get_mail_config("cf_temp_email")
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.admin_password = cfg.get("password") or ""
        self.default_domain = (cfg.get("domain") or "").lstrip("@").strip()
        self.session = requests.Session()
        self.token: str | None = None
        self._address_jwts: dict[str, str] = {}

    # ---- helpers

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "x-admin-auth": self.admin_password}

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _get(self, path, params=None):
        return self.session.get(self._url(path), headers=self._headers(), params=params, timeout=30)

    def _post(self, path, data=None):
        return self.session.post(self._url(path), headers=self._headers(), json=data, timeout=30)

    def _delete(self, path):
        return self.session.delete(self._url(path), headers=self._headers(), timeout=30)

    @staticmethod
    def _sanitize_prefix(prefix: str | None) -> str:
        if not prefix:
            return uuid.uuid4().hex[:10]
        cleaned = re.sub(r"[^A-Za-z0-9._]", "", str(prefix)).strip("._")
        return cleaned[:60] or uuid.uuid4().hex[:10]

    # ---- auth

    def login(self) -> str:
        if not self.base_url:
            raise Exception("cf_temp_email 未配置 base_url")
        if not self.admin_password:
            raise Exception("cf_temp_email 未配置 admin password")
        r = self._get("/admin/address", params={"limit": 1, "offset": 0})
        if r.status_code in (401, 403):
            raise Exception(f"cf_temp_email admin 密码无效 (HTTP {r.status_code})")
        if r.status_code != 200:
            raise Exception(f"cf_temp_email login 失败: HTTP {r.status_code} {(r.text or '')[:200]}")
        try:
            payload = r.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict) or "results" not in payload:
            raise Exception(
                "cf_temp_email 响应不像 dreamhunter2333 后端 — base_url 可能错配"
            )
        self.token = "admin-" + (self.admin_password[:6] if self.admin_password else "")
        return self.token

    # ---- accounts

    def create_temp_email(self, prefix=None, domain=None):
        domain = (domain or self.default_domain).lstrip("@").strip()
        if not domain:
            raise Exception("cf_temp_email 创建邮箱失败: 未配置注册域名")
        cleaned = self._sanitize_prefix(prefix)
        r = self._post("/admin/new_address", {"name": cleaned, "domain": domain, "enablePrefix": False})
        if r.status_code != 200:
            raise Exception(f"创建邮箱失败: HTTP {r.status_code} {(r.text or '')[:200]}")
        data = r.json() or {}
        if "address" not in data:
            raise Exception(f"cf_temp_email 创建邮箱响应缺 address: {data!r}")
        address = data.get("address")
        jwt = data.get("jwt") or ""
        payload = decode_jwt_payload(jwt) if jwt else {}
        address_id = data.get("address_id") or payload.get("address_id")
        if not address_id:
            try:
                listed = self._get("/admin/address", params={"limit": 1, "offset": 0, "query": address or cleaned})
                results = (listed.json() or {}).get("results") or []
                if results:
                    address_id = results[0].get("id")
                    address = address or results[0].get("name")
            except Exception:
                pass
        if jwt and address:
            self._address_jwts[normalize_email_addr(address)] = jwt
        logger.info("[cf_temp_email] 临时邮箱: %s id=%s", address, address_id)
        return address_id, address

    def list_accounts(self, size=200):
        r = self._get("/admin/address", params={"limit": size, "offset": 0})
        if r.status_code != 200:
            return []
        try:
            data = r.json() or {}
        except Exception:
            return []
        out = []
        for row in data.get("results", []):
            out.append(
                {
                    "accountId": row.get("id"),
                    "email": row.get("name"),
                    "createTime": row.get("created_at"),
                }
            )
        return out

    def delete_account(self, account_id):
        real_id = self._resolve_id(account_id)
        if not real_id:
            return {"code": 404, "message": "address not found"}
        r = self._delete(f"/admin/delete_address/{real_id}")
        if r.status_code == 200 and (r.json() or {}).get("success"):
            return {"code": 200}
        return {"code": r.status_code, "message": (r.text or "")[:200]}

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
        try:
            r = self._get("/admin/address", params={"limit": 5, "offset": 0, "query": s})
            for row in (r.json() or {}).get("results") or []:
                if normalize_email_addr(row.get("name")) == s:
                    return row.get("id")
        except Exception:
            return None
        return None

    def _resolve_email(self, account_id):
        if not account_id:
            return None
        try:
            int(account_id)
        except (TypeError, ValueError):
            return str(account_id) if "@" in str(account_id) else None
        try:
            r = self._get("/admin/address", params={"limit": 50, "offset": 0})
            for row in (r.json() or {}).get("results") or []:
                if str(row.get("id")) == str(account_id):
                    return row.get("name")
        except Exception:
            return None
        return None

    def _normalize_mail(self, row):
        raw = row.get("raw") or ""
        subject, text, html_body, from_addr, to_addr, message_id = parse_mime(raw)
        return {
            "emailId": row.get("id"),
            "accountEmail": row.get("address"),
            "toEmail": to_addr or row.get("address"),
            "sendEmail": row.get("source") or from_addr,
            "sender": from_addr,
            "subject": subject,
            "text": text,
            "content": html_body,
            "messageId": message_id,
            "createTime": row.get("created_at"),
        }

    def list_emails(self, account_id, size=10):
        target = (
            account_id if isinstance(account_id, str) and "@" in account_id else self._resolve_email(account_id)
        )
        if not target:
            return []
        return self.search_emails_by_recipient(target, size=size, account_id=account_id)

    def search_emails_by_recipient(self, to_email, size=10, account_id=None):
        target = normalize_email_addr(to_email)
        if not target:
            return []
        r = self._get("/admin/mails", params={"limit": size, "offset": 0, "address": target})
        if r.status_code != 200:
            return []
        out = []
        for row in (r.json() or {}).get("results") or []:
            if normalize_email_addr(row.get("address")) and normalize_email_addr(row.get("address")) != target:
                continue
            out.append(self._normalize_mail(row))
        return out

    def delete_emails_for(self, to_email):
        target = normalize_email_addr(to_email)
        if not target:
            return 0
        r = self._delete(f"/admin/clear_inbox/{target}")
        if r.status_code == 200 and (r.json() or {}).get("success"):
            return 1
        deleted = 0
        for mail in self.search_emails_by_recipient(target, size=100):
            mid = mail.get("emailId")
            if mid:
                try:
                    if self._delete(f"/admin/mails/{mid}").status_code == 200:
                        deleted += 1
                except Exception:
                    pass
        return deleted
