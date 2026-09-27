"""Sync, snapshot, consents, break-glass, privacy notice, health (API-Guide §6.12, §6.14, §8, §4.4)."""

from __future__ import annotations

import gzip
import json
import uuid
import zlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field
from sqlalchemy import insert, select, text

from app.core import redis as rds
from app.core.config import settings
from app.core.db import T, engine
from app.core.errors import AppError
from app.core.ids import uuid7
from app.core.logging import security_event
from app.core.rbac import Principal, can_access_patient, policy, public
from app.core.shapes import In
from app.core.uow import uow
from app.modules.platform import projections, sync
from app.modules.platform.consents import record_consent

router = APIRouter(tags=["platform"])
SYNC_ROLES = ("patient", "asha", "volunteer")
MAX_SYNC_BYTES = 2 * 1024 * 1024  # decompressed


@router.post("/sync")
async def sync_route(request: Request, p: Principal = Depends(policy(*SYNC_ROLES, purpose="continuity_of_care", allow_pending=True))) -> dict[str, Any]:
    raw = await request.body()
    if request.headers.get("content-encoding", "").lower() == "gzip":
        raw = gzip.decompress(raw)
    if len(raw) > MAX_SYNC_BYTES:
        raise AppError("PAYLOAD_TOO_LARGE")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise AppError("VALIDATION_FAILED", "Body is not JSON") from exc
    ops = body.get("ops") or []
    if len(ops) > 200:
        raise AppError("VALIDATION_FAILED", "At most 200 ops per call", fields=[{"path": "ops", "code": "too_many"}])
    if not await rds.rate_limit("sync", str(p.device_id or p.user_id), 30, 60):
        raise AppError("RATE_LIMITED", headers={"Retry-After": "10"})
    allowed = p.sync_scopes()
    requested = body.get("scopes") or sorted(allowed)
    scopes = [s for s in requested if s in allowed]
    denied = [s for s in requested if s not in allowed]
    for s in denied:
        security_event("authz.denied", reason="sync_scope", scope=s, user_id=str(p.user_id))
    pushed = await sync.push(p, ops) if ops else {"applied": [], "results": {}, "rejected": [], "conflicts": [],
                                                  "assigned": {"patientShortCodes": {}}}
    pulled = await sync.pull(p, body.get("cursor"), scopes)
    await sync.touch_device(p, pulled["cursor"])
    rejected = pushed["rejected"] + [{"opId": None, "code": "FORBIDDEN_SCOPE", "retry": False, "scope": s} for s in denied]
    return {**pushed, "rejected": rejected, **pulled, "wipe": await sync.wipe_instruction(p),
            "serverTime": datetime.now(UTC).isoformat()}


@router.get("/sync/snapshot")
async def snapshot(scopes: str = Query(..., max_length=4000), resumeAfter: str | None = None,  # noqa: N803
                   p: Principal = Depends(policy(*SYNC_ROLES, purpose="continuity_of_care", allow_pending=True))) -> StreamingResponse:
    """Bootstrap as gzipped NDJSON (API-Guide §8.4, database.md §9.3). REPEATABLE READ; watermark first."""
    allowed = p.sync_scopes()
    wanted = [s for s in scopes.split(",") if s in allowed]

    async def gen() -> AsyncIterator[bytes]:
        comp = zlib.compressobj(6, zlib.DEFLATED, 31)  # wbits=31 → gzip container
        rows = 0
        skipping = resumeAfter is not None
        async with engine().connect() as raw_conn:
            conn = await raw_conn.execution_options(isolation_level="REPEATABLE READ")
            watermark = (await conn.execute(text("SELECT pg_snapshot_xmin(pg_current_snapshot())::text"))).scalar()
            plan: list[tuple[str, list[str]]] = []
            for s in wanted:
                plan += await projections.snapshot_ids(conn, p, s)
            counts: dict[str, int] = {}
            for ent, ids in plan:
                counts[ent] = counts.get(ent, 0) + (len(ids) if ids != ["*"] else 0)
            meta = {"type": "meta", "cursor": sync.encode_cursor(watermark), "serverTime": datetime.now(UTC).isoformat(),
                    "counts": counts}
            yield comp.compress((json.dumps(meta) + "\n").encode())
            for ent, ids in plan:
                for i in range(0, len(ids), 200):
                    chunk = ids[i:i + 200]
                    loaded = await projections.load(conn, p, ent, chunk)
                    lines = []
                    for eid, data in loaded.items():
                        if skipping:
                            if f"{ent}:{eid}" == resumeAfter:
                                skipping = False
                            continue
                        lines.append(json.dumps({"type": "row", "entity": ent, "id": eid, "version": data.get("version"),
                                                 "data": data}, default=str))
                        rows += 1
                    if lines:
                        yield comp.compress(("\n".join(lines) + "\n").encode())
        async with uow(p) as tx:
            await tx.audit("sync.snapshot", "device", p.device_id, diff={"scopes": wanted, "rows": rows})
        yield comp.compress((json.dumps({"type": "end", "rows": rows}) + "\n").encode())
        yield comp.flush()

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Content-Encoding": "gzip", "Cache-Control": "no-store"})


# ---------------- consent & privacy ----------------

