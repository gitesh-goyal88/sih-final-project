"""Redis 7 access (database.md Part C). Every key is prefixed `am:{env}:` and is rebuildable or safe to lose."""

from __future__ import annotations

import asyncio
import contextvars
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.errors import AppError

_client: aioredis.Redis | None = None

_UNLOCK = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"


def r() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def k(key: str) -> str:
    return settings.redis_prefix + key


_held: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar("am_case_locks", default=frozenset())


@asynccontextmanager
async def case_lock(case_id: str, *, wait_s: float = 0.3) -> AsyncIterator[None]:
    """`lock:case:{id}` SET NX PX 15000; retry with 100 ms jitter, then 409 (database.md §15.4).

    API commands retry 3× (≈ 0.3 s, then 409 so the client re-renders); background tasks pass a longer
    `wait_s` (≤ the 15 s TTL). Re-entrant within one async context, so work that runs after commit inside
    a locked request (inline tasks) does not deadlock on its own lock.
    The Redis lock only avoids thundering workers; the `SELECT … FOR UPDATE` row lock is the real
    correctness guarantee (TRD §5.3). If Redis is unreachable we proceed on the row lock alone.
    """
    key, token = k(f"lock:case:{case_id}"), secrets.token_hex(8)
    if key in _held.get():
        yield
        return
    acquired = False
    try:
        deadline = asyncio.get_running_loop().time() + wait_s
        while True:
            if await r().set(key, token, nx=True, px=15000):
                acquired = True
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AppError("CASE_STATE_CONFLICT", "Case is being updated by someone else; retry")
            await asyncio.sleep(0.1 + secrets.randbelow(50) / 1000)
    except AppError:
        raise
    except Exception:  # noqa: BLE001 - Redis down: row lock still protects correctness
        acquired = False
    reset = _held.set(_held.get() | {key})
    try:
        yield
    finally:
        _held.reset(reset)
        if acquired:
            try:
                await r().eval(_UNLOCK, 1, key, token)
            except Exception:  # noqa: BLE001, S110
                pass


async def rate_limit(scope: str, ident: str, limit: int, window_s: int) -> bool:
    """Fixed-window counter `rl:{scope}:{id}:{window}`. True if allowed. Fails open (never blocks SOS)."""
    import time

    window = int(time.time() // window_s)
    key = k(f"rl:{scope}:{ident}:{window}")
    try:
        pipe = r().pipeline()
        pipe.incr(key)
        pipe.expire(key, window_s)
        count, _ = await pipe.execute()
        return int(count) <= limit
    except Exception:  # noqa: BLE001
        return True
