"""Households and patients — the unified household-based record (PRD pillar 1, API-Guide §6.2, §8.2).

Every function takes a `UoW` so REST routes and /sync ops share one implementation. Writable fields are
explicit allow-lists per entity and role (SEC-API-02, SF-08); server-derived fields are never client-set.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import and_, exists, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.core import hlc as hlcmod
from app.core.crypto import decrypt, encrypt, mask_phone, normalise_phone, phone_hash, preload_deks
from app.core.db import Conn, T, lat_of, lng_of, point
from app.core.errors import AppError
from app.core.ids import new_short_code
from app.core.logging import security_event
from app.core.rbac import Principal, can_access_patient
from app.core.uow import UoW, household_scopes, patient_scopes

HOUSEHOLD_WRITABLE = {"houseNumber", "location", "locationSource", "registeredPhone", "headMemberId"}
PATIENT_WRITABLE = {"name", "sex", "dateOfBirth", "dobIsEstimated", "phone", "relationshipToHead", "bloodGroup"}
COHORT_WRITABLE = {"cohort", "startedOn", "endedOn", "endedReason", "lmpDate", "eddDate"}
SEXES = {"F", "M", "O"}
RELATIONSHIPS = {"self", "spouse", "child", "parent", "sibling", "grandchild", "in_law", "other"}
BLOOD = {"A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-", "unknown"}


def _bad(path: str, code: str, **extra: Any) -> AppError:
    return AppError("VALIDATION_FAILED", f"{path}: {code}", fields=[{"path": path, "code": code, **extra}])


def _date(value: Any, path: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise _bad(path, "invalid_date") from exc


def _ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _uuid(value: Any, path: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise _bad(path, "invalid_uuid") from exc


def age_years(dob: date | None) -> int | None:
    if dob is None:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


async def _assign_short_code(conn: Conn, table: Any, row_id: Any) -> str:
    """6-char Crockford code, globally unique (FR-D02, schema R16). Retries on the rare collision."""
    for _ in range(8):
        code = new_short_code()
        taken = (await conn.execute(select(exists().where(table.c.short_code == code)))).scalar()
        if not taken:
            return code
    raise AppError("INTERNAL", "Could not allocate a short code")


def _validate_patient(data: dict[str, Any], path: str) -> None:
    if data.get("sex") is not None and data["sex"] not in SEXES:
        raise _bad(f"{path}.sex", "invalid")
    dob = _date(data.get("dateOfBirth"), f"{path}.dateOfBirth")
    if dob and dob < date(1900, 1, 1):
        raise _bad(f"{path}.dateOfBirth", "before_1900")
    if dob and dob > date.today():
        raise _bad(f"{path}.dateOfBirth", "in_future")
    if data.get("relationshipToHead") not in (None, *RELATIONSHIPS):
        raise _bad(f"{path}.relationshipToHead", "invalid")
    if data.get("bloodGroup") not in (None, *BLOOD):
        raise _bad(f"{path}.bloodGroup", "invalid")
    if "name" in data and (not isinstance(data["name"], str) or not data["name"].strip() or len(data["name"]) > 120):
        raise _bad(f"{path}.name", "invalid")


def _check_allowed(data: dict[str, Any], allowed: set[str], entity: str, p: Principal) -> None:
    extra = set(data) - allowed
    if extra:
        security_event("sync.field_not_writable", entity=entity, fields=sorted(extra), user_id=str(p.user_id))
        raise AppError("FIELD_NOT_WRITABLE", f"Not writable: {', '.join(sorted(extra))}",
                       fields=[{"path": f, "code": "not_writable"} for f in sorted(extra)])


async def _require_village(p: Principal, village_id: Any) -> None:
    if p.role != "asha" or str(village_id) not in p.villages:
        raise AppError("FORBIDDEN_SCOPE", "Village is not assigned to you")


async def _household_row(conn: Conn, household_id: Any) -> Any:
    h = T.households
    return (await conn.execute(select(h).where(h.c.id == household_id, h.c.deleted_at.is_(None)))).first()


async def create_household(tx: UoW, p: Principal, data: dict[str, Any], stamp: str | None = None) -> uuid.UUID:
    hid = _uuid(data.get("id"), "id")
    village_id = _uuid(data.get("villageId"), "villageId")
    await _require_village(p, village_id)
    existing = await _household_row(tx.conn, hid)
    if existing is not None:
        if existing.village_id == village_id and existing.asha_id == p.user_id:
            return hid  # idempotent replay
        raise AppError("DUPLICATE_ID")
    stamp, _ = hlcmod.sanitise(stamp)
    recorded_at = _ts(data.get("recordedAt"))
    loc = data.get("location")
    loc_source = data.get("locationSource") if loc else None
    if loc and loc_source not in ("gps", "map_pin", "village"):
        raise _bad("locationSource", "invalid")
    reg_phone = data.get("registeredPhone")
    values: dict[str, Any] = dict(
        id=hid, village_id=village_id, asha_id=p.user_id, house_number=data.get("houseNumber"),
        location=point(loc["lat"], loc["lng"]) if loc else None, location_source=loc_source,
        field_clock={f: stamp for f in HOUSEHOLD_WRITABLE}, recorded_at=recorded_at, created_by=p.user_id)
    if reg_phone:
        values["registered_phone_enc"] = await encrypt(tx.conn, "household", hid, "households", "registered_phone_enc",
                                                       hid, normalise_phone(reg_phone))
        values["registered_phone_hash"] = phone_hash(reg_phone)
    head = data.get("headMemberId")
    members = data.get("members") or []
    if members and not head:
        head = members[0].get("id")
    await tx.conn.execute(insert(T.households).values(head_member_id=head, **values))  # head FK is DEFERRED
    scopes = await household_scopes(tx.conn, hid)
    await tx.journal("household", hid, scopes)
    await tx.audit("household.create", "household", hid, diff={"fields": sorted(k for k in data if k != "members")})
    for i, m in enumerate(members):
        await create_patient(tx, p, hid, m, stamp, path=f"members[{i}]", skip_scope_check=True)
    for i, c in enumerate(data.get("consents") or []):
        from app.modules.platform.consents import record_consent

        await record_consent(tx, p, c, path=f"consents[{i}]")
    return hid


async def create_patient(tx: UoW, p: Principal, household_id: Any, data: dict[str, Any], stamp: str | None = None,
                         *, path: str = "", skip_scope_check: bool = False) -> uuid.UUID:
    pid = _uuid(data.get("id"), f"{path}.id")
    hh = await _household_row(tx.conn, household_id)
    if hh is None:
        raise AppError("NOT_FOUND", "Household not found")
    if not skip_scope_check:
        await _require_village(p, hh.village_id)
    pt = T.patients
    existing = (await tx.conn.execute(select(pt.c.household_id).where(pt.c.id == pid))).first()
    if existing is not None:
        if existing.household_id == hh.id:
            return pid
        raise AppError("DUPLICATE_ID")
    fields = {k: v for k, v in data.items() if k in PATIENT_WRITABLE}
    _validate_patient({"sex": data.get("sex"), **fields}, path or "patient")
    if not data.get("name") or data.get("sex") not in SEXES:
        raise _bad(f"{path}.name" if not data.get("name") else f"{path}.sex", "missing")
    stamp, _ = hlcmod.sanitise(stamp)
    phone = data.get("phone")
    await tx.conn.execute(insert(pt).values(
        id=pid, household_id=hh.id, short_code=await _assign_short_code(tx.conn, pt, pid),
        name_enc=await encrypt(tx.conn, "patient", pid, "patients", "name_enc", pid, data["name"].strip()),
        sex=data["sex"], date_of_birth=_date(data.get("dateOfBirth"), f"{path}.dateOfBirth"),
        dob_is_estimated=bool(data.get("dobIsEstimated", False)), relationship_to_head=data.get("relationshipToHead"),
        phone_enc=await encrypt(tx.conn, "patient", pid, "patients", "phone_enc", pid, normalise_phone(phone)) if phone else None,
        phone_hash=phone_hash(phone) if phone else None, blood_group=data.get("bloodGroup"),
        field_clock={f: stamp for f in PATIENT_WRITABLE}, recorded_at=_ts(data.get("recordedAt")), created_by=p.user_id))
    scopes = await household_scopes(tx.conn, hh.id)
    await tx.journal("patient", pid, scopes)
    await tx.audit("patient.create", "patient", pid, patient_id=pid, diff={"fields": sorted(fields)})
    for j, c in enumerate(data.get("cohorts") or []):
        await start_cohort(tx, p, pid, c, sex=data["sex"], path=f"{path}.cohorts[{j}]")
    return pid


async def start_cohort(tx: UoW, p: Principal, patient_id: Any, data: dict[str, Any], *, sex: str | None = None,
                       path: str = "cohort") -> uuid.UUID:
    cid = _uuid(data.get("id"), f"{path}.id")
    cohort = data.get("cohort")
    if cohort not in ("pregnant", "newborn", "chronic", "elderly"):
        raise _bad(f"{path}.cohort", "invalid")
    if sex is None:
        sex = (await tx.conn.execute(select(T.patients.c.sex).where(T.patients.c.id == patient_id))).scalar()
    if cohort == "pregnant" and sex == "M":
        raise _bad(f"{path}.cohort", "pregnant_cohort_on_male")
    lmp, edd = _date(data.get("lmpDate"), f"{path}.lmpDate"), _date(data.get("eddDate"), f"{path}.eddDate")
    if cohort != "pregnant" and (lmp or edd):
        raise _bad(f"{path}.lmpDate", "pregnancy_only")
    if lmp and edd and edd <= lmp:
        raise _bad(f"{path}.eddDate", "edd_before_lmp")
    pc = T.patient_cohorts
    if (await tx.conn.execute(select(pc.c.id).where(pc.c.id == cid))).first():
        return cid
    active = (await tx.conn.execute(select(pc.c.id).where(and_(pc.c.patient_id == patient_id, pc.c.cohort == cohort,
                                                                pc.c.ended_on.is_(None))))).first()
    if active:
        raise AppError("DUPLICATE_ID", f"Patient already has an active {cohort} cohort")
    await tx.conn.execute(insert(pc).values(
        id=cid, patient_id=patient_id, cohort=cohort, started_on=_date(data.get("startedOn"), f"{path}.startedOn") or date.today(),
        lmp_date=lmp, edd_date=edd, recorded_by=p.user_id, recorded_at=_ts(data.get("recordedAt"))))
    await tx.journal("patient_cohort", cid, await patient_scopes(tx.conn, patient_id))
    await tx.audit("patient_cohort.create", "patient_cohort", cid, patient_id=patient_id, diff={"cohort": cohort})
    return cid


async def end_cohort(tx: UoW, p: Principal, patient_id: Any, cohort_id: Any, data: dict[str, Any]) -> None:
    _check_allowed(data, {"endedOn", "endedReason"}, "patient_cohort", p)
    reason = data.get("endedReason")
    if reason not in (None, "delivered", "pregnancy_loss", "aged_out", "resolved", "deceased", "moved", "data_error"):
        raise _bad("endedReason", "invalid")
    pc = T.patient_cohorts
    res = await tx.conn.execute(update(pc).where(and_(pc.c.id == cohort_id, pc.c.patient_id == patient_id))
                                .values(ended_on=_date(data.get("endedOn"), "endedOn") or date.today(), ended_reason=reason))
    if not res.rowcount:
        raise AppError("NOT_FOUND")
    await tx.journal("patient_cohort", cohort_id, await patient_scopes(tx.conn, patient_id))
    await tx.audit("patient_cohort.end", "patient_cohort", cohort_id, patient_id=patient_id, diff={"endedReason": reason})


async def update_household(tx: UoW, p: Principal, household_id: Any, fields: dict[str, Any], stamp: str | None,
                           base_version: int | None = None) -> list[str]:
    _check_allowed(fields, HOUSEHOLD_WRITABLE, "household", p)
    hh = await _household_row(tx.conn, household_id)
    if hh is None:
        raise AppError("NOT_FOUND")
    await _require_village(p, hh.village_id)
    stamp, restamped = hlcmod.sanitise(stamp)
    apply, lost, clock = hlcmod.merge_fields(hh.field_clock, fields, stamp)
    values: dict[str, Any] = {"field_clock": clock}
    if "houseNumber" in apply:
        values["house_number"] = apply["houseNumber"]
    if "location" in apply:
        loc = apply["location"]
        values["location"] = point(loc["lat"], loc["lng"]) if loc else None
        if not loc:
            values["location_source"] = None
    if "locationSource" in apply and apply.get("location", True):
        values["location_source"] = apply["locationSource"]
    if "registeredPhone" in apply:
        rp = apply["registeredPhone"]
        values["registered_phone_enc"] = await encrypt(tx.conn, "household", hh.id, "households", "registered_phone_enc",
                                                       hh.id, normalise_phone(rp)) if rp else None
        values["registered_phone_hash"] = phone_hash(rp) if rp else None
    if "headMemberId" in apply:
        values["head_member_id"] = apply["headMemberId"]
    await tx.conn.execute(update(T.households).where(T.households.c.id == hh.id).values(**values))
    await tx.journal("household", hh.id, await household_scopes(tx.conn, hh.id))
    await tx.audit("household.update", "household", hh.id,
                   diff={"fields": sorted(apply), "lost": sorted(lost), "clockSkew": restamped})
    return lost


async def update_patient(tx: UoW, p: Principal, patient_id: Any, fields: dict[str, Any], stamp: str | None,
                         base_version: int | None = None) -> list[str]:
    """Field-level LWW; the losing value is recorded (field names only — values are encrypted) in audit."""
    _check_allowed(fields, PATIENT_WRITABLE, "patient", p)
    _validate_patient(fields, "patient")
    pt = T.patients
    row = (await tx.conn.execute(select(pt).where(pt.c.id == patient_id, pt.c.deleted_at.is_(None)))).first()
    if row is None or not await can_access_patient(tx.conn, p, patient_id, write=True) or p.role != "asha":
        raise AppError("FORBIDDEN_SCOPE" if row is not None else "NOT_FOUND")
    stamp, restamped = hlcmod.sanitise(stamp)
    apply, lost, clock = hlcmod.merge_fields(row.field_clock, fields, stamp)
    values: dict[str, Any] = {"field_clock": clock}
    if "name" in apply:
        values["name_enc"] = await encrypt(tx.conn, "patient", row.id, "patients", "name_enc", row.id, apply["name"].strip())
    if "phone" in apply:
        ph = apply["phone"]
        values["phone_enc"] = await encrypt(tx.conn, "patient", row.id, "patients", "phone_enc", row.id,
                                            normalise_phone(ph)) if ph else None
        values["phone_hash"] = phone_hash(ph) if ph else None
    for api, col in (("sex", "sex"), ("relationshipToHead", "relationship_to_head"), ("bloodGroup", "blood_group"),
                     ("dobIsEstimated", "dob_is_estimated")):
        if api in apply:
            values[col] = apply[api]
    if "dateOfBirth" in apply:
        values["date_of_birth"] = _date(apply["dateOfBirth"], "dateOfBirth")
    await tx.conn.execute(update(pt).where(pt.c.id == row.id).values(**values))
    await tx.journal("patient", row.id, await patient_scopes(tx.conn, row.id))
    await tx.audit("patient.update", "patient", row.id, patient_id=row.id,
                   diff={"fields": sorted(apply), "lost": sorted(lost), "clockSkew": restamped})
    return lost


async def patient_command(tx: UoW, p: Principal, patient_id: Any, name: str, args: dict[str, Any]) -> None:
    pt = T.patients
    if not await can_access_patient(tx.conn, p, patient_id, write=True) or p.role != "asha":
        raise AppError("FORBIDDEN_SCOPE")
    if name == "MovePatient":
        to_hh = await _household_row(tx.conn, args.get("toHouseholdId"))
        if to_hh is None:
            raise AppError("NOT_FOUND", "Target household not found")
        await _require_village(p, to_hh.village_id)
        old_scopes = await patient_scopes(tx.conn, patient_id)
        await tx.conn.execute(update(pt).where(pt.c.id == patient_id).values(household_id=to_hh.id))
        await tx.journal("patient", patient_id, old_scopes + await household_scopes(tx.conn, to_hh.id))
        await tx.audit("patient.move", "patient", patient_id, patient_id=patient_id,
                       diff={"toHouseholdId": str(to_hh.id), "reason": args.get("reason")})
    elif name == "MarkDeceased":
        await tx.conn.execute(update(pt).where(pt.c.id == patient_id).values(deceased_at=_ts(args.get("deceasedAt"))))
        await tx.journal("patient", patient_id, await patient_scopes(tx.conn, patient_id))
        await tx.audit("patient.deceased", "patient", patient_id, patient_id=patient_id)
    else:
        raise AppError("VALIDATION_FAILED", f"Unknown command {name}")


async def add_condition(tx: UoW, p: Principal, patient_id: Any, data: dict[str, Any]) -> uuid.UUID:
    _check_allowed(data, {"id", "conditionCode", "note", "notedOn", "status", "recordedAt"}, "patient_condition", p)
    if not await can_access_patient(tx.conn, p, patient_id, write=True) or p.role not in ("asha", "doctor"):
        raise AppError("FORBIDDEN_SCOPE")
    cid = _uuid(data.get("id"), "id")
    pc = T.patient_conditions
    if (await tx.conn.execute(select(pc.c.id).where(pc.c.id == cid))).first():
        return cid
    await tx.conn.execute(insert(pc).values(
        id=cid, patient_id=patient_id, condition_code=data.get("conditionCode"),
        note_enc=await encrypt(tx.conn, "patient", patient_id, "patient_conditions", "note_enc", cid, data["note"]) if data.get("note") else None,
        status=data.get("status", "active"), noted_on=_date(data.get("notedOn"), "notedOn") or date.today(),
        recorded_by=p.user_id, recorded_at=_ts(data.get("recordedAt"))))
    await tx.journal("patient_condition", cid, await patient_scopes(tx.conn, patient_id))
    await tx.audit("patient_condition.create", "patient_condition", cid, patient_id=patient_id)
    return cid


async def add_medication(tx: UoW, p: Principal, patient_id: Any, data: dict[str, Any]) -> uuid.UUID:
    _check_allowed(data, {"id", "medicineName", "doseText", "frequencyText", "startedOn", "stoppedOn", "recordedAt"},
                   "patient_medication", p)
    if not await can_access_patient(tx.conn, p, patient_id, write=True) or p.role not in ("asha", "doctor"):
        raise AppError("FORBIDDEN_SCOPE")
    mid = _uuid(data.get("id"), "id")
    pm = T.patient_medications
    if (await tx.conn.execute(select(pm.c.id).where(pm.c.id == mid))).first():
        return mid
    if not data.get("medicineName"):
        raise _bad("medicineName", "missing")
    await tx.conn.execute(insert(pm).values(
        id=mid, patient_id=patient_id, medicine_name=data["medicineName"], dose_text=data.get("doseText"),
        frequency_text=data.get("frequencyText"), started_on=_date(data.get("startedOn"), "startedOn"),
        stopped_on=_date(data.get("stoppedOn"), "stoppedOn"),
        prescribed_by=p.user_id if p.role == "doctor" else None, recorded_by=p.user_id,
        recorded_at=_ts(data.get("recordedAt"))))
    await tx.journal("patient_medication", mid, await patient_scopes(tx.conn, patient_id))
    await tx.audit("patient_medication.create", "patient_medication", mid, patient_id=patient_id)
    return mid


# ---------------- serializers ----------------

async def patients_out(conn: Conn, rows: list[Any], projection: str = "asha") -> list[dict[str, Any]]:
    """Patient projection per role (API-Guide §2.9). Family mode hides adult details (SEC-PRV-04)."""
    if not rows:
        return []
    ids = [r.id for r in rows]
    await preload_deks(conn, "patient", ids)
    pc, vc, e = T.patient_cohorts, T.v_active_consents, T.health_record_entries
    cohorts: dict[Any, list[dict[str, Any]]] = {}
    for c in (await conn.execute(select(pc).where(pc.c.patient_id.in_(ids), pc.c.ended_on.is_(None)))).all():
        cohorts.setdefault(c.patient_id, []).append({
            "id": c.id, "cohort": c.cohort, "startedOn": c.started_on, "endedOn": c.ended_on,
            "lmpDate": c.lmp_date, "eddDate": c.edd_date})
    consents: dict[Any, list[dict[str, Any]]] = {}
    for c in (await conn.execute(select(vc).where(vc.c.patient_id.in_(ids)))).all():
        consents.setdefault(c.patient_id, []).append({"purpose": c.purpose, "status": c.status,
                                                      "effectiveAt": c.effective_at})
    latest = select(e.c.patient_id, e.c.high_risk, func.row_number().over(
        partition_by=e.c.patient_id, order_by=e.c.recorded_at.desc()).label("rn")).where(
        e.c.patient_id.in_(ids), e.c.kind == "screening", e.c.entered_in_error.is_(False)).subquery()
    risk = {r.patient_id: r.high_risk for r in (await conn.execute(select(latest).where(latest.c.rn == 1))).all()}
    out = []
    for r in rows:
        name = await decrypt(conn, "patient", r.id, "patients", "name_enc", r.id, r.name_enc)
        phone = await decrypt(conn, "patient", r.id, "patients", "phone_enc", r.id, r.phone_enc)
        age = age_years(r.date_of_birth)
        item: dict[str, Any] = {
            "id": r.id, "householdId": r.household_id, "shortCode": r.short_code, "name": name, "sex": r.sex,
            "dateOfBirth": r.date_of_birth, "dobIsEstimated": r.dob_is_estimated, "ageYears": age,
            "relationshipToHead": r.relationship_to_head, "phoneMasked": mask_phone(phone), "bloodGroup": r.blood_group,
            "abhaLinked": r.abha_linked_at is not None, "cohorts": cohorts.get(r.id, []),
            "highRisk": bool(risk.get(r.id, False)), "consents": consents.get(r.id, []),
            "recordedAt": r.recorded_at, "updatedAt": r.updated_at, "version": r.version}
        if projection == "volunteer":
            item = {"id": r.id, "householdId": r.household_id, "name": (name or "").split(" ")[0], "sex": r.sex,
                    "dobIsEstimated": r.dob_is_estimated, "recordedAt": r.recorded_at, "updatedAt": r.updated_at,
                    "version": r.version}
        elif projection == "patient" and (age is None or age >= 18):
            item.update({"phoneMasked": None, "bloodGroup": None, "cohorts": [], "highRisk": False})
        out.append(item)
    return out


async def patient_rows(conn: Conn, ids: list[Any]) -> list[Any]:
    pt = T.patients
    return (await conn.execute(select(pt).where(pt.c.id.in_(ids), pt.c.deleted_at.is_(None)))).all() if ids else []


async def households_out(conn: Conn, rows: list[Any], *, with_members: bool, projection: str = "asha") -> list[dict[str, Any]]:
    if not rows:
        return []
    await preload_deks(conn, "household", [r.id for r in rows])
    members: dict[Any, list[dict[str, Any]]] = {}
    if with_members:
        pt = T.patients
        prow = (await conn.execute(select(pt).where(pt.c.household_id.in_([r.id for r in rows]), pt.c.deleted_at.is_(None))
                                   .order_by(pt.c.recorded_at))).all()
        for m in await patients_out(conn, prow, projection):
            members.setdefault(m["householdId"], []).append(m)
    out = []
    for r in rows:
        phone = await decrypt(conn, "household", r.id, "households", "registered_phone_enc", r.id, r.registered_phone_enc)
        item = {"id": r.id, "villageId": r.village_id, "ashaId": r.asha_id, "houseNumber": r.house_number,
                "headMemberId": r.head_member_id,
                "location": {"lat": float(r.lat), "lng": float(r.lng)} if getattr(r, "lat", None) is not None else None,
                "locationSource": r.location_source, "registeredPhoneMasked": mask_phone(phone),
                "recordedAt": r.recorded_at, "createdAt": r.created_at, "updatedAt": r.updated_at, "version": r.version}
        if with_members:
            item["members"] = members.get(r.id, [])
        out.append(item)
    return out


def household_select() -> Any:
    h = T.households
    return select(h, lat_of(h.c.location).label("lat"), lng_of(h.c.location).label("lng"))


async def load_households(conn: Conn, ids: list[Any]) -> list[Any]:
    h = T.households
    return (await conn.execute(household_select().where(h.c.id.in_(ids), h.c.deleted_at.is_(None)))).all() if ids else []


def integrity(exc: IntegrityError) -> AppError:
    return AppError("VALIDATION_FAILED", "Data violates a database rule", fields=[{"path": "", "code": str(exc.orig)[:120]}])
