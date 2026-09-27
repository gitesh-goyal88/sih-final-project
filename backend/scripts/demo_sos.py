"""Demo helper: log in as the demo family (OTP read from the fake SMS outbox) and raise an SOS for Kamla.

    python scripts/demo_sos.py [http://localhost:8000]
"""

from __future__ import annotations

import sys
import uuid

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000") + "/api/v1"
KAMLA = str(uuid.uuid5(uuid.UUID("5d1d3c1e-6a7b-4c8d-9e0f-aa7a7a7a7a01"), "demo:patient:kamla"))

with httpx.Client(timeout=30) as c:
    ch = c.post(f"{BASE}/auth/otp/request", json={"phone": "+919812345678", "purpose": "login"}).json()["challengeId"]
    otp = next(m["text"].split()[1] for m in c.get(f"{BASE}/dev/sms/outbox").json()["data"] if "AapatMitra code" in m["text"])
    tok = c.post(f"{BASE}/auth/otp/verify", json={"challengeId": ch, "otp": otp, "role": "patient",
                                                  "device": {"id": str(uuid.uuid4()), "platform": "android"}}).json()["accessToken"]
    case_id = str(uuid.uuid4())
    r = c.post(f"{BASE}/sos", headers={"Authorization": f"Bearer {tok}", "Idempotency-Key": case_id},
               json={"caseId": case_id, "idempotencyKey": case_id, "patientId": KAMLA, "category": "pregnancy",
                     "pickup": {"lat": 27.6206, "lng": 77.4101, "accuracyM": 20}, "locationSource": "gps"})
    print(r.status_code, r.json())
