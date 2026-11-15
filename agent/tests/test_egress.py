"""The egress proxy with raw sockets against local test servers: what it forwards, where, and what it refuses."""

import socket
import threading
import time

import pytest

import egress
import guard
from egress import EgressProxy

OK_REPLY = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"


class Upstream:
    """Raw TCP server on 127.0.0.1. Records the bytes of every accepted connection; answers each request head with
    a canned reply (or echoes everything back), then keeps reading for a moment so stray bytes are recorded too."""

    def __init__(self, reply=OK_REPLY, echo=False):
        self.reply, self.echo = reply, echo
        self.connections: list[bytearray] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            data = bytearray()
            self.connections.append(data)
            threading.Thread(target=self._serve, args=(conn, data), daemon=True).start()

    def _serve(self, conn, data):
        conn.settimeout(1)
        answered = False
        with conn:
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                    if self.echo:
                        conn.sendall(chunk)
                    elif not answered and b"\r\n\r\n" in data:
                        conn.sendall(self.reply)
                        answered = True
            except OSError:
                return

    def close(self):
        self.sock.close()


@pytest.fixture
def upstreams():
    made = []

    def make(**kw):
        u = Upstream(**kw)
        made.append(u)
        return u
    yield make
    for u in made:
        u.close()


@pytest.fixture
def proxy_for():
    made = []

    def make(addresses_for):
        p = EgressProxy(addresses_for, [], timeout=3).start()
        made.append(p)
        return p
    yield make
    for p in made:
        p.stop()


def allow(*urls_to_address):
    """addresses_for that allows exactly the given (url prefix, address) pairs and records every call."""
    calls = []

    def addresses_for(url):
        calls.append(url)
        for prefix, address in urls_to_address:
            if url.startswith(prefix):
                return [address]
        return []
    addresses_for.calls = calls
    return addresses_for


def exchange(proxy, data: bytes, wait: float = 3.0) -> bytes:
    """Send data to the proxy and read until it closes the connection (or wait runs out)."""
    port = int(proxy.address.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port)) as s:
        s.settimeout(wait)
        s.sendall(data)
        out = bytearray()
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
        except TimeoutError:
            pass
        return bytes(out)


def settle():
    time.sleep(0.3)      # let the upstream threads record what they got


# --- DNS rebinding: the proxy connects only to the address the check returned ---------------------------------

def test_proxy_connects_to_the_checked_address_and_never_resolves_the_name(upstreams, proxy_for):
    up = upstreams()
    addresses_for = allow((f"http://never-resolvable.invalid:{up.port}/", "127.0.0.1"))
    reply = exchange(proxy_for(addresses_for),
                     f"GET http://never-resolvable.invalid:{up.port}/x HTTP/1.1\r\nHost: never-resolvable.invalid\r\n\r\n"
                     .encode())
    assert reply.startswith(b"HTTP/1.1 200 OK") and reply.endswith(b"ok")
    settle()
    assert len(up.connections) == 1 and up.connections[0].startswith(b"GET /x HTTP/1.1\r\n")


def test_rebinding_resolver_cannot_redirect_the_connection(monkeypatch, proxy_for):
    answers = iter([["93.184.216.34"]])
    lookups = []

    def flip(host):                  # public on the first lookup, loopback on every later one
        lookups.append(host)
        return next(answers, ["127.0.0.1"])

    connects = []

    def fake_connect(address, timeout=None, *args, **kw):
        connects.append(address)
        raise OSError("no network in this test")

    real_getaddrinfo = socket.getaddrinfo
    name_lookups = []

    def spy_getaddrinfo(host, *a, **kw):
        name_lookups.append(host)
        return real_getaddrinfo(host, *a, **kw)

    monkeypatch.setattr(egress, "_connect", fake_connect)
    monkeypatch.setattr(socket, "getaddrinfo", spy_getaddrinfo)
    proxy = proxy_for(lambda url: guard.public_addresses(url, resolve=flip, blocked=()))
    assert exchange(proxy, b"GET http://rebind.example/ HTTP/1.1\r\nHost: rebind.example\r\n\r\n") == b""
    assert lookups == ["rebind.example"]              # one check, one lookup
    assert connects == [("93.184.216.34", 80)]         # to the vetted address, not to the name
    assert "rebind.example" not in name_lookups


