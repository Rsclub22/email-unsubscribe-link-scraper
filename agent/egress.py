"""Filtering forward proxy for one case: every request of the browser, redirect hops included, is checked here.

Playwright's route handler never sees the hops of a redirect (Chromium follows them itself), so the route gate
alone cannot keep the browser away from private addresses. The browser context is pointed at this proxy, which
checks each plain-HTTP request by its full URL and each CONNECT tunnel (HTTPS, WebSocket) by host and port.

The check is addresses_for(url) -> list of IP addresses: the addresses the host may be reached at, or an empty
list if the URL is refused. The proxy connects only to those addresses and never resolves the name itself, so a
DNS answer that changes between check and connection (DNS rebinding) cannot redirect it.
"""

import re
import socket
import socketserver
import threading
from urllib.parse import urlsplit

MAX_HEADER_LINES = 100
MAX_LINE = 16384
HOP_BY_HOP = {"connection", "proxy-connection", "keep-alive", "proxy-authorization", "proxy-authenticate", "te",
              "trailer", "upgrade"}
_HOST = re.compile(r"[A-Za-z0-9._-]+")
_PORT = re.compile(r"[0-9]{1,5}")


def _connect(address: tuple[str, int], timeout: float) -> socket.socket:
    return socket.create_connection(address, timeout=timeout)


def _read_head(rfile) -> list[bytes] | None:
    """Request or status line plus header lines, without the final blank line; None on a malformed head."""
    lines = []
    while True:
        line = rfile.readline(MAX_LINE + 1)
        if not line or len(line) > MAX_LINE or len(lines) > MAX_HEADER_LINES:
            return None
        if line in (b"\r\n", b"\n"):
            return lines
        lines.append(line.rstrip(b"\r\n"))


def _headers(lines: list[bytes]) -> list[tuple[str, str]]:
    out = []
    for line in lines:
        name, sep, value = line.decode("latin-1").partition(":")
        if not sep or not name or name != name.strip():
            raise ValueError("bad header line")
        out.append((name, value.strip()))
    return out


def _keep(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Headers without the hop-by-hop ones, including those the Connection header names."""
    named = {token.strip().lower() for k, v in headers if k.lower() == "connection" for token in v.split(",")}
    return [(k, v) for k, v in headers if k.lower() not in HOP_BY_HOP and k.lower() not in named]


def _port(text: str) -> int | None:
    if not _PORT.fullmatch(text):
        return None
    port = int(text)
    return port if 0 < port < 65536 else None


def _authority(target: str) -> tuple[str, int] | None:
    """Host and port of a CONNECT target: host:port or [ipv6]:port, nothing else (no userinfo, no default port)."""
    if target.startswith("["):
        host, sep, port = target[1:].partition("]:")
        if not sep or not host or not all(c in "0123456789abcdefABCDEF:." for c in host):
            return None
    else:
        host, sep, port = target.rpartition(":")
        if not sep or not _HOST.fullmatch(host):
            return None
    number = _port(port)
    return (host, number) if number else None


def _absolute(target: str) -> tuple[str, int, str] | None:
    """Host, port and origin-form path of an absolute http:// request target."""
    try:
        parts = urlsplit(target)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "http" or not parts.hostname or "@" in parts.netloc or port == 0:
        return None
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.hostname, port or 80, path


def _content_length(headers: list[tuple[str, str]]) -> int | None:
    """The request body length; None if the request must be refused (chunked, duplicate or bad length)."""
    if any(k.lower() == "transfer-encoding" for k, _ in headers):
        return None
    values = [v for k, v in headers if k.lower() == "content-length"]
    if not values:
        return 0
    if len(values) > 1 or not values[0].isdigit():
        return None
    return int(values[0])


def _pump(src, dst) -> None:
    """Copy bytes from src (socket or buffered file) to dst until src ends or either side fails."""
    try:
        while True:
            data = src.read1(65536) if hasattr(src, "read1") else src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class _Handler(socketserver.StreamRequestHandler):
    """One client connection carries exactly one request (or one tunnel); then both sides close. A kept-alive
    connection could otherwise carry the browser's next request, meant for another host, to this upstream."""

    def handle(self):
        proxy = self.server.egress
        self.connection.settimeout(proxy.timeout)
        head = _read_head(self.rfile)
        if not head:
            return
        try:
            method, target, version = head[0].decode("latin-1").split(" ")
            headers = _headers(head[1:])
        except ValueError:
            return
        if method == "CONNECT":
            authority = _authority(target)
            if not authority:
                return
            host, port = authority
            url = f"https://[{host}]:{port}/" if ":" in host else f"https://{host}:{port}/"
            length = path = None
        else:
            absolute = _absolute(target)
            length = _content_length(headers)
            if not absolute or length is None or not version.startswith("HTTP/1."):
                return
            host, port, path = absolute
            url = target
        try:
            addresses = list(proxy.addresses_for(url))
        except Exception:
            addresses = []
        if not addresses:
            proxy.blocked.append(url)
            return      # no answer at all: the browser sees a failed request, as with a blocked route
        upstream = self._open(addresses, port, proxy.timeout)
        if upstream is None:
            return
        with upstream:
            if method == "CONNECT":
                self._tunnel(upstream, proxy.timeout)
            else:
                self._forward(upstream, method, path, version, headers, length)

    @staticmethod
    def _open(addresses: list[str], port: int, timeout: float) -> socket.socket | None:
        for address in addresses:
            try:
                return _connect((address, port), timeout)
            except OSError:
                continue
        return None

    def _tunnel(self, upstream, timeout):
        self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        self.wfile.flush()
        t = threading.Thread(target=_pump, args=(self.rfile, upstream), daemon=True)
        t.start()
        _pump(upstream, self.connection)
        t.join(timeout)

    def _forward(self, upstream, method, path, version, headers, length):
        lines = [f"{method} {path} {version}"] + [f"{k}: {v}" for k, v in _keep(headers)] + ["Connection: close"]
        upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        while length > 0:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                return
            upstream.sendall(chunk)
            length -= len(chunk)
        ufile = upstream.makefile("rb")
        while True:
            head = _read_head(ufile)
            if not head:
                return
            status = head[0].decode("latin-1")
            code = status.split(" ")[1] if status.count(" ") else ""
            if code == "101":
                return      # no protocol switches: the Upgrade header was not passed on, so this is not ours
            if not (len(code) == 3 and code.startswith("1")):
                break       # interim 1xx answers (100 Continue, 103 Early Hints) are dropped; the final one counts
        try:
            response_headers = _keep(_headers(head[1:]))
        except ValueError:
            return
        out = [status] + [f"{k}: {v}" for k, v in response_headers] + ["Connection: close"]
        self.wfile.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
        self.wfile.flush()
        _pump(ufile, self.connection)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass        # a failed connection only fails that request; nothing about the case is logged


class EgressProxy:
    """Proxy on 127.0.0.1 with a random port, between start() and stop(). Refused URLs are appended to blocked."""

    def __init__(self, addresses_for, blocked: list, timeout: float = 30):
        self.addresses_for = addresses_for
        self.blocked = blocked
        self.timeout = timeout
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.egress = self
        self.address = f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "EgressProxy":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
