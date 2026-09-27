"""Entity loaders for sync pulls and snapshots — role projections applied here (API-Guide §2.9, §8.2).

Every loader returns {id: data} for the ids asked, already projected for the caller. Volunteers never
receive clinical fields; patients never see adult members' details (SEC-AZ-03, SEC-PRV-04).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, select

from app.core import cfg
from app.core.crypto import decrypt
from app.core.db import Conn, T, lat_of, lng_of
from app.core.rbac import Principal, is_case_participant
from app.modules.continuity import service as continuity
from app.modules.onboarding import service as onboarding
from app.modules.reference import serial
from app.modules.routine_care import service as routine
from app.modules.transport import legs as tlegs


def _proj(p: Principal) -> str:
    return {"asha": "asha", "patient": "patient", "volunteer": "volunteer"}.get(p.role, "asha")


async def load(conn: Conn, p: Principal, entity: str, ids: list[str]) -> dict[str, Any]:
    fn = LOADERS.get(entity)
    if fn is None or not ids:
        return {}
    return await fn(conn, p, ids)


async def _villages(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    return {str(v["id"]): v for v in await serial.villages(conn, ids)}


async def _waypoints(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    w = T.village_waypoints
    rows = (await conn.execute(select(w.c.id, w.c.village_id, w.c.kind, w.c.label, w.c.is_default,
                                      lat_of(w.c.location).label("lat"), lng_of(w.c.location).label("lng"))
                               .where(w.c.id.in_(ids)))).all()
    return {str(r.id): {"id": r.id, "villageId": r.village_id, "kind": r.kind, "label": r.label, "isDefault": r.is_default,
                        "lat": float(r.lat), "lng": float(r.lng)} for r in rows}


async def _facilities(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    return {str(f["id"]): f for f in await serial.facilities(conn, ids)}


async def _facility_caps(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    fc = T.facility_capabilities
    out = {}
    for key in ids:
        fid, _, code = key.partition(":")
        r = (await conn.execute(select(fc).where(and_(fc.c.facility_id == fid, fc.c.capability_code == code)))).first()
        if r:
            out[key] = {"facilityId": r.facility_id, "capabilityCode": r.capability_code, "available": r.available}
    return out


async def _catalog_like(conn: Conn, p: Principal, entity: str) -> dict[str, Any]:
    cat = await serial.catalog(conn, p.district_code)
    key = {"capability": "capabilities", "emergency_category": "emergencyCategories", "symptom": "symptoms",
           "risk_rule": "riskRules"}[entity]
    return {str(x.get("code") if entity != "risk_rule" else x["id"]): x for x in cat[key]}


async def _households(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    rows = await onboarding.load_households(conn, ids)
    return {str(h["id"]): h for h in await onboarding.households_out(conn, rows, with_members=False, projection=_proj(p))}


async def _patients(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    rows = await onboarding.patient_rows(conn, ids)
    out = {str(x["id"]): x for x in await onboarding.patients_out(conn, rows, _proj(p))}
    if p.role == "asha":
        for r in rows:  # the ASHA device needs the plain name/phone inside its encrypted Room DB (database.md B6)
            out[str(r.id)]["phone"] = await decrypt(conn, "patient", r.id, "patients", "phone_enc", r.id, r.phone_enc)
    return out


async def _simple(conn: Conn, table: Any, ids: list[str], fn: Any) -> dict[str, Any]:
    rows = (await conn.execute(select(table).where(table.c.id.in_(ids)))).all()
    return {str(r.id): await fn(r) if callable(fn) else fn for r in rows}


async def _cohorts(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    if p.role == "volunteer":
        return {}

    async def f(r: Any) -> dict[str, Any]:
        return {"id": r.id, "patientId": r.patient_id, "cohort": r.cohort, "startedOn": r.started_on, "endedOn": r.ended_on,
                "lmpDate": r.lmp_date, "eddDate": r.edd_date, "version": r.version}
    return await _simple(conn, T.patient_cohorts, ids, f)


async def _conditions(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    if p.role != "asha":
        return {}

    async def f(r: Any) -> dict[str, Any]:
        return {"id": r.id, "patientId": r.patient_id, "conditionCode": r.condition_code, "status": r.status,
                "notedOn": r.noted_on, "note": await decrypt(conn, "patient", r.patient_id, "patient_conditions", "note_enc", r.id, r.note_enc),
                "version": r.version}
    return await _simple(conn, T.patient_conditions, ids, f)


async def _medications(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    if p.role not in ("asha", "patient"):
        return {}

    async def f(r: Any) -> dict[str, Any]:
        return {"id": r.id, "patientId": r.patient_id, "medicineName": r.medicine_name, "doseText": r.dose_text,
                "frequencyText": r.frequency_text, "startedOn": r.started_on, "stoppedOn": r.stopped_on, "version": r.version}
    return await _simple(conn, T.patient_medications, ids, f)


async def _consents(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    if p.role == "volunteer":
        return {}

    async def f(r: Any) -> dict[str, Any]:
        return {"id": r.id, "patientId": r.patient_id, "purpose": r.purpose, "status": r.status, "method": r.method,
                "language": r.language, "noticeVersion": r.notice_version, "effectiveAt": r.effective_at,
                "expiresAt": r.expires_at}
    return await _simple(conn, T.consents, ids, f)


async def _entries(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    if p.role != "asha":
        return {}
    rows = (await conn.execute(select(T.health_record_entries).where(T.health_record_entries.c.id.in_(ids)))).all()
    return {str(e["id"]): e for e in await routine.entries_out(conn, rows)}


async def _attachments(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    if p.role != "asha":
        return {}

    async def f(r: Any) -> dict[str, Any]:
        return {"id": r.id, "patientId": r.patient_id, "entryId": r.entry_id, "caseId": r.case_id, "purpose": r.purpose,
                "contentType": r.content_type, "sizeBytes": r.size_bytes, "uploadStatus": r.upload_status}
    return await _simple(conn, T.attachments, ids, f)


async def _teleconsults(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    if p.role != "asha":
        return {}

    async def f(r: Any) -> dict[str, Any]:
        return {"id": r.id, "patientId": r.patient_id, "reasonCode": r.reason_code, "status": r.status, "mode": r.mode,
                "requestedAt": r.requested_at, "doctorId": r.doctor_id, "outcomeEntryId": r.outcome_entry_id,
                "version": r.version}
    return await _simple(conn, T.teleconsult_sessions, ids, f)


async def _care_plans(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    if p.role not in ("asha", "patient"):
        return {}
    cpi = T.care_plan_items

    async def f(r: Any) -> dict[str, Any]:
        items = (await conn.execute(select(cpi).where(cpi.c.care_plan_id == r.id).order_by(cpi.c.sort_order))).all()
        return {"id": r.id, "patientId": r.patient_id, "caseId": r.case_id, "status": r.status, "nextVisitOn": r.next_visit_on,
                "summary": await decrypt(conn, "patient", r.patient_id, "care_plans", "summary_enc", r.id, r.summary_enc)
                if p.role == "asha" else None, "recordedAt": r.recorded_at,
                "items": [{"id": i.id, "kind": i.kind, "medicineName": i.medicine_name, "doseText": i.dose_text,
                           "frequencyText": i.frequency_text, "dueOffsetDays": i.due_offset_days, "sortOrder": i.sort_order}
                          for i in items]}
    return await _simple(conn, T.care_plans, ids, f)


async def _cases(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    from app.modules.referral import cases, core

    out = {}
    rows = (await conn.execute(core.case_select().where(T.cases.c.id.in_(ids)))).all()
    for r in rows:
        projection = await is_case_participant(conn, p, r)
        if projection is None:
            continue
        data = await cases.case_out(conn, r, projection)
        legs = await tlegs.legs_out(conn, await tlegs.legs_of(conn, r.id), projection=projection, viewer=p.user_id)
        data["legs"] = legs
        if data.get("currentFacility"):
            data["currentFacilityName"] = data["currentFacility"]["name"]
        out[str(r.id)] = data
    return out


async def _case_events(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    if p.role == "volunteer":
        return {}
    ce = T.case_events
    rows = (await conn.execute(select(ce).where(ce.c.id.in_(ids)))).all()
    keep = ("case_created", "status_changed", "pickup_confirmed", "custody_handover") if p.role == "patient" else None
    return {str(r.id): {"id": r.id, "caseId": r.case_id, "action": r.action, "fromStatus": r.from_status,
                        "toStatus": r.to_status, "actorRole": r.actor_role, "occurredAt": r.occurred_at}
            for r in rows if keep is None or r.action in keep}


async def _legs(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    tl = T.transport_legs
    rows = (await conn.execute(tlegs.leg_select().where(tl.c.id.in_(ids)))).all()
    if p.role == "volunteer":
        rows = [r for r in rows if r.custodian_user_id == p.user_id]
    out = {str(x["id"]): x for x in await tlegs.legs_out(conn, rows, projection=_proj(p), viewer=p.user_id)}
    if p.role == "volunteer":
        for r in rows:  # offline handover verification material (receiver key) travels with the leg (SF-01)
            nxt = (await conn.execute(tlegs.leg_select().where(and_(tl.c.case_id == r.case_id,
                                                                     tl.c.leg_order == r.leg_order + 1)))).first()
            if nxt is not None and str(r.id) in out:
                out[str(r.id)]["nextCustodian"] = await tlegs.custodian_view(conn, nxt, with_phone=False)
                out[str(r.id)]["nextLegId"] = nxt.id
    return out


async def _vol_offers(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    vo, tl, c, v = T.volunteer_offers, T.transport_legs, T.cases, T.villages
    rows = (await conn.execute(select(vo, c.c.short_code, c.c.emergency_category, v.c.name.label("village_name"),
                                      tl.c.to_kind).join(tl, tl.c.id == vo.c.leg_id).join(c, c.c.id == tl.c.case_id)
                               .outerjoin(v, v.c.id == c.c.village_id)
                               .where(vo.c.id.in_(ids), vo.c.volunteer_id == p.user_id))).all()
    return {str(r.id): {"id": r.id, "legId": r.leg_id, "caseShortCode": r.short_code, "category": r.emergency_category,
                        "pickupLabel": r.village_name, "destinationLabel": r.to_kind,
                        "distanceM": r.distance_m, "expiresAt": r.expires_at, "status": r.result} for r in rows}


async def _tasks(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    ft = T.follow_up_tasks
    rows = (await conn.execute(select(ft).where(ft.c.id.in_(ids), ft.c.asha_id == p.user_id))).all()
    return {str(t["id"]): t for t in await continuity.tasks_out(conn, rows)}


async def _incentives(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    res = await continuity.my_incentives(conn, p, date.today().replace(day=1))
    return {str(e["id"]): e for e in res["entries"]}


async def _vol_profile(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    vp = T.volunteer_profiles
    r = (await conn.execute(select(vp).where(vp.c.user_id == p.user_id))).first()
    if r is None:
        return {}
    return {str(r.user_id): {"userId": r.user_id, "homeVillageId": r.home_village_id, "available": r.available,
                             "verified": r.verified_at is not None, "version": r.version}}


async def _vehicles(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    v = T.vehicles
    rows = (await conn.execute(select(v).where(v.c.id.in_(ids), v.c.owner_user_id == p.user_id))).all()
    return {str(r.id): {"id": r.id, "kind": r.kind, "seats": r.seats, "active": r.active, "version": r.version} for r in rows}


async def _user_self(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    from app.core.rbac import resolve_scope
    from app.modules.identity.service import user_out

    u = (await conn.execute(select(T.users).where(T.users.c.id == p.user_id))).first()
    return {str(p.user_id): await user_out(conn, u, await resolve_scope(conn, p.user_id))}


async def _config(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    merged = await cfg.merged(conn, p.district_code)
    return {k: {"key": k, "value": merged[k]} for k in merged}


async def _channel_numbers(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    cn = T.channel_numbers
    rows = (await conn.execute(select(cn).where(cn.c.active))).all()
    return {r.number_e164: {"numberE164": r.number_e164, "kind": r.kind, "districtCode": r.district_code} for r in rows}


async def _templates(conn: Conn, p: Principal, ids: list[str]) -> dict[str, Any]:
    mt = T.message_templates
    rows = (await conn.execute(select(mt).where(mt.c.channel == "sms", mt.c.active))).all()
    return {f"{r.code}:{r.language}": {"code": r.code, "language": r.language, "body": r.body} for r in rows}


LOADERS: dict[str, Any] = {
    "village": _villages, "village_waypoint": _waypoints, "facility": _facilities, "facility_capability": _facility_caps,
    "capability": lambda c, p, i: _catalog_like(c, p, "capability"),
    "emergency_category": lambda c, p, i: _catalog_like(c, p, "emergency_category"),
    "symptom": lambda c, p, i: _catalog_like(c, p, "symptom"),
    "risk_rule": lambda c, p, i: _catalog_like(c, p, "risk_rule"),
    "config": _config, "channel_number": _channel_numbers, "message_template": _templates,
    "household": _households, "patient": _patients, "patient_cohort": _cohorts, "patient_condition": _conditions,
    "patient_medication": _medications, "consent": _consents, "health_record_entry": _entries,
    "attachment": _attachments, "teleconsult_session": _teleconsults, "care_plan": _care_plans, "case": _cases,
    "case_event": _case_events, "transport_leg": _legs, "volunteer_offer": _vol_offers, "follow_up_task": _tasks,
    "incentive": _incentives, "volunteer_profile": _vol_profile, "vehicle": _vehicles, "user_self": _user_self,
}


# ---------------- snapshot scope expansion (GET /sync/snapshot) ----------------

async def snapshot_ids(conn: Conn, p: Principal, scope: str) -> list[tuple[str, list[str]]]:
    """(entity, ids) pairs for one scope, in dependency order."""
    kind, _, ident = scope.partition(":")
    out: list[tuple[str, list[str]]] = []
    if kind == "global":
        for ent in ("capability", "emergency_category", "symptom", "risk_rule", "config", "channel_number", "message_template"):
            out.append((ent, ["*"]))
        return out
    if kind == "district":
        f = T.facilities
        fids = [str(r) for r in (await conn.execute(select(f.c.id).where(f.c.district_code == ident, f.c.deleted_at.is_(None)))).scalars()]
        out.append(("facility", fids))
        return out
    if kind == "block":
        v = T.villages
        vids = [str(r) for r in (await conn.execute(select(v.c.id).where(v.c.block_code == ident, v.c.deleted_at.is_(None)))).scalars()]
        out.append(("village", vids))
        return out
    h, pt = T.households, T.patients
    if kind == "village":
        hh = [str(r) for r in (await conn.execute(select(h.c.id).where(h.c.village_id == ident, h.c.deleted_at.is_(None)))).scalars()]
    elif kind == "household":
        hh = [ident]
    else:
        hh = []
    if hh:
        pids = [str(r) for r in (await conn.execute(select(pt.c.id).where(pt.c.household_id.in_(hh), pt.c.deleted_at.is_(None)))).scalars()]
        out.append(("household", hh))
        out.append(("patient", pids))
        for ent, table in (("patient_cohort", T.patient_cohorts), ("patient_condition", T.patient_conditions),
                           ("patient_medication", T.patient_medications), ("consent", T.consents),
                           ("teleconsult_session", T.teleconsult_sessions), ("care_plan", T.care_plans)):
            ids = [str(r) for r in (await conn.execute(select(table.c.id).where(table.c.patient_id.in_(pids)))).scalars()] if pids else []
            out.append((ent, ids))
        if p.role == "asha" and pids:
            e = T.health_record_entries
            out.append(("health_record_entry", [str(r) for r in (await conn.execute(
                select(e.c.id).where(e.c.patient_id.in_(pids)))).scalars()]))
        c = T.cases
        since = datetime.now(UTC) - timedelta(days=90)
        cond = c.c.household_id.in_(hh) if kind == "household" else c.c.village_id == ident
        cids = [str(r) for r in (await conn.execute(select(c.c.id).where(cond, c.c.created_at > since))).scalars()]
        out.append(("case", cids))
        if cids:
            ce = T.case_events
            out.append(("case_event", [str(r) for r in (await conn.execute(select(ce.c.id).where(ce.c.case_id.in_(cids)))).scalars()]))
    if kind == "user" and ident == str(p.user_id):
        ft, vo, tl, veh = T.follow_up_tasks, T.volunteer_offers, T.transport_legs, T.vehicles
        if p.role == "asha":
            out.append(("follow_up_task", [str(r) for r in (await conn.execute(select(ft.c.id).where(
                ft.c.asha_id == p.user_id, ft.c.status.in_(("open", "done"))))).scalars()]))
        if p.role == "volunteer":
            out.append(("volunteer_profile", [str(p.user_id)]))
            out.append(("vehicle", [str(r) for r in (await conn.execute(select(veh.c.id).where(veh.c.owner_user_id == p.user_id))).scalars()]))
            out.append(("volunteer_offer", [str(r) for r in (await conn.execute(select(vo.c.id).where(
                vo.c.volunteer_id == p.user_id, vo.c.result == "pending"))).scalars()]))
            lids = [str(r) for r in (await conn.execute(select(tl.c.id).where(tl.c.custodian_user_id == p.user_id, tl.c.status.in_(
                ("accepted", "picked_up"))))).scalars()]
            out.append(("transport_leg", lids))
            case_ids = [str(r) for r in (await conn.execute(select(tl.c.case_id).where(tl.c.id.in_(lids)))).scalars()] if lids else []
            out.append(("case", case_ids))
        if p.role in ("volunteer", "asha"):
            out.append(("incentive", ["*"]))
        out.append(("user_self", [str(p.user_id)]))
    return out
