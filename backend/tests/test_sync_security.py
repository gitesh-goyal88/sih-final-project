"""Sync protocol (TC-SYNC), RBAC/IDOR (TC-SEC-01/02), mass assignment (TC-SEC-03), idempotency (TC-SEC-15),
screening risk rules (US3), referral override (FR-M03), DB invariants (R15, C5, I4), handover lockout (TC-SEC-04)."""

from __future__ import annotations

import asyncio
import gzip
import json

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.db import T, engine
from app.core.ids import uuid5, uuid7
from tests.helpers import API, h, login

KAMLA = str(uuid5("demo:patient:kamla"))
NAGLA = str(uuid5("demo:village:Nagla"))
SANKET_HH = str(uuid5("demo:household:Sanket:0"))


async def _bootstrap(client, who: str, scopes: str) -> tuple[str, list[dict]]:
    r = await client.get(f"{API}/sync/snapshot", headers=await h(client, who), params={"scopes": scopes})
    assert r.status_code == 200, r.text
    raw = r.content
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    lines = [json.loads(x) for x in raw.decode().splitlines() if x.strip()]
    assert lines[0]["type"] == "meta" and lines[-1]["type"] == "end"
    return lines[0]["cursor"], lines[1:-1]


async def test_offline_household_registration_sync_and_pull(client, fresh):
    t = await login(client, "asha1")
    cursor, rows = await _bootstrap(client, "asha1", f"village:{NAGLA},user:{t['user']['id']},global")
    assert any(r["entity"] == "household" for r in rows)
    kamla = next(r for r in rows if r["entity"] == "patient" and r["id"] == KAMLA)
    assert kamla["data"]["name"].startswith("Kamla") and kamla["data"]["shortCode"]

    hid, pid = str(uuid7()), str(uuid7())
    ops = [
        # P3 routine op listed first — the server still runs the P0 SOS first
        {"opId": str(uuid7()), "priority": 3, "entity": "household", "op": "create", "id": hid, "hlc": "1727251800000:0001:dev7",
         "data": {"id": hid, "villageId": NAGLA, "houseNumber": "77", "recordedAt": "2026-09-25T09:00:00Z",
                  "members": [{"id": pid, "name": "Radha Test", "sex": "F", "dateOfBirth": "1999-02-01"}],
                  "consents": [{"id": str(uuid7()), "patientId": pid, "purpose": "continuity_of_care", "status": "granted",
                                "method": "verbal_witnessed", "language": "hi", "noticeVersion": "cc-2026-09",
                                "effectiveAt": "2026-09-25T09:00:00Z"}]}},
        {"opId": str(uuid7()), "priority": 2, "entity": "health_record_entry", "op": "create", "hlc": "1727251900000:0001:dev7",
         "data": {"id": str(uuid7()), "patientId": KAMLA, "kind": "screening", "vitals": {"spo2Pct": 91},
                  "deviceRiskFlags": ["any_spo2_low"], "recordedAt": "2026-09-25T09:05:00Z"}},
        # mass assignment attempt (SF-08): household_id / shortCode are never client-writable
        {"opId": str(uuid7()), "priority": 3, "entity": "patient", "op": "update", "id": KAMLA, "hlc": "1727252000000:0000:dev7",
         "fields": {"shortCode": "ZZZZZZ", "householdId": SANKET_HH}},
    ]
    r = await client.post(f"{API}/sync", headers=await h(client, "asha1"), json={"cursor": cursor, "ops": ops,
                                                                                  "scopes": [f"village:{NAGLA}"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert ops[0]["opId"] in body["applied"] and ops[1]["opId"] in body["applied"]
    assert body["assigned"]["patientShortCodes"][pid]  # short code assigned on first sync (FR-D02)
    assert body["results"][ops[1]["opId"]]["highRisk"] is True  # server re-evaluates, server wins
    rej = {x["opId"]: x for x in body["rejected"]}
    assert rej[ops[2]["opId"]]["code"] == "FIELD_NOT_WRITABLE" and rej[ops[2]["opId"]]["retry"] is False
    # replaying the same ops is idempotent
    r2 = await client.post(f"{API}/sync", headers=await h(client, "asha1"), json={"cursor": body["cursor"], "ops": ops[:2]})
    assert set(r2.json()["applied"]) == {ops[0]["opId"], ops[1]["opId"]}
    # a different device pulls the new household via the commit-safe cursor
    r3 = await client.post(f"{API}/sync", headers=await h(client, "asha1"), json={"cursor": cursor, "ops": []})
    changed = {(c["entity"], c["id"]) for c in r3.json()["changes"]}
    assert ("household", hid) in changed and ("patient", pid) in changed
    # the high-risk flag created an urgent recheck task on the ASHA's list
    tasks = (await client.get(f"{API}/tasks", headers=await h(client, "asha1"))).json()["data"]
    assert any(t["taskType"] == "high_risk_recheck" and t["priority"] == "urgent" for t in tasks)


async def test_field_level_lww_conflict(client, fresh):
    t = await login(client, "asha1")
    cursor, _ = await _bootstrap(client, "asha1", f"user:{t['user']['id']}")
    newer = {"opId": str(uuid7()), "priority": 3, "entity": "patient", "op": "update", "id": KAMLA,
             "hlc": "1900000000000:0000:devB", "fields": {"bloodGroup": "B+"}}
    older = {"opId": str(uuid7()), "priority": 3, "entity": "patient", "op": "update", "id": KAMLA,
             "hlc": "1727000000000:0000:devA", "fields": {"bloodGroup": "O+"}}
    r = await client.post(f"{API}/sync", headers=await h(client, "asha1"), json={"cursor": cursor, "ops": [newer]})
    assert r.status_code == 200
    r = await client.post(f"{API}/sync", headers=await h(client, "asha1"), json={"cursor": cursor, "ops": [older]})
    conflicts = r.json()["conflicts"]
    assert conflicts and conflicts[0]["fields"] == ["bloodGroup"] and conflicts[0]["resolution"] == "server_won"


async def test_idor_and_role_boundaries(client, fresh):
    # ASHA 2 cannot read ASHA 1's patient (404, existence not confirmed)
    r = await client.get(f"{API}/patients/{KAMLA}/record", headers=await h(client, "asha2"))
    assert r.status_code == 404
    # volunteers never read clinical records (TC-SEC-02)
    r = await client.get(f"{API}/patients/{KAMLA}/record", headers=await h(client, "vol1"))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ROLE"
    # ASHA 1 can, and the read is audited
    r = await client.get(f"{API}/patients/{KAMLA}/record", headers=await h(client, "asha1"))
    assert r.status_code == 200 and r.json()["summary"]["highRisk"] is True
    # ASHA 2 cannot sync-request ASHA 1's village scope
    t2 = await login(client, "asha2")
    cursor, _ = await _bootstrap(client, "asha2", f"user:{t2['user']['id']}")
    r = await client.post(f"{API}/sync", headers=await h(client, "asha2"), json={"cursor": cursor, "ops": [],
                                                                                  "scopes": [f"village:{NAGLA}"]})
    assert any(x.get("scope") == f"village:{NAGLA}" and x["code"] == "FORBIDDEN_SCOPE" for x in r.json()["rejected"])
    # facility staff can't see a patient without a case at their facility
    r = await client.get(f"{API}/patients/{KAMLA}/record", headers=await h(client, "staff_dh"))
    assert r.status_code == 404
    # unknown fields are rejected, never ignored (SEC-API-01)
    r = await client.post(f"{API}/sos", headers=await h(client, "family"), json={
        "caseId": str(uuid7()), "idempotencyKey": str(uuid7()), "category": "injury", "verified": True})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_FAILED"


async def test_idempotency_replay_mismatch_and_other_actor(client, fresh):
    key = str(uuid7())
    tid = str(uuid7())
    body = {"id": tid, "patientId": KAMLA, "taskType": "bp_check", "dueDate": "2026-10-01"}
    r1 = await client.post(f"{API}/tasks", headers=await h(client, "asha1", key=key), json=body)
    r2 = await client.post(f"{API}/tasks", headers=await h(client, "asha1", key=key), json=body)
    assert r1.status_code == 201 and r2.status_code == 201 and r2.headers.get("idempotent-replayed") == "true"
    assert "patientName" not in r2.json()  # stored replay bodies carry no names (SF-12)
    r3 = await client.post(f"{API}/tasks", headers=await h(client, "asha1", key=key), json={**body, "dueDate": "2026-10-02"})
    assert r3.status_code == 422 and r3.json()["code"] == "IDEMPOTENCY_MISMATCH"
    r4 = await client.post(f"{API}/tasks", headers=await h(client, "asha2", key=key), json=body)
    assert r4.status_code != 201 or r4.headers.get("idempotent-replayed") is None  # another actor never gets a replay


async def test_screening_consent_and_referral_override(client, fresh):
    # a high-risk screening creates an urgent task with the rule's follow-up days
    r = await client.post(f"{API}/patients/{KAMLA}/screenings", headers=await h(client, "asha1"), json={
        "id": str(uuid7()), "vitals": {"bpSystolic": 150, "bpDiastolic": 96}, "recordedAt": "2026-09-25T07:10:00Z"})
    assert r.status_code == 201, r.text
    assert r.json()["highRisk"] and "referral" in r.json()["suggestedActions"]
    # physiologically impossible vitals are rejected with the range (TC: BP 1500)
    r = await client.post(f"{API}/patients/{KAMLA}/screenings", headers=await h(client, "asha1"), json={
        "id": str(uuid7()), "vitals": {"bpSystolic": 1500, "bpDiastolic": 96}})
    assert r.status_code == 422 and r.json()["fields"][0]["max"] == 280
    # referral preview: DH is stale so CHC ranks first; picking DH needs an override reason (FR-M03)
    m = (await client.get(f"{API}/facilities/match", headers=await h(client, "asha1"),
                          params={"patientId": KAMLA, "needs": "obstetric_emergency"})).json()
    assert m["data"][0]["facility"]["name"] == "CHC Barsana" and m["data"][1]["reasons"]["stale"] is True
    dh = m["data"][1]["facility"]["id"]
    body = {"id": str(uuid7()), "patientId": KAMLA, "neededCapabilities": ["obstetric_emergency"],
            "referralReason": "BP 150/96 at 36 weeks", "preferredFacilityId": dh}
    r = await client.post(f"{API}/cases", headers=await h(client, "asha1"), json=body)
    assert r.status_code == 422 and r.json()["code"] == "OVERRIDE_REASON_REQUIRED"
    r = await client.post(f"{API}/cases", headers=await h(client, "asha1"),
                          json={**body, "overrideReason": "Family has relatives near DH Mathura"})
    assert r.status_code == 201, r.text
    detail = r.json()
    assert detail["case"]["status"] == "matched" and detail["offers"][0]["source"] == "override"


async def test_database_invariants(client, fresh):
    case_id = str(uuid7())
    await client.post(f"{API}/sos", headers=await h(client, "family", key=case_id), json={
        "caseId": case_id, "idempotencyKey": case_id, "patientId": KAMLA, "category": "pregnancy"})
    async with engine().connect() as conn:
        # R15: the trigger rejects an illegal edge even from SQL
        with pytest.raises(DBAPIError) as err:
            async with conn.begin():
                await conn.execute(update(T.cases).where(T.cases.c.id == case_id).values(status="closed"))
        assert "illegal case transition" in str(err.value)
    async with engine().connect() as conn:
        # C5: the app role cannot rewrite the timeline
        with pytest.raises(DBAPIError):
            async with conn.begin():
                await conn.execute(update(T.case_events).where(T.case_events.c.case_id == case_id).values(action="x"))
    async with engine().connect() as conn:
        # the app writes the audit trail but cannot read it back (database.md §10.2)
        with pytest.raises(DBAPIError):
            async with conn.begin():
                await conn.execute(text("SELECT count(*) FROM audit_log"))


async def test_parallel_volunteer_accept_one_winner(client, fresh):
    case_id = str(uuid7())
    await client.post(f"{API}/sos", headers=await h(client, "family", key=case_id), json={
        "caseId": case_id, "idempotencyKey": case_id, "patientId": KAMLA, "category": "injury"})
    o1 = (await client.get(f"{API}/volunteers/me/offers", headers=await h(client, "vol1"))).json()["data"][0]
    o2 = (await client.get(f"{API}/volunteers/me/offers", headers=await h(client, "vol2"))).json()["data"][0]
    r1, r2 = await asyncio.gather(
        client.post(f"{API}/cases/{case_id}/legs/{o1['legId']}/accept", headers=await h(client, "vol1"), json={"offerId": o1["offerId"]}),
        client.post(f"{API}/cases/{case_id}/legs/{o2['legId']}/accept", headers=await h(client, "vol2"), json={"offerId": o2["offerId"]}))
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], (r1.text, r2.text)


async def test_handover_bad_codes_lock_after_five(client, fresh):
    case_id = str(uuid7())
    await client.post(f"{API}/sos", headers=await h(client, "family", key=case_id), json={
        "caseId": case_id, "idempotencyKey": case_id, "patientId": KAMLA, "category": "pregnancy"})
    o1 = (await client.get(f"{API}/volunteers/me/offers", headers=await h(client, "vol1"))).json()["data"][0]
    leg1 = o1["legId"]
    assert (await client.post(f"{API}/cases/{case_id}/legs/{leg1}/accept", headers=await h(client, "vol1"),
                              json={"offerId": o1["offerId"]})).status_code == 200
    detail = (await client.get(f"{API}/cases/{case_id}", headers=await h(client, "asha1"))).json()
    leg2 = next(x for x in detail["legs"] if x["legOrder"] == 2)["id"]
    r = await client.post(f"{API}/cases/{case_id}/commands", headers=await h(client, "asha1"), json={
        "command": "AssignAmbulance", "args": {"legId": leg2, "mode": "jssk", "driverName": "Driver", "driverPhone": "+919811100000"}})
    assert r.status_code == 200, r.text
    assert (await client.post(f"{API}/cases/{case_id}/commands", headers=await h(client, "vol1"),
                              json={"command": "ConfirmPickup", "args": {"legId": leg1}})).status_code == 200
    outbox = (await client.get(f"{API}/dev/sms/outbox")).json()["data"]
    real = next(m["text"] for m in outbox if "handover code" in m["text"] or "handover code" in m["text"].lower())
    assert "+919811100000" in [m["to"] for m in outbox]  # the code goes to the receiver only (SEC-CUS-03)
    for i in range(4):
        r = await client.post(f"{API}/cases/{case_id}/legs/{leg1}/handover", headers=await h(client, "vol1"),
                              json={"method": "sms_code", "code": "000000"})
        assert r.status_code == 422 and r.json()["code"] == "HANDOVER_CODE_INVALID", r.text
    r = await client.post(f"{API}/cases/{case_id}/legs/{leg1}/handover", headers=await h(client, "vol1"),
                          json={"method": "sms_code", "code": "000000"})
    assert r.status_code == 423 and r.json()["code"] == "HANDOVER_LOCKED"
    assert real  # the real code exists, but even it is refused once locked
    async with engine().connect() as conn:
        esc = (await conn.execute(select(T.case_escalations.c.note).where(T.case_escalations.c.case_id == case_id))).scalars().all()
    assert "handover_locked" in esc
