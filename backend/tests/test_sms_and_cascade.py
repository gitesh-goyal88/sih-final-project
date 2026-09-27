"""API-Guide §15.2 (SMS then app merge), §15.3 (decline cascade + escalation), SEC-CH-02 (unverified hold)."""

from __future__ import annotations

from sqlalchemy import func, select

from app.core.db import T, engine
from app.core.ids import idem_tag, uuid5, uuid7
from tests.helpers import API, h, sweep_now

KAMLA = str(uuid5("demo:patient:kamla"))
HOUSEHOLD = str(uuid5("demo:household:Nagla:0"))
CHC, DH = str(uuid5("demo:facility:chc")), str(uuid5("demo:facility:dh"))
SMS_NUMBER = "+911204567890"


async def _count_cases(patient_id: str) -> int:
    async with engine().connect() as conn:
        return (await conn.execute(select(func.count()).select_from(T.cases).where(T.cases.c.patient_id == patient_id))).scalar()


async def _sms(client, frm: str, body: str) -> None:
    r = await client.post(f"{API}/dev/sms/json", json={"from": frm, "to": SMS_NUMBER, "body": body})
    assert r.status_code == 200 and r.json()["inboundId"], r.text


async def test_sms_sos_then_app_sync_merges_to_one_case(client, fresh):
    key = uuid7()
    tag = idem_tag(key)
    short = (await client.get(f"{API}/households/{HOUSEHOLD}", headers=await h(client, "asha1"))).json()
    kamla_code = next(m["shortCode"] for m in short["members"] if str(m["id"]) == KAMLA)
    await _sms(client, "+919812345678", f"SOS {kamla_code} P 27.62060,77.41010 #{tag}")
    assert await _count_cases(KAMLA) == 1
    outbox = (await client.get(f"{API}/dev/sms/outbox")).json()["data"]
    assert any(m["text"].startswith("AM OK ") for m in outbox)  # app-composed SMS → "AM OK <code>"

    # hours later the app syncs the original CreateSOS with the full key → same case, deduplicated
    r = await client.post(f"{API}/sync", headers=await h(client, "family"), json={
        "deviceId": "x", "cursor": None, "ops": [{"opId": str(key), "priority": 0, "entity": "case", "op": "command",
                                                  "name": "CreateSOS", "hlc": "1727258400000:0001:dev7",
                                                  "payload": {"caseId": str(key), "idempotencyKey": str(key), "patientId": KAMLA,
                                                              "category": "pregnancy", "pickup": {"lat": 27.6206, "lng": 77.4101}}}]})
    assert r.status_code == 410 and r.json()["code"] == "CURSOR_EXPIRED"  # no cursor: must bootstrap first
    r = await client.post(f"{API}/sync", headers=await h(client, "family"), json={
        "cursor": await _cursor(client, "family"),
        "ops": [{"opId": str(key), "priority": 0, "entity": "case", "op": "command", "name": "CreateSOS",
                 "hlc": "1727258400000:0001:dev7",
                 "payload": {"caseId": str(key), "idempotencyKey": str(key), "patientId": KAMLA,
                             "category": "pregnancy", "pickup": {"lat": 27.6206, "lng": 77.4101}}}]})
    assert r.status_code == 200, r.text
    res = r.json()["results"][str(key)]
    assert res["deduplicated"] is True
    assert await _count_cases(KAMLA) == 1  # exactly one case (PRD §2 zero duplicates)


async def _cursor(client, who: str) -> str:
    """Bootstrap: the snapshot's meta line carries the starting cursor."""
    import gzip
    import json

    r = await client.get(f"{API}/sync/snapshot", headers=await h(client, who), params={"scopes": "global"})
    assert r.status_code == 200
    raw = r.content
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    first = json.loads(raw.decode().splitlines()[0])
    assert first["type"] == "meta"
    return first["cursor"]


async def test_unverified_sms_matches_facilities_but_holds_volunteers(client, fresh):
    await _sms(client, "+919877700001", "SOS - I 27.6206,77.4101")
    async with engine().connect() as conn:
        case = (await conn.execute(select(T.cases).where(T.cases.c.verified.is_(False)))).first()
        assert case is not None and case.verification_level == "none"
        offers = (await conn.execute(select(func.count()).select_from(T.facility_offers).where(
            T.facility_offers.c.case_id == case.id))).scalar()
        vol = (await conn.execute(select(func.count()).select_from(T.volunteer_offers).join(
            T.transport_legs, T.transport_legs.c.id == T.volunteer_offers.c.leg_id).where(
            T.transport_legs.c.case_id == case.id))).scalar()
        esc = (await conn.execute(select(T.case_escalations.c.reason).where(T.case_escalations.c.case_id == case.id))).scalars().all()
    assert offers == 1 and vol == 0 and "unverified_sms" in esc  # TC-SEC-08
    async with engine().connect() as conn:
        tpl = (await conn.execute(select(T.notifications.c.template_code).where(
            T.notifications.c.case_id == case.id))).scalars().all()
    assert "sos_unverified" in tpl  # "Reply with your village name. Your ASHA will call you." (hi by default)
    # the sender replies with a village matching the coordinates → verified → dispatch released
    await _sms(client, "+919877700001", "Nagla")
    async with engine().connect() as conn:
        c2 = (await conn.execute(select(T.cases).where(T.cases.c.id == case.id))).first()
        vol = (await conn.execute(select(func.count()).select_from(T.volunteer_offers).join(
            T.transport_legs, T.transport_legs.c.id == T.volunteer_offers.c.leg_id).where(
            T.transport_legs.c.case_id == case.id))).scalar()
    assert c2.verified is True and vol >= 1


