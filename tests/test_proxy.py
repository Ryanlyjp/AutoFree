import select
import socket
import socketserver
import threading
from urllib.parse import urlparse

import pytest

from autofree.proxy import (
    ProxyConfigError,
    build_browser_proxy,
    build_playwright_proxy,
    normalize_proxy_url,
    parse_proxy_config,
)


def test_normalize_proxy_url_adds_http_scheme() -> None:
    assert normalize_proxy_url("127.0.0.1:7890") == "http://127.0.0.1:7890"


def test_parse_socks5_proxy_with_auth_marks_browser_bridge() -> None:
    cfg = parse_proxy_config("socks5://us%40er:pa%3Ass@127.0.0.1:1080")
    assert cfg is not None
    assert cfg.url == "socks5://us%40er:pa%3Ass@127.0.0.1:1080"
    assert cfg.browser_server == "socks5://127.0.0.1:1080"
    assert cfg.requests_url == "socks5h://us%40er:pa%3Ass@127.0.0.1:1080"
    assert cfg.username == "us@er"
    assert cfg.password == "pa:ss"
    assert cfg.needs_browser_bridge is True


def test_build_playwright_proxy_sanitizes_socks5h_for_browser() -> None:
    assert build_playwright_proxy("socks5h://127.0.0.1:1080") == {"server": "socks5://127.0.0.1:1080"}


def test_rejects_comma_separated_proxy_format() -> None:
    with pytest.raises(ProxyConfigError):
        normalize_proxy_url("127.0.0.1,1080,user,pass")


def test_rejects_proxy_url_with_extra_path() -> None:
    with pytest.raises(ProxyConfigError):
        normalize_proxy_url("http://127.0.0.1:7890/extra")


def test_browser_proxy_bridges_authenticated_socks5_connect() -> None:
    target = _HelloServer(("127.0.0.1", 0))
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()

    socks = _FakeSocks5Server(("127.0.0.1", 0))
    socks_thread = threading.Thread(target=socks.serve_forever, daemon=True)
    socks_thread.start()

    bridge = build_browser_proxy(f"socks5://user:pass@127.0.0.1:{socks.server_address[1]}")
    bridge.start()
    proxy_cfg = bridge.playwright()
    assert proxy_cfg is not None

    parsed = urlparse(proxy_cfg["server"])
    assert parsed.scheme == "http"

    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as client:
            client.sendall(
                f"CONNECT 127.0.0.1:{target.server_address[1]} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target.server_address[1]}\r\n\r\n".encode("ascii")
            )
            response = _recv_until(client, b"\r\n\r\n")
            assert b"200 Connection established" in response

            client.sendall(b"GET /hello HTTP/1.1\r\nHost: example.test\r\nConnection: close\r\n\r\n")
            tunneled = _recv_all(client)
            assert b"HTTP/1.1 200 OK" in tunneled
            assert tunneled.endswith(b"hello")

        assert socks.username == "user"
        assert socks.password == "pass"
        assert socks.target == ("127.0.0.1", target.server_address[1])
        assert target.last_request is not None
        assert b"GET /hello HTTP/1.1" in target.last_request
    finally:
        bridge.close()
        socks.shutdown()
        socks.server_close()
        socks_thread.join(timeout=2)
        target.shutdown()
        target.server_close()
        target_thread.join(timeout=2)


class _ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _HelloServer(_ThreadedServer):
    def __init__(self, server_address: tuple[str, int]):
        self.last_request: bytes | None = None
        super().__init__(server_address, _HelloHandler)


class _HelloHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = _recv_until(self.request, b"\r\n\r\n")
        self.server.last_request = data
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 5\r\n"
            b"Connection: close\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"hello"
        )


class _FakeSocks5Server(_ThreadedServer):
    def __init__(self, server_address: tuple[str, int]):
        self.username: str | None = None
        self.password: str | None = None
        self.target: tuple[str, int] | None = None
        super().__init__(server_address, _FakeSocks5Handler)


class _FakeSocks5Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock = self.request
        sock.settimeout(5)

        version, nmethods = _recv_exact(sock, 2)
        assert version == 5
        methods = _recv_exact(sock, nmethods)
        assert 2 in methods
        sock.sendall(b"\x05\x02")

        auth_version = _recv_exact(sock, 1)[0]
        assert auth_version == 1
        username = _recv_exact(sock, _recv_exact(sock, 1)[0]).decode("utf-8")
        password = _recv_exact(sock, _recv_exact(sock, 1)[0]).decode("utf-8")
        self.server.username = username
        self.server.password = password
        sock.sendall(b"\x01\x00")

        ver, cmd, _rsv, atyp = _recv_exact(sock, 4)
        assert ver == 5
        assert cmd == 1
        host = _recv_socks_host(sock, atyp)
        port = int.from_bytes(_recv_exact(sock, 2), "big")
        self.server.target = (host, port)

        upstream = socket.create_connection((host, port), timeout=5)
        sock.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x04\x38")
        _relay(sock, upstream)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("unexpected EOF")
        chunks.extend(chunk)
    return bytes(chunks)


def _recv_until(sock: socket.socket, marker: bytes) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _recv_all(sock: socket.socket) -> bytes:
    data = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _recv_socks_host(sock: socket.socket, atyp: int) -> str:
    if atyp == 1:
        return socket.inet_ntoa(_recv_exact(sock, 4))
    if atyp == 3:
        return _recv_exact(sock, _recv_exact(sock, 1)[0]).decode("utf-8")
    if atyp == 4:
        return socket.inet_ntop(socket.AF_INET6, _recv_exact(sock, 16))
    raise AssertionError(f"unsupported atyp {atyp}")


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 1.0)
            if not readable:
                continue
            for current in readable:
                other = right if current is left else left
                chunk = current.recv(65536)
                if not chunk:
                    return
                other.sendall(chunk)
    finally:
        for sock in (left, right):
            try:
                sock.close()
            except OSError:
                pass
