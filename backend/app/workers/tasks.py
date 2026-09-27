"""Task catalog (database.md §16.3). Every handler is idempotent: it re-checks state before acting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, select, text, update

from app.core import redis as rds
from app.core.crypto import decrypt
from app.core.db import T, engine
from app.core.logging import log
from app.core.uow import uow
from app.integrations import objectstore
from app.modules.comms import inbound
from app.modules.comms.notify import household_contact, push_to_user, send_sms, user_contact, ws_publish
from app.modules.continuity import service as continuity
from app.modules.referral import cascade, core, matching, timers
from app.modules.transport import legs as tlegs
from app.workers.registry import task

FAMILY_SMS = {"accepted": "family_hospital_yes", "transport_assigned": "family_accepted", "closed": "family_closed"}


# ---------------- emergency queue ----------------

@task("match_and_offer", "emergency")
async def match_and_offer(case_id: str) -> None:
    await cascade.match_and_offer(case_id)


@task("offer_timeout", "emergency")
async def offer_timeout(offer_id: str) -> None:
    await cascade.offer_timeout(offer_id)


@task("find_volunteer", "emergency")
async def find_volunteer(leg_id: str, round_no: int | None = None) -> None:
    await tlegs.find_volunteer(leg_id, round_no)


@task("volunteer_round_timeout", "emergency")
async def volunteer_round_timeout(leg_id: str) -> None:
    await tlegs.round_timeout(leg_id)


@task("process_inbound", "emergency")
async def process_inbound(inbound_id: str) -> None:
    await inbound.process(inbound_id)


@task("sweep_timers", "emergency")
async def sweep_timers() -> int:
    """Backstop every 10 s (ADR-02): claim due timers with SKIP LOCKED and run their handlers."""
    async with uow() as tx:
        due = await timers.claim_due(tx)
    for t in due:
        await _fire(t.case_id, t.kind, t.ref_id)
    await inbound.retry_unprocessed()
    return len(due)


@task("fire_timer", "emergency")
async def fire_timer(timer_id: str) -> None:
    """Fast path from the Celery countdown; the sweep covers anything this misses."""
    async with uow() as tx:
        t = await timers.claim_one(tx, timer_id)
    if t is not None:
        await _fire(t.case_id, t.kind, t.ref_id)


async def _fire(case_id: Any, kind: str, ref_id: Any) -> None:
    try:
        if kind == "offer_timeout":
            await cascade.offer_timeout(ref_id)
        elif kind == "offer_sms_nudge":
            await _offer_nudge(ref_id)
        elif kind == "volunteer_round_timeout":
            await tlegs.round_timeout(ref_id)
        elif kind == "stage_sla":
            await _stage_sla(case_id, ref_id)
        elif kind == "close_reminder":
            await _close_reminder(case_id)
        elif kind == "closure_escalation":
            await _simple_escalation(case_id, "closure_overdue", ("arrived_seen",))
        elif kind == "custodian_check":
            await _custodian_check(case_id, ref_id)
    except Exception as exc:  # noqa: BLE001 - never let one timer stop the sweep
        log.error("timer_handler_failed", kind=kind, error=repr(exc))


async def _offer_nudge(offer_id: Any) -> None:
    """Rung 2 for facilities: SMS to the duty phone if the offer is still pending and unopened after 60 s."""
    o, f, c = T.facility_offers, T.facilities, T.cases
    async with uow() as tx:
        row = (await tx.conn.execute(select(o.c.id, o.c.case_id, o.c.result, o.c.opened_at, o.c.offered_at, f.c.id.label("fid"),
                                            f.c.duty_phone_enc, c.c.short_code, c.c.emergency_category)
                                     .join(f, f.c.id == o.c.facility_id).join(c, c.c.id == o.c.case_id)
                                     .where(o.c.id == offer_id))).first()
        if row is None or row.result != "pending" or row.opened_at is not None or row.duty_phone_enc is None:
            return
        phone = await decrypt(tx.conn, "external_contact", row.fid, "facilities", "duty_phone_enc", row.fid, row.duty_phone_enc)
        letter = (await tx.conn.execute(select(T.emergency_categories.c.sms_letter).where(
            T.emergency_categories.c.code == row.emergency_category))).scalar() or "R"
        minutes = max(1, int((datetime.now(UTC) - row.offered_at).total_seconds() // 60))
        await send_sms(tx.conn, to=phone, template="facility_offer_nudge", language="en",
                       params={"case": row.short_code, "cat": letter, "min": minutes}, purpose="facility_offer",
                       case_id=row.case_id, recipient_facility_id=row.fid)
        await core.event(tx, row.case_id, "notification_ladder_step", {"rung": "sms", "offerId": str(offer_id)},
                         offer_id=offer_id, system=True)


async def _stage_sla(case_id: Any, ref_id: Any) -> None:
    async with rds.case_lock(str(case_id), wait_s=15):
        async with uow() as tx:
            case = await core.load_case(tx, case_id, lock=True)
            if case is None:
                return
            if ref_id is None:
                if case.status == "created":
                    await core.escalate(tx, case, "platform_fault")  # created → matched > 30 s (TRD §5.2)
                    from app.workers.registry import enqueue

                    tx.after_commit(lambda: enqueue("match_and_offer", case_id=case_id))
                return
            if str(ref_id) == str(timers.acceptance_ref(case_id)):
                if case.status in ("created", "matched"):
                    await core.escalate(tx, case, "cascade_exhausted")
                return
            leg = await tlegs.load_leg(tx.conn, ref_id)
            if leg is not None and leg.status == "picked_up" and case.status in ("in_transit", "matched", "accepted",
                                                                                   "transport_assigned"):
                await core.event(tx, case.id, "leg_eta_breach", {"legOrder": leg.leg_order}, leg_id=leg.id, system=True)
                await core.escalate(tx, case, "leg_sla_breach")
                await timers.schedule(tx, case.id, "custodian_check", 1, leg.id)


async def _custodian_check(case_id: Any, leg_id: Any) -> None:
    """'Are you OK?' prompt to the custodian (TRD §5.2, R-09)."""
    async with uow() as tx:
        leg = await tlegs.load_leg(tx.conn, leg_id)
        if leg is None or leg.status != "picked_up" or leg.custodian_user_id is None:
            return
        await push_to_user(tx.conn, leg.custodian_user_id, {"t": "custodian.check", "caseId": str(case_id),
                                                            "legId": str(leg_id)}, purpose="reminder", case_id=case_id)


async def _close_reminder(case_id: Any) -> None:
    async with uow() as tx:
        case = await core.load_case(tx, case_id)
        if case is None or case.status != "arrived_seen" or case.current_facility_id is None:
            return
        await ws_publish(f"facility:{case.current_facility_id}", {"type": "case.close_reminder", "caseId": str(case_id),
                                                                  "shortCode": case.short_code})
        await core.event(tx, case_id, "notification_ladder_step", {"rung": "close_reminder"}, system=True)


async def _simple_escalation(case_id: Any, reason: str, statuses: tuple[str, ...]) -> None:
    async with rds.case_lock(str(case_id), wait_s=15):
        async with uow() as tx:
            case = await core.load_case(tx, case_id, lock=True)
            if case is not None and case.status in statuses:
                await core.escalate(tx, case, reason)


# ---------------- notifications ----------------

async def _family_users(conn: Any, household_id: Any) -> list[Any]:
    if not household_id:
        return []
    u = T.users
    return list((await conn.execute(select(u.c.id).where(u.c.household_id == household_id, u.c.status == "active"))).scalars())


@task("notify_sos", "emergency")
async def notify_sos(case_id: str) -> None:
    """T1: tell the household's ASHA and the family app that the request was received."""
    async with uow() as tx:
        case = await core.load_case(tx, case_id)
        if case is None:
            return
        data = {"t": "case.status_changed", "caseId": str(case.id), "status": case.status}
        asha = await tlegs.household_asha(tx.conn, case.household_id)
        if asha is None and case.village_id:
            ava = T.asha_village_assignments
            asha = (await tx.conn.execute(select(ava.c.asha_id).where(ava.c.village_id == case.village_id,
                                                                      ava.c.assigned_to.is_(None)).limit(1))).scalar()
        for uid in [asha, *await _family_users(tx.conn, case.household_id)]:
            if uid and uid != case.raised_by_id:
                await push_to_user(tx.conn, uid, data, purpose="case_status", case_id=case.id)


