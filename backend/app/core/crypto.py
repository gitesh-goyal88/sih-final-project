"""Field-level encryption, blind indexes and secret hashing (SECURITY §8, database.md §5.6.1, §20).

* AES-256-GCM envelope encryption, 96-bit random nonce, AAD = ``table:column:row_id`` (SECURITY §8.1).
* Per-subject data keys (DEKs) in ``subject_keys``, wrapped by the environment KEK. Destroying the
  wrapped DEK crypto-shreds every ``*_enc`` value of that person (DPDP erasure, database.md §21).
  In local/demo the KEK comes from the environment; in staging/prod it is Vault Transit / KMS.
* Blind indexes: HMAC-SHA-256 with a separate versioned key, truncated to 16 bytes and prefixed with
  the key version (SECURITY §8.1, SF-22).
* Decrypted DEKs are cached in process memory for ≤ 5 minutes (SECURITY §8.2).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time
import uuid
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.db import Conn, T

FORMAT_V1 = b"\x01"
DEK_CACHE_TTL_S = 300
_dek_cache: dict[tuple[str, str], tuple[float, int, bytes]] = {}


def _b64key(value: str, what: str) -> bytes:
    if not value:
        raise RuntimeError(f"{what} is not configured — run scripts/gen_keys.py (local) or load from Vault")
    key = base64.b64decode(value)
    if len(key) != 32:
        raise RuntimeError(f"{what} must be 32 bytes")
    return key


def _kek() -> bytes:
    return _b64key(settings.kek_b64, "AM_KEK_B64")


def _hmac_key() -> bytes:
    return _b64key(settings.hmac_key_b64, "AM_HMAC_KEY_B64")


# ---------------- DEKs ----------------

def _wrap(dek: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_kek()).encrypt(nonce, dek, aad)


def _unwrap(blob: bytes, aad: bytes) -> bytes:
    return AESGCM(_kek()).decrypt(blob[:12], blob[12:], aad)


async def dek_for(conn: Conn, kind: str, subject_id: uuid.UUID | str, *, create: bool = True) -> tuple[int, bytes] | None:
    """Current DEK of a subject, creating it on first use. None if shredded (erased person)."""
    sid = str(subject_id)
    cached = _dek_cache.get((kind, sid))
    if cached and cached[0] > time.monotonic():
        return cached[1], cached[2]
    sk = T.subject_keys
    q = select(sk.c.key_version, sk.c.wrapped_dek, sk.c.shredded_at).where(
        and_(sk.c.subject_kind == kind, sk.c.subject_id == sid, sk.c.rotated_at.is_(None))
    ).order_by(sk.c.key_version.desc()).limit(1)
    row = (await conn.execute(q)).first()
    if row is None:
        if not create:
            return None
        dek = AESGCM.generate_key(bit_length=256)
        aad = f"subject:{kind}:{sid}:1".encode()
        await conn.execute(
            pg_insert(sk).values(subject_kind=kind, subject_id=sid, key_version=1,
                                 wrapped_dek=_wrap(dek, aad), kek_id=settings.kek_id)
            .on_conflict_do_nothing()
        )
        row = (await conn.execute(q)).first()
    if row is None or row.shredded_at is not None or row.wrapped_dek is None:
        return None
    dek = _unwrap(bytes(row.wrapped_dek), f"subject:{kind}:{sid}:{row.key_version}".encode())
    _dek_cache[(kind, sid)] = (time.monotonic() + DEK_CACHE_TTL_S, row.key_version, dek)
    return row.key_version, dek


async def preload_deks(conn: Conn, kind: str, subject_ids: list[Any]) -> None:
    """Warm the DEK cache for many subjects with one query (lists, snapshots)."""
    missing = [str(s) for s in set(subject_ids) if s is not None and not (
        (c := _dek_cache.get((kind, str(s)))) and c[0] > time.monotonic())]
    if not missing:
        return
    sk = T.subject_keys
    rows = (await conn.execute(select(sk.c.subject_id, sk.c.key_version, sk.c.wrapped_dek).where(
        and_(sk.c.subject_kind == kind, sk.c.subject_id.in_(missing), sk.c.rotated_at.is_(None),
             sk.c.shredded_at.is_(None))))).all()
    for r in rows:
        sid = str(r.subject_id)
        dek = _unwrap(bytes(r.wrapped_dek), f"subject:{kind}:{sid}:{r.key_version}".encode())
        _dek_cache[(kind, sid)] = (time.monotonic() + DEK_CACHE_TTL_S, r.key_version, dek)


def forget_dek(kind: str, subject_id: uuid.UUID | str) -> None:
    _dek_cache.pop((kind, str(subject_id)), None)


def _aad(table: str, column: str, row_id: Any) -> bytes:
    return f"{table}:{column}:{row_id}".encode()


async def encrypt(conn: Conn, kind: str, subject_id: Any, table: str, column: str, row_id: Any,
                  plaintext: str | None) -> bytes | None:
    if plaintext is None:
        return None
    got = await dek_for(conn, kind, subject_id)
    if got is None:
        raise RuntimeError("subject key has been shredded")
    version, dek = got
    nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), _aad(table, column, row_id))
    return FORMAT_V1 + version.to_bytes(2, "big") + nonce + ct


async def decrypt(conn: Conn, kind: str, subject_id: Any, table: str, column: str, row_id: Any,
                  blob: bytes | memoryview | None) -> str | None:
    if blob is None:
        return None
    blob = bytes(blob)
    got = await dek_for(conn, kind, subject_id, create=False)
    if got is None:
        return None  # erased (crypto-shredded) or never keyed
    _, dek = got
    if blob[:1] != FORMAT_V1:
        raise ValueError("unknown ciphertext format")
    nonce, ct = blob[3:15], blob[15:]
    return AESGCM(dek).decrypt(nonce, ct, _aad(table, column, row_id)).decode("utf-8")


# ---------------- system-level encryption (no personal subject) ----------------

def _system_key(purpose: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"am:system:{purpose}".encode()).derive(_kek())


def system_encrypt(purpose: str, data: bytes, aad: str) -> bytes:
    nonce = os.urandom(12)
    return FORMAT_V1 + nonce + AESGCM(_system_key(purpose)).encrypt(nonce, data, aad.encode())


def system_decrypt(purpose: str, blob: bytes | memoryview, aad: str) -> bytes:
    blob = bytes(blob)
    return AESGCM(_system_key(purpose)).decrypt(blob[1:13], blob[13:], aad.encode())


# ---------------- blind indexes and phones ----------------

def blind_index(value: str) -> bytes:
    mac = hmac.new(_hmac_key(), value.encode("utf-8"), hashlib.sha256).digest()[:16]
    return bytes([settings.hmac_key_version]) + mac


_DIGITS = re.compile(r"\D+")


def normalise_phone(raw: str) -> str:
    """E.164; bare Indian 10-digit mobiles get +91 (API-Guide §3.2)."""
    raw = raw.strip()
    digits = _DIGITS.sub("", raw)
    if raw.startswith("+"):
        e164 = "+" + digits
    elif len(digits) == 10:
        e164 = "+91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        e164 = "+91" + digits[1:]
    elif len(digits) == 12 and digits.startswith("91"):
        e164 = "+" + digits
    else:
        e164 = "+" + digits
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", e164):
        raise ValueError("invalid phone number")
    return e164


def phone_hash(raw: str) -> bytes:
    return blind_index("phone:" + normalise_phone(raw))


def mask_phone(phone: str | None) -> str | None:
    """'+91 98xxxxxx21' (API-Guide §5 User.phoneMasked)."""
    if not phone:
        return None
    p = normalise_phone(phone)
    if p.startswith("+91") and len(p) == 13:
        local = p[3:]
        return f"+91 {local[:2]}{'x' * 6}{local[-2:]}"
    return p[:4] + "x" * (len(p) - 6) + p[-2:]


# ---------------- secrets: tokens, OTPs, codes ----------------

def sha256(data: str | bytes) -> bytes:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).digest()


def scrypt_hash(code: str, salt: bytes) -> bytes:
    # transport_legs.handover_code_hash = scrypt(code, salt) (database.md §5.9)
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(code.encode())


def constant_time_eq(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
