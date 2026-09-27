"""Acceptance cascade (TRD §9, API-Guide §6.5, FR-C01..C05).

* One pending offer per case (`sequential`); `parallel_top2` district flag offers the top 2, first accept wins.
* Offer timeout `cascade.offer_timeout_s` (3 min); SMS nudge to the facility duty phone at 60 s unopened.
* Decline → next offer immediately (event-driven, FR-C03). `no_bed` marks the facility full;
  `not_our_capability` flags the capability for admin review (FR-C02).
* After 3 declines/timeouts or 10 min total → escalate to the District Admin while the cascade CONTINUES (FR-C05).
* The server always re-runs matching when offering (FR-M02).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core import cfg
from app.core import redis as rds
from app.core.db import T
from app.core.errors import AppError
from app.core.ids import uuid7
from app.core.uow import UoW, uow
from app.modules.referral import core, matching, timers


async def _needed(tx: UoW, case_id: Any) -> list[str]:
    return [c["code"] for c in await core.needed_capabilities(tx, case_id)]


async def create_offer(tx: UoW, case: Any, m: matching.Match, *, slot: int = 1, source: str = "cascade",
                       attempt: int = 1) -> Any:
    o = T.facility_offers
    conf = await cfg.merged(tx.conn, case.district_code)
    timeout = int(conf.get("cascade.offer_timeout_s", 180))
    depth = (await tx.conn.execute(select(func.count()).select_from(o).where(o.c.case_id == case.id))).scalar() or 0
    oid = uuid7()
    now = datetime.now(UTC)
    await tx.conn.execute(insert(o).values(
        id=oid, case_id=case.id, facility_id=m.facility_id, rank=min(50, depth + 1), attempt=attempt, slot=slot,
        source=source, eta_seconds=m.eta_s, eta_estimated=m.eta_estimated, distance_m=m.distance_m,
        stale_capability=m.stale, capability_unconfirmed=m.capability_unconfirmed, match_reasons=m.reasons(),
        offered_at=now, expires_at=now + timedelta(seconds=timeout)))
    await timers.schedule(tx, case.id, "offer_timeout", timeout, oid)
    await timers.schedule(tx, case.id, "offer_sms_nudge", int(conf.get("cascade.sms_nudge_after_s", 60)), oid)
    await core.event(tx, case.id, "fallback_offered" if m.capability_unconfirmed else "offer_created",
                     {"facilityId": str(m.facility_id), "rank": depth + 1, "etaMin": m.reasons()["etaMin"],
                      "stale": m.stale, "source": source}, offer_id=oid, system=source == "cascade")
    expires = now + timedelta(seconds=timeout)
    category = case.emergency_category

    async def _nudge() -> None:
        from app.modules.comms.notify import ws_publish
        from app.workers.registry import enqueue

        await ws_publish(f"facility:{m.facility_id}", {"type": "offer.created", "caseId": str(case.id),
                                                       "offerId": str(oid), "facilityId": str(m.facility_id),
                                                       "expiresAt": expires.isoformat(), "category": category})
        await enqueue("notify_facility_offer", offer_id=oid)
    tx.after_commit(_nudge)
    return oid


async def match_and_offer(case_id: Any) -> None:
    """Emergency-queue task (T2/T3). Idempotent: acts only if the case is created/matched with a free slot."""
    async with rds.case_lock(str(case_id), wait_s=15):
        async with uow() as tx:
            case = await core.load_case(tx, case_id, lock=True)
            if case is None or case.status not in ("created", "matched"):
                return
            o = T.facility_offers
            pending_slots = {r.slot for r in (await tx.conn.execute(select(o.c.slot).where(
                and_(o.c.case_id == case.id, o.c.result == "pending")))).all()}
            slots = [1, 2] if case.offer_mode == "parallel_top2" else [1]
            free = [s for s in slots if s not in pending_slots]
            if not free:
                return
            needed = await _needed(tx, case.id)
            exclude = await matching.offered_facility_ids(tx.conn, case.id)
            pickup = (case.pickup_lat, case.pickup_lng) if case.pickup_lat is not None else None
            results, no_capable = await matching.match(tx.conn, needed=needed, pickup=pickup,
                                                       district_code=case.district_code, village_id=case.village_id,
                                                       exclude=exclude)
            await core.event(tx, case.id, "match_run", {"candidates": len(results), "needed": needed,
                                                        "noCapableFacility": no_capable}, system=True)
            fails = (await tx.conn.execute(select(func.count()).select_from(o).where(and_(
                o.c.case_id == case.id, o.c.result.in_(("declined", "timeout")))))).scalar() or 0
            if no_capable:
                await core.escalate(tx, case, "no_capable_facility" if fails == 0 else "cascade_exhausted")
            if not results:
                return  # nothing left to offer; the escalation is with the district admin (reassign)
            for slot, m in zip(free, results):
                await create_offer(tx, case, m, slot=slot, source="fallback" if m.capability_unconfirmed else "cascade")
            if case.status == "created":
                case = await core.set_status(tx, case, "matched", system=True)
                await timers.cancel(tx, case.id, ("stage_sla",), None)
            conf = await cfg.merged(tx.conn, case.district_code)
            if fails >= int(conf.get("cascade.max_fails_before_escalation", 3)):
                case = await core.require_case(tx, case.id)
                await core.escalate(tx, case, "cascade_exhausted")


async def respond(tx: UoW, case_id: Any, offer_id: Any, decision: str, reason: str | None, note: str | None,
                  beds: int | None) -> dict[str, Any]:
    """Facility staff accept / decline (T3/T4). Caller holds the case lock."""
    p = tx.principal
    assert p is not None
    o, f = T.facility_offers, T.facilities
    case = await core.require_case(tx, case_id)
    offer = (await tx.conn.execute(select(o).where(and_(o.c.id == offer_id, o.c.case_id == case_id)).with_for_update())).first()
    if offer is None:
        raise AppError("NOT_FOUND")
    if str(offer.facility_id) not in p.facility_ids:
        raise AppError("FORBIDDEN_SCOPE", "Not your facility")
    current = {"id": str(case.id), "status": case.status, "version": case.version}
    if offer.result != "pending" or offer.expires_at <= datetime.now(UTC):
        if case.status not in ("created", "matched"):
            raise AppError("CASE_STATE_CONFLICT", "Case already moved on", current=current)
        raise AppError("OFFER_EXPIRED", "Case moved to the next facility", current=current)
    if case.status != "matched":
        raise AppError("CASE_STATE_CONFLICT", current=current)
    now = datetime.now(UTC)
    if decision == "accept":
        await tx.conn.execute(update(o).where(o.c.id == offer.id).values(
            result="accepted", responded_at=now, responded_by=p.user_id, responded_by_role=p.role))
        superseded = (await tx.conn.execute(update(o).where(and_(o.c.case_id == case.id, o.c.result == "pending"))
                                            .values(result="superseded").returning(o.c.id, o.c.facility_id))).all()
        for sid, _ in superseded:
            await core.event(tx, case.id, "offer_superseded", {}, offer_id=sid, system=True)
        await timers.cancel(tx, case.id, ("offer_timeout", "offer_sms_nudge"))
        await timers.cancel(tx, case.id, ("stage_sla",), timers.acceptance_ref(case.id))
        if beds is not None:
            await tx.conn.execute(update(f).where(f.c.id == offer.facility_id).values(beds_available=beds))
        await core.event(tx, case.id, "facility_accepted", {"facilityId": str(offer.facility_id)}, offer_id=offer.id)
        case = await core.set_status(tx, case, "accepted", extra={"current_facility_id": offer.facility_id})
        await tx.conn.execute(pg_insert(T.facility_admissions).values(case_id=case.id, facility_id=offer.facility_id)
                              .on_conflict_do_nothing())
        from app.modules.transport import legs as tlegs

        await tlegs.set_destination_facility(tx, case.id, offer.facility_id)
        case = await tlegs.maybe_transport_assigned(tx, case.id)
        await tx.audit("offer.accept", "facility_offer", offer.id, patient_id=case.patient_id)
        open_later = [x.id for x in await tlegs.legs_of(tx.conn, case.id) if x.status == "open" and x.leg_order > 1]

        async def _after() -> None:
            from app.modules.comms.notify import ws_publish
            from app.workers.registry import enqueue

            await ws_publish(f"facility:{offer.facility_id}", {"type": "offer.closed", "offerId": str(offer.id),
                                                               "result": "accepted"})
            for sid, fid in superseded:
                await ws_publish(f"facility:{fid}", {"type": "offer.closed", "offerId": str(sid), "result": "superseded"})
            for lid in open_later:
                await enqueue("find_volunteer", leg_id=lid)
            if beds is not None:
                await matching.refresh_facility_cache(offer.facility_id)
        tx.after_commit(_after)
    elif decision == "decline":
        if reason not in ("no_bed", "no_specialist", "equipment_down", "not_our_capability", "other"):
            raise AppError("VALIDATION_FAILED", "Decline needs a reason", fields=[{"path": "reason", "code": "missing"}])
        if reason == "other" and not (note and note.strip()):
            raise AppError("VALIDATION_FAILED", "Note required for 'other'", fields=[{"path": "note", "code": "missing"}])
        await tx.conn.execute(update(o).where(o.c.id == offer.id).values(
            result="declined", decline_reason=reason, decline_note=note, responded_at=now, responded_by=p.user_id,
            responded_by_role=p.role))
        await timers.cancel(tx, case.id, ("offer_timeout", "offer_sms_nudge"), offer.id)
        if reason == "no_bed":
            await tx.conn.execute(update(f).where(f.c.id == offer.facility_id).values(
                status="full", status_note="Declined: no bed — pending staff confirmation"))
        if reason == "not_our_capability":
            fc = T.facility_capabilities
            await tx.conn.execute(update(fc).where(and_(fc.c.facility_id == offer.facility_id,
                                                        fc.c.capability_code.in_(await _needed(tx, case.id))))
                                  .values(flagged_for_review=True))
        await core.event(tx, case.id, "facility_declined", {"facilityId": str(offer.facility_id), "reason": reason},
                         offer_id=offer.id)
        await tx.audit("offer.decline", "facility_offer", offer.id, patient_id=case.patient_id, diff={"reason": reason})

        async def _next() -> None:
            from app.modules.comms.notify import ws_publish
            from app.workers.registry import enqueue

            await ws_publish(f"facility:{offer.facility_id}", {"type": "offer.closed", "offerId": str(offer.id),
                                                               "result": "declined"})
            if reason == "no_bed":
                await matching.refresh_facility_cache(offer.facility_id)
            await enqueue("match_and_offer", case_id=case.id)  # FR-C03: next offer within 10 s
            await enqueue("notify_case_status", case_id=case.id, status="matched", version=0, finding=True)
        tx.after_commit(_next)
    else:
        raise AppError("VALIDATION_FAILED", "decision must be accept or decline")
    offer = (await tx.conn.execute(select(o).where(o.c.id == offer.id))).first()
    return {"offer": offer, "case": await core.load_case(tx, case.id)}


async def offer_timeout(offer_id: Any) -> None:
    o = T.facility_offers
    async with uow() as tx:
        offer = (await tx.conn.execute(select(o).where(o.c.id == offer_id))).first()
    if offer is None:
        return
    async with rds.case_lock(str(offer.case_id), wait_s=15):
        async with uow() as tx:
            await core.require_case(tx, offer.case_id)
            res = await tx.conn.execute(update(o).where(and_(o.c.id == offer_id, o.c.result == "pending",
                                                             o.c.expires_at <= datetime.now(UTC)))
                                        .values(result="timeout").returning(o.c.id))
            if res.first() is None:
                return
            await core.event(tx, offer.case_id, "offer_timeout", {"facilityId": str(offer.facility_id)},
                             offer_id=offer_id, system=True)
            await timers.cancel(tx, offer.case_id, ("offer_sms_nudge",), offer_id)

            async def _next() -> None:
                from app.modules.comms.notify import ws_publish
                from app.workers.registry import enqueue

                await ws_publish(f"facility:{offer.facility_id}", {"type": "offer.closed", "offerId": str(offer_id),
                                                                   "result": "timeout"})
                await enqueue("match_and_offer", case_id=offer.case_id)
                await enqueue("notify_case_status", case_id=offer.case_id, status="matched", version=0, finding=True)
            tx.after_commit(_next)


async def admin_reassign(tx: UoW, case: Any, facility_id: Any, reason: str) -> Any:
    """District admin reassignment: an `admin_reassign` offer that skips the queue (API-Guide §6.11)."""
    o = T.facility_offers
    if case.status not in ("created", "matched"):
        raise AppError("CASE_STATE_CONFLICT", current={"id": str(case.id), "status": case.status})
    for (oid, fid) in (await tx.conn.execute(update(o).where(and_(o.c.case_id == case.id, o.c.result == "pending"))
                                             .values(result="withdrawn").returning(o.c.id, o.c.facility_id))).all():
        await timers.cancel(tx, case.id, ("offer_timeout", "offer_sms_nudge"), oid)
    needed = await _needed(tx, case.id)
    pickup = (case.pickup_lat, case.pickup_lng) if case.pickup_lat is not None else None
    results, _ = await matching.match(tx.conn, needed=needed, pickup=pickup, district_code=case.district_code,
                                      village_id=case.village_id, limit=50)
    m = next((x for x in results if str(x.facility_id) == str(facility_id)), None)
    if m is None:
        fb, _ = await matching.match(tx.conn, needed=[], pickup=pickup, district_code=case.district_code,
                                     village_id=case.village_id, limit=50)
        m = next((x for x in fb if str(x.facility_id) == str(facility_id)), None)
        if m is None:
            raise AppError("VALIDATION_FAILED", "Facility not open or not in this district")
        m.missing = sorted(set(needed) - set(m.matched))
        m.capability_unconfirmed = bool(m.missing)
    prev = (await tx.conn.execute(select(func.max(o.c.attempt)).where(and_(o.c.case_id == case.id,
                                                                            o.c.facility_id == facility_id)))).scalar()
    oid = await create_offer(tx, case, m, source="admin_reassign", attempt=(prev or 0) + 1)
    if case.status == "created":
        await core.set_status(tx, case, "matched")
    await tx.audit("case.reassign", "case", case.id, diff={"facilityId": str(facility_id), "reason": reason},
                   purpose="administration")
    return oid
