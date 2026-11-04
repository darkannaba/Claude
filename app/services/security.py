"""Sécurité : hachage Argon2, jetons CSRF, limitation des tentatives de connexion, en-têtes HTTP."""
from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_hasher = PasswordHasher()

PASSWORD_MIN_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(f"au moins {PASSWORD_MIN_LENGTH} caractères")
    if not any(c.isdigit() for c in password):
        problems.append("au moins un chiffre")
    if not any(c.isalpha() for c in password):
        problems.append("au moins une lettre")
    return problems


# ---------------------------------------------------------------- CSRF
CSRF_SESSION_KEY = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"


def get_csrf_token(session: dict) -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_tokens_match(session: dict, submitted: str | None) -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))


# ---------------------------------------------------------------- anti force brute
class LoginThrottle:
    """Verrouillage temporaire après N échecs (par identifiant et par adresse IP)."""

    def __init__(self, max_attempts: int, lockout_seconds: int):
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = defaultdict(list)

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.lockout_seconds
        attempts = [t for t in self._failures.get(key, []) if t > cutoff]
        self._failures[key] = attempts
        return attempts

    def is_locked(self, *keys: str) -> bool:
        return any(len(self._recent(k)) >= self.max_attempts for k in keys)

    def failure(self, *keys: str) -> None:
        now = time.monotonic()
        for key in keys:
            self._failures[key].append(now)

    def success(self, *keys: str) -> None:
        for key in keys:
            self._failures.pop(key, None)


# ---------------------------------------------------------------- en-têtes de sécurité
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-eval'; "     # Alpine.js évalue ses expressions
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-src 'self' blob:; "
    "object-src 'self'; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware:
    """Middleware ASGI pur : ajoute les en-têtes de sécurité à chaque réponse HTTP."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name.lower() for name, _ in headers}
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"SAMEORIGIN",
                    b"referrer-policy": b"same-origin",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
                    b"content-security-policy": CONTENT_SECURITY_POLICY.encode(),
                }
                for name, value in extra.items():
                    if name not in existing:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
