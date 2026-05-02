"""Mail provider base class + shared text helpers (OTP extraction, MIME parse)."""

from __future__ import annotations

import base64
import email as email_pkg
import html as html_lib
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from email.header import decode_header, make_header
from typing import Any

from autofree.config import EMAIL_POLL_INTERVAL, EMAIL_POLL_TIMEOUT

logger = logging.getLogger(__name__)


# Two-pass: first the labelled form ("verification code is 123456"),
# then a bare 6-digit fallback. Both case-insensitive.
_OTP_PATTERNS = (
    r"(?:temporary\s+(?:openai|chatgpt)\s+login\s+code(?:\s+is)?"
    r"|verification\s+code(?:\s+is)?"
    r"|login\s+code(?:\s+is)?"
    r"|code(?:\s+is)?"
    r"|验证码(?:为|是)?)\D{0,24}(\d{6})",
    r"\b(\d{6})\b",
)


# ------------------------------------------------------------ helpers


def normalize_email_addr(value: Any) -> str:
    return str(value or "").strip().lower()


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def decode_jwt_payload(jwt: str) -> dict:
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode("utf-8", errors="replace"))
    except Exception:
        return {}


def _part_to_text(part) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:
        try:
            return str(part.get_payload())
        except Exception:
            return ""


def parse_mime(raw: str | None) -> tuple[str, str, str, str, str, str]:
    """Return (subject, text, html, from_addr, to_addr, message_id)."""
    if not raw:
        return "", "", "", "", "", ""
    try:
        msg = email_pkg.message_from_string(raw)
    except Exception:
        return "", raw, "", "", "", ""

    subject = decode_mime_header(msg.get("Subject", ""))
    from_addr = decode_mime_header(msg.get("From", ""))
    to_addr = decode_mime_header(msg.get("To", ""))
    message_id = (msg.get("Message-ID") or "").strip()

    text_body = ""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            dispo = (part.get("Content-Disposition") or "").lower()
            if "attachment" in dispo:
                continue
            if ctype == "text/plain" and not text_body:
                text_body = _part_to_text(part)
            elif ctype == "text/html" and not html_body:
                html_body = _part_to_text(part)
    else:
        decoded = _part_to_text(msg)
        if msg.get_content_type() == "text/html":
            html_body = decoded
        else:
            text_body = decoded

    return subject, text_body, html_body, from_addr, to_addr, message_id


def html_to_visible_text(value: Any) -> str:
    content = str(value or "")
    if not content:
        return ""
    content = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", content)
    content = re.sub(r"(?is)<!--.*?-->", " ", content)
    content = re.sub(r"(?i)<br\s*/?>", "\n", content)
    content = re.sub(r"(?i)</(?:p|div|tr|table|h[1-6]|li|td|section|article)>", "\n", content)
    content = re.sub(r"(?s)<[^>]+>", " ", content)
    content = html_lib.unescape(content)
    content = re.sub(r"[\t\r\f\v ]+", " ", content)
    content = re.sub(r"\n\s+", "\n", content)
    content = re.sub(r"\n{2,}", "\n", content)
    return content.strip()


def extract_otp_from_email(email_data: dict) -> str | None:
    """Pull a 6-digit OTP from an email dict's text/content/subject."""
    sources: list[str] = []
    text = str(email_data.get("text") or "").strip()
    if text:
        sources.append(text)
    visible = html_to_visible_text(email_data.get("content"))
    if visible and visible not in sources:
        sources.append(visible)
    subj = str(email_data.get("subject") or "").strip()
    if subj:
        sources.append(subj)
    for source in sources:
        for pattern in _OTP_PATTERNS:
            m = re.search(pattern, source, re.IGNORECASE)
            if m:
                return m.group(1)
    return None


def extract_invite_link(email_data: dict) -> str | None:
    """Pull the first ChatGPT invite/auth link out of an email body."""
    html_body = str(email_data.get("content") or "")
    text = str(email_data.get("text") or "")
    for pattern in (
        r'href="(https://chatgpt\.com/auth/login\?[^"]*)"',
        r'(https://chatgpt\.com/auth/login\?[^\s<>"\']+)',
        r'https?://[^\s<>"\']+(?:invite|accept|join|workspace)[^\s<>"\']*',
    ):
        for source in (html_body, text):
            m = re.search(pattern, source, re.IGNORECASE)
            if m:
                return m.group(1) if m.groups() else m.group(0)
    return None


