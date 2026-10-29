"""
OAuth access to the collection mailbox (XOAUTH2) instead of a stored password.

An admin connects the mailbox once: OIDC code flow with PKCE, signing in at the SSO as the technical
account of the mailbox. The refresh token is stored encrypted in DATA_DIR; access tokens stay in memory.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

SCOPES = "openid profile email offline_access"
REFRESH_MARGIN_SEC = 60
PENDING_TTL_SEC = 600
MAX_PENDING = 20   # /connect is reachable without login: bound the open requests


class OAuthError(Exception):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    return verifier, _b64url(hashlib.sha256(verifier.encode()).digest())


def jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as e:
        raise OAuthError("Access token is not a JWT") from e


def xoauth2_string(user: str, token: str) -> str:
    return f"user={user}\x01auth=Bearer {token}\x01\x01"


class MailboxOAuth:
    """Token handling for exactly one mailbox."""

    def __init__(self, issuer, client_id, client_secret, redirect_uri, mailbox, data_dir, key,
                 transport=None, clock=time.time):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.mailbox = mailbox
        self.authorization_endpoint = issuer.rstrip("/") + "/api/oidc/authorization"
        self.token_endpoint = issuer.rstrip("/") + "/api/oidc/token"
        self.token_file = Path(data_dir) / "oauth-token"
        self.cipher = Fernet(key.encode() if isinstance(key, str) else key)
        self.transport = transport
        self.clock = clock
        self._lock = threading.Lock()
        self._access: tuple[str, float] | None = None
        self._pending: dict[str, tuple[str, float]] = {}

    @classmethod
    def from_env(cls):
        names = ["OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_REDIRECT_URI", "TOKEN_KEY"]
        if not all(os.environ.get(n) for n in names):
            return None
        return cls(
            issuer=os.environ["OIDC_ISSUER"],
            client_id=os.environ["OIDC_CLIENT_ID"],
            client_secret=os.environ["OIDC_CLIENT_SECRET"],
            redirect_uri=os.environ["OIDC_REDIRECT_URI"],
            mailbox=os.environ.get("EMAIL_USERNAME", ""),
            data_dir=os.environ.get("DATA_DIR", "/data"),
            key=os.environ["TOKEN_KEY"],
        )

    # --- stored refresh token ---------------------------------------------------------------
    def connected(self) -> bool:
        return self._read_refresh() is not None

    def _read_refresh(self) -> str | None:
        try:
            return self.cipher.decrypt(self.token_file.read_bytes()).decode()
        except (FileNotFoundError, InvalidToken):
            return None

    def _write_refresh(self, token: str | None) -> None:
        if token is None:
            self.token_file.unlink(missing_ok=True)
            return
        tmp = self.token_file.with_suffix(".tmp")
        tmp.write_bytes(self.cipher.encrypt(token.encode()))
        tmp.chmod(0o600)
        tmp.replace(self.token_file)

    # --- connect flow -------------------------------------------------------------------------
    def start(self) -> str:
        verifier, challenge = new_pkce()
        state = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items() if now - v[1] < PENDING_TTL_SEC}
            while len(self._pending) >= MAX_PENDING:
                del self._pending[min(self._pending, key=lambda k: self._pending[k][1])]
            self._pending[state] = (verifier, now)
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "login",            # sign in as the mailbox account, not with the admin's session
            "login_hint": self.mailbox,
        }
        return self.authorization_endpoint + "?" + urlencode(query)

    def finish(self, state: str, code: str) -> None:
        with self._lock:
            entry = self._pending.pop(state, None)
        if not entry or self.clock() - entry[1] > PENDING_TTL_SEC or not code:
            raise OAuthError("Invalid or expired request")
        tokens = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": entry[0],
        })
        email = jwt_claims(tokens["access_token"]).get("email", "")
        if email.lower() != self.mailbox.lower():
            raise OAuthError(f"Signed in as {email or 'unknown'}, expected {self.mailbox}")
        if not tokens.get("refresh_token"):
            raise OAuthError("SSO returned no refresh token")
        with self._lock:
            self._store(tokens)

    def disconnect(self) -> None:
        with self._lock:
            self._write_refresh(None)
            self._access = None

    # --- tokens -------------------------------------------------------------------------------
    def _token_request(self, data: dict) -> dict:
        try:
            with httpx.Client(timeout=20.0, transport=self.transport) as client:
                r = client.post(self.token_endpoint, data=data, auth=(self.client_id, self.client_secret))
        except httpx.HTTPError as e:
            raise OAuthError(f"Token endpoint unreachable: {e}") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200 or not body.get("access_token"):
            raise OAuthError(f"Token request failed ({r.status_code}): {body.get('error', 'unknown')}")
        return body

    def _store(self, tokens: dict) -> None:
        if tokens.get("refresh_token"):
            self._write_refresh(tokens["refresh_token"])
        self._access = (tokens["access_token"], self.clock() + float(tokens.get("expires_in") or 300))

    def access_token(self) -> str:
        with self._lock:
            if self._access and self._access[1] - REFRESH_MARGIN_SEC > self.clock():
                return self._access[0]
            refresh = self._read_refresh()
            if not refresh:
                raise OAuthError("Mailbox not connected")
            try:
                tokens = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
            except OAuthError as e:
                if "invalid_grant" in str(e):
                    self._write_refresh(None)   # revoked/expired: connect again
                    self._access = None
                raise
            self._store(tokens)
            return tokens["access_token"]
