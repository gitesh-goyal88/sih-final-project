"""Households, patients (demographics), volunteer profile, facility registry (API-Guide §6.2, §6.7, §6.8.1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import Field
from sqlalchemy import and_, func, insert, or_, select, update

from app.core import paging
from app.core.crypto import encrypt
from app.core.db import T
from app.core.errors import AppError
from app.core.rbac import Principal, can_access_patient, policy
from app.core.shapes import FacilityOut, HouseholdOut, In, PatientCohortOut, PatientOut
from app.core.uow import CommitThenRaise, uow
from app.modules.onboarding import service as svc
from app.modules.reference import serial

router = APIRouter(tags=["onboarding"])
CC = "continuity_of_care"


# ---------------- households ----------------

class HouseholdCreateIn(In):
    id: uuid.UUID
    village_id: uuid.UUID
    house_number: str | None = Field(default=None, max_length=40)
    location: dict[str, Any] | None = None
    location_source: Literal["gps", "map_pin", "village"] | None = None
    registered_phone: str | None = None
    head_member_id: uuid.UUID | None = None
    members: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    consents: list[dict[str, Any]] = Field(default_factory=list, max_length=60)
    recorded_at: datetime | None = None


async def _household_detail(conn: Any, hid: Any, projection: str = "asha") -> dict[str, Any]:
    rows = await svc.load_households(conn, [hid])
    if not rows:
        raise AppError("NOT_FOUND")
    return (await svc.households_out(conn, rows, with_members=True, projection=projection))[0]


@router.post("/households", response_model=HouseholdOut, status_code=201)
async def create_household(body: HouseholdCreateIn, p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    data = body.model_dump(by_alias=True, mode="json", exclude_none=True)
    async with uow(p) as tx:
        hid = await svc.create_household(tx, p, data)
        return await _household_detail(tx.conn, hid)


@router.get("/households")
async def list_households(villageId: uuid.UUID | None = None, q: str | None = Query(default=None, max_length=40),  # noqa: N803
                          limit: int = 50, cursor: str | None = None,
                          p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    h, pt = T.households, T.patients
    villages = [str(villageId)] if villageId else p.villages
    if villageId and str(villageId) not in p.villages:
        raise AppError("FORBIDDEN_SCOPE")
    n = paging.limit(limit)
    cond = and_(h.c.village_id.in_(villages or ["00000000-0000-0000-0000-000000000000"]), h.c.deleted_at.is_(None))
    if q:
        # name search runs on the device (names are encrypted on the server — schema R8, D14)
        cond = and_(cond, or_(h.c.house_number == q.strip(), h.c.id.in_(
            select(pt.c.household_id).where(pt.c.short_code == q.strip().upper()))))
    c = paging.decode(cursor)
    page_cond = and_(cond, h.c.id > c["id"]) if c else cond
    async with uow(p) as tx:
        rows = (await tx.conn.execute(svc.household_select().where(page_cond).order_by(h.c.id).limit(n + 1))).all()
        total = (await tx.conn.execute(select(func.count()).select_from(h).where(cond))).scalar()
        data = await svc.households_out(tx.conn, rows[:n], with_members=True)
        await tx.audit("household.list", "household", None, purpose=CC, diff={"count": len(data)})
    next_cursor = paging.encode({"id": str(rows[n - 1].id)}) if len(rows) > n else None
    return {"data": [HouseholdOut.model_validate(d).model_dump(by_alias=True) for d in data],
            "nextCursor": next_cursor, "total": total}


@router.get("/households/{household_id}", response_model=HouseholdOut)
async def get_household(household_id: uuid.UUID, response: Response,
                        p: Principal = Depends(policy("asha", "patient", purpose=CC))) -> dict[str, Any]:
    async with uow(p) as tx:
        rows = await svc.load_households(tx.conn, [household_id])
        if not rows:
            raise AppError("NOT_FOUND")
        hh = rows[0]
        if (p.role == "asha" and str(hh.village_id) not in p.villages) or (
                p.role == "patient" and str(hh.id) != p.household_id):
            raise AppError("NOT_FOUND")
        detail = await _household_detail(tx.conn, household_id, "patient" if p.role == "patient" else "asha")
        await tx.audit("household.read", "household", household_id)
    response.headers["ETag"] = f'"{detail["version"]}"'
    return detail


@router.patch("/households/{household_id}", response_model=HouseholdOut)
async def patch_household(household_id: uuid.UUID, body: dict[str, Any], if_match: str | None = Header(default=None),
                          p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    async with uow(p) as tx:
        rows = await svc.load_households(tx.conn, [household_id])
        if not rows or str(rows[0].village_id) not in p.villages:
            raise AppError("NOT_FOUND")
        paging.if_match(if_match, rows[0].version, {"id": str(household_id), "version": rows[0].version})
        await svc.update_household(tx, p, household_id, body, None)
        return await _household_detail(tx.conn, household_id)


@router.post("/households/{household_id}/members", response_model=PatientOut, status_code=201)
async def add_member(household_id: uuid.UUID, body: dict[str, Any], p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    allowed = svc.PATIENT_WRITABLE | {"id", "cohorts", "recordedAt"}
    extra = set(body) - allowed
    if extra:
        raise AppError("FIELD_NOT_WRITABLE", f"Not writable: {sorted(extra)}")
    async with uow(p) as tx:
        pid = await svc.create_patient(tx, p, household_id, body)
        return (await svc.patients_out(tx.conn, await svc.patient_rows(tx.conn, [pid])))[0]


# ---------------- patients ----------------

async def _patient_or_404(tx: Any, p: Principal, patient_id: Any) -> Any:
    rows = await svc.patient_rows(tx.conn, [patient_id])
    if not rows or not await can_access_patient(tx.conn, p, patient_id):
        raise AppError("NOT_FOUND")
    return rows[0]


@router.patch("/patients/{patient_id}", response_model=PatientOut)
async def patch_patient(patient_id: uuid.UUID, body: dict[str, Any], if_match: str | None = Header(default=None),
                        p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    async with uow(p) as tx:
        row = await _patient_or_404(tx, p, patient_id)
        paging.if_match(if_match, row.version, {"id": str(patient_id), "version": row.version})
        await svc.update_patient(tx, p, patient_id, body, None)
        return (await svc.patients_out(tx.conn, await svc.patient_rows(tx.conn, [patient_id])))[0]


class PatientCommandIn(In):
    command: Literal["MovePatient", "MarkDeceased"]
    args: dict[str, Any] = Field(default_factory=dict)


@router.post("/patients/{patient_id}/commands", response_model=PatientOut)
async def patient_command(patient_id: uuid.UUID, body: PatientCommandIn,
                          p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    async with uow(p) as tx:
        await _patient_or_404(tx, p, patient_id)
        await svc.patient_command(tx, p, patient_id, body.command, body.args)
        return (await svc.patients_out(tx.conn, await svc.patient_rows(tx.conn, [patient_id])))[0]


@router.post("/patients/{patient_id}/cohorts", response_model=PatientCohortOut, status_code=201)
async def add_cohort(patient_id: uuid.UUID, body: dict[str, Any], p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    extra = set(body) - (svc.COHORT_WRITABLE | {"id", "recordedAt"})
    if extra:
        raise AppError("FIELD_NOT_WRITABLE", f"Not writable: {sorted(extra)}")
    async with uow(p) as tx:
        await _patient_or_404(tx, p, patient_id)
        cid = await svc.start_cohort(tx, p, patient_id, body)
        row = (await tx.conn.execute(select(T.patient_cohorts).where(T.patient_cohorts.c.id == cid))).first()
    return {"id": row.id, "cohort": row.cohort, "startedOn": row.started_on, "endedOn": row.ended_on,
            "lmpDate": row.lmp_date, "eddDate": row.edd_date}


@router.patch("/patients/{patient_id}/cohorts/{cohort_id}", response_model=PatientCohortOut)
async def end_cohort(patient_id: uuid.UUID, cohort_id: uuid.UUID, body: dict[str, Any],
                     p: Principal = Depends(policy("asha", purpose=CC))) -> dict[str, Any]:
    async with uow(p) as tx:
        await _patient_or_404(tx, p, patient_id)
        await svc.end_cohort(tx, p, patient_id, cohort_id, body)
        row = (await tx.conn.execute(select(T.patient_cohorts).where(T.patient_cohorts.c.id == cohort_id))).first()
    return {"id": row.id, "cohort": row.cohort, "startedOn": row.started_on, "endedOn": row.ended_on,
            "lmpDate": row.lmp_date, "eddDate": row.edd_date}


@router.post("/patients/{patient_id}/conditions", status_code=201)
async def add_condition(patient_id: uuid.UUID, body: dict[str, Any],
                        p: Principal = Depends(policy("asha", "doctor", purpose=CC))) -> dict[str, Any]:
    async with uow(p) as tx:
        await _patient_or_404(tx, p, patient_id)
        cid = await svc.add_condition(tx, p, patient_id, body)
    return {"id": cid}


@router.post("/patients/{patient_id}/medications", status_code=201)
async def add_medication(patient_id: uuid.UUID, body: dict[str, Any],
                         p: Principal = Depends(policy("asha", "doctor", purpose=CC))) -> dict[str, Any]:
    async with uow(p) as tx:
        await _patient_or_404(tx, p, patient_id)
        mid = await svc.add_medication(tx, p, patient_id, body)
    return {"id": mid}


@router.get("/patients/by-code/{short_code}", response_model=PatientOut)
async def patient_by_code(short_code: str, x_break_glass: str | None = Header(default=None),
                          p: Principal = Depends(policy("doctor", "facility_staff", "district_admin", purpose=CC))) -> dict[str, Any]:
    pt = T.patients
    async with uow(p) as tx:
        row = (await tx.conn.execute(select(pt).where(pt.c.short_code == short_code.upper(), pt.c.deleted_at.is_(None)))).first()
        if row is None or not await can_access_patient(tx.conn, p, row.id, grant_id=x_break_glass):
            await tx.audit("patient.lookup", "patient", None, outcome="denied")
            raise CommitThenRaise(AppError("NOT_FOUND"))
        await tx.audit("patient.read", "patient", row.id, patient_id=row.id)
        return (await svc.patients_out(tx.conn, [row], "facility"))[0]


# ---------------- volunteer profile & vehicles ----------------

@router.get("/volunteers/me")
async def volunteer_me(p: Principal = Depends(policy("volunteer", purpose="administration", allow_pending=True))) -> dict[str, Any]:
    vp, v = T.volunteer_profiles, T.vehicles
    async with uow(p) as tx:
        prof = (await tx.conn.execute(select(vp).where(vp.c.user_id == p.user_id))).first()
        vehicles = (await tx.conn.execute(select(v).where(v.c.owner_user_id == p.user_id, v.c.deleted_at.is_(None)))).all()
    if prof is None:
        raise AppError("NOT_FOUND", "Volunteer profile not set up")
    return {"profile": {"homeVillageId": prof.home_village_id, "available": prof.available,
                        "verified": prof.verified_at is not None, "firstAidTrained": prof.first_aid_trained},
            "vehicles": [{"id": x.id, "kind": x.kind, "seats": x.seats, "active": x.active, "version": x.version}
                         for x in vehicles]}


class VehicleIn(In):
    id: uuid.UUID
    kind: Literal["bike", "auto", "car", "tractor", "jeep", "ambulance"]
    seats: int | None = Field(default=None, ge=1, le=20)
    registration: str | None = Field(default=None, max_length=20)


@router.post("/volunteers/me/vehicles", status_code=201)
async def add_vehicle(body: VehicleIn, p: Principal = Depends(policy("volunteer", purpose="administration", allow_pending=True))) -> dict[str, Any]:
    async with uow(p) as tx:
        if (await tx.conn.execute(select(T.vehicles.c.id).where(T.vehicles.c.id == body.id))).first() is None:
            home = p.home_village_id
            await tx.conn.execute(insert(T.vehicles).values(
                id=body.id, owner_user_id=p.user_id, village_id=home, kind=body.kind, seats=body.seats,
                registration_enc=await encrypt(tx.conn, "user", p.user_id, "vehicles", "registration_enc", body.id,
                                               body.registration) if body.registration else None))
            await tx.journal("vehicle", body.id, [f"user:{p.user_id}"])
            await tx.audit("vehicle.create", "vehicle", body.id)
    return {"id": body.id, "kind": body.kind, "seats": body.seats, "active": True}


class VehiclePatchIn(In):
    active: bool


@router.patch("/volunteers/me/vehicles/{vehicle_id}")
async def patch_vehicle(vehicle_id: uuid.UUID, body: VehiclePatchIn,
                        p: Principal = Depends(policy("volunteer", purpose="administration", allow_pending=True))) -> dict[str, Any]:
    v = T.vehicles
    async with uow(p) as tx:
        res = await tx.conn.execute(update(v).where(and_(v.c.id == vehicle_id, v.c.owner_user_id == p.user_id))
                                    .values(active=body.active))
        if not res.rowcount:
            raise AppError("NOT_FOUND")
        await tx.journal("vehicle", vehicle_id, [f"user:{p.user_id}"])
        await tx.audit("vehicle.update", "vehicle", vehicle_id, diff={"active": body.active})
    return {"id": vehicle_id, "active": body.active}


# ---------------- facility registry & capability panel ----------------

@router.get("/facilities/{facility_id}", response_model=FacilityOut)
async def get_facility(facility_id: uuid.UUID, response: Response,
                       p: Principal = Depends(policy("asha", "doctor", "facility_staff", "district_admin",
                                                     purpose="emergency_care"))) -> dict[str, Any]:
    async with uow(p) as tx:
        rows = await serial.facilities(tx.conn, [facility_id])
    if not rows:
        raise AppError("NOT_FOUND")
    response.headers["ETag"] = f'"{rows[0]["version"]}"'
    return rows[0]


class FacilityPatchIn(In):
    beds_available: int | None = Field(default=None, ge=0, le=5000)
    beds_total: int | None = Field(default=None, ge=0, le=5000)
    status: Literal["open", "full", "closed"] | None = None
    status_note: str | None = Field(default=None, max_length=200)
    capabilities: dict[str, bool] | None = None
    confirm_all: bool = False
    reason: str | None = Field(default=None, max_length=300)


@router.patch("/facilities/{facility_id}", response_model=FacilityOut)
async def patch_facility(facility_id: uuid.UUID, body: FacilityPatchIn, if_match: str | None = Header(default=None),
                         p: Principal = Depends(policy("facility_staff", "district_admin", purpose="administration"))) -> dict[str, Any]:
    from app.modules.comms.notify import ws_publish
    from app.modules.referral.matching import refresh_facility_cache

    f, fc = T.facilities, T.facility_capabilities
    async with uow(p) as tx:
        fac = (await tx.conn.execute(select(f).where(f.c.id == facility_id, f.c.deleted_at.is_(None)).with_for_update())).first()
        if fac is None:
            raise AppError("NOT_FOUND")
        if p.role == "facility_staff" and str(facility_id) not in p.facility_ids:
            raise AppError("FORBIDDEN_SCOPE", "Not your facility")
        if p.role == "district_admin":
            if fac.district_code != p.district_code:
                raise AppError("FORBIDDEN_SCOPE")
            if not body.reason or len(body.reason.strip()) < 10:
                raise AppError("VALIDATION_FAILED", "reason (≥ 10 chars) required for admin edits",
                               fields=[{"path": "reason", "code": "missing"}])
        paging.if_match(if_match, fac.version, {"id": str(facility_id), "version": fac.version})
        now = datetime.now(UTC)
        values: dict[str, Any] = {"capability_updated_at": now, "capability_updated_by": p.user_id}
        for field in ("beds_available", "beds_total", "status", "status_note"):
            val = getattr(body, field)
            if val is not None:
                values[field] = val
        changed_caps: list[str] = []
        if body.capabilities:
            declared = {r.capability_code for r in (await tx.conn.execute(select(fc.c.capability_code).where(fc.c.facility_id == facility_id))).all()}
            unknown = set(body.capabilities) - declared
            if unknown:
                raise AppError("VALIDATION_FAILED", "Only declared capabilities can be toggled; declaring is an admin action",
                               fields=[{"path": f"capabilities.{c}", "code": "not_declared"} for c in sorted(unknown)])
            for code, avail in body.capabilities.items():
                await tx.conn.execute(update(fc).where(and_(fc.c.facility_id == facility_id, fc.c.capability_code == code))
                                      .values(available=avail, updated_at=now, updated_by=p.user_id))
                changed_caps.append(code)
        await tx.conn.execute(update(f).where(f.c.id == facility_id).values(**values))
        scopes = [f"district:{fac.district_code}"] + ([f"block:{fac.block_code}"] if fac.block_code else [])
        await tx.journal("facility", facility_id, scopes)
        for code in changed_caps:
            await tx.journal("facility_capability", f"{facility_id}:{code}", scopes)
        await tx.audit("facility.capability_update", "facility", facility_id,
                       diff={"fields": sorted(k for k in values if not k.startswith("capability_")),
                             "capabilities": body.capabilities, "confirmAll": body.confirm_all, "reason": body.reason})
        out = (await serial.facilities(tx.conn, [facility_id]))[0]

        async def _after() -> None:
            await refresh_facility_cache(facility_id)
            await ws_publish(f"district:{fac.district_code}", {"type": "facility.updated", "facilityId": str(facility_id),
                                                               "status": out["status"], "bedsAvailable": out["bedsAvailable"],
                                                               "stale": out["stale"]})
            await ws_publish(f"facility:{facility_id}", {"type": "facility.updated", "facilityId": str(facility_id),
                                                         "status": out["status"], "bedsAvailable": out["bedsAvailable"],
                                                         "stale": out["stale"]})
        tx.after_commit(_after)
    return out