@router.post("/consents", status_code=201)
async def create_consent(body: dict[str, Any], p: Principal = Depends(policy("asha", "patient", purpose="continuity_of_care"))) -> dict[str, Any]:
    async with uow(p) as tx:
        cid = await record_consent(tx, p, body)
    return {"id": cid}


@router.get("/patients/{patient_id}/consents")
async def patient_consents(patient_id: uuid.UUID, p: Principal = Depends(policy("asha", "patient", purpose="continuity_of_care"))) -> dict[str, Any]:
    c = T.consents
    async with uow(p) as tx:
        if not await can_access_patient(tx.conn, p, patient_id):
            raise AppError("NOT_FOUND")
        rows = (await tx.conn.execute(select(c).where(c.c.patient_id == patient_id).order_by(c.c.effective_at.desc()))).all()
    return {"data": [{"id": r.id, "purpose": r.purpose, "status": r.status, "method": r.method, "language": r.language,
                      "noticeVersion": r.notice_version, "effectiveAt": r.effective_at, "expiresAt": r.expires_at,
                      "supersedesId": r.supersedes_id} for r in rows]}


NOTICES = {
    ("continuity_of_care", "en"): "Your ASHA will keep your family's health record on AapatMitra so that nurses and doctors "
                                  "can see your history and remind you of visits. Only your ASHA, and doctors or hospitals "
                                  "treating you, can see it. You can withdraw this at any time by telling your ASHA.",
    ("continuity_of_care", "hi"): "आपकी ASHA आपके परिवार का स्वास्थ्य रिकॉर्ड आपातमित्र पर रखेंगी ताकि नर्स और डॉक्टर आपका इतिहास "
                                  "देख सकें और जाँच की याद दिला सकें। इसे केवल आपकी ASHA और इलाज करने वाले डॉक्टर या अस्पताल "
                                  "देख सकते हैं। आप कभी भी ASHA को बताकर यह सहमति वापस ले सकते हैं।",
    ("emergency_care", "en"): "In an emergency AapatMitra shares your location and the type of emergency with a hospital "
                              "and a driver so that help reaches you.",
    ("emergency_care", "hi"): "आपातकाल में आपातमित्र आपकी जगह और आपात स्थिति का प्रकार अस्पताल और ड्राइवर को भेजता है "
                              "ताकि मदद आप तक पहुँचे।",
}


@router.get("/privacy/notice", dependencies=[Depends(public())])
async def privacy_notice(purpose: str, lang: str = "hi") -> dict[str, Any]:
    """Notice text for the consent screen (DPDP). Audio files are packaged in the language pack (Part E tiles bucket)."""
    textv = NOTICES.get((purpose, lang)) or NOTICES.get((purpose, "en"))
    if textv is None:
        raise AppError("NOT_FOUND")
    return {"purpose": purpose, "language": lang, "noticeVersion": f"{purpose[:2]}-2026-09", "text": textv,
            "audioUrl": None, "reviewNote": "Legal/native-speaker review pending (UI-UX §4.4, SECURITY SQ1-SQ2)"}


# ---------------- break-glass ----------------

class BreakGlassIn(In):
    patient_id: uuid.UUID
    reason: str = Field(min_length=10, max_length=500)


@router.post("/break-glass", status_code=201)
async def break_glass(body: BreakGlassIn, p: Principal = Depends(policy("facility_staff", "district_admin", purpose="security"))) -> dict[str, Any]:
    """Time-limited (1 h), patient-specific emergency access; DPO alerted (FR-SEC05, SEC-AZ-06)."""
    gid = uuid7()
    now = datetime.now(UTC)
    async with uow(p) as tx:
        if (await tx.conn.execute(select(T.patients.c.id).where(T.patients.c.id == body.patient_id))).first() is None:
            raise AppError("NOT_FOUND")
        await tx.conn.execute(insert(T.break_glass_grants).values(
            id=gid, user_id=p.user_id, patient_id=body.patient_id, reason=body.reason, granted_at=now,
            expires_at=now + timedelta(hours=1), dpo_notified_at=now))
        await tx.audit("break_glass.grant", "patient", body.patient_id, patient_id=body.patient_id,
                       diff={"grantId": str(gid)}, purpose="security")
    security_event("authz.break_glass", user_id=str(p.user_id), grant_id=str(gid))
    return {"grantId": gid, "expiresAt": (now + timedelta(hours=1)).isoformat()}


# ---------------- health ----------------

@router.get("/health", dependencies=[Depends(public())])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", dependencies=[Depends(public())])
async def ready() -> dict[str, Any]:
    checks: dict[str, bool] = {}
    try:
        async with engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:  # noqa: BLE001
        checks["postgres"] = False
    try:
        checks["redis"] = bool(await rds.r().ping())
    except Exception:  # noqa: BLE001
        checks["redis"] = False
    if not all(checks.values()):
        raise AppError("DEPENDENCY_UNAVAILABLE", json.dumps(checks))
    return {"status": "ready", **checks}


@router.get("/version")
async def version(p: Principal = Depends(policy("patient", "asha", "volunteer", "doctor", "facility_staff", "district_admin",
                                                purpose="administration", allow_pending=True))) -> dict[str, Any]:
    async with uow(p) as tx:
        from app.modules.reference import serial

        cat = await serial.catalog(tx.conn, p.district_code)
    return {"api": "1.0.0", "build": settings.env, "minAndroidVersion": settings.min_android_version,
            "catalogVersion": cat["catalogVersion"]}