@task("notify_case_status", "default")
async def notify_case_status(case_id: str, status: str, version: int = 0, finding: bool = False) -> None:
    """Family, ASHA and custodians get a nudge; SMS-only families get the plain-language SMS (UI §8, §14)."""
    async with uow() as tx:
        case = await core.load_case(tx, case_id)
        if case is None:
            return
        data = {"t": "case.status_changed", "caseId": str(case.id), "status": case.status}
        recipients = set(await _family_users(tx.conn, case.household_id))
        asha = await tlegs.household_asha(tx.conn, case.household_id)
        if asha:
            recipients.add(asha)
        for leg in await tlegs.legs_of(tx.conn, case.id):
            if leg.custodian_user_id and leg.status in ("accepted", "picked_up"):
                recipients.add(leg.custodian_user_id)
        for uid in recipients:
            await push_to_user(tx.conn, uid, data, purpose="case_status", case_id=case.id)
        template = "family_finding" if finding else FAMILY_SMS.get(status)
        if template is None or not case.household_id:
            return
        has_app = bool(await _family_users(tx.conn, case.household_id))
        if has_app and case.channel not in ("sms", "ivr"):
            return  # the app shows the live case card; SMS is for families without data
        phone, lang = await household_contact(tx.conn, case.household_id)
        if phone is None:
            return
        params: dict[str, Any] = {"case": case.short_code}
        if case.current_facility_id:
            params["facility"] = (await tx.conn.execute(select(T.facilities.c.name).where(
                T.facilities.c.id == case.current_facility_id))).scalar()
        if template == "family_accepted":
            leg1 = next((x for x in await tlegs.legs_of(tx.conn, case.id) if x.leg_order == 1), None)
            cust = await tlegs.custodian_view(tx.conn, leg1, with_phone=True) if leg1 else None
            if not cust:
                template = "family_hospital_yes"
            else:
                params.update(driver=cust.get("firstName") or "", driver_phone=cust.get("phone") or "",
                              eta=max(1, round((leg1.eta_seconds or 900) / 60)))
        await send_sms(tx.conn, to=phone, template=template, language=lang, params=params, purpose="case_status",
                       case_id=case.id)


