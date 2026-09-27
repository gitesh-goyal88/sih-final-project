"""POST /sync and GET /sync/snapshot (TRD §6, API-Guide §8, database.md §9).

Push: ops sorted by (priority, hlc); P0 (SOS) first; each op in its own transaction so one failure never
rejects the batch; every op idempotent by `opId`. Pull: changes since the cursor, scoped by role, grouped per
entity, projected per role. The cursor is a transaction-id watermark (database.md §9.2) — a change committed
late is never skipped (schema R12).
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core import redis as rds
from app.core.crypto import system_decrypt, system_encrypt
from app.core.db import T, engine
from app.core.errors import CODES, AppError
from app.core.rbac import Principal
from app.core.uow import uow
from app.modules.platform import projections

PAGE = 500
CURSOR_MAX_AGE_S = 30 * 24 * 3600
PRIORITY_OF = {"CreateSOS": 0}


def encode_cursor(watermark: str, txid: str = "0", seq: int = 0) -> str:
    raw = json.dumps({"w": watermark, "t": txid, "s": seq, "iat": int(time.time())}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any]:
    if not cursor:
        raise AppError("CURSOR_EXPIRED", "No cursor — bootstrap with GET /sync/snapshot")
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        int(data["w"]), int(data["t"]), int(data["s"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AppError("CURSOR_EXPIRED", "Unreadable cursor") from exc
    if time.time() - int(data.get("iat", 0)) > CURSOR_MAX_AGE_S:
        raise AppError("CURSOR_EXPIRED")
    return data


# ---------------- push ----------------

async def _op_done(actor: uuid.UUID, op_id: uuid.UUID) -> dict[str, Any] | None:
    ik = T.idempotency_keys
    async with engine().connect() as conn:
        row = (await conn.execute(select(ik).where(and_(ik.c.key == op_id, ik.c.actor_id == actor)))).first()
    if row is None:
        return None
    return json.loads(system_decrypt("idempotency", row.response_enc, f"{actor}:{op_id}"))


async def _remember(actor: uuid.UUID, op_id: uuid.UUID, name: str, result: dict[str, Any]) -> None:
    ik = T.idempotency_keys
    import hashlib

    async with engine().begin() as conn:
        await conn.execute(pg_insert(ik).values(
            key=op_id, actor_id=actor, endpoint=f"sync:{name}", request_hash=hashlib.sha256(op_id.bytes).digest(),
            status_code=200, response_enc=system_encrypt("idempotency", json.dumps(result, default=str).encode(),
                                                         f"{actor}:{op_id}"),
            expires_at=datetime.now(UTC) + timedelta(days=7)).on_conflict_do_nothing())


async def apply_op(p: Principal, op: dict[str, Any]) -> dict[str, Any]:
    """Returns {'result': …} or {'conflict': …}. Raises AppError for rejected ops."""
    from app.modules.continuity import service as continuity
    from app.modules.onboarding import service as onboarding
    from app.modules.platform.consents import record_consent
    from app.modules.referral import cases, commands, core
    from app.modules.routine_care import service as routine
    from app.modules.transport import legs as tlegs

    entity, kind = op.get("entity"), op.get("op")
    name = op.get("name") or kind
    payload = op.get("payload") or {}
    data = op.get("data") or {}
    fields = op.get("fields") or {}
    hlc = op.get("hlc")

    if entity == "case" and kind == "command":
        if name == "CreateSOS":
            if p.role not in ("patient", "asha", "volunteer"):
                raise AppError("FORBIDDEN_ROLE")
            case_id = uuid.UUID(str(payload["caseId"]))
            async with rds.case_lock(str(case_id)):
                async with uow(p) as tx:
                    pk = payload.get("pickup")
                    rec = datetime.fromisoformat(str(payload["recordedAt"]).replace("Z", "+00:00")) if payload.get("recordedAt") else None
                    case, dedup = await cases.create_sos(
                        tx, case_id=case_id, idempotency_key=uuid.UUID(str(payload.get("idempotencyKey") or case_id)),
                        category=payload["category"], patient_id=payload.get("patientId"),
                        household_id=payload.get("householdId"), flags=payload.get("flags") or [],
                        pickup=pk, location_source=payload.get("locationSource"),
                        self_transport=bool(payload.get("selfTransport")), recorded_at=rec, channel="app",
                        dedupe_channel="app")
            return {"result": {"caseId": case.id, "shortCode": case.short_code, "status": case.status,
                               "deduplicated": dedup}}
        if name == "CreateReferral":
            async with uow(p) as tx:
                case = await cases.create_referral(tx, p, payload)
            return {"result": {"caseId": case.id, "shortCode": case.short_code, "status": case.status}}
        case_id = payload.get("caseId") or op.get("id")
        async with rds.case_lock(str(case_id)):
            async with uow(p) as tx:
                case = await core.require_case(tx, case_id)
                from app.core.rbac import is_case_participant

                if await is_case_participant(tx.conn, p, case) is None:
                    raise AppError("FORBIDDEN_SCOPE")
                args = payload.get("args") or {k: v for k, v in payload.items() if k not in ("caseId", "expectedVersion")}
                case = await commands.run(tx, case, name, args, payload.get("expectedVersion"))
        return {"result": {"caseId": case.id, "status": case.status, "version": case.version}}

    if entity == "transport_leg" and kind == "command":
        case_id, leg_id = payload["caseId"], payload["legId"]
        async with rds.case_lock(str(case_id)):
            async with uow(p) as tx:
                if name == "AcceptLeg":
                    res = await tlegs.accept_leg(tx, case_id, leg_id, offer_id=payload.get("offerId"),
                                                 vehicle_id=payload.get("vehicleId"), location=payload.get("location"))
                    return {"result": {"legId": leg_id, "status": res["leg"]["status"]}}
                if name == "DeclineLeg":
                    await tlegs.decline_leg(tx, case_id, leg_id, payload.get("offerId"))
                    return {"result": {"legId": leg_id, "status": "declined"}}
                if name == "Handover":
                    res = await tlegs.handover(tx, case_id, leg_id, payload)
                    return {"result": res}
        raise AppError("VALIDATION_FAILED", f"Unknown leg command {name}")

    async with uow(p) as tx:
        if entity == "household":
            if p.role != "asha":
                raise AppError("FORBIDDEN_ROLE")
            if kind == "create":
                hid = await onboarding.create_household(tx, p, data, hlc)
                return {"result": {"id": hid}, "assigned": await _short_codes(tx, [m.get("id") for m in data.get("members") or []])}
            if kind == "update":
                lost = await onboarding.update_household(tx, p, op["id"], fields, hlc)
                return _conflict(op, lost, {})
        if entity == "patient":
            if p.role != "asha":
                raise AppError("FORBIDDEN_ROLE")
            if kind == "create":
                pid = await onboarding.create_patient(tx, p, data["householdId"], {k: v for k, v in data.items() if k != "householdId"}, hlc)
                return {"result": {"id": pid}, "assigned": await _short_codes(tx, [pid])}
            if kind == "update":
                lost = await onboarding.update_patient(tx, p, op["id"], fields, hlc)
                cur = {}
                if lost:
                    prow = (await onboarding.patients_out(tx.conn, await onboarding.patient_rows(tx.conn, [op["id"]])))[0]
                    cur = {f: (prow.get("phoneMasked") if f == "phone" else prow.get(f)) for f in lost}
                return _conflict(op, lost, cur)
            if kind == "command":
                await onboarding.patient_command(tx, p, op["id"], name, payload)
                return {"result": {"id": op["id"]}}
        if entity == "patient_cohort":
            if kind == "create":
                return {"result": {"id": await onboarding.start_cohort(tx, p, data["patientId"], data)}}
            if kind == "update":
                pc = T.patient_cohorts
                pid = (await tx.conn.execute(select(pc.c.patient_id).where(pc.c.id == op["id"]))).scalar()
                await onboarding.end_cohort(tx, p, pid, op["id"], fields)
                return {"result": {"id": op["id"]}}
        if entity == "patient_condition" and kind == "create":
            return {"result": {"id": await onboarding.add_condition(tx, p, data["patientId"], {k: v for k, v in data.items() if k != "patientId"})}}
        if entity == "patient_medication" and kind == "create":
            return {"result": {"id": await onboarding.add_medication(tx, p, data["patientId"], {k: v for k, v in data.items() if k != "patientId"})}}
        if entity == "consent" and kind == "create":
            return {"result": {"id": await record_consent(tx, p, data)}}
        if entity == "health_record_entry" and kind == "create":
            if p.role != "asha":
                raise AppError("FORBIDDEN_ROLE")
            from app.core.rbac import can_access_patient, require_consent

            pid = data.get("patientId")
            if not pid or not await can_access_patient(tx.conn, p, pid, write=True):
                raise AppError("FORBIDDEN_SCOPE")
            await require_consent(tx.conn, p, pid, "continuity_of_care")
            res = await routine.insert_entry(tx, p, pid, {k: v for k, v in data.items() if k != "patientId"})
            return {"result": {"id": res["entryId"], "highRisk": res["highRisk"], "tasksCreated": res["tasksCreated"]}}
        if entity == "follow_up_task":
            if kind == "create":
                from datetime import date

                tid = await continuity.make_task(tx, patient_id=data["patientId"], asha_id=p.user_id,
                                                 task_type=data["taskType"], due=date.fromisoformat(data["dueDate"]),
                                                 source_kind="manual", dedupe_key=f"manual:{data['id']}",
                                                 priority=data.get("priority", "normal"), title=data.get("title"))
                return {"result": {"id": tid}}
            if kind == "update":
                row = await continuity.update_task(tx, p, op["id"], fields, via_sync=True)
                return {"result": {"id": row.id, "status": row.status}}
            if kind == "command":
                row = await continuity.task_command(tx, p, op["id"], name, payload)
                return {"result": {"id": row.id, "status": row.status}}
        if entity == "volunteer_profile" and kind == "update":
            extra = set(fields) - {"available", "firstAidTrained"}
            if extra or p.role != "volunteer":
                raise AppError("FIELD_NOT_WRITABLE", f"Not writable: {sorted(extra)}")
            vp = T.volunteer_profiles
            prof = (await tx.conn.execute(select(vp).where(vp.c.user_id == p.user_id))).first()
            if fields.get("available") and prof.verified_at is None:
                raise AppError("VOLUNTEER_NOT_VERIFIED")
            vals = {}
            if "available" in fields:
                vals.update(available=bool(fields["available"]), available_changed_at=datetime.now(UTC))
            if "firstAidTrained" in fields:
                vals["first_aid_trained"] = bool(fields["firstAidTrained"])
            await tx.conn.execute(update(vp).where(vp.c.user_id == p.user_id).values(**vals))
            await tx.journal("volunteer_profile", p.user_id, [f"user:{p.user_id}"])
            return {"result": {"userId": p.user_id}}
        if entity == "user_self" and kind == "update":
            extra = set(fields) - {"preferredLanguage", "textScale"}
            if extra:
                raise AppError("FIELD_NOT_WRITABLE", f"Not writable: {sorted(extra)}")
            vals = {}
            if "preferredLanguage" in fields:
                vals["preferred_language"] = fields["preferredLanguage"]
            if "textScale" in fields:
                if fields["textScale"] not in (100, 130, 160):
                    raise AppError("VALIDATION_FAILED", "textScale must be 100/130/160")
                vals["text_scale"] = fields["textScale"]
            if vals:
                await tx.conn.execute(update(T.users).where(T.users.c.id == p.user_id).values(**vals))
                await tx.journal("user_self", p.user_id, [f"user:{p.user_id}"])
            return {"result": {"id": p.user_id}}
        if entity == "attachment" and kind == "command" and name == "MarkUploaded":
            a = T.attachments
            res = await tx.conn.execute(update(a).where(and_(a.c.id == payload.get("id") or op.get("id"),
                                                             a.c.created_by == p.user_id, a.c.upload_status == "pending"))
                                        .values(upload_status="uploaded", uploaded_at=datetime.now(UTC)))
            if res.rowcount:
                att_id = payload.get("id") or op.get("id")

                async def _verify() -> None:
                    from app.workers.registry import enqueue

                    await enqueue("verify_attachment", attachment_id=att_id)
                tx.after_commit(_verify)
            return {"result": {"id": payload.get("id") or op.get("id")}}
    raise AppError("VALIDATION_FAILED", f"Unsupported op {entity}/{kind}/{name}")


def _conflict(op: dict[str, Any], lost: list[str], server_value: dict[str, Any]) -> dict[str, Any]:
    if not lost:
        return {"result": {"id": op["id"]}}
    return {"result": {"id": op["id"]}, "conflict": {"opId": op["opId"], "entity": op["entity"], "id": op["id"],
                                                     "fields": lost, "serverValue": server_value,
                                                     "resolution": "server_won"}}


async def _short_codes(tx: Any, ids: list[Any]) -> dict[str, str]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    pt = T.patients
    return {str(r.id): r.short_code for r in (await tx.conn.execute(select(pt.c.id, pt.c.short_code).where(pt.c.id.in_(ids)))).all()}


def _hlc_key(op: dict[str, Any]) -> tuple[int, int, int]:
    from app.core import hlc

    ms, counter, _ = hlc.parse(op.get("hlc"))
    return (int(op.get("priority", 3)), ms, counter)


async def push(p: Principal, ops: list[dict[str, Any]]) -> dict[str, Any]:
    applied: list[str] = []
    results: dict[str, Any] = {}
    rejected: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    assigned: dict[str, str] = {}
    for op in sorted(ops, key=_hlc_key):
        op_id = str(op.get("opId"))
        try:
            key = uuid.UUID(op_id)
        except ValueError:
            rejected.append({"opId": op_id, "code": "VALIDATION_FAILED", "retry": False})
            continue
        prior = await _op_done(p.user_id, key)
        if prior is not None:
            applied.append(op_id)
            if prior.get("result"):
                results[op_id] = prior["result"]
            continue
        try:
            out = await apply_op(p, op)
        except AppError as exc:
            rejected.append({"opId": op_id, "code": exc.code, "retry": CODES.get(exc.code, (0, True))[1],
                             "detail": exc.detail, "current": exc.current, "fields": exc.fields})
            continue
        except (KeyError, ValueError, TypeError) as exc:
            rejected.append({"opId": op_id, "code": "VALIDATION_FAILED", "retry": False, "detail": f"bad payload: {exc}"})
            continue
        except Exception:  # noqa: BLE001 - transient (DB/Redis): client keeps the op and retries
            rejected.append({"opId": op_id, "code": "INTERNAL", "retry": True})
            continue
        applied.append(op_id)
        if out.get("result") is not None:
            results[op_id] = out["result"]
        if out.get("conflict"):
            conflicts.append(out["conflict"])
        assigned.update(out.get("assigned") or {})
        await _remember(p.user_id, key, str(op.get("name") or op.get("op")), {"result": out.get("result")})
    return {"applied": applied, "results": results, "rejected": rejected, "conflicts": conflicts,
            "assigned": {"patientShortCodes": assigned}}


# ---------------- pull ----------------

async def pull(p: Principal, cursor: str | None, scopes: list[str]) -> dict[str, Any]:
    cur = decode_cursor(cursor)
    async with engine().connect() as conn:
        async with conn.begin():
            new_w = (await conn.execute(text("SELECT pg_snapshot_xmin(pg_current_snapshot())::text"))).scalar()
            # database.md §9.2 — reviewed: parameterised, watermark cursor
            rows = (await conn.execute(text("""
                SELECT seq, txid::text AS txid, entity, entity_id, op
                FROM sync_changes
                WHERE scope = ANY(:scopes)
                  AND txid >= (:w)::xid8
                  AND txid <  (:nw)::xid8
                  AND (txid, seq) > ((:t)::xid8, :s)
                ORDER BY txid, seq
                LIMIT :lim"""), {"scopes": scopes, "w": int(cur["w"]), "nw": int(new_w), "t": int(cur["t"]),
                                  "s": int(cur["s"]), "lim": PAGE})).all()  # asyncpg binds xid8 as an integer
            has_more = len(rows) == PAGE
            latest: dict[tuple[str, str], str] = {}
            for r in rows:
                latest[(r.entity, r.entity_id)] = r.op
            by_entity: dict[str, list[str]] = {}
            for (ent, eid), op in latest.items():
                if op == "upsert":
                    by_entity.setdefault(ent, []).append(eid)
            changes: list[dict[str, Any]] = []
            for ent, ids in by_entity.items():
                loaded = await projections.load(conn, p, ent, ids)
                for eid in ids:
                    data = loaded.get(eid)
                    if data is not None:
                        changes.append({"entity": ent, "id": eid, "op": "upsert", "version": data.get("version"),
                                        "data": data})
            for (ent, eid), op in latest.items():
                if op == "delete":
                    changes.append({"entity": ent, "id": eid, "op": "delete"})
    if has_more:
        last = rows[-1]
        next_cursor = encode_cursor(str(cur["w"]), last.txid, int(last.seq))
    else:
        next_cursor = encode_cursor(new_w)
    return {"changes": changes, "cursor": next_cursor, "hasMore": has_more}


async def wipe_instruction(p: Principal) -> dict[str, Any] | None:
    if p.device_id is None:
        return None
    d = T.devices
    async with engine().connect() as conn:
        row = (await conn.execute(select(d.c.wipe_requested_at, d.c.revoked_at).where(d.c.id == p.device_id))).first()
    if row is not None and row.wipe_requested_at is not None:
        return {"scope": "all", "reason": "device_revoked"}
    return None


async def touch_device(p: Principal, cursor: str) -> None:
    if p.device_id is None:
        return
    async with uow(p) as tx:
        await tx.conn.execute(update(T.devices).where(and_(T.devices.c.id == p.device_id, T.devices.c.user_id == p.user_id))
                              .values(last_sync_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
                                      last_sync_cursor=cursor))


