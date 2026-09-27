"""Identifiers (TRD FR-D01/FR-D02, API-Guide §2.3).

- UUIDv7 (RFC 9562) for every row; client-generated where rows are created offline.
- Crockford base32 short codes (6 chars) for patients and cases.
- `idem_tag` = first 8 Crockford base32 chars of the idempotency-key UUID bytes (SMS `#tag`).
"""

from __future__ import annotations

import os
import secrets
import time
import uuid

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SHORT_CODE_RE = r"^[0-9A-HJKMNP-TV-Z]{6}$"
# uuid5 namespace for server-derived ids (SMS-created cases, dedupe-keyed tasks)
NAMESPACE = uuid.UUID("5d1d3c1e-6a7b-4c8d-9e0f-aa7a7a7a7a01")


def uuid7() -> uuid.UUID:
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76                      # version 7
    value |= ((rand >> 68) & 0xFFF) << 64   # rand_a (12 bits)
    value |= 0b10 << 62                     # variant
    value |= rand & ((1 << 62) - 1)         # rand_b (62 bits)
    return uuid.UUID(int=value)


def uuid5(name: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, name)


def crockford_encode(data: bytes) -> str:
    bits = int.from_bytes(data, "big")
    nbits = len(data) * 8
    out = []
    for shift in range(nbits - 5, -5, -5):
        out.append(CROCKFORD[(bits >> shift) & 31] if shift >= 0 else CROCKFORD[(bits << -shift) & 31])
    return "".join(out)


def idem_tag(key: uuid.UUID) -> str:
    return crockford_encode(key.bytes)[:8]


def normalise_crockford(s: str) -> str:
    """Crockford decoding rules: case-insensitive; I/L → 1, O → 0; U is invalid."""
    s = s.strip().upper().replace("I", "1").replace("L", "1").replace("O", "0")
    return s


def new_short_code() -> str:
    return "".join(secrets.choice(CROCKFORD) for _ in range(6))
