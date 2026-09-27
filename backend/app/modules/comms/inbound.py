"""Inbound SMS / IVR processing (TRD §7.2, API-Guide §10.2–10.4, SEC-CH-01..07).

1. Store the raw message (encrypted) in `inbound_messages` and answer the provider fast (< 2 s).
2. Process asynchronously: parse → resolve sender and patient → act → write processing columns once.

Verification levels (SEC-CH-04, SF-13): `code` (valid patient short code for the district) or `tag`
(app-composed SMS from a registered number) = verified; `number_only` = registered number, no code —
verified but flagged; unknown sender without code = `none` (verified = false): facility matching starts,
volunteer dispatch is held for a human decision (SEC-CH-02). Nothing is ever dropped (SECURITY §1.1).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core import redis as rds
from app.core.crypto import decrypt, encrypt, normalise_phone, phone_hash
from app.core.db import Conn, T, engine, lat_of, lng_of
from app.core.ids import uuid5, uuid7
from app.core.logging import log, security_event
from app.core.rbac import Principal, resolve_scope
from app.core.uow import uow
from app.integrations.router import haversine_m
from app.integrations.sms import InboundMessage
from app.modules.comms import parser
from app.modules.comms.notify import send_sms

UNVERIFIED_SENDER_DAILY_CAP = 3
STATUS_WORDS = {"created": "Request received", "matched": "Hospital found", "accepted": "Hospital said YES",
                "transport_assigned": "Driver is coming", "in_transit": "On the way to hospital",
                "arrived_seen": "Reached hospital", "closed": "Treatment done", "follow_up": "Next visit soon",
                "cancelled": "Request cancelled"}  # patient wording, API-Guide Appendix E


async def store(msg: InboundMessage, signature_valid: bool) -> uuid.UUID | None:
    """Raw first, parse later. Returns the row id, or None for a replay / unknown DID."""
    try:
        if not await rds.r().set(rds.k(f"sms:seen:{msg.provider}:{msg.provider_message_id}"), "1", nx=True, ex=86400):
            return None  # fast replay reject; the unique index is the real guard
    except Exception:  # noqa: BLE001, S110
        pass
    im, cn = T.inbound_messages, T.channel_numbers
    rid = uuid7()
    async with uow() as tx:
        to = normalise_phone(msg.to)
        if (await tx.conn.execute(select(cn.c.number_e164).where(cn.c.number_e164 == to))).first() is None:
            log.warning("inbound_unknown_did")
            return None
        frm = normalise_phone(msg.from_)
        res = await tx.conn.execute(pg_insert(im).values(
            id=rid, channel=msg.channel, provider=msg.provider, provider_message_id=msg.provider_message_id,
            to_number=to,
            from_phone_enc=await encrypt(tx.conn, "external_contact", rid, "inbound_messages", "from_phone_enc", rid, frm),
            from_phone_hash=phone_hash(frm),
            body_enc=await encrypt(tx.conn, "external_contact", rid, "inbound_messages", "body_enc", rid, msg.body)
            if msg.body else None,
            dtmf=msg.dtmf, provider_timestamp=msg.provider_timestamp, signature_valid=signature_valid)
            .on_conflict_do_nothing())
        if not res.rowcount:
            return None
    return rid


async def process(inbound_id: Any) -> None:
    im, cn = T.inbound_messages, T.channel_numbers
    async with engine().connect() as conn:
        row = (await conn.execute(select(im, cn.c.district_code).join(cn, cn.c.number_e164 == im.c.to_number)
                                  .where(im.c.id == inbound_id))).first()
        if row is None or row.processed_at is not None:
            return
        frm = await decrypt(conn, "external_contact", row.id, "inbound_messages", "from_phone_enc", row.id, row.from_phone_enc)
        body = await decrypt(conn, "external_contact", row.id, "inbound_messages", "body_enc", row.id, row.body_enc)
    parsed = parser.parse(body) if row.channel == "sms" else parser.Parsed("sos", "parsed", category="other")
    if row.channel == "ivr_keypress":
        parsed = parser.Parsed("ivr_category", "parsed", digit=int(row.dtmf) if (row.dtmf or "").isdigit() else None)
    outcome: str = "ignored"
    result: dict[str, Any] = {}
    try:
        if not row.signature_valid:
            outcome = "rejected_signature"
        elif parsed.intent == "sos":
            outcome, result = await _sos(row, frm or "", parsed)
        elif parsed.intent == "ride_reply":
            outcome, result = await _ride_reply(row, frm or "", parsed)
        elif parsed.intent == "status_query":
            outcome = await _status_query(row, frm or "", parsed)
        elif parsed.intent == "bed_update":
            outcome = await _bed_update(row, frm or "", parsed)
        elif parsed.intent == "other":
            outcome = await _village_reply(row, frm or "", parsed)
    except Exception as exc:  # noqa: BLE001 - leave unprocessed for the retry sweeper
        log.error("inbound_process_failed", error=repr(exc))
        return
    async with uow() as tx:
        await tx.conn.execute(update(im).where(and_(im.c.id == inbound_id, im.c.processed_at.is_(None))).values(
            processed_at=datetime.now(UTC), parse_status=parsed.status,
            intent=parsed.intent if parsed.intent in ("sos", "ride_reply", "bed_update", "status_query", "stop", "other")
            else "ivr_category", parsed=parsed.as_json(), outcome=outcome,
            sender_user_id=result.get("userId"), sender_household_id=result.get("householdId"),
            case_id=result.get("caseId"), leg_id=result.get("legId")))


async def _sender(conn: Conn, ph: bytes) -> tuple[Any, Any]:
    """(user row or None, household id or None) for a phone hash."""
    u, h, pt = T.users, T.households, T.patients
    user = (await conn.execute(select(u).where(u.c.phone_hash == ph, u.c.deleted_at.is_(None), u.c.status == "active")
                               .order_by((u.c.role == "patient").desc()).limit(1))).first()
    hh = (await conn.execute(select(h.c.id).where(h.c.registered_phone_hash == ph, h.c.deleted_at.is_(None)).limit(1))).scalar()
    if hh is None:
        hh = (await conn.execute(select(pt.c.household_id).where(pt.c.phone_hash == ph, pt.c.deleted_at.is_(None)).limit(1))).scalar()
    if hh is None and user is not None and user.household_id:
        hh = user.household_id
    return user, hh


async def _sos(row: Any, frm: str, p: parser.Parsed) -> tuple[str, dict[str, Any]]:
    from app.modules.referral import cases

    ph = bytes(row.from_phone_hash)
    pt, h, v = T.patients, T.households, T.villages
    async with engine().connect() as conn:
        user, household = await _sender(conn, ph)
        patient_id = None
        code_valid = False
        if p.short_code:
            prow = (await conn.execute(select(pt.c.id, pt.c.household_id, v.c.district_code)
                                       .join(h, h.c.id == pt.c.household_id).join(v, v.c.id == h.c.village_id)
                                       .where(pt.c.short_code == p.short_code, pt.c.deleted_at.is_(None)))).first()
            if prow is not None and prow.district_code == row.district_code:
                patient_id, household, code_valid = prow.id, prow.household_id, True
        if patient_id is None and household is not None and user is not None and user.patient_id:
            patient_id = user.patient_id
        registered = user is not None or household is not None
        unverified_today = (await conn.execute(select(func.count()).select_from(T.inbound_messages).where(and_(
            T.inbound_messages.c.from_phone_hash == ph, T.inbound_messages.c.outcome == "unverified_case",
            T.inbound_messages.c.received_at > datetime.now(UTC) - timedelta(days=1))))).scalar() or 0
    if row.channel == "ivr_missed_call" and not registered:
        security_event("sms.unverified_sos", district=row.district_code, channel="ivr")
        return "ignored", {}  # TH-07: unregistered missed calls go to a human (block duty number), not auto-cases
    if code_valid:
        level, verified = ("tag" if p.tag and registered else "code"), True
    elif registered:
        level, verified = ("tag" if p.tag else "number_only"), True
    else:
        level, verified = "none", False
    if not verified and unverified_today >= UNVERIFIED_SENDER_DAILY_CAP:
        security_event("sms.abuse_cap_hit", district=row.district_code)
    if not verified:
        security_event("sms.unverified_sos", district=row.district_code)
    key = uuid5(f"{row.provider}:{row.provider_message_id}")
    raised = (user.id, user.role) if user is not None else None
    async with rds.case_lock(str(key), wait_s=15):
        async with uow() as tx:
            channel = "ivr" if row.channel.startswith("ivr") else "sms"
            case, dedup = await cases.create_sos(
                tx, case_id=key, idempotency_key=key, category=p.category or "other", patient_id=patient_id,
                household_id=household, pickup={"lat": p.lat, "lng": p.lng} if p.lat is not None else None,
                location_source="gps" if p.lat is not None else None, channel=channel, tag=p.tag,
                verified=verified, verification_level=level, raised_by=raised, fallback_district=row.district_code,
                dedupe_channel="sms")
            if dedup:
                from app.modules.referral import core

                await core.event(tx, case.id, "sms_attached", {"verification": level}, channel="sms")
            lang = user.preferred_language if user is not None else "hi"
            template = "sos_app_ack" if p.tag and verified else ("sos_received" if verified else "sos_unverified")
            await send_sms(tx.conn, to=frm, template=template, language=lang, params={"case": case.short_code},
                           purpose="case_status", case_id=case.id)
    outcome = "attached_existing" if dedup else ("new_case" if verified else "unverified_case")
    return outcome, {"caseId": case.id, "userId": user.id if user else None, "householdId": household}


async def _principal_for(conn: Conn, user: Any) -> Principal:
    scope = await resolve_scope(conn, user.id)
    return Principal(user_id=user.id, role=user.role, status=user.status, device_id=None, jti="sms",
                     villages=scope["villages"], household_id=scope["household_id"], facility_ids=scope["facility_ids"],
                     district_code=scope["district_code"], block_code=scope["block_code"],
                     home_village_id=scope.get("home_village_id"), purpose="emergency_care")


async def _ride_reply(row: Any, frm: str, p: parser.Parsed) -> tuple[str, dict[str, Any]]:
    """SEC-CH-07: must come from the offered volunteer's number and reference the offer's case code."""
    from app.core.errors import AppError
    from app.modules.transport import legs as tlegs

    u, vo, tl, c = T.users, T.volunteer_offers, T.transport_legs, T.cases
    ph = bytes(row.from_phone_hash)
    async with engine().connect() as conn:
        vol = (await conn.execute(select(u).where(u.c.phone_hash == ph, u.c.role == "volunteer", u.c.status == "active"))).first()
        if vol is None:
            return "ignored", {}
        q = (select(vo.c.id, vo.c.leg_id, tl.c.case_id, c.c.short_code).join(tl, tl.c.id == vo.c.leg_id)
             .join(c, c.c.id == tl.c.case_id).where(vo.c.volunteer_id == vol.id, vo.c.result == "pending"))
        if p.case_code:
            q = q.where(c.c.short_code == p.case_code)
        offers = (await conn.execute(q)).all()
        principal = await _principal_for(conn, vol)
    if len(offers) != 1:
        return "ignored", {"userId": vol.id}
    offer = offers[0]
    if p.digit == 2:
        async with uow(principal) as tx:
            await tlegs.decline_leg(tx, offer.case_id, offer.leg_id, offer.id, channel="sms")
        return "leg_declined", {"userId": vol.id, "caseId": offer.case_id, "legId": offer.leg_id}
    try:
        async with rds.case_lock(str(offer.case_id), wait_s=15):
            async with uow(principal) as tx:
                await tlegs.accept_leg(tx, offer.case_id, offer.leg_id, offer_id=offer.id, vehicle_id=None,
                                       location=None, channel="sms")
                await send_sms(tx.conn, to=frm, template="ride_confirmed", language=vol.preferred_language,
                               params={"case": offer.short_code}, purpose="ride_request", case_id=offer.case_id,
                               recipient_user_id=vol.id)
        return "leg_accepted", {"userId": vol.id, "caseId": offer.case_id, "legId": offer.leg_id}
    except AppError:
        async with uow() as tx:
            await send_sms(tx.conn, to=frm, template="ride_taken", language=vol.preferred_language,
                           params={"case": offer.short_code}, purpose="ride_request", case_id=offer.case_id,
                           recipient_user_id=vol.id)
        return "ignored", {"userId": vol.id, "caseId": offer.case_id}


async def _status_query(row: Any, frm: str, p: parser.Parsed) -> str:
    c, u, h, ava = T.cases, T.users, T.households, T.asha_village_assignments
    ph = bytes(row.from_phone_hash)
    async with uow() as tx:
        case = (await tx.conn.execute(select(c).where(c.c.short_code == (p.case_code or "")))).first()
        if case is None:
            return "ignored"
        hh_match = case.household_id is not None and (await tx.conn.execute(select(h.c.id).where(and_(
            h.c.id == case.household_id, h.c.registered_phone_hash == ph)))).first() is not None
        asha = (await tx.conn.execute(select(u.c.id).join(ava, ava.c.asha_id == u.c.id).where(and_(
            u.c.phone_hash == ph, u.c.role == "asha", ava.c.village_id == case.village_id, ava.c.assigned_to.is_(None))))).first()
        if not hh_match and asha is None:
            return "ignored"
        await send_sms(tx.conn, to=frm, template="status_reply", language="hi",
                       params={"case": case.short_code, "status": STATUS_WORDS.get(case.status, case.status)},
                       purpose="case_status", case_id=case.id)
    return "replied_status"


async def _bed_update(row: Any, frm: str, p: parser.Parsed) -> str:
    from app.modules.referral.matching import refresh_facility_cache

    f = T.facilities
    ph = bytes(row.from_phone_hash)
    async with uow() as tx:
        fac = (await tx.conn.execute(select(f.c.id, f.c.district_code, f.c.block_code).where(
            f.c.duty_phone_hash == ph, f.c.deleted_at.is_(None)))).first()
        if fac is None:
            return "ignored"
        values: dict[str, Any] = {"capability_updated_at": datetime.now(UTC)}
        if p.beds is not None:
            values["beds_available"] = p.beds
            values["status"] = "open" if p.beds > 0 else "full"
        if p.facility_status:
            values["status"] = p.facility_status
        await tx.conn.execute(update(f).where(f.c.id == fac.id).values(**values))
        await tx.journal("facility", fac.id, [f"district:{fac.district_code}"] + ([f"block:{fac.block_code}"] if fac.block_code else []))
        await tx.audit("facility.sms_update", "facility", fac.id, diff={"fields": sorted(values)}, purpose="administration")
        tx.after_commit(lambda: refresh_facility_cache(fac.id))
    return "facility_updated"


async def _village_reply(row: Any, frm: str, p: parser.Parsed) -> str:
    """Unverified sender replies with a village name that matches the coordinates → verified (SEC-CH-02)."""
    from app.modules.referral import commands, core

    if not p.text:
        return "ignored"
    im, c, v = T.inbound_messages, T.cases, T.villages
    ph = bytes(row.from_phone_hash)
    async with engine().connect() as conn:
        case_id = (await conn.execute(select(im.c.case_id).join(c, c.c.id == im.c.case_id).where(and_(
            im.c.from_phone_hash == ph, c.c.verified.is_(False), c.c.status.in_(core.OPEN),
            c.c.created_at > datetime.now(UTC) - timedelta(minutes=30))).order_by(im.c.received_at.desc()).limit(1))).scalar()
        if case_id is None:
            return "ignored"
        vill = (await conn.execute(select(v.c.id, lat_of(v.c.location).label("lat"), lng_of(v.c.location).label("lng"))
                                   .where(and_(func.lower(v.c.name) == p.text.strip().lower(),
                                               v.c.district_code == row.district_code)))).first()
    if vill is None:
        return "ignored"
    async with rds.case_lock(str(case_id), wait_s=15):
        async with uow() as tx:
            case = await core.require_case(tx, case_id)
            if case.pickup_lat is not None and haversine_m((case.pickup_lat, case.pickup_lng), (vill.lat, vill.lng)) > 10000:
                return "ignored"
            values: dict[str, Any] = {"verified": True, "verified_at": datetime.now(UTC), "verification_level": "callback",
                                      "village_id": vill.id}
            case = await core.touch_case(tx, case, values)
            await core.event(tx, case.id, "case_verified", {"method": "village_match"}, channel="sms")
            await core.resolve_escalations(tx, case.id, "called_family", ("unverified_sms",))
            await commands._release_dispatch(tx, case)
    return "attached_existing"


async def retry_unprocessed() -> None:
    im = T.inbound_messages
    async with engine().connect() as conn:
        ids = (await conn.execute(select(im.c.id).where(and_(im.c.processed_at.is_(None),
                                                             im.c.received_at < datetime.now(UTC) - timedelta(seconds=30)))
                                  .limit(50))).scalars().all()
    for i in ids:
        await process(i)


