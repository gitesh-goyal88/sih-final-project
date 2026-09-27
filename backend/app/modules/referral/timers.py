"""Durable timers (TRD ADR-02, database.md §5.8 `case_timers`, §8.3 sweep).

Celery `countdown` is the fast path; the 10-second `sweep_timers` Beat job is the guarantee — a worker
restart can never leave a case stuck (NFR-A04). Handlers are idempotent: they re-check state before acting.

`stage_sla` timers are told apart by `ref_id`:
  NULL                          → created → matched (30 s, TRD §5.2)
  uuid5('acceptance:<case>')    → matched → accepted total (10 min)
  <leg id>                      → in-transit leg SLA (OSRM ETA × 2 + 15 min)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import T
from app.core.ids import uuid5, uuid7
from app.core.uow import UoW


FAST_PATH_MAX_S = 300  # Celery countdown only for short emergency timers; the sweep covers everything


def acceptance_ref(case_id: Any) -> uuid.UUID:
    return uuid5(f"acceptance:{case_id}")


async def schedule(tx: UoW, case_id: Any, kind: str, due_in_s: float, ref_id: Any = None) -> None:
    """Insert a live timer (at most one per case/kind/ref — `case_timers_live_uq`) and publish the countdown."""
    tid = uuid7()
    due_at = datetime.now(UTC) + timedelta(seconds=due_in_s)
    res = await tx.conn.execute(pg_insert(T.case_timers).values(
        id=tid, case_id=case_id, kind=kind, ref_id=ref_id, due_at=due_at).on_conflict_do_nothing())
    if not res.rowcount or due_in_s > FAST_PATH_MAX_S:
        return  # long timers (closure reminders, 72 h escalation) are driven by the 10 s sweep alone

    async def _publish() -> None:
        from app.workers.registry import enqueue

        await enqueue("fire_timer", countdown=due_in_s, timer_id=tid)

    tx.after_commit(_publish)


async def cancel(tx: UoW, case_id: Any, kinds: tuple[str, ...] | None = None, ref_id: Any = None) -> None:
    ct = T.case_timers
    cond = and_(ct.c.case_id == case_id, ct.c.fired_at.is_(None), ct.c.cancelled_at.is_(None))
    if kinds:
        cond = and_(cond, ct.c.kind.in_(kinds))
    if ref_id is not None:
        cond = and_(cond, ct.c.ref_id == ref_id)
    await tx.conn.execute(update(ct).where(cond).values(cancelled_at=datetime.now(UTC)))


async def claim_due(tx: UoW, limit: int = 100) -> list[Any]:
    """database.md §8.3 — safe with several workers (FOR UPDATE SKIP LOCKED)."""
    rows = (await tx.conn.execute(text("""
        WITH due AS (
          SELECT id FROM case_timers
          WHERE fired_at IS NULL AND cancelled_at IS NULL AND due_at <= now()
          ORDER BY due_at
          LIMIT :limit
          FOR UPDATE SKIP LOCKED
        )
        UPDATE case_timers t SET fired_at = now(), fire_attempts = fire_attempts + 1
        FROM due WHERE t.id = due.id
        RETURNING t.id, t.case_id, t.kind, t.ref_id
    """), {"limit": limit})).all()
    return list(rows)


async def claim_one(tx: UoW, timer_id: Any) -> Any:
    """Fast path (Celery countdown): claim a single timer if it is still live and due."""
    ct = T.case_timers
    row = (await tx.conn.execute(select(ct).where(and_(
        ct.c.id == timer_id, ct.c.fired_at.is_(None), ct.c.cancelled_at.is_(None),
        ct.c.due_at <= datetime.now(UTC) + timedelta(seconds=1))).with_for_update(skip_locked=True))).first()
    if row is None:
        return None
    await tx.conn.execute(update(ct).where(ct.c.id == timer_id).values(
        fired_at=datetime.now(UTC), fire_attempts=ct.c.fire_attempts + 1))
    return row
