"""Follow-up tasks, incentives, leaderboards (API-Guide §6.9)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header
from pydantic import Field
from sqlalchemy import and_, select

from app.core import paging
from app.core.db import T
from app.core.errors import AppError
from app.core.rbac import Principal, can_access_patient, policy
from app.core.shapes import FollowUpTaskOut, In
from app.core.uow import uow
from app.modules.continuity import service as svc

router = APIRouter(tags=["continuity"])
CC = "continuity_of_care"


@router.get("/tasks")
async def list_tasks(assignee: str | None = None, status: str = "open", dueBefore: date | None = None,  # noqa: N803
                     villageId: uuid.UUID | None = None,  # noqa: N803
                     p: Principal = Depends(policy("asha", "district_admin", purpose=CC))) -> dict[str, Any]:
    ft, pt, h, v = T.follow_up_tasks, T.patients, T.households, T.villages
    statuses = status.split(",")
    async with uow(p) as tx:
        if p.role == "asha":
            cond = and_(ft.c.asha_id == p.user_id, ft.c.status.in_(statuses))
            if dueBefore:
                cond = and_(cond, ft.c.due_date <= dueBefore)
            rows = (await tx.conn.execute(select(ft).where(cond).order_by(
                (ft.c.priority == "urgent").desc(), ft.c.due_date))).all()
            data = await svc.tasks_out(tx.conn, rows)
        else:
            # district view — projection without patient names (API-Guide §6.9)
            cond = and_(ft.c.status.in_(statuses), v.c.district_code == p.district_code)
            if villageId:
                cond = and_(cond, v.c.id == villageId)
            rows = (await tx.conn.execute(select(ft).join(pt, pt.c.id == ft.c.patient_id).join(h, h.c.id == pt.c.household_id)
                                          .join(v, v.c.id == h.c.village_id).where(cond).order_by(ft.c.due_date).limit(500))).all()
            data = await svc.tasks_out(tx.conn, rows, with_names=False)
    return {"data": [FollowUpTaskOut.model_validate(d).model_dump(by_alias=True) for d in data], "total": len(data)}


class TaskIn(In):
    id: uuid.UUID
    patient_id: uuid.UUID
    task_type: Literal["anc_visit", "pnc_visit", "newborn_check", "bp_check", "sugar_check", "medicine_adherence",
                       "referral_followup", "high_risk_recheck", "immunisation", "teleconsult_followup", "other"]
    title: str | None = Field(default=None, max_length=120)
    due_date: date
    priority: Literal["urgent", "normal"] = "normal"


@router.post("/tasks", status_code=201, response_model=FollowUpTaskOut)
async def create_task(body: TaskIn, p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    if body.task_type == "other" and not body.title:
        raise AppError("VALIDATION_FAILED", "title required for 'other'")
    async with uow(p) as tx:
        if not await can_access_patient(tx.conn, p, body.patient_id, write=True):
            raise AppError("FORBIDDEN_SCOPE")
        tid = await svc.make_task(tx, patient_id=body.patient_id, asha_id=p.user_id, task_type=body.task_type,
                                  due=body.due_date, source_kind="manual", dedupe_key=f"manual:{body.id}",
                                  priority=body.priority, title=body.title)
        await tx.audit("task.create", "follow_up_task", tid, patient_id=body.patient_id)
        row = (await tx.conn.execute(select(T.follow_up_tasks).where(
            T.follow_up_tasks.c.dedupe_key == f"manual:{body.id}"))).first()
        return (await svc.tasks_out(tx.conn, [row]))[0]


@router.patch("/tasks/{task_id}", response_model=FollowUpTaskOut)
async def patch_task(task_id: uuid.UUID, body: dict[str, Any], if_match: str | None = Header(default=None),
                     p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    async with uow(p) as tx:
        row = (await tx.conn.execute(select(T.follow_up_tasks).where(T.follow_up_tasks.c.id == task_id))).first()
        if row is None or row.asha_id != p.user_id:
            raise AppError("NOT_FOUND")
        paging.if_match(if_match, row.version, {"id": str(task_id), "version": row.version})
        row = await svc.update_task(tx, p, task_id, body)
        return (await svc.tasks_out(tx.conn, [row]))[0]


class TaskCommandIn(In):
    command: Literal["Reopen", "Cancel", "MarkMissed"]
    args: dict[str, Any] = Field(default_factory=dict)


@router.post("/tasks/{task_id}/commands", response_model=FollowUpTaskOut)
async def task_command(task_id: uuid.UUID, body: TaskCommandIn, p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    async with uow(p) as tx:
        row = await svc.task_command(tx, p, task_id, body.command, body.args)
        return (await svc.tasks_out(tx.conn, [row]))[0]


@router.get("/incentives/me")
async def my_incentives(month: str | None = None, p: Principal = Depends(policy("volunteer", "asha", purpose="administration"))) -> dict[str, Any]:
    m = datetime.strptime(month, "%Y-%m").date() if month else date.today().replace(day=1)
    async with uow(p) as tx:
        return await svc.my_incentives(tx.conn, p, m)


@router.get("/leaderboards")
async def leaderboards(scope: Literal["village", "district"] = "village", scopeId: str | None = None,  # noqa: N803
                       period: Literal["month"] = "month", role: Literal["volunteer", "asha"] | None = None,
                       p: Principal = Depends(policy("volunteer", "asha", "district_admin", purpose="administration"))) -> dict[str, Any]:
    async with uow(p) as tx:
        data = await svc.leaderboard(tx.conn, p, scope=scope, period_start=date.today().replace(day=1),
                                     scope_id=scopeId, role=role)
    return {"data": data}


