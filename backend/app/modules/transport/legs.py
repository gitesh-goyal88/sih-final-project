"""Transport and custody (TRD §10, API-Guide §6.8, SECURITY SF-01/SF-06, database.md §5.9).

Leg planning (TRD §10.1) from the village survey (`village_waypoints`):
  no roadhead surveyed             → 1 leg  house → facility (road access to the house)
  roadhead only                    → 2 legs house → roadhead (2-wheeler ok) · roadhead → facility (4-wheeler)
  roadhead + junction              → 3 legs house → junction → roadhead → facility
  pickup already at a facility     → 1 leg  facility → facility, no volunteer search
Leg 1 is always known at T1; the last leg's destination is filled in when a facility accepts.

Volunteer search (TRD §10.2, ADR-09): nearest 3 available, verified, non-busy volunteers with a suitable
vehicle in the search village are offered in parallel; first accept wins (Redis `lock:leg` for speed,
`vo_one_winner_uq` + `tl_one_active_leg_per_volunteer_uq` for correctness). Offers expire after
`volunteer.offer_expiry_s` (45 s, UI "Auto-skip"); while the village round (`volunteer.round_timeout_s`,
2 min) is open, expired offers are refilled from the same village; then the next linked village is
searched; after all linked villages → escalate `volunteer_exhausted` (ASHA + admin; family sees 108/JSSK).

Before accept a volunteer sees village + landmark-level area + category; the exact pin only after winning
the race (SEC-PRV-03). Unverified SMS cases hold dispatch until a human decides (SEC-CH-02).
"""

from __future__ import annotations

import base64
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import and_, exists, func, insert, select, update

from app.core import cfg
from app.core import redis as rds
from app.core.crypto import (
    b64u_decode,
    constant_time_eq,
    decrypt,
    encrypt,
    mask_phone,
    normalise_phone,
    phone_hash,
    scrypt_hash,
)
from app.core.db import Conn, T, lat_of, lng_of, point
from app.core.errors import AppError
from app.core.ids import uuid7
from app.core.logging import security_event
from app.core.uow import CommitThenRaise, UoW, case_scopes
from app.integrations.router import haversine_m, router
from app.modules.referral import core, timers

TWO_WHEELER_OK = ("bike", "auto", "car", "tractor", "jeep")
FOUR_WHEELER = ("car", "jeep", "ambulance")
HANDOVER_MAX_ATTEMPTS = 5
HANDOVER_WINDOW = timedelta(minutes=30)
HANDOVER_GPS_TOLERANCE_M = 2000  # "GPS plausible against the leg's handover point" — threshold set in this build


def leg_select() -> Any:
    tl = T.transport_legs
    return select(tl, lat_of(tl.c.from_point).label("from_lat"), lng_of(tl.c.from_point).label("from_lng"),
                  lat_of(tl.c.to_point).label("to_lat"), lng_of(tl.c.to_point).label("to_lng"))


async def legs_of(conn: Conn, case_id: Any) -> list[Any]:
    tl = T.transport_legs
    return list((await conn.execute(leg_select().where(tl.c.case_id == case_id).order_by(tl.c.leg_order))).all())


async def load_leg(conn: Conn, leg_id: Any, *, lock: bool = False) -> Any:
    q = leg_select().where(T.transport_legs.c.id == leg_id)
    if lock:
        q = q.with_for_update(of=T.transport_legs)
    return (await conn.execute(q)).first()


# ---------------- planning (T1) ----------------

async def plan_legs(tx: UoW, case: Any) -> list[Any]:
    """Create legs at T1 (TRD §10.1). Returns the new leg ids."""
    w = T.village_waypoints
    wps = (await tx.conn.execute(select(w.c.id, w.c.kind, w.c.label, w.c.is_default,
                                        lat_of(w.c.location).label("lat"), lng_of(w.c.location).label("lng"))
                                 .where(w.c.village_id == case.village_id, w.c.deleted_at.is_(None)))).all() if case.village_id else []
    roadhead = next((x for x in wps if x.kind == "roadhead" and x.is_default), None)
    junction = next((x for x in wps if x.kind == "junction"), None)
    start_kind = "facility" if case.location_source == "facility" else (
        "house" if case.location_source in ("gps", "household") else "village")
    start = (case.pickup_lat, case.pickup_lng) if case.pickup_lat is not None else None
    plan: list[dict[str, Any]] = []
    if start_kind == "facility" or roadhead is None:
        plan.append({"from_kind": start_kind, "from": start, "to_kind": "facility", "vneed": "four_wheeler"})
    elif junction is not None:
        plan += [{"from_kind": start_kind, "from": start, "to_kind": "junction", "to_wp": junction, "vneed": "two_wheeler_ok"},
                 {"from_kind": "junction", "from_wp": junction, "to_kind": "roadhead", "to_wp": roadhead, "vneed": "two_wheeler_ok"},
                 {"from_kind": "roadhead", "from_wp": roadhead, "to_kind": "facility", "vneed": "four_wheeler"}]
    else:
        plan += [{"from_kind": start_kind, "from": start, "to_kind": "roadhead", "to_wp": roadhead, "vneed": "two_wheeler_ok"},
                 {"from_kind": "roadhead", "from_wp": roadhead, "to_kind": "facility", "vneed": "four_wheeler"}]
    ids = []
    for order, leg in enumerate(plan, start=1):
        lid = uuid7()
        frm = leg.get("from") or ((leg["from_wp"].lat, leg["from_wp"].lng) if leg.get("from_wp") else None)
        to = (leg["to_wp"].lat, leg["to_wp"].lng) if leg.get("to_wp") else None
        await tx.conn.execute(insert(T.transport_legs).values(
            id=lid, case_id=case.id, leg_order=order, from_kind=leg["from_kind"],
            from_point=point(*frm) if frm else None, from_waypoint_id=leg["from_wp"].id if leg.get("from_wp") else None,
            to_kind=leg["to_kind"], to_point=point(*to) if to else None,
            to_waypoint_id=leg["to_wp"].id if leg.get("to_wp") else None, vehicle_kind_needed=leg["vneed"]))
        ids.append(lid)
        await core.event(tx, case.id, "leg_planned", {"legOrder": order, "fromKind": leg["from_kind"],
                                                      "toKind": leg["to_kind"]}, leg_id=lid, system=True)
    return ids


