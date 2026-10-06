"""
utils/security.py — Token handling (access + refresh) and OTP generation/hashing.

Token model
-----------
* **Access token** — a short-lived JWT (ACCESS_TOKEN_EXPIRE_MINUTES, default 30 min)
  sent as `Authorization: Bearer <token>` on every protected request. It is
  stateless: the server does not store it and cannot revoke an individual one
  before it expires — that's what the short lifetime is for.

* **Refresh token** — a long-lived opaque random string (not a JWT). Only its
  SHA-256 hash is stored in MongoDB, so a database leak does not hand out
  sessions. It is exchanged at POST /api/auth/refresh for a fresh access token,
  and is *rotated* on every use: the old one is immediately revoked. If a
  revoked refresh token is presented again (a sign it was stolen and replayed),
  the whole token family is revoked and the user must log in again.
"""

import hashlib
import hmac
import secrets
import string
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from app.core.config import settings

ACCESS_TOKEN_TYPE = "access"

# Session scopes. Every access token, refresh token and OTP carries one.
#   app   — issued via /auth/signin or /auth/signup (the mobile app)
#   admin — issued via /admin/auth/signin (the dashboard); admin accounts only
# Tokens minted before scopes existed have no claim and count as "app".
SCOPE_APP = "app"
SCOPE_ADMIN = "admin"

#: OTP purposes that are NOT sessions. An OTP sent for one of these can never
#: be exchanged for a token at /auth/verify-otp, and a sign-in OTP can never be
#: used to perform one of these actions. The purpose is stored with the OTP and
#: read from storage, never from the request.
PURPOSE_DELETE = "delete"
SESSION_SCOPES = (SCOPE_APP, SCOPE_ADMIN)
SCOPES = (SCOPE_APP, SCOPE_ADMIN)


# ── Access tokens (JWT) ──────────────────────────────────────────────────────

def _access_minutes(scope: str) -> int:
    return settings.ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES if scope == SCOPE_ADMIN else settings.ACCESS_TOKEN_EXPIRE_MINUTES


def create_access_token(subject: str, scope: str = SCOPE_APP) -> str:
    """subject = the user's mobile_number ('sub' claim); scope = 'app' | 'admin'."""
    if scope not in SCOPES:
        raise ValueError(f"unknown token scope: {scope!r}")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "type": ACCESS_TOKEN_TYPE,
        "scope": scope,
        "iat": now,
        "exp": now + timedelta(minutes=_access_minutes(scope)),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_claims(token: str) -> tuple[str, str] | None:
    """(mobile_number, scope) if the token is a valid, unexpired access token, else None."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None
    # Reject anything that isn't an access token (e.g. a token minted elsewhere).
    if payload.get("type") != ACCESS_TOKEN_TYPE or not payload.get("sub"):
        return None
    scope = payload.get("scope", SCOPE_APP)
    if scope not in SCOPES:
        return None
    return payload["sub"], scope


def decode_access_token(token: str) -> str | None:
    """Return the 'sub' claim (mobile_number) of a valid access token of any scope."""
    claims = decode_access_claims(token)
    return claims[0] if claims else None


def access_token_expires_in_seconds(scope: str = SCOPE_APP) -> int:
    return _access_minutes(scope) * 60


# ── Refresh tokens (opaque, stored hashed) ───────────────────────────────────

def generate_refresh_token() -> str:
    """A 64-char URL-safe random string — plenty of entropy, never stored raw."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """Refresh tokens are high-entropy random values, so a plain SHA-256 is the
    right tool here — key-stretching (bcrypt/argon2) buys nothing and would make
    every API call slower."""
    return hashlib.sha256(token.encode()).hexdigest()


def refresh_token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)


def new_token_family_id() -> str:
    return secrets.token_hex(16)


# ── OTP ──────────────────────────────────────────────────────────────────────

def generate_otp() -> str:
    # secrets, not random — OTPs are a security control.
    return "".join(secrets.choice(string.digits) for _ in range(settings.OTP_LENGTH))


def hash_otp(otp: str) -> str:
    # OTPs are short-lived and low-entropy, but we still avoid storing them in
    # plaintext in the DB. SHA-256 is sufficient here (not a password-hash case).
    return hashlib.sha256(otp.encode()).hexdigest()


def verify_otp_hash(otp: str, otp_hash: str) -> bool:
    # Constant-time compare so a timing side channel can't leak the digest.
    return hmac.compare_digest(hash_otp(otp), otp_hash)
