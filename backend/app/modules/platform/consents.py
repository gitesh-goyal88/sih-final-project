"""DPDP consent artefacts (TRD §14.4, API-Guide §6.12, SEC-PRV-01). Append-only: withdrawal = new row."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select

from app.core.db import T
from app.core.errors import AppError
from app.core.rbac import Principal, can_access_patient
from app.core.uow import UoW, patient_scopes

ALLOWED = {"id", "patientId", "purpose", "status", "method", "language", "noticeVersion", "effectiveAt", "expiresAt",
           "witnessUserId", "guardianPatientId", "artefactAttachmentId", "supersedesId", "recordedAt"}
PURPOSES = ("emergency_care", "continuity_of_care", "programme_reporting", "abdm_sharing")
METHODS = ("verbal_witnessed", "otp", "thumb_impression", "signature", "guardian")


def _ts(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


async def record_consent(tx: UoW, p: Principal, data: dict[str, Any], *, path: str = "consent") -> uuid.UUID:
    extra = set(data) - ALLOWED
    if extra:
        raise AppError("FIELD_NOT_WRITABLE", f"Not writable: {sorted(extra)}")
    try:
        cid, patient_id = uuid.UUID(str(data["id"])), uuid.UUID(str(data["patientId"]))
    except (KeyError, ValueError) as exc:
        raise AppError("VALIDATION_FAILED", f"{path}: id and patientId required") from exc
    if data.get("purpose") not in PURPOSES:
        raise AppError("VALIDATION_FAILED", f"{path}.purpose invalid", fields=[{"path": f"{path}.purpose", "code": "invalid"}])
    if data.get("method") not in METHODS or data.get("status") not in ("granted", "withdrawn"):
        raise AppError("VALIDATION_FAILED", f"{path}.method/status invalid", fields=[{"path": f"{path}.method", "code": "invalid"}])
    if not await can_access_patient(tx.conn, p, patient_id, write=p.role == "asha"):
        raise AppError("FORBIDDEN_SCOPE")
    c = T.consents
    if (await tx.conn.execute(select(c.c.id).where(c.c.id == cid))).first():
        return cid  # append-only: same id = idempotent replay
    witness = data.get("witnessUserId")
    if data["method"] == "verbal_witnessed" and not witness and p.role == "asha":
        witness = p.user_id  # verbal consent witnessed by the ASHA who captured it (TRD §14.4)
    effective = _ts(data.get("effectiveAt")) or datetime.now(UTC)
    await tx.conn.execute(insert(c).values(
        id=cid, patient_id=patient_id, purpose=data["purpose"], status=data["status"], method=data["method"],
        language=data.get("language", "hi"), notice_version=data.get("noticeVersion", "unknown"),
        captured_by=p.user_id, witness_user_id=witness, guardian_patient_id=data.get("guardianPatientId"),
        artefact_attachment_id=data.get("artefactAttachmentId"), supersedes_id=data.get("supersedesId"),
        effective_at=effective, expires_at=_ts(data.get("expiresAt")),
        recorded_at=_ts(data.get("recordedAt")) or datetime.now(UTC)))
    await tx.journal("consent", cid, await patient_scopes(tx.conn, patient_id))
    await tx.audit("consent.create", "consent", cid, patient_id=patient_id,
                   diff={"purpose": data["purpose"], "status": data["status"], "method": data["method"]},
                   purpose="continuity_of_care")
    return cid
