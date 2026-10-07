"""Server-side admin sessions and single-use login form CSRF tokens."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

COOKIE = "plex_admin_session"
SESSION_SECONDS = 8 * 3600


@dataclass(frozen=True)
class Session:
    csrf: str
    expires: float
    password_hash: str
    secure: bool


class AdminSessions:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.login_tokens: dict[str, float] = {}

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def prune(self) -> None:
        now = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items() if value.expires > now}
        self.login_tokens = {key: expiry for key, expiry in self.login_tokens.items() if expiry > now}

    def login_token(self) -> str:
        self.prune()
        if len(self.login_tokens) >= 1000:
            raise HTTPException(429, "Too many pending login forms; try again later")
        token = secrets.token_urlsafe(32)
        self.login_tokens[self.digest(token)] = time.monotonic() + 600
        return token

    def consume_login_token(self, token: str) -> bool:
        expiry = self.login_tokens.pop(self.digest(token), 0)
        return expiry > time.monotonic()

    def create(self, password_hash: str, secure: bool) -> tuple[str, Session]:
        self.prune()
        if len(self.sessions) >= 1000:
            raise HTTPException(429, "Too many active administrator sessions")
        token = secrets.token_urlsafe(32)
        session = Session(secrets.token_urlsafe(32), time.monotonic() + SESSION_SECONDS, password_hash, secure)
        self.sessions[self.digest(token)] = session
        return token, session

    def get(self, request: Request, password_hash: str | None) -> Session | None:
        self.prune()
        token = request.cookies.get(COOKIE, "")
        session = self.sessions.get(self.digest(token))
        if session is not None and session.password_hash == password_hash:
            return session
        self.sessions.pop(self.digest(token), None)
        return None

    def remove(self, request: Request) -> None:
        self.sessions.pop(self.digest(request.cookies.get(COOKIE, "")), None)