async def test_decline_cascade_escalation_and_admin_reassign(client, fresh):
    case_id = str(uuid7())
    r = await client.post(f"{API}/sos", headers=await h(client, "asha1", key=case_id), json={
        "caseId": case_id, "idempotencyKey": case_id, "patientId": KAMLA, "category": "pregnancy",
        "pickup": {"lat": 27.6206, "lng": 77.4101}})
    assert r.status_code == 202
    # 1) CHC declines no_bed → CHC marked full, next offer (DH) immediately (FR-C02/C03)
    inbox = (await client.get(f"{API}/facilities/{CHC}/offers", headers=await h(client, "staff_chc"))).json()["data"]
    r = await client.post(f"{API}/cases/{case_id}/offers/{inbox[0]['offer']['id']}/respond",
                          headers=await h(client, "staff_chc"), json={"decision": "decline", "reason": "no_bed"})
    assert r.status_code == 200, r.text
    fac = (await client.get(f"{API}/facilities/{CHC}", headers=await h(client, "staff_chc"))).json()
    assert fac["status"] == "full"
    dh_inbox = (await client.get(f"{API}/facilities/{DH}/offers", headers=await h(client, "staff_dh"))).json()["data"]
    assert len(dh_inbox) == 1 and dh_inbox[0]["offer"]["staleCapability"] is True  # stale, still offered
    # the family never sees "declined"
    fam = (await client.get(f"{API}/cases/{case_id}", headers=await h(client, "family"))).json()
    assert "declined" not in str(fam["events"]).lower()
    # 2) DH ignores it → timeout via the sweep (ADR-02) → no candidates left → escalation to the district admin
    await sweep_now(case_id, ("offer_timeout",))
    esc = (await client.get(f"{API}/admin/escalations", headers=await h(client, "admin"))).json()["data"]
    mine = [e for e in esc if e["caseId"] == case_id]
    assert mine and mine[0]["reason"] in ("cascade_exhausted", "no_capable_facility")
    # accept after expiry → 410 OFFER_EXPIRED (FR-C04)
    r = await client.post(f"{API}/cases/{case_id}/offers/{dh_inbox[0]['offer']['id']}/respond",
                          headers=await h(client, "staff_dh"), json={"decision": "accept"})
    assert r.status_code == 410 and r.json()["code"] == "OFFER_EXPIRED"
    # 3) admin reassigns to DH with a reason → admin_reassign offer → DH accepts
    r = await client.post(f"{API}/admin/cases/{case_id}/reassign", headers=await h(client, "admin"),
                          json={"facilityId": DH, "reason": "Called DH duty officer, bed confirmed"})
    assert r.status_code == 200, r.text
    dh_inbox = (await client.get(f"{API}/facilities/{DH}/offers", headers=await h(client, "staff_dh"))).json()["data"]
    assert dh_inbox[0]["offer"]["source"] == "admin_reassign"
    r = await client.post(f"{API}/cases/{case_id}/offers/{dh_inbox[0]['offer']['id']}/respond",
                          headers=await h(client, "staff_dh"), json={"decision": "accept"})
    assert r.status_code == 200 and r.json()["case"]["status"] in ("accepted", "transport_assigned")


async def test_decline_other_needs_note_and_wrong_facility_forbidden(client, fresh):
    case_id = str(uuid7())
    await client.post(f"{API}/sos", headers=await h(client, "family", key=case_id), json={
        "caseId": case_id, "idempotencyKey": case_id, "patientId": KAMLA, "category": "pregnancy"})
    inbox = (await client.get(f"{API}/facilities/{CHC}/offers", headers=await h(client, "staff_chc"))).json()["data"]
    oid = inbox[0]["offer"]["id"]
    r = await client.post(f"{API}/cases/{case_id}/offers/{oid}/respond", headers=await h(client, "staff_chc"),
                          json={"decision": "decline", "reason": "other"})
    assert r.status_code == 422
    r = await client.post(f"{API}/cases/{case_id}/offers/{oid}/respond", headers=await h(client, "staff_dh"),
                          json={"decision": "accept"})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_SCOPE"
    r = await client.post(f"{API}/cases/{case_id}/commands", headers=await h(client, "family"),
                          json={"command": "Cancel", "args": {"reason": "false_alarm"}})
    assert r.status_code == 200 and r.json()["case"]["status"] == "cancelled"
    r = await client.post(f"{API}/cases/{case_id}/offers/{oid}/respond", headers=await h(client, "staff_chc"),
                          json={"decision": "accept"})
    assert r.status_code in (409, 410)
