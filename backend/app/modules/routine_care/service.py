"""Longitudinal record: append-only entries, screenings with server-side risk rules (API-Guide §6.3, TRD §12A)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, insert, select

from app.core.crypto import decrypt, encrypt
from app.core.db import Conn, T, point
from app.core.errors import AppError
from app.core.rbac import Principal
from app.core.uow import UoW, patient_scopes
from app.modules.routine_care import risk

ENTRY_KINDS = ("screening", "teleconsult", "referral_outcome", "discharge", "note", "anc_visit", "pnc_visit", "immunisation")
ENTRY_WRITABLE = {"id", "kind", "vitals", "symptoms", "notes", "deviceRiskFlags", "riskRuleSetVersion", "supersedesEntryId",
                  "enteredInError", "location", "attachmentIds", "recordedAt", "caseId", "teleconsultSessionId"}
# Vitals ranges mirror entry_vitals ev_*_ck (database.md §5.7) so the API answers 422 with min/max
RANGES = {"bpSystolic": (50, 280), "bpDiastolic": (20, 180), "pulseBpm": (20, 260), "respRate": (4, 120),
          "spo2Pct": (40, 100), "tempC": (28.0, 44.0), "weightKg": (0.30, 250.0), "heightCm": (20.0, 230.0),
          "muacCm": (5.0, 50.0), "hbGDl": (2.0, 25.0), "rbsMgDl": (20, 900), "fetalHrBpm": (60, 220),
          "gestationWeeks": (1, 45)}


def _validate_vitals(vitals: dict[str, Any]) -> dict[str, Any]:
    cols: dict[str, Any] = {}
    fields = []
    for k, v in vitals.items():
        if k not in risk.VITAL_COLUMNS:
            fields.append({"path": f"vitals.{k}", "code": "not_allowed"})
            continue
        if v is None:
            continue
        if k in RANGES:
            lo, hi = RANGES[k]
            if not isinstance(v, (int, float)) or not lo <= v <= hi:
                fields.append({"path": f"vitals.{k}", "code": "out_of_range", "min": lo, "max": hi})
                continue
        cols[risk.VITAL_COLUMNS[k]] = v
    if ("bp_systolic" in cols) != ("bp_diastolic" in cols) or (
            "bp_systolic" in cols and cols["bp_systolic"] <= cols["bp_diastolic"]):
        fields.append({"path": "vitals.bpSystolic", "code": "bp_pair"})
    if fields:
        raise AppError("VALIDATION_FAILED", "Vitals out of range", fields=fields)
    return cols


async def insert_entry(tx: UoW, p: Principal, patient_id: Any, data: dict[str, Any], *, case_id: Any = None,
                       facility_id: Any = None, teleconsult_id: Any = None) -> dict[str, Any]:
    """Append one entry (+ vitals, symptoms, server risk flags, follow-up task). Idempotent by `id`."""
    extra = set(data) - ENTRY_WRITABLE
    if extra:
        raise AppError("FIELD_NOT_WRITABLE", f"Not writable: {sorted(extra)}",
                       fields=[{"path": f, "code": "not_writable"} for f in sorted(extra)])
    e = T.health_record_entries
    entry_id = uuid.UUID(str(data["id"]))
    kind = data.get("kind")
    if kind not in ENTRY_KINDS:
        raise AppError("VALIDATION_FAILED", "Invalid kind", fields=[{"path": "kind", "code": "invalid"}])
    existing = (await tx.conn.execute(select(e.c.id, e.c.patient_id).where(e.c.id == entry_id))).first()
    if existing is not None:
        if existing.patient_id != uuid.UUID(str(patient_id)):
            raise AppError("DUPLICATE_ID")
        return {"entryId": entry_id, "highRisk": None, "riskFlags": [], "tasksCreated": [], "replay": True}
    vitals_cols = _validate_vitals(data.get("vitals") or {})
    symptoms = {s["code"]: bool(s["present"]) for s in (data.get("symptoms") or [])}
    if symptoms:
        known = set((await tx.conn.execute(select(T.symptoms.c.code))).scalars().all())
        bad = set(symptoms) - known
        if bad:
            raise AppError("VALIDATION_FAILED", "Unknown symptom", fields=[{"path": "symptoms", "code": "unknown", "values": sorted(bad)}])
    retract = bool(data.get("enteredInError"))
    has_vital = any(k != "measured_with" for k in vitals_cols)
    if kind == "screening" and not retract and not has_vital and not symptoms:
        raise AppError("VALIDATION_FAILED", "At least one vital or symptom is required",
                       fields=[{"path": "vitals", "code": "missing"}])
    sup = data.get("supersedesEntryId")
    if sup:
        if (await tx.conn.execute(select(exists().where(e.c.supersedes_entry_id == sup)))).scalar():
            raise AppError("ALREADY_SUPERSEDED")
        if not (await tx.conn.execute(select(exists().where(and_(e.c.id == sup, e.c.patient_id == patient_id))))).scalar():
            raise AppError("VALIDATION_FAILED", "supersedesEntryId must be an entry of this patient")
    if retract and not sup:
        raise AppError("VALIDATION_FAILED", "enteredInError needs supersedesEntryId")
    now = datetime.now(UTC)
    recorded = datetime.fromisoformat(str(data["recordedAt"]).replace("Z", "+00:00")) if data.get("recordedAt") else now
    if recorded > now + timedelta(minutes=5):
        recorded = now  # hre_future_ck / HLC skew rule
    district = await _district_of_patient(tx.conn, patient_id)
    fired = [] if retract else await risk.evaluate_patient(tx.conn, patient_id=patient_id, district=district,
                                                           vitals_cols=vitals_cols, symptoms=symptoms)
    loc = data.get("location")
    await tx.conn.execute(insert(e).values(
        id=entry_id, patient_id=patient_id, kind=kind, author_id=p.user_id, author_role=p.role, case_id=case_id or data.get("caseId"),
        teleconsult_session_id=teleconsult_id or data.get("teleconsultSessionId"), facility_id=facility_id,
        notes_enc=await encrypt(tx.conn, "patient", patient_id, "health_record_entries", "notes_enc", entry_id,
                                data["notes"]) if data.get("notes") else None,
        high_risk=bool(fired), risk_rule_set_version=max((r.rule_set_version for r in fired), default=None),
        supersedes_entry_id=sup, entered_in_error=retract,
        location=point(loc["lat"], loc["lng"]) if loc else None, recorded_at=recorded))
    if has_vital:
        await tx.conn.execute(insert(T.entry_vitals).values(entry_id=entry_id, **vitals_cols))
    for code, present in symptoms.items():
        await tx.conn.execute(insert(T.entry_symptoms).values(entry_id=entry_id, symptom_code=code, present=present))
    rr = T.risk_rules
    device_flags = set(data.get("deviceRiskFlags") or [])
    if device_flags:
        for r in (await tx.conn.execute(select(rr.c.id).where(rr.c.code.in_(device_flags), rr.c.retired_at.is_(None)))).all():
            await tx.conn.execute(insert(T.entry_risk_flags).values(entry_id=entry_id, risk_rule_id=r.id, evaluated_by="device"))
    for r in fired:
        await tx.conn.execute(insert(T.entry_risk_flags).values(entry_id=entry_id, risk_rule_id=r.id, evaluated_by="server"))
    for att in data.get("attachmentIds") or []:
        from sqlalchemy import update

        await tx.conn.execute(update(T.attachments).where(and_(T.attachments.c.id == att,
                                                               T.attachments.c.patient_id == patient_id)).values(entry_id=entry_id))
    await tx.journal("health_record_entry", entry_id, await patient_scopes(tx.conn, patient_id))
    await tx.audit("entry.create", "health_record_entry", entry_id, patient_id=patient_id,
                   diff={"kind": kind, "highRisk": bool(fired)}, purpose="continuity_of_care")
    tasks = []
    if fired and p.role == "asha":
        from app.modules.continuity.service import make_task

        rule = min(fired, key=lambda r: r.follow_up_days)
        asha_id = await _asha_of_patient(tx.conn, patient_id)
        tid = await make_task(tx, patient_id=patient_id, asha_id=asha_id, task_type="high_risk_recheck",
                              due=date.today() + timedelta(days=rule.follow_up_days), source_kind="risk_rule",
                              dedupe_key=f"entry:{entry_id}:risk:{rule.code}", priority="urgent",
                              source_entry_id=entry_id, risk_rule_id=rule.id)
        if tid:
            tasks.append({"id": tid, "taskType": "high_risk_recheck",
                          "dueDate": (date.today() + timedelta(days=rule.follow_up_days)).isoformat(), "priority": "urgent"})
    return {"entryId": entry_id, "highRisk": bool(fired),
            "riskFlags": [{"ruleCode": r.code, "evaluatedBy": "server"} for r in fired], "tasksCreated": tasks}


async def _district_of_patient(conn: Conn, patient_id: Any) -> str | None:
    return (await conn.execute(select(T.villages.c.district_code).join(
        T.households, T.households.c.village_id == T.villages.c.id).join(
        T.patients, T.patients.c.household_id == T.households.c.id).where(T.patients.c.id == patient_id))).scalar()


async def _asha_of_patient(conn: Conn, patient_id: Any) -> Any:
    return (await conn.execute(select(T.households.c.asha_id).join(
        T.patients, T.patients.c.household_id == T.households.c.id).where(T.patients.c.id == patient_id))).scalar()


async def entries_out(conn: Conn, rows: list[Any], *, include_notes: bool = True) -> list[dict[str, Any]]:
    if not rows:
        return []
    ids = [r.id for r in rows]
    ev, es, erf, rr, u, att = (T.entry_vitals, T.entry_symptoms, T.entry_risk_flags, T.risk_rules, T.users,
                               T.attachments)
    vit = {r.entry_id: r for r in (await conn.execute(select(ev).where(ev.c.entry_id.in_(ids)))).all()}
    sym: dict[Any, list[dict[str, Any]]] = {}
    for r in (await conn.execute(select(es).where(es.c.entry_id.in_(ids)))).all():
        sym.setdefault(r.entry_id, []).append({"code": r.symptom_code, "present": r.present})
    flags: dict[Any, list[dict[str, Any]]] = {}
    for r in (await conn.execute(select(erf.c.entry_id, erf.c.evaluated_by, rr.c.code).join(rr, rr.c.id == erf.c.risk_rule_id)
                                 .where(erf.c.entry_id.in_(ids)))).all():
        flags.setdefault(r.entry_id, []).append({"ruleCode": r.code, "evaluatedBy": r.evaluated_by})
    atts: dict[Any, list[dict[str, Any]]] = {}
    for r in (await conn.execute(select(att.c.id, att.c.entry_id, att.c.purpose, att.c.content_type)
                                 .where(att.c.entry_id.in_(ids), att.c.deleted_at.is_(None)))).all():
        atts.setdefault(r.entry_id, []).append({"id": r.id, "purpose": r.purpose, "contentType": r.content_type})
    superseded = set((await conn.execute(select(T.health_record_entries.c.supersedes_entry_id).where(
        T.health_record_entries.c.supersedes_entry_id.in_(ids)))).scalars().all())
    authors = {r.id: r for r in (await conn.execute(select(u.c.id, u.c.name_enc).where(
        u.c.id.in_({r.author_id for r in rows})))).all()}
    out = []
    for r in rows:
        v = vit.get(r.id)
        vitals = None
        if v is not None:
            vitals = {api: (float(getattr(v, col)) if getattr(v, col) is not None and col in (
                "temp_c", "weight_kg", "height_cm", "muac_cm", "hb_g_dl") else getattr(v, col))
                for api, col in risk.VITAL_COLUMNS.items()}
        a = authors.get(r.author_id)
        out.append({
            "id": r.id, "patientId": r.patient_id, "kind": r.kind, "authorId": r.author_id, "authorRole": r.author_role,
            "authorName": await decrypt(conn, "user", a.id, "users", "name_enc", a.id, a.name_enc) if a else None,
            "caseId": r.case_id, "teleconsultSessionId": r.teleconsult_session_id, "facilityId": r.facility_id,
            "vitals": vitals, "symptoms": sym.get(r.id), "notes": await decrypt(
                conn, "patient", r.patient_id, "health_record_entries", "notes_enc", r.id, r.notes_enc) if include_notes else None,
            "highRisk": r.high_risk, "riskFlags": flags.get(r.id, []), "supersedesEntryId": r.supersedes_entry_id,
            "enteredInError": r.entered_in_error, "superseded": r.id in superseded, "attachments": atts.get(r.id),
            "recordedAt": r.recorded_at, "serverReceivedAt": r.server_received_at})
    return out


