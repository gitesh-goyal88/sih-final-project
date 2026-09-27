"""Generic case commands (API-Guide §6.4.4). Each validates guards on the locked row; the DB trigger is the backstop."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select, update

from app.core.db import T, point
from app.core.errors import AppError
from app.core.rbac import Principal
from app.core.uow import UoW
from app.modules.continuity import service as continuity
from app.modules.referral import core, timers

CANCEL_REASONS = ("false_alarm", "self_transported_elsewhere", "patient_deceased", "duplicate")
CANCEL_ALLOWED_LATE = {"in_transit": ("patient_deceased", "self_transported_elsewhere"),
                       "arrived_seen": ("patient_deceased", "duplicate")}


def _is_raiser(p: Principal, case: Any) -> bool:
    return case.raised_by_id is not None and case.raised_by_id == p.user_id


def _asha_of(p: Principal, case: Any) -> bool:
    return p.role == "asha" and case.village_id is not None and str(case.village_id) in p.villages


def _admin_of(p: Principal, case: Any) -> bool:
    return p.role == "district_admin" and case.district_code == p.district_code


async def run(tx: UoW, case: Any, command: str, args: dict[str, Any], expected_version: int | None) -> Any:
    from app.modules.transport import legs as tlegs

    p = tx.principal
    assert p is not None
    if expected_version is not None and expected_version != case.version:
        raise AppError("CASE_STATE_CONFLICT", "Case changed since you loaded it",
                       current={"id": str(case.id), "status": case.status, "version": case.version})

    if command == "Cancel":
        if not (_is_raiser(p, case) or _asha_of(p, case) or _admin_of(p, case)):
            raise AppError("FORBIDDEN_ROLE")
        reason = args.get("reason")
        if reason not in CANCEL_REASONS:
            raise AppError("VALIDATION_FAILED", "Invalid cancel reason", fields=[{"path": "args.reason", "code": "invalid"}])
        if case.status not in core.OPEN or (case.status in CANCEL_ALLOWED_LATE and reason not in CANCEL_ALLOWED_LATE[case.status]):
            raise AppError("CASE_STATE_CONFLICT", current={"id": str(case.id), "status": case.status})
        return await cancel(tx, case, reason, args.get("note"))

    if command == "SelfTransport":
        if not (_asha_of(p, case) or (p.role == "patient" and _is_raiser(p, case))):
            raise AppError("FORBIDDEN_ROLE")
        if case.status not in ("created", "matched", "accepted"):
            raise AppError("CASE_STATE_CONFLICT", current={"id": str(case.id), "status": case.status})
        tl, vo = T.transport_legs, T.volunteer_offers
        await tx.conn.execute(update(vo).where(and_(vo.c.leg_id.in_(select(tl.c.id).where(tl.c.case_id == case.id)),
                                                    vo.c.result == "pending")).values(result="cancelled"))
        await tx.conn.execute(update(tl).where(and_(tl.c.case_id == case.id, tl.c.status.in_(("open", "accepted"))))
                              .values(status="cancelled", cancelled_reason="self_transport"))
        await timers.cancel(tx, case.id, ("volunteer_round_timeout",))
        case = await core.touch_case(tx, case, {"transport_mode": "self"})
        await core.event(tx, case.id, "self_transport_marked", {})
        return await tlegs.maybe_transport_assigned(tx, case.id)

    if command == "AssignAmbulance":
        if not (_asha_of(p, case) or _admin_of(p, case) or (
                p.role == "facility_staff" and case.current_facility_id and str(case.current_facility_id) in p.facility_ids)):
            raise AppError("FORBIDDEN_ROLE")
        if case.status not in ("created", "matched", "accepted"):
            raise AppError("CASE_STATE_CONFLICT", current={"id": str(case.id), "status": case.status})
        await tlegs.assign_ambulance(tx, case, args)
        return await core.load_case(tx, case.id)

    if command == "ConfirmPickup":
        if p.role not in ("volunteer", "patient"):
            raise AppError("FORBIDDEN_ROLE")
        if case.status not in ("created", "matched", "accepted", "transport_assigned"):
            raise AppError("CASE_STATE_CONFLICT", current={"id": str(case.id), "status": case.status})
        return await tlegs.confirm_pickup(tx, case, args.get("legId"), args.get("location"))

    if command in ("VerifyCase", "DispatchUnverified"):
        if not (_asha_of(p, case) or _admin_of(p, case)):
            raise AppError("FORBIDDEN_ROLE")
        if case.verified or case.status not in core.OPEN:
            raise AppError("CASE_STATE_CONFLICT", current={"id": str(case.id), "status": case.status, "verified": case.verified})
        if command == "VerifyCase":
            method = args.get("method")
            if method not in ("callback", "village_match"):
                raise AppError("VALIDATION_FAILED", "method must be callback or village_match")
            values: dict[str, Any] = {"verified": True, "verified_by": p.user_id, "verified_at": datetime.now(UTC),
                                      "verification_level": "callback"}
            if args.get("patientId"):
                values["patient_id"] = args["patientId"]
            if args.get("pickup"):
                values["pickup_point"] = point(args["pickup"]["lat"], args["pickup"]["lng"])
                values["location_source"] = "gps"
            case = await core.touch_case(tx, case, values)
            await core.event(tx, case.id, "case_verified", {"method": method})
            await core.resolve_escalations(tx, case.id, "called_family", ("unverified_sms",))
        else:
            await core.resolve_escalations(tx, case.id, "other", ("unverified_sms",),
                                           note=f"dispatch_unverified: {args.get('note') or ''}"[:300])
            await core.event(tx, case.id, "case_verified", {"method": "dispatch_unverified", "verified": False})
        await _release_dispatch(tx, case)
        return await core.load_case(tx, case.id)

    if command == "UpdatePickup":
        if not (_is_raiser(p, case) or _asha_of(p, case)):
            raise AppError("FORBIDDEN_ROLE")
        if case.status not in ("created", "matched", "accepted", "transport_assigned"):
            raise AppError("CASE_STATE_CONFLICT", current={"id": str(case.id), "status": case.status})
        pk = args.get("pickup") or {}
        if "lat" not in pk or "lng" not in pk:
            raise AppError("VALIDATION_FAILED", "pickup required")
        case = await core.touch_case(tx, case, {"pickup_point": point(pk["lat"], pk["lng"]),
                                                "location_source": args.get("locationSource", "gps")})
        tl, vo = T.transport_legs, T.volunteer_offers
        leg1 = (await tx.conn.execute(select(tl.c.id, tl.c.status).where(and_(tl.c.case_id == case.id, tl.c.leg_order == 1)))).first()
        if leg1 is not None and leg1.status in ("open", "accepted"):
            await tx.conn.execute(update(tl).where(tl.c.id == leg1.id).values(from_point=point(pk["lat"], pk["lng"])))
            await tx.conn.execute(update(vo).where(and_(vo.c.leg_id == leg1.id, vo.c.result == "pending")).values(result="cancelled"))
            if leg1.status == "open":
                await _release_dispatch(tx, case)
        await core.event(tx, case.id, "leg_planned", {"updatedPickup": True})
        return case

    if command == "AddNeed":
        if p.role not in ("asha", "doctor", "facility_staff"):
            raise AppError("FORBIDDEN_ROLE")
        if case.status not in ("created", "matched"):
            raise AppError("CASE_STATE_CONFLICT", current={"id": str(case.id), "status": case.status})
        code = args.get("capabilityCode")
        if not (await tx.conn.execute(select(T.capabilities.c.code).where(T.capabilities.c.code == code))).first():
            raise AppError("VALIDATION_FAILED", "Unknown capability")
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        await tx.conn.execute(pg_insert(T.case_needed_capabilities).values(
            case_id=case.id, capability_code=code, source=p.role, added_by=p.user_id).on_conflict_do_nothing())
        await core.event(tx, case.id, "match_run", {"addedNeed": code})
        return case

    if command == "Escalate":
        if p.role not in ("asha", "facility_staff"):
            raise AppError("FORBIDDEN_ROLE")
        if case.status not in core.OPEN:
            raise AppError("CASE_STATE_CONFLICT", current={"id": str(case.id), "status": case.status})
        await core.escalate(tx, case, "manual", note=(args.get("note") or "")[:500], system=False)
        return await core.load_case(tx, case.id)

    raise AppError("VALIDATION_FAILED", f"Unknown command {command}")


async def _release_dispatch(tx: UoW, case: Any) -> None:
    tl = T.transport_legs
    leg1 = (await tx.conn.execute(select(tl.c.id).where(and_(tl.c.case_id == case.id, tl.c.leg_order == 1,
                                                             tl.c.status == "open")))).scalar()
    if leg1 is None or case.transport_mode == "self":
        return

    async def _go() -> None:
        from app.workers.registry import enqueue

        await enqueue("find_volunteer", leg_id=leg1)
    tx.after_commit(_go)


async def cancel(tx: UoW, case: Any, reason: str, note: str | None) -> Any:
    """T11: timers cancelled, offers withdrawn, legs cancelled, everyone notified, credits reversed."""
    o, tl, vo = T.facility_offers, T.transport_legs, T.volunteer_offers
    await timers.cancel(tx, case.id)
    withdrawn = (await tx.conn.execute(update(o).where(and_(o.c.case_id == case.id, o.c.result == "pending"))
                                       .values(result="withdrawn").returning(o.c.id, o.c.facility_id))).all()
    await tx.conn.execute(update(vo).where(and_(vo.c.leg_id.in_(select(tl.c.id).where(tl.c.case_id == case.id)),
                                                vo.c.result == "pending")).values(result="cancelled"))
    await tx.conn.execute(update(tl).where(and_(tl.c.case_id == case.id, tl.c.status.in_(("open", "accepted", "picked_up"))))
                          .values(status="cancelled", cancelled_reason=reason))
    case = await core.set_status(tx, case, "cancelled", extra={"cancel_reason": reason, "cancel_note": note,
                                                                "current_leg_id": None})
    await core.event(tx, case.id, "case_cancelled", {"reason": reason})
    await core.resolve_escalations(tx, case.id, "false_alarm" if reason == "false_alarm" else "closed_by_system")
    p = tx.principal
    await continuity.reverse_case_credits(tx, case.id, p.user_id if p else case.raised_by_id)

    async def _after() -> None:
        from app.modules.comms.notify import ws_publish

        for oid, fid in withdrawn:
            await ws_publish(f"facility:{fid}", {"type": "offer.closed", "offerId": str(oid), "result": "withdrawn"})
    tx.after_commit(_after)
    return case


