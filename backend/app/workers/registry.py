"""Task registry shared by the API (publisher) and Celery workers (consumers).

Tasks are async functions registered with their queue (TRD §3.3). The API publishes them *after commit*
(database.md §0.3). With `AM_INLINE_TASKS=true` (tests, single-box demo) they run in-process instead; the
durable `case_timers` table + sweep (ADR-02) remains the guarantee in every mode.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import settings
from app.core.logging import log

TaskFn = Callable[..., Awaitable[Any]]
REGISTRY: dict[str, tuple[TaskFn, str]] = {}


def task(name: str, queue: str) -> Callable[[TaskFn], TaskFn]:
    assert queue in ("emergency", "default", "bulk")

    def deco(fn: TaskFn) -> TaskFn:
        REGISTRY[name] = (fn, queue)
        return fn

    return deco


async def enqueue(name: str, *, countdown: float | None = None, **kwargs: Any) -> None:
    """Publish a task. Message = IDs only (database.md §16.2). Never raises: timers repair lost publishes."""
    kwargs = {k: (str(v) if v is not None and not isinstance(v, (int, float, bool, str)) else v) for k, v in kwargs.items()}
    fn, queue = REGISTRY[name]
    if settings.inline_tasks:
        if countdown:
            return  # delayed work is driven by the case_timers sweep in inline mode
        try:
            await fn(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.error("inline_task_failed", task=name, error=repr(exc))
        return
    try:
        from app.workers.celery_app import celery_app

        celery_app.send_task(name, kwargs=kwargs, queue=queue, countdown=countdown)
    except Exception as exc:  # noqa: BLE001
        log.error("task_publish_failed", task=name, error=repr(exc))
