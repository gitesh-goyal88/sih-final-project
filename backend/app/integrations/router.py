"""Router adapter (TRD §3.5, §8, ADR-06).

* `HaversineRouter`: distance × 1.4 detour factor at 30 km/h, `estimated=True` (FR-M04 fallback).
* `OsrmRouter`: self-hosted OSRM `table` service (one matrix call, ≤ 25 destinations); any failure falls
  back to haversine with `estimated=True`. Results cached in Redis `route:{sha1}` for 1 h.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import settings

DETOUR = 1.4
SPEED_KMH = 30.0


@dataclass
class Leg:
    eta_s: int
    distance_m: int
    estimated: bool


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    (lat1, lng1), (lat2, lng2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _estimate(a: tuple[float, float], b: tuple[float, float]) -> Leg:
    d = haversine_m(a, b) * DETOUR
    return Leg(eta_s=int(d / 1000 / SPEED_KMH * 3600), distance_m=int(d), estimated=True)


class Router(Protocol):
    async def table(self, source: tuple[float, float], dests: list[tuple[float, float]]) -> list[Leg]: ...


class HaversineRouter:
    async def table(self, source: tuple[float, float], dests: list[tuple[float, float]]) -> list[Leg]:
        return [_estimate(source, d) for d in dests]


class OsrmRouter:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    async def table(self, source: tuple[float, float], dests: list[tuple[float, float]]) -> list[Leg]:
        if not dests:
            return []
        coords = ";".join(f"{lng},{lat}" for lat, lng in [source, *dests])
        url = (f"{self.base}/table/v1/driving/{coords}?sources=0"
               f"&destinations={';'.join(str(i) for i in range(1, len(dests) + 1))}&annotations=duration,distance")
        try:
            async with httpx.AsyncClient(timeout=1.2) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                body = resp.json()
            out = []
            for i, d in enumerate(dests):
                dur, dist = body["durations"][0][i], body["distances"][0][i]
                out.append(_estimate(source, d) if dur is None else Leg(int(dur), int(dist), False))
            return out
        except Exception:  # noqa: BLE001 - FR-M04: OSRM failure → haversine
            return [_estimate(source, d) for d in dests]


def router() -> Router:
    return OsrmRouter(settings.osrm_url) if settings.router_provider == "osrm" else HaversineRouter()