@task("notify_facility_offer", "emergency")
async def notify_facility_offer(offer_id: str) -> None:
    o, fm = T.facility_offers, T.facility_memberships
    async with uow() as tx:
        offer = (await tx.conn.execute(select(o).where(o.c.id == offer_id))).first()
        if offer is None or offer.result != "pending":
            return
        members = (await tx.conn.execute(select(fm.c.user_id).where(fm.c.facility_id == offer.facility_id,
                                                                    fm.c.active_to.is_(None)))).scalars().all()
        for uid in members:
            await push_to_user(tx.conn, uid, {"t": "offer.created", "caseId": str(offer.case_id), "offerId": str(offer.id)},
                               purpose="facility_offer", case_id=offer.case_id)


@task("notify_ride_offer", "emergency")
async def notify_ride_offer(offer_id: str) -> None:
    """Push + ring; the ride SMS goes at once to volunteers not live in the app (UI §6.4 'also sent by SMS')."""
    vo, tl, c, v = T.volunteer_offers, T.transport_legs, T.cases, T.villages
    async with uow() as tx:
        row = (await tx.conn.execute(select(vo, tl.c.case_id, c.c.short_code, c.c.emergency_category, v.c.name.label("village"))
                                     .join(tl, tl.c.id == vo.c.leg_id).join(c, c.c.id == tl.c.case_id)
                                     .outerjoin(v, v.c.id == c.c.village_id).where(vo.c.id == offer_id))).first()
        if row is None or row.result != "pending":
            return
        await push_to_user(tx.conn, row.volunteer_id, {"t": "leg.offered", "caseId": str(row.case_id), "legId": str(row.leg_id),
                                                       "offerId": str(row.id), "exp": row.expires_at.isoformat()},
                           purpose="ride_request", case_id=row.case_id)
        try:
            live = bool(await rds.r().exists(rds.k(f"presence:user:{row.volunteer_id}")))
        except Exception:  # noqa: BLE001
            live = False
        if not live:
            phone, lang = await user_contact(tx.conn, row.volunteer_id)
            if phone:
                await send_sms(tx.conn, to=phone, template="ride_offer", language=lang,
                               params={"case": row.short_code, "village": row.village or "",
                                       "km": round((row.distance_m or 0) / 1000)},
                               purpose="ride_request", case_id=row.case_id, recipient_user_id=row.volunteer_id)
                await tx.conn.execute(update(vo).where(vo.c.id == row.id).values(sms_sent_at=datetime.now(UTC)))