async def set_destination_facility(tx: UoW, case_id: Any, facility_id: Any) -> None:
    """When a facility accepts, legs ending at 'facility' get their destination (TRD §10.1)."""
    f, tl = T.facilities, T.transport_legs
    loc = select(f.c.location).where(f.c.id == facility_id).scalar_subquery()
    await tx.conn.execute(update(tl).where(and_(tl.c.case_id == case_id, tl.c.to_kind == "facility",
                                                tl.c.status.notin_(("handed_over", "cancelled"))))
                          .values(to_facility_id=facility_id, to_point=loc))


# ---------------- volunteer search ----------------

async def _dispatch_allowed(conn: Conn, case: Any) -> bool:
    """SEC-CH-02: unverified cases dispatch only after VerifyCase or a human DispatchUnverified decision."""
    if case.verified:
        return True
    ce = T.case_escalations
    return bool((await conn.execute(select(exists().where(and_(
        ce.c.case_id == case.id, ce.c.reason == "unverified_sms", ce.c.resolved_at.is_not(None)))))).scalar())


async def _search_villages(conn: Conn, village_id: Any) -> list[Any]:
    vl = T.village_links
    links = (await conn.execute(select(vl.c.linked_village_id).where(vl.c.village_id == village_id)
                                .order_by(vl.c.search_rank))).scalars().all()
    return [village_id, *links]


async def find_volunteer(leg_id: Any, round_no: int | None = None) -> None:
    """Worker entry (emergency queue). Offers a batch in the current (or next) village round."""
    from app.core.uow import uow

    async with uow() as tx:
        leg = await load_leg(tx.conn, leg_id, lock=True)
        if leg is None or leg.status != "open":
            return
        case = await core.load_case(tx, leg.case_id, lock=True)
        if case is None or case.status not in core.OPEN or case.transport_mode == "self":
            return
        if not case.village_id or not await _dispatch_allowed(tx.conn, case):
            return
        conf = await cfg.merged(tx.conn, case.district_code)
        villages = await _search_villages(tx.conn, case.village_id)
        vo = T.volunteer_offers
        last = (await tx.conn.execute(select(func.max(vo.c.round)).where(vo.c.leg_id == leg_id))).scalar()
        rnd = round_no if round_no is not None else (last if last is not None else 0)
        round_timeout = int(conf.get("volunteer.round_timeout_s", 120))
        while rnd < len(villages):
            started = (await tx.conn.execute(select(func.min(vo.c.offered_at)).where(
                vo.c.leg_id == leg_id, vo.c.round == rnd))).scalar()
            if started is not None and started < datetime.now(UTC) - timedelta(seconds=round_timeout):
                rnd += 1
                continue
            n = await _offer_batch(tx, case, leg, villages[rnd], rnd, conf)
            if n:
                return
            if started is not None:
                # round still open and all candidates already offered: wait for the round to end
                pending = (await tx.conn.execute(select(exists().where(and_(vo.c.leg_id == leg_id, vo.c.round == rnd,
                                                                            vo.c.result == "pending"))))).scalar()
                remaining = round_timeout - (datetime.now(UTC) - started).total_seconds()
                if pending or remaining > 0:
                    await timers.schedule(tx, case.id, "volunteer_round_timeout", max(1.0, remaining), leg_id)
                    return
            rnd += 1
        await core.escalate(tx, case, "volunteer_exhausted")


