"""Webhooks, WebSocket, notifications, and the local/demo SMS console (API-Guide §9, §11, §13, §17)."""

from __future__ import annotations

import html
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, Response, WebSocket
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, select, update

from app.core import redis as rds
from app.core.config import settings
from app.core.db import T
from app.core.errors import AppError
from app.core.ids import uuid7
from app.core.logging import security_event
from app.core.rbac import ROLES, Principal, policy, public
from app.core.uow import uow
from app.integrations import sms as sms_gw
from app.integrations.push import FakePush
from app.integrations.sms import FakeSms, InboundMessage
from app.modules.comms import inbound, ws
from app.modules.comms.notify import mark_opened
from app.workers.registry import enqueue

router = APIRouter(tags=["comms"])
REPLAY_WINDOW = timedelta(minutes=5)


async def _accept(msg: InboundMessage | None, request: Request) -> Response:
    if msg is None:
        security_event("sms.webhook_auth_failed", ip=request.client.host if request.client else None)
        return Response(status_code=401)  # never reveal which check failed (SEC-CH-01)
    if msg.provider_timestamp and abs(datetime.now(UTC) - msg.provider_timestamp) > REPLAY_WINDOW:
        security_event("sms.webhook_auth_failed", reason="replay_window")
        return Response(status_code=401)
    rid = await inbound.store(msg, signature_valid=True)
    if rid is not None:
        await enqueue("process_inbound", inbound_id=rid)
    return Response(status_code=200)


@router.post("/webhooks/sms", dependencies=[Depends(public())])
@router.post("/webhooks/sms/{path_secret}", dependencies=[Depends(public())])
async def webhook_sms(request: Request, path_secret: str | None = None) -> Response:
    raw = await request.body()
    msg = await sms_gw.gateway().verify_webhook(request, raw, path_secret)
    return await _accept(msg, request)


@router.post("/webhooks/sms/status", dependencies=[Depends(public())])
async def webhook_sms_status(request: Request) -> Response:
    """Delivery report → `notifications.delivered_at` (matched by provider message id)."""
    import httpx

    raw = await request.body()
    probe = await sms_gw.gateway().verify_webhook(request, raw, None) if settings.sms_provider == "twilio" else None
    if settings.sms_provider == "twilio" and probe is None:
        return Response(status_code=401)
    form = dict(httpx.QueryParams(raw.decode("utf-8", "replace")))
    mid = form.get("MessageSid") or form.get("SmsSid")
    status = (form.get("MessageStatus") or form.get("Status") or "").lower()
    if mid and status in ("delivered", "failed", "undelivered"):
        n = T.notifications
        async with uow() as tx:
            await tx.conn.execute(update(n).where(and_(n.c.provider_message_id == mid)).values(
                **({"status": "delivered", "delivered_at": datetime.now(UTC)} if status == "delivered"
                   else {"status": "failed", "failed_at": datetime.now(UTC), "error_code": status})))
    return Response(status_code=200)


@router.post("/webhooks/ivr", dependencies=[Depends(public())])
@router.post("/webhooks/ivr/keypress", dependencies=[Depends(public())])
async def webhook_ivr(request: Request) -> Response:
    """IVR (Phase 2): missed call → case with household location; keypress → category (API-Guide §10.4)."""
    return Response(status_code=501)


# ---------------- WebSocket ----------------

@router.post("/ws/ticket")
async def ws_ticket(request: Request, p: Principal = Depends(policy(*ROLES, purpose="security", allow_pending=True))) -> dict[str, Any]:
    return await ws.issue_ticket(p, request.headers.get("origin"))


@router.websocket("/ws")
async def websocket(websocket: WebSocket) -> None:
    await ws.endpoint(websocket)


# ---------------- notifications ----------------

@router.post("/notifications/{notification_id}/opened", status_code=204)
async def notification_opened(notification_id: uuid.UUID,
                              p: Principal = Depends(policy(*ROLES, purpose="security", allow_pending=True))) -> Response:
    async with uow(p) as tx:
        await mark_opened(tx.conn, notification_id, p.user_id)
    return Response(status_code=204)


