"""Playwright flow — register a free ChatGPT account, then OAuth into the
Personal workspace to harvest a `plan_type=free` Codex auth token.

Adapted from `/opt/code-server/project/team/daily-playwright.py` with three
behaviour changes for the AutoFree CTF use case:

  1. **OTP comes from the mail backend, not stdin**.
     Caller passes `mail_client` to the Flow constructor; the `_wait_otp`
     helper polls the inbox via `MailProvider.wait_for_otp`.

  2. **Workspace selection forces personal**, not `workspaces[0]`.
     `_select_personal_workspace` filters `workspaces[]` for entries that
     look personal (structure ∈ personal*, plan_type=free, is_personal=True)
     and POSTs `/api/accounts/workspace/select` with that id. If no personal
     workspace appears, raises so the runner can mark the cohort failed.

  3. **No interactive breakpoints / `_prompt_yes` consent**. `breakpoint()`
     becomes a no-op log line; consent is auto-clicked.

The HTTP / sentinel / PKCE / cookie machinery is preserved verbatim — those
pieces went through real-traffic verification in the source script.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import re
import secrets
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from playwright.sync_api import sync_playwright

from autofree.config import EMAIL_POLL_TIMEOUT
from autofree.mail.base import MailProvider

logger = logging.getLogger(__name__)


# ============================================================ constants

BASE = "https://chatgpt.com"
AUTH = "https://auth.openai.com"
OAUTH_ISSUER = AUTH

# Codex CLI public client — same id used by the official codex tool.
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"

PLAYWRIGHT_HEADLESS_DEFAULT = True
PLAYWRIGHT_SLOW_MO_MS = 50
PLAYWRIGHT_TIMEOUT_MS = 45000

SENTINEL_BASE = "https://sentinel.openai.com"
SENTINEL_SDK_VERSION = "20260219f9f6"
SENTINEL_FRAME_URL = f"{SENTINEL_BASE}/backend-api/sentinel/frame.html?sv={SENTINEL_SDK_VERSION}"

_CHROME_PROFILES = [
    {"major": 131, "build": 6778, "patch": (69, 205), "ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'},
    {"major": 133, "build": 6943, "patch": (33, 153), "ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"'},
    {"major": 136, "build": 7103, "patch": (48, 175), "ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"'},
]


# ============================================================ workspace classification


_PERSONAL_STRUCTURES = ("personal", "personal_v2", "personal_account")


def _is_personal_workspace(item: Any) -> bool:
    """Three-way OR: structure ∈ personal*, plan_type=free, or is_personal=True."""
    if not isinstance(item, dict):
        return False
    structure = str(item.get("structure") or "").lower()
    if structure in _PERSONAL_STRUCTURES:
        return True
    if str(item.get("plan_type") or "").lower() == "free":
        return True
    if item.get("is_personal") is True:
        return True
    return False


# ============================================================ helpers


def _random_chrome_version() -> tuple[str, str, str]:
    p = random.choice(_CHROME_PROFILES)
    patch = random.randint(*p["patch"])
    full = f"{p['major']}.0.{p['build']}.{patch}"
    ua = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full} Safari/537.36"
    )
    return full, ua, p["ua"]


def _normalize_proxy(proxy: str | None) -> str | None:
    raw = str(proxy or "").strip()
    if not raw:
        return None
    return raw if "://" in raw else f"http://{raw}"


def _extract_code(url: str) -> str | None:
    try:
        return parse_qs(urlparse(url).query).get("code", [None])[0]
    except Exception:
        return None


def _generate_pkce() -> tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def _trace_headers() -> dict[str, str]:
    trace_id = random.randint(10**17, 10**18 - 1)
    parent_id = random.randint(10**17, 10**18 - 1)
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(parent_id, '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": str(trace_id),
        "x-datadog-parent-id": str(parent_id),
    }


def _extract_direct_token(raw: Any) -> str | None:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, dict):
        for key in ("token", "sentinel", "sentinel_token", "sentinelToken", "value", "result"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _extract_triplet(raw: Any) -> dict | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    p = str(raw.get("p") or raw.get("pow") or raw.get("proof") or "").strip()
    t = str(raw.get("t") or raw.get("turnstile") or raw.get("turnstile_token") or "").strip()
    c = str(raw.get("c") or raw.get("challenge") or raw.get("challenge_token") or raw.get("token") or "").strip()
    return {"p": p, "t": t, "c": c} if p and c else None


# ============================================================ Flow


class FlowError(Exception):
    pass


class Flow:
    """One Playwright session encapsulating a single account's register/oauth lifecycle."""

    def __init__(
        self,
        proxy: str | None = None,
        tag: str = "",
        headless: bool = PLAYWRIGHT_HEADLESS_DEFAULT,
        mail_client: MailProvider | None = None,
        otp_timeout: int = EMAIL_POLL_TIMEOUT,
        log_emitter=None,
        master_account_id: str | None = None,
    ):
        self.tag = tag
        self.proxy = _normalize_proxy(proxy)
        self.headless = bool(headless)
        self.mail_client = mail_client
        self.otp_timeout = int(otp_timeout)
        # Callback (line: str, level: str) -> None for surfacing per-step
        # progress to the run log so the web UI shows fine-grained detail.
        self.log_emitter = log_emitter
        # The master Team workspace id — used to *exclude* it when picking
        # personal in OAuth; the personal workspace is "the other one".
        self.master_account_id = (master_account_id or "").strip().lower() or None
        # Recorded by oauth_personal so the runner can show why we couldn't
        # finish (workspace missing, OTP timeout, consent stuck, ...).
        self.oauth_fail_reason: str = ""

        self.device_id = str(uuid.uuid4())
        self.auth_session_logging_id = str(uuid.uuid4())
        self.chrome_full, self.ua, self.sec_ch_ua = _random_chrome_version()
        self.accept_language = random.choice(
            ["en-US,en;q=0.9", "en-US,en;q=0.9,zh-CN;q=0.8", "en,en-US;q=0.9"]
        )

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.sentinel_page = None
        self.api = None

        self.callback_url = ""
        self.captured_code: str | None = None
        self.captured_code_url: str = ""

        self.last_oauth_continue_url = ""
        self.last_oauth_continue_source = ""
        self.last_oauth_continue_at = 0.0
        self.last_otp_url = ""

        self._sentinel_bundle_loaded = False
        self._sentinel_flow_tokens: dict[str, str] = {}
        self._sentinel_flow_so_tokens: dict[str, str] = {}

        # The mailbox id used by mail_client.wait_for_otp — set when caller
        # creates the mailbox via mail.create_temp_email() and passes it to
        # set_mail_context().
        self._mailbox_id: int | str | None = None

    # ============================================================ public

    def p(self, msg: str, level: str = "info") -> None:
        prefix = f"[{self.tag}] " if self.tag else ""
        line = prefix + msg
        getattr(logger, level if level in ("debug", "info", "warning", "error") else "info")(line)
        # Mirror to run log so the web UI shows step-by-step.
        if self.log_emitter:
            try:
                self.log_emitter(line, level)
            except Exception:
                pass

    def set_mail_context(self, mailbox_id: int | str | None) -> None:
        """Tell the flow which mailbox to poll for OTPs.
        Optional — `_wait_otp` falls back to address-based search."""
        self._mailbox_id = mailbox_id

    def start(self) -> None:
        self.playwright = sync_playwright().start()
        launch: dict[str, Any] = {"headless": self.headless, "slow_mo": PLAYWRIGHT_SLOW_MO_MS}
        if self.proxy:
            launch["proxy"] = {"server": self.proxy}
        self.browser = self.playwright.chromium.launch(**launch)
        self.context = self.browser.new_context(
            user_agent=self.ua,
            locale="en-US",
            viewport={"width": 1600, "height": 980},
            ignore_https_errors=True,
        )
        self.context.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
        self._prime_cookies()
        self.page = self.context.new_page()
        self.sentinel_page = self.context.new_page()
        self._hook_code_capture(self.page)
        self._hook_code_capture(self.sentinel_page)
        try:
            self.sentinel_page.goto(SENTINEL_FRAME_URL, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
            self.sentinel_page.wait_for_timeout(3000)
        except Exception as e:
            self.p(f"[Sentinel] open failed: {e}")
        self._sync_api_from_browser()
        self.p(f"[Playwright] ready proxy={self.proxy or 'none'} headless={self.headless}")

    def close(self, keep_open: bool = False) -> None:
        for name in ("api", "sentinel_page", "page", "context", "browser"):
            obj = getattr(self, name, None)
            if not obj:
                continue
            try:
                if name == "api":
                    obj.dispose()
                else:
                    obj.close()
            except Exception:
                pass
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass

    # ============================================================ cookies / browser↔api bridge

    def _prime_cookies(self) -> None:
        cookies = []
        for raw in (BASE, AUTH, OAUTH_ISSUER):
            host = urlparse(raw).hostname or ""
            for domain in (host, f".{host}"):
                cookies.append({
                    "name": "oai-did",
                    "value": self.device_id,
                    "domain": domain,
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                })
        self.context.add_cookies(cookies)

    def _hook_code_capture(self, page) -> None:
        def remember(url: str) -> None:
            code = _extract_code(url)
            if code:
                self.captured_code = code
                self.captured_code_url = url
                self.p(f"[OAuth] captured code url: {url[:220]}")

        def remember_auth_payload(resp) -> None:
            try:
                url = str(resp.url or "").strip()
                if OAUTH_ISSUER not in url or "/api/accounts/" not in url:
                    return
                data = resp.json()
            except Exception:
                return
            self._remember_oauth_continue(url, data)

        def on_response(resp):
            remember(resp.url)
            remember_auth_payload(resp)

        def on_request_finished(req):
            try:
                resp = req.response()
                if resp:
                    remember(resp.url)
            except Exception:
                pass

        page.on("response", on_response)
        page.on("requestfinished", on_request_finished)

    def _sync_api_from_browser(self) -> None:
        if self.api:
            try:
                self.api.dispose()
            except Exception:
                pass
        kwargs: dict[str, Any] = {
            "storage_state": self.context.storage_state(),
            "ignore_https_errors": True,
            "user_agent": self.ua,
            "timeout": PLAYWRIGHT_TIMEOUT_MS,
            "fail_on_status_code": False,
            "extra_http_headers": {
                "Accept-Language": self.accept_language,
                "sec-ch-ua": self.sec_ch_ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        }
        if self.proxy:
            kwargs["proxy"] = {"server": self.proxy}
        self.api = self.playwright.request.new_context(**kwargs)

    def _sync_browser_from_api(self) -> None:
        try:
            cookies = self.api.storage_state().get("cookies") or []
            if cookies:
                self.context.add_cookies(cookies)
        except Exception as e:
            self.p(f"[WARN] cookie sync failed: {e}")

    def _std(self) -> dict[str, str]:
        return {
            "User-Agent": self.ua,
            "Accept-Language": self.accept_language,
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

    def _json_headers(self, referer: str, origin: str) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": referer,
            "Origin": origin,
            "oai-device-id": self.device_id,
        }
        h.update(self._std())
        h.update(_trace_headers())
        return h

    # ============================================================ HTTP

    def _api_call(self, method: str, url: str, step: str = "", max_redirects: int = 20, **kwargs) -> dict[str, Any]:
        self._sync_api_from_browser()
        kwargs.setdefault("timeout", PLAYWRIGHT_TIMEOUT_MS)
        kwargs.setdefault("ignore_https_errors", True)
        kwargs.setdefault("fail_on_status_code", False)
        kwargs.setdefault("max_redirects", max_redirects)
        if "json_body" in kwargs:
            kwargs["data"] = json.dumps(kwargs.pop("json_body"))
        fn = getattr(self.api, method.lower())
        resp = fn(url, **kwargs)
        text = ""
        data: Any = None
        try:
            text = resp.text()
        except Exception:
            pass
        try:
            data = resp.json()
        except Exception:
            data = None
        out = {
            "status": int(resp.status),
            "url": str(resp.url),
            "text": text,
            "json": data,
            "headers": {str(k).lower(): str(v) for k, v in dict(resp.headers or {}).items()},
        }
        try:
            resp.dispose()
        except Exception:
            pass
        self._sync_browser_from_api()
        if step:
            self.p(f"[{step}] {method.upper()} {url} -> {out['status']}")
        return out

    def goto(self, url: str, referer: str | None = None) -> str:
        self.captured_code = None
        self.captured_code_url = ""
        try:
            self.page.goto(url, referer=referer, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
        finally:
            self._sync_api_from_browser()
        return self.page.url

    # ============================================================ OAuth-continue tracking

    def _abs_auth_url(self, url: str) -> str:
        raw = str(url or "").strip()
        if not raw:
            return ""
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        if raw.startswith("/"):
            return f"{AUTH}{raw}"
        return raw

    def _clear_oauth_continue(self) -> None:
        self.last_oauth_continue_url = ""
        self.last_oauth_continue_source = ""
        self.last_oauth_continue_at = 0.0

    def _remember_oauth_continue(self, source_url: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        next_url = str(data.get("continue_url") or data.get("url") or data.get("redirect_url") or "").strip()
        if not next_url:
            return
        target = self._abs_auth_url(next_url)
        if not target:
            return
        self.last_oauth_continue_url = target
        self.last_oauth_continue_source = str(source_url or "").strip()
        self.last_oauth_continue_at = time.time()

    def _consume_oauth_continue(self, max_age: float = 12.0) -> tuple[str, str]:
        target = self._abs_auth_url(self.last_oauth_continue_url)
        age = time.time() - float(self.last_oauth_continue_at or 0.0)
        source = self.last_oauth_continue_source
        self._clear_oauth_continue()
        if not target or age > float(max_age or 0):
            return "", ""
        return target, source

    def _follow_browser_continue(self, referer: str | None = None, max_age: float = 12.0) -> str | None:
        target, source = self._consume_oauth_continue(max_age=max_age)
        if not target:
            return None
        self.p(f"[OAuth] follow browser continue from {source or '-'} -> {target[:220]}")
        return (
            _extract_code(target)
            or self._follow_for_code(target, referer=referer)[0]
            or self._allow_redirect_code(target, referer=referer)
        )

    # ============================================================ OTP plumbing (via mail_client)

    def _wait_otp(self, email: str) -> str:
        """Pull the next OTP for `email` from the mail backend.

        Logs which mailbox id we're polling and how long the timeout is, so
        the run log clearly distinguishes "OTP not arrived" from "OTP wrong"
        from "code wasn't extracted".
        """
        if not self.mail_client:
            raise FlowError("Flow.mail_client 未注入,无法拉 OTP")
        provider = getattr(self.mail_client, "provider_name", "?")
        self.p(
            f"  [OTP] 开始轮询邮箱后端({provider}) target={email} "
            f"mailbox_id={self._mailbox_id} timeout={self.otp_timeout}s"
        )
        t0 = time.time()
        try:
            code = self.mail_client.wait_for_otp(
                email,
                timeout=self.otp_timeout,
                sender_keyword="openai",
                account_id=self._mailbox_id,
            )
        except TimeoutError as exc:
            elapsed = time.time() - t0
            self.p(
                f"  [OTP] ✗ 等了 {elapsed:.1f}s 仍未拿到 OTP — {exc}。"
                " 可能原因: ① OpenAI 没发邮件(检查 send_otp 状态码) "
                "② 域名 MX 没指向后端 ③ OpenAI 标黑此 IP/域 ④ 后端 OTP 提取正则没匹配",
                "error",
            )
            raise
        elapsed = time.time() - t0
        if not (isinstance(code, str) and re.fullmatch(r"\d{6}", code)):
            self.p(f"  [OTP] ✗ 后端返回非 6 位 OTP: {code!r}", "error")
            raise FlowError(f"mail backend 返回非 6 位 OTP: {code!r}")
        self.p(f"  [OTP] ✓ {elapsed:.1f}s 内拿到验证码 {code}")
        return code

    def _client_auth_session_dump(self, referer: str | None = None) -> dict[str, Any]:
        """GET /api/accounts/client_auth_session_dump — browser calls this
        between OAuth steps to advance server-side state machine.

        Captured in auth.har between authorize/continue → email-otp/validate
        and between email-otp/validate → workspace/select. Skipping it makes
        the next POST 409 'Invalid session. Please start over.'
        """
        r = self._api_call(
            "get",
            f"{OAUTH_ISSUER}/api/accounts/client_auth_session_dump",
            step="session-dump",
            headers={
                "Accept": "application/json",
                "Referer": referer or getattr(self.page, "url", "") or f"{OAUTH_ISSUER}/log-in",
                **self._std(),
            },
            max_redirects=0,
        )
        return r.get("json") or {}

    def _request_otp_resend(self, referer: str | None = None, *, why: str = "manual") -> int:
        self.p(f"  [OTP] → POST /api/accounts/email-otp/send (要求 OpenAI 发送 OTP, why={why})")
        r = self._api_call(
            "get",
            f"{AUTH}/api/accounts/email-otp/send",
            step="OTP Send",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": referer or getattr(self.page, "url", "") or f"{AUTH}/email-verification",
                "Upgrade-Insecure-Requests": "1",
                **self._std(),
            },
        )
        self.last_otp_url = self._abs_auth_url(r["url"]) or self.last_otp_url
        if r["status"] == 200:
            self.p(f"  [OTP] ✓ OpenAI 已接受 send 请求(HTTP 200),邮件应该数秒内到达")
        else:
            preview = (r.get("text") or "")[:200]
            self.p(
                f"  [OTP] ⚠ send-otp HTTP {r['status']}: {preview!r} "
                f"— 这通常意味着 OpenAI 拒绝下发 OTP(IP 风控/账号异常)",
                "warning",
            )
        return r["status"]

    # ============================================================ Sentinel

    def _ensure_sentinel_bundle(self) -> None:
        if self._sentinel_bundle_loaded:
            return
        self._sentinel_bundle_loaded = True
        try:
            result = self.sentinel_page.evaluate(
                """async (flows) => {
                    const out = {};
                    const sdk = window.SentinelSDK || window.sentinelSDK || window.__SentinelSDK || (window.openai && window.openai.SentinelSDK);
                    if (!sdk) return out;
                    for (const flow of flows) {
                        let tokenRaw = null, soRaw = null, error = null;
                        try {
                            if (typeof sdk.init === "function") await sdk.init(flow);
                            tokenRaw = await sdk.token(flow);
                        } catch (e) { error = String((e && e.message) || e || "token failed"); }
                        try {
                            if (typeof sdk.sessionObserverToken === "function") {
                                soRaw = await sdk.sessionObserverToken(flow);
                            }
                        } catch (e) { if (!error) error = String((e && e.message) || e || "so failed"); }
                        out[flow] = { tokenRaw, soRaw, error };
                    }
                    return out;
                }""",
                [
                    "authorize_continue",
                    "username_password_create",
                    "password_verify",
                    "oauth_create_account",
                    "email_otp_verification",
                ],
            )
        except Exception as e:
            self.p(f"[Sentinel] preload bundle failed: {e}")
            return
        if not isinstance(result, dict):
            return
        for flow, data in result.items():
            if not isinstance(data, dict):
                continue
            token = _extract_direct_token(data.get("tokenRaw"))
            if not token:
                tri = _extract_triplet(data.get("tokenRaw"))
                if tri and tri.get("p") and tri.get("c"):
                    tri["id"] = self.device_id
                    tri["flow"] = flow
                    token = json.dumps(tri, separators=(",", ":"))
            so = _extract_direct_token(data.get("soRaw")) or ""
            if token:
                self._sentinel_flow_tokens[str(flow)] = token
            if so:
                self._sentinel_flow_so_tokens[str(flow)] = so

    def _resolve_sentinel_token(self, flow: str, fallback_flow: str | None = None) -> str | None:
        self._ensure_sentinel_bundle()
        flow = str(flow or "").strip()
        fallback_flow = str(fallback_flow or "").strip()
        token = self._sentinel_flow_tokens.get(flow) or (
            self._sentinel_flow_tokens.get(fallback_flow) if fallback_flow else None
        )
        if token:
            return token
        try:
            result = self.sentinel_page.evaluate(
                """async ({flow,deviceId}) => {
                    const sdk = window.SentinelSDK || window.sentinelSDK || window.__SentinelSDK || (window.openai && window.openai.SentinelSDK);
                    if (!sdk || typeof sdk.token !== 'function') return null;
                    const lang = navigator.language || 'en-US';
                    const caps = JSON.stringify({is_passkey_supported:false,is_platform_authenticator_available:false,is_conditional_mediation_available:false});
                    const tries = [
                        () => sdk.token({flow,id:deviceId}),
                        () => sdk.token({flow,id:deviceId,'data-build':lang}),
                        () => sdk.token({flow,id:deviceId,dataBuild:lang}),
                        () => sdk.token({flow,id:deviceId,'data-build':lang,'ext-passkey-client-capabilities':caps}),
                        () => sdk.token(flow),
                        () => sdk.token({flow}),
                        () => sdk.token(),
                    ];
                    for (const fn of tries) { try { return await fn(); } catch(e) {} }
                    return null;
                }""",
                {"flow": flow, "deviceId": self.device_id},
            )
        except Exception as e:
            self.p(f"[Sentinel] token({flow}) failed: {e}")
            return None
        direct = _extract_direct_token(result)
        if direct:
            return direct
        tri = _extract_triplet(result)
        if tri and tri.get("p") and tri.get("c"):
            tri["id"] = self.device_id
            tri["flow"] = flow
            return json.dumps(tri, separators=(",", ":"))
        return None

    def _resolve_sentinel_so_token(self, flow: str) -> str:
        self._ensure_sentinel_bundle()
        token = self._sentinel_flow_so_tokens.get(str(flow or "").strip())
        if token:
            return token
        try:
            return self.sentinel_page.evaluate(
                """async (flow) => {
                    const sdk = window.SentinelSDK || window.sentinelSDK || window.__SentinelSDK || (window.openai && window.openai.SentinelSDK);
                    if (!sdk || typeof sdk.sessionObserverToken !== 'function') return '';
                    try { return await sdk.sessionObserverToken(flow); } catch(e) { return ''; }
                }""",
                flow,
            ) or ""
        except Exception:
            return ""

    # ============================================================ register flow (gpt.har)

    def _visit_homepage(self) -> None:
        self.goto(f"{BASE}/")

    def _get_csrf(self) -> str:
        r = self._api_call(
            "get",
            f"{BASE}/api/auth/csrf",
            step="csrf",
            headers={"Accept": "application/json", "Referer": f"{BASE}/", **self._std()},
        )
        token = str((r["json"] or {}).get("csrfToken") or "").strip()
        if not token:
            raise FlowError("csrfToken missing")
        return token

    def _signin(self, email: str, csrf: str) -> str:
        r = self._api_call(
            "post",
            f"{BASE}/api/auth/signin/openai",
            step="signin",
            params={
                "prompt": "login",
                "ext-oai-did": self.device_id,
                "auth_session_logging_id": self.auth_session_logging_id,
                "screen_hint": "login_or_signup",
                "login_hint": email,
            },
            form={"callbackUrl": f"{BASE}/", "csrfToken": csrf, "json": "true"},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "Referer": f"{BASE}/",
                "Origin": BASE,
                **self._std(),
            },
        )
        url = str((r["json"] or {}).get("url") or "").strip()
        if not url:
            raise FlowError("authorize url missing")
        return url

    def _authorize(self, url: str) -> str:
        return self.goto(url, referer=f"{BASE}/")

    def _register(self, email: str, password: str) -> tuple[int, dict]:
        headers = self._json_headers(f"{AUTH}/create-account/password", AUTH)
        token = self._resolve_sentinel_token("username_password_create")
        if token:
            headers["openai-sentinel-token"] = token
        r = self._api_call(
            "post",
            f"{AUTH}/api/accounts/user/register",
            step="register",
            params={
                "ext-passkey-client-capabilities": json.dumps(
                    {
                        "is_passkey_supported": False,
                        "is_platform_authenticator_available": False,
                        "is_conditional_mediation_available": False,
                    },
                    separators=(",", ":"),
                )
            },
            json_body={"username": email, "password": password},
            headers=headers,
        )
        return r["status"], (r["json"] or {"text": (r["text"] or "")[:300]})

    def _send_otp(self) -> int:
        return self._request_otp_resend(referer=f"{AUTH}/create-account/password")

    def _validate_register_otp(self, code: str) -> tuple[int, dict]:
        headers = self._json_headers(f"{AUTH}/email-verification", AUTH)
        token = self._resolve_sentinel_token("email_otp_verification")
        if token:
            headers["openai-sentinel-token"] = token
        r = self._api_call(
            "post",
            f"{AUTH}/api/accounts/email-otp/validate",
            step="validate-otp",
            json_body={"code": code},
            headers=headers,
        )
        return r["status"], (r["json"] or {"text": (r["text"] or "")[:300]})

    def _create_account(self, name: str, birthdate: str) -> tuple[int, dict]:
        headers = self._json_headers(f"{AUTH}/about-you", AUTH)
        token = self._resolve_sentinel_token("oauth_create_account", "create_account")
        if token:
            headers["openai-sentinel-token"] = token
        so = self._resolve_sentinel_so_token("oauth_create_account")
        if so:
            headers["openai-sentinel-so-token"] = so
        r = self._api_call(
            "post",
            f"{AUTH}/api/accounts/create_account",
            step="create-account",
            json_body={"name": name, "birthdate": birthdate},
            headers=headers,
        )
        data = r["json"] or {"text": (r["text"] or "")[:300]}
        if isinstance(data, dict):
            self.callback_url = data.get("continue_url") or data.get("url") or data.get("redirect_url") or ""
        return r["status"], data

    def _consume_callback(self, url: str | None = None) -> None:
        url = url or self.callback_url
        if not url:
            return
        try:
            self.goto(url)
        except Exception as exc:
            self.p(f"[register] callback open failed (ignored): {exc}")

    # public

    def run_register(self, email: str, password: str, name: str, birthdate: str) -> None:
        """Drive the gpt.har register flow end-to-end (no Team join — AP is off)."""
        self.p(f"[register] === 开始注册 {email} ===")
        self.p("[register] step 1/7 — GET chatgpt.com 首页 (拿初始 cookies)")
        self._visit_homepage()
        self.p("[register] step 2/7 — GET /api/auth/csrf")
        csrf = self._get_csrf()
        self.p(f"[register] step 3/7 — POST /api/auth/signin/openai (login_hint={email})")
        auth_url = self._signin(email, csrf)
        self.p(f"[register] step 4/7 — GET authorize URL → 走到登录/注册页")
        final = self._authorize(auth_url)
        path = urlparse(final).path.lower()
        self.p(f"[register] 当前路径: {path}")

        need_otp = False
        if "create-account/password" in path:
            self.p("[register] step 5/7 — POST /api/accounts/user/register (提交邮箱+密码)")
            status, data = self._register(email, password)
            if status != 200:
                self.p(f"[register] ✗ register HTTP {status}: {data}", "error")
                raise FlowError(f"register failed: {data}")
            self.p(f"[register] ✓ 注册账号已创建,接下来要求 OpenAI 发 OTP")
            self.p("[register] step 6/7 — 触发 send-otp")
            self._send_otp()
            need_otp = True
        elif "email-verification" in path or "email-otp" in path:
            self.p("[register] 路径直接进 OTP 验证页(账号或已存在),走 OTP 分支")
            need_otp = True
        elif "about-you" in path:
            self.p("[register] 路径直接进 about-you 页,跳过 OTP 直接 create_account")
            status, data = self._create_account(name, birthdate)
            if status != 200:
                raise FlowError(f"create_account failed: {data}")
            self._consume_callback()
            self.p(f"[register] === ✓ 注册完成 {email} (无 OTP 路径) ===")
            return
        else:
            self.p(f"[register] 未识别路径,fallback 走 register: {final}", "warning")
            status, data = self._register(email, password)
            if status != 200:
                raise FlowError(f"register fallback failed: {data}")
            self._send_otp()
            need_otp = True

        if need_otp:
            self.last_otp_url = self.last_otp_url or f"{AUTH}/email-verification"
            self.p(f"[register] step 6/7 — 等待邮箱 OTP for {email} (最多 3 次)")
            ok = False
            last: dict = {}
            for i in range(3):
                self.p(f"[register]   OTP try {i+1}/3 — 等邮件...")
                code = self._wait_otp(email)
                self.p(f"[register]   提交 code={code} 到 /api/accounts/email-otp/validate")
                status, data = self._validate_register_otp(code)
                last = data
                if status == 200:
                    self.p(f"[register]   ✓ OTP 验证通过")
                    ok = True
                    break
                self.p(f"[register]   ✗ OTP 验证失败 status={status} body={data}; 重发后重试", "warning")
                self._request_otp_resend(why=f"validate-failed-{status}")
                time.sleep(2)
            if not ok:
                raise FlowError(f"register OTP validate failed: {last}")

        self.p("[register] step 7/7 — POST /api/accounts/create_account (生日 + 姓名)")
        status, data = self._create_account(name, birthdate)
        if status != 200:
            self.p(f"[register] ✗ create_account HTTP {status}: {data}", "error")
            raise FlowError(f"create_account failed: {data}")
        self._consume_callback()
        self.p(f"[register] === ✓ 注册完成 {email} ===")

    # ============================================================ OAuth flow (auth.har)

    def _auth_cookie_names(self) -> list[str]:
        names = []
        for c in self.context.cookies([AUTH, OAUTH_ISSUER]):
            name = str(c.get("name") or "").strip()
            domain = str(c.get("domain") or "").lower().strip()
            if name and ("auth.openai.com" in domain or domain.endswith(".openai.com")):
                names.append(name)
        return names

    def _decode_oauth_session(self) -> dict | None:
        for c in self.context.cookies([AUTH, OAUTH_ISSUER]):
            name = str(c.get("name") or "")
            if "oai-client-auth-session" not in name:
                continue
            raw = str(c.get("value") or "").strip()
            if not raw:
                continue
            for val in (raw, unquote(raw)):
                try:
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        val = val[1:-1]
                    part = val.split(".")[0] if "." in val else val
                    part += "=" * ((4 - len(part) % 4) % 4)
                    data = json.loads(base64.urlsafe_b64decode(part).decode())
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
        return None

    def _follow_for_code(self, start_url: str, referer: str | None = None, max_hops: int = 16) -> tuple[str | None, str]:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
            **self._std(),
        }
        if referer:
            headers["Referer"] = referer
        current = start_url
        last = start_url
        for hop in range(max_hops):
            if "add-phone" in current or "add_phone" in current:
                self.p(f"[OAuth] ⚠ add-phone gate 拦截 ({current[:80]}), 中止 follow", "warning")
                return None, current
            r = self._api_call("get", current, step=f"follow[{hop+1}]", headers=headers, max_redirects=0)
            last = r["url"] or current
            code = _extract_code(last)
            if code:
                return code, last
            if r["status"] in (301, 302, 303, 307, 308):
                loc = str(r["headers"].get("location") or "").strip()
                if not loc:
                    return None, last
                if loc.startswith("/"):
                    loc = f"{OAUTH_ISSUER}{loc}"
                if "add-phone" in loc or "add_phone" in loc:
                    self.p(f"[OAuth] ⚠ add-phone gate 重定向 ({loc[:80]}), 中止 follow", "warning")
                    return None, loc
                code = _extract_code(loc)
                if code:
                    return code, loc
                current = loc
                headers["Referer"] = last
                continue
            return None, last
        return None, last

    def _allow_redirect_code(self, url: str, referer: str | None = None) -> str | None:
        try:
            final = self.goto(url, referer=referer)
        except Exception as e:
            m = re.search(r"(https?://localhost[^\s'\"]+)", str(e))
            final = m.group(1) if m else (self.captured_code_url or "")
        return self.captured_code or _extract_code(final or "")

    # ---- workspace selection (PERSONAL ONLY) ----

    def _select_personal_workspace(self, consent_url: str) -> str | None:
        """Decode oai-client-auth-session, find a personal workspace, POST select.

        Selection priority:
          1. Heuristic match (`structure ∈ personal_*` / `plan_type=free` / `is_personal=True`)
          2. The workspace whose id is NOT the master Team account_id (when known)
          3. Pick the *last* workspace — UI shows Team first, Personal second
        Logs every workspace it sees so the user can debug if all 3 paths miss.
        """
        session = self._decode_oauth_session()
        if not session:
            self.p("[OAuth][ws] ✗ 找不到 oai-client-auth-session cookie — 没法选 workspace", "error")
            return None
        workspaces = session.get("workspaces") or []
        if not isinstance(workspaces, list) or not workspaces:
            self.p("[OAuth][ws] ✗ session cookie 里 workspaces[] 为空", "error")
            return None

        # Always print the bundle so the user can see what we're picking from.
        for i, w in enumerate(workspaces):
            if not isinstance(w, dict):
                continue
            self.p(
                f"[OAuth][ws] 候选 [{i}] id={w.get('id')} "
                f"structure={w.get('structure')!r} plan_type={w.get('plan_type')!r} "
                f"is_personal={w.get('is_personal')!r} name={w.get('name') or w.get('workspace_name')!r}"
            )

        personal = None
        reason = ""

        # ① heuristic
        personal = next((w for w in workspaces if _is_personal_workspace(w)), None)
        if personal:
            reason = "heuristic-match"

        # ② not master
        if not personal and self.master_account_id:
            for w in workspaces:
                if isinstance(w, dict) and str(w.get("id") or "").lower() != self.master_account_id:
                    personal = w
                    reason = f"not-master ({w.get('id')} ≠ master {self.master_account_id[:8]}...)"
                    break

        # ③ last entry — UI shows Team first, Personal/个人空间 second
        if not personal:
            personal = workspaces[-1] if isinstance(workspaces[-1], dict) else None
            if personal:
                reason = f"last-entry-fallback (workspaces[{len(workspaces)-1}])"

        if not personal or not personal.get("id"):
            visible = [
                {"id": w.get("id"), "structure": w.get("structure"),
                 "plan_type": w.get("plan_type"), "is_personal": w.get("is_personal")}
                for w in workspaces if isinstance(w, dict)
            ]
            self.p(f"[OAuth][ws] ✗ 三轮筛选都没找到 personal — bundle={visible}", "error")
            raise FlowError(f"OAuth workspaces[] 中无 personal 选项: {visible}")

        ws_id = personal["id"]
        self.p(f"[OAuth][ws] ✓ 选中 workspace_id={ws_id} (策略: {reason})")
        headers = self._json_headers(consent_url, OAUTH_ISSUER)
        r = self._api_call(
            "post",
            f"{OAUTH_ISSUER}/api/accounts/workspace/select",
            step="ws-select",
            json_body={"workspace_id": ws_id},
            headers=headers,
            max_redirects=0,
        )

        if r["status"] in (301, 302, 303, 307, 308):
            loc = str(r["headers"].get("location") or "")
            if loc.startswith("/"):
                loc = f"{OAUTH_ISSUER}{loc}"
            return (
                _extract_code(loc)
                or self._follow_for_code(loc, referer=consent_url)[0]
                or self._allow_redirect_code(loc, referer=consent_url)
            )

        if r["status"] != 200:
            raise FlowError(f"workspace/select HTTP {r['status']}: {(r['text'] or '')[:200]}")

        data = r["json"] or {}
        next_url = str(data.get("continue_url") or "")
        # Some flows insert an extra organization/select hop; we follow it if present.
        orgs = ((data.get("data") or {}).get("orgs")) or []
        if orgs and (orgs[0] or {}).get("id"):
            org_payload = {"org_id": orgs[0]["id"]}
            projects = (orgs[0] or {}).get("projects") or []
            if projects and (projects[0] or {}).get("id"):
                org_payload["project_id"] = projects[0]["id"]
            h2 = dict(headers)
            if next_url:
                h2["Referer"] = next_url if next_url.startswith("http") else f"{OAUTH_ISSUER}{next_url}"
            r2 = self._api_call(
                "post",
                f"{OAUTH_ISSUER}/api/accounts/organization/select",
                step="org-select",
                json_body=org_payload,
                headers=h2,
                max_redirects=0,
            )
            if r2["status"] in (301, 302, 303, 307, 308):
                loc = str(r2["headers"].get("location") or "")
                if loc.startswith("/"):
                    loc = f"{OAUTH_ISSUER}{loc}"
                return (
                    _extract_code(loc)
                    or self._follow_for_code(loc, referer=h2.get("Referer"))[0]
                    or self._allow_redirect_code(loc, referer=h2.get("Referer"))
                )
            if r2["status"] == 200:
                d2 = r2["json"] or {}
                next2 = str(d2.get("continue_url") or "")
                if next2:
                    if next2.startswith("/"):
                        next2 = f"{OAUTH_ISSUER}{next2}"
                    return (
                        self._follow_for_code(next2, referer=h2.get("Referer"))[0]
                        or self._allow_redirect_code(next2, referer=h2.get("Referer"))
                    )

        if next_url:
            if next_url.startswith("/"):
                next_url = f"{OAUTH_ISSUER}{next_url}"
            return (
                self._follow_for_code(next_url, referer=consent_url)[0]
                or self._allow_redirect_code(next_url, referer=consent_url)
            )
        return None

    # ---- consent auto-click (no _prompt_yes) ----

    def _refresh_consent_challenge(self, consent_url: str = "", referer: str | None = None) -> None:
        target = self._abs_auth_url(consent_url or getattr(self.page, "url", ""))
        if target:
            try:
                self.goto(target, referer=referer)
            except Exception as e:
                self.p(f"[OAuth] consent challenge reopen failed: {e}")
        try:
            self.page.wait_for_timeout(900)
        except Exception:
            time.sleep(0.9)
        self._resolve_sentinel_token("email_otp_verification", "password_verify")

    def _auto_click_consent(self, consent_url: str, referer: str | None = None) -> tuple[bool, str | None]:
        target = self._abs_auth_url(consent_url or getattr(self.page, "url", ""))
        if not target:
            return False, None
        self._clear_oauth_continue()
        try:
            self.goto(target, referer=referer)
        except Exception as exc:
            self.p(f"[OAuth] consent goto failed: {exc}")
        code = (
            self.captured_code
            or _extract_code(self.captured_code_url)
            or _extract_code(getattr(self.page, "url", ""))
        )
        if code:
            return False, code

        self._refresh_consent_challenge(target, referer=referer)
        code = (
            self.captured_code
            or _extract_code(self.captured_code_url)
            or _extract_code(getattr(self.page, "url", ""))
        )
        if code:
            return False, code

        # CRITICAL: the Codex consent page renders a workspace radio list and
        # **defaults to the Team workspace** (first option). If we just click
        # "Continue" without selecting Personal first, OAuth completes with a
        # team-plan token, not the free-plan one we want.
        # Click the Personal radio BEFORE clicking Continue.
        self._click_personal_workspace_radio()

        selectors = [
            "button[data-dd-action-name='Continue']",
            "button:has-text('Continue')",
            "button:has-text('Authorize')",
            "button:has-text('Allow')",
            "button:has-text('Accept')",
            "button:has-text('Approve')",
            "button:has-text('Confirm')",
            "[role=button]:has-text('Continue')",
            "[role=button]:has-text('Allow')",
            "[role=button]:has-text('Accept')",
            "button[type='submit']",
            "form button[type='submit']",
            "input[type='submit']",
        ]
        for selector in selectors:
            try:
                loc = self.page.locator(selector).first
                if loc.count() < 1 or not loc.is_visible():
                    continue
                loc.click(timeout=5000)
                try:
                    self.page.wait_for_timeout(2200)
                except Exception:
                    time.sleep(2.2)
                self._sync_api_from_browser()
                self.p(f"[OAuth] consent clicked selector={selector}")
                current = self._abs_auth_url(getattr(self.page, "url", "")) or self._abs_auth_url(self.captured_code_url) or target
                code = self.captured_code or _extract_code(current) or _extract_code(self.captured_code_url)
                if code:
                    return True, code
                code = self._follow_browser_continue(referer=current, max_age=20.0)
                return True, code
            except Exception as e:
                m = re.search(r"(https?://localhost[^\s'\"]+)", str(e))
                if m:
                    code = _extract_code(m.group(1)) or self.captured_code or _extract_code(self.captured_code_url)
                    if code:
                        self.p(f"[OAuth] consent clicked selector={selector}")
                        return True, code
        return False, None

    def _click_personal_workspace_radio(self) -> bool:
        """On the Codex consent page, click the 'Personal account' radio so
        the form's selected workspace is the personal one (instead of the
        Team default).

        Looks for English / Chinese label variants. Returns True if a radio
        was clicked. Failure is non-fatal — the API workspace/select path
        runs separately.
        """
        # Variants in priority order. Stop at the first that exists+visible.
        # Pure-text matches: "Personal account" English, "个人账户/个人空间" CJK.
        # Fallback: pick the LAST radio in the workspace list (Team is first).
        candidates: list[tuple[str, str]] = [
            ("text=/^Personal account$/i", "exact 'Personal account'"),
            ("text=/^Personal Account$/", "exact 'Personal Account'"),
            ("text=/Personal account/i", "contains 'Personal account'"),
            ("[role='radio']:has-text('Personal')", "[role=radio] :has-text Personal"),
            ("label:has-text('Personal account')", "label :has-text 'Personal account'"),
            ("text=/个人(?:账户|账号|空间)/", "中文 个人账户/账号/空间"),
            ("[role='radio']:has-text('个人')", "[role=radio] :has-text 个人"),
        ]
        for selector, desc in candidates:
            try:
                loc = self.page.locator(selector).first
                if loc.count() < 1:
                    continue
                if not loc.is_visible(timeout=500):
                    continue
                loc.click(timeout=3000)
                self.p(f"[OAuth][consent] ✓ 已点选 Personal radio (selector: {desc})")
                try:
                    self.page.wait_for_timeout(600)
                except Exception:
                    time.sleep(0.6)
                return True
            except Exception as exc:
                self.p(f"[OAuth][consent]   尝试 {desc} 失败: {exc}", "debug")
                continue

        # Last-resort fallback: pick the last radio in the page. Codex consent
        # lists Team first, Personal second.
        try:
            radios = self.page.locator("[role='radio']")
            n = radios.count()
            if n >= 2:
                radios.nth(n - 1).click(timeout=3000)
                self.p(f"[OAuth][consent] ✓ 已点选最后一个 [role=radio] (共 {n} 个,fallback 选最后)")
                try:
                    self.page.wait_for_timeout(600)
                except Exception:
                    time.sleep(0.6)
                return True
        except Exception:
            pass

        self.p("[OAuth][consent] ⚠ 未找到 Personal account radio,继续点 Continue (可能拿到 team token)", "warning")
        return False

    def _resolve_code_from_consent(self, consent_url: str, referer: str | None) -> str | None:
        candidates: list[str] = []
        seen = set()

        def add(url):
            raw = self._abs_auth_url(url)
            if raw and raw not in seen:
                seen.add(raw)
                candidates.append(raw)

        add(consent_url)
        add(getattr(self.page, "url", ""))
        add(self.last_otp_url)
        add(f"{OAUTH_ISSUER}/sign-in-with-chatgpt/codex/consent")

        for candidate in candidates:
            # Check if browser already landed on add-phone gate
            page_url_now = self._abs_auth_url(getattr(self.page, "url", "")) or ""
            if "add-phone" in page_url_now or "add_phone" in page_url_now:
                self.oauth_fail_reason = f"add_phone_required — consent 重定向到绑手机页 ({page_url_now[:80]})"
                self.p(f"[OAuth] ⚠ {self.oauth_fail_reason}", "warning")
                return None
            if "add-phone" in candidate or "add_phone" in candidate:
                self.p(f"[OAuth] ⚠ consent candidate 是 add-phone 页面,跳过", "warning")
                continue

            self.p(f"[OAuth] consent candidate -> {candidate}")
            code = _extract_code(candidate)
            if code:
                return code

            code = self._allow_redirect_code(candidate, referer=referer)
            if code:
                return code

            current = self._abs_auth_url(getattr(self.page, "url", "")) or candidate

            # Step A — try the **API** workspace/select path FIRST. This sends
            # `{"workspace_id": <personal>}` directly, bypassing the consent
            # UI entirely. When it works, returns a usable code.
            try:
                code = self._select_personal_workspace(current)
                if code:
                    self.p("[OAuth][consent] ✓ 通过 API workspace/select 拿到 code")
                    return code
            except FlowError as exc:
                self.p(f"[OAuth][consent] API workspace/select 失败: {exc}", "warning")

            # Step B — UI fallback. Click 'Personal account' radio + Continue.
            clicked, code = self._auto_click_consent(candidate, referer=referer)
            if code:
                self.p("[OAuth][consent] ✓ 通过 UI click 拿到 code")
                return code

            current = self._abs_auth_url(getattr(self.page, "url", "")) or candidate
            code = self._follow_browser_continue(referer=current, max_age=20.0)
            if code:
                return code

            if clicked:
                current = self._abs_auth_url(getattr(self.page, "url", "")) or current
            code, _ = self._follow_for_code(current, referer=referer)
            if code:
                return code
        return None

    def _oauth_validate_otp(self, code: str) -> dict[str, Any]:
        referer = self._abs_auth_url(self.last_otp_url or getattr(self.page, "url", "")) or f"{OAUTH_ISSUER}/email-verification"
        headers = self._json_headers(referer, OAUTH_ISSUER)
        token = self._resolve_sentinel_token("email_otp_verification", "password_verify")
        if token:
            headers["openai-sentinel-token"] = token
        return self._api_call(
            "post",
            f"{OAUTH_ISSUER}/api/accounts/email-otp/validate",
            step="oauth-validate-otp",
            json_body={"code": code},
            headers=headers,
            max_redirects=0,
        )

    # public

    def oauth_personal(self, email: str, password: str) -> dict[str, Any] | None:
        """Codex OAuth flow forcing personal-workspace selection.
        Returns {access_token, refresh_token, id_token, ...} on success, None on failure.
        Failure reason recorded in self.oauth_fail_reason for runner to surface.
        """
        self.oauth_fail_reason = ""
        self.p(f"[OAuth] === 开始 OAuth {email} (强制选 personal workspace) ===")
        self._clear_oauth_continue()
        self._prime_cookies()
        self._sync_api_from_browser()

        code_verifier, code_challenge = _generate_pkce()
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": "openid profile email offline_access",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "prompt": "login",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        authorize_url = f"{OAUTH_ISSUER}/oauth/authorize?{urlencode(params)}"

        # 1/8 GET /oauth/authorize → seeds login_session cookie
        r0 = self._api_call(
            "get",
            authorize_url,
            step="oauth-authorize",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": f"{BASE}/",
                "Upgrade-Insecure-Requests": "1",
                **self._std(),
            },
            max_redirects=20,
        )
        final0 = r0["url"]
        cookies0 = self._auth_cookie_names()
        has_login = ("login_session" in cookies0) or any(n.startswith("oai-client-auth-session") for n in cookies0)
        if not has_login:
            r1 = self._api_call(
                "get",
                f"{OAUTH_ISSUER}/api/oauth/oauth2/auth",
                step="oauth-auth-retry",
                params=params,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": authorize_url,
                    "Upgrade-Insecure-Requests": "1",
                    **self._std(),
                },
                max_redirects=20,
            )
            final0 = r1["url"] or final0

        # 2/8 POST authorize/continue (username)
        # Wrapped so we can retry from /oauth/authorize bootstrap on 409
        # invalid_state. The chatgpt server requires very specific state
        # progression — when our session cookies don't line up, it returns
        # 409 and tells us to "start over".
        def _do_authorize_continue() -> dict:
            headers = self._json_headers(
                final0 if str(final0).startswith(OAUTH_ISSUER) else f"{OAUTH_ISSUER}/log-in",
                OAUTH_ISSUER,
            )
            tok = self._resolve_sentinel_token("authorize_continue")
            if tok:
                headers["openai-sentinel-token"] = tok
            return self._api_call(
                "post",
                f"{OAUTH_ISSUER}/api/accounts/authorize/continue",
                step="authorize-continue",
                json_body={"username": {"kind": "email", "value": email}},
                headers=headers,
                max_redirects=0,
            )

        r = _do_authorize_continue()
        if r["status"] != 200:
            self.oauth_fail_reason = f"authorize/continue HTTP {r['status']}: {(r['text'] or '')[:200]}"
            self.p(f"[OAuth] ✗ {self.oauth_fail_reason}", "error")
            return None
        data = r["json"] or {}
        next_url = str(data.get("continue_url") or "")
        page_type = str(((data.get("page") or {}).get("type")) or "")
        self.p(f"[OAuth] step 2/8 ← page_type={page_type!r} next={next_url[:120]}")

        # Browser-emulated state advance — auth.har shows /client_auth_session_dump
        # called between every POST step. Skipping it leaves the server-side
        # state machine on the previous step, causing 409 invalid_state when
        # we POST the next thing.
        self._client_auth_session_dump(referer=self._abs_auth_url(next_url) or final0)

        # add-phone gate check after step 2
        if "add_phone" in page_type or "add-phone" in next_url or "add_phone" in next_url:
            self.oauth_fail_reason = f"add_phone_required — step2 要求绑手机 (page_type={page_type!r})"
            self.p(f"[OAuth] ⚠ {self.oauth_fail_reason}", "warning")
            return None

        # 3/8 password verify (CONDITIONAL — see auth.har: when the just-
        # registered account is still "logged in" via chatgpt session cookies,
        # authorize/continue's response immediately points at email-otp or
        # consent without requiring password. Forcing password/verify in that
        # state returns 409 invalid_state.)
        need_password = (
            page_type in ("login_password", "password", "log-in/password")
            or "log-in/password" in next_url
            or "/password" in next_url.lower()
        )
        if need_password:
            self.p("[OAuth] step 3/8 — POST /api/accounts/password/verify (page 要求 password)")
            headers = self._json_headers(f"{OAUTH_ISSUER}/log-in/password", OAUTH_ISSUER)
            token = self._resolve_sentinel_token("password_verify")
            if token:
                headers["openai-sentinel-token"] = token
            r = self._api_call(
                "post",
                f"{OAUTH_ISSUER}/api/accounts/password/verify",
                step="password-verify",
                json_body={"password": password},
                headers=headers,
                max_redirects=0,
            )

            # 409 invalid_state retry: re-bootstrap once with fresh authorize.
            if r["status"] == 409 and "invalid_state" in (r["text"] or ""):
                self.p("[OAuth] ⚠ password/verify 409 invalid_state — 重 bootstrap 一次", "warning")
                self._clear_oauth_continue()
                self._prime_cookies()
                self._sync_api_from_browser()
                self._api_call(
                    "get", authorize_url, step="oauth-authorize-retry",
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": f"{BASE}/", "Upgrade-Insecure-Requests": "1", **self._std(),
                    },
                    max_redirects=20,
                )
                r2 = _do_authorize_continue()
                if r2["status"] == 200:
                    data2 = r2["json"] or {}
                    next_url = str(data2.get("continue_url") or next_url)
                    page_type = str(((data2.get("page") or {}).get("type")) or page_type)
                    self._client_auth_session_dump(referer=self._abs_auth_url(next_url))
                    # retry password
                    headers = self._json_headers(f"{OAUTH_ISSUER}/log-in/password", OAUTH_ISSUER)
                    token = self._resolve_sentinel_token("password_verify")
                    if token:
                        headers["openai-sentinel-token"] = token
                    r = self._api_call(
                        "post",
                        f"{OAUTH_ISSUER}/api/accounts/password/verify",
                        step="password-verify-retry",
                        json_body={"password": password},
                        headers=headers,
                        max_redirects=0,
                    )

            if r["status"] != 200:
                self.oauth_fail_reason = f"password/verify HTTP {r['status']}: {(r['text'] or '')[:200]}"
                self.p(f"[OAuth] ✗ {self.oauth_fail_reason}", "error")
                return None
            data = r["json"] or {}
            next_url = str(data.get("continue_url") or next_url)
            page_type = str(((data.get("page") or {}).get("type")) or page_type)
            self.p(f"[OAuth] step 3/8 ← page_type={page_type!r} next={next_url[:120]}")
            self._client_auth_session_dump(referer=self._abs_auth_url(next_url))
            if "add_phone" in page_type or "add-phone" in next_url or "add_phone" in next_url:
                self.oauth_fail_reason = f"add_phone_required — step3 要求绑手机 (page_type={page_type!r})"
                self.p(f"[OAuth] ⚠ {self.oauth_fail_reason}", "warning")
                return None
        else:
            self.p(
                f"[OAuth] step 3/8 — **跳过 password/verify** "
                f"(page_type={page_type!r} 表示 server 不需要密码,直接进下一步)"
            )

        # 4/8 OAuth-stage OTP if page demands it (mail-driven)
        need_otp = page_type == "email_otp_verification" or "email-verification" in next_url or "email-otp" in next_url
        if need_otp:
            self.last_otp_url = self._abs_auth_url(next_url or f"{OAUTH_ISSUER}/email-verification")
            self.p(f"[OAuth] step 4/8 — OAuth 阶段需要二次 OTP @ {self.last_otp_url}")
            ok = False
            last_status = 0
            for i in range(3):
                self.p(f"[OAuth]   OTP try {i+1}/3 — 等邮件...")
                code = self._wait_otp(email)
                self.p(f"[OAuth]   提交 code={code}")
                r = self._oauth_validate_otp(code)
                last_status = r["status"]
                if r["status"] == 200:
                    data = r["json"] or {}
                    next_url = str(data.get("continue_url") or next_url)
                    page_type = str(((data.get("page") or {}).get("type")) or page_type)
                    self.last_otp_url = self._abs_auth_url(next_url or self.last_otp_url)
                    self.p(f"[OAuth]   ✓ OTP 通过, page_type={page_type!r} next={next_url[:120]}")
                    # auth.har: GET /client_auth_session_dump after OTP validate
                    self._client_auth_session_dump(referer=self.last_otp_url)
                    if "add_phone" in page_type or "add-phone" in next_url or "add_phone" in next_url:
                        self.oauth_fail_reason = f"add_phone_required — OTP 后要求绑手机 (page_type={page_type!r})"
                        self.p(f"[OAuth] ⚠ {self.oauth_fail_reason}", "warning")
                        return None
                    ok = True
                    break
                self.p(f"[OAuth]   ✗ OTP 拒 status={r['status']}; resend + retry", "warning")
                self._request_otp_resend(why=f"oauth-otp-{r['status']}")
                time.sleep(2)
            if not ok:
                self.oauth_fail_reason = f"OAuth 二次 OTP 3 次都失败 (last status={last_status})"
                self.p(f"[OAuth] ✗ {self.oauth_fail_reason}", "error")
                return None
        else:
            self.p("[OAuth] step 4/8 — 不需要 OAuth 二次 OTP, 直接进 consent")

        # 5/8 consent / workspace / organization → authorization code
        self.p("[OAuth] step 5/8 — consent + workspace 选择 (强制 personal)")
        consent_url = next_url
        if consent_url.startswith("/"):
            consent_url = f"{OAUTH_ISSUER}{consent_url}"
        code = _extract_code(consent_url) if consent_url else None

        if not code and consent_url:
            code, _ = self._follow_for_code(consent_url, referer=f"{OAUTH_ISSUER}/log-in/password")

        consent_hint = (
            ("consent" in (consent_url or ""))
            or ("workspace" in (consent_url or ""))
            or ("organization" in (consent_url or ""))
            or ("consent" in page_type)
            or ("organization" in page_type)
        )
        if not code and consent_hint:
            code = self._resolve_code_from_consent(consent_url, referer=f"{OAUTH_ISSUER}/log-in/password")
        if not code:
            code = self._resolve_code_from_consent("", referer=f"{OAUTH_ISSUER}/log-in/password")
        if not code:
            self.oauth_fail_reason = (
                "consent 后没拿到 authorization code — 可能是 workspace[] 里没 personal "
                "(auto_provision 没生效?), 或者 consent 被 add-phone gate 拦截。"
                " 翻上面的 [OAuth][ws] 候选日志。"
            )
            self.p(f"[OAuth] ✗ {self.oauth_fail_reason}", "error")
            return None
        self.p(f"[OAuth] step 5/8 ✓ 拿到 code={code[:12]}...")

        # 6/8 exchange code for tokens
        self.p("[OAuth] step 6/8 — POST /oauth/token (用 code 换 access/refresh/id)")
        r = self._api_call(
            "post",
            f"{OAUTH_ISSUER}/oauth/token",
            step="oauth-token",
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "client_id": OAUTH_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", **self._std()},
        )
        data = r["json"] or {}
        if r["status"] != 200 or not data.get("access_token"):
            self.oauth_fail_reason = f"oauth/token HTTP {r['status']}: {(r['text'] or '')[:200]}"
            self.p(f"[OAuth] ✗ {self.oauth_fail_reason}", "error")
            return None
        self.p("[OAuth] === ✓ OAuth 完成,拿到 access_token + refresh_token ===")
        return data


