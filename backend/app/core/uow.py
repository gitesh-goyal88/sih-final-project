"""Unit of work: one transaction per request; audit + sync journal in the SAME transaction; side effects after commit.

* SEC-LOG-01 / TRD §3.2: an `audit_log` row is written in the same transaction as every write and every
  S1 read. (TRD names a SQLAlchemy after_flush hook; this codebase uses Core, so the equivalent guarantee
  is that services write through `UoW.audit` inside the transaction — a failed audit fails the request.)
* database.md C13 / §9: one `sync_changes` row per changed syncable entity per scope, same transaction.
* database.md §0.3: "commit to PostgreSQL first, then touch derived stores" — Redis, RabbitMQ, WS
  and notifications run in `after_commit` callbacks.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import insert, select

from app.core.db import Conn, T, engine
from app.core.logging import log
from app.core.rbac import Principal

AfterCommit = Callable[[], Awaitable[Any]]


class UoW:
    def __init__(self, conn: Conn, principal: Principal | None) -> None:
        self.conn = conn
        self.principal = principal
        self._after: list[AfterCommit] = []

    def after_commit(self, fn: AfterCommit) -> None:
        self._after.append(fn)

    async def audit(self, action: str, entity: str, entity_id: Any = None, *, patient_id: Any = None,
                    diff: dict[str, Any] | None = None, outcome: str = "allowed", purpose: str | None = None) -> None:
        p = self.principal
        await self.conn.execute(insert(T.audit_log).values(
            actor_id=p.user_id if p else None,
            actor_role=p.role if p else None,
            device_id=p.device_id if p else None,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            patient_id=patient_id,
            purpose=purpose or (p.purpose if p else None),
            break_glass_grant_id=p.break_glass_grant_id if p else None,
            outcome=outcome,
            diff=diff,
            ip=p.ip if p else None,
            request_id=p.request_id if p else None,
        ))

    async def journal(self, entity: str, entity_id: Any, scopes: Iterable[str], op: str = "upsert") -> None:
        rows = [{"scope": s, "entity": entity, "entity_id": str(entity_id), "op": op} for s in dict.fromkeys(scopes) if s]
        if rows:
            await self.conn.execute(insert(T.sync_changes), rows)


class CommitThenRaise(Exception):
    """Raise inside a UoW to COMMIT what was written (failed-attempt counters, denied-access audit rows,
    lockouts) and then surface `error` to the caller. A plain exception rolls everything back."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


@asynccontextmanager
async def uow(principal: Principal | None = None, role: str | None = None) -> AsyncIterator[UoW]:
    pending: BaseException | None = None
    async with engine(role).connect() as conn:
        async with conn.begin():
            u = UoW(conn, principal)
            try:
                yield u
            except CommitThenRaise as exc:
                pending = exc.error
    for fn in u._after:
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001 - derived stores are repaired by sweeps / warm-up
            log.error("after_commit_failed", error=repr(exc), fn=getattr(fn, "__name__", "?"))
    if pending is not None:
        raise pending


# ---------------- scope fan-out (database.md §9.4) ----------------

async def household_scopes(conn: Conn, household_id: Any) -> list[str]:
    h = T.households
    village = (await conn.execute(select(h.c.village_id).where(h.c.id == household_id))).scalar()
    return [f"village:{village}", f"household:{household_id}"] if village else [f"household:{household_id}"]


async def patient_scopes(conn: Conn, patient_id: Any) -> list[str]:
    pt = T.patients
    hh = (await conn.execute(select(pt.c.household_id).where(pt.c.id == patient_id))).scalar()
    return await household_scopes(conn, hh) if hh else []


async def case_scopes(conn: Conn, case_id: Any) -> list[str]:
    c, tl, vo, o = T.cases, T.transport_legs, T.volunteer_offers, T.facility_offers
    case = (await conn.execute(select(c.c.village_id, c.c.household_id, c.c.current_facility_id)
                               .where(c.c.id == case_id))).first()
    if case is None:
        return []
    scopes: list[str] = []
    if case.village_id:
        scopes.append(f"village:{case.village_id}")
    if case.household_id:
        scopes.append(f"household:{case.household_id}")
    if case.current_facility_id:
        scopes.append(f"facility:{case.current_facility_id}")
    for (uid,) in (await conn.execute(select(tl.c.custodian_user_id).where(
            tl.c.case_id == case_id, tl.c.custodian_user_id.is_not(None)))).all():
        scopes.append(f"user:{uid}")
    for (uid,) in (await conn.execute(select(vo.c.volunteer_id).join(tl, tl.c.id == vo.c.leg_id).where(
            tl.c.case_id == case_id, vo.c.result == "pending"))).all():
        scopes.append(f"user:{uid}")
    for (fid,) in (await conn.execute(select(o.c.facility_id).where(o.c.case_id == case_id, o.c.result == "pending"))).all():
        scopes.append(f"facility:{fid}")
    return scopes


def new_id() -> uuid.UUID:
    from app.core.ids import uuid7

    return uuid7()
