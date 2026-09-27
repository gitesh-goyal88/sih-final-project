"""HTTP middleware: request id, size limits, security headers, idempotency (API-Guide §2.1–2.5, SEC-API-07).

Idempotency (API-Guide §2.5):
  new (actorId, key)             → process; store (actor, key, endpoint, sha256(body), status, minimal body)
  same actor+key, same body      → replay stored status/body, header `Idempotent-Replayed: true`
  same actor+key, different body → 422 IDEMPOTENCY_MISMATCH
  same key, different actor      → treated as a new key (never replays another user's response)
  same key still in flight       → 409 IDEMPOTENCY_IN_PROGRESS, Retry-After: 1
Stored replay bodies contain ids and status only: PII-bearing keys are stripped, and the stored copy is
encrypted at rest (`idempotency_keys.response_enc`, database.md §5.12).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core import redis as rds
from app.core.config import settings
from app.core.crypto import system_decrypt, system_encrypt
from app.core.db import T, engine
from app.core.errors import problem
from app.core.security import TokenError, decode_access_token

API = "/api/v1"
MAX_BODY = 256 * 1024
SOS_MAX_BODY = 1024
IDEMPOTENCY_EXEMPT = (f"{API}/auth/", f"{API}/webhooks/", f"{API}/dev/", f"{API}/ws/ticket", f"{API}/sync")
PII_KEYS = {"name", "firstName", "phone", "notes", "summary", "referralReason", "ashaNotes", "note",
            "registeredPhone", "custodianPhone", "driverName", "driverPhone", "text", "declineNote",
            "patientName", "authorName", "ashaName", "villageName", "landmark", "phoneMasked",
            "registeredPhoneMasked", "contacts", "patient", "fromLabel", "toLabel"}


def _minimal(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _minimal(v) for k, v in obj.items() if k not in PII_KEYS}
    if isinstance(obj, list):
        return [_minimal(v) for v in obj]
    return obj


def _canonical_hash(body: bytes) -> bytes:
    try:
        data = json.loads(body or b"null")
        canon = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    except ValueError:
        canon = body
    return hashlib.sha256(canon).digest()


def _actor(request: Request) -> uuid.UUID | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    try:
        return uuid.UUID(decode_access_token(auth[7:].strip())["sub"])
    except (TokenError, ValueError, KeyError):
        return None


def _parse_version(v: str) -> int:
    try:
        return int(v.split("+", 1)[1])
    except (IndexError, ValueError):
        return 0


class AppMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        try:
            rid = str(uuid.UUID(rid))
        except ValueError:
            rid = str(uuid.uuid4())
        request.state.request_id = rid
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)

        path = request.url.path
        resp = await self._handle(request, call_next, path)
        resp.headers["X-Request-Id"] = rid
        if path.startswith(API):
            resp.headers.setdefault("Cache-Control", "no-store")  # SEC-WEB-06
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["Referrer-Policy"] = "no-referrer"
            resp.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        return resp

    async def _handle(self, request: Request, call_next: RequestResponseEndpoint, path: str) -> Response:
        method = request.method
        if method in ("POST", "PATCH", "PUT"):
            limit = SOS_MAX_BODY if path == f"{API}/sos" else MAX_BODY
            if path.startswith(f"{API}/admin/users/import"):
                limit = 1024 * 1024
            clen = request.headers.get("content-length")
            if clen and clen.isdigit() and int(clen) > limit:
                return problem(request, "PAYLOAD_TOO_LARGE", f"Body over {limit} bytes", status=413)
            body = await request.body()
            if len(body) > limit:
                return problem(request, "PAYLOAD_TOO_LARGE", f"Body over {limit} bytes", status=413)

        # 426 for unsupported Android versions — never on /sos (TRD §13.1)
        app_version = request.headers.get("x-app-version")
        if app_version and path != f"{API}/sos" and _parse_version(app_version) < _parse_version(settings_min_android()):
            return problem(request, "UPGRADE_REQUIRED", "Please update the app")

        if method not in ("POST", "PATCH") or not path.startswith(API) or path.startswith(IDEMPOTENCY_EXEMPT):
            return await call_next(request)
        return await self._idempotent(request, call_next, path)

    async def _idempotent(self, request: Request, call_next: RequestResponseEndpoint, path: str) -> Response:
        raw_key = request.headers.get("idempotency-key")
        body = await request.body()
        if not raw_key and path == f"{API}/sos":
            # /sos is never rejected: fall back to the body's idempotencyKey
            try:
                raw_key = json.loads(body).get("idempotencyKey")
            except (ValueError, AttributeError):
                raw_key = None
        if not raw_key:
            return problem(request, "VALIDATION_FAILED", "Idempotency-Key header is required",
                           fields=[{"path": "Idempotency-Key", "code": "missing"}])
        try:
            key = uuid.UUID(raw_key)
        except ValueError:
            return problem(request, "VALIDATION_FAILED", "Idempotency-Key must be a UUID",
                           fields=[{"path": "Idempotency-Key", "code": "invalid"}])
        actor = _actor(request)
        endpoint = f"{request.method} {path}"
        req_hash = _canonical_hash(body)
        ik = T.idempotency_keys
        async with engine().connect() as conn:
            row = (await conn.execute(select(ik).where(and_(
                ik.c.key == key,
                ik.c.actor_id.is_(None) if actor is None else ik.c.actor_id == actor,
                ik.c.expires_at > datetime.now(UTC))))).first()
        if row is not None:
            if bytes(row.request_hash) != req_hash or row.endpoint != endpoint:
                return problem(request, "IDEMPOTENCY_MISMATCH", "Idempotency-Key reused with a different request")
            stored = system_decrypt("idempotency", row.response_enc, f"{actor}:{key}")
            return JSONResponse(json.loads(stored), status_code=row.status_code,
                                headers={"Idempotent-Replayed": "true"})

        guard = rds.k(f"idem:{actor}:{key}")
        try:
            if not await rds.r().set(guard, "processing", nx=True, ex=60):
                return problem(request, "IDEMPOTENCY_IN_PROGRESS", "Same request still processing",
                               headers={"Retry-After": "1"})
        except Exception:  # noqa: BLE001, S110 - Redis down: DB row still dedupes afterwards
            guard = ""
        try:
            response = await call_next(request)
            chunks = [c async for c in response.body_iterator]  # type: ignore[attr-defined]
            content = b"".join(chunks)
            status = response.status_code
            if status < 500 and status not in (401, 403, 429) and response.headers.get(
                    "content-type", "").startswith(("application/json", "application/problem+json")):
                try:
                    minimal = json.dumps(_minimal(json.loads(content or b"null"))).encode()
                    async with engine().begin() as conn:
                        await conn.execute(pg_insert(ik).values(
                            key=key, actor_id=actor, endpoint=endpoint, request_hash=req_hash, status_code=status,
                            response_enc=system_encrypt("idempotency", minimal, f"{actor}:{key}"),
                            expires_at=datetime.now(UTC) + timedelta(days=7),
                        ).on_conflict_do_nothing())
                except Exception as exc:  # noqa: BLE001
                    structlog.get_logger().warning("idempotency_store_failed", error=repr(exc))
            headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
            return Response(content=content, status_code=status, headers=headers, media_type=response.media_type)
        finally:
            if guard:
                try:
                    await rds.r().delete(guard)
                except Exception:  # noqa: BLE001, S110
                    pass


def settings_min_android() -> str:
    return settings.min_android_version