# ============================================================ MailProvider ABC


class MailProvider(ABC):
    """All mail backends implement this. Method names match the AutoTeam-Free
    interface so call sites stay compatible."""

    provider_name: str = "mail"

    # ---- auth ----
    @abstractmethod
    def login(self) -> str:
        """Initialise / verify auth. Returns an opaque token (logged only)."""

    # ---- account management ----
    @abstractmethod
    def create_temp_email(
        self, prefix: str | None = None, domain: str | None = None
    ) -> tuple[int | str, str]:
        """Returns (account_id, email)."""

    @abstractmethod
    def list_accounts(self, size: int = 200) -> list[dict]:
        """Return [{accountId, email, ...}, ...]."""

    @abstractmethod
    def delete_account(self, account_id: int | str) -> dict:
        """Accept numeric id OR email; returns {code, message?}."""

    # ---- email read ----
    @abstractmethod
    def search_emails_by_recipient(
        self, to_email: str, size: int = 10, account_id: int | str | None = None
    ) -> list[dict]:
        """Newest-first."""

    @abstractmethod
    def list_emails(self, account_id: int | str, size: int = 10) -> list[dict]:
        """By account id."""

    # ---- delete ----
    @abstractmethod
    def delete_emails_for(self, to_email: str) -> int:
        """Best-effort batch delete; returns count or 1 on bulk-success."""

    # ---- shared waiting / OTP ----
    def wait_for_email(
        self,
        to_email: str,
        timeout: int | None = None,
        sender_keyword: str | None = None,
        account_id: int | str | None = None,
    ) -> dict:
        """Poll until a matching message arrives, or raise TimeoutError."""
        timeout = timeout or EMAIL_POLL_TIMEOUT
        deadline = time.time() + timeout
        logger.info("[%s] 等待邮件 %s (超时 %ds)", self.provider_name, to_email, timeout)
        while time.time() < deadline:
            try:
                emails = self.search_emails_by_recipient(to_email, size=10, account_id=account_id)
            except Exception as exc:
                logger.warning("[%s] 轮询失败,稍后重试: %s", self.provider_name, exc)
                emails = []
            for em in emails:
                sender = (em.get("sendEmail") or em.get("sender") or "")
                if sender_keyword and sender_keyword.lower() not in str(sender).lower():
                    continue
                logger.info("[%s] 收到邮件: %s (from %s)", self.provider_name, em.get("subject"), sender)
                return em
            time.sleep(EMAIL_POLL_INTERVAL)
        raise TimeoutError(f"等待 {to_email} 邮件超时 ({timeout}s)")

    def wait_for_otp(
        self,
        to_email: str,
        timeout: int | None = None,
        sender_keyword: str | None = "openai",
        account_id: int | str | None = None,
    ) -> str:
        """Wait for an OTP-bearing email and return the 6-digit code.

        Polls every EMAIL_POLL_INTERVAL seconds, checking each new email's
        text/HTML/subject. Returns as soon as a matching code is found, even
        if a non-OTP email arrived first.
        """
        timeout = timeout or EMAIL_POLL_TIMEOUT
        deadline = time.time() + timeout
        seen_ids: set = set()
        while time.time() < deadline:
            try:
                emails = self.search_emails_by_recipient(to_email, size=10, account_id=account_id)
            except Exception as exc:
                logger.warning("[%s] OTP 轮询失败: %s", self.provider_name, exc)
                emails = []
            for em in emails:
                eid = em.get("emailId") or em.get("id")
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                if sender_keyword:
                    sender = str(em.get("sendEmail") or em.get("sender") or "").lower()
                    if sender_keyword.lower() not in sender:
                        continue
                code = extract_otp_from_email(em)
                if code:
                    logger.info("[%s] OTP 命中: %s", self.provider_name, code)
                    return code
            time.sleep(EMAIL_POLL_INTERVAL)
        raise TimeoutError(f"等待 {to_email} OTP 超时 ({timeout}s)")
