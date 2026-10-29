"""Tests for the OAuth connection of the collection mailbox."""

import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

import oauth


def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def _mb(tmp_path, responses, seen=None, clock=None):
    def handler(request):
        if seen is not None:
            seen.append(parse_qs(request.content.decode()))
        return responses.pop(0)
    return oauth.MailboxOAuth(
        "https://sso.example.org", "unsubscriber", "secret", "https://u.example.org/oidc/callback",
        "unsubscribe-bot@example.org", tmp_path, Fernet.generate_key().decode(),
        transport=httpx.MockTransport(handler), clock=clock or Clock(),
    )


def _start_state(mb):
    url = urlparse(mb.start())
    q = parse_qs(url.query)
    assert q["prompt"] == ["login"] and q["login_hint"] == ["unsubscribe-bot@example.org"]
    assert "offline_access" in q["scope"][0] and q["code_challenge_method"] == ["S256"]
    return q["state"][0]


def test_connect_stores_encrypted_refresh_token(tmp_path):
    mb = _mb(tmp_path, [httpx.Response(200, json={
        "access_token": _jwt({"email": "Unsubscribe-Bot@example.org"}), "refresh_token": "rt-1", "expires_in": 3600})])
    mb.finish(_start_state(mb), "code")
    assert mb.connected()
    assert b"rt-1" not in (tmp_path / "oauth-token").read_bytes()
    assert oct((tmp_path / "oauth-token").stat().st_mode)[-3:] == "600"


def test_connect_with_wrong_account_is_rejected(tmp_path):
    mb = _mb(tmp_path, [httpx.Response(200, json={
        "access_token": _jwt({"email": "philipp@example.org"}), "refresh_token": "rt", "expires_in": 3600})])
    with pytest.raises(oauth.OAuthError, match="expected unsubscribe-bot"):
        mb.finish(_start_state(mb), "code")
    assert not mb.connected()


def test_unknown_state_is_rejected(tmp_path):
    with pytest.raises(oauth.OAuthError):
        _mb(tmp_path, []).finish("forged", "code")


def test_access_token_refreshes_and_rotates(tmp_path):
    seen, clock = [], Clock()
    mb = _mb(tmp_path, [
        httpx.Response(200, json={"access_token": _jwt({"email": "unsubscribe-bot@example.org"}),
                                  "refresh_token": "rt-1", "expires_in": 3600}),
        httpx.Response(200, json={"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}),
    ], seen, clock)
    mb.finish(_start_state(mb), "code")
    first = mb.access_token()
    assert len(seen) == 1                       # cached from the code exchange
    clock.now += 3600
    assert mb.access_token() == "at-2" != first
    assert seen[1]["refresh_token"] == ["rt-1"]
    assert mb._read_refresh() == "rt-2"


def test_revoked_token_disconnects(tmp_path):
    clock = Clock()
    mb = _mb(tmp_path, [
        httpx.Response(200, json={"access_token": _jwt({"email": "unsubscribe-bot@example.org"}),
                                  "refresh_token": "rt-1", "expires_in": 10}),
        httpx.Response(400, json={"error": "invalid_grant"}),
    ], clock=clock)
    mb.finish(_start_state(mb), "code")
    clock.now += 100
    with pytest.raises(oauth.OAuthError):
        mb.access_token()
    assert not mb.connected()


def test_not_connected(tmp_path):
    with pytest.raises(oauth.OAuthError):
        _mb(tmp_path, []).access_token()


def test_app_uses_xoauth2_when_oauth_configured(monkeypatch):
    import app as unsub

    calls = {}

    class FakeIMAP:
        def __init__(self, host, port):
            pass

        def authenticate(self, mech, cb):
            calls["auth"] = (mech, cb(b""))

        def login(self, *a):
            calls["login"] = a

        def select(self, folder):
            pass

    class FakeOAuth:
        def access_token(self):
            return "tok"

    monkeypatch.setattr(unsub.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(unsub, "OAUTH", FakeOAuth())
    monkeypatch.setattr(unsub, "EMAIL_USERNAME", "unsubscribe-bot@example.org")
    unsub.connect()
    assert calls == {"auth": ("XOAUTH2", oauth.xoauth2_string("unsubscribe-bot@example.org", "tok").encode())}


def test_index_shows_connect_when_not_connected(monkeypatch):
    import app as unsub

    class FakeOAuth:
        def connected(self):
            return False

    monkeypatch.setattr(unsub, "OAUTH", FakeOAuth())
    html = unsub.app.test_client().get("/").get_data(as_text=True)
    assert 'action="/connect"' in html and 'action="/result"' not in html
