"""Celery 5 on RabbitMQ (TRD §3.3, database.md Part D, SECURITY Appendix D).

* Queues `emergency` (dedicated pool, prefetch 1), `default`, `bulk` — quorum queues with dead-lettering.
* JSON serializer only (SF-21), no result backend (fire-and-forget + DB state), acks_late,
  reject_on_worker_lost.
* Beat: `sweep_timers` every 10 s is the backstop that guarantees no case is stuck (ADR-02).
"""

from __future__ import annotations

import asyncio
from typing import Any

from celery import Celery
from celery.signals import worker_init, worker_process_init
from kombu import Exchange, Queue

from app.core.config import settings

celery_app = Celery("aapatmitra", broker=settings.amqp_url)

_dlx = Exchange("am.dlx", type="fanout", durable=True)
_tasks = Exchange("am.tasks", type="topic", durable=True)  # topic: required by native delayed delivery


def _q(name: str, delivery_limit: int, extra: dict[str, Any] | None = None) -> Queue:
    args = {"x-queue-type": "quorum", "x-delivery-limit": delivery_limit, "x-dead-letter-exchange": "am.dlx"}
    args.update(extra or {})
    return Queue(name, _tasks, routing_key=name, queue_arguments=args)


celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_backend=None,
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    task_default_exchange="am.tasks",
    task_queues=[
        _q("emergency", 5),
        _q("default", 10),
        _q("bulk", 3, {"x-max-length": 100000}),
    ],
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "sweep-timers": {"task": "sweep_timers", "schedule": 10.0, "options": {"queue": "emergency"}},
        "warm-redis": {"task": "warm_redis", "schedule": 600.0, "options": {"queue": "default"}},
        "recompute-leaderboards": {"task": "recompute_leaderboards", "schedule": 3600.0, "options": {"queue": "bulk"}},
        "refresh-mvs": {"task": "refresh_materialized_views", "schedule": 300.0, "options": {"queue": "bulk"}},
        "ensure-partitions": {"task": "ensure_partitions", "schedule": 86400.0, "options": {"queue": "bulk"}},
        "purge-expired": {"task": "purge_expired", "schedule": 3600.0, "options": {"queue": "bulk"}},
        "mark-missed-tasks": {"task": "mark_missed_tasks", "schedule": 3600.0, "options": {"queue": "bulk"}},
    },
)

_loop: asyncio.AbstractEventLoop | None = None


def _run(coro: Any) -> Any:
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop.run_until_complete(coro)


@worker_init.connect
def _declare_dead_letters(**_: Any) -> None:
    """am.dlx + dlq.all (database.md §16.1) are declared up front — not as a task queue, so Celery's native
    delayed-delivery setup for quorum queues never tries to bind them before the exchange exists."""
    with celery_app.connection_for_write() as conn:
        dlq = Queue("dlq.all", _dlx, queue_arguments={"x-queue-type": "quorum", "x-message-ttl": 14 * 24 * 3600 * 1000})
        dlq.bind(conn.default_channel).declare()


@worker_process_init.connect
def _init_worker(**_: Any) -> None:
    from app.core import db
    from app.core.logging import configure

    configure()
    db.DEFAULT_ROLE = "worker"
    _run(db.reflect("worker"))


def _register() -> None:
    import app.workers.tasks  # noqa: F401  (populates REGISTRY)
    from app.workers.registry import REGISTRY

    for name, (fn, queue) in REGISTRY.items():
        def make(f: Any, n: str) -> Any:
            def runner(**kwargs: Any) -> None:
                _run(f(**kwargs))
            runner.__name__ = n
            return runner

        celery_app.task(name=name, queue=queue, time_limit=300, soft_time_limit=240)(make(fn, name))


_register()
