"""Tokens (TRD §14.1, API-Guide §3.4–3.5, SEC-ID-05).

* Access token: JWT EdDSA (Ed25519), 15 min, `iss`/`aud` checked, `jti` recorded, JWKS published.
* Refresh token: opaque 256-bit CSPRNG, stored as SHA-256, rotated on every use; reuse of a rotated
  token revokes the whole family (`refresh_tokens.family_id`).
"""

from __future__ import annotations

import base64
import secrets
import time
import uuid
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.core.config import settings
from app.core.ids import uuid7


@lru_cache(maxsize=1)
def _private_key() -> Ed25519PrivateKey:
    if not settings.jwt_private_key_pem_b64:
        raise RuntimeError("AM_JWT_PRIVATE_KEY_PEM_B64 not configured — run scripts/gen_keys.py")
    pem = base64.b64decode(settings.jwt_private_key_pem_b64)
    key = serialization.load_pem_private_key(pem, password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


def public_key() -> Ed25519PublicKey:
    return _private_key().public_key()


def jwks() -> dict[str, Any]:
    raw = public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    x = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return {"keys": [{"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "use": "sig", "kid": settings.jwt_kid, "x": x}]}


def issue_access_token(*, user_id: uuid.UUID, role: str, status: str, device_id: uuid.UUID | None,
                       scope: dict[str, Any]) -> tuple[str, int, str]:
    now = int(time.time())
    exp = now + settings.access_token_ttl_s
    jti = str(uuid7())
    claims = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(user_id),
        "role": role,
        "scope": scope,  # routing hint only — scope is resolved from the DB per request (SEC-AZ-04)
        "deviceId": str(device_id) if device_id else None,
        "status": status,
        "iat": now,
        "exp": exp,
        "jti": jti,
    }
    token = jwt.encode(claims, _private_key(), algorithm="EdDSA", headers={"kid": settings.jwt_kid})
    return token, exp, jti


class TokenError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, public_key(), algorithms=["EdDSA"], audience=settings.jwt_audience,
                          issuer=settings.jwt_issuer, options={"require": ["exp", "iat", "sub", "jti"]})
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("TOKEN_EXPIRED") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("UNAUTHENTICATED") from exc


def new_refresh_token() -> str:
    return "rt_" + secrets.token_urlsafe(32)


def new_opaque(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(24)
