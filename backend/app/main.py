"""AapatMitra API — FastAPI modular monolith (TRD ADR-01, §3.2). All routes under /api/v1."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import DBAPIError

import app.workers.tasks  # noqa: F401  (registers the task catalog)
from app.core import db
from app.core import redis as rds
from app.core.config import settings
from app.core.errors import AppError, app_error_handler, db_error_handler, validation_handler
from app.core.logging import configure, log
from app.core.middleware import AppMiddleware
from app.modules.admin.router import router as admin_router
from app.modules.comms.router import dev as dev_router
from app.modules.comms.router import router as comms_router
from app.modules.comms.ws import hub
from app.modules.continuity.router import router as continuity_router
from app.modules.identity.router import jwks_route
from app.modules.identity.router import router as identity_router
from app.modules.onboarding.router import router as onboarding_router
from app.modules.platform.router import router as platform_router
from app.modules.reference.router import router as reference_router
from app.modules.referral.router import router as referral_router
from app.modules.routine_care.router import router as routine_router
from app.modules.transport.router import router as transport_router

API = "/api/v1"


async def _inline_sweeper() -> None:
    """Single-box mode without Celery Beat: run the ADR-02 timer sweep in-process every 10 s."""
    from app.workers.tasks import sweep_timers

    while True:
        try:
            await sweep_timers()
        except Exception as exc:  # noqa: BLE001
            log.error("inline_sweep_failed", error=repr(exc))
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure()
    await db.reflect()
    hub.start()
    sweeper = None
    if settings.inline_tasks and settings.env != "test":
        sweeper = asyncio.create_task(_inline_sweeper())
    try:
        from app.modules.referral.matching import warm_redis

        await warm_redis()
    except Exception as exc:  # noqa: BLE001
        log.warning("warm_redis_failed", error=repr(exc))
    yield
    if sweeper:
        sweeper.cancel()
    await hub.stop()
    await rds.close()
    await db.dispose_engines()


def create_app() -> FastAPI:
    app = FastAPI(
        title="AapatMitra API", version="1.0.0", lifespan=lifespan,
        docs_url=f"{API}/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url=f"{API}/openapi.json" if settings.docs_enabled else None,
    )
    app.add_middleware(AppMiddleware)
    app.add_middleware(  # SEC-API-09: only the console origin, never a wildcard with credentials
        CORSMiddleware, allow_origins=[settings.web_origin], allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-Match", "X-Request-Id", "X-CSRF-Token",
                       "X-Device-Id", "X-App-Version", "Accept-Language", "X-Break-Glass", "X-Purpose"],
        expose_headers=["X-Request-Id", "ETag", "Idempotent-Replayed", "Retry-After"])
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(DBAPIError, db_error_handler)  # type: ignore[arg-type]
    # referral before onboarding: GET /facilities/match must win over /facilities/{facility_id}
    for r in (identity_router, reference_router, referral_router, onboarding_router, routine_router, transport_router,
              continuity_router, comms_router, platform_router, admin_router):
        app.include_router(r, prefix=API)
    if settings.dev_routes_enabled:
        app.include_router(dev_router, prefix=API)
    app.add_api_route("/.well-known/jwks.json", jwks_route, methods=["GET"], include_in_schema=False)
    return app


app = create_app()
