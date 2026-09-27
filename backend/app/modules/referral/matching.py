"""Facility matching (TRD §8, API-Guide §6.5.1, database.md §8.2, §15.3).

Deterministic and explainable — no urgency scoring (PRD anti-goal):
  1. filter: status = open ∧ every needed capability available ∧ within `matching.radius_m` ∧ not already offered
  2. empty → nearest higher-level facilities (DH/SDH/MC) ignoring capability, `capability_unconfirmed` (T12)
  3. ETA pickup → village roadhead → facility (OSRM, haversine × 1.4 fallback with `eta_estimated`)
  4. sort key (stale, eta_seconds, −beds_available, level_rank); return top 10 with reasons

Redis hot copy (`fac:{id}`, `fac:geo:{district}`, `fac:cap:{district}:{cap}`) is the fast pre-filter; if it is
empty or unreachable the PostgreSQL query of database.md §8.2 gives the same candidate set.

`level_rank` is not defined in the TRD; this build ranks higher-level facilities first on a tie
(MC, DH, SDH, CHC, PHC, SC, private) — documented in docs/IMPLEMENTATION_NOTES.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select

from app.core import cfg
from app.core import redis as rds
from app.core.db import Conn, T, engine, lat_of, lng_of
from app.core.logging import log
from app.integrations.router import haversine_m, router

LEVEL_RANK = {"MC": 0, "DH": 1, "SDH": 2, "CHC": 3, "PHC": 4, "SC": 5, "private": 6}
HIGHER_LEVELS = ("MC", "DH", "SDH")


@dataclass
class Match:
    facility_id: Any
    name: str
    level: str
    lat: float
    lng: float
    beds: int
    status: str
    stale: bool
    distance_m: int
    eta_s: int
    eta_estimated: bool
    matched: list[str]
    missing: list[str] = field(default_factory=list)
    capability_unconfirmed: bool = False
    rank: int = 0

    def reasons(self) -> dict[str, Any]:
        return {"capabilitiesMatched": self.matched, "capabilitiesMissing": self.missing,
                "etaMin": max(1, round(self.eta_s / 60)), "etaEstimated": self.eta_estimated,
                "distanceKm": round(self.distance_m / 1000, 1), "beds": self.beds, "stale": self.stale,
                "capabilityUnconfirmed": self.capability_unconfirmed}

    def out(self) -> dict[str, Any]:
        return {"facility": {"id": self.facility_id, "name": self.name, "level": self.level,
                             "location": {"lat": self.lat, "lng": self.lng}, "bedsAvailable": self.beds,
                             "status": self.status},
                "rank": self.rank, "reasons": self.reasons()}


async def roadhead_of(conn: Conn, village_id: Any) -> tuple[float, float] | None:
    if not village_id:
        return None
    w = T.village_waypoints
    row = (await conn.execute(select(lat_of(w.c.location).label("lat"), lng_of(w.c.location).label("lng")).where(and_(
        w.c.village_id == village_id, w.c.kind == "roadhead", w.c.is_default, w.c.deleted_at.is_(None))))).first()
    return (float(row.lat), float(row.lng)) if row else None


async def _redis_candidates(district: str, pickup: tuple[float, float], radius_m: int, needed: list[str]) -> list[str] | None:
    """database.md §15.3 steps 1–2. None = Redis cold/unavailable → caller uses PostgreSQL."""
    try:
        geo_key = rds.k(f"fac:geo:{district}")
        if not await rds.r().exists(geo_key):
            return None
        ids = await rds.r().geosearch(geo_key, longitude=pickup[1], latitude=pickup[0], radius=radius_m, unit="m",
                                      sort="ASC", count=50)
        if needed:
            caps = await rds.r().sinter([rds.k(f"fac:cap:{district}:{c}") for c in needed])
            ids = [i for i in ids if i in caps]
        out = []
        for fid in ids:
            status = await rds.r().hget(rds.k(f"fac:{fid}"), "status")
            if status == "open":
                out.append(fid)
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("redis_match_fallback", error=repr(exc))
        return None


async def _pg_candidates(conn: Conn, district: str, pickup: tuple[float, float], radius_m: int, needed: list[str]) -> list[str]:
    """database.md §8.2 (relational division: has ALL needed caps), KNN on the geography GiST."""
    f, fc = T.facilities, T.facility_capabilities
    pt = func.ST_SetSRID(func.ST_MakePoint(pickup[1], pickup[0]), 4326).cast(f.c.location.type)
    q = select(f.c.id).where(and_(f.c.status == "open", f.c.deleted_at.is_(None),
                                  func.ST_DWithin(f.c.location, pt, radius_m)))
    if needed:
        capable = (select(fc.c.facility_id).where(and_(fc.c.available, fc.c.capability_code.in_(needed)))
                   .group_by(fc.c.facility_id).having(func.count() == len(set(needed))))
        q = q.where(f.c.id.in_(capable))
    q = q.order_by(f.c.location.op("<->")(pt)).limit(25)
    return [str(r.id) for r in (await conn.execute(q)).all()]


async def match(conn: Conn, *, needed: list[str], pickup: tuple[float, float] | None, district_code: str,
                village_id: Any = None, exclude: set[str] | None = None, limit: int = 10) -> tuple[list[Match], bool]:
    """Returns (ranked matches, no_capable_facility)."""
    exclude = exclude or set()
    conf = await cfg.merged(conn, district_code)
    radius = int(conf.get("matching.radius_m", 100000))
    stale_after = timedelta(hours=float(conf.get("matching.stale_after_h", 12)))
    roadhead = await roadhead_of(conn, village_id)
    origin = pickup or roadhead or await _village_point(conn, village_id)

    ids: list[str]
    if origin is None:
        ids = await _pg_any_location(conn, district_code, needed)
    else:
        cached = await _redis_candidates(district_code, origin, radius, needed)
        ids = cached if cached is not None else await _pg_candidates(conn, district_code, origin, radius, needed)
    ids = [i for i in ids if i not in exclude]
    matches = await _build(conn, ids, needed, origin, roadhead, stale_after, unconfirmed=False)
    no_capable = not matches
    if no_capable:
        fallback = await _fallback_ids(conn, district_code, origin, exclude)
        matches = await _build(conn, fallback, needed, origin, roadhead, stale_after, unconfirmed=True)
    matches.sort(key=lambda m: (m.stale, m.eta_s, -m.beds, LEVEL_RANK.get(m.level, 9)))
    matches = matches[:limit]
    for i, m in enumerate(matches, start=1):
        m.rank = i
    return matches, no_capable


async def _village_point(conn: Conn, village_id: Any) -> tuple[float, float] | None:
    if not village_id:
        return None
    v = T.villages
    row = (await conn.execute(select(lat_of(v.c.location).label("lat"), lng_of(v.c.location).label("lng"))
                              .where(v.c.id == village_id))).first()
    return (float(row.lat), float(row.lng)) if row else None


async def _pg_any_location(conn: Conn, district: str, needed: list[str]) -> list[str]:
    f, fc = T.facilities, T.facility_capabilities
    q = select(f.c.id).where(f.c.district_code == district, f.c.status == "open", f.c.deleted_at.is_(None))
    if needed:
        capable = (select(fc.c.facility_id).where(and_(fc.c.available, fc.c.capability_code.in_(needed)))
                   .group_by(fc.c.facility_id).having(func.count() == len(set(needed))))
        q = q.where(f.c.id.in_(capable))
    return [str(r.id) for r in (await conn.execute(q.limit(25))).all()]


async def _fallback_ids(conn: Conn, district: str, origin: tuple[float, float] | None, exclude: set[str]) -> list[str]:
    """TRD §8 step 2: nearest higher-level facility ignoring capability."""
    f = T.facilities
    q = select(f.c.id).where(f.c.level.in_(HIGHER_LEVELS), f.c.status == "open", f.c.deleted_at.is_(None),
                             f.c.district_code == district)
    if origin:
        pt = func.ST_SetSRID(func.ST_MakePoint(origin[1], origin[0]), 4326).cast(f.c.location.type)
        q = q.order_by(f.c.location.op("<->")(pt))
    return [str(r.id) for r in (await conn.execute(q.limit(5))).all() if str(r.id) not in exclude][:3]


async def _build(conn: Conn, ids: list[str], needed: list[str], origin: tuple[float, float] | None,
                 roadhead: tuple[float, float] | None, stale_after: timedelta, *, unconfirmed: bool) -> list[Match]:
    if not ids:
        return []
    f, fc = T.facilities, T.facility_capabilities
    rows = (await conn.execute(select(f.c.id, f.c.name, f.c.level, f.c.beds_available, f.c.status,
                                      f.c.capability_updated_at, lat_of(f.c.location).label("lat"),
                                      lng_of(f.c.location).label("lng")).where(f.c.id.in_(ids)))).all()
    caps: dict[Any, set[str]] = {}
    for r in (await conn.execute(select(fc.c.facility_id, fc.c.capability_code).where(
            fc.c.facility_id.in_(ids), fc.c.available))).all():
        caps.setdefault(r.facility_id, set()).add(r.capability_code)
    dests = [(float(r.lat), float(r.lng)) for r in rows]
    rt = router()
    if origin is None:
        legs = None
    elif roadhead:
        first = (await rt.table(origin, [roadhead]))[0]
        second = await rt.table(roadhead, dests)
        legs = [(first.eta_s + s.eta_s, first.distance_m + s.distance_m, first.estimated or s.estimated) for s in second]
    else:
        legs = [(x.eta_s, x.distance_m, x.estimated) for x in await rt.table(origin, dests)]
    now = datetime.now(UTC)
    out = []
    for i, r in enumerate(rows):
        have = caps.get(r.id, set())
        eta, dist, est = legs[i] if legs else (0, 0, True)
        out.append(Match(facility_id=r.id, name=r.name, level=r.level, lat=float(r.lat), lng=float(r.lng),
                         beds=r.beds_available, status=r.status, stale=r.capability_updated_at < now - stale_after,
                         distance_m=int(dist or (haversine_m(origin, dests[i]) if origin else 0)), eta_s=int(eta),
                         eta_estimated=est, matched=sorted(set(needed) & have), missing=sorted(set(needed) - have),
                         capability_unconfirmed=unconfirmed))
    return out


async def offered_facility_ids(conn: Conn, case_id: Any) -> set[str]:
    o = T.facility_offers
    rows = (await conn.execute(select(o.c.facility_id).where(and_(o.c.case_id == case_id, o.c.source != "admin_reassign")))).all()
    return {str(r.facility_id) for r in rows}


# ---------------- Redis hot copy (database.md §15.2, §15.6) ----------------

async def refresh_facility_cache(facility_id: Any, conn: Conn | None = None) -> None:
    try:
        if conn is None:
            async with engine().connect() as c2:
                return await refresh_facility_cache(facility_id, c2)
        vf = T.v_facility_live
        row = (await conn.execute(select(vf.c.id, vf.c.district_code, vf.c.level, vf.c.status, vf.c.beds_available,
                                         vf.c.capability_updated_at, vf.c.is_stale, vf.c.available_capabilities,
                                         lat_of(vf.c.location).label("lat"), lng_of(vf.c.location).label("lng"))
                                  .where(vf.c.id == facility_id))).first()
        if row is None:
            return
        fid, d = str(row.id), row.district_code
        pipe = rds.r().pipeline()
        pipe.hset(rds.k(f"fac:{fid}"), mapping={
            "status": row.status, "beds": row.beds_available, "caps": ",".join(row.available_capabilities or []),
            "level": row.level, "updatedAt": row.capability_updated_at.isoformat(), "stale": int(bool(row.is_stale))})
        pipe.geoadd(rds.k(f"fac:geo:{d}"), (float(row.lng), float(row.lat), fid))
        all_caps = (await conn.execute(select(T.capabilities.c.code))).scalars().all()
        for cap in all_caps:
            key = rds.k(f"fac:cap:{d}:{cap}")
            if cap in (row.available_capabilities or []):
                pipe.sadd(key, fid)
            else:
                pipe.srem(key, fid)
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001 - cache is derived; warm-up job repairs it
        log.warning("facility_cache_refresh_failed", error=repr(exc))


async def warm_redis() -> int:
    """Rebuild facility hot copies and volunteer GEO sets from PostgreSQL (database.md §15.6)."""
    n = 0
    async with engine().connect() as conn:
        ids = (await conn.execute(select(T.facilities.c.id).where(T.facilities.c.deleted_at.is_(None)))).scalars().all()
        for fid in ids:
            await refresh_facility_cache(fid, conn)
            n += 1
        vp, v = T.volunteer_profiles, T.villages
        rows = (await conn.execute(select(vp.c.user_id, v.c.block_code, lat_of(vp.c.last_location).label("lat"),
                                          lng_of(vp.c.last_location).label("lng"), vp.c.home_village_id)
                                   .join(v, v.c.id == vp.c.home_village_id)
                                   .where(vp.c.available, vp.c.last_location_at > datetime.now(UTC) - timedelta(minutes=10)))).all()
        for r in rows:
            try:
                await rds.r().geoadd(rds.k(f"vol:geo:{r.block_code}"), (float(r.lng), float(r.lat), str(r.user_id)))
            except Exception:  # noqa: BLE001, S110
                pass
    try:
        keys = [key async for key in rds.r().scan_iter(match=rds.k("cfg:*"))]
        if keys:
            await rds.r().delete(*keys)
    except Exception:  # noqa: BLE001, S110
        pass
    return n


