"""SOS, cases, matching, facility inbox, offer responses, arrival/closure (API-Guide §6.4–6.6)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import Field
from sqlalchemy import and_, or_, select, update

from app.core import paging
from app.core import redis as rds
from app.core.crypto import decrypt
from app.core.db import T
from app.core.errors import AppError
from app.core.rbac import Principal, can_access_patient, is_case_participant, policy
from app.core.shapes import (
    CaseDetailOut,
    CaseEventOut,
    CaseOut,
    Category,
    FacilityAdmissionOut,
    FacilityOfferOut,
    GeoPoint,
    In,
    MatchResponse,
    Out,
)
from app.core.uow import uow
from app.modules.referral import cascade, cases, commands, core, matching, status

router = APIRouter(tags=["referral"])
EC = "emergency_care"


# ---------------- /sos ----------------

class SosIn(In):
    case_id: uuid.UUID
    idempotency_key: uuid.UUID
    patient_id: uuid.UUID | None = None
    household_id: uuid.UUID | None = None
    category: Category
    flags: list[Literal["c_section_flag", "spo2_lt_90", "bleeding_heavy"]] = Field(default_factory=list, max_length=5)
    pickup: GeoPoint | None = None
    location_source: Literal["gps", "household", "village", "facility"] | None = None
    self_transport: bool = False
    recorded_at: datetime | None = None


class SosOut(Out):
    case_id: uuid.UUID
    short_code: str
    status: str
    deduplicated: bool
    version: int
    server_time: datetime
    track: dict[str, Any]


@router.post("/sos", response_model=SosOut, status_code=202)
async def sos(body: SosIn, p: Principal = Depends(policy("patient", "asha", "volunteer", purpose=EC, allow_pending=True))) -> dict[str, Any]:
    """The fastest, most protected endpoint: never 426, never hard-rejects a verified user (API-Guide §6.4.1)."""
    async with rds.case_lock(str(body.case_id)):
        async with uow(p) as tx:
            case, dedup = await cases.create_sos(
                tx, case_id=body.case_id, idempotency_key=body.idempotency_key, category=body.category,
                patient_id=body.patient_id, household_id=body.household_id, flags=list(body.flags),
                pickup=body.pickup.model_dump(by_alias=True) if body.pickup else None,
                location_source=body.location_source, self_transport=body.self_transport,
                recorded_at=body.recorded_at, channel="app")
    return {"caseId": case.id, "shortCode": case.short_code, "status": case.status, "deduplicated": dedup,
            "version": case.version, "serverTime": datetime.now(UTC),
            "track": {"ws": f"case:{case.id}", "pollAfterS": 5}}


# ---------------- /cases ----------------

class ReferralIn(In):
    id: uuid.UUID
    idempotency_key: uuid.UUID | None = None
    type: Literal["referral"] = "referral"
    patient_id: uuid.UUID
    needed_capabilities: list[str] = Field(min_length=1, max_length=10)
    referral_reason: str | None = Field(default=None, max_length=1000)
    source_entry_id: uuid.UUID | None = None
    teleconsult_session_id: uuid.UUID | None = None
    origin_facility_id: uuid.UUID | None = None
    preferred_facility_id: uuid.UUID | None = None
    override_reason: str | None = Field(default=None, max_length=300)
    pickup: GeoPoint | None = None
    needs_transport: bool = True
    recorded_at: datetime | None = None


async def _detail(tx: Any, case_id: Any, p: Principal) -> dict[str, Any]:
    case = await core.load_case(tx, case_id)
    if case is None:
        raise AppError("NOT_FOUND")
    projection = await is_case_participant(tx.conn, p, case)
    if projection is None:
        raise AppError("NOT_FOUND")
    return await cases.case_detail(tx.conn, case, projection, p)


@router.post("/cases", response_model=CaseDetailOut, status_code=201)
async def create_referral(body: ReferralIn, p: Principal = Depends(policy("asha", "doctor", "facility_staff", purpose=EC))) -> dict[str, Any]:
    async with rds.case_lock(str(body.id)):
        async with uow(p) as tx:
            case = await cases.create_referral(tx, p, body.model_dump(by_alias=True, mode="json", exclude_none=True))
            await tx.audit("case.read", "case", case.id, patient_id=case.patient_id)
            return await cases.case_detail(tx.conn, case, "asha" if p.role == "asha" else "facility", p)


@router.get("/cases/{case_id}", response_model=CaseDetailOut, response_model_exclude_none=False)
async def get_case(case_id: uuid.UUID, response: Response,
                   p: Principal = Depends(policy("patient", "asha", "volunteer", "doctor", "facility_staff", "district_admin", purpose=EC))) -> dict[str, Any]:
    async with uow(p) as tx:
        detail = await _detail(tx, case_id, p)
        await tx.audit("case.read", "case", case_id, patient_id=detail["case"]["patientId"])
    response.headers["ETag"] = f'"{detail["case"]["version"]}"'
    return detail


@router.get("/cases/by-code/{short_code}", response_model=CaseDetailOut)
async def get_case_by_code(short_code: str,
                           p: Principal = Depends(policy("patient", "asha", "volunteer", "doctor", "facility_staff", "district_admin", purpose=EC))) -> dict[str, Any]:
    async with uow(p) as tx:
        cid = (await tx.conn.execute(select(T.cases.c.id).where(T.cases.c.short_code == short_code.upper()))).scalar()
        if cid is None:
            raise AppError("NOT_FOUND")
        detail = await _detail(tx, cid, p)
        await tx.audit("case.read", "case", cid, patient_id=detail["case"]["patientId"])
    return detail


@router.get("/cases")
async def list_cases(status: list[str] = Query(default=["open"]), villageId: uuid.UUID | None = None,  # noqa: N803
                     facilityId: uuid.UUID | None = None, type: str | None = None,  # noqa: A002, N803
                     since: datetime | None = None, limit: int = 50, cursor: str | None = None,
                     p: Principal = Depends(policy("asha", "facility_staff", "district_admin", "doctor", purpose=EC))) -> dict[str, list[CaseOut] | str | None]:
    c = T.cases
    statuses = [s for s in status if s != "open"] + (list(core.OPEN) if "open" in status else [])
    cond = c.c.status.in_(statuses)
    if p.role == "asha":
        vids = [str(villageId)] if villageId and str(villageId) in p.villages else p.villages
        cond = and_(cond, c.c.village_id.in_(vids or ["00000000-0000-0000-0000-000000000000"]))
    elif p.role == "facility_staff":
        fids = [str(facilityId)] if facilityId and str(facilityId) in p.facility_ids else p.facility_ids
        cond = and_(cond, c.c.current_facility_id.in_(fids or ["00000000-0000-0000-0000-000000000000"]))
    elif p.role == "district_admin":
        cond = and_(cond, c.c.district_code == p.district_code)
    else:
        cond = and_(cond, c.c.raised_by_id == p.user_id)
    if type:
        cond = and_(cond, c.c.type == type)
    if since:
        cond = and_(cond, c.c.status_changed_at >= since)
    cur = paging.decode(cursor)
    if cur:
        cond = and_(cond, or_(c.c.status_changed_at < cur["t"], and_(c.c.status_changed_at == cur["t"], c.c.id < cur["id"])))
    n = paging.limit(limit)
    projection = {"asha": "asha", "facility_staff": "facility", "district_admin": "admin", "doctor": "facility"}[p.role]
    async with uow(p) as tx:
        rows = (await tx.conn.execute(core.case_select().where(cond).order_by(c.c.status_changed_at.desc(), c.c.id.desc())
                                      .limit(n + 1))).all()
        data = [await cases.case_out(tx.conn, r, projection) for r in rows[:n]]
    nxt = paging.encode({"t": rows[n - 1].status_changed_at.isoformat(), "id": str(rows[n - 1].id)}) if len(rows) > n else None
    return {"data": [CaseOut.model_validate(d) for d in data], "nextCursor": nxt}  # type: ignore[dict-item]


@router.get("/cases/{case_id}/events")
async def case_events(case_id: uuid.UUID, after: datetime | None = None,
                      p: Principal = Depends(policy("patient", "asha", "volunteer", "doctor", "facility_staff", "district_admin", purpose=EC))) -> dict[str, list[CaseEventOut]]:
    async with uow(p) as tx:
        detail = await _detail(tx, case_id, p)
    events = detail["events"]
    if after:
        events = [e for e in events if e["occurredAt"] > after]
    return {"data": events}  # type: ignore[dict-item]


@router.get("/me/cases")
async def my_cases(active: bool = True, p: Principal = Depends(policy("patient", "volunteer", purpose=EC, allow_pending=True))) -> dict[str, list[CaseOut]]:
    c, tl = T.cases, T.transport_legs
    if p.role == "patient":
        cond = c.c.household_id == p.household_id if p.household_id else c.c.raised_by_id == p.user_id
        cond = or_(cond, c.c.raised_by_id == p.user_id)
    else:
        cond = or_(c.c.id.in_(select(tl.c.case_id).where(tl.c.custodian_user_id == p.user_id)), c.c.raised_by_id == p.user_id)
    if active:
        cond = and_(cond, c.c.status.in_(core.OPEN))
    async with uow(p) as tx:
        rows = (await tx.conn.execute(core.case_select().where(cond).order_by(c.c.created_at.desc()).limit(20))).all()
        data = [await cases.case_out(tx.conn, r, "patient" if p.role == "patient" else "volunteer") for r in rows]
    return {"data": data}  # type: ignore[dict-item]


class CommandIn(In):
    command: Literal["Cancel", "SelfTransport", "AssignAmbulance", "ConfirmPickup", "VerifyCase", "DispatchUnverified",
                     "UpdatePickup", "AddNeed", "Escalate"]
    expected_version: int | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime | None = None


@router.post("/cases/{case_id}/commands", response_model=CaseDetailOut)
async def case_command(case_id: uuid.UUID, body: CommandIn,
                       p: Principal = Depends(policy("patient", "asha", "volunteer", "doctor", "facility_staff", "district_admin", purpose=EC))) -> dict[str, Any]:
    async with rds.case_lock(str(case_id)):
        async with uow(p) as tx:
            case = await core.require_case(tx, case_id)
            if await is_case_participant(tx.conn, p, case) is None:
                raise AppError("NOT_FOUND")
            await commands.run(tx, case, body.command, body.args, body.expected_version)
            return await _detail(tx, case_id, p)


# ---------------- matching & inbox ----------------

@router.get("/facilities/match", response_model=MatchResponse)
async def facility_match(caseId: uuid.UUID | None = None, patientId: uuid.UUID | None = None,  # noqa: N803
                         needs: str | None = None, lat: float | None = Query(default=None, ge=-90, le=90),
                         lng: float | None = Query(default=None, ge=-180, le=180),
                         p: Principal = Depends(policy("asha", "doctor", "facility_staff", purpose=EC))) -> dict[str, Any]:
    async with uow(p) as tx:
        if caseId:
            case = await core.load_case(tx, caseId)
            if case is None or await is_case_participant(tx.conn, p, case) is None:
                raise AppError("NOT_FOUND")
            needed = [c["code"] for c in await core.needed_capabilities(tx, caseId)]
            pickup = (case.pickup_lat, case.pickup_lng) if case.pickup_lat is not None else None
            district, village = case.district_code, case.village_id
            exclude = await matching.offered_facility_ids(tx.conn, caseId)
        else:
            if not patientId or not needs:
                raise AppError("VALIDATION_FAILED", "caseId, or patientId + needs, is required")
            if not await can_access_patient(tx.conn, p, patientId):
                raise AppError("NOT_FOUND")
            needed = [n for n in needs.split(",") if n]
            hh = await cases._household_geo(tx.conn, await cases._household_of_patient(tx.conn, patientId))
            pickup = (lat, lng) if lat is not None and lng is not None else (
                (float(hh.h_lat), float(hh.h_lng)) if hh and hh.h_lat is not None else None)
            district, village = (hh.district_code if hh else p.district_code), (hh.village_id if hh else None)
            exclude = set()
        results, no_capable = await matching.match(tx.conn, needed=needed, pickup=pickup, district_code=district,
                                                   village_id=village, exclude=exclude)
    return {"data": [m.out() for m in results], "noCapableFacility": no_capable, "neededCapabilities": needed,
            "computedAt": datetime.now(UTC)}


class InboxItemOut(Out):
    offer: FacilityOfferOut
    case: dict[str, Any]
    patient: dict[str, Any]
    from_: dict[str, Any] = Field(alias="from")
    eta_min: int | None = None
    handoff_packet_available: bool
    server_time: datetime


@router.get("/facilities/{facility_id}/offers")
async def facility_offers(facility_id: uuid.UUID, result: str = "pending", since: datetime | None = None,
                          p: Principal = Depends(policy("facility_staff", "doctor", purpose=EC))) -> dict[str, Any]:
    if str(facility_id) not in p.facility_ids:
        raise AppError("FORBIDDEN_SCOPE")
    o, c, pt, u, v, f = T.facility_offers, T.cases, T.patients, T.users, T.villages, T.facilities
    results = result.split(",")
    q = (select(o, c.c.short_code, c.c.type, c.c.emergency_category, c.c.status.label("case_status"), c.c.verified,
                c.c.patient_id, c.c.raised_by_id, c.c.raised_by_role, c.c.village_id, c.c.origin_facility_id,
                c.c.id.label("cid"))
         .join(c, c.c.id == o.c.case_id).where(o.c.facility_id == facility_id, o.c.result.in_(results)))
    if since:
        q = q.where(o.c.offered_at >= since)
    q = q.order_by(o.c.expires_at if results == ["pending"] else o.c.offered_at.desc()).limit(100)
    out = []
    async with uow(p) as tx:
        for r in (await tx.conn.execute(q)).all():
            needed = await core.needed_capabilities(tx, r.cid)
            patient: dict[str, Any] = {"shortCode": None, "firstName": None, "ageYears": None, "sex": None, "highRisk": False}
            if r.patient_id:
                prow = (await tx.conn.execute(select(pt).where(pt.c.id == r.patient_id))).first()
                from app.modules.onboarding.service import age_years, patients_out

                full = (await patients_out(tx.conn, [prow]))[0]
                patient = {"id": prow.id, "shortCode": prow.short_code, "firstName": (full["name"] or "").split(" ")[0],
                           "ageYears": age_years(prow.date_of_birth), "sex": prow.sex, "highRisk": full["highRisk"]}
            frm: dict[str, Any] = {}
            if r.raised_by_role == "asha" and r.raised_by_id:
                urow = (await tx.conn.execute(select(u.c.id, u.c.name_enc).where(u.c.id == r.raised_by_id))).first()
                frm["ashaName"] = await decrypt(tx.conn, "user", urow.id, "users", "name_enc", urow.id, urow.name_enc)
            if r.village_id:
                frm["villageName"] = (await tx.conn.execute(select(v.c.name).where(v.c.id == r.village_id))).scalar()
            if r.origin_facility_id:
                frm["originFacilityName"] = (await tx.conn.execute(select(f.c.name).where(f.c.id == r.origin_facility_id))).scalar()
            offer = {"id": r.id, "caseId": r.case_id, "facilityId": r.facility_id, "rank": r.rank, "attempt": r.attempt,
                     "slot": r.slot, "source": r.source, "result": r.result, "declineReason": r.decline_reason,
                     "declineNote": r.decline_note, "etaSeconds": r.eta_seconds, "etaEstimated": r.eta_estimated,
                     "distanceM": r.distance_m, "staleCapability": r.stale_capability,
                     "capabilityUnconfirmed": r.capability_unconfirmed, "matchReasons": r.match_reasons,
                     "offeredAt": r.offered_at, "expiresAt": r.expires_at, "openedAt": r.opened_at,
                     "respondedAt": r.responded_at}
            out.append({"offer": offer, "case": {"id": r.cid, "shortCode": r.short_code, "type": r.type,
                                                 "emergencyCategory": r.emergency_category, "neededCapabilities": needed,
                                                 "status": r.case_status, "verified": r.verified},
                        "patient": patient, "from": frm,
                        "etaMin": max(1, round(r.eta_seconds / 60)) if r.eta_seconds else None,
                        "handoffPacketAvailable": r.patient_id is not None, "serverTime": datetime.now(UTC)})
        await tx.audit("facility.inbox_read", "facility", facility_id, diff={"count": len(out)})
    return {"data": [InboxItemOut.model_validate(x).model_dump(by_alias=True) for x in out]}


@router.post("/cases/{case_id}/offers/{offer_id}/opened", status_code=204)
async def offer_opened(case_id: uuid.UUID, offer_id: uuid.UUID,
                       p: Principal = Depends(policy("facility_staff", "doctor", purpose=EC))) -> Response:
    o = T.facility_offers
    async with uow(p) as tx:
        row = (await tx.conn.execute(select(o.c.facility_id, o.c.opened_at).where(and_(o.c.id == offer_id, o.c.case_id == case_id)))).first()
        if row is None or str(row.facility_id) not in p.facility_ids:
            raise AppError("NOT_FOUND")
        if row.opened_at is None:
            await tx.conn.execute(update(o).where(o.c.id == offer_id).values(opened_at=datetime.now(UTC)))
            await core.event(tx, case_id, "offer_opened", {}, offer_id=offer_id)
    return Response(status_code=204)


class RespondIn(In):
    decision: Literal["accept", "decline"]
    reason: Literal["no_bed", "no_specialist", "equipment_down", "not_our_capability", "other"] | None = None
    note: str | None = Field(default=None, max_length=300)
    beds_available: int | None = Field(default=None, ge=0, le=5000)


class RespondOut(Out):
    offer: FacilityOfferOut
    case: CaseOut


@router.post("/cases/{case_id}/offers/{offer_id}/respond", response_model=RespondOut)
async def respond(case_id: uuid.UUID, offer_id: uuid.UUID, body: RespondIn,
                  p: Principal = Depends(policy("facility_staff", "doctor", purpose=EC))) -> dict[str, Any]:
    async with rds.case_lock(str(case_id)):
        async with uow(p) as tx:
            res = await cascade.respond(tx, case_id, offer_id, body.decision, body.reason, body.note, body.beds_available)
            o = res["offer"]
            return {"offer": {"id": o.id, "caseId": o.case_id, "facilityId": o.facility_id, "rank": o.rank,
                              "attempt": o.attempt, "slot": o.slot, "source": o.source, "result": o.result,
                              "declineReason": o.decline_reason, "declineNote": o.decline_note,
                              "etaSeconds": o.eta_seconds, "etaEstimated": o.eta_estimated, "distanceM": o.distance_m,
                              "staleCapability": o.stale_capability, "capabilityUnconfirmed": o.capability_unconfirmed,
                              "matchReasons": o.match_reasons, "offeredAt": o.offered_at, "expiresAt": o.expires_at,
                              "openedAt": o.opened_at, "respondedAt": o.responded_at},
                    "case": await cases.case_out(tx.conn, res["case"], "facility")}


@router.get("/cases/{case_id}/handoff-packet")
async def handoff_packet(case_id: uuid.UUID, p: Principal = Depends(policy("facility_staff", "doctor", purpose=EC))) -> dict[str, Any]:
    """In-transit pre-registration packet (API-Guide §6.5.4) — accepted facility only."""
    from app.modules.comms.notify import user_contact
    from app.modules.onboarding.service import age_years
    from app.modules.routine_care.service import entries_out
    from app.modules.transport import legs as tlegs

    async with uow(p) as tx:
        case = await core.load_case(tx, case_id)
        if case is None or case.current_facility_id is None or str(case.current_facility_id) not in p.facility_ids:
            raise AppError("NOT_FOUND")
        patient: dict[str, Any] | None = None
        latest_vitals, risk_reasons, conditions, meds, asha_notes, asha_contact = None, [], [], [], None, None
        if case.patient_id:
            pt, e = T.patients, T.v_patient_timeline
            prow = (await tx.conn.execute(select(pt).where(pt.c.id == case.patient_id))).first()
            patient = {"name": await decrypt(tx.conn, "patient", prow.id, "patients", "name_enc", prow.id, prow.name_enc),
                       "ageYears": age_years(prow.date_of_birth), "sex": prow.sex, "bloodGroup": prow.blood_group,
                       "shortCode": prow.short_code}
            erows = (await tx.conn.execute(select(e).where(e.c.patient_id == case.patient_id)
                                           .order_by(e.c.recorded_at.desc()).limit(10))).all()
            entries = await entries_out(tx.conn, erows)
            with_vitals = next((x for x in entries if x["vitals"]), None)
            if with_vitals:
                latest_vitals = {k: v for k, v in with_vitals["vitals"].items() if v is not None}
                latest_vitals["recordedAt"] = with_vitals["recordedAt"]
            risk_reasons = sorted({f["ruleCode"] for x in entries for f in x["riskFlags"] if f["evaluatedBy"] == "server"})
            asha_notes = next((x["notes"] for x in entries if x["authorRole"] == "asha" and x["notes"]), None)
            pc, pm = T.patient_conditions, T.patient_medications
            conditions = [r.condition_code for r in (await tx.conn.execute(select(pc.c.condition_code).where(
                pc.c.patient_id == case.patient_id, pc.c.status == "active"))).all()]
            meds = [r.medicine_name for r in (await tx.conn.execute(select(pm.c.medicine_name).where(
                pm.c.patient_id == case.patient_id, pm.c.stopped_on.is_(None)))).all()]
        asha_id = await tlegs.household_asha(tx.conn, case.household_id)
        if asha_id:
            u = T.users
            urow = (await tx.conn.execute(select(u.c.id, u.c.name_enc).where(u.c.id == asha_id))).first()
            phone, _ = await user_contact(tx.conn, asha_id)
            from app.core.crypto import mask_phone

            asha_contact = {"name": await decrypt(tx.conn, "user", urow.id, "users", "name_enc", urow.id, urow.name_enc),
                            "phone": mask_phone(phone)}
        legs = await tlegs.legs_of(tx.conn, case.id)
        cur = next((x for x in legs if x.id == case.current_leg_id), None)
        current_leg = None
        if cur is not None:
            cust = await tlegs.custodian_view(tx.conn, cur, with_phone=False)
            current_leg = {"legOrder": cur.leg_order, "custodianFirstName": (cust or {}).get("firstName"),
                           "etaMin": max(1, round(cur.eta_seconds / 60)) if cur.eta_seconds else None}
        await tx.audit("patient.read", "case", case.id, patient_id=case.patient_id, diff={"view": "handoff_packet"})
    return {"caseId": case.id, "shortCode": case.short_code, "patient": patient, "category": case.emergency_category,
            "neededCapabilities": [c["code"] for c in await _needs(case.id, p)], "latestVitals": latest_vitals,
            "riskReasons": risk_reasons, "conditions": conditions, "medications": meds, "ashaNotes": asha_notes,
            "ashaContact": asha_contact, "currentLeg": current_leg}


async def _needs(case_id: Any, p: Principal) -> list[dict[str, str]]:
    async with uow(p) as tx:
        return await core.needed_capabilities(tx, case_id)


class PreRegisterIn(In):
    facility_reg_no: str | None = Field(default=None, max_length=60)


@router.post("/cases/{case_id}/pre-register", response_model=FacilityAdmissionOut)
async def pre_register(case_id: uuid.UUID, body: PreRegisterIn,
                       p: Principal = Depends(policy("facility_staff", purpose=EC))) -> dict[str, Any]:
    async with rds.case_lock(str(case_id)):
        async with uow(p) as tx:
            case = await core.require_case(tx, case_id)
            a = await status.pre_register(tx, case, body.facility_reg_no)
    return {"caseId": a.case_id, "facilityId": a.facility_id, "preRegisteredAt": a.pre_registered_at,
            "facilityRegNo": a.facility_reg_no, "arrivedAt": a.arrived_at, "seenAt": a.seen_at, "outcome": a.outcome,
            "onwardCaseId": a.onward_case_id, "closedAt": a.closed_at, "version": a.version}


class StatusIn(In):
    action: Literal["arrived", "seen", "close"]
    at: datetime | None = None
    handover: dict[str, Any] | None = None
    outcome: Literal["treated_discharged", "admitted", "referred_onward", "left_against_advice", "death", "other"] | None = None
    outcome_entry: dict[str, Any] | None = None
    care_plan: dict[str, Any] | None = None
    onward_referral: dict[str, Any] | None = None


@router.post("/cases/{case_id}/status", response_model=CaseDetailOut)
async def case_status(case_id: uuid.UUID, body: StatusIn,
                      p: Principal = Depends(policy("facility_staff", "doctor", purpose=EC))) -> dict[str, Any]:
    if body.action == "arrived" and p.role != "facility_staff":
        raise AppError("FORBIDDEN_ROLE", "Arrival is confirmed by facility staff")
    async with rds.case_lock(str(case_id)):
        async with uow(p) as tx:
            case = await core.require_case(tx, case_id)
            if body.action == "arrived":
                await status.arrived(tx, case, body.model_dump(by_alias=True, mode="json"))
            elif body.action == "seen":
                await status.seen(tx, case)
            else:
                await status.close(tx, case, body.model_dump(by_alias=True, mode="json", exclude_none=True))
            return await cases.case_detail(tx.conn, await core.load_case(tx, case_id), "facility", p)




@router.get("/facilities/{facility_id}/stats")
async def facility_stats(facility_id: uuid.UUID, period: Literal["week", "month"] = "week",
                         p: Principal = Depends(policy("facility_staff", "district_admin", purpose="programme_reporting"))) -> dict[str, Any]:
    """Response-time and closure stats for one facility (API-Guide §6.7; R-02 facility response leaderboard)."""
    from datetime import timedelta

    o, fa, f = T.facility_offers, T.facility_admissions, T.facilities
    since = datetime.now(UTC) - timedelta(days=7 if period == "week" else 30)
    async with uow(p) as tx:
        fac = (await tx.conn.execute(select(f.c.district_code).where(f.c.id == facility_id))).first()
        if fac is None or (p.role == "facility_staff" and str(facility_id) not in p.facility_ids) or (
                p.role == "district_admin" and fac.district_code != p.district_code):
            raise AppError("NOT_FOUND")
        offers = (await tx.conn.execute(select(o.c.result, o.c.decline_reason, o.c.offered_at, o.c.responded_at).where(
            o.c.facility_id == facility_id, o.c.offered_at >= since))).all()
        arrivals = (await tx.conn.execute(select(fa.c.arrived_at, fa.c.closed_at).where(
            fa.c.facility_id == facility_id, fa.c.arrived_at >= since))).all()
    responded = sorted((r.responded_at - r.offered_at).total_seconds() for r in offers if r.responded_at)
    declined: dict[str, int] = {}
    for r in offers:
        if r.result == "declined":
            declined[r.decline_reason] = declined.get(r.decline_reason, 0) + 1
    closed72 = sum(1 for a in arrivals if a.closed_at and (a.closed_at - a.arrived_at).total_seconds() <= 72 * 3600)
    return {"period": period, "offersReceived": len(offers), "accepted": sum(1 for r in offers if r.result == "accepted"),
            "timedOut": sum(1 for r in offers if r.result == "timeout"), "declinedByReason": declined,
            "medianResponseS": round(responded[len(responded) // 2]) if responded else None,
            "arrivals": len(arrivals), "closedWithin72hPct": round(100.0 * closed72 / len(arrivals), 1) if arrivals else None}