@task("notify_escalation", "emergency")
async def notify_escalation(escalation_id: str) -> None:
    e, u, c = T.case_escalations, T.users, T.cases
    async with uow() as tx:
        esc = (await tx.conn.execute(select(e, c.c.short_code).join(c, c.c.id == e.c.case_id).where(e.c.id == escalation_id))).first()
        if esc is None or esc.resolved_at is not None:
            return
        admins = (await tx.conn.execute(select(u.c.id).where(u.c.role == "district_admin", u.c.status == "active",
                                                             u.c.district_code == esc.district_code))).scalars().all()
        for uid in admins:
            await push_to_user(tx.conn, uid, {"t": "escalation.opened", "caseId": str(esc.case_id),
                                              "escalationId": str(esc.id)}, purpose="escalation", case_id=esc.case_id)
            phone, lang = await user_contact(tx.conn, uid)
            if phone and esc.reason not in ("platform_fault",):
                await send_sms(tx.conn, to=phone, template="escalation_admin", language=lang,
                               params={"case": esc.short_code, "reason": esc.reason}, purpose="escalation",
                               case_id=esc.case_id, recipient_user_id=uid)
        if esc.reason in ("volunteer_exhausted", "unverified_sms"):
            asha = await tlegs.household_asha(tx.conn, (await core.load_case(tx, esc.case_id)).household_id)
            if asha:
                await push_to_user(tx.conn, asha, {"t": "escalation.opened", "caseId": str(esc.case_id)},
                                   purpose="escalation", case_id=esc.case_id)


# ---------------- default queue ----------------

@task("create_follow_ups", "default")
async def create_follow_ups(case_id: str) -> None:
    await continuity.create_follow_ups(case_id)


@task("verify_attachment", "default")
async def verify_attachment(attachment_id: str) -> None:
    """HEAD, size and magic bytes against the declared type before `verified` (SF-16). AV scan hook: TODO pilot."""
    magic = {"image/jpeg": [b"\xff\xd8\xff"], "image/webp": [b"RIFF"], "audio/ogg": [b"OggS"],
             "audio/mp4": [b"\x00\x00\x00"], "audio/amr": [b"#!AMR"], "application/pdf": [b"%PDF"]}
    a = T.attachments
    async with uow() as tx:
        row = (await tx.conn.execute(select(a).where(a.c.id == attachment_id))).first()
        if row is None or row.upload_status != "uploaded":
            return
        head = objectstore.head(row.bucket, row.object_key)
        ok = head is not None and (row.size_bytes is None or int(head.get("ContentLength", -1)) == row.size_bytes)
        if ok:
            prefix = objectstore.read_prefix(row.bucket, row.object_key)
            ok = any(prefix.startswith(m) for m in magic.get(row.content_type, []))
        await tx.conn.execute(update(a).where(a.c.id == attachment_id).values(upload_status="verified" if ok else "rejected"))
        if not ok:
            from app.core.logging import security_event

            security_event("upload.rejected", attachment_id=str(attachment_id))


@task("warm_redis", "default")
async def warm_redis() -> None:
    await matching.warm_redis()


@task("reevaluate_risk", "default")
async def reevaluate_risk(entry_id: str) -> None:
    """Server evaluation already runs inline on insert (TRD §12A); kept for rule-set republish backfills."""
    return None


# ---------------- bulk queue ----------------

@task("recompute_leaderboards", "bulk")
async def recompute_leaderboards() -> None:
    await continuity.recompute_leaderboards()


@task("refresh_materialized_views", "bulk")
async def refresh_materialized_views() -> None:
    async with engine("worker").begin() as conn:
        await conn.execute(text("SELECT am_refresh_materialized_views()"))


@task("ensure_partitions", "bulk")
async def ensure_partitions() -> None:
    async with engine("worker").begin() as conn:
        await conn.execute(text("SELECT am_ensure_partitions(4, 3)"))


@task("purge_expired", "bulk")
async def purge_expired() -> None:
    """Retention (database.md §12): idempotency 7 d, OTP 90 d, sync journal 35 d — 5 000-row batches."""
    async with engine("worker").begin() as conn:
        ik, oc, sc = T.idempotency_keys, T.otp_challenges, T.sync_changes
        await conn.execute(delete(ik).where(ik.c.expires_at < datetime.now(UTC)))
        await conn.execute(delete(oc).where(oc.c.requested_at < datetime.now(UTC) - timedelta(days=90)))
        await conn.execute(delete(sc).where(sc.c.changed_at < datetime.now(UTC) - timedelta(days=35)))


@task("mark_missed_tasks", "bulk")
async def mark_missed_tasks() -> None:
    """Nightly: open tasks overdue by more than 7 days become 'missed' (fut_overdue_idx)."""
    ft = T.follow_up_tasks
    async with uow() as tx:
        await tx.conn.execute(update(ft).where(and_(ft.c.status == "open",
                                                    ft.c.due_date < func.current_date() - 7)).values(status="missed"))


