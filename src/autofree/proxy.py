"""Proxy parsing helpers shared by browser and requests clients."""

from __future__ import annotations

import contextlib
import select
import socket
import socketserver
import threading
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

import socks


_ALLOWED_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
_SOCKS_SCHEMES = frozenset({"socks5", "socks5h"})
_HTTP_SCHEMES = frozenset({"http", "https"})


class ProxyConfigError(ValueError):
    """Raised when the configured proxy URL is invalid or unsupported."""


@dataclass(frozen=True)
class ProxyConfig:
    scheme: str
    host: str
    port: int | None
    url: str
    browser_server: str
    requests_url: str
    username: str | None = None
    password: str | None = None

    @property
    def needs_browser_bridge(self) -> bool:
        return self.scheme in _SOCKS_SCHEMES and self.username is not None

    def playwright(self) -> dict[str, str]:
        cfg = {"server": self.browser_server}
        if self.username is not None:
            cfg["username"] = self.username
        if self.password is not None:
            cfg["password"] = self.password
        return cfg


class BrowserProxy:
    """Runtime browser proxy settings, with an optional local bridge."""

    def __init__(self, upstream: ProxyConfig | None = None):
        self.upstream = upstream
        self._bridge: _SocksHttpBridge | None = None

    @property
    def server(self) -> str | None:
        if not self.upstream:
            return None
        if self._bridge:
            return self._bridge.server
        return self.upstream.browser_server

    @property
    def display_url(self) -> str:
        if not self.upstream:
            return "none"
        if self._bridge:
            return f"{self.upstream.url} -> {self._bridge.server}"
        return self.upstream.url

    def start(self) -> "BrowserProxy":
        if self.upstream and self.upstream.needs_browser_bridge and not self._bridge:
            self._bridge = _SocksHttpBridge(self.upstream)
            self._bridge.start()
        return self

    def playwright(self) -> dict[str, str] | None:
        if not self.upstream:
            return None
        if self._bridge:
            return {"server": self._bridge.server}
        return self.upstream.playwright()

    def close(self) -> None:
        if self._bridge:
            self._bridge.close()
            self._bridge = None


def parse_proxy_config(value: str | None) -> ProxyConfig | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if "," in raw and "://" not in raw:
        raise ProxyConfigError(
            "代理必须填写完整 URL，例如 socks5://127.0.0.1:1080；不支持 IP,PORT,USER,PWD 这种格式。"
        )

    candidate = raw if "://" in raw else f"http://{raw}"
    parsed = urlparse(candidate)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        allowed = ", ".join(sorted(_ALLOWED_SCHEMES))
        raise ProxyConfigError(f"不支持的代理协议: {scheme or '(empty)'}。当前支持: {allowed}")
    if not parsed.netloc:
        raise ProxyConfigError("代理地址格式无效，请填写类似 socks5://127.0.0.1:1080 的完整 URL。")
    if parsed.query or parsed.fragment:
        raise ProxyConfigError("代理 URL 不能包含 query 或 fragment。")
    if parsed.path not in ("", "/"):
        raise ProxyConfigError("代理 URL 只能包含主机和端口，不能带额外路径。")
    if "@" in parsed.netloc and parsed.username in (None, ""):
        raise ProxyConfigError("代理认证格式无效，请使用 scheme://user:pass@host:port。")
    if not parsed.hostname:
        raise ProxyConfigError("代理 URL 缺少主机名或 IP。")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ProxyConfigError("代理端口无效，请检查 host:port。") from exc

    username = unquote(parsed.username) if parsed.username is not None else None
    password = unquote(parsed.password) if parsed.password is not None else None

    normalized_netloc = _build_netloc(parsed.hostname, port, username, password)
    browser_scheme = "socks5" if scheme in _SOCKS_SCHEMES else scheme
    browser_server = _build_url(browser_scheme, _build_netloc(parsed.hostname, port))
    requests_scheme = "socks5h" if scheme in _SOCKS_SCHEMES else scheme
    requests_url = _build_url(requests_scheme, normalized_netloc)

    return ProxyConfig(
        scheme=scheme,
        host=parsed.hostname,
        port=port,
        url=_build_url(scheme, normalized_netloc),
        browser_server=browser_server,
        requests_url=requests_url,
        username=username,
        password=password,
    )


def normalize_proxy_url(value: str | None) -> str | None:
    cfg = parse_proxy_config(value)
    return cfg.url if cfg else None


def build_playwright_proxy(value: str | None) -> dict[str, str] | None:
    cfg = parse_proxy_config(value)
    return cfg.playwright() if cfg else None


def build_browser_proxy(value: str | ProxyConfig | None) -> BrowserProxy:
    cfg = value if isinstance(value, ProxyConfig) or value is None else parse_proxy_config(value)
    return BrowserProxy(cfg)


def build_requests_proxy_map(value: str | None) -> dict[str, str] | None:
    cfg = parse_proxy_config(value)
    if not cfg:
        return None
    return {"http": cfg.requests_url, "https": cfg.requests_url}


