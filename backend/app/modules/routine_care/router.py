"""Longitudinal record, screenings, teleconsultation, uploads (API-Guide §6.3, §6.10, §12)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header
from pydantic import Field
from sqlalchemy import and_, insert, or_, select, update

from app.core import paging
from app.core.config import settings
from app.core.crypto import decrypt
from app.core.db import T
from app.core.errors import AppError
from app.core.rbac import Principal, can_access_patient, policy, require_consent
from app.core.shapes import HealthRecordEntryOut, In, Out, PatientOut, SymptomAnswer, Vitals
from app.core.uow import CommitThenRaise, uow
from app.integrations import objectstore
from app.modules.onboarding.service import patient_rows, patients_out
from app.modules.routine_care import service as svc

router = APIRouter(tags=["routine_care"])
CC = "continuity_of_care"


async def _patient_access(tx: Any, p: Principal, patient_id: Any, grant: str | None = None) -> None:
    if not await can_access_patient(tx.conn, p, patient_id, grant_id=grant):
        await tx.audit("patient.read", "patient", patient_id, patient_id=patient_id, outcome="denied")
        raise CommitThenRaise(AppError("NOT_FOUND"))  # the denial is audited even though the request fails


@router.get("/patients/{patient_id}/record")
async def record(patient_id: uuid.UUID, kinds: str | None = None, limit: int = 50, cursor: str | None = None,
                 x_break_glass: str | None = Header(default=None),
                 p: Principal = Depends(policy("asha", "doctor", "facility_staff", "patient", "district_admin", purpose=CC))) -> dict[str, Any]:
    e = T.health_record_entries
    n = paging.limit(limit)
    async with uow(p) as tx:
        await _patient_access(tx, p, patient_id, x_break_glass)
        if p.role in ("asha", "doctor") and not p.break_glass_grant_id:
            await require_consent(tx.conn, p, patient_id, CC)
        elif p.role == "facility_staff":
            p.purpose = "emergency_care"  # facility access flows from an offered/accepted case (API-Guide §4.2)
        q = select(e).where(e.c.patient_id == patient_id)
        if kinds:
            q = q.where(e.c.kind.in_(kinds.split(",")))
        cur = paging.decode(cursor)
        if cur:
            q = q.where(e.c.recorded_at < cur["t"])
        rows = (await tx.conn.execute(q.order_by(e.c.recorded_at.desc()).limit(n + 1))).all()
        entries = await svc.entries_out(tx.conn, rows[:n], include_notes=p.role != "patient")
        prow = await patient_rows(tx.conn, [patient_id])
        patient = (await patients_out(tx.conn, prow, "patient" if p.role == "patient" else "asha"))[0]
        summary = await _summary(tx, patient_id, entries)
        if p.role == "patient":
            summary.update({"riskReasons": [], "conditions": [], "latestVitals": None})
            entries = [x for x in entries if x["kind"] in ("screening", "anc_visit", "pnc_visit", "immunisation")]
        await tx.audit("patient.read", "patient", patient_id, patient_id=patient_id)
    nxt = paging.encode({"t": rows[n - 1].recorded_at.isoformat()}) if len(rows) > n else None
    return {"patient": PatientOut.model_validate(patient).model_dump(by_alias=True), "summary": summary,
            "entries": [HealthRecordEntryOut.model_validate(x).model_dump(by_alias=True) for x in entries],
            "nextCursor": nxt}


async def _summary(tx: Any, patient_id: Any, entries: list[dict[str, Any]]) -> dict[str, Any]:
    pc, pm, c, cp = T.patient_conditions, T.patient_medications, T.cases, T.care_plans
    live = [x for x in entries if not x["superseded"] and not x["enteredInError"]]
    screens = [x for x in live if x["kind"] == "screening"]
    with_vitals = next((x for x in live if x["vitals"]), None)
    latest = screens[0] if screens else None
    open_cases = (await tx.conn.execute(select(c.c.id, c.c.short_code, c.c.status).where(and_(
        c.c.patient_id == patient_id, c.c.status.notin_(("closed", "follow_up", "cancelled")))))).all()
    plan = (await tx.conn.execute(select(cp.c.id, cp.c.next_visit_on).where(and_(
        cp.c.patient_id == patient_id, cp.c.status == "active")).order_by(cp.c.recorded_at.desc()).limit(1))).first()
    return {
        "highRisk": bool(latest and latest["highRisk"]),
        "riskReasons": sorted({f["ruleCode"] for f in (latest or {}).get("riskFlags", []) if f["evaluatedBy"] == "server"}),
        "lastScreeningAt": latest["recordedAt"] if latest else None,
        "latestVitals": {k: v for k, v in (with_vitals["vitals"] or {}).items() if v is not None} if with_vitals else None,
        "conditions": [{"code": r.condition_code, "status": r.status} for r in (await tx.conn.execute(
            select(pc.c.condition_code, pc.c.status).where(pc.c.patient_id == patient_id, pc.c.status == "active"))).all()],
        "medications": [{"medicineName": r.medicine_name, "frequencyText": r.frequency_text} for r in (await tx.conn.execute(
            select(pm.c.medicine_name, pm.c.frequency_text).where(pm.c.patient_id == patient_id, pm.c.stopped_on.is_(None)))).all()],
        "openCases": [{"id": r.id, "shortCode": r.short_code, "status": r.status} for r in open_cases],
        "activeCarePlan": {"id": plan.id, "nextVisitOn": plan.next_visit_on} if plan else None,
    }


class ScreeningIn(In):
    id: uuid.UUID
    kind: Literal["screening"] = "screening"
    vitals: Vitals | None = None
    symptoms: list[SymptomAnswer] = Field(default_factory=list, max_length=40)
    notes: str | None = Field(default=None, max_length=2000)
    device_risk_flags: list[str] = Field(default_factory=list, max_length=20)
    risk_rule_set_version: int | None = None
    location: dict[str, float] | None = None
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    recorded_at: datetime | None = None


@router.post("/patients/{patient_id}/screenings", status_code=201)
async def screening(patient_id: uuid.UUID, body: ScreeningIn, p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    data = body.model_dump(by_alias=True, mode="json", exclude_none=True)
    if body.vitals:
        data["vitals"] = body.vitals.model_dump(by_alias=True, exclude_none=True)
    async with uow(p) as tx:
        await _patient_access(tx, p, patient_id)
        await require_consent(tx.conn, p, patient_id, CC)
        res = await svc.insert_entry(tx, p, patient_id, data)
        rows = (await tx.conn.execute(select(T.health_record_entries).where(T.health_record_entries.c.id == res["entryId"]))).all()
        entry = (await svc.entries_out(tx.conn, rows))[0]
    high = entry["highRisk"]
    return {"entry": HealthRecordEntryOut.model_validate(entry).model_dump(by_alias=True), "highRisk": high,
            "riskFlags": [f for f in entry["riskFlags"] if f["evaluatedBy"] == "server"],
            "tasksCreated": res.get("tasksCreated", []), "suggestedActions": ["teleconsult", "referral"] if high else []}


@router.post("/patients/{patient_id}/entries", status_code=201, response_model=HealthRecordEntryOut)
async def add_entry(patient_id: uuid.UUID, body: dict[str, Any],
                    p: Principal = Depends(policy("asha", "doctor", "facility_staff", purpose=CC))) -> dict[str, Any]:
    async with uow(p) as tx:
        await _patient_access(tx, p, patient_id)
        if p.role in ("asha", "doctor"):
            await require_consent(tx.conn, p, patient_id, CC)
        facility = p.facility_ids[0] if p.role in ("doctor", "facility_staff") and p.facility_ids else None
        res = await svc.insert_entry(tx, p, patient_id, body, facility_id=facility)
        if body.get("kind") == "teleconsult" and body.get("teleconsultSessionId"):
            tc = T.teleconsult_sessions
            await tx.conn.execute(update(tc).where(and_(tc.c.id == body["teleconsultSessionId"],
                                                        tc.c.outcome_entry_id.is_(None))).values(outcome_entry_id=res["entryId"]))
        rows = (await tx.conn.execute(select(T.health_record_entries).where(T.health_record_entries.c.id == res["entryId"]))).all()
        return (await svc.entries_out(tx.conn, rows))[0]


# ---------------- teleconsultation ----------------

def turn_credentials(user_id: Any) -> list[dict[str, Any]]:
    """TURN REST shared-secret scheme, 1 h TTL (TRD §12, SECURITY Appendix C)."""
    username = f"{int(time.time()) + 3600}:{user_id}"
    cred = base64.b64encode(hmac.new(settings.turn_secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
    return [{"urls": settings.turn_urls, "username": username, "credential": cred}]


MEDIA_PROFILE = {"audio": {"codec": "opus", "bitrateKbps": 16, "dtx": True, "fec": True},
                 "video": {"codec": "vp8", "maxWidth": 160, "maxHeight": 120, "maxFps": 10, "maxKbps": 150,
                           "disableBelowKbps": 120}}


class TeleconsultIn(In):
    id: uuid.UUID
    patient_id: uuid.UUID
    reason_code: Literal["high_risk_flag", "asha_concern", "follow_up_review", "referral_advice", "other"]
    trigger_entry_id: uuid.UUID | None = None
    preferred_mode: Literal["audio", "video", "async"] = "audio"
    requested_at: datetime | None = None


async def _hub_facility(tx: Any, district: str | None) -> Any:
    f, fm = T.facilities, T.facility_memberships
    q = (select(f.c.id).join(fm, fm.c.facility_id == f.c.id).where(fm.c.user_role == "doctor", fm.c.active_to.is_(None)))
    if district:
        q = q.where(f.c.district_code == district)
    return (await tx.conn.execute(q.limit(1))).scalar()


def _session_out(r: Any) -> dict[str, Any]:
    return {"sessionId": r.id, "id": r.id, "patientId": r.patient_id, "ashaId": r.asha_id, "doctorId": r.doctor_id,
            "facilityId": r.facility_id, "reasonCode": r.reason_code, "triggerEntryId": r.trigger_entry_id,
            "status": r.status, "mode": r.mode, "requestedAt": r.requested_at, "acceptedAt": r.accepted_at,
            "startedAt": r.started_at, "endedAt": r.ended_at, "endReason": r.end_reason,
            "outcomeEntryId": r.outcome_entry_id, "referralCaseId": r.referral_case_id, "version": r.version,
            "wsChannel": f"teleconsult:{r.id}"}


@router.post("/teleconsults", status_code=201)
async def request_teleconsult(body: TeleconsultIn, p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    tc = T.teleconsult_sessions
    async with uow(p) as tx:
        await _patient_access(tx, p, body.patient_id)
        await require_consent(tx.conn, p, body.patient_id, CC)
        if (await tx.conn.execute(select(tc.c.id).where(tc.c.id == body.id))).first() is None:
            await tx.conn.execute(insert(tc).values(
                id=body.id, patient_id=body.patient_id, asha_id=p.user_id, facility_id=await _hub_facility(tx, p.district_code),
                reason_code=body.reason_code, trigger_entry_id=body.trigger_entry_id,
                mode="async" if body.preferred_mode == "async" else body.preferred_mode,
                requested_at=body.requested_at or datetime.now(UTC)))
            from app.core.uow import patient_scopes

            await tx.journal("teleconsult_session", body.id, await patient_scopes(tx.conn, body.patient_id))
            await tx.audit("teleconsult.request", "teleconsult_session", body.id, patient_id=body.patient_id)
        row = (await tx.conn.execute(select(tc).where(tc.c.id == body.id))).first()

        async def _nudge() -> None:
            from app.modules.comms.notify import ws_publish

            if row.facility_id:
                await ws_publish(f"facility:{row.facility_id}", {"type": "teleconsult.requested", "sessionId": str(row.id)})
        tx.after_commit(_nudge)
    return {**_session_out(row), "iceServers": turn_credentials(p.user_id), "mediaProfile": MEDIA_PROFILE,
            "esanjeevaniDeepLink": None}


@router.get("/teleconsults")
async def teleconsult_queue(status: str = "requested", facilityId: uuid.UUID | None = None,  # noqa: N803
                            p: Principal = Depends(policy("doctor", "asha", purpose=CC))) -> dict[str, Any]:
    tc, pt, u = T.teleconsult_sessions, T.patients, T.users
    q = select(tc).where(tc.c.status.in_(status.split(",")))
    if p.role == "doctor":
        fids = [str(facilityId)] if facilityId and str(facilityId) in p.facility_ids else p.facility_ids
        q = q.where(or_(tc.c.facility_id.in_(fids or ["00000000-0000-0000-0000-000000000000"]), tc.c.doctor_id == p.user_id))
    else:
        q = q.where(tc.c.asha_id == p.user_id)
    out = []
    async with uow(p) as tx:
        for r in (await tx.conn.execute(q.order_by(tc.c.requested_at).limit(100))).all():
            prow = (await tx.conn.execute(select(pt).where(pt.c.id == r.patient_id))).first()
            from app.modules.onboarding.service import age_years

            pname = await decrypt(tx.conn, "patient", prow.id, "patients", "name_enc", prow.id, prow.name_enc)
            arow = (await tx.conn.execute(select(u.c.id, u.c.name_enc).where(u.c.id == r.asha_id))).first()
            out.append({**_session_out(r), "patient": {"firstName": (pname or "").split(" ")[0], "ageYears": age_years(prow.date_of_birth),
                                                       "sex": prow.sex, "shortCode": prow.short_code},
                        "ashaName": await decrypt(tx.conn, "user", arow.id, "users", "name_enc", arow.id, arow.name_enc),
                        "waitingS": int((datetime.now(UTC) - r.requested_at).total_seconds())})
    return {"data": out}


async def _session_for(tx: Any, p: Principal, session_id: Any) -> Any:
    tc = T.teleconsult_sessions
    row = (await tx.conn.execute(select(tc).where(tc.c.id == session_id).with_for_update())).first()
    if row is None:
        raise AppError("NOT_FOUND")
    participant = row.asha_id == p.user_id or row.doctor_id == p.user_id or (
        p.role == "doctor" and row.status == "requested" and row.facility_id and str(row.facility_id) in p.facility_ids)
    if not participant:
        raise AppError("NOT_FOUND")
    return row


@router.get("/teleconsults/{session_id}")
async def get_teleconsult(session_id: uuid.UUID, p: Principal = Depends(policy("asha", "doctor", purpose=CC))) -> dict[str, Any]:
    async with uow(p) as tx:
        row = await _session_for(tx, p, session_id)
        out = _session_out(row)
        if p.role == "doctor":
            e = T.v_patient_timeline
            rows = (await tx.conn.execute(select(e).where(e.c.patient_id == row.patient_id).order_by(e.c.recorded_at.desc()).limit(20))).all()
            entries = await svc.entries_out(tx.conn, rows)
            prow = await patient_rows(tx.conn, [row.patient_id])
            out["patient"] = (await patients_out(tx.conn, prow))[0]
            out["summary"] = await _summary(tx, row.patient_id, entries)
            out["entries"] = entries
            await tx.audit("patient.read", "patient", row.patient_id, patient_id=row.patient_id, diff={"view": "teleconsult"})
    return out


class TeleCommandIn(In):
    command: Literal["Accept", "Start", "End", "SwitchAsync", "Cancel", "HandOffESanjeevani"]
    args: dict[str, Any] = Field(default_factory=dict)


@router.post("/teleconsults/{session_id}/commands")
async def teleconsult_command(session_id: uuid.UUID, body: TeleCommandIn,
                              p: Principal = Depends(policy("asha", "doctor", purpose=CC))) -> dict[str, Any]:
    tc = T.teleconsult_sessions
    now = datetime.now(UTC)
    async with uow(p) as tx:
        row = await _session_for(tx, p, session_id)
        values: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        if body.command == "Accept":
            if p.role != "doctor" or row.status != "requested":
                raise AppError("CASE_STATE_CONFLICT", current={"status": row.status})
            values = {"status": "accepted", "doctor_id": p.user_id, "accepted_at": now}
        elif body.command == "Start":
            if row.status not in ("accepted", "async"):
                raise AppError("CASE_STATE_CONFLICT", current={"status": row.status})
            values = {"status": "in_call", "started_at": row.started_at or now, "mode": body.args.get("mode", row.mode)}
        elif body.command == "End":
            reason = body.args.get("endReason", "completed")
            if reason not in ("completed", "call_dropped", "switched_async", "cancelled", "timeout"):
                raise AppError("VALIDATION_FAILED", "Invalid endReason")
            done = row.outcome_entry_id is not None
            values = {"status": "completed" if done else "async", "ended_at": now, "end_reason": reason,
                      "min_bitrate_kbps": body.args.get("minBitrateKbps")}
        elif body.command == "SwitchAsync":
            values = {"status": "async", "mode": "async"}
        elif body.command == "Cancel":
            values = {"status": "cancelled", "end_reason": "cancelled", "ended_at": now}
        elif body.command == "HandOffESanjeevani":
            if p.role != "doctor":
                raise AppError("FORBIDDEN_ROLE")
            values = {"mode": "esanjeevani"}
            extra = {"deepLink": None}  # eSanjeevani integration is Phase 3 (TRD §12)
        await tx.conn.execute(update(tc).where(tc.c.id == session_id).values(**values))
        from app.core.uow import patient_scopes

        await tx.journal("teleconsult_session", session_id, await patient_scopes(tx.conn, row.patient_id))
        await tx.audit(f"teleconsult.{body.command.lower()}", "teleconsult_session", session_id, patient_id=row.patient_id)
        row = (await tx.conn.execute(select(tc).where(tc.c.id == session_id))).first()

        async def _nudge() -> None:
            from app.modules.comms.notify import ws_publish

            await ws_publish(f"teleconsult:{session_id}", {"type": "teleconsult.status", "sessionId": str(session_id),
                                                           "status": row.status})
            await ws_publish(f"user:{row.asha_id}", {"type": "teleconsult.status", "sessionId": str(session_id),
                                                     "status": row.status})
        tx.after_commit(_nudge)
    return {**_session_out(row), **extra}


@router.post("/teleconsults/{session_id}/turn-credentials")
async def fresh_turn(session_id: uuid.UUID, p: Principal = Depends(policy("asha", "doctor", purpose=CC))) -> dict[str, Any]:
    async with uow(p) as tx:
        await _session_for(tx, p, session_id)
    return {"iceServers": turn_credentials(p.user_id), "mediaProfile": MEDIA_PROFILE}


class TeleMessageIn(In):
    id: uuid.UUID
    kind: Literal["vitals", "voice_note", "text"]
    entry_id: uuid.UUID | None = None
    attachment_id: uuid.UUID | None = None
    text: str | None = Field(default=None, max_length=2000)


@router.post("/teleconsults/{session_id}/messages", status_code=201)
async def teleconsult_message(session_id: uuid.UUID, body: TeleMessageIn,
                              p: Principal = Depends(policy("asha", "doctor", purpose=CC))) -> dict[str, Any]:
    """Async fallback after a call drop (TRD §12): text becomes a note entry; vitals/voice link existing items."""
    async with uow(p) as tx:
        row = await _session_for(tx, p, session_id)
        if body.kind == "text":
            if not body.text:
                raise AppError("VALIDATION_FAILED", "text required")
            await svc.insert_entry(tx, p, row.patient_id, {"id": str(body.id), "kind": "note", "notes": body.text},
                                   teleconsult_id=session_id)
        elif body.kind == "voice_note" and body.attachment_id:
            await tx.conn.execute(update(T.attachments).where(and_(T.attachments.c.id == body.attachment_id,
                                                                   T.attachments.c.patient_id == row.patient_id))
                                  .values(entry_id=body.entry_id))
        if row.status in ("in_call", "accepted"):
            await tx.conn.execute(update(T.teleconsult_sessions).where(T.teleconsult_sessions.c.id == session_id)
                                  .values(status="async"))
        await tx.audit("teleconsult.message", "teleconsult_session", session_id, patient_id=row.patient_id,
                       diff={"kind": body.kind})

        async def _nudge() -> None:
            from app.modules.comms.notify import ws_publish

            await ws_publish(f"teleconsult:{session_id}", {"type": "teleconsult.message", "sessionId": str(session_id),
                                                           "messageId": str(body.id), "kind": body.kind})
        tx.after_commit(_nudge)
    return {"id": body.id}


# ---------------- uploads ----------------

EXT = {"image/jpeg": "jpg", "image/webp": "webp", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/amr": "amr",
       "application/pdf": "pdf"}


class PresignIn(In):
    id: uuid.UUID
    purpose: Literal["patient_photo", "screening_photo", "voice_note", "consent_artefact", "prescription",
                     "discharge_summary", "other"]
    content_type: str
    size_bytes: int = Field(ge=1)
    sha256: str = Field(max_length=64)
    patient_id: uuid.UUID | None = None
    entry_id: uuid.UUID | None = None
    case_id: uuid.UUID | None = None
    duration_s: int | None = Field(default=None, ge=0, le=3600)


class PresignOut(Out):
    attachment_id: uuid.UUID
    upload_url: str
    method: str
    headers: dict[str, str]
    expires_at: datetime


@router.post("/uploads/presign", status_code=201, response_model=PresignOut)
async def presign(body: PresignIn, p: Principal = Depends(policy("asha", "doctor", "facility_staff", "patient", purpose=CC))) -> dict[str, Any]:
    if body.content_type not in EXT:
        raise AppError("UNSUPPORTED_MEDIA_TYPE")
    if body.size_bytes > 5 * 1024 * 1024:
        raise AppError("UPLOAD_TOO_LARGE")
    if p.role == "patient" and body.purpose != "consent_artefact":
        raise AppError("FORBIDDEN_ROLE", "Patients may upload consent artefacts only")
    async with uow(p) as tx:
        district = p.district_code or "NA"
        if body.patient_id:
            await _patient_access(tx, p, body.patient_id)
            district = await svc._district_of_patient(tx.conn, body.patient_id) or district
        key = f"{body.purpose}/{district}/{body.id}.{EXT[body.content_type]}"
        a = T.attachments
        if (await tx.conn.execute(select(a.c.id).where(a.c.id == body.id))).first() is None:
            try:
                sha = base64.b64decode(body.sha256)
            except ValueError:
                sha = None
            await tx.conn.execute(insert(a).values(
                id=body.id, purpose=body.purpose, patient_id=body.patient_id, entry_id=body.entry_id, case_id=body.case_id,
                bucket=settings.bucket("attachments"), object_key=key, content_type=body.content_type,
                size_bytes=body.size_bytes, sha256=sha, duration_s=body.duration_s, created_by=p.user_id,
                recorded_at=datetime.now(UTC)))
            await tx.audit("attachment.presign", "attachment", body.id, patient_id=body.patient_id)
    url = objectstore.presign_put(settings.bucket("attachments"), key, body.content_type, body.size_bytes)
    return {"attachmentId": body.id, "uploadUrl": url, "method": "PUT",
            "headers": {"Content-Type": body.content_type, "Content-Length": str(body.size_bytes)},
            "expiresAt": datetime.fromtimestamp(time.time() + 600, UTC)}


@router.post("/uploads/{attachment_id}/complete", status_code=202)
async def upload_complete(attachment_id: uuid.UUID, p: Principal = Depends(policy("asha", "doctor", "facility_staff", "patient", purpose=CC))) -> dict[str, Any]:
    a = T.attachments
    async with uow(p) as tx:
        res = await tx.conn.execute(update(a).where(and_(a.c.id == attachment_id, a.c.created_by == p.user_id,
                                                         a.c.upload_status == "pending"))
                                    .values(upload_status="uploaded", uploaded_at=datetime.now(UTC)))
        if not res.rowcount:
            raise AppError("NOT_FOUND")

        async def _verify() -> None:
            from app.workers.registry import enqueue

            await enqueue("verify_attachment", attachment_id=attachment_id)
        tx.after_commit(_verify)
    return {"attachmentId": attachment_id, "uploadStatus": "uploaded"}


@router.get("/attachments/{attachment_id}/url")
async def attachment_url(attachment_id: uuid.UUID, x_break_glass: str | None = Header(default=None),
                         p: Principal = Depends(policy("asha", "doctor", "facility_staff", "patient", purpose=CC))) -> dict[str, Any]:
    a = T.attachments
    async with uow(p) as tx:
        row = (await tx.conn.execute(select(a).where(a.c.id == attachment_id, a.c.deleted_at.is_(None)))).first()
        if row is None or row.upload_status != "verified" or (row.patient_id and not await can_access_patient(
                tx.conn, p, row.patient_id, grant_id=x_break_glass)):
            raise AppError("NOT_FOUND")
        await tx.audit("attachment.read", "attachment", attachment_id, patient_id=row.patient_id)
    return {"url": objectstore.presign_get(row.bucket, row.object_key), "expiresInS": 300}


# ---------------- care plans ----------------

@router.post("/care-plans", status_code=201)
async def create_care_plan(body: dict[str, Any], p: Principal = Depends(policy("doctor", "facility_staff", purpose=CC))) -> dict[str, Any]:
    from app.modules.continuity import service as continuity

    allowed = {"id", "patientId", "caseId", "teleconsultSessionId", "summary", "nextVisitOn", "items"}
    if set(body) - allowed:
        raise AppError("FIELD_NOT_WRITABLE", f"Not writable: {sorted(set(body) - allowed)}")
    async with uow(p) as tx:
        await _patient_access(tx, p, body.get("patientId"))
        plan_id = await continuity.save_care_plan(tx, p, body)
        asha = await svc._asha_of_patient(tx.conn, body["patientId"])
        tasks = await continuity.tasks_from_plan(tx, plan_id, asha_id=asha, source_case_id=body.get("caseId"))
        if body.get("teleconsultSessionId"):
            tc = T.teleconsult_sessions
            row = (await tx.conn.execute(select(tc).where(tc.c.id == body["teleconsultSessionId"]))).first()
            if row and row.outcome_entry_id and row.status == "async":
                await tx.conn.execute(update(tc).where(tc.c.id == row.id).values(status="completed"))
    return {"id": plan_id, "tasksCreated": [str(t) for t in tasks]}


@router.get("/patients/{patient_id}/care-plans")
async def patient_care_plans(patient_id: uuid.UUID, status: str = "active",
                             p: Principal = Depends(policy("asha", "doctor", "facility_staff", purpose=CC))) -> dict[str, Any]:
    cp, cpi = T.care_plans, T.care_plan_items
    async with uow(p) as tx:
        await _patient_access(tx, p, patient_id)
        plans = (await tx.conn.execute(select(cp).where(cp.c.patient_id == patient_id, cp.c.status.in_(status.split(",")))
                                       .order_by(cp.c.recorded_at.desc()))).all()
        out = []
        for pl in plans:
            items = (await tx.conn.execute(select(cpi).where(cpi.c.care_plan_id == pl.id).order_by(cpi.c.sort_order))).all()
            out.append({"id": pl.id, "patientId": pl.patient_id, "caseId": pl.case_id,
                        "teleconsultSessionId": pl.teleconsult_session_id, "authorId": pl.author_id,
                        "authorRole": pl.author_role, "facilityId": pl.facility_id,
                        "summary": await decrypt(tx.conn, "patient", patient_id, "care_plans", "summary_enc", pl.id, pl.summary_enc),
                        "nextVisitOn": pl.next_visit_on, "status": pl.status, "recordedAt": pl.recorded_at,
                        "items": [{"id": i.id, "kind": i.kind, "medicineName": i.medicine_name, "doseText": i.dose_text,
                                   "frequencyText": i.frequency_text, "durationDays": i.duration_days,
                                   "dueOffsetDays": i.due_offset_days, "taskType": i.task_type,
                                   "note": await decrypt(tx.conn, "patient", patient_id, "care_plan_items", "note_enc", i.id, i.note_enc),
                                   "sortOrder": i.sort_order} for i in items]})
        await tx.audit("patient.read", "care_plan", None, patient_id=patient_id)
    return {"data": out}


