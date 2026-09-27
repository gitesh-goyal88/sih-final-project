"""Hybrid logical clocks for field-level last-writer-wins (TRD §6.4, ADR-05).

Stamp format: ``physicalMs:counter:deviceId``. The server rejects physical parts more than 5 minutes in
the future and re-stamps them with server time (TH-31, SECURITY §14.2 `sync.hlc_future`).
"""

from __future__ import annotations

import time
from typing import Any

FUTURE_SKEW_MS = 5 * 60 * 1000


def parse(stamp: str | None) -> tuple[int, int, str]:
    if not stamp:
        return (0, 0, "")
    try:
        ms, counter, dev = stamp.split(":", 2)
        return int(ms), int(counter), dev
    except ValueError:
        return (0, 0, "")


def now_stamp(device: str = "server") -> str:
    return f"{int(time.time() * 1000)}:0000:{device}"


def sanitise(stamp: str | None) -> tuple[str, bool]:
    """Return (stamp, restamped). Far-future stamps are replaced with server time."""
    ms, counter, dev = parse(stamp)
    now_ms = int(time.time() * 1000)
    if ms == 0:
        return now_stamp(), True
    if ms > now_ms + FUTURE_SKEW_MS:
        return f"{now_ms}:{counter:04d}:{dev or 'server'}", True
    return f"{ms}:{counter:04d}:{dev}", False


def newer(a: str | None, b: str | None) -> bool:
    """True if stamp a is strictly newer than b."""
    return parse(a) > parse(b)


def merge_fields(field_clock: dict[str, Any], incoming: dict[str, Any], hlc: str) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Return (fields_to_apply, lost_fields, new_field_clock)."""
    apply: dict[str, Any] = {}
    lost: list[str] = []
    clock = dict(field_clock or {})
    for field, value in incoming.items():
        if newer(hlc, clock.get(field)):
            apply[field] = value
            clock[field] = hlc
        else:
            lost.append(field)
    return apply, lost, clock
