"""Structured JSON logs without PII (TRD §16, SEC-LOG-02, SECURITY §14.1).

A scrubber drops any key that could carry personal data or secrets; coordinates are rounded to ~1 km.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

_FORBIDDEN = {"phone", "name", "otp", "token", "code", "ticket", "idempotency_key", "body", "notes",
              "password", "refresh_token", "access_token", "firstname", "first_name"}


def _scrub(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    for key in list(event.keys()):
        low = key.lower()
        if low in _FORBIDDEN or low.endswith("_phone") or low.endswith("_name"):
            event[key] = "[scrubbed]"
        elif low in ("lat", "lng") and isinstance(event[key], (int, float)):
            event[key] = round(float(event[key]), 2)
    return event


def configure() -> None:
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _scrub,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


log = structlog.get_logger("aapatmitra")


def security_event(name: str, **fields: Any) -> None:
    """SECURITY §14.2 security events (auth.otp_failed, authz.denied, sync.field_not_writable, ...)."""
    log.warning("security_event", security_event=name, **fields)