async def _offer_batch(tx: UoW, case: Any, leg: Any, village_id: Any, rnd: int, conf: dict[str, Any]) -> int:
    vp, v, veh, tl, vo, vill = (T.volunteer_profiles, T.users, T.vehicles, T.transport_legs, T.volunteer_offers,
                                T.villages)
    kinds = FOUR_WHEELER if leg.vehicle_kind_needed == "four_wheeler" else TWO_WHEELER_OK
    busy = exists().where(and_(tl.c.custodian_user_id == vp.c.user_id, tl.c.status.in_(("accepted", "picked_up"))))
    already = exists().where(and_(vo.c.leg_id == leg.id, vo.c.volunteer_id == vp.c.user_id))
    has_vehicle = exists().where(and_(veh.c.owner_user_id == vp.c.user_id, veh.c.active, veh.c.deleted_at.is_(None),
                                      veh.c.kind.in_(kinds)))
    rows = (await tx.conn.execute(select(vp.c.user_id, lat_of(vp.c.last_location).label("lat"),
                                         lng_of(vp.c.last_location).label("lng"), vp.c.last_location_at,
                                         lat_of(vill.c.location).label("vlat"), lng_of(vill.c.location).label("vlng"))
                                  .join(v, v.c.id == vp.c.user_id).join(vill, vill.c.id == vp.c.home_village_id)
                                  .where(vp.c.home_village_id == village_id, vp.c.available, vp.c.verified_at.is_not(None),
                                         v.c.status == "active", v.c.deleted_at.is_(None),
                                         ~busy, ~already, has_vehicle))).all()
    if not rows:
        return 0
    origin = (leg.from_lat, leg.from_lng) if leg.from_lat is not None else None
    fresh = timedelta(seconds=int(conf.get("volunteer.ping_fresh_s", 600)))
    cands = []
    for r in rows:
        live = await _live_ping(r.user_id)
        if live:
            loc, basis = live, "live_ping"
        elif r.lat is not None and r.last_location_at and r.last_location_at > datetime.now(UTC) - fresh:
            loc, basis = (float(r.lat), float(r.lng)), "live_ping"
        else:
            loc, basis = (float(r.vlat), float(r.vlng)), "home_village"
        d = int(haversine_m(origin, loc)) if origin else None
        cands.append((d if d is not None else 10**9, r.user_id, basis, d))
    cands.sort()
    n = int(conf.get("volunteer.parallel_offers", 3))
    expiry = int(conf.get("volunteer.offer_expiry_s", 45))
    offered = []
    for _, uid, basis, dist in cands[:n]:
        oid = uuid7()
        await tx.conn.execute(insert(vo).values(
            id=oid, leg_id=leg.id, volunteer_id=uid, round=rnd, searched_village_id=village_id, distance_m=dist,
            location_basis=basis, expires_at=datetime.now(UTC) + timedelta(seconds=expiry)))
        offered.append((oid, uid))
        await tx.journal("volunteer_offer", oid, [f"user:{uid}", f"village:{case.village_id}"])
    await core.event(tx, case.id, "volunteer_search_round", {"round": rnd, "villageId": str(village_id),
                                                             "offered": len(offered)}, leg_id=leg.id, system=True)
    await timers.schedule(tx, case.id, "volunteer_round_timeout", expiry, leg.id)
    await tx.journal("case", case.id, await case_scopes(tx.conn, case.id))

    async def _notify() -> None:
        from app.workers.registry import enqueue

        for oid, uid in offered:
            await enqueue("notify_ride_offer", offer_id=oid)
    tx.after_commit(_notify)
    return len(offered)


async def _live_ping(user_id: Any) -> tuple[float, float] | None:
    try:
        ping = await rds.r().hgetall(rds.k(f"vol:ping:{user_id}"))
        if ping and ping.get("lat"):
            return float(ping["lat"]), float(ping["lng"])
    except Exception:  # noqa: BLE001, S110
        pass
    return None


async def round_timeout(leg_id: Any) -> None:
    """Timer handler: expire stale offers, then refill the round or move to the next village."""
    from app.core.uow import uow

    async with uow() as tx:
        vo = T.volunteer_offers
        await tx.conn.execute(update(vo).where(and_(vo.c.leg_id == leg_id, vo.c.result == "pending",
                                                    vo.c.expires_at <= datetime.now(UTC))).values(result="timeout"))
    await find_volunteer(leg_id)


# ---------------- accept / decline ----------------

