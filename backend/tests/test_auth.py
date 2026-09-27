"""TC-SEC-09/10: OTP login, no existence oracle, lockout, refresh rotation + reuse detection."""

from __future__ import annotations

import uuid

from tests.helpers import API, last_otp, login


async def test_otp_login_staff_and_me(client):
    t = await login(client, "asha1")
    assert t["user"]["role"] == "asha"
    assert t["user"]["name"] == "Sunita Devi"
    assert t["user"]["phoneMasked"].startswith("+91 90")
    r = await client.get(f"{API}/me", headers={"Authorization": f"Bearer {t['token']}"})
    assert r.status_code == 200
    assert len(r.json()["villageIds"]) == 3


async def test_unknown_phone_same_response_no_sms(client):
    before = (await client.get(f"{API}/dev/sms/outbox")).json()["data"]
    r = await client.post(f"{API}/auth/otp/request", json={"phone": "+919999999990", "purpose": "login"})
    assert r.status_code == 200 and r.json()["sent"] is True  # SEC-ID-03: identical body
    after = (await client.get(f"{API}/dev/sms/outbox")).json()["data"]
    assert len(after) == len(before)  # no SMS pumping for unknown numbers


async def test_wrong_otp_then_lock(client):
    r = await client.post(f"{API}/auth/otp/request", json={"phone": "+919000000044", "purpose": "login"})
    cid = r.json()["challengeId"]
    otp = await last_otp(client, "044")
    wrong = "000000" if otp != "000000" else "111111"
    dev = {"id": str(uuid.uuid4()), "platform": "android"}
    for i in range(4):
        r = await client.post(f"{API}/auth/otp/verify", json={"challengeId": cid, "otp": wrong, "device": dev, "role": "volunteer"})
        assert r.status_code == 401 and r.json()["code"] == "OTP_INVALID"
    r = await client.post(f"{API}/auth/otp/verify", json={"challengeId": cid, "otp": wrong, "device": dev, "role": "volunteer"})
    assert r.status_code == 429 and r.json()["code"] == "OTP_LOCKED"
    # even the right code is refused while locked
    r = await client.post(f"{API}/auth/otp/verify", json={"challengeId": cid, "otp": otp, "device": dev, "role": "volunteer"})
    assert r.status_code == 429


async def test_refresh_rotation_and_reuse_revokes_family(client):
    t = await login(client, "vol3")
    first = t["refresh"]
    r = await client.post(f"{API}/auth/refresh", json={"refreshToken": first, "deviceId": t["deviceId"]})
    assert r.status_code == 200
    second = r.json()["refreshToken"]
    assert second != first
    r = await client.post(f"{API}/auth/refresh", json={"refreshToken": first, "deviceId": t["deviceId"]})
    assert r.status_code == 401 and r.json()["code"] == "REFRESH_REUSED"
    r = await client.post(f"{API}/auth/refresh", json={"refreshToken": second, "deviceId": t["deviceId"]})
    assert r.status_code == 401  # whole family revoked (TC-SEC-10)


async def test_problem_json_and_request_id(client):
    r = await client.get(f"{API}/me")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["code"] == "UNAUTHENTICATED" and body["requestId"] == r.headers["x-request-id"]
