"""PushGateway adapter (TRD §3.5, API-Guide §13.2).

Data messages only, IDs only — no names, no conditions (SEC-CH-06, TRD §14.4).
* `FakePush`: in-memory recorder (Redis list) visible at `GET /dev/push-log`.
* `FcmPush`: FCM HTTP v1 with a service-account OAuth token (service-account JSON from Vault).
"""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime
from typing import Protocol

import httpx
import jwt

from app.core import redis as rds


class PushGateway(Protocol):
    name: str

    async def send(self, tokens: list[str], data: dict[str, str]) -> list[str]: ...


class FakePush:
    name = "fake"
    LOG = "dev:push:log"

    async def send(self, tokens: list[str], data: dict[str, str]) -> list[str]:
        ids = []
        for t in tokens:
            mid = f"fakepush-{time.time_ns()}"
            await rds.r().lpush(rds.k(self.LOG), json.dumps({"id": mid, "token": t[-6:], "data": data,
                                                             "at": datetime.now(UTC).isoformat()}))
            ids.append(mid)
        await rds.r().ltrim(rds.k(self.LOG), 0, 499)
        return ids


class FcmPush:
    name = "fcm"
    TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint, not a secret

    def __init__(self, service_account_json_b64: str) -> None:
        self.sa = json.loads(base64.b64decode(service_account_json_b64))
        self._token: tuple[float, str] | None = None

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if self._token and self._token[0] > time.time() + 60:
            return self._token[1]
        now = int(time.time())
        assertion = jwt.encode({"iss": self.sa["client_email"], "scope": "https://www.googleapis.com/auth/firebase.messaging",
                                "aud": self.TOKEN_URL, "iat": now, "exp": now + 3600},
                               self.sa["private_key"], algorithm="RS256")
        resp = await client.post(self.TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                                       "assertion": assertion})
        resp.raise_for_status()
        body = resp.json()
        self._token = (time.time() + int(body.get("expires_in", 3600)), body["access_token"])
        return self._token[1]

    async def send(self, tokens: list[str], data: dict[str, str]) -> list[str]:
        ids: list[str] = []
        async with httpx.AsyncClient(timeout=10) as client:
            bearer = await self._access_token(client)
            for t in tokens:
                resp = await client.post(
                    f"https://fcm.googleapis.com/v1/projects/{self.sa['project_id']}/messages:send",
                    headers={"Authorization": f"Bearer {bearer}"},
                    json={"message": {"token": t, "android": {"priority": "high", "ttl": "120s"}, "data": data}})
                if resp.status_code == 200:
                    ids.append(resp.json().get("name", ""))
        return ids


_gateway: PushGateway | None = None


def gateway() -> PushGateway:
    global _gateway
    if _gateway is None:
        import os

        from app.core.config import settings

        if settings.push_provider == "fcm":
            _gateway = FcmPush(os.environ["AM_FCM_SERVICE_ACCOUNT_B64"])
        else:
            _gateway = FakePush()
    return _gateway