@router.get("/notifications")
async def notifications(unread: bool = True, p: Principal = Depends(policy("doctor", "facility_staff", "district_admin", purpose="security"))) -> dict[str, Any]:
    n, c = T.notifications, T.cases
    cond = n.c.recipient_user_id == p.user_id
    if p.facility_ids:
        cond = cond | n.c.recipient_facility_id.in_(p.facility_ids)
    q = (select(n.c.id, n.c.purpose, n.c.case_id, n.c.queued_at, n.c.opened_at, c.c.short_code)
         .outerjoin(c, c.c.id == n.c.case_id).where(cond, n.c.channel == "ws"))
    if unread:
        q = q.where(n.c.opened_at.is_(None))
    async with uow(p) as tx:
        rows = (await tx.conn.execute(q.order_by(n.c.queued_at.desc()).limit(50))).all()
    return {"data": [{"id": r.id, "purpose": r.purpose, "caseId": r.case_id, "caseShortCode": r.short_code,
                      "at": r.queued_at, "opened": r.opened_at is not None} for r in rows]}


# ---------------- local / demo console (API-Guide §11, §17) ----------------

dev = APIRouter(tags=["dev"], include_in_schema=False)


@dev.get("/dev/sms", response_class=HTMLResponse, dependencies=[Depends(public())])
async def dev_sms_page() -> str:
    raw = await rds.r().lrange(rds.k(FakeSms.OUTBOX), 0, 60)
    items = "".join(
        f"<tr><td>{html.escape(m['at'][11:19])}</td><td>{html.escape(m['to'])}</td><td>{html.escape(m['text'])}</td></tr>"
        for m in (json.loads(x) for x in raw))
    async with uow() as tx:
        nums = (await tx.conn.execute(select(T.channel_numbers.c.number_e164, T.channel_numbers.c.kind))).all()
    opts = "".join(f"<option>{html.escape(n.number_e164)}</option>" for n in nums if n.kind == "sms")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>AapatMitra — fake SMS gateway</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;max-width:960px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:6px;text-align:left;vertical-align:top}}input,select{{padding:6px;margin:4px 0}}</style></head>
<body><h1>Fake SMS gateway (local/demo only)</h1>
<p>Send an inbound SMS exactly as a phone would. OTPs and every outbound SMS appear in the outbox below.</p>
<form method="post" action="/api/v1/dev/sms">
<label>From <input name="from_" value="+919812345678" required></label>
<label>To (district number) <select name="to">{opts}</select></label><br>
<label>Body <input name="body" size="80" value="SOS - P 27.1767,78.0081"></label>
<button type="submit">Send inbound SMS</button></form>
<h2>Outbox (newest first)</h2><table><tr><th>Time (UTC)</th><th>To</th><th>Text</th></tr>{items}</table>
</body></html>"""


@dev.post("/dev/sms", dependencies=[Depends(public())])
async def dev_sms_send(from_: str = Form(...), to: str = Form(...), body: str = Form(...)) -> Response:
    msg = InboundMessage("fake", f"dev-{uuid7()}", "sms", from_, to, body, None, datetime.now(UTC))
    rid = await inbound.store(msg, signature_valid=True)
    if rid is None:
        raise AppError("VALIDATION_FAILED", "Unknown district number")
    await enqueue("process_inbound", inbound_id=rid)
    return Response(status_code=303, headers={"Location": "/api/v1/dev/sms"})


@dev.post("/dev/sms/json", dependencies=[Depends(public())])
async def dev_sms_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Same as the form, for scripts and tests: {from, to, body}."""
    msg = InboundMessage("fake", payload.get("providerMessageId") or f"dev-{uuid7()}", payload.get("channel", "sms"),
                         payload["from"], payload["to"], payload.get("body"), payload.get("dtmf"), datetime.now(UTC))
    rid = await inbound.store(msg, signature_valid=True)
    if rid is not None:
        await enqueue("process_inbound", inbound_id=rid)
    return {"inboundId": rid}


@dev.get("/dev/sms/outbox", dependencies=[Depends(public())])
async def dev_sms_outbox(limit: int = 50) -> dict[str, Any]:
    raw = await rds.r().lrange(rds.k(FakeSms.OUTBOX), 0, max(1, min(limit, 500)) - 1)
    return {"data": [json.loads(x) for x in raw]}


@dev.get("/dev/push-log", dependencies=[Depends(public())])
async def dev_push_log(limit: int = 50) -> dict[str, Any]:
    raw = await rds.r().lrange(rds.k(FakePush.LOG), 0, max(1, min(limit, 500)) - 1)
    return {"data": [json.loads(x) for x in raw]}
