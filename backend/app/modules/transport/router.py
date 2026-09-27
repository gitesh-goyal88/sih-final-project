"""Volunteer availability, ride offers, legs, custody handover (API-Guide §6.8)."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import Field
from sqlalchemy import and_, select, update

from app.core import paging
from app.core import redis as rds
from app.core.crypto import b64u
from app.core.db import T, lat_of, lng_of, point
from app.core.errors import AppError
from app.core.rbac import Principal, policy
from app.core.shapes import GeoPoint, In, Out
from app.core.uow import uow
from app.integrations.router import haversine_m
from app.integrations.router import router as route_adapter
from app.modules.transport import legs as tlegs

router = APIRouter(tags=["transport"])
EC = "emergency_care"


class AvailabilityIn(In):
    available: bool
    location: GeoPoint | None = None


def _coarse(v: float) -> float:
    return round(v, 2)  # ~1 km — the DB keeps only a coarse last location (SF-17)


@router.post("/volunteers/me/availability")
async def availability(body: AvailabilityIn, p: Principal = Depends(policy("volunteer", purpose=EC, allow_pending=True))) -> dict[str, Any]:
    vp, v = T.volunteer_profiles, T.villages
    async with uow(p) as tx:
        prof = (await tx.conn.execute(select(vp).where(vp.c.user_id == p.user_id).with_for_update())).first()
        if prof is None:
            raise AppError("NOT_FOUND", "Volunteer profile not set up")
        if body.available and prof.verified_at is None:
            raise AppError("VOLUNTEER_NOT_VERIFIED")
        now = datetime.now(UTC)
        values: dict[str, Any] = {"available": body.available, "available_changed_at": now}
        if body.location and body.available:
            values.update(last_location=point(_coarse(body.location.lat), _coarse(body.location.lng)), last_location_at=now)
        await tx.conn.execute(update(vp).where(vp.c.user_id == p.user_id).values(**values))
        await tx.journal("volunteer_profile", p.user_id, [f"user:{p.user_id}"])
        await tx.audit("volunteer.availability", "volunteer_profile", p.user_id, diff={"available": body.available})
        block = (await tx.conn.execute(select(v.c.block_code).where(v.c.id == prof.home_village_id))).scalar()

    geo_key = rds.k(f"vol:geo:{block}")
    try:
        if body.available and body.location:
            await rds.r().geoadd(geo_key, (body.location.lng, body.location.lat, str(p.user_id)))
            await rds.r().hset(rds.k(f"vol:ping:{p.user_id}"), mapping={
                "lat": body.location.lat, "lng": body.location.lng, "acc": body.location.accuracy_m or 0,
                "at": now.isoformat(), "villageId": str(prof.home_village_id)})
            await rds.r().expire(rds.k(f"vol:ping:{p.user_id}"), 600)
        elif not body.available:
            await rds.r().zrem(geo_key, str(p.user_id))
            await rds.r().delete(rds.k(f"vol:ping:{p.user_id}"))
    except Exception:  # noqa: BLE001, S110
        pass
    return {"available": body.available, "availableChangedAt": now}


class LocationIn(In):
    location: GeoPoint
    leg_id: uuid.UUID | None = None
    at: datetime | None = None


@router.post("/volunteers/me/location", status_code=204)
async def location(body: LocationIn, p: Principal = Depends(policy("volunteer", purpose=EC))) -> Response:
    """Live pings go to Redis with a TTL only (SEC-PRV-05); during a leg they drive the ETA and tracker."""
    tl, vp = T.transport_legs, T.volunteer_profiles
    async with uow(p) as tx:
        active = (await tx.conn.execute(tlegs.leg_select().where(and_(
            tl.c.custodian_user_id == p.user_id, tl.c.status.in_(("accepted", "picked_up")))))).first()
        prof = (await tx.conn.execute(select(vp.c.available, vp.c.home_village_id).where(vp.c.user_id == p.user_id))).first()
    if active is None and (prof is None or not prof.available):
        return Response(status_code=204)  # never track an unavailable volunteer
    lat, lng = body.location.lat, body.location.lng
    try:
        await rds.r().hset(rds.k(f"vol:ping:{p.user_id}"), mapping={"lat": lat, "lng": lng,
                                                                    "at": datetime.now(UTC).isoformat()})
        await rds.r().expire(rds.k(f"vol:ping:{p.user_id}"), 600)
    except Exception:  # noqa: BLE001, S110
        pass
    if active is not None:
        target = (active.from_lat, active.from_lng) if active.status == "accepted" else (active.to_lat, active.to_lng)
        eta = None
        if target[0] is not None:
            eta = (await route_adapter().table((lat, lng), [target]))[0].eta_s
            try:
                await rds.r().hset(rds.k(f"eta:case:{active.case_id}"), mapping={"legOrder": active.leg_order, "etaS": eta,
                                                                                 "at": datetime.now(UTC).isoformat()})
                await rds.r().expire(rds.k(f"eta:case:{active.case_id}"), 600)
            except Exception:  # noqa: BLE001, S110
                pass
        from app.modules.comms.notify import ws_publish

        await ws_publish(f"case:{active.case_id}", {"type": "leg.location", "legId": str(active.id),
                                                    "point": {"lat": round(lat, 3), "lng": round(lng, 3)},
                                                    "at": datetime.now(UTC).isoformat(),
                                                    "etaMin": max(1, round(eta / 60)) if eta else None})
    return Response(status_code=204)


class RideOfferOut(Out):
    offer_id: uuid.UUID
    leg_id: uuid.UUID
    case_id: uuid.UUID
    case_short_code: str
    category: str | None
    area: dict[str, Any]
    destination: dict[str, Any]
    verified: bool
    expires_at: datetime
    server_time: datetime


@router.get("/volunteers/me/offers")
async def my_offers(result: str = "pending", p: Principal = Depends(policy("volunteer", purpose=EC))) -> dict[str, list[RideOfferOut]]:
    """Coarse area before accept — no pin, no house, no patient name (SEC-PRV-03)."""
    vo, tl, c, v, w = T.volunteer_offers, T.transport_legs, T.cases, T.villages, T.village_waypoints
    q = (select(vo.c.id, vo.c.leg_id, vo.c.expires_at, vo.c.distance_m, tl.c.to_kind, tl.c.to_waypoint_id,
                c.c.id.label("case_id"), c.c.short_code, c.c.emergency_category, c.c.verified, c.c.village_id,
                lat_of(tl.c.from_point).label("lat"), lng_of(tl.c.from_point).label("lng"))
         .join(tl, tl.c.id == vo.c.leg_id).join(c, c.c.id == tl.c.case_id)
         .where(vo.c.volunteer_id == p.user_id, vo.c.result.in_(result.split(","))))
    if result == "pending":
        q = q.where(vo.c.expires_at > datetime.now(UTC))
    out = []
    async with uow(p) as tx:
        for r in (await tx.conn.execute(q.order_by(vo.c.expires_at))).all():
            vname = (await tx.conn.execute(select(v.c.name).where(v.c.id == r.village_id))).scalar()
            landmark = None
            if r.lat is not None and r.village_id:
                wps = (await tx.conn.execute(select(w.c.label, lat_of(w.c.location).label("lat"),
                                                    lng_of(w.c.location).label("lng")).where(w.c.village_id == r.village_id))).all()
                if wps:
                    near = min(wps, key=lambda x: haversine_m((r.lat, r.lng), (x.lat, x.lng)))
                    landmark = f"near {near.label}"
            dest_label = (await tx.conn.execute(select(w.c.label).where(w.c.id == r.to_waypoint_id))).scalar() \
                if r.to_waypoint_id else ("Hospital" if r.to_kind == "facility" else r.to_kind.capitalize())
            out.append({"offerId": r.id, "legId": r.leg_id, "caseId": r.case_id, "caseShortCode": r.short_code,
                        "category": r.emergency_category,
                        "area": {"villageName": vname, "landmark": landmark,
                                 "distanceKm": round((r.distance_m or 0) / 1000) if r.distance_m is not None else None},
                        "destination": {"kind": r.to_kind, "label": dest_label}, "verified": r.verified,
                        "expiresAt": r.expires_at, "serverTime": datetime.now(UTC)})
    return {"data": out}  # type: ignore[dict-item]


@router.get("/volunteers/me/legs")
async def my_legs(active: bool = False, history: bool = False, limit: int = 20, cursor: str | None = None,
                  p: Principal = Depends(policy("volunteer", purpose=EC))) -> dict[str, Any]:
    tl, c, v, il = T.transport_legs, T.cases, T.villages, T.incentive_ledger
    async with uow(p) as tx:
        if active or not history:
            rows = (await tx.conn.execute(select(tl.c.id, tl.c.case_id).where(
                tl.c.custodian_user_id == p.user_id, tl.c.status.in_(("accepted", "picked_up"))))).all()
            return {"data": [await tlegs.accepted_leg_view(tx, r.case_id, r.id) for r in rows]}
        n = paging.limit(limit)
        cur = paging.decode(cursor)
        q = (select(tl.c.id, tl.c.status, tl.c.accepted_at, tl.c.handed_over_at, v.c.name.label("village_name"), c.c.status.label("case_status"))
             .join(c, c.c.id == tl.c.case_id).outerjoin(v, v.c.id == c.c.village_id)
             .where(tl.c.custodian_user_id == p.user_id))
        if cur:
            q = q.where(tl.c.accepted_at < cur["t"])
        rows = (await tx.conn.execute(q.order_by(tl.c.accepted_at.desc()).limit(n + 1))).all()
        credited = {r.leg_id for r in (await tx.conn.execute(select(il.c.leg_id).where(
            il.c.user_id == p.user_id, il.c.kind == "transport_trip"))).all()}
    data = [{"legId": r.id, "date": (r.handed_over_at or r.accepted_at), "villageName": r.village_name,
             "status": r.status,
             "creditState": "verified" if r.id in credited else ("pending" if r.status == "handed_over"
                                                                  and r.case_status not in ("cancelled",) else "none")}
            for r in rows[:n]]
    nxt = paging.encode({"t": rows[n - 1].accepted_at.isoformat()}) if len(rows) > n else None
    return {"data": data, "nextCursor": nxt}


class AcceptIn(In):
    offer_id: uuid.UUID | None = None
    vehicle_id: uuid.UUID | None = None
    location: GeoPoint | None = None


@router.post("/cases/{case_id}/legs/{leg_id}/accept")
async def accept_leg(case_id: uuid.UUID, leg_id: uuid.UUID, body: AcceptIn,
                     p: Principal = Depends(policy("volunteer", purpose=EC))) -> dict[str, Any]:
    async with rds.case_lock(str(case_id)):
        async with uow(p) as tx:
            return await tlegs.accept_leg(tx, case_id, leg_id, offer_id=body.offer_id, vehicle_id=body.vehicle_id,
                                          location=body.location.model_dump() if body.location else None)


class DeclineIn(In):
    offer_id: uuid.UUID | None = None


@router.post("/cases/{case_id}/legs/{leg_id}/decline", status_code=204)
async def decline_leg(case_id: uuid.UUID, leg_id: uuid.UUID, body: DeclineIn,
                      p: Principal = Depends(policy("volunteer", purpose=EC))) -> Response:
    async with uow(p) as tx:
        await tlegs.decline_leg(tx, case_id, leg_id, body.offer_id)
    return Response(status_code=204)


class HandoverIn(In):
    method: Literal["signed_qr", "sms_code"]
    assertion: str | None = Field(default=None, max_length=2000)
    signature: str | None = Field(default=None, max_length=400)
    code: str | None = Field(default=None, pattern=r"^\d{6}$")
    location: GeoPoint | None = None
    recorded_at: datetime | None = None


@router.post("/cases/{case_id}/legs/{leg_id}/handover")
async def handover(case_id: uuid.UUID, leg_id: uuid.UUID, body: HandoverIn,
                   p: Principal = Depends(policy("volunteer", "patient", purpose=EC))) -> dict[str, Any]:
    async with rds.case_lock(str(case_id)):
        async with uow(p) as tx:
            return await tlegs.handover(tx, case_id, leg_id, body.model_dump(by_alias=True, mode="json", exclude_none=True))


@router.get("/cases/{case_id}/legs/{leg_id}/handover-qr-payload")
async def handover_qr_payload(case_id: uuid.UUID, leg_id: uuid.UUID,
                              p: Principal = Depends(policy("volunteer", "patient", purpose=EC))) -> dict[str, Any]:
    """Receiver side: pre-filled payload the app signs with its device key (API-Guide §6.8.6)."""
    tl = T.transport_legs
    async with uow(p) as tx:
        mine = await tlegs.load_leg(tx.conn, leg_id)
        if mine is None or mine.case_id != case_id or mine.custodian_user_id != p.user_id or mine.leg_order < 2:
            raise AppError("NOT_FOUND")
        prev = (await tx.conn.execute(select(tl.c.id).where(and_(tl.c.case_id == case_id,
                                                                 tl.c.leg_order == mine.leg_order - 1)))).scalar()
    return {"v": 1, "legId": prev, "nextLegId": mine.id, "receiverUserId": p.user_id, "receiverDeviceId": p.device_id,
            "nonce": b64u(secrets.token_bytes(16)), "ts": datetime.now(UTC).isoformat()}


