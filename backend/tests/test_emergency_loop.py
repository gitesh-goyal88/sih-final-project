"""API-Guide §15.1 / TRD §18 E2E: online SOS → facility accepts → volunteer relay → arrival → closure → follow-up.

Demo geography: Nagla has a junction and a roadhead → 3 legs (house→junction→roadhead→facility).
Pregnancy needs `obstetric_emergency`: PHC lacks it, CHC Barsana is fresh, DH Mathura is stale by 14 h →
CHC is offered first. Two Nagla volunteers race for leg 1 ("Already taken").
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.core.db import T, engine
from app.core.ids import uuid5, uuid7
from tests.helpers import API, h, login, signed_handover

KAMLA = str(uuid5("demo:patient:kamla"))
HOUSEHOLD = str(uuid5("demo:household:Nagla:0"))
CHC = str(uuid5("demo:facility:chc"))


async def _custodians(case_id: str) -> int:
    tl = T.transport_legs
    async with engine().connect() as conn:
        return (await conn.execute(select(func.count()).select_from(tl).where(tl.c.case_id == case_id,
                                                                              tl.c.status == "picked_up"))).scalar()


async def _case(client: Any, who: str, case_id: str) -> dict[str, Any]:
    r = await client.get(f"{API}/cases/{case_id}", headers=await h(client, who))
    assert r.status_code == 200, r.text
    return r.json()


async def test_full_emergency_loop(client, fresh):
    case_id = str(uuid7())
    r = await client.post(f"{API}/sos", headers=await h(client, "family", key=case_id), json={
        "caseId": case_id, "idempotencyKey": case_id, "patientId": KAMLA, "householdId": HOUSEHOLD,
        "category": "pregnancy", "pickup": {"lat": 27.6206, "lng": 77.4101, "accuracyM": 20}, "locationSource": "gps",
        "recordedAt": "2026-09-25T10:00:00Z"})
    assert r.status_code == 202, r.text
    sos = r.json()
    assert sos["status"] == "created" and len(sos["shortCode"]) == 6 and sos["deduplicated"] is False

    # replay with the same key → same case, no second case (I12)
    r2 = await client.post(f"{API}/sos", headers=await h(client, "family", key=case_id), json={
        "caseId": case_id, "idempotencyKey": case_id, "patientId": KAMLA, "householdId": HOUSEHOLD,
        "category": "pregnancy", "pickup": {"lat": 27.6206, "lng": 77.4101, "accuracyM": 20}, "locationSource": "gps",
        "recordedAt": "2026-09-25T10:00:00Z"})
    assert r2.status_code == 202 and r2.json()["caseId"] == case_id

    detail = await _case(client, "family", case_id)
    assert detail["case"]["status"] == "matched"
    assert detail["offers"] == []  # patients never see facility decisions (UI §14)
    assert [x["legOrder"] for x in detail["legs"]] == [1, 2, 3]

    # facility inbox shows the CHC offer, with a countdown from server time
    staff = await h(client, "staff_chc")
    inbox = (await client.get(f"{API}/facilities/{CHC}/offers", headers=staff)).json()["data"]
    assert len(inbox) == 1
    offer = inbox[0]
    assert offer["case"]["id"] == case_id and offer["patient"]["firstName"] == "Kamla"
    assert offer["offer"]["staleCapability"] is False
    offer_id = offer["offer"]["id"]
    assert (await client.post(f"{API}/cases/{case_id}/offers/{offer_id}/opened", headers=staff)).status_code == 204

    # both Nagla volunteers were rung for leg 1 — coarse area only, no pin before accept (SEC-PRV-03)
    offers1 = (await client.get(f"{API}/volunteers/me/offers", headers=await h(client, "vol1"))).json()["data"]
    offers2 = (await client.get(f"{API}/volunteers/me/offers", headers=await h(client, "vol2"))).json()["data"]
    assert len(offers1) == 1 and len(offers2) == 1
    assert "lat" not in str(offers1[0]["area"]) and offers1[0]["area"]["villageName"] == "Nagla"
    leg1 = offers1[0]["legId"]

    r = await client.post(f"{API}/cases/{case_id}/legs/{leg1}/accept", headers=await h(client, "vol1"),
                          json={"offerId": offers1[0]["offerId"]})
    assert r.status_code == 200, r.text
    won = r.json()
    assert won["leg"]["fromPoint"]["lat"] > 27 and won["contacts"]["family"]["phone"]  # exact pin after winning
    r = await client.post(f"{API}/cases/{case_id}/legs/{leg1}/accept", headers=await h(client, "vol2"),
                          json={"offerId": offers2[0]["offerId"]})
    assert r.status_code == 409 and r.json()["code"] == "LEG_ALREADY_TAKEN"
    assert "fromPoint" not in r.text  # the loser never receives the pin

    # facility accepts → accepted → transport_assigned (leg 1 already accepted, T5)
    r = await client.post(f"{API}/cases/{case_id}/offers/{offer_id}/respond", headers=await h(client, "staff_chc"),
                          json={"decision": "accept", "bedsAvailable": 2})
    assert r.status_code == 200, r.text
    assert r.json()["case"]["status"] == "transport_assigned"

    # legs 2 and 3 are searched once the destination is known: vol2 (auto, Nagla) and vol4 (car, Rankauli — 2nd link)
    o2 = (await client.get(f"{API}/volunteers/me/offers", headers=await h(client, "vol2"))).json()["data"]
    o4 = (await client.get(f"{API}/volunteers/me/offers", headers=await h(client, "vol4"))).json()["data"]
    assert len(o2) == 1 and len(o4) == 1
    leg2, leg3 = o2[0]["legId"], o4[0]["legId"]
    assert o4[0]["destination"]["kind"] == "facility"
    assert (await client.post(f"{API}/cases/{case_id}/legs/{leg2}/accept", headers=await h(client, "vol2"),
                              json={"offerId": o2[0]["offerId"]})).status_code == 200
    assert (await client.post(f"{API}/cases/{case_id}/legs/{leg3}/accept", headers=await h(client, "vol4"),
                              json={"offerId": o4[0]["offerId"]})).status_code == 200

    # pickup (T6) → in_transit, exactly one custodian
    r = await client.post(f"{API}/cases/{case_id}/commands", headers=await h(client, "vol1"),
                          json={"command": "ConfirmPickup", "args": {"legId": leg1}})
    assert r.status_code == 200, r.text
    assert r.json()["case"]["status"] == "in_transit"
    assert await _custodians(case_id) == 1

    # handover leg1 → leg2 with vol2's device-signed QR (SF-01); receiver identity comes from the server
    v2 = await login(client, "vol2")
    detail = await _case(client, "vol1", case_id)
    junction = next(x for x in detail["legs"] if x["id"] == leg1)["toPoint"]
    body = signed_handover("vol2", v2["user"]["id"], v2["deviceId"], leg1, leg2, gps=junction)
    r = await client.post(f"{API}/cases/{case_id}/legs/{leg1}/handover", headers=await h(client, "vol1"), json=body)
    assert r.status_code == 200, r.text
    assert r.json()["leg"]["handoverState"] == "verified" and r.json()["nextLeg"]["status"] == "picked_up"
    assert await _custodians(case_id) == 1
    # replaying the same assertion is refused (nonce reuse) — vol1 is no longer custodian anyway
    r = await client.post(f"{API}/cases/{case_id}/legs/{leg1}/handover", headers=await h(client, "vol1"), json=body)
    assert r.status_code == 409

    v4 = await login(client, "vol4")
    detail = await _case(client, "vol2", case_id)
    roadhead = next(x for x in detail["legs"] if x["id"] == leg2)["toPoint"]
    r = await client.post(f"{API}/cases/{case_id}/legs/{leg2}/handover", headers=await h(client, "vol2"),
                          json=signed_handover("vol4", v4["user"]["id"], v4["deviceId"], leg2, leg3, gps=roadhead))
    assert r.status_code == 200, r.text
    assert await _custodians(case_id) == 1

    # the facility sees the handoff packet before arrival (US9) and pre-registers (in-transit registration)
    packet = (await client.get(f"{API}/cases/{case_id}/handoff-packet", headers=await h(client, "staff_chc"))).json()
    assert packet["patient"]["name"].startswith("Kamla")
    assert packet["latestVitals"]["bpSystolic"] == 146 and "preg_bp_high" in packet["riskReasons"]
    assert packet["currentLeg"]["legOrder"] == 3
    r = await client.post(f"{API}/cases/{case_id}/pre-register", headers=await h(client, "staff_chc"),
                          json={"facilityRegNo": "OPD/2026/1182"})
    assert r.status_code == 200 and r.json()["preRegisteredAt"]

    # arrival → seen → close with care plan (T8–T10)
    staff = await h(client, "staff_chc")
    r = await client.post(f"{API}/cases/{case_id}/status", headers=staff, json={"action": "arrived"})
    assert r.status_code == 200, r.text
    assert r.json()["case"]["status"] == "arrived_seen"
    assert await _custodians(case_id) == 0
    r = await client.post(f"{API}/cases/{case_id}/status", headers=await h(client, "staff_chc"), json={"action": "seen"})
    assert r.status_code == 200
    r = await client.post(f"{API}/cases/{case_id}/status", headers=await h(client, "staff_chc"), json={
        "action": "close", "outcome": "treated_discharged",
        "outcomeEntry": {"id": str(uuid7()), "kind": "referral_outcome", "notes": "Normal delivery, mother and baby stable",
                         "vitals": {"bpSystolic": 128, "bpDiastolic": 82}},
        "carePlan": {"id": str(uuid7()), "summary": "PNC follow-up", "nextVisitOn": "2026-09-28",
                     "items": [{"id": str(uuid7()), "kind": "medicine", "medicineName": "IFA", "frequencyText": "1 daily",
                                "durationDays": 90, "sortOrder": 1},
                               {"id": str(uuid7()), "kind": "visit", "dueOffsetDays": 3, "taskType": "pnc_visit", "sortOrder": 2}]}})
    assert r.status_code == 200, r.text

    final = await _case(client, "asha1", case_id)
    assert final["case"]["status"] == "follow_up"
    transitions = [e["toStatus"] for e in final["events"] if e["action"] == "status_changed"]
    assert transitions == ["matched", "accepted", "transport_assigned", "in_transit", "arrived_seen", "closed", "follow_up"]

    # the household's ASHA got follow-up tasks (≥ 1 at +3 days) and the referral_closed credit
    tasks = (await client.get(f"{API}/tasks", headers=await h(client, "asha1"), params={"status": "open"})).json()["data"]
    mine = [t for t in tasks if t["sourceCaseId"] == case_id or t["taskType"] == "pnc_visit"]
    assert {t["taskType"] for t in mine} >= {"referral_followup", "pnc_visit"}
    asha_inc = (await client.get(f"{API}/incentives/me", headers=await h(client, "asha1"))).json()
    assert any(e["kind"] == "referral_closed" for e in asha_inc["entries"])

    # every volunteer who handed over custody is credited only now, after facility-confirmed arrival (SEC-FRD-01)
    for vol in ("vol1", "vol2", "vol4"):
        inc = (await client.get(f"{API}/incentives/me", headers=await h(client, vol))).json()
        assert inc["credits"] == 10 and inc["entries"][0]["state"] == "verified", (vol, inc)