async def accept_leg(tx: UoW, case_id: Any, leg_id: Any, *, offer_id: Any | None, vehicle_id: Any | None,
                     location: dict[str, float] | None, channel: str = "app") -> dict[str, Any]:
    p = tx.principal
    assert p is not None
    vo, veh, tl = T.volunteer_offers, T.vehicles, T.transport_legs
    case = await core.require_case(tx, case_id)
    leg = await load_leg(tx.conn, leg_id, lock=True)
    if leg is None or leg.case_id != case.id:
        raise AppError("NOT_FOUND")
    cond = and_(vo.c.leg_id == leg_id, vo.c.volunteer_id == p.user_id)
    if offer_id:
        cond = and_(cond, vo.c.id == offer_id)
    offer = (await tx.conn.execute(select(vo).where(cond).with_for_update())).first()
    if offer is None:
        raise AppError("NOT_FOUND", "No offer for you on this leg")
    if case.status not in core.OPEN:
        raise AppError("CASE_STATE_CONFLICT", "Request cancelled", current={"id": str(case.id), "status": case.status})
    lock_key = rds.k(f"lock:leg:{leg_id}")
    try:
        won = await rds.r().set(lock_key, str(p.user_id), nx=True, px=120000)
        if not won and await rds.r().get(lock_key) != str(p.user_id):
            raise AppError("LEG_ALREADY_TAKEN", current={"id": str(leg_id), "status": "accepted"})
    except AppError:
        raise
    except Exception:  # noqa: BLE001, S110 - Redis down: DB unique indexes still pick one winner
        pass
    if leg.status != "open":
        await tx.conn.execute(update(vo).where(and_(vo.c.id == offer.id, vo.c.result == "pending"))
                              .values(result="lost_race", responded_at=datetime.now(UTC)))
        raise AppError("LEG_ALREADY_TAKEN", current={"id": str(leg_id), "status": leg.status})
    if offer.result != "pending" or offer.expires_at <= datetime.now(UTC):
        raise AppError("OFFER_EXPIRED")
    busy = (await tx.conn.execute(select(exists().where(and_(
        tl.c.custodian_user_id == p.user_id, tl.c.status.in_(("accepted", "picked_up"))))))).scalar()
    if busy:
        raise AppError("VOLUNTEER_BUSY")
    kinds = FOUR_WHEELER if leg.vehicle_kind_needed == "four_wheeler" else TWO_WHEELER_OK
    vq = select(veh.c.id).where(veh.c.owner_user_id == p.user_id, veh.c.active, veh.c.deleted_at.is_(None),
                                veh.c.kind.in_(kinds))
    if vehicle_id:
        vq = vq.where(veh.c.id == vehicle_id)
    vid = (await tx.conn.execute(vq.limit(1))).scalar()
    now = datetime.now(UTC)
    eta = None
    if leg.from_lat is not None and leg.to_lat is not None:
        eta = (await router().table((leg.from_lat, leg.from_lng), [(leg.to_lat, leg.to_lng)]))[0]
    await tx.conn.execute(update(tl).where(tl.c.id == leg_id).values(
        status="accepted", custodian_kind="volunteer", custodian_user_id=p.user_id, vehicle_id=vid, mode="volunteer",
        accepted_at=now, eta_seconds=eta.eta_s if eta else None, distance_m=eta.distance_m if eta else None))
    await tx.conn.execute(update(vo).where(vo.c.id == offer.id).values(result="accepted", responded_at=now,
                                                                       response_channel=channel))
    losers = (await tx.conn.execute(update(vo).where(and_(vo.c.leg_id == leg_id, vo.c.result == "pending"))
                                    .values(result="lost_race", responded_at=now).returning(vo.c.id, vo.c.volunteer_id))).all()
    await timers.cancel(tx, case.id, ("volunteer_round_timeout",), leg_id)
    await core.event(tx, case.id, "leg_accepted", {"legOrder": leg.leg_order, "channel": channel}, leg_id=leg_id)
    if losers:
        await core.event(tx, case.id, "leg_lost_race", {"count": len(losers)}, leg_id=leg_id, system=True)
    if case.transport_mode is None:
        case = await core.touch_case(tx, case, {"transport_mode": "volunteer"})
    await tx.audit("leg.accept", "transport_leg", leg_id, patient_id=case.patient_id, purpose="emergency_care")
    await tx.journal("transport_leg", leg_id, [f"user:{p.user_id}", f"village:{case.village_id}"])
    if leg.leg_order == 1:
        await maybe_transport_assigned(tx, case.id)

    async def _after() -> None:
        from app.modules.comms.notify import ws_publish

        await rds.r().set(rds.k(f"vol:busy:{p.user_id}"), str(leg_id), ex=6 * 3600)
        await ws_publish(f"case:{case.id}", {"type": "leg.updated", "caseId": str(case.id), "legId": str(leg_id),
                                             "status": "accepted", "handoverState": "none"})
        for lid, uid in losers:
            await ws_publish(f"user:{uid}", {"type": "leg.updated", "caseId": str(case.id), "legId": str(leg_id),
                                             "status": "accepted", "offerId": str(lid), "result": "lost_race"})
    tx.after_commit(_after)
    return await accepted_leg_view(tx, case.id, leg_id)


async def decline_leg(tx: UoW, case_id: Any, leg_id: Any, offer_id: Any | None, channel: str = "app") -> None:
    p = tx.principal
    assert p is not None
    vo = T.volunteer_offers
    cond = and_(vo.c.leg_id == leg_id, vo.c.volunteer_id == p.user_id, vo.c.result == "pending")
    if offer_id:
        cond = and_(cond, vo.c.id == offer_id)
    res = await tx.conn.execute(update(vo).where(cond).values(result="declined", responded_at=datetime.now(UTC),
                                                              response_channel=channel))
    if not res.rowcount:
        return
    remaining = (await tx.conn.execute(select(exists().where(and_(vo.c.leg_id == leg_id, vo.c.result == "pending"))))).scalar()
    if not remaining:
        async def _refill() -> None:
            from app.workers.registry import enqueue

            await enqueue("find_volunteer", leg_id=leg_id)
        tx.after_commit(_refill)


