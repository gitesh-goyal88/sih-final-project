"""WebSocket `/ws` (TRD §11, API-Guide §9, SEC-API-08).

* One-time ticket (30 s, GETDEL) bound to user + device + Origin; sent as the FIRST message (preferred) or ?ticket=.
* Subscriptions are authorised on EVERY subscribe; messages are nudges (IDs only).
* Max message 16 KB; idle timeout 60 s without heartbeat; re-auth with a fresh ticket every 15 min.
* Fan-out across API replicas through Redis pub/sub `ws:ch:*`.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core import redis as rds
from app.core.db import T, engine
from app.core.logging import log
from app.core.rbac import Principal, is_case_participant, resolve_scope

MAX_MSG = 16 * 1024
IDLE_S = 60
REAUTH_S = 15 * 60
HEARTBEAT_S = 25


class Hub:
    def __init__(self) -> None:
        self.channels: dict[str, set[WebSocket]] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._pump())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _pump(self) -> None:
        prefix = rds.k("ws:ch:")
        while True:
            try:
                pubsub = rds.r().pubsub()
                await pubsub.psubscribe(prefix + "*")
                async for msg in pubsub.listen():
                    if msg.get("type") != "pmessage":
                        continue
                    channel = msg["channel"][len(prefix):]
                    for ws in list(self.channels.get(channel, ())):
                        try:
                            await ws.send_text(msg["data"])
                        except Exception:  # noqa: BLE001
                            self.discard(ws)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("ws_pump_restart", error=repr(exc))
                await asyncio.sleep(1)

    def add(self, channel: str, ws: WebSocket) -> None:
        self.channels.setdefault(channel, set()).add(ws)

    def remove(self, channel: str, ws: WebSocket) -> None:
        self.channels.get(channel, set()).discard(ws)

    def discard(self, ws: WebSocket) -> None:
        for subs in self.channels.values():
            subs.discard(ws)


hub = Hub()


async def issue_ticket(p: Principal, origin: str | None) -> dict[str, Any]:
    ticket = "wst_" + uuid.uuid4().hex
    await rds.r().set(rds.k(f"ws:ticket:{ticket}"), json.dumps({"userId": str(p.user_id), "role": p.role,
                                                                "deviceId": str(p.device_id) if p.device_id else None,
                                                                "origin": origin}), ex=30)
    return {"ticket": ticket, "expiresAt": datetime.fromtimestamp(time.time() + 30, UTC).isoformat()}


async def _redeem(ticket: str, origin: str | None) -> Principal | None:
    raw = await rds.r().getdel(rds.k(f"ws:ticket:{ticket}"))
    if not raw:
        return None
    data = json.loads(raw)
    if data.get("origin") and origin and data["origin"] != origin:
        return None
    uid = uuid.UUID(data["userId"])
    async with engine().connect() as conn:
        scope = await resolve_scope(conn, uid)
    if scope["status"] not in ("active", "pending_approval"):
        return None
    return Principal(user_id=uid, role=scope["role"], status=scope["status"],
                     device_id=uuid.UUID(data["deviceId"]) if data.get("deviceId") else None, jti="ws",
                     villages=scope["villages"], household_id=scope["household_id"], facility_ids=scope["facility_ids"],
                     district_code=scope["district_code"], block_code=scope["block_code"],
                     home_village_id=scope.get("home_village_id"))


async def _may_subscribe(p: Principal, channel: str) -> bool:
    kind, _, ident = channel.partition(":")
    if kind == "user":
        return ident == str(p.user_id)
    if kind == "facility":
        return ident in p.facility_ids
    if kind == "district":
        return p.role == "district_admin" and ident == p.district_code
    async with engine().connect() as conn:
        if kind == "case":
            c = T.cases
            case = (await conn.execute(select(c).where(c.c.id == ident))).first() if _is_uuid(ident) else None
            return case is not None and await is_case_participant(conn, p, case) is not None
        if kind == "teleconsult" and _is_uuid(ident):
            tc = T.teleconsult_sessions
            row = (await conn.execute(select(tc.c.asha_id, tc.c.doctor_id).where(tc.c.id == ident))).first()
            return row is not None and p.user_id in (row.asha_id, row.doctor_id)
    return False


def _is_uuid(v: str) -> bool:
    try:
        uuid.UUID(v)
        return True
    except ValueError:
        return False


def _defaults(p: Principal) -> list[str]:
    chans = [f"user:{p.user_id}"]
    if p.role in ("facility_staff", "doctor"):
        chans += [f"facility:{f}" for f in p.facility_ids]
    if p.role == "district_admin" and p.district_code:
        chans.append(f"district:{p.district_code}")
    return chans


async def endpoint(ws: WebSocket) -> None:
    await ws.accept()
    origin = ws.headers.get("origin")
    principal: Principal | None = None
    subs: set[str] = set()
    authed_at = 0.0
    try:
        ticket = ws.query_params.get("ticket")
        if ticket:
            principal = await _redeem(ticket, origin)
        if principal is None:
            first = await asyncio.wait_for(ws.receive_text(), timeout=10)
            msg = json.loads(first[:MAX_MSG])
            if msg.get("type") == "auth":
                principal = await _redeem(str(msg.get("ticket", "")), origin)
        if principal is None:
            await ws.send_json({"type": "error", "code": "UNAUTHENTICATED"})
            await ws.close(code=4401)
            return
        authed_at = time.monotonic()
        for ch in _defaults(principal):
            hub.add(ch, ws)
            subs.add(ch)
        await ws.send_json({"type": "ready", "userId": str(principal.user_id), "serverTime": datetime.now(UTC).isoformat(),
                            "heartbeatS": HEARTBEAT_S})
        await rds.r().set(rds.k(f"presence:user:{principal.user_id}"), "1", ex=90)
        while True:
            if time.monotonic() - authed_at > REAUTH_S + 60:
                await ws.close(code=4401)
                return
            raw = await asyncio.wait_for(ws.receive_text(), timeout=IDLE_S)
            if len(raw) > MAX_MSG:
                await ws.send_json({"type": "error", "code": "PAYLOAD_TOO_LARGE"})
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            await rds.r().set(rds.k(f"presence:user:{principal.user_id}"), "1", ex=90)
            t = msg.get("type")
            if t == "ping":
                await ws.send_json({"type": "pong", "serverTime": datetime.now(UTC).isoformat()})
            elif t == "auth":
                again = await _redeem(str(msg.get("ticket", "")), origin)
                if again is None or again.user_id != principal.user_id:
                    await ws.close(code=4401)
                    return
                principal, authed_at = again, time.monotonic()
            elif t == "subscribe":
                ch = str(msg.get("channel", ""))
                if await _may_subscribe(principal, ch):
                    hub.add(ch, ws)
                    subs.add(ch)
                    await ws.send_json({"type": "subscribed", "channel": ch, "id": msg.get("id")})
                else:
                    await ws.send_json({"type": "error", "id": msg.get("id"), "code": "FORBIDDEN_SCOPE"})
            elif t == "unsubscribe":
                ch = str(msg.get("channel", ""))
                hub.remove(ch, ws)
                subs.discard(ch)
            elif t == "ack" and msg.get("notificationId"):
                from app.core.uow import uow
                from app.modules.comms.notify import mark_opened

                async with uow(principal) as tx:
                    await mark_opened(tx.conn, msg["notificationId"], principal.user_id)
            elif isinstance(t, str) and t.startswith("teleconsult."):
                sid = str(msg.get("sessionId", ""))
                ch = f"teleconsult:{sid}"
                if ch in subs or await _may_subscribe(principal, ch):
                    relay = {k: v for k, v in msg.items() if k in ("type", "sessionId", "sdp", "candidate", "sdpMid",
                                                                   "sdpMLineIndex", "estimatedKbps", "reason", "role")}
                    relay["from"] = str(principal.user_id)
                    from app.modules.comms.notify import ws_publish

                    await ws_publish(ch, relay)
                    if t == "teleconsult.hangup":
                        await ws_publish(ch, {"type": "teleconsult.peer_left", "sessionId": sid})
    except (TimeoutError, WebSocketDisconnect):
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("ws_error", error=repr(exc))
    finally:
        hub.discard(ws)
        if principal is not None:
            for ch in subs:
                if ch.startswith("teleconsult:"):
                    from app.modules.comms.notify import ws_publish

                    await ws_publish(ch, {"type": "teleconsult.peer_left", "sessionId": ch.split(":", 1)[1]})


