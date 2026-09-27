"""Integration test harness: real PostgreSQL/PostGIS + Redis (TRD §17.3), separate `aapatmitra_test` database.

Background tasks run in-process after commit (AM_INLINE_TASKS=true) so every flow is deterministic; delayed
work is driven by calling the timer sweep explicitly after moving `due_at` into the past (ADR-02).
"""

from __future__ import annotations

import os

os.environ["AM_ENV"] = "test"
os.environ["AM_DB_NAME"] = "aapatmitra_test"
os.environ["AM_INLINE_TASKS"] = "true"

import subprocess  # noqa: E402
import sys  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def reset_database() -> None:
    env = {**os.environ}
    subprocess.run([sys.executable, "scripts/reset_local_db.py"], check=True, cwd=ROOT, env=env,
                   stdout=subprocess.DEVNULL)


async def flush_redis() -> None:
    from app.core import redis as rds

    keys = [k async for k in rds.r().scan_iter(match=rds.k("*"))]
    if keys:
        await rds.r().delete(*keys)


@pytest.fixture(scope="session", autouse=True)
def _db() -> None:
    reset_database()


@pytest_asyncio.fixture(scope="session")
async def app() -> AsyncIterator[Any]:
    from app.core import db
    from app.main import app as fastapi_app

    await db.reflect()
    await flush_redis()
    yield fastapi_app
    await db.dispose_engines()


@pytest_asyncio.fixture(scope="session")
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def fresh(app: Any) -> AsyncIterator[None]:
    """Reset DB + Redis for tests that need the untouched demo seed."""
    from app.core import crypto, db

    await db.dispose_engines()
    reset_database()
    crypto._dek_cache.clear()
    await flush_redis()
    from tests import helpers

    helpers.TOKENS.clear()
    yield
