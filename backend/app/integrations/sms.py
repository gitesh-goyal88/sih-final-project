"""SmsGateway adapter (TRD §3.5, API-Guide §11).

* `FakeSms` (local/demo/test): outbound messages go to a Redis list shown at `/dev/sms`; inbound SMS are
  posted by the `/dev/sms` form in the normalised shape — no provider account needed for the demo.
* `TwilioSms`: HMAC-SHA1 `X-Twilio-Signature` verification; send via the Messages API.
* `ExotelSms`: HTTP Basic over TLS + secret path segment (Exotel offers no body signature); send via the
  Sms/send API. Field names for Exotel webhooks must be confirmed during provider onboarding (TRD R-06).
IP allow-listing of provider ranges happens at NGINX (SEC-CH-01).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx
from starlette.requests import Request

from app.core import redis as rds
from app.core.config import settings
from app.core.ids import uuid7


@dataclass
class InboundMessage:
    """Normalised internal shape (API-Guide §11)."""

    provider: str
    provider_message_id: str
    channel: str  # sms | ivr_missed_call | ivr_keypress
    from_: str
    to: str
    body: str | None
    dtmf: str | None
    provider_timestamp: datetime | None


class SmsGateway(Protocol):
    name: str

    async def send(self, to: str, text: str) -> str: ...

    async def verify_webhook(self, request: Request, raw: bytes, path_secret: str | None) -> InboundMessage | None: ...


class FakeSms:
    name = "fake"
    OUTBOX = "dev:sms:outbox"

    async def send(self, to: str, text: str) -> str:
        mid = f"fake-{uuid7()}"
        entry = {"id": mid, "to": to, "text": text, "at": datetime.now(UTC).isoformat()}
        await rds.r().lpush(rds.k(self.OUTBOX), json.dumps(entry))
        await rds.r().ltrim(rds.k(self.OUTBOX), 0, 499)
        return mid

    async def verify_webhook(self, request: Request, raw: bytes, path_secret: str | None) -> InboundMessage | None:
        return None  # fake messages only arrive through /dev/sms


class TwilioSms:
    name = "twilio"

    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        self.account_sid, self.auth_token, self.from_number = account_sid, auth_token, from_number

    async def send(self, to: str, text: str) -> str:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json",
                data={"To": to, "From": self.from_number, "Body": text}, auth=(self.account_sid, self.auth_token))
            resp.raise_for_status()
            return str(resp.json()["sid"])

    async def verify_webhook(self, request: Request, raw: bytes, path_secret: str | None) -> InboundMessage | None:
        form = dict(httpx.QueryParams(raw.decode("utf-8", "replace")))
        url = str(request.url)
        payload = url + "".join(f"{k}{form[k]}" for k in sorted(form))
        expected = base64.b64encode(hmac.new(self.auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()
        if not hmac.compare_digest(expected, request.headers.get("x-twilio-signature", "")):
            return None
        if not form.get("MessageSid"):
            return None
        return InboundMessage("twilio", form["MessageSid"], "sms", form.get("From", ""), form.get("To", ""),
                              form.get("Body"), None, None)


class ExotelSms:
    name = "exotel"

    def __init__(self, sid: str, api_key: str, api_token: str, from_number: str) -> None:
        self.sid, self.api_key, self.api_token, self.from_number = sid, api_key, api_token, from_number

    async def send(self, to: str, text: str) -> str:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"https://api.exotel.com/v1/Accounts/{self.sid}/Sms/send.json",
                                     data={"From": self.from_number, "To": to, "Body": text},
                                     auth=(self.api_key, self.api_token))
            resp.raise_for_status()
            return str(resp.json()["SMSMessage"]["Sid"])

    async def verify_webhook(self, request: Request, raw: bytes, path_secret: str | None) -> InboundMessage | None:
        auth = request.headers.get("authorization", "")
        expected = "Basic " + base64.b64encode(
            f"{settings.exotel_webhook_user}:{settings.exotel_webhook_password}".encode()).decode()
        if not settings.exotel_webhook_password or not hmac.compare_digest(auth, expected):
            return None
        if not path_secret or not hmac.compare_digest(path_secret, settings.exotel_webhook_path_secret):
            return None
        form = dict(httpx.QueryParams(raw.decode("utf-8", "replace")))
        mid = form.get("SmsSid") or form.get("CallSid")
        if not mid:
            return None
        ts = None
        if form.get("Date"):
            try:
                ts = datetime.fromisoformat(form["Date"]).replace(tzinfo=UTC)
            except ValueError:
                ts = None
        return InboundMessage("exotel", mid, "sms", form.get("From", ""), form.get("To", ""), form.get("Body"), None, ts)


_gateway: SmsGateway | None = None


def gateway() -> SmsGateway:
    global _gateway
    if _gateway is None:
        if settings.sms_provider == "twilio":
            import os

            _gateway = TwilioSms(os.environ.get("AM_TWILIO_ACCOUNT_SID", ""), settings.twilio_auth_token,
                                 os.environ.get("AM_TWILIO_FROM", ""))
        elif settings.sms_provider == "exotel":
            import os

            _gateway = ExotelSms(os.environ.get("AM_EXOTEL_SID", ""), os.environ.get("AM_EXOTEL_API_KEY", ""),
                                 os.environ.get("AM_EXOTEL_API_TOKEN", ""), os.environ.get("AM_EXOTEL_FROM", ""))
        else:
            _gateway = FakeSms()
    return _gateway