def test_next_checked_address_is_tried_when_the_first_fails(upstreams, proxy_for):
    up = upstreams()                  # listens on 127.0.0.1 only: 127.0.0.2 on the same port refuses

    def addresses_for(url):
        return ["127.0.0.2", "127.0.0.1"]
    reply = exchange(proxy_for(addresses_for), f"GET http://two.example:{up.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert reply.endswith(b"ok")


def test_denied_url_makes_no_upstream_connection_and_gets_no_answer(upstreams, proxy_for):
    up = upstreams()
    proxy = proxy_for(lambda url: [])
    assert exchange(proxy, f"GET http://127.0.0.1:{up.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode()) == b""
    settle()
    assert up.connections == [] and proxy.blocked == [f"http://127.0.0.1:{up.port}/"]


def test_failing_check_counts_as_denied(upstreams, proxy_for):
    up = upstreams()

    def broken(url):
        raise RuntimeError("resolver crashed")
    assert exchange(proxy_for(broken), f"GET http://127.0.0.1:{up.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode()) == b""
    settle()
    assert up.connections == []


# --- CONNECT ------------------------------------------------------------------------------------------------

def test_connect_allowed_tunnels_bytes(upstreams, proxy_for):
    up = upstreams(echo=True)
    addresses_for = allow((f"https://tunnel.example:{up.port}/", "127.0.0.1"))
    port = int(proxy_for(addresses_for).address.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port)) as s:
        s.settimeout(3)
        s.sendall(f"CONNECT tunnel.example:{up.port} HTTP/1.1\r\nHost: tunnel.example:{up.port}\r\n\r\n".encode())
        head = s.recv(1024)
        assert head == b"HTTP/1.1 200 Connection established\r\n\r\n"
        s.sendall(b"opaque tls bytes")
        assert s.recv(1024) == b"opaque tls bytes"
    assert addresses_for.calls == [f"https://tunnel.example:{up.port}/"]


def test_connect_denied_sends_nothing_and_connects_nowhere(upstreams, proxy_for):
    up = upstreams(echo=True)
    proxy = proxy_for(lambda url: [])
    assert exchange(proxy, f"CONNECT 127.0.0.1:{up.port} HTTP/1.1\r\n\r\n".encode()) == b""
    settle()
    assert up.connections == []


@pytest.mark.parametrize("authority", ["tunnel.example", "tunnel.example:abc", "tunnel.example:0",
                                       "tunnel.example:70000", "user@tunnel.example:443", "user:pw@tunnel.example:443",
                                       ":443", "[::1", "::1:443", "tunnel.example:+443", "tunnel.example: 443"])
def test_malformed_connect_is_refused_without_a_check(proxy_for, authority):
    addresses_for = allow(("https://", "127.0.0.1"))
    assert exchange(proxy_for(addresses_for), f"CONNECT {authority} HTTP/1.1\r\n\r\n".encode()) == b""
    assert addresses_for.calls == []


def test_connect_to_bracketed_ipv6_literal_is_checked_with_brackets(proxy_for):
    addresses_for = allow()
    assert exchange(proxy_for(addresses_for), b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n") == b""
    assert addresses_for.calls == ["https://[::1]:443/"]


# --- plain HTTP (absolute form) --------------------------------------------------------------------------------

def test_absolute_form_request_is_forwarded_in_origin_form(upstreams, proxy_for):
    up = upstreams()
    addresses_for = allow((f"http://site.example:{up.port}/", "127.0.0.1"))
    reply = exchange(proxy_for(addresses_for),
                     f"POST http://site.example:{up.port}/u?x=1 HTTP/1.1\r\nHost: site.example:{up.port}\r\n"
                     "Proxy-Connection: keep-alive\r\nConnection: keep-alive, X-Secret\r\nX-Secret: 1\r\n"
                     "Content-Length: 5\r\n\r\nhello".encode())
    assert reply.endswith(b"ok")
    settle()
    head, _, body = bytes(up.connections[0]).partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    assert lines[0] == "POST /u?x=1 HTTP/1.1" and body == b"hello"
    names = [line.split(":")[0].lower() for line in lines[1:]]
    assert "host" in names and "proxy-connection" not in names and "x-secret" not in names
    assert "Connection: close" in lines and "Connection: keep-alive, X-Secret" not in lines
    assert addresses_for.calls == [f"http://site.example:{up.port}/u?x=1"]


def test_absolute_form_denied(upstreams, proxy_for):
    up = upstreams()
    assert exchange(proxy_for(allow()), f"GET http://127.0.0.1:{up.port}/ HTTP/1.1\r\n\r\n".encode()) == b""
    settle()
    assert up.connections == []


@pytest.mark.parametrize("target", ["/relative", "https://site.example/", "ftp://site.example/", "http:///x",
                                    "http://site.example:0/", "http://site.example:abc/", "http://u:p@site.example/"])
def test_other_request_targets_are_refused_without_a_check(proxy_for, target):
    addresses_for = allow(("http://", "127.0.0.1"))
    assert exchange(proxy_for(addresses_for), f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode()) == b""
    assert addresses_for.calls == []


def test_request_with_transfer_encoding_is_dropped(upstreams, proxy_for):
    up = upstreams()
    addresses_for = allow((f"http://127.0.0.1:{up.port}/", "127.0.0.1"))
    reply = exchange(proxy_for(addresses_for), f"POST http://127.0.0.1:{up.port}/ HTTP/1.1\r\nHost: x\r\n"
                     "Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n".encode())
    settle()
    assert reply == b"" and up.connections == []


@pytest.mark.parametrize("lengths", [("5", "5"), ("5", "7"), ("5, 5",), ("-1",), ("abc",)])
def test_duplicate_or_bad_content_length_is_rejected(upstreams, proxy_for, lengths):
    up = upstreams()
    addresses_for = allow((f"http://127.0.0.1:{up.port}/", "127.0.0.1"))
    headers = "".join(f"Content-Length: {v}\r\n" for v in lengths)
    reply = exchange(proxy_for(addresses_for),
                     f"POST http://127.0.0.1:{up.port}/ HTTP/1.1\r\nHost: x\r\n{headers}\r\nhello".encode())
    settle()
    assert reply == b"" and up.connections == []


def test_pipelined_second_request_is_never_forwarded(upstreams, proxy_for):
    a, b = upstreams(), upstreams()
    addresses_for = allow((f"http://127.0.0.1:{a.port}/", "127.0.0.1"), (f"http://127.0.0.1:{b.port}/", "127.0.0.1"))
    first = f"GET http://127.0.0.1:{a.port}/first HTTP/1.1\r\nHost: a\r\n\r\n"
    second = f"GET http://127.0.0.1:{b.port}/second HTTP/1.1\r\nHost: b\r\n\r\n"
    reply = exchange(proxy_for(addresses_for), (first + second).encode())
    settle()
    assert reply.count(b"HTTP/1.1 200 OK") == 1
    assert b.connections == [] and len(a.connections) == 1
    assert bytes(a.connections[0]).count(b" HTTP/1.1\r\n") == 1 and b"/second" not in a.connections[0]


def test_second_request_on_the_same_connection_is_never_forwarded(upstreams, proxy_for):
    a, b = upstreams(), upstreams()
    addresses_for = allow((f"http://127.0.0.1:{a.port}/", "127.0.0.1"), (f"http://127.0.0.1:{b.port}/", "127.0.0.1"))
    port = int(proxy_for(addresses_for).address.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port)) as s:
        s.settimeout(3)
        s.sendall(f"GET http://127.0.0.1:{a.port}/first HTTP/1.1\r\nHost: a\r\n\r\n".encode())
        got = bytearray()
        while not got.endswith(b"ok"):
            got += s.recv(65536)
        assert b"Connection: close" in got
        try:
            s.sendall(f"GET http://127.0.0.1:{b.port}/second HTTP/1.1\r\nHost: b\r\n\r\n".encode())
            assert s.recv(65536) == b""
        except (BrokenPipeError, ConnectionResetError):
            pass
    settle()
    assert b.connections == [] and b"/second" not in a.connections[0]


def test_chunked_response_passes_through_intact(upstreams, proxy_for):
    body = b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"
    up = upstreams(reply=b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: text/plain\r\n\r\n" + body)
    addresses_for = allow((f"http://127.0.0.1:{up.port}/", "127.0.0.1"))
    reply = exchange(proxy_for(addresses_for), f"GET http://127.0.0.1:{up.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    head, _, rest = reply.partition(b"\r\n\r\n")
    assert b"Transfer-Encoding: chunked" in head and rest == body


def test_interim_1xx_responses_are_skipped_and_hop_by_hop_headers_of_the_final_one_stripped(upstreams, proxy_for):
    up = upstreams(reply=b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 103 Early Hints\r\nLink: </a.css>\r\n\r\n"
                         b"HTTP/1.1 200 OK\r\nConnection: keep-alive, X-Hop\r\nKeep-Alive: timeout=5\r\nX-Hop: 1\r\n"
                         b"Proxy-Connection: keep-alive\r\nContent-Length: 2\r\n\r\nok")
    addresses_for = allow((f"http://127.0.0.1:{up.port}/", "127.0.0.1"))
    reply = exchange(proxy_for(addresses_for), f"GET http://127.0.0.1:{up.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    head, _, rest = reply.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    assert lines[0] == "HTTP/1.1 200 OK" and rest == b"ok" and reply.count(b"HTTP/1.1 ") == 1
    names = [line.split(":")[0].lower() for line in lines[1:]]
    assert "keep-alive" not in names and "x-hop" not in names and "proxy-connection" not in names
    assert lines.count("Connection: close") == 1 and names.count("connection") == 1


def test_switching_protocols_is_not_passed_on(upstreams, proxy_for):
    up = upstreams(reply=b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\nraw")
    addresses_for = allow((f"http://127.0.0.1:{up.port}/", "127.0.0.1"))
    reply = exchange(proxy_for(addresses_for), f"GET http://127.0.0.1:{up.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert reply == b""


# --- limits: interim answers and connection lifetime --------------------------------------------------------------

@pytest.mark.parametrize("interim,answered", [(5, True), (6, False)])
def test_at_most_five_interim_answers(upstreams, proxy_for, interim, answered):
    up = upstreams(reply=b"HTTP/1.1 100 Continue\r\n\r\n" * interim + OK_REPLY)
    addresses_for = allow((f"http://127.0.0.1:{up.port}/", "127.0.0.1"))
    reply = exchange(proxy_for(addresses_for), f"GET http://127.0.0.1:{up.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert reply == (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok" if answered else b"")


def test_connection_ends_at_its_lifetime_even_while_bytes_trickle():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    upstream_closed = threading.Event()

    def trickle():
        conn, _ = server.accept()
        with conn:
            conn.recv(65536)
            try:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
                for _ in range(100):          # one byte every 0.2 s: never idle long enough for the socket timeout
                    conn.sendall(b"x")
                    time.sleep(0.2)
            except OSError:
                upstream_closed.set()

    threading.Thread(target=trickle, daemon=True).start()
    proxy = EgressProxy(lambda url: ["127.0.0.1"], [], timeout=3, lifetime=1).start()
    try:
        start = time.monotonic()
        reply = exchange(proxy, f"GET http://127.0.0.1:{port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode(), wait=10)
        elapsed = time.monotonic() - start
    finally:
        proxy.stop()
        server.close()
    assert reply.startswith(b"HTTP/1.1 200 OK") and len(reply) < 200
    assert elapsed < 2
    assert upstream_closed.wait(2)        # the upstream side was closed too, not left to the daemon thread


def test_tunnel_ends_at_its_lifetime(upstreams):
    up = upstreams(echo=True)
    proxy = EgressProxy(lambda url: ["127.0.0.1"], [], timeout=3, lifetime=1).start()
    try:
        port = int(proxy.address.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port)) as s:
            s.settimeout(5)
            s.sendall(f"CONNECT tunnel.example:{up.port} HTTP/1.1\r\n\r\n".encode())
            assert s.recv(1024).startswith(b"HTTP/1.1 200")
            start = time.monotonic()
            while True:                        # keep the tunnel busy; the lifetime must still end it
                try:
                    s.sendall(b"ping")
                    if not s.recv(1024):
                        break
                except OSError:
                    break
                time.sleep(0.1)
            assert time.monotonic() - start < 2
    finally:
        proxy.stop()
