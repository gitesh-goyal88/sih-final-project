"""Outbound notifications and the escalation ladder (TRD §11, API-Guide §13).

Rules enforced here:
* WS / FCM messages are nudges — IDs, codes, status, timestamps only (API-Guide §9.3, §13.2).
* SMS use DLT templates; only non-PII placeholders are *stored* in `notifications.params`
  (database.md §5.11: rendered bodies are never stored). Names needed in a message (facility, driver)
  are passed separately and used only at render time (SEC-CH-06 allows facility + driver first name).
* Every attempt is a `notifications` row — it feeds the district response-time stats.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, insert, select, update

from app.core import redis as rds
from app.core.crypto import decrypt, encrypt, normalise_phone, phone_hash
from app.core.db import Conn, T
from app.core.ids import uuid7
from app.core.logging import log
from app.integrations import push as push_gw
from app.integrations import sms as sms_gw

SAFE_PARAMS = {"case", "eta", "cat", "min", "km", "status", "reason", "code_len"}


async def ws_publish(channel: str, message: dict[str, Any]) -> None:
    """Fan out a nudge to every API pod (Redis pub/sub `ws:ch:*`, database.md §15.2)."""
    msg = {**message, "serverTime": datetime.now(UTC).isoformat()}
    try:
        await rds.r().publish(rds.k(f"ws:ch:{channel}"), json.dumps(msg, default=str))
    except Exception as exc:  # noqa: BLE001
        log.warning("ws_publish_failed", channel=channel, error=repr(exc))


async def render_template(conn: Conn, code: str, language: str, params: dict[str, Any], channel: str = "sms") -> str:
    mt = T.message_templates
    rows = (await conn.execute(select(mt.c.language, mt.c.body).where(
        mt.c.code == code, mt.c.channel == channel, mt.c.active))).all()
    bodies = {r.language: r.body for r in rows}
    body = bodies.get(language) or bodies.get("en")
    if body is None:
        raise KeyError(f"template {code} missing")

    class _Safe(dict):  # type: ignore[type-arg]
        def __missing__(self, key: str) -> str:
            return ""

    return body.format_map(_Safe(params))[:320]


async def user_contact(conn: Conn, user_id: Any) -> tuple[str | None, str]:
    u = T.users
    row = (await conn.execute(select(u.c.id, u.c.phone_enc, u.c.preferred_language).where(u.c.id == user_id))).first()
    if row is None:
        return None, "hi"
    phone = await decrypt(conn, "user", row.id, "users", "phone_enc", row.id, row.phone_enc)
    return phone, row.preferred_language


async def household_contact(conn: Conn, household_id: Any) -> tuple[str | None, str]:
    h, u = T.households, T.users
    row = (await conn.execute(select(h.c.id, h.c.registered_phone_enc).where(h.c.id == household_id))).first()
    if row is None or row.registered_phone_enc is None:
        return None, "hi"
    phone = await decrypt(conn, "household", row.id, "households", "registered_phone_enc", row.id,
                          row.registered_phone_enc)
    lang = (await conn.execute(select(u.c.preferred_language).where(u.c.household_id == household_id).limit(1))).scalar()
    return phone, lang or "hi"


async def send_sms(conn: Conn, *, to: str, template: str, language: str, params: dict[str, Any], purpose: str,
                   case_id: Any = None, recipient_user_id: Any = None, recipient_facility_id: Any = None,
                   parent_id: Any = None) -> uuid.UUID:
    """Render + send one SMS and record the attempt. Called inside a transaction (after the domain commit)."""
    nid = uuid7()
    text = await render_template(conn, template, language, params)
    values: dict[str, Any] = dict(
        id=nid, case_id=case_id, recipient_user_id=recipient_user_id, recipient_facility_id=recipient_facility_id,
        channel="sms", purpose=purpose, template_code=template, language=language,
        params={k: v for k, v in params.items() if k in SAFE_PARAMS}, provider=sms_gw.gateway().name,
        parent_notification_id=parent_id,
    )
    if recipient_user_id is None and recipient_facility_id is None:
        values["recipient_phone_enc"] = await encrypt(conn, "external_contact", nid, "notifications",
                                                      "recipient_phone_enc", nid, normalise_phone(to))
        values["recipient_phone_hash"] = phone_hash(to)
    await conn.execute(insert(T.notifications).values(**values))
    try:
        mid = await sms_gw.gateway().send(normalise_phone(to), text)
        await conn.execute(update(T.notifications).where(T.notifications.c.id == nid).values(
            status="sent", sent_at=datetime.now(UTC), provider_message_id=mid))
    except Exception as exc:  # noqa: BLE001
        await conn.execute(update(T.notifications).where(T.notifications.c.id == nid).values(
            status="failed", failed_at=datetime.now(UTC), error_code=type(exc).__name__))
    return nid


async def _is_live(user_id: Any) -> bool:
    try:
        return bool(await rds.r().exists(rds.k(f"presence:user:{user_id}")))
    except Exception:  # noqa: BLE001
        return False


async def push_to_user(conn: Conn, user_id: Any, data: dict[str, str], *, purpose: str, case_id: Any = None) -> uuid.UUID:
    """Rung 1 of the ladder: WS (always published) + FCM data message if the user is not live on WS."""
    nid = uuid7()
    data = {**data, "nid": str(nid), "v": "1"}
    await ws_publish(f"user:{user_id}", {"type": data.get("t", "update"), **{k: v for k, v in data.items() if k != "t"}})
    live = await _is_live(user_id)
    channel = "ws" if live else "fcm"
    await conn.execute(insert(T.notifications).values(
        id=nid, case_id=case_id, recipient_user_id=user_id, channel=channel, purpose=purpose,
        params={k: v for k, v in data.items() if k in ("t", "caseId", "legId", "offerId", "taskId")},
        provider="internal_ws" if live else push_gw.gateway().name, status="sent", sent_at=datetime.now(UTC)))
    if not live:
        d = T.devices
        rows = (await conn.execute(select(d.c.id, d.c.fcm_token_enc).where(and_(
            d.c.user_id == user_id, d.c.revoked_at.is_(None), d.c.push_enabled, d.c.fcm_token_enc.is_not(None))))).all()
        tokens = []
        for row in rows:
            tok = await decrypt(conn, "user", user_id, "devices", "fcm_token_enc", row.id, row.fcm_token_enc)
            if tok:
                tokens.append(tok)
        if tokens:
            try:
                await push_gw.gateway().send(tokens, {k: str(v) for k, v in data.items()})
            except Exception as exc:  # noqa: BLE001
                log.warning("fcm_failed", error=repr(exc))
    return nid


async def mark_opened(conn: Conn, notification_id: Any, user_id: Any) -> bool:
    n = T.notifications
    res = await conn.execute(update(n).where(and_(n.c.id == notification_id, n.c.recipient_user_id == user_id,
                                                  n.c.opened_at.is_(None)))
                             .values(opened_at=datetime.now(UTC), status="opened"))
    return (res.rowcount or 0) > 0
