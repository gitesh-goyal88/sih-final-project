"""Test helpers: OTP login through the fake SMS outbox, device keys, idempotency headers, sweep driving."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.ids import uuid7

API = "/api/v1"
TOKENS: dict[str, dict[str, Any]] = {}
KEYS: dict[str, Ed25519PrivateKey] = {}

STAFF = {"admin": "ADM-0915-01", "asha1": "ASHA-0915-001", "asha2": "ASHA-0915-002", "doctor": "DOC-0915-01",
         "staff_chc": "FAC-CHC-01", "staff_dh": "FAC-DH-01"}
PHONES = {"vol1": "+919000000041", "vol2": "+919000000042", "vol3": "+919000000043", "vol4": "+919000000044",
          "family": "+919812345678"}
ROLE_OF = {"vol1": "volunteer", "vol2": "volunteer", "vol3": "volunteer", "vol4": "volunteer", "family": "patient"}


async def last_otp(client: httpx.AsyncClient, phone_suffix: str | None = None) -> str:
    out = (await client.get(f"{API}/dev/sms/outbox", params={"limit": 20})).json()["data"]
    for m in out:
        if "AapatMitra code" in m["text"] and (phone_suffix is None or m["to"].endswith(phone_suffix)):
            return m["text"].split()[1]
    raise AssertionError("no OTP in outbox")


def device_key(who: str) -> Ed25519PrivateKey:
    if who not in KEYS:
        KEYS[who] = Ed25519PrivateKey.generate()
    return KEYS[who]


def spki_b64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(serialization.Encoding.DER,
                                                          serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


async def login(client: httpx.AsyncClient, who: str, *, platform: str = "android") -> dict[str, Any]:
    if who in TOKENS:
        return TOKENS[who]
    body: dict[str, Any] = {"purpose": "login"}
    if who in STAFF:
        body["staffId"] = STAFF[who]
    else:
        body["phone"] = PHONES[who]
    r = await client.post(f"{API}/auth/otp/request", json=body)
    assert r.status_code == 200, r.text
    challenge = r.json()["challengeId"]
    otp = await last_otp(client)
    device_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"device-{who}"))
    verify = {"challengeId": challenge, "otp": otp,
              "device": {"id": device_id, "platform": platform, "appVersion": "1.0.0+1", "model": "test",
                         "publicKeyEd25519": spki_b64(device_key(who)), "fcmToken": f"fcm-{who}"}}
    if who in ROLE_OF:
        verify["role"] = ROLE_OF[who]
    r = await client.post(f"{API}/auth/otp/verify", json=verify)
    assert r.status_code == 200, r.text
    data = r.json()
    TOKENS[who] = {"token": data["accessToken"], "deviceId": device_id, "user": data["user"],
                   "refresh": data.get("refreshToken")}
    return TOKENS[who]


async def h(client: httpx.AsyncClient, who: str, *, key: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    t = await login(client, who)
    headers = {"Authorization": f"Bearer {t['token']}", "X-Device-Id": t["deviceId"],
               "Idempotency-Key": key or str(uuid7())}
    headers.update(extra or {})
    return headers


async def sweep_now(case_id: Any | None = None, kinds: tuple[str, ...] | None = None) -> int:
    """Move matching live timers into the past and run the ADR-02 sweep."""
    from sqlalchemy import and_, func, update

    from app.core.db import T, engine
    from app.workers.tasks import sweep_timers

    ct = T.case_timers
    cond = and_(ct.c.fired_at.is_(None), ct.c.cancelled_at.is_(None))
    if case_id is not None:
        cond = and_(cond, ct.c.case_id == case_id)
    if kinds:
        cond = and_(cond, ct.c.kind.in_(kinds))
    async with engine().begin() as conn:
        await conn.execute(update(ct).where(cond).values(due_at=datetime(2000, 1, 1, tzinfo=UTC)))
        if kinds is None or "offer_timeout" in kinds:
            fo = T.facility_offers
            oc = fo.c.result == "pending" if case_id is None else and_(fo.c.result == "pending", fo.c.case_id == case_id)
            await conn.execute(update(fo).where(oc).values(offered_at=func.now() - timedelta(minutes=5),
                                                           expires_at=func.now() - timedelta(seconds=1)))
        if kinds is None or "volunteer_round_timeout" in kinds:
            vo = T.volunteer_offers
            await conn.execute(update(vo).where(vo.c.result == "pending").values(
                offered_at=func.now() - timedelta(minutes=5), expires_at=func.now() - timedelta(seconds=1)))
    return await sweep_timers()


def signed_handover(receiver: str, receiver_user_id: str, receiver_device_id: str, leg_id: str, next_leg_id: str,
                    gps: dict[str, float] | None = None, ts: datetime | None = None, nonce: str | None = None) -> dict[str, Any]:
    payload = {"v": 1, "legId": leg_id, "nextLegId": next_leg_id, "receiverUserId": receiver_user_id,
               "receiverDeviceId": receiver_device_id, "nonce": nonce or base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("="),
               "ts": (ts or datetime.now(UTC)).isoformat()}
    if gps:
        payload["gps"] = gps
    assertion = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = device_key(receiver).sign(assertion.encode())
    return {"method": "signed_qr", "assertion": assertion,
            "signature": base64.urlsafe_b64encode(sig).decode().rstrip("="), "location": gps}