# ============================================================ master Playwright login (alt path)


def master_playwright_login(
    email: str,
    password: str | None = None,
    *,
    proxy: str | None = None,
    headless: bool = False,
    otp_provider: MailProvider | None = None,
) -> dict[str, Any]:
    """Drive the master account through chatgpt.com → /api/auth/signin → email OTP,
    then extract `__Secure-next-auth.session-token` from cookies and persist
    it into admin_state.

    `otp_provider` is optional — if None, the OTP step requires a human at the
    keyboard via prompt(). For headless CTF use, pass a configured MailProvider
    or use the session_token-import path instead.
    """
    from autofree import admin_state, master  # late import to avoid cycle

    proxy = _normalize_proxy(proxy)
    pw = sync_playwright().start()
    launch: dict[str, Any] = {"headless": headless, "slow_mo": PLAYWRIGHT_SLOW_MO_MS}
    if proxy:
        launch["proxy"] = {"server": proxy}
    browser = pw.chromium.launch(**launch)
    try:
        ctx = browser.new_context(
            user_agent=_random_chrome_version()[1],
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        page = ctx.new_page()
        page.goto(f"{BASE}/auth/login", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # Type email
        try:
            page.locator("input[type='email'], input[name='email']").first.fill(email, timeout=10000)
            page.locator("button[type='submit'], button:has-text('Continue')").first.click(timeout=10000)
        except Exception as exc:
            raise FlowError(f"email step failed: {exc}")

        page.wait_for_timeout(3000)

        # Password (if password page appears)
        if password:
            try:
                pw_input = page.locator("input[type='password']").first
                if pw_input.is_visible(timeout=5000):
                    pw_input.fill(password)
                    page.locator("button[type='submit']").first.click(timeout=10000)
                    page.wait_for_timeout(3000)
            except Exception as exc:
                logger.warning("[master-login] password step skipped: %s", exc)

        # OTP
        try:
            otp_input = page.locator("input[autocomplete='one-time-code'], input[inputmode='numeric']").first
            if otp_input.is_visible(timeout=5000):
                if otp_provider:
                    code = otp_provider.wait_for_otp(email, timeout=180, sender_keyword="openai")
                else:
                    code = input(f"母号 OTP for {email}: ").strip()
                otp_input.fill(code)
                try:
                    page.locator("button[type='submit']").first.click(timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(4000)
        except Exception as exc:
            logger.info("[master-login] OTP step skipped: %s", exc)

        # Extract session_token + account_id from /api/auth/session
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        cookies = ctx.cookies([BASE])
        session_token = ""
        for c in cookies:
            if c.get("name") == "__Secure-next-auth.session-token":
                session_token = c.get("value") or ""
                break
        if not session_token:
            # Some cookies are split across .0/.1
            parts = {c["name"]: c.get("value") for c in cookies if c.get("name", "").startswith("__Secure-next-auth.session-token.")}
            if parts:
                session_token = "".join(parts[k] for k in sorted(parts))
        if not session_token:
            raise FlowError("登录后未抽到 __Secure-next-auth.session-token cookie")

        # Resolve account_id via /api/auth/session
        sess = page.evaluate(
            """async () => {
                const r = await fetch('/api/auth/session');
                if (!r.ok) return null;
                return await r.json();
            }"""
        )
        account_id = ""
        if isinstance(sess, dict):
            user = sess.get("user") or {}
            account_id = user.get("default_workspace_id") or user.get("account_id") or ""

        # Persist
        info = master.import_session_token(session_token, account_id=account_id, email=email)
        return info
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