def _build_url(scheme: str, netloc: str) -> str:
    return f"{scheme}://{netloc}"


def _build_netloc(
    hostname: str,
    port: int | None,
    username: str | None = None,
    password: str | None = None,
) -> str:
    host = _format_host(hostname)
    auth = ""
    if username is not None:
        auth = quote(username, safe="")
        if password is not None:
            auth += f":{quote(password, safe='')}"
        auth += "@"
    port_part = f":{port}" if port is not None else ""
    return f"{auth}{host}{port_part}"


def _format_host(hostname: str) -> str:
    return f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname


class _BridgeServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], upstream: ProxyConfig):
        self.upstream = upstream
        super().__init__(server_address, _BridgeHandler)


class _SocksHttpBridge:
    """Expose a local HTTP proxy that forwards traffic through an upstream SOCKS5 proxy."""

    def __init__(self, upstream: ProxyConfig):
        if not upstream.needs_browser_bridge:
            raise ProxyConfigError("只有带认证的 SOCKS5 代理才需要浏览器桥接。")
        self.upstream = upstream
        self._server = _BridgeServer(("127.0.0.1", 0), upstream)
        self._thread = threading.Thread(target=self._server.serve_forever, name="autofree-socks-bridge", daemon=True)
        self._started = False

    @property
    def server(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def close(self) -> None:
        if not self._started:
            self._server.server_close()
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


class _BridgeHandler(socketserver.BaseRequestHandler):
    timeout = 60.0

    def handle(self) -> None:
        self.request.settimeout(self.timeout)
        raw = _recv_until(self.request, b"\r\n\r\n")
        if not raw:
            return
        head, _, rest = raw.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        if not lines:
            return
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            self._send_error(400, "Bad Request")
            return
        if method.upper() == "CONNECT":
            self._handle_connect(target)
            return
        self._handle_forward(method, target, version, lines[1:], rest)

    def _handle_connect(self, target: str) -> None:
        try:
            host, port = _parse_authority(target, default_port=443)
            upstream = self._open_upstream(host, port)
        except Exception:
            self._send_error(502, "Bad Gateway")
            return
        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        _relay_bidirectional(self.request, upstream)

    def _handle_forward(self, method: str, target: str, version: str, headers: list[str], body: bytes) -> None:
        parsed = urlparse(target)
        if parsed.scheme.lower() not in _HTTP_SCHEMES or not parsed.hostname:
            self._send_error(400, "Unsupported Request Target")
            return

        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        rewritten = [f"{method} {path} {version}"]
        saw_connection = False
        saw_host = False
        for line in headers:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if not sep:
                continue
            lname = name.strip().lower()
            if lname == "proxy-connection":
                continue
            if lname == "connection":
                rewritten.append("Connection: close")
                saw_connection = True
                continue
            if lname == "host":
                saw_host = True
            rewritten.append(f"{name.strip()}: {value.strip()}")
        if not saw_host:
            rewritten.append(f"Host: {_build_netloc(host, port)}")
        if not saw_connection:
            rewritten.append("Connection: close")

        payload = ("\r\n".join(rewritten) + "\r\n\r\n").encode("iso-8859-1") + body
        try:
            upstream = self._open_upstream(host, port)
            upstream.sendall(payload)
        except Exception:
            self._send_error(502, "Bad Gateway")
            return
        _relay_bidirectional(self.request, upstream)

    def _open_upstream(self, host: str, port: int) -> socks.socksocket:
        upstream = socks.socksocket()
        upstream.settimeout(self.timeout)
        upstream.set_proxy(
            proxy_type=socks.SOCKS5,
            addr=self.server.upstream.host,
            port=self.server.upstream.port or 1080,
            username=self.server.upstream.username,
            password=self.server.upstream.password,
            rdns=True,
        )
        upstream.connect((host, port))
        return upstream

    def _send_error(self, status: int, reason: str) -> None:
        body = f"{status} {reason}\n".encode("utf-8")
        response = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
        ).encode("iso-8859-1") + body
        with contextlib.suppress(OSError):
            self.request.sendall(response)


def _recv_until(sock: socket.socket, marker: bytes, max_bytes: int = 65536) -> bytes:
    data = bytearray()
    while marker not in data and len(data) < max_bytes:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _parse_authority(value: str, default_port: int) -> tuple[str, int]:
    parsed = urlparse(f"//{value}")
    if not parsed.hostname:
        raise ValueError("missing host")
    return parsed.hostname, parsed.port or default_port


def _relay_bidirectional(client: socket.socket, upstream: socket.socket) -> None:
    sockets = [client, upstream]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 1.0)
            if not readable:
                continue
            for current in readable:
                other = upstream if current is client else client
                chunk = current.recv(65536)
                if not chunk:
                    return
                other.sendall(chunk)
    finally:
        for sock in (upstream, client):
            with contextlib.suppress(OSError):
                sock.close()
