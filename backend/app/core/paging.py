"""Opaque cursors (API-Guide §2.7): base64url JSON, valid 24 h."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from app.core.errors import AppError

CURSOR_TTL_S = 24 * 3600


def encode(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps({**data, "_t": int(time.time())}, default=str).encode()).decode().rstrip("=")


def decode(cursor: str | None) -> dict[str, Any] | None:
    if not cursor:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AppError("VALIDATION_FAILED", "Bad cursor", fields=[{"path": "cursor", "code": "invalid"}]) from exc
    if int(time.time()) - int(data.get("_t", 0)) > CURSOR_TTL_S:
        raise AppError("VALIDATION_FAILED", "Cursor expired", fields=[{"path": "cursor", "code": "expired"}])
    return data


def limit(value: int | None) -> int:
    return max(1, min(int(value or 50), 200))


def if_match(header: str | None, version: int, current: Any = None) -> None:
    """PATCH on mutable entities requires If-Match: <version> (API-Guide §2.6)."""
    if header is None:
        raise AppError("VALIDATION_FAILED", "If-Match header is required", fields=[{"path": "If-Match", "code": "missing"}])
    if header.strip('"W/ ') != str(version):
        raise AppError("VERSION_MISMATCH", f"Current version is {version}", current=current)
