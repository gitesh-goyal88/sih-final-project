"""Arrival → seen → close (T8–T10, API-Guide §6.6, schema R14 `facility_admissions`)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core import cfg
from app.core.db import T
from app.core.errors import AppError
from app.core.uow import UoW
from app.modules.continuity import service as continuity
from app.modules.referral import core, timers

OUTCOMES = ("treated_discharged", "admitted", "referred_onward", "left_against_advice", "death", "other")


def _require_facility(p: Any, case: Any) -> None:
    if case.current_facility_id is None or str(case.current_facility_id) not in p.facility_ids:
        raise AppError("FORBIDDEN_SCOPE", "Only the accepting facility can do this")


async def arrived(tx: UoW, case: Any, body: dict[str, Any]) -> Any:
    p = tx.principal
    assert p is not None
    if p.role != "facility_staff" and p.role != "doctor":
        raise AppError("FORBIDDEN_ROLE")
    _require_facility(p, case)
    tl = T.transport_legs
    now = datetime.now(UTC)
    if case.status in ("accepted", "transport_assigned"):
        # walk-in / family brought the patient (API decision D12): step through the state machine
        await core.event(tx, case.id, "self_transport_marked", {"from": case.status})
        if case.status == "accepted":
            case = await core.set_status(tx, case, "transport_assigned")
        case = await core.set_status(tx, case, "in_transit")
    elif case.status != "in_transit":
        raise AppError("CASE_STATE_CONFLICT", current={"id": str(case.id), "status": case.status})
    # final custody handover to the facility; unfinished legs are closed
    await tx.conn.execute(update(tl).where(and_(tl.c.case_id == case.id, tl.c.status == "picked_up"))
                          .values(status="handed_over", handed_over_at=now))
    await tx.conn.execute(update(tl).where(and_(tl.c.case_id == case.id, tl.c.status.in_(("open", "accepted"))))
                          .values(status="cancelled", cancelled_reason="patient_arrived"))
    await tx.conn.execute(update(T.volunteer_offers).where(and_(
        T.volunteer_offers.c.leg_id.in_(select(tl.c.id).where(tl.c.case_id == case.id)),
        T.volunteer_offers.c.result == "pending")).values(result="cancelled"))
    fa = T.facility_admissions
    await tx.conn.execute(pg_insert(fa).values(case_id=case.id, facility_id=case.current_facility_id, arrived_at=now,
                                               arrived_confirmed_by=p.user_id)
                          .on_conflict_do_update(index_elements=["case_id"], set_={"arrived_at": now,
                                                                                   "arrived_confirmed_by": p.user_id}))
    eid = await core.event(tx, case.id, "patient_arrived", {"handover": (body.get("handover") or {}).get("method")})
    case = await core.set_status(tx, case, "arrived_seen")
    await continuity.credit_trips_on_arrival(tx, case, p.user_id, eid)
    await timers.cancel(tx, case.id, ("stage_sla", "custodian_check", "volunteer_round_timeout"))
    conf = await cfg.merged(tx.conn, case.district_code)
    await timers.schedule(tx, case.id, "close_reminder", int(conf.get("sla.close_reminder_h", 24)) * 3600)
    await timers.schedule(tx, case.id, "closure_escalation", int(conf.get("sla.close_escalation_h", 72)) * 3600)
    return case


async def seen(tx: UoW, case: Any) -> Any:
    p = tx.principal
    assert p is not None
    _require_facility(p, case)
    if case.status != "arrived_seen":
        raise AppError("CASE_STATE_CONFLICT", "Mark arrived first", current={"id": str(case.id), "status": case.status})
    fa = T.facility_admissions
    await tx.conn.execute(update(fa).where(and_(fa.c.case_id == case.id, fa.c.seen_at.is_(None)))
                          .values(seen_at=datetime.now(UTC), seen_by=p.user_id))
    await core.event(tx, case.id, "patient_seen", {})
    return case


async def close(tx: UoW, case: Any, body: dict[str, Any]) -> Any:
    from app.modules.referral import cases as case_svc
    from app.modules.routine_care.service import insert_entry

    p = tx.principal
    assert p is not None
    _require_facility(p, case)
    if case.status != "arrived_seen":
        raise AppError("CASE_STATE_CONFLICT", current={"id": str(case.id), "status": case.status})
    fa = T.facility_admissions
    adm = (await tx.conn.execute(select(fa).where(fa.c.case_id == case.id))).first()
    if adm is None or adm.seen_at is None:
        raise AppError("CASE_STATE_CONFLICT", "Mark 'seen by doctor' first")
    outcome = body.get("outcome")
    entry = body.get("outcomeEntry") or {}
    if outcome not in OUTCOMES or not entry.get("id"):
        raise AppError("VALIDATION_FAILED", "outcome and outcomeEntry are required",
                       fields=[{"path": "outcome", "code": "missing"}])
    if case.patient_id is None:
        raise AppError("VALIDATION_FAILED", "Link the patient to this case before closing")
    entry = {**entry, "kind": entry.get("kind", "referral_outcome")}
    await insert_entry(tx, p, case.patient_id, entry, case_id=case.id, facility_id=case.current_facility_id)
    onward_id = None
    if outcome == "referred_onward":
        onward = body.get("onwardReferral")
        if not onward:
            raise AppError("VALIDATION_FAILED", "onwardReferral is required for referred_onward")
        onward = {**onward, "patientId": str(case.patient_id), "originFacilityId": str(case.current_facility_id)}
        onward_case = await case_svc.create_referral(tx, p, onward)
        onward_id = onward_case.id
    plan = body.get("carePlan")
    if plan:
        await continuity.save_care_plan(tx, p, {**plan, "patientId": case.patient_id}, case_id=case.id,
                                        facility_id=case.current_facility_id)
    now = datetime.now(UTC)
    await tx.conn.execute(update(fa).where(fa.c.case_id == case.id).values(
        outcome=outcome, outcome_entry_id=entry["id"], onward_case_id=onward_id, closed_at=now, closed_by=p.user_id))
    eid = await core.event(tx, case.id, "case_closed", {"outcome": outcome})
    case = await core.set_status(tx, case, "closed")
    await timers.cancel(tx, case.id, ("close_reminder", "closure_escalation"))
    await core.resolve_escalations(tx, case.id, "closed_by_system", ("closure_overdue",))
    asha_id = (await tx.conn.execute(select(T.households.c.asha_id).where(T.households.c.id == case.household_id))).scalar() \
        if case.household_id else None
    if asha_id and asha_id != p.user_id:
        await continuity.credit(tx, user_id=asha_id, kind="referral_closed", verified_by=p.user_id,
                                district_code=case.district_code, case_id=case.id, event_id=eid, village_id=case.village_id)

    async def _after() -> None:
        from app.workers.registry import enqueue

        await enqueue("create_follow_ups", case_id=case.id)
    tx.after_commit(_after)
    return case


async def pre_register(tx: UoW, case: Any, reg_no: str | None) -> Any:
    p = tx.principal
    assert p is not None
    _require_facility(p, case)
    fa = T.facility_admissions
    now = datetime.now(UTC)
    await tx.conn.execute(pg_insert(fa).values(case_id=case.id, facility_id=case.current_facility_id,
                                               pre_registered_at=now, facility_reg_no=reg_no)
                          .on_conflict_do_update(index_elements=["case_id"],
                                                 set_={"pre_registered_at": now, "facility_reg_no": reg_no}))
    await core.event(tx, case.id, "pre_registered", {})
    return (await tx.conn.execute(select(fa).where(fa.c.case_id == case.id))).first()
