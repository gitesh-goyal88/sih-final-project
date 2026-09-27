"""District administration (API-Guide §6.11, UI-UX §7.4, TRD §14.2)."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import Field
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core import cfg
from app.core import redis as rds
from app.core.crypto import encrypt, normalise_phone, phone_hash
from app.core.db import T, lat_of, lng_of, point
from app.core.errors import AppError
from app.core.ids import uuid7
from app.core.rbac import Principal, invalidate_scope, policy
from app.core.shapes import CaseOut, EscalationOut, In
from app.core.uow import uow
from app.modules.referral import cascade, cases, core

router = APIRouter(prefix="/admin", tags=["admin"])
ADMIN = "district_admin"


def _reason(r: str | None) -> str:
    if not r or len(r.strip()) < 10:
        raise AppError("VALIDATION_FAILED", "reason (≥ 10 chars) is required", fields=[{"path": "reason", "code": "missing"}])
    return r.strip()


@router.get("/dashboard")
async def dashboard(districtCode: str | None = None, p: Principal = Depends(policy(ADMIN, purpose="programme_reporting"))) -> dict[str, Any]:  # noqa: N803
    """Exactly the four UI §7.4 numbers + live stuck list + facility status + weekly aggregates."""
    d = p.district_code
    if districtCode and districtCode != d:
        raise AppError("FORBIDDEN_SCOPE")
    c, f, e = T.cases, T.facilities, T.case_escalations
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    async with uow(p) as tx:
        open_rows = (await tx.conn.execute(select(c.c.status, c.c.type, func.count()).where(
            c.c.district_code == d, c.c.status.in_(core.OPEN)).group_by(c.c.status, c.c.type))).all()
        by_status: dict[str, int] = {s: 0 for s in core.OPEN}
        open_sos = referrals_waiting = 0
        for status, ctype, n in open_rows:
            by_status[status] += n
            if ctype == "sos":
                open_sos += n
            elif status in ("created", "matched"):
                referrals_waiting += n
        closed_today = (await tx.conn.execute(select(func.count()).select_from(c).where(
            c.c.district_code == d, c.c.closed_at >= today))).scalar()
        conf = await cfg.merged(tx.conn, d)
        stale_cut = datetime.now(UTC) - timedelta(hours=float(conf.get("matching.stale_after_h", 12)))
        not_updated = (await tx.conn.execute(select(func.count()).select_from(f).where(
            f.c.district_code == d, f.c.deleted_at.is_(None), f.c.capability_updated_at < stale_cut))).scalar()
        stuck = [{"caseId": r.case_id, "shortCode": r.short_code, "reason": r.reason, "level": r.level,
                  "ageS": int((datetime.now(UTC) - r.raised_at).total_seconds())}
                 for r in (await tx.conn.execute(select(e.c.case_id, e.c.reason, e.c.level, e.c.raised_at, c.c.short_code)
                                                 .join(c, c.c.id == e.c.case_id).where(e.c.district_code == d,
                                                                                       e.c.resolved_at.is_(None))
                                                 .order_by(e.c.level.desc(), e.c.raised_at).limit(50))).all()]
        facilities = [{"id": r.id, "name": r.name, "status": r.status, "bedsAvailable": r.beds_available,
                       "stale": r.capability_updated_at < stale_cut}
                      for r in (await tx.conn.execute(select(f.c.id, f.c.name, f.c.status, f.c.beds_available,
                                                             f.c.capability_updated_at).where(
                          f.c.district_code == d, f.c.deleted_at.is_(None)).order_by(f.c.name))).all()]
        week = datetime.now(UTC) - timedelta(days=7)
        w = (await tx.conn.execute(select(
            func.percentile_cont(0.5).within_group(func.extract("epoch", c.c.accepted_at - c.c.created_at)),
            func.percentile_cont(0.5).within_group(func.extract("epoch", c.c.arrived_at - c.c.created_at) / 60.0),
        ).where(c.c.district_code == d, c.c.created_at >= week, c.c.type == "sos"))).first()
        arrived = (await tx.conn.execute(select(func.count()).select_from(c).where(
            c.c.district_code == d, c.c.arrived_at >= week))).scalar() or 0
        closed72 = (await tx.conn.execute(select(func.count()).select_from(c).where(
            c.c.district_code == d, c.c.arrived_at >= week, c.c.closed_at <= c.c.arrived_at + timedelta(hours=72)))).scalar() or 0
        await tx.audit("dashboard.read", "district", d, purpose="programme_reporting")
    return {"districtCode": d, "asOf": datetime.now(UTC),
            "tiles": {"openEmergencies": open_sos, "referralsWaiting": referrals_waiting,
                      "casesClosedToday": closed_today, "facilitiesNotUpdated": not_updated},
            "openByStatus": by_status, "stuck": stuck, "facilities": facilities,
            "weekly": {"medianSosToAcceptS": round(w[0]) if w and w[0] is not None else None,
                       "medianSosToArrivalMin": round(w[1]) if w and w[1] is not None else None,
                       "closedWithin72hPct": round(100.0 * closed72 / arrived, 1) if arrived else None}}


@router.get("/map/cases")
async def map_cases(status: str = "open", p: Principal = Depends(policy(ADMIN, purpose="programme_reporting"))) -> dict[str, Any]:
    """Open cases for the district map; point rounded to ~100 m (API-Guide §6.11)."""
    oc = T.v_open_cases
    async with uow(p) as tx:
        rows = (await tx.conn.execute(select(oc.c.id, oc.c.short_code, oc.c.status, oc.c.emergency_category,
                                             oc.c.escalation_level, oc.c.status_changed_at, oc.c.type, oc.c.verified,
                                             lat_of(oc.c.pickup_point).label("lat"), lng_of(oc.c.pickup_point).label("lng"))
                                      .where(oc.c.district_code == p.district_code))).all()
    return {"data": [{"caseId": r.id, "shortCode": r.short_code, "status": r.status, "type": r.type,
                      "category": r.emergency_category, "verified": r.verified,
                      "point": {"lat": round(float(r.lat), 3), "lng": round(float(r.lng), 3)} if r.lat is not None else None,
                      "escalationLevel": r.escalation_level,
                      "ageS": int((datetime.now(UTC) - r.status_changed_at).total_seconds())} for r in rows]}


@router.get("/escalations")
async def escalations(status: Literal["open", "all"] = "open", p: Principal = Depends(policy(ADMIN, purpose="emergency_care"))) -> dict[str, Any]:
    e = T.case_escalations
    cond = e.c.district_code == p.district_code
    if status == "open":
        cond = and_(cond, e.c.resolved_at.is_(None))
    out = []
    async with uow(p) as tx:
        for r in (await tx.conn.execute(select(e).where(cond).order_by(e.c.level.desc(), e.c.raised_at).limit(200))).all():
            case = await core.load_case(tx, r.case_id)
            out.append({**EscalationOut.model_validate({"id": r.id, "caseId": r.case_id, "level": r.level, "reason": r.reason,
                                                        "raisedAt": r.raised_at, "acknowledgedAt": r.acknowledged_at,
                                                        "resolvedAt": r.resolved_at, "resolution": r.resolution}).model_dump(by_alias=True),
                        "note": r.note,
                        "case": CaseOut.model_validate(await cases.case_out(tx.conn, case, "admin")).model_dump(by_alias=True)})
    return {"data": out}


@router.post("/escalations/{escalation_id}/acknowledge", status_code=204)
async def acknowledge(escalation_id: uuid.UUID, p: Principal = Depends(policy(ADMIN, purpose="emergency_care"))) -> None:
    e = T.case_escalations
    async with uow(p) as tx:
        row = (await tx.conn.execute(select(e).where(e.c.id == escalation_id, e.c.district_code == p.district_code))).first()
        if row is None:
            raise AppError("NOT_FOUND")
        if row.acknowledged_at is None:
            await tx.conn.execute(update(e).where(e.c.id == escalation_id).values(
                acknowledged_at=datetime.now(UTC), assigned_to=p.user_id, assigned_role="district_admin"))
            await core.event(tx, row.case_id, "escalation_acknowledged", {"escalationId": str(escalation_id)})


class ResolveIn(In):
    resolution: Literal["facility_reassigned", "transport_arranged", "called_family", "false_alarm", "closed_by_system", "other"]
    note: str | None = Field(default=None, max_length=500)


@router.post("/escalations/{escalation_id}/resolve", response_model=EscalationOut)
async def resolve(escalation_id: uuid.UUID, body: ResolveIn, p: Principal = Depends(policy(ADMIN, purpose="emergency_care"))) -> dict[str, Any]:
    e = T.case_escalations
    async with uow(p) as tx:
        row = (await tx.conn.execute(select(e).where(e.c.id == escalation_id, e.c.district_code == p.district_code))).first()
        if row is None:
            raise AppError("NOT_FOUND")
        if row.resolved_at is None:
            now = datetime.now(UTC)
            await tx.conn.execute(update(e).where(e.c.id == escalation_id).values(
                resolved_at=now, resolution=body.resolution, note=body.note, acknowledged_at=row.acknowledged_at or now))
            await core.event(tx, row.case_id, "escalation_resolved", {"escalationId": str(escalation_id),
                                                                      "resolution": body.resolution})
            if row.reason == "unverified_sms":
                from app.modules.referral.commands import _release_dispatch

                case = await core.load_case(tx, row.case_id)
                await _release_dispatch(tx, case)
        r = (await tx.conn.execute(select(e).where(e.c.id == escalation_id))).first()
    return {"id": r.id, "caseId": r.case_id, "level": r.level, "reason": r.reason, "raisedAt": r.raised_at,
            "acknowledgedAt": r.acknowledged_at, "resolvedAt": r.resolved_at, "resolution": r.resolution}


class ReassignIn(In):
    facility_id: uuid.UUID
    reason: str


@router.post("/cases/{case_id}/reassign")
async def reassign(case_id: uuid.UUID, body: ReassignIn, p: Principal = Depends(policy(ADMIN, purpose="emergency_care"))) -> dict[str, Any]:
    reason = _reason(body.reason)
    async with rds.case_lock(str(case_id)):
        async with uow(p) as tx:
            case = await core.require_case(tx, case_id)
            if case.district_code != p.district_code:
                raise AppError("NOT_FOUND")
            oid = await cascade.admin_reassign(tx, case, body.facility_id, reason)
            await core.resolve_escalations(tx, case_id, "facility_reassigned", ("cascade_exhausted", "no_capable_facility"))
    return {"offerId": oid}


class ForceHandoverIn(In):
    reason: str


@router.post("/cases/{case_id}/legs/{leg_id}/force-handover")
async def force_handover(case_id: uuid.UUID, leg_id: uuid.UUID, body: ForceHandoverIn,
                         p: Principal = Depends(policy(ADMIN, purpose="emergency_care"))) -> dict[str, Any]:
    """Only after HANDOVER_LOCKED (API-Guide §6.8.5); reason is highlighted in the audit report."""
    reason = _reason(body.reason)
    from app.modules.transport import legs as tlegs

    tl = T.transport_legs
    async with rds.case_lock(str(case_id)):
        async with uow(p) as tx:
            case = await core.require_case(tx, case_id)
            leg = await tlegs.load_leg(tx.conn, leg_id, lock=True)
            if case.district_code != p.district_code or leg is None or leg.case_id != case.id:
                raise AppError("NOT_FOUND")
            if leg.handover_attempts < tlegs.HANDOVER_MAX_ATTEMPTS or leg.status != "picked_up":
                raise AppError("CASE_STATE_CONFLICT", "Force handover is only for a locked handover")
            nxt = (await tx.conn.execute(select(tl.c.id, tl.c.status).where(and_(tl.c.case_id == case.id,
                                                                                  tl.c.leg_order == leg.leg_order + 1)))).first()
            if nxt is None or nxt.status != "accepted":
                raise AppError("CASE_STATE_CONFLICT", "Next custodian not assigned")
            now = datetime.now(UTC)
            await tx.conn.execute(update(tl).where(tl.c.id == leg.id).values(status="handed_over", handed_over_at=now,
                                                                             handover_to_leg_id=nxt.id))
            await tx.conn.execute(update(tl).where(tl.c.id == nxt.id).values(status="picked_up", picked_up_at=now))
            await core.touch_case(tx, case, {"current_leg_id": nxt.id})
            await core.event(tx, case.id, "custody_handover", {"fromLeg": leg.leg_order, "toLeg": leg.leg_order + 1,
                                                               "method": "admin_force"}, leg_id=leg.id)
            await tx.audit("leg.force_handover", "transport_leg", leg.id, patient_id=case.patient_id,
                           diff={"reason": reason, "privileged": True})
    return {"leg": {"id": leg_id, "status": "handed_over"}, "nextLeg": {"id": nxt.id, "status": "picked_up"}}


@router.get("/facilities/stale")
async def stale_facilities(p: Principal = Depends(policy(ADMIN, purpose="administration"))) -> dict[str, Any]:
    from app.modules.reference import serial

    async with uow(p) as tx:
        data = [f for f in await serial.facilities(tx.conn, district_code=p.district_code) if f["stale"]]
    return {"data": data}


class FacilityCreateIn(In):
    id: uuid.UUID
    name: str = Field(max_length=120)
    level: Literal["SC", "PHC", "CHC", "SDH", "DH", "MC", "private"]
    ownership: Literal["public", "private", "ngo"] = "public"
    block_code: str | None = None
    location: dict[str, float]
    beds_total: int | None = Field(default=None, ge=0)
    beds_available: int = Field(default=0, ge=0)
    duty_phone: str | None = None
    capabilities: list[str] = Field(default_factory=list)


@router.post("/facilities", status_code=201)
async def create_facility(body: FacilityCreateIn, p: Principal = Depends(policy(ADMIN, purpose="administration"))) -> dict[str, Any]:
    f, fc = T.facilities, T.facility_capabilities
    async with uow(p) as tx:
        await tx.conn.execute(insert(f).values(
            id=body.id, name=body.name, level=body.level, ownership=body.ownership, district_code=p.district_code,
            block_code=body.block_code, location=point(body.location["lat"], body.location["lng"]),
            beds_total=body.beds_total, beds_available=body.beds_available, capability_updated_by=p.user_id,
            duty_phone_enc=await encrypt(tx.conn, "external_contact", body.id, "facilities", "duty_phone_enc", body.id,
                                         normalise_phone(body.duty_phone)) if body.duty_phone else None,
            duty_phone_hash=phone_hash(body.duty_phone) if body.duty_phone else None))
        for code in body.capabilities:
            await tx.conn.execute(insert(fc).values(facility_id=body.id, capability_code=code, updated_by=p.user_id))
        await tx.journal("facility", body.id, [f"district:{p.district_code}"] + ([f"block:{body.block_code}"] if body.block_code else []))
        await tx.audit("facility.create", "facility", body.id, purpose="administration")
    from app.modules.referral.matching import refresh_facility_cache

    await refresh_facility_cache(body.id)
    return {"id": body.id}


class CapabilityDeclareIn(In):
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    reason: str


@router.post("/facilities/{facility_id}/capabilities")
async def declare_capabilities(facility_id: uuid.UUID, body: CapabilityDeclareIn,
                               p: Principal = Depends(policy(ADMIN, purpose="administration"))) -> dict[str, Any]:
    reason = _reason(body.reason)
    f, fc = T.facilities, T.facility_capabilities
    async with uow(p) as tx:
        fac = (await tx.conn.execute(select(f).where(f.c.id == facility_id, f.c.district_code == p.district_code))).first()
        if fac is None:
            raise AppError("NOT_FOUND")
        for code in body.add:
            await tx.conn.execute(pg_insert(fc).values(facility_id=facility_id, capability_code=code, updated_by=p.user_id)
                                  .on_conflict_do_nothing())
        if body.remove:
            from sqlalchemy import delete

            await tx.conn.execute(delete(fc).where(and_(fc.c.facility_id == facility_id, fc.c.capability_code.in_(body.remove))))
        await tx.conn.execute(update(f).where(f.c.id == facility_id).values(capability_updated_at=datetime.now(UTC),
                                                                            capability_updated_by=p.user_id))
        await tx.journal("facility", facility_id, [f"district:{fac.district_code}"])
        await tx.audit("facility.capabilities_declared", "facility", facility_id,
                       diff={"add": body.add, "remove": body.remove, "reason": reason, "privileged": True})
    from app.modules.referral.matching import refresh_facility_cache

    await refresh_facility_cache(facility_id)
    return {"id": facility_id}


# ---------------- staff & volunteers ----------------

class UserCreateIn(In):
    id: uuid.UUID
    role: Literal["asha", "doctor", "facility_staff", "district_admin"]
    staff_id: str = Field(max_length=40)
    name: str = Field(max_length=80)
    phone: str
    district_code: str | None = None
    village_ids: list[uuid.UUID] = Field(default_factory=list)
    facility_id: uuid.UUID | None = None
    preferred_language: str = "hi"


async def _create_user(tx: Any, p: Principal, body: UserCreateIn) -> uuid.UUID:
    u = T.users
    phone = normalise_phone(body.phone)
    now = datetime.now(UTC)
    await tx.conn.execute(insert(u).values(
        id=body.id, role=body.role, status="active", staff_id=body.staff_id,
        name_enc=await encrypt(tx.conn, "user", body.id, "users", "name_enc", body.id, body.name),
        phone_enc=await encrypt(tx.conn, "user", body.id, "users", "phone_enc", body.id, phone),
        phone_hash=phone_hash(phone), preferred_language=body.preferred_language,
        district_code=body.district_code or p.district_code, approved_by=p.user_id, approved_at=now))
    for vid in body.village_ids:
        if body.role != "asha":
            raise AppError("VALIDATION_FAILED", "Villages are assigned to ASHAs only")
        await tx.conn.execute(insert(T.asha_village_assignments).values(asha_id=body.id, village_id=vid, assigned_by=p.user_id))
    if body.facility_id:
        if body.role not in ("doctor", "facility_staff"):
            raise AppError("VALIDATION_FAILED", "Facility membership is for doctors and facility staff")
        await tx.conn.execute(insert(T.facility_memberships).values(user_id=body.id, user_role=body.role,
                                                                    facility_id=body.facility_id))
    await tx.audit("user.provision", "user", body.id, diff={"role": body.role, "privileged": True}, purpose="administration")
    return body.id


@router.post("/users", status_code=201)
async def create_user(body: UserCreateIn, p: Principal = Depends(policy(ADMIN, purpose="administration"))) -> dict[str, Any]:
    """Staff are pre-provisioned by the district admin only — no self sign-up (SEC-ID-07)."""
    async with uow(p) as tx:
        uid = await _create_user(tx, p, body)
    return {"id": uid}


@router.post("/users/import", status_code=202)
async def import_users(file: UploadFile = File(...), p: Principal = Depends(policy(ADMIN, purpose="administration"))) -> dict[str, Any]:
    """Bulk CSV (≤ 1 MB): role,staffId,name,phone,villageIds(;-separated),facilityId. Processed inline at pilot scale."""
    raw = await file.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise AppError("UPLOAD_TOO_LARGE")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    job = uuid7()
    ok, errors = 0, []
    for i, row in enumerate(reader, start=2):
        try:
            body = UserCreateIn(id=uuid7(), role=row["role"].strip(), staff_id=row["staffId"].strip(), name=row["name"].strip(),
                                phone=row["phone"].strip(),
                                village_ids=[uuid.UUID(v) for v in (row.get("villageIds") or "").split(";") if v.strip()],
                                facility_id=uuid.UUID(row["facilityId"]) if row.get("facilityId") else None)
            async with uow(p) as tx:
                await _create_user(tx, p, body)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"line": i, "error": type(exc).__name__})
    await rds.r().set(rds.k(f"job:{job}"), __import__("json").dumps({"created": ok, "errors": errors}), ex=86400)
    return {"jobId": job}


@router.get("/jobs/{job_id}")
async def job(job_id: uuid.UUID, p: Principal = Depends(policy(ADMIN, purpose="administration"))) -> dict[str, Any]:
    raw = await rds.r().get(rds.k(f"job:{job_id}"))
    if raw is None:
        raise AppError("NOT_FOUND")
    return __import__("json").loads(raw)


class UserPatchIn(In):
    status: Literal["active", "suspended", "deactivated"] | None = None
    village_ids: list[uuid.UUID] | None = None
    reason: str | None = None


@router.patch("/users/{user_id}")
async def patch_user(user_id: uuid.UUID, body: UserPatchIn, p: Principal = Depends(policy(ADMIN, purpose="administration"))) -> dict[str, Any]:
    u, ava, h = T.users, T.asha_village_assignments, T.households
    async with uow(p) as tx:
        user = (await tx.conn.execute(select(u).where(u.c.id == user_id))).first()
        if user is None or (user.district_code and user.district_code != p.district_code):
            raise AppError("NOT_FOUND")
        if body.status:
            await tx.conn.execute(update(u).where(u.c.id == user_id).values(status=body.status))
            if body.status != "active":
                await tx.conn.execute(update(T.refresh_tokens).where(and_(T.refresh_tokens.c.user_id == user_id,
                                                                          T.refresh_tokens.c.revoked_at.is_(None)))
                                      .values(revoked_at=datetime.now(UTC), revoke_reason="admin"))
        if body.village_ids is not None:
            if user.role != "asha":
                raise AppError("VALIDATION_FAILED", "Only ASHAs have villages")
            current = {str(r.village_id) for r in (await tx.conn.execute(select(ava.c.village_id).where(
                ava.c.asha_id == user_id, ava.c.assigned_to.is_(None)))).all()}
            wanted = {str(v) for v in body.village_ids}
            removed = current - wanted
            if removed:
                await tx.conn.execute(update(ava).where(and_(ava.c.asha_id == user_id, ava.c.assigned_to.is_(None),
                                                             ava.c.village_id.in_(removed))).values(assigned_to=date.today()))
                # SEC-AZ-04: tombstones remove the villages' data from her device at the next sync
                for (hid,) in (await tx.conn.execute(select(h.c.id).where(h.c.village_id.in_(removed)))).all():
                    await tx.journal("household", hid, [f"user:{user_id}"], op="delete")
                pt = T.patients
                for (pid,) in (await tx.conn.execute(select(pt.c.id).join(h, h.c.id == pt.c.household_id)
                                                     .where(h.c.village_id.in_(removed)))).all():
                    await tx.journal("patient", pid, [f"user:{user_id}"], op="delete")
            for vid in wanted - current:
                await tx.conn.execute(pg_insert(ava).values(asha_id=user_id, village_id=vid, assigned_by=p.user_id)
                                      .on_conflict_do_nothing())
        await tx.audit("user.update", "user", user_id, diff={"status": body.status, "villages": body.village_ids is not None,
                                                            "reason": body.reason, "privileged": True}, purpose="administration")
    await invalidate_scope(user_id)
    return {"id": user_id}


class VerifyVolunteerIn(In):
    method: Literal["asha_endorsement", "id_check"]


@router.post("/volunteers/{volunteer_id}/verify")
async def verify_volunteer(volunteer_id: uuid.UUID, body: VerifyVolunteerIn,
                           p: Principal = Depends(policy(ADMIN, "asha", purpose="administration"))) -> dict[str, Any]:
    vp, u = T.volunteer_profiles, T.users
    async with uow(p) as tx:
        prof = (await tx.conn.execute(select(vp).where(vp.c.user_id == volunteer_id))).first()
        if prof is None:
            raise AppError("NOT_FOUND")
        if p.role == "asha" and str(prof.home_village_id) not in p.villages:
            raise AppError("FORBIDDEN_SCOPE", "Volunteer is not from your villages")
        now = datetime.now(UTC)
        await tx.conn.execute(update(vp).where(vp.c.user_id == volunteer_id).values(verified_by=p.user_id, verified_at=now))
        await tx.conn.execute(update(u).where(and_(u.c.id == volunteer_id, u.c.status == "pending_approval"))
                              .values(status="active", approved_by=p.user_id, approved_at=now))
        await tx.journal("volunteer_profile", volunteer_id, [f"user:{volunteer_id}"])
        await tx.audit("volunteer.verify", "user", volunteer_id, diff={"method": body.method})
    await invalidate_scope(volunteer_id)
    return {"id": volunteer_id, "verified": True}


@router.get("/devices")
async def devices(userId: uuid.UUID | None = None, p: Principal = Depends(policy(ADMIN, purpose="security"))) -> dict[str, Any]:  # noqa: N803
    d, u = T.devices, T.users
    q = select(d, u.c.role).join(u, u.c.id == d.c.user_id).where(u.c.district_code == p.district_code)
    if userId:
        q = q.where(d.c.user_id == userId)
    async with uow(p) as tx:
        rows = (await tx.conn.execute(q.order_by(d.c.last_seen_at.desc().nullslast()).limit(200))).all()
    return {"data": [{"id": r.id, "userId": r.user_id, "role": r.role, "platform": r.platform, "model": r.model,
                      "lastSeenAt": r.last_seen_at, "lastSyncAt": r.last_sync_at, "revoked": r.revoked_at is not None,
                      "wipeRequested": r.wipe_requested_at is not None} for r in rows]}


class RevokeIn(In):
    wipe: bool = True
    reason: str


@router.post("/devices/{device_id}/revoke")
async def revoke_device(device_id: uuid.UUID, body: RevokeIn, p: Principal = Depends(policy(ADMIN, purpose="security"))) -> dict[str, Any]:
    from app.modules.identity.service import revoke_device as do_revoke

    reason = _reason(body.reason)
    async with uow(p) as tx:
        d, u = T.devices, T.users
        row = (await tx.conn.execute(select(d.c.id).join(u, u.c.id == d.c.user_id).where(
            d.c.id == device_id, u.c.district_code == p.district_code))).first()
        if row is None:
            raise AppError("NOT_FOUND")
        await do_revoke(tx, device_id, "device_revoked", body.wipe)
        await tx.audit("device.revoke", "device", device_id, diff={"wipe": body.wipe, "reason": reason, "privileged": True},
                       purpose="security")
    return {"id": device_id, "revoked": True, "wipe": body.wipe}


@router.get("/config")
async def get_config(p: Principal = Depends(policy(ADMIN, purpose="administration"))) -> dict[str, Any]:
    async with uow(p) as tx:
        return {"districtCode": p.district_code, "config": await cfg.merged(tx.conn, p.district_code)}


class ConfigPutIn(In):
    values: dict[str, Any]
    reason: str


@router.put("/config")
async def put_config(body: ConfigPutIn, p: Principal = Depends(policy(ADMIN, purpose="administration"))) -> dict[str, Any]:
    """District overrides; every change audited; emergency-path keys raise an alert (SECURITY §14.3)."""
    reason = _reason(body.reason)
    ce = T.config_entries
    async with uow(p) as tx:
        known = set((await cfg.merged(tx.conn, None)).keys())
        for key, value in body.values.items():
            if key not in known:
                raise AppError("VALIDATION_FAILED", f"Unknown key {key}")
            if isinstance(value, (int, float)) and value <= 0:
                raise AppError("VALIDATION_FAILED", f"{key} must be positive")
            await tx.conn.execute(pg_insert(ce).values(district_code=p.district_code, key=key, value=value, updated_by=p.user_id)
                                  .on_conflict_do_update(index_elements=["district_code", "key"],
                                                         set_={"value": value, "updated_by": p.user_id}))
            await tx.journal("config", key, [f"district:{p.district_code}"])
        await tx.audit("admin.config_changed", "config", p.district_code, diff={"keys": sorted(body.values), "reason": reason,
                                                                               "privileged": True}, purpose="administration")
    await cfg.invalidate(p.district_code)
    from app.core.logging import security_event

    security_event("admin.config_changed", keys=sorted(body.values), user_id=str(p.user_id))
    async with uow(p) as tx:
        return {"districtCode": p.district_code, "config": await cfg.merged(tx.conn, p.district_code)}


@router.get("/reports/weekly")
async def weekly_report(week: date | None = None, p: Principal = Depends(policy(ADMIN, purpose="programme_reporting"))) -> dict[str, Any]:
    """Aggregates from the materialized views (database.md §19.2)."""
    start = week or (date.today() - timedelta(days=date.today().weekday()))
    kpi, frs = T.mv_district_daily_kpis, T.mv_facility_response_stats
    async with uow(p) as tx:
        days = (await tx.conn.execute(select(kpi).where(kpi.c.district_code == p.district_code,
                                                        kpi.c.day >= start, kpi.c.day < start + timedelta(days=7)))).all()
        fac = (await tx.conn.execute(select(frs, T.facilities.c.name).join(T.facilities, T.facilities.c.id == frs.c.facility_id)
                                     .where(frs.c.week == start, T.facilities.c.district_code == p.district_code))).all()
    return {"weekStart": start,
            "daily": [{"day": r.day, "type": r.type, "cases": r.cases, "offlineChannelCases": r.offline_channel_cases,
                       "medianSToAcceptance": r.median_s_to_acceptance, "medianSToTransport": r.median_s_to_transport,
                       "closureWithin72hRate": float(r.closure_within_72h_rate) if r.closure_within_72h_rate is not None else None,
                       "cancelled": r.cancelled} for r in days],
            "facilities": [{"facilityId": r.facility_id, "name": r.name, "offers": r.offers, "accepted": r.accepted,
                            "declined": r.declined, "timedOut": r.timed_out, "medianResponseS": r.median_response_s}
                           for r in fac]}


