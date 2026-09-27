"""Continuity: care plans → ASHA follow-up tasks, verified incentives, leaderboards (PRD pillar 1, US10, US11).

* Tasks carry a `dedupe_key` so the same trigger never creates two tasks (database.md §5.10).
* `done` is monotonic — only an explicit `Reopen` command reopens a task (TRD §6.4).
* The ledger holds only VERIFIED credit (database.md §5.10 note):
    transport_trip   — only after facility-confirmed arrival, verified by the facility staff (SEC-FRD-01, D9)
    referral_closed  — to the household's ASHA when the facility closes the case
    asha_followup    — when a visit-type task is marked done with the visit entry as proof
  Cancelled / false-alarm cases earn nothing; earlier credits are reversed (append-only reversal rows).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core import cfg
from app.core.crypto import decrypt, encrypt
from app.core.db import Conn, T
from app.core.errors import AppError
from app.core.ids import uuid5, uuid7
from app.core.rbac import Principal
from app.core.uow import UoW, patient_scopes, uow

VISIT_TASKS = ("anc_visit", "pnc_visit", "newborn_check", "bp_check", "sugar_check", "referral_followup",
               "high_risk_recheck", "immunisation", "teleconsult_followup", "medicine_adherence")


async def _rule_version(conn: Conn, key: str, district: str | None) -> tuple[int, int]:
    ce = T.config_entries
    rows = (await conn.execute(select(ce.c.district_code, ce.c.value, ce.c.version).where(and_(
        ce.c.key == key, or_(ce.c.district_code.is_(None), ce.c.district_code == district))))).all()
    row = next((r for r in rows if r.district_code == district), None) or next((r for r in rows), None)
    if row is None:
        return 0, 1
    return int(row.value), int(row.version)


async def credit(tx: UoW, *, user_id: Any, kind: str, verified_by: Any, district_code: str, case_id: Any = None,
                 leg_id: Any = None, task_id: Any = None, event_id: Any = None, entry_id: Any = None,
                 village_id: Any = None) -> bool:
    credits, version = await _rule_version(tx.conn, f"incentive.{kind}", district_code)
    if credits <= 0:
        return False
    today = date.today()
    res = await tx.conn.execute(pg_insert(T.incentive_ledger).values(
        id=uuid7(), user_id=user_id, kind=kind, credits=credits, case_id=case_id, leg_id=leg_id, task_id=task_id,
        verified_by_id=verified_by, verification_event_id=event_id, verification_entry_id=entry_id,
        district_code=district_code, village_id=village_id, period_month=today.replace(day=1),
        rule_version=version).on_conflict_do_nothing())
    if res.rowcount:
        await tx.journal("incentive", f"{user_id}:{kind}:{case_id or task_id}", [f"user:{user_id}"])
    return bool(res.rowcount)


async def credit_trips_on_arrival(tx: UoW, case: Any, verified_by: Any, event_id: Any) -> None:
    """transport_trip for every volunteer leg handed over, after facility-confirmed arrival (SEC-FRD-01)."""
    tl, vp = T.transport_legs, T.volunteer_profiles
    rows = (await tx.conn.execute(select(tl.c.id, tl.c.custodian_user_id, vp.c.home_village_id)
                                  .join(vp, vp.c.user_id == tl.c.custodian_user_id)
                                  .where(tl.c.case_id == case.id, tl.c.custodian_kind == "volunteer",
                                         tl.c.status == "handed_over"))).all()
    for r in rows:
        if r.custodian_user_id != verified_by:
            await credit(tx, user_id=r.custodian_user_id, kind="transport_trip", verified_by=verified_by,
                         district_code=case.district_code, case_id=case.id, leg_id=r.id, event_id=event_id,
                         village_id=r.home_village_id)


async def reverse_case_credits(tx: UoW, case_id: Any, verified_by: Any) -> None:
    il = T.incentive_ledger
    rows = (await tx.conn.execute(select(il).where(and_(il.c.case_id == case_id, il.c.kind != "reversal",
                                                        ~il.c.id.in_(select(il.c.reverses_entry_id).where(
                                                            il.c.reverses_entry_id.is_not(None))))))).all()
    for r in rows:
        await tx.conn.execute(insert(il).values(
            id=uuid7(), user_id=r.user_id, kind="reversal", credits=-r.credits, case_id=r.case_id, leg_id=r.leg_id,
            task_id=r.task_id, verified_by_id=verified_by, verification_event_id=r.verification_event_id,
            reverses_entry_id=r.id, district_code=r.district_code, village_id=r.village_id,
            period_month=date.today().replace(day=1), rule_version=r.rule_version))
        await tx.journal("incentive", f"{r.user_id}:reversal:{r.id}", [f"user:{r.user_id}"])


# ---------------- care plans ----------------

async def save_care_plan(tx: UoW, p: Principal, data: dict[str, Any], *, case_id: Any = None,
                         teleconsult_id: Any = None, facility_id: Any = None) -> uuid.UUID:
    cp, cpi = T.care_plans, T.care_plan_items
    plan_id = uuid.UUID(str(data["id"]))
    if (await tx.conn.execute(select(cp.c.id).where(cp.c.id == plan_id))).first():
        return plan_id
    patient_id = data["patientId"]
    case_id = case_id or data.get("caseId")
    teleconsult_id = teleconsult_id or data.get("teleconsultSessionId")
    if not case_id and not teleconsult_id:
        raise AppError("VALIDATION_FAILED", "A care plan comes from a case or a teleconsult")
    next_visit = date.fromisoformat(data["nextVisitOn"]) if data.get("nextVisitOn") else None
    # insert inactive first so the old active plan can point at it (cp_superseded_ck + cp_one_active_per_case_uq)
    await tx.conn.execute(insert(cp).values(
        id=plan_id, patient_id=patient_id, case_id=case_id, teleconsult_session_id=teleconsult_id,
        author_id=p.user_id, author_role=p.role, facility_id=facility_id or (p.facility_ids[0] if p.facility_ids else None),
        summary_enc=await encrypt(tx.conn, "patient", patient_id, "care_plans", "summary_enc", plan_id,
                                  data["summary"]) if data.get("summary") else None,
        next_visit_on=next_visit, recorded_at=datetime.now(UTC), status="cancelled"))
    if case_id:
        await tx.conn.execute(update(cp).where(and_(cp.c.case_id == case_id, cp.c.status == "active"))
                              .values(status="superseded", superseded_by=plan_id))
    await tx.conn.execute(update(cp).where(cp.c.id == plan_id).values(status="active"))
    for i, item in enumerate(data.get("items") or []):
        iid = uuid.UUID(str(item.get("id") or uuid7()))
        await tx.conn.execute(insert(cpi).values(
            id=iid, care_plan_id=plan_id, kind=item["kind"], medicine_name=item.get("medicineName"),
            dose_text=item.get("doseText"), frequency_text=item.get("frequencyText"),
            duration_days=item.get("durationDays"), due_offset_days=item.get("dueOffsetDays"),
            task_type=item.get("taskType"),
            note_enc=await encrypt(tx.conn, "patient", patient_id, "care_plan_items", "note_enc", iid, item["note"]) if item.get("note") else None,
            sort_order=item.get("sortOrder", 100 + i)))
        if item["kind"] == "medicine" and item.get("medicineName"):
            await tx.conn.execute(insert(T.patient_medications).values(
                id=uuid7(), patient_id=patient_id, medicine_name=item["medicineName"], dose_text=item.get("doseText"),
                frequency_text=item.get("frequencyText"), started_on=date.today(), prescribed_by=p.user_id,
                source_care_plan_id=plan_id, recorded_by=p.user_id, recorded_at=datetime.now(UTC)))
    await tx.journal("care_plan", plan_id, await patient_scopes(tx.conn, patient_id))
    await tx.audit("care_plan.create", "care_plan", plan_id, patient_id=patient_id, purpose="continuity_of_care")
    return plan_id


async def tasks_from_plan(tx: UoW, plan_id: Any, *, asha_id: Any, source_case_id: Any = None) -> list[Any]:
    cp, cpi = T.care_plans, T.care_plan_items
    plan = (await tx.conn.execute(select(cp).where(cp.c.id == plan_id))).first()
    if plan is None or asha_id is None:
        return []
    created = []
    base = plan.recorded_at.date()
    for item in (await tx.conn.execute(select(cpi).where(and_(cpi.c.care_plan_id == plan_id,
                                                              cpi.c.kind.in_(("visit", "test")))))).all():
        tid = await make_task(tx, patient_id=plan.patient_id, asha_id=asha_id,
                              task_type=item.task_type or ("referral_followup" if source_case_id else "teleconsult_followup"),
                              due=base + timedelta(days=item.due_offset_days or 0), source_kind="care_plan",
                              dedupe_key=f"cpi:{item.id}", care_plan_item_id=item.id, source_case_id=source_case_id)
        if tid:
            created.append(tid)
    return created


async def make_task(tx: UoW, *, patient_id: Any, asha_id: Any, task_type: str, due: date, source_kind: str,
                    dedupe_key: str, priority: str = "normal", title: str | None = None, source_case_id: Any = None,
                    source_entry_id: Any = None, care_plan_item_id: Any = None, risk_rule_id: Any = None) -> Any:
    tid = uuid5(f"task:{dedupe_key}")
    res = await tx.conn.execute(pg_insert(T.follow_up_tasks).values(
        id=tid, patient_id=patient_id, asha_id=asha_id, task_type=task_type, title=title, priority=priority,
        source_kind=source_kind, source_case_id=source_case_id, source_entry_id=source_entry_id,
        care_plan_item_id=care_plan_item_id, risk_rule_id=risk_rule_id, dedupe_key=dedupe_key, due_date=due)
        .on_conflict_do_nothing())
    if not res.rowcount:
        return None
    village = (await tx.conn.execute(select(T.households.c.village_id).join(
        T.patients, T.patients.c.household_id == T.households.c.id).where(T.patients.c.id == patient_id))).scalar()
    await tx.journal("follow_up_task", tid, [f"village:{village}", f"user:{asha_id}"])

    async def _nudge() -> None:
        from app.modules.comms.notify import ws_publish

        await ws_publish(f"user:{asha_id}", {"type": "task.created", "taskId": str(tid), "taskType": task_type,
                                             "dueDate": due.isoformat(), "priority": priority})
    tx.after_commit(_nudge)
    return tid


async def create_follow_ups(case_id: Any) -> None:
    """T10 (default queue): closed → follow_up when the household has an ASHA; ≥ 1 task at +3 days."""
    from app.core import redis as rds
    from app.modules.referral import core

    async with rds.case_lock(str(case_id), wait_s=15):
        async with uow() as tx:
            case = await core.load_case(tx, case_id, lock=True)
            if case is None or case.status != "closed" or case.patient_id is None:
                return
            h = T.households
            asha_id = (await tx.conn.execute(select(h.c.asha_id).where(h.c.id == case.household_id))).scalar() \
                if case.household_id else None
            if asha_id is None:
                return
            conf = await cfg.merged(tx.conn, case.district_code)
            days = int(conf.get("followup.after_close_days", 3))
            base = (case.closed_at or datetime.now(UTC)).date()
            created = [await make_task(tx, patient_id=case.patient_id, asha_id=asha_id, task_type="referral_followup",
                                       due=base + timedelta(days=days), source_kind="case_closed",
                                       dedupe_key=f"case:{case.id}:referral_followup:d{days}", source_case_id=case.id)]
            cp = T.care_plans
            plan = (await tx.conn.execute(select(cp.c.id).where(and_(cp.c.case_id == case.id, cp.c.status == "active")))).first()
            if plan:
                created += await tasks_from_plan(tx, plan.id, asha_id=asha_id, source_case_id=case.id)
            await core.event(tx, case.id, "follow_up_created", {"tasks": len([t for t in created if t])}, system=True)
            await core.set_status(tx, case, "follow_up", system=True)


# ---------------- tasks ----------------

async def update_task(tx: UoW, p: Principal, task_id: Any, fields: dict[str, Any], *, via_sync: bool = False) -> Any:
    ft = T.follow_up_tasks
    extra = set(fields) - {"status", "dueDate", "doneEntryId"}
    if extra:
        raise AppError("FIELD_NOT_WRITABLE", f"Not writable: {sorted(extra)}")
    task = (await tx.conn.execute(select(ft).where(ft.c.id == task_id).with_for_update())).first()
    if task is None or task.asha_id != p.user_id:
        raise AppError("NOT_FOUND")
    values: dict[str, Any] = {}
    status = fields.get("status")
    if status == "done" and task.status != "done":
        values.update(status="done", done_at=datetime.now(UTC), done_by=p.user_id, done_entry_id=fields.get("doneEntryId"))
    elif status == "open" and task.status == "done":
        pass  # monotonic: only the Reopen command reopens (TRD §6.4)
    elif status and status not in ("done", "open"):
        raise AppError("VALIDATION_FAILED", "status must be open or done (use commands for others)")
    if fields.get("dueDate") and task.status == "open":
        values["due_date"] = date.fromisoformat(fields["dueDate"])
    if fields.get("doneEntryId") and task.status == "done" and task.done_entry_id is None:
        values["done_entry_id"] = fields["doneEntryId"]
    if values:
        await tx.conn.execute(update(ft).where(ft.c.id == task_id).values(**values))
        await tx.journal("follow_up_task", task_id, await _task_scopes(tx.conn, task))
        await tx.audit("task.update", "follow_up_task", task_id, patient_id=task.patient_id,
                       diff={"fields": sorted(values)}, purpose="continuity_of_care")
    entry = values.get("done_entry_id")
    if entry and task.task_type in VISIT_TASKS:
        district = (await tx.conn.execute(select(T.villages.c.district_code).join(
            T.households, T.households.c.village_id == T.villages.c.id).join(
            T.patients, T.patients.c.household_id == T.households.c.id).where(T.patients.c.id == task.patient_id))).scalar()
        await credit(tx, user_id=p.user_id, kind="asha_followup", verified_by=p.user_id, district_code=district,
                     task_id=task_id, entry_id=entry)
    return (await tx.conn.execute(select(ft).where(ft.c.id == task_id))).first()


async def task_command(tx: UoW, p: Principal, task_id: Any, name: str, args: dict[str, Any]) -> Any:
    ft = T.follow_up_tasks
    task = (await tx.conn.execute(select(ft).where(ft.c.id == task_id).with_for_update())).first()
    if task is None or task.asha_id != p.user_id:
        raise AppError("NOT_FOUND")
    if name == "Reopen":
        values = {"status": "open", "done_at": None, "done_by": None, "reopened_count": task.reopened_count + 1}
    elif name == "Cancel":
        values = {"status": "cancelled", "done_at": None, "done_by": None}
    elif name == "MarkMissed":
        values = {"status": "missed", "done_at": None, "done_by": None}
    else:
        raise AppError("VALIDATION_FAILED", f"Unknown command {name}")
    await tx.conn.execute(update(ft).where(ft.c.id == task_id).values(**values))
    await tx.journal("follow_up_task", task_id, await _task_scopes(tx.conn, task))
    await tx.audit(f"task.{name.lower()}", "follow_up_task", task_id, patient_id=task.patient_id,
                   diff={"reason": args.get("reason")}, purpose="continuity_of_care")
    return (await tx.conn.execute(select(ft).where(ft.c.id == task_id))).first()


async def _task_scopes(conn: Conn, task: Any) -> list[str]:
    return [s for s in await patient_scopes(conn, task.patient_id) if s.startswith("village:")] + [f"user:{task.asha_id}"]


async def tasks_out(conn: Conn, rows: list[Any], *, with_names: bool = True) -> list[dict[str, Any]]:
    pt = T.patients
    ids = list({r.patient_id for r in rows})
    names: dict[Any, tuple[str | None, str | None]] = {}
    if ids:
        for prow in (await conn.execute(select(pt.c.id, pt.c.name_enc, pt.c.short_code).where(pt.c.id.in_(ids)))).all():
            nm = await decrypt(conn, "patient", prow.id, "patients", "name_enc", prow.id, prow.name_enc) if with_names else None
            names[prow.id] = (nm, prow.short_code)
    return [{"id": r.id, "patientId": r.patient_id, "patientName": names.get(r.patient_id, (None, None))[0],
             "patientShortCode": names.get(r.patient_id, (None, None))[1], "ashaId": r.asha_id,
             "taskType": r.task_type, "title": r.title, "priority": r.priority, "sourceKind": r.source_kind,
             "sourceCaseId": r.source_case_id, "dueDate": r.due_date, "status": r.status, "doneAt": r.done_at,
             "doneEntryId": r.done_entry_id, "version": r.version} for r in rows]


# ---------------- incentives & leaderboards ----------------

async def my_incentives(conn: Conn, p: Principal, month: date) -> dict[str, Any]:
    il, c, v, tl = T.incentive_ledger, T.cases, T.villages, T.transport_legs
    rows = (await conn.execute(select(il, c.c.short_code, v.c.name.label("village_name"))
                               .outerjoin(c, c.c.id == il.c.case_id).outerjoin(v, v.c.id == il.c.village_id)
                               .where(il.c.user_id == p.user_id, il.c.period_month == month)
                               .order_by(il.c.created_at.desc()))).all()
    entries = [{"id": r.id, "kind": r.kind, "credits": r.credits, "caseShortCode": r.short_code,
                "villageName": r.village_name, "createdAt": r.created_at, "state": "verified"} for r in rows]
    # "Pending" = legs handed over whose arrival is not yet facility-confirmed (computed, never in the ledger)
    pend = (await conn.execute(select(tl.c.id, tl.c.handed_over_at, c.c.short_code).join(c, c.c.id == tl.c.case_id)
                               .where(tl.c.custodian_user_id == p.user_id, tl.c.status == "handed_over",
                                      c.c.status.in_(("in_transit", "transport_assigned", "accepted"))))).all()
    for r in pend:
        entries.insert(0, {"id": r.id, "kind": "transport_trip", "credits": 0, "caseShortCode": r.short_code,
                           "villageName": None, "createdAt": r.handed_over_at, "state": "pending"})
    total = sum(e["credits"] for e in entries if e["state"] == "verified")
    activity = len([e for e in entries if e["kind"] in ("transport_trip", "asha_followup") and e["state"] == "verified"])
    board = await leaderboard(conn, p, scope="village", period_start=month)
    my_rank = next((row["rank"] for row in board if row["isMe"]), None)
    return {"credits": total, "activityCount": activity, "rank": my_rank, "rankScope": "village", "entries": entries}


async def _village_for(conn: Conn, p: Principal) -> Any:
    if p.role == "volunteer":
        return p.home_village_id
    return p.villages[0] if p.villages else None


async def leaderboard(conn: Conn, p: Principal, *, scope: str, period_start: date, scope_id: Any = None,
                      role: str | None = None) -> list[dict[str, Any]]:
    """Village top 5, first names only, opted-out users excluded (SEC-FRD-04, UI §6.4)."""
    il, u = T.incentive_ledger, T.users
    role = role or ("volunteer" if p.role == "volunteer" else "asha")
    village = scope_id or await _village_for(conn, p)
    q = (select(il.c.user_id, func.sum(il.c.credits).label("credits"),
                func.count().filter(il.c.kind != "reversal").label("activity"))
         .join(u, u.c.id == il.c.user_id)
         .where(il.c.period_month == period_start, u.c.role == role, u.c.leaderboard_opt_out.is_(False))
         .group_by(il.c.user_id).order_by(func.sum(il.c.credits).desc()).limit(5))
    if scope == "village":
        q = q.where(il.c.village_id == village)
    elif scope == "district":
        q = q.where(il.c.district_code == (scope_id or p.district_code))
    rows = (await conn.execute(q)).all()
    out = []
    for i, r in enumerate(rows, start=1):
        urow = (await conn.execute(select(u.c.id, u.c.name_enc).where(u.c.id == r.user_id))).first()
        name = await decrypt(conn, "user", urow.id, "users", "name_enc", urow.id, urow.name_enc)
        out.append({"rank": i, "firstName": (name or "").split(" ")[0], "credits": int(r.credits),
                    "activityCount": int(r.activity), "isMe": r.user_id == p.user_id})
    return out


async def recompute_leaderboards() -> None:
    """Bulk queue, hourly: snapshot village/district boards (database.md §5.10 leaderboard_snapshots)."""
    il, u, ls = T.incentive_ledger, T.users, T.leaderboard_snapshots
    month = date.today().replace(day=1)
    async with uow() as tx:
        for role in ("volunteer", "asha"):
            for scope_kind, col in (("village", il.c.village_id), ("district", il.c.district_code)):
                rows = (await tx.conn.execute(
                    select(col.label("scope_id"), il.c.user_id, func.sum(il.c.credits).label("credits"),
                           func.count().filter(il.c.kind != "reversal").label("activity"))
                    .join(u, u.c.id == il.c.user_id)
                    .where(il.c.period_month == month, u.c.role == role, col.is_not(None))
                    .group_by(col, il.c.user_id))).all()
                by_scope: dict[str, list[Any]] = {}
                for r in rows:
                    by_scope.setdefault(str(r.scope_id), []).append(r)
                for sid, items in by_scope.items():
                    items.sort(key=lambda r: -int(r.credits))
                    for rank, r in enumerate(items[:20], start=1):
                        await tx.conn.execute(pg_insert(ls).values(
                            scope_kind=scope_kind, scope_id=sid, period_kind="month", period_start=month, role=role,
                            rank=rank, user_id=r.user_id, credits=int(r.credits), activity_count=int(r.activity))
                            .on_conflict_do_update(index_elements=["scope_kind", "scope_id", "period_kind",
                                                                   "period_start", "role", "rank"],
                                                   set_={"user_id": r.user_id, "credits": int(r.credits),
                                                         "activity_count": int(r.activity),
                                                         "computed_at": datetime.now(UTC)}))
