"""SOS and referral creation (T1) and case projections (TRD §5, §7.2, API-Guide §6.4, §2.9).

Dedupe — no second case for the same emergency (I12, ADR-04/07):
  1. same `idempotency_key` → that case
  2. `idem_tag` (first 8 base32 chars of the key, the SMS `#tag`) + same patient/household → that case
     (`channel_duplicate`): the SMS-created case and the later app sync merge
  3. an open SOS for the same patient/household created < `sms.dedupe_window_min` (30 min) → that case
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, insert, or_, select

from app.core import cfg
from app.core import redis as rds
from app.core.crypto import decrypt, encrypt
from app.core.db import Conn, T, lat_of, lng_of, point
from app.core.errors import AppError
from app.core.ids import idem_tag as make_idem_tag
from app.core.ids import new_short_code
from app.core.rbac import Principal, can_access_patient
from app.core.uow import UoW, case_scopes
from app.modules.referral import cascade, core, matching, timers

FLAG_CONDITIONS = {"c_section_flag": "c_section_flag", "spo2_lt_90": "spo2_lt_90"}
CLOSED = ("closed", "follow_up", "cancelled")


async def _short_code(conn: Conn) -> str:
    c = T.cases
    for _ in range(8):
        code = new_short_code()
        if not (await conn.execute(select(exists().where(c.c.short_code == code)))).scalar():
            return code
    raise AppError("INTERNAL", "Could not allocate a case code")


async def _household_of_patient(conn: Conn, patient_id: Any) -> Any:
    return (await conn.execute(select(T.patients.c.household_id).where(T.patients.c.id == patient_id))).scalar()


async def _household_geo(conn: Conn, household_id: Any) -> Any:
    h, v = T.households, T.villages
    return (await conn.execute(select(h.c.id, h.c.village_id, v.c.district_code,
                                      lat_of(h.c.location).label("h_lat"), lng_of(h.c.location).label("h_lng"),
                                      lat_of(v.c.location).label("v_lat"), lng_of(v.c.location).label("v_lng"))
                               .join(v, v.c.id == h.c.village_id).where(h.c.id == household_id))).first()


async def _nearest_village(conn: Conn, lat: float, lng: float, district: str | None) -> Any:
    v = T.villages
    pt = func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326).cast(v.c.location.type)
    q = select(v.c.id, v.c.district_code).where(v.c.deleted_at.is_(None), func.ST_DWithin(v.c.location, pt, 25000))
    if district:
        q = q.where(v.c.district_code == district)
    return (await conn.execute(q.order_by(v.c.location.op("<->")(pt)).limit(1))).first()


async def find_existing(conn: Conn, *, key: uuid.UUID, tag: str, patient_id: Any, household_id: Any,
                        window_min: int) -> tuple[Any, str | None]:
    c = T.cases
    row = (await conn.execute(select(c.c.id).where(c.c.idempotency_key == key))).first()
    if row:
        return row.id, None
    who = []
    if patient_id:
        who.append(c.c.patient_id == patient_id)
    if household_id:
        who.append(c.c.household_id == household_id)
    if who:
        row = (await conn.execute(select(c.c.id).where(and_(c.c.idem_tag == tag, or_(*who))).limit(1))).first()
        if row:
            return row.id, "channel_duplicate"
        row = (await conn.execute(select(c.c.id).where(and_(
            c.c.type == "sos", or_(*who), c.c.status.notin_(CLOSED),
            c.c.created_at > datetime.now(UTC) - timedelta(minutes=window_min))).order_by(c.c.created_at.desc()).limit(1))).first()
        if row:
            return row.id, "channel_duplicate"
    return None, None


async def create_sos(tx: UoW, *, case_id: uuid.UUID, idempotency_key: uuid.UUID, category: str,
                     patient_id: Any = None, household_id: Any = None, flags: list[str] | None = None,
                     pickup: dict[str, Any] | None = None, location_source: str | None = None,
                     self_transport: bool = False, recorded_at: datetime | None = None, channel: str = "app",
                     tag: str | None = None, verified: bool = True, verification_level: str = "app",
                     raised_by: tuple[Any, str] | None = None, fallback_district: str | None = None,
                     dedupe_channel: str | None = None) -> tuple[Any, bool]:
    """T1 for an SOS. Returns (case row, deduplicated)."""
    p = tx.principal
    c = T.cases
    if patient_id and not household_id:
        household_id = await _household_of_patient(tx.conn, patient_id)
    if p is not None:
        # role scope for raising (API-Guide §4.3): patient own household · asha assigned villages · volunteer on behalf
        if p.role == "patient" and household_id and str(household_id) != p.household_id:
            raise AppError("FORBIDDEN_SCOPE", "Not your household")
        if p.role == "asha" and patient_id and not await can_access_patient(tx.conn, p, patient_id):
            raise AppError("FORBIDDEN_SCOPE", "Patient outside your villages")
        if p.role == "patient" and not household_id:
            household_id = p.household_id
    conf = await cfg.merged(tx.conn, p.district_code if p else fallback_district)
    tag = tag or make_idem_tag(idempotency_key)
    existing, how = await find_existing(tx.conn, key=idempotency_key, tag=tag, patient_id=patient_id,
                                        household_id=household_id, window_min=int(conf.get("sms.dedupe_window_min", 30)))
    if existing is None and p is not None and not await rds.rate_limit("sos", str(p.user_id), 5, 60):
        # TRD §13.4: excess SOS are deduplicated into the raiser's open case, never rejected
        row = (await tx.conn.execute(select(c.c.id).where(and_(c.c.raised_by_id == p.user_id, c.c.type == "sos",
                                                              c.c.status.notin_(CLOSED))).order_by(c.c.created_at.desc()).limit(1))).first()
        if row:
            existing, how = row.id, "channel_duplicate"
    if existing is not None:
        case = await core.load_case(tx, existing, lock=True)
        if how:
            await core.event(tx, case.id, how, {"channel": dedupe_channel or channel}, channel=_ev_channel(dedupe_channel or channel))
        return case, True

    hh = await _household_geo(tx.conn, household_id) if household_id else None
    lat = lng = None
    src = "none"
    if pickup:
        lat, lng, src = pickup["lat"], pickup["lng"], location_source if location_source in ("gps", "facility") else "gps"
    elif hh is not None and hh.h_lat is not None:
        lat, lng, src = float(hh.h_lat), float(hh.h_lng), "household"
    elif hh is not None:
        lat, lng, src = float(hh.v_lat), float(hh.v_lng), "village"
    village_id = hh.village_id if hh else None
    district = hh.district_code if hh else None
    if village_id is None and lat is not None:
        near = await _nearest_village(tx.conn, lat, lng, p.district_code if p else fallback_district)
        if near:
            village_id, district = near.id, near.district_code
    if village_id is None and p is not None and p.home_village_id:
        village_id = p.home_village_id
    district = district or (p.district_code if p else None) or fallback_district
    if village_id and not district:
        district = (await tx.conn.execute(select(T.villages.c.district_code).where(T.villages.c.id == village_id))).scalar()
    if not district:
        raise AppError("VALIDATION_FAILED", "Cannot resolve the district for this SOS")
    now = datetime.now(UTC)
    rec = recorded_at or now
    if rec > now + timedelta(minutes=5):
        rec = now  # device clock in the future — re-stamped (API-Guide §2.4)
    raiser_id, raiser_role = raised_by if raised_by else ((p.user_id, p.role) if p else (None, None))
    await tx.conn.execute(insert(c).values(
        id=case_id, short_code=await _short_code(tx.conn), type="sos", channel=channel, patient_id=patient_id,
        household_id=household_id, district_code=district, village_id=village_id, raised_by_id=raiser_id,
        raised_by_role=raiser_role, emergency_category=category,
        transport_mode="self" if self_transport else None,
        pickup_point=point(lat, lng) if lat is not None else None,
        pickup_accuracy_m=(pickup or {}).get("accuracyM"), location_source=src, idempotency_key=idempotency_key,
        idem_tag=tag, verified=verified, verification_level=verification_level,
        offer_mode=conf.get("cascade.offer_mode", "sequential"), recorded_at=rec))
    await _needed_from_category(tx, case_id, category, flags or [], district)
    case = await core.load_case(tx, case_id, lock=True)
    await core.event(tx, case_id, "case_created", {"type": "sos", "category": category, "channel": channel,
                                                   "verified": verified, "locationSource": src},
                     channel=_ev_channel(channel), recorded_at=rec)
    await tx.audit("case.create", "case", case_id, patient_id=patient_id, purpose="emergency_care",
                   diff={"type": "sos", "channel": channel, "category": category})
    await _after_create(tx, case, conf, self_transport=self_transport)
    return await core.load_case(tx, case_id), False


def _ev_channel(channel: str) -> str:
    return {"app": "app", "web": "web", "sms": "sms", "ivr": "ivr"}.get(channel, "system")


async def _needed_from_category(tx: UoW, case_id: Any, category: str, flags: list[str], district: str) -> None:
    ccd, cnc = T.category_capability_defaults, T.case_needed_capabilities
    conds = [FLAG_CONDITIONS[f] for f in flags if f in FLAG_CONDITIONS]
    rows = (await tx.conn.execute(select(ccd.c.capability_code, ccd.c.district_code).where(and_(
        ccd.c.category_code == category,
        or_(ccd.c.district_code.is_(None), ccd.c.district_code == district),
        or_(ccd.c.condition_code.is_(None), ccd.c.condition_code.in_(conds or ["-"])))))).all()
    district_rows = [r for r in rows if r.district_code == district]
    chosen = {r.capability_code for r in (district_rows or rows)}
    for code in sorted(chosen):
        await tx.conn.execute(insert(cnc).values(case_id=case_id, capability_code=code, source="category_default"))


async def _after_create(tx: UoW, case: Any, conf: dict[str, Any], *, self_transport: bool) -> None:
    from app.modules.transport import legs as tlegs

    leg_ids: list[Any] = []
    if not self_transport:
        leg_ids = await tlegs.plan_legs(tx, case)
    await timers.schedule(tx, case.id, "stage_sla", int(conf.get("sla.created_to_matched_s", 30)), None)
    await timers.schedule(tx, case.id, "stage_sla", int(conf.get("cascade.total_timeout_s", 600)),
                          timers.acceptance_ref(case.id))
    if not case.verified:
        await core.escalate(tx, case, "unverified_sms")  # human decision ≤ 3 min (SEC-CH-02)
    await tx.journal("case", case.id, await case_scopes(tx.conn, case.id))
    dispatch_leg = leg_ids[0] if leg_ids and case.location_source != "facility" and case.verified else None

    async def _go() -> None:
        from app.workers.registry import enqueue

        await enqueue("match_and_offer", case_id=case.id)
        if dispatch_leg:
            await enqueue("find_volunteer", leg_id=dispatch_leg)
        await enqueue("notify_sos", case_id=case.id)
    tx.after_commit(_go)


async def create_referral(tx: UoW, p: Principal, data: dict[str, Any]) -> Any:
    """POST /cases (API-Guide §6.4.2). Matching is re-run on the server (FR-M02); override needs a reason (FR-M03)."""
    c, cnc = T.cases, T.case_needed_capabilities
    case_id = uuid.UUID(str(data["id"]))
    key = uuid.UUID(str(data.get("idempotencyKey") or data["id"]))
    existing = (await tx.conn.execute(select(c.c.id).where(or_(c.c.id == case_id, c.c.idempotency_key == key)))).first()
    if existing:
        return await core.load_case(tx, existing.id)
    patient_id = uuid.UUID(str(data["patientId"]))
    if not await can_access_patient(tx.conn, p, patient_id):
        raise AppError("FORBIDDEN_SCOPE", "Patient outside your scope")
    needs = sorted(set(data.get("neededCapabilities") or []))
    known = set((await tx.conn.execute(select(T.capabilities.c.code))).scalars().all())
    unknown = set(needs) - known
    if unknown or not needs:
        raise AppError("VALIDATION_FAILED", "Unknown or missing capabilities",
                       fields=[{"path": "neededCapabilities", "code": "invalid", "values": sorted(unknown)}])
    origin_facility = data.get("originFacilityId")
    if p.role == "facility_staff":
        if not origin_facility or str(origin_facility) not in p.facility_ids:
            raise AppError("FORBIDDEN_SCOPE", "Outbound referrals are from your own facility")
    household_id = await _household_of_patient(tx.conn, patient_id)
    hh = await _household_geo(tx.conn, household_id)
    pickup = data.get("pickup")
    lat = lng = None
    src = "none"
    if pickup:
        lat, lng, src = pickup["lat"], pickup["lng"], "gps"
    elif origin_facility:
        f = T.facilities
        frow = (await tx.conn.execute(select(lat_of(f.c.location).label("lat"), lng_of(f.c.location).label("lng"))
                                      .where(f.c.id == origin_facility))).first()
        lat, lng, src = float(frow.lat), float(frow.lng), "facility"
    elif hh is not None and hh.h_lat is not None:
        lat, lng, src = float(hh.h_lat), float(hh.h_lng), "household"
    elif hh is not None:
        lat, lng, src = float(hh.v_lat), float(hh.v_lng), "village"
    district = hh.district_code if hh else p.district_code
    conf = await cfg.merged(tx.conn, district)
    results, no_capable = await matching.match(tx.conn, needed=needs, pickup=(lat, lng) if lat is not None else None,
                                               district_code=district, village_id=hh.village_id if hh else None)
    preferred = data.get("preferredFacilityId")
    override_reason = (data.get("overrideReason") or "").strip() or None
    chosen = results[0] if results else None
    source = "cascade"
    if preferred:
        pick = next((m for m in results if str(m.facility_id) == str(preferred)), None)
        if pick is None:
            raise AppError("VALIDATION_FAILED", "Preferred facility is not a capable, open facility",
                           fields=[{"path": "preferredFacilityId", "code": "not_capable"}])
        if pick.rank != 1:
            if not override_reason:
                raise AppError("OVERRIDE_REASON_REQUIRED")
            source = "override"
        chosen = pick
    rec = datetime.fromisoformat(str(data["recordedAt"]).replace("Z", "+00:00")) if data.get("recordedAt") else datetime.now(UTC)
    if rec > datetime.now(UTC) + timedelta(minutes=5):
        rec = datetime.now(UTC)
    await tx.conn.execute(insert(c).values(
        id=case_id, short_code=await _short_code(tx.conn), type="referral", channel="app" if p.role == "asha" else "web",
        patient_id=patient_id, household_id=household_id, district_code=district,
        village_id=hh.village_id if hh else None, raised_by_id=p.user_id, raised_by_role=p.role,
        origin_facility_id=origin_facility,
        referral_reason_enc=await encrypt(tx.conn, "patient", patient_id, "cases", "referral_reason_enc", case_id,
                                          data["referralReason"]) if data.get("referralReason") else None,
        override_reason=override_reason if source == "override" else None,
        transport_mode=None if data.get("needsTransport", True) else "self",
        pickup_point=point(lat, lng) if lat is not None else None, location_source=src, idempotency_key=key,
        idem_tag=make_idem_tag(key), verified=True, verification_level="app",
        offer_mode=conf.get("cascade.offer_mode", "sequential"), recorded_at=rec))
    src_role = p.role if p.role in ("asha", "doctor", "facility_staff") else "admin"
    for code in needs:
        await tx.conn.execute(insert(cnc).values(case_id=case_id, capability_code=code, source=src_role, added_by=p.user_id))
    case = await core.load_case(tx, case_id, lock=True)
    await core.event(tx, case_id, "case_created", {"type": "referral", "sourceEntryId": data.get("sourceEntryId"),
                                                   "teleconsultSessionId": data.get("teleconsultSessionId")},
                     recorded_at=rec)
    await tx.audit("case.create", "case", case_id, patient_id=patient_id, diff={"type": "referral", "needs": needs})
    if data.get("teleconsultSessionId"):
        from sqlalchemy import update

        tc = T.teleconsult_sessions
        await tx.conn.execute(update(tc).where(tc.c.id == data["teleconsultSessionId"]).values(referral_case_id=case_id))
    if source == "override":
        await core.event(tx, case_id, "override_used", {"facilityId": str(chosen.facility_id), "rank": chosen.rank})
    await timers.schedule(tx, case_id, "stage_sla", int(conf.get("cascade.total_timeout_s", 600)),
                          timers.acceptance_ref(case_id))
    if chosen is not None:
        await cascade.create_offer(tx, case, chosen, source="fallback" if chosen.capability_unconfirmed else source)
        case = await core.set_status(tx, case, "matched")
    if no_capable:
        await core.escalate(tx, case, "no_capable_facility")
    from app.modules.transport import legs as tlegs

    leg_ids = await tlegs.plan_legs(tx, case) if data.get("needsTransport", True) else []
    await tx.journal("case", case_id, await case_scopes(tx.conn, case_id))
    dispatch = leg_ids[0] if leg_ids and src != "facility" else None

    async def _go() -> None:
        from app.workers.registry import enqueue

        if dispatch:
            await enqueue("find_volunteer", leg_id=dispatch)
        if chosen is None:
            await enqueue("match_and_offer", case_id=case_id)
    tx.after_commit(_go)
    return await core.load_case(tx, case_id)


# ---------------- projections ----------------

async def case_out(conn: Conn, case: Any, projection: str) -> dict[str, Any]:
    from app.modules.transport import legs as tlegs

    f = T.facilities
    needed = [{"code": r.capability_code, "source": r.source} for r in (await conn.execute(
        select(T.case_needed_capabilities).where(T.case_needed_capabilities.c.case_id == case.id))).all()]
    current_facility = None
    if case.current_facility_id:
        fr = (await conn.execute(select(f.c.id, f.c.name, f.c.duty_phone_enc).where(f.c.id == case.current_facility_id))).first()
        phone = await decrypt(conn, "external_contact", fr.id, "facilities", "duty_phone_enc", fr.id, fr.duty_phone_enc) \
            if fr.duty_phone_enc is not None and projection in ("patient", "asha", "admin") else None
        current_facility = {"id": fr.id, "name": fr.name, "phone": phone}
    legs = [x for x in await tlegs.legs_of(conn, case.id) if x.status != "cancelled"]
    current = next((x for x in legs if x.id == case.current_leg_id), None)
    eta_min = None
    if current is not None and current.eta_seconds:
        eta_min = max(1, round(current.eta_seconds / 60))
    try:
        cached = await rds.r().hget(rds.k(f"eta:case:{case.id}"), "etaS")
        if cached:
            eta_min = max(1, round(int(cached) / 60))
    except Exception:  # noqa: BLE001, S110
        pass
    out = {
        "id": case.id, "shortCode": case.short_code, "type": case.type, "channel": case.channel,
        "patientId": case.patient_id, "householdId": case.household_id, "villageId": case.village_id,
        "districtCode": case.district_code, "raisedById": case.raised_by_id, "raisedByRole": case.raised_by_role,
        "emergencyCategory": case.emergency_category, "neededCapabilities": needed,
        "originFacilityId": case.origin_facility_id, "overrideReason": case.override_reason, "status": case.status,
        "statusChangedAt": case.status_changed_at, "version": case.version,
        "currentFacilityId": case.current_facility_id, "currentFacility": current_facility,
        "currentLegId": case.current_leg_id, "transportMode": case.transport_mode,
        "pickupPoint": {"lat": float(case.pickup_lat), "lng": float(case.pickup_lng),
                        "accuracyM": case.pickup_accuracy_m} if case.pickup_lat is not None else None,
        "locationSource": case.location_source, "verified": case.verified,
        "verificationLevel": case.verification_level, "escalationLevel": case.escalation_level,
        "offerMode": case.offer_mode, "cancelReason": case.cancel_reason, "recordedAt": case.recorded_at,
        "serverReceivedAt": case.server_received_at, "acceptedAt": case.accepted_at, "arrivedAt": case.arrived_at,
        "closedAt": case.closed_at,
        "legProgress": {"current": current.leg_order, "total": len(legs)} if current is not None else None,
        "etaMin": eta_min,
    }
    if projection == "volunteer":
        for k in ("patientId", "householdId", "raisedById", "overrideReason", "neededCapabilities", "originFacilityId"):
            out[k] = [] if k == "neededCapabilities" else None
    if projection == "patient":
        out["overrideReason"] = None
    return out


async def case_detail(conn: Conn, case: Any, projection: str, viewer: Principal | None = None) -> dict[str, Any]:
    from app.modules.transport import legs as tlegs

    ce, o, f, fa, esc = T.case_events, T.facility_offers, T.facilities, T.facility_admissions, T.case_escalations
    events = (await conn.execute(select(ce).where(ce.c.case_id == case.id).order_by(ce.c.occurred_at))).all()
    ev = [{"id": e.id, "caseId": e.case_id, "action": e.action, "fromStatus": e.from_status, "toStatus": e.to_status,
           "actorId": e.actor_id, "actorRole": e.actor_role, "channel": e.channel, "offerId": e.offer_id,
           "legId": e.leg_id, "payload": e.payload, "occurredAt": e.occurred_at, "recordedAt": e.recorded_at}
          for e in events]
    offers = []
    if projection not in ("patient", "volunteer"):
        q = select(o, f.c.name.label("facility_name")).join(f, f.c.id == o.c.facility_id).where(o.c.case_id == case.id)
        if projection == "facility" and viewer is not None:
            q = q.where(o.c.facility_id.in_(viewer.facility_ids))
        for r in (await conn.execute(q.order_by(o.c.offered_at))).all():
            offers.append({"id": r.id, "caseId": r.case_id, "facilityId": r.facility_id, "facilityName": r.facility_name,
                           "rank": r.rank, "attempt": r.attempt, "slot": r.slot, "source": r.source, "result": r.result,
                           "declineReason": r.decline_reason, "declineNote": r.decline_note, "etaSeconds": r.eta_seconds,
                           "etaEstimated": r.eta_estimated, "distanceM": r.distance_m,
                           "staleCapability": r.stale_capability, "capabilityUnconfirmed": r.capability_unconfirmed,
                           "matchReasons": r.match_reasons, "offeredAt": r.offered_at, "expiresAt": r.expires_at,
                           "openedAt": r.opened_at, "respondedAt": r.responded_at})
    if projection == "patient":
        # patients never see "Declined" (UI §14): only lifecycle events, no facility decisions
        ev = [e for e in ev if e["action"] in ("case_created", "status_changed", "pickup_confirmed", "custody_handover")]
        for e in ev:
            e["payload"] = {}
    if projection == "volunteer":
        ev = []
    legs = await tlegs.legs_out(conn, await tlegs.legs_of(conn, case.id), projection=projection,
                                viewer=viewer.user_id if viewer else None)
    adm = (await conn.execute(select(fa).where(fa.c.case_id == case.id))).first()
    admission = None if adm is None or projection == "volunteer" else {
        "caseId": adm.case_id, "facilityId": adm.facility_id, "preRegisteredAt": adm.pre_registered_at,
        "facilityRegNo": adm.facility_reg_no, "arrivedAt": adm.arrived_at, "seenAt": adm.seen_at,
        "outcome": adm.outcome, "onwardCaseId": adm.onward_case_id, "closedAt": adm.closed_at, "version": adm.version}
    escalations = None
    if projection in ("admin", "asha", "facility"):
        escalations = [{"id": r.id, "caseId": r.case_id, "level": r.level, "reason": r.reason, "raisedAt": r.raised_at,
                        "acknowledgedAt": r.acknowledged_at, "resolvedAt": r.resolved_at, "resolution": r.resolution}
                       for r in (await conn.execute(select(esc).where(esc.c.case_id == case.id).order_by(esc.c.raised_at))).all()]
    return {"case": await case_out(conn, case, projection), "events": ev, "offers": offers, "legs": legs,
            "admission": admission, "escalations": escalations}
