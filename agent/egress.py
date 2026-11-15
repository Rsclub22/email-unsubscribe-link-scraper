"""Filtering forward proxy for one case: every request of the browser, redirect hops included, passes url_ok here.

Playwright's route handler never sees the hops of a redirect (Chromium follows them itself), so the route gate
alone cannot keep the browser away from private addresses. The browser context is pointed at this proxy, which
checks each plain-HTTP request by its full URL and each CONNECT tunnel (HTTPS, WebSocket) by its host and port.
"""

import socket
import socketserver
import threading
from urllib.parse import urlsplit

MAX_HEADER_LINES = 100
MAX_LINE = 16384
HOP_BY_HOP = {"connection", "proxy-connection", "keep-alive", "proxy-authorization", "proxy-authenticate", "te",
              "trailer", "upgrade"}


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
        if not sep:
            raise ValueError("bad header line")
        out.append((name.strip(), value.strip()))
    return out


def _keep(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    named = {token.strip().lower() for k, v in headers if k.lower() == "connection" for token in v.split(",")}
    return [(k, v) for k, v in headers if k.lower() not in HOP_BY_HOP and k.lower() not in named]


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
            host, _, port = target.rpartition(":")
            url = f"https://{host}:{port}/"
        else:
            url = target
            parts = urlsplit(target)
            host, port = parts.hostname, parts.port or 80
            if parts.scheme != "http" or not host:
                return
            host = f"[{host}]" if ":" in host else host
        if not proxy.url_ok(url):
            proxy.blocked.append(url)
            return      # no answer at all: the browser sees a failed request, as with a blocked route
        try:
            upstream = socket.create_connection((host.strip("[]"), int(port)), timeout=proxy.timeout)
        except (OSError, ValueError):
            return
        with upstream:
            if method == "CONNECT":
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()
                t = threading.Thread(target=_pump, args=(self.rfile, upstream), daemon=True)
                t.start()
                _pump(upstream, self.connection)
                t.join(proxy.timeout)
                return
            self._forward(upstream, method, parts, version, headers)

    def _forward(self, upstream, method, parts, version, headers):
        """One plain-HTTP request, then both connections close: a kept-alive connection could carry the next
        request of the browser, meant for another host, to this upstream without a check."""
        if any(k.lower() == "transfer-encoding" for k, _ in headers):
            return      # chunked request bodies are not supported; the request fails
        length = next((int(v) for k, v in headers if k.lower() == "content-length" and v.isdigit()), 0)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        lines = [f"{method} {path} {version}"] + [f"{k}: {v}" for k, v in _keep(headers)] + ["Connection: close"]
        upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        while length > 0:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                return
            upstream.sendall(chunk)
            length -= len(chunk)
        ufile = upstream.makefile("rb")
        head = _read_head(ufile)
        if not head:
            return
        try:
            status, response_headers = head[0], _keep(_headers(head[1:]))
        except ValueError:
            return
        out = [status.decode("latin-1")] + [f"{k}: {v}" for k, v in response_headers] + ["Connection: close"]
        self.wfile.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
        self.wfile.flush()
        _pump(ufile, self.connection)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass        # a failed connection only fails that request; nothing about the case is logged


class EgressProxy:
    """Proxy on 127.0.0.1 with a random port, between start() and stop(). Blocked URLs are appended to blocked."""

    def __init__(self, url_ok, blocked: list, timeout: float = 30):
        self.url_ok = url_ok
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