async def maybe_transport_assigned(tx: UoW, case_id: Any) -> Any:
    """T5 whichever is last of 'facility accepted' and 'leg 1 accepted' (TRD §5.1 refinement); then T6 if the
    patient is already picked up (database.md §7.1)."""
    case = await core.require_case(tx, case_id)
    if case.status != "accepted":
        return case
    legs = await legs_of(tx.conn, case_id)
    leg1 = legs[0] if legs else None
    if case.transport_mode != "self" and not (leg1 and leg1.status in ("accepted", "picked_up", "handed_over")):
        return case
    case = await core.set_status(tx, case, "transport_assigned", system=True)
    active = next((x for x in legs if x.status == "picked_up"), None)
    if active is not None:
        case = await core.set_status(tx, case, "in_transit", extra={"current_leg_id": active.id}, system=True)
    return case


# ---------------- pickup (T6) ----------------

async def confirm_pickup(tx: UoW, case: Any, leg_id: Any, location: dict[str, float] | None) -> Any:
    p = tx.principal
    assert p is not None
    tl = T.transport_legs
    leg = await load_leg(tx.conn, leg_id, lock=True)
    if leg is None or leg.case_id != case.id or leg.leg_order != 1:
        raise AppError("CASE_STATE_CONFLICT", "Pickup is confirmed on leg 1")
    if leg.status != "accepted" or leg.custodian_user_id != p.user_id:
        raise AppError("CASE_STATE_CONFLICT", "You are not the custodian of this leg",
                       current={"id": str(leg.id), "status": leg.status})
    now = datetime.now(UTC)
    await tx.conn.execute(update(tl).where(tl.c.id == leg_id).values(status="picked_up", picked_up_at=now))
    await core.event(tx, case.id, "pickup_confirmed", {"legOrder": 1}, leg_id=leg_id)
    await tx.journal("transport_leg", leg_id, [f"user:{p.user_id}", f"village:{case.village_id}"])
    conf = await cfg.merged(tx.conn, case.district_code)
    sla = (leg.eta_seconds or 1800) * int(conf.get("sla.leg_eta_multiplier", 2)) + int(conf.get("sla.leg_eta_buffer_s", 900))
    await timers.schedule(tx, case.id, "stage_sla", sla, leg_id)
    if case.status == "transport_assigned":
        return await core.set_status(tx, case, "in_transit", extra={"current_leg_id": leg_id})
    # parallel transport: leg is in progress while the facility side is still open (API-Guide §7.2)
    return await core.touch_case(tx, case, {"current_leg_id": leg_id})


# ---------------- handover (T7) ----------------

def _verify_signature(spki: bytes, message: bytes, signature: bytes) -> bool:
    key = serialization.load_der_public_key(spki)
    try:
        if isinstance(key, Ed25519PublicKey):
            key.verify(signature, message)
        elif isinstance(key, ec.EllipticCurvePublicKey):  # API < 33 fallback: ECDSA P-256 (SECURITY §8.1)
            key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        else:
            return False
        return True
    except InvalidSignature:
        return False


async def _fail_handover(tx: UoW, case: Any, leg: Any, why: str) -> AppError:
    tl = T.transport_legs
    attempts = min(HANDOVER_MAX_ATTEMPTS, leg.handover_attempts + 1)
    await tx.conn.execute(update(tl).where(tl.c.id == leg.id).values(handover_attempts=attempts))
    await core.event(tx, case.id, "handover_code_failed", {"attempts": attempts, "why": why}, leg_id=leg.id)
    security_event("case.handover_code_invalid", leg_id=str(leg.id), attempts=attempts, why=why)
    if attempts >= HANDOVER_MAX_ATTEMPTS:
        security_event("case.handover_locked", leg_id=str(leg.id))
        await core.escalate(tx, case, "manual", note="handover_locked")
        return AppError("HANDOVER_LOCKED")
    return AppError("HANDOVER_CODE_INVALID", f"{HANDOVER_MAX_ATTEMPTS - attempts} attempts left")


