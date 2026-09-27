"""Case state machine primitives (TRD §5, database.md §7, API-Guide §7).

The server is the only authority on case status. Every transition:
  Redis `lock:case:{id}` (speed) + `SELECT … FOR UPDATE` (correctness) → guard → UPDATE (the DB trigger
  `enforce_case_transition` rejects any edge not in `case_status_transitions`) → `case_events` +
  `audit_log` + `sync_changes` in the same transaction → publish `case.status_changed` after commit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import T, lat_of, lng_of
from app.core.errors import AppError
from app.core.ids import uuid7
from app.core.uow import UoW, case_scopes

OPEN = ("created", "matched", "accepted", "transport_assigned", "in_transit", "arrived_seen")


def case_select() -> Any:
    c = T.cases
    return select(c, lat_of(c.c.pickup_point).label("pickup_lat"), lng_of(c.c.pickup_point).label("pickup_lng"))


async def load_case(tx: UoW, case_id: Any, *, lock: bool = False) -> Any:
    q = case_select().where(T.cases.c.id == case_id)
    if lock:
        q = q.with_for_update(of=T.cases)
    return (await tx.conn.execute(q)).first()


async def require_case(tx: UoW, case_id: Any, *, lock: bool = True) -> Any:
    row = await load_case(tx, case_id, lock=lock)
    if row is None:
        raise AppError("NOT_FOUND")
    return row


def _actor(tx: UoW) -> tuple[Any, Any]:
    p = tx.principal
    return (p.user_id, p.role) if p else (None, None)


async def event(tx: UoW, case_id: Any, action: str, payload: dict[str, Any] | None = None, *, offer_id: Any = None,
                leg_id: Any = None, channel: str | None = None, from_status: str | None = None,
                to_status: str | None = None, recorded_at: datetime | None = None, system: bool = False) -> Any:
    """Append to the case timeline. `payload` holds IDs, codes and numbers only — never names or phones."""
    actor_id, actor_role = (None, None) if system else _actor(tx)
    eid = uuid7()
    await tx.conn.execute(insert(T.case_events).values(
        id=eid, case_id=case_id, action=action, from_status=from_status, to_status=to_status,
        actor_id=actor_id, actor_role=actor_role,
        channel=channel or ("system" if actor_id is None else ("web" if actor_role in ("doctor", "facility_staff", "district_admin") else "app")),
        offer_id=offer_id, leg_id=leg_id, payload=payload or {},
        request_id=tx.principal.request_id if tx.principal else None, recorded_at=recorded_at))
    await tx.journal("case_event", eid, await case_scopes(tx.conn, case_id))

    async def _nudge() -> None:
        from app.modules.comms.notify import ws_publish

        await ws_publish(f"case:{case_id}", {"type": "case.event_added", "caseId": str(case_id), "eventId": str(eid),
                                             "action": action})
    tx.after_commit(_nudge)
    return eid


async def set_status(tx: UoW, case: Any, to_status: str, *, extra: dict[str, Any] | None = None,
                     system: bool = False, payload: dict[str, Any] | None = None) -> Any:
    """One edge of the state machine. The caller holds the row lock; the WHERE on the old status is a final guard.
    (Client `expectedVersion` is compared at command entry — our own updates in this tx also bump `version`.)"""
    c = T.cases
    values = {"status": to_status, **(extra or {})}
    res = await tx.conn.execute(update(c).where(and_(c.c.id == case.id, c.c.status == case.status)).values(**values))
    if not res.rowcount:
        current = await load_case(tx, case.id)
        raise AppError("CASE_STATE_CONFLICT", f"Case is now '{current.status}'",
                       current={"id": str(case.id), "status": current.status, "version": current.version})
    await event(tx, case.id, "status_changed", payload, from_status=case.status, to_status=to_status, system=system)
    await tx.audit("case.transition", "case", case.id, patient_id=case.patient_id,
                   diff={"from": case.status, "to": to_status, **({k: str(v) for k, v in (extra or {}).items()})},
                   purpose="emergency_care")
    await tx.journal("case", case.id, await case_scopes(tx.conn, case.id))
    updated = await load_case(tx, case.id)

    async def _publish() -> None:
        from app.modules.comms.notify import ws_publish
        from app.workers.registry import enqueue

        msg = {"type": "case.status_changed", "caseId": str(case.id), "shortCode": updated.short_code,
               "status": to_status, "version": updated.version, "at": datetime.now(UTC).isoformat()}
        await ws_publish(f"case:{case.id}", msg)
        if updated.district_code:
            await ws_publish(f"district:{updated.district_code}", msg)
        if updated.current_facility_id:
            await ws_publish(f"facility:{updated.current_facility_id}", msg)
        await enqueue("notify_case_status", case_id=case.id, status=to_status, version=updated.version)
    tx.after_commit(_publish)
    return updated


async def touch_case(tx: UoW, case: Any, values: dict[str, Any]) -> Any:
    """Non-status update on a locked case (current leg, transport mode, …)."""
    c = T.cases
    res = await tx.conn.execute(update(c).where(c.c.id == case.id).values(**values))
    if not res.rowcount:
        raise AppError("NOT_FOUND")
    await tx.journal("case", case.id, await case_scopes(tx.conn, case.id))
    return await load_case(tx, case.id)


async def escalate(tx: UoW, case: Any, reason: str, *, level: int = 1, note: str | None = None,
                   system: bool = True) -> bool:
    """T12: open an escalation (one open per reason), bump escalation_level, alert the district admin."""
    ce = T.case_escalations
    eid = uuid7()
    res = await tx.conn.execute(pg_insert(ce).values(
        id=eid, case_id=case.id, level=level, reason=reason, district_code=case.district_code,
        raised_by=None if system or not tx.principal else tx.principal.user_id, note=note).on_conflict_do_nothing())
    if not res.rowcount:
        return False
    c = T.cases
    await tx.conn.execute(update(c).where(c.c.id == case.id).values(escalation_level=func.least(5, c.c.escalation_level + 1)))
    await event(tx, case.id, "escalated", {"reason": reason, "level": level, "escalationId": str(eid)}, system=system)
    await tx.audit("case.escalate", "case", case.id, diff={"reason": reason, "level": level}, purpose="emergency_care")

    async def _alert() -> None:
        from app.modules.comms.notify import ws_publish
        from app.workers.registry import enqueue

        await ws_publish(f"district:{case.district_code}", {"type": "escalation.opened", "escalationId": str(eid),
                                                            "caseId": str(case.id), "reason": reason, "level": level})
        await enqueue("notify_escalation", escalation_id=eid)
    tx.after_commit(_alert)
    return True


async def resolve_escalations(tx: UoW, case_id: Any, resolution: str, reasons: tuple[str, ...] | None = None,
                              note: str | None = None) -> None:
    ce = T.case_escalations
    cond = and_(ce.c.case_id == case_id, ce.c.resolved_at.is_(None))
    if reasons:
        cond = and_(cond, ce.c.reason.in_(reasons))
    rows = (await tx.conn.execute(update(ce).where(cond).values(
        resolved_at=datetime.now(UTC), resolution=resolution, note=note).returning(ce.c.id))).all()
    for (eid,) in rows:
        await event(tx, case_id, "escalation_resolved", {"escalationId": str(eid), "resolution": resolution})


async def needed_capabilities(tx: UoW, case_id: Any) -> list[dict[str, str]]:
    cnc = T.case_needed_capabilities
    rows = (await tx.conn.execute(select(cnc.c.capability_code, cnc.c.source).where(cnc.c.case_id == case_id)
                                  .order_by(cnc.c.capability_code))).all()
    return [{"code": r.capability_code, "source": r.source} for r in rows]