async def handover(tx: UoW, case_id: Any, leg_id: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Giver submits the receiver's proof; custody passes leg n → n+1 (T7). Exactly one custodian (US8)."""
    p = tx.principal
    assert p is not None
    tl = T.transport_legs
    case = await core.require_case(tx, case_id)
    leg = await load_leg(tx.conn, leg_id, lock=True)
    if leg is None or leg.case_id != case.id:
        raise AppError("NOT_FOUND")
    if leg.status != "picked_up" or leg.custodian_user_id != p.user_id:
        raise AppError("CASE_STATE_CONFLICT", "You are not the current custodian",
                       current={"id": str(leg.id), "status": leg.status})
    if leg.handover_attempts >= HANDOVER_MAX_ATTEMPTS:
        raise AppError("HANDOVER_LOCKED")
    nxt = (await tx.conn.execute(leg_select().where(and_(tl.c.case_id == case.id, tl.c.leg_order == leg.leg_order + 1))
                                 .with_for_update(of=tl))).first()
    if nxt is None:
        raise AppError("CASE_STATE_CONFLICT", "Last leg: the facility confirms arrival instead")
    if nxt.status != "accepted":
        raise AppError("CASE_STATE_CONFLICT", "Next custodian is not assigned yet",
                       current={"id": str(nxt.id), "status": nxt.status})
    method = body.get("method")
    gps = body.get("location")
    if method == "signed_qr":
        err = await _check_signed_qr(tx, leg, nxt, body)
        if err:
            raise CommitThenRaise(await _fail_handover(tx, case, leg, err))
    elif method == "sms_code":
        code = str(body.get("code") or "")
        if not nxt.handover_code_hash or not nxt.handover_code_salt or not constant_time_eq(
                bytes(nxt.handover_code_hash), scrypt_hash(code, bytes(nxt.handover_code_salt))):
            raise CommitThenRaise(await _fail_handover(tx, case, leg, "bad_code"))
    else:
        raise AppError("VALIDATION_FAILED", "method must be signed_qr or sms_code")
    now = datetime.now(UTC)
    await tx.conn.execute(update(tl).where(tl.c.id == leg.id).values(
        status="handed_over", handed_over_at=now, handover_to_leg_id=nxt.id,
        handover_location=point(gps["lat"], gps["lng"]) if gps else None))
    await tx.conn.execute(update(tl).where(tl.c.id == nxt.id).values(status="picked_up", picked_up_at=now))
    await core.event(tx, case.id, "custody_handover", {"fromLeg": leg.leg_order, "toLeg": nxt.leg_order,
                                                       "method": method}, leg_id=leg.id)
    await timers.cancel(tx, case.id, ("stage_sla", "custodian_check"), leg.id)
    conf = await cfg.merged(tx.conn, case.district_code)
    sla = (nxt.eta_seconds or 1800) * int(conf.get("sla.leg_eta_multiplier", 2)) + int(conf.get("sla.leg_eta_buffer_s", 900))
    await timers.schedule(tx, case.id, "stage_sla", sla, nxt.id)
    case = await core.touch_case(tx, case, {"current_leg_id": nxt.id})
    for lg in (leg, nxt):
        scopes = [f"village:{case.village_id}"] + ([f"user:{lg.custodian_user_id}"] if lg.custodian_user_id else [])
        await tx.journal("transport_leg", lg.id, scopes)
    await tx.audit("leg.handover", "transport_leg", leg.id, patient_id=case.patient_id, diff={"method": method})

    async def _after() -> None:
        from app.modules.comms.notify import ws_publish

        for lg, st in ((leg, "handed_over"), (nxt, "picked_up")):
            await ws_publish(f"case:{case.id}", {"type": "leg.updated", "caseId": str(case.id), "legId": str(lg.id),
                                                 "status": st, "handoverState": "verified"})
    tx.after_commit(_after)
    return {"leg": {"id": leg.id, "status": "handed_over", "handoverState": "verified"},
            "nextLeg": {"id": nxt.id, "status": "picked_up"},
            "case": {"status": case.status, "currentLegId": nxt.id}}


async def _check_signed_qr(tx: UoW, leg: Any, nxt: Any, body: dict[str, Any]) -> str | None:
    """SEC-CUS-01/02: receiver-signed assertion — signature, nonce, ±30 min, GPS plausibility."""
    try:
        raw = body["assertion"]
        assertion = json.loads(b64u_decode(raw))
        sig = b64u_decode(body["signature"])
    except (KeyError, ValueError):
        return "malformed"
    if assertion.get("legId") != str(leg.id) or assertion.get("nextLegId") != str(nxt.id):
        return "wrong_leg"
    if assertion.get("receiverUserId") != str(nxt.custodian_user_id):
        return "wrong_receiver"
    d = T.devices
    dev = (await tx.conn.execute(select(d.c.public_key_spki, d.c.revoked_at, d.c.user_id).where(
        d.c.id == assertion.get("receiverDeviceId")))).first()
    if dev is None or dev.revoked_at is not None or str(dev.user_id) != str(nxt.custodian_user_id) or not dev.public_key_spki:
        return "unknown_device"
    if not _verify_signature(bytes(dev.public_key_spki), raw.encode(), sig):
        return "bad_signature"
    try:
        ts = datetime.fromisoformat(str(assertion["ts"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return "bad_ts"
    if abs(datetime.now(UTC) - ts) > HANDOVER_WINDOW:
        return "stale"
    gps = assertion.get("gps") or body.get("location")
    if gps and leg.to_lat is not None and haversine_m((gps["lat"], gps["lng"]), (leg.to_lat, leg.to_lng)) > HANDOVER_GPS_TOLERANCE_M:
        return "gps_implausible"
    nonce = str(assertion.get("nonce", ""))
    if len(nonce) < 16:
        return "bad_nonce"
    try:
        fresh = await rds.r().set(rds.k(f"handover:nonce:{nonce}"), "1", nx=True, ex=int(HANDOVER_WINDOW.total_seconds() * 4))
    except Exception:  # noqa: BLE001
        fresh = True
    if not fresh:
        return "nonce_reused"
    return None


# ---------------- external custodians (AssignAmbulance) ----------------

async def assign_ambulance(tx: UoW, case: Any, args: dict[str, Any]) -> None:
    """Leg accepted by an external driver; driver alone receives a one-time handover code (SEC-CUS-03)."""
    tl, vo = T.transport_legs, T.volunteer_offers
    leg = await load_leg(tx.conn, args.get("legId"), lock=True)
    if leg is None or leg.case_id != case.id:
        raise AppError("NOT_FOUND", "Leg not found")
    if leg.status != "open":
        raise AppError("CASE_STATE_CONFLICT", "Leg is already taken", current={"id": str(leg.id), "status": leg.status})
    mode = args.get("mode")
    if mode not in ("ambulance", "jssk", "facility_vehicle"):
        raise AppError("VALIDATION_FAILED", "mode must be ambulance, jssk or facility_vehicle")
    phone = normalise_phone(str(args.get("driverPhone", "")))
    code = f"{secrets.randbelow(10**6):06d}"
    salt = secrets.token_bytes(16)
    await tx.conn.execute(update(tl).where(tl.c.id == leg.id).values(
        status="accepted", mode=mode, custodian_kind="ambulance_driver", accepted_at=datetime.now(UTC),
        custodian_name_enc=await encrypt(tx.conn, "external_contact", leg.id, "transport_legs", "custodian_name_enc",
                                         leg.id, str(args.get("driverName") or "Driver")),
        custodian_phone_enc=await encrypt(tx.conn, "external_contact", leg.id, "transport_legs", "custodian_phone_enc",
                                          leg.id, phone),
        custodian_phone_hash=phone_hash(phone), vehicle_id=args.get("vehicleId"),
        handover_code_hash=scrypt_hash(code, salt), handover_code_salt=salt))
    await tx.conn.execute(update(vo).where(and_(vo.c.leg_id == leg.id, vo.c.result == "pending"))
                          .values(result="cancelled"))
    await timers.cancel(tx, case.id, ("volunteer_round_timeout",), leg.id)
    if case.transport_mode is None:
        case = await core.touch_case(tx, case, {"transport_mode": mode})
    await core.event(tx, case.id, "ambulance_assigned", {"legOrder": leg.leg_order, "mode": mode}, leg_id=leg.id)
    await tx.journal("transport_leg", leg.id, [f"village:{case.village_id}"])
    if leg.leg_order == 1:
        await maybe_transport_assigned(tx, case.id)
    short = case.short_code

    async def _sms() -> None:
        from app.core.uow import uow
        from app.modules.comms.notify import send_sms

        async with uow() as t2:
            await send_sms(t2.conn, to=phone, template="handover_code", language="hi",
                           params={"case": short, "code": code}, purpose="handover_code", case_id=case.id)
    tx.after_commit(_sms)


# ---------------- views ----------------

async def custodian_view(conn: Conn, leg: Any, *, with_phone: bool) -> dict[str, Any] | None:
    if leg.custodian_user_id:
        u, d = T.users, T.devices
        user = (await conn.execute(select(u.c.id, u.c.name_enc, u.c.phone_enc).where(u.c.id == leg.custodian_user_id))).first()
        name = await decrypt(conn, "user", user.id, "users", "name_enc", user.id, user.name_enc) if user else None
        phone = await decrypt(conn, "user", user.id, "users", "phone_enc", user.id, user.phone_enc) if user else None
        dev = (await conn.execute(select(d.c.id, d.c.public_key_spki).where(and_(
            d.c.user_id == leg.custodian_user_id, d.c.revoked_at.is_(None), d.c.public_key_spki.is_not(None)))
            .order_by(d.c.last_seen_at.desc().nullslast()).limit(1))).first()
        out = {"userId": leg.custodian_user_id, "firstName": (name or "").split(" ")[0] or None,
               "phoneMasked": mask_phone(phone),
               "publicKeyEd25519": base64.b64encode(bytes(dev.public_key_spki)).decode() if dev else None,
               "deviceId": str(dev.id) if dev else None}
        if with_phone:
            out["phone"] = phone
        return out
    if leg.custodian_phone_enc is not None:
        name = await decrypt(conn, "external_contact", leg.id, "transport_legs", "custodian_name_enc", leg.id,
                             leg.custodian_name_enc)
        phone = await decrypt(conn, "external_contact", leg.id, "transport_legs", "custodian_phone_enc", leg.id,
                              leg.custodian_phone_enc)
        out = {"userId": None, "firstName": (name or "").split(" ")[0] or None, "phoneMasked": mask_phone(phone)}
        if with_phone:
            out["phone"] = phone
        return out
    return None


async def labels(conn: Conn, legs: list[Any]) -> dict[Any, tuple[str | None, str | None]]:
    w, f = T.village_waypoints, T.facilities
    wp_ids = {x.from_waypoint_id for x in legs} | {x.to_waypoint_id for x in legs}
    wp = {r.id: r.label for r in (await conn.execute(select(w.c.id, w.c.label).where(w.c.id.in_([i for i in wp_ids if i])))).all()}
    fac_ids = [x.to_facility_id for x in legs if x.to_facility_id]
    fac = {r.id: r.name for r in (await conn.execute(select(f.c.id, f.c.name).where(f.c.id.in_(fac_ids)))).all()} if fac_ids else {}
    out = {}
    for x in legs:
        frm = wp.get(x.from_waypoint_id) or {"house": "House", "village": "Village", "facility": "Facility"}.get(x.from_kind)
        to = fac.get(x.to_facility_id) or wp.get(x.to_waypoint_id) or x.to_kind.capitalize()
        out[x.id] = (frm, to)
    return out


def handover_state(leg: Any) -> str:
    if leg.status == "handed_over":
        return "verified"
    if leg.handover_attempts >= HANDOVER_MAX_ATTEMPTS:
        return "rejected"
    return "none"


async def legs_out(conn: Conn, legs: list[Any], *, projection: str, viewer: Any = None) -> list[dict[str, Any]]:
    lbl = await labels(conn, legs)
    out = []
    for x in legs:
        if projection == "volunteer" and x.custodian_user_id != viewer:
            continue
        show_pin = projection != "volunteer" or x.custodian_user_id == viewer
        out.append({
            "id": x.id, "caseId": x.case_id, "legOrder": x.leg_order, "fromKind": x.from_kind,
            "fromPoint": {"lat": float(x.from_lat), "lng": float(x.from_lng)} if show_pin and x.from_lat is not None else None,
            "fromLabel": lbl[x.id][0], "toKind": x.to_kind,
            "toPoint": {"lat": float(x.to_lat), "lng": float(x.to_lng)} if show_pin and x.to_lat is not None else None,
            "toLabel": lbl[x.id][1], "toFacilityId": x.to_facility_id, "mode": x.mode,
            "vehicleKindNeeded": x.vehicle_kind_needed, "custodianKind": x.custodian_kind,
            "custodian": await custodian_view(conn, x, with_phone=projection in ("patient", "asha", "facility")),
            "vehicleId": x.vehicle_id, "status": x.status, "handoverState": handover_state(x),
            "acceptedAt": x.accepted_at, "pickedUpAt": x.picked_up_at, "handedOverAt": x.handed_over_at,
            "etaSeconds": x.eta_seconds, "distanceM": x.distance_m, "version": x.version})
    return out


async def accepted_leg_view(tx: UoW, case_id: Any, leg_id: Any) -> dict[str, Any]:
    """API-Guide §6.8.3 200 body: exact pin, contacts, next custodian, steps."""
    from app.modules.comms.notify import household_contact, user_contact

    case = await core.load_case(tx, case_id)
    legs = await legs_of(tx.conn, case_id)
    leg = next(x for x in legs if x.id == leg_id)
    view = (await legs_out(tx.conn, [leg], projection="asha"))[0]
    view.pop("custodian", None)
    contacts: dict[str, Any] = {}
    if case.household_id:
        phone, _ = await household_contact(tx.conn, case.household_id)
        first = None
        if case.patient_id:
            pt = T.patients
            prow = (await tx.conn.execute(select(pt.c.id, pt.c.name_enc).where(pt.c.id == case.patient_id))).first()
            full = await decrypt(tx.conn, "patient", prow.id, "patients", "name_enc", prow.id, prow.name_enc) if prow else None
            first = (full or "").split(" ")[0] or None
        contacts["family"] = {"firstName": first, "phone": phone}
    asha_id = await household_asha(tx.conn, case.household_id)
    if asha_id:
        phone, _ = await user_contact(tx.conn, asha_id)
        u = T.users
        urow = (await tx.conn.execute(select(u.c.id, u.c.name_enc).where(u.c.id == asha_id))).first()
        contacts["asha"] = {"name": await decrypt(tx.conn, "user", urow.id, "users", "name_enc", urow.id, urow.name_enc),
                            "phone": phone}
    nxt = next((x for x in legs if x.leg_order == leg.leg_order + 1), None)
    next_custodian = None
    if nxt is not None and nxt.status in ("accepted", "picked_up"):
        c = await custodian_view(tx.conn, nxt, with_phone=False)
        if c:
            next_custodian = {"kind": nxt.custodian_kind, **c}
    return {"leg": view, "contacts": contacts, "nextCustodian": next_custodian,
            "steps": ["go_to_pickup", "picked_up", "reached_handover_point", "handed_over"]}


async def household_asha(conn: Conn, household_id: Any) -> Any:
    if not household_id:
        return None
    return (await conn.execute(select(T.households.c.asha_id).where(T.households.c.id == household_id))).scalar()


