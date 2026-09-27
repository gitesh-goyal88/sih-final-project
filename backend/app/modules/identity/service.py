"""Identity: OTP login, registration, devices, token rotation (API-Guide §3, SECURITY §9, SEC-ID-01..07)."""

from __future__ import annotations

import base64
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, insert, select, update

from app.core import redis as rds
from app.core.config import settings
from app.core.crypto import (
    blind_index,
    constant_time_eq,
    decrypt,
    encrypt,
    mask_phone,
    normalise_phone,
    phone_hash,
    sha256,
    system_decrypt,
    system_encrypt,
)
from app.core.db import Conn, T
from app.core.errors import AppError
from app.core.ids import uuid7
from app.core.logging import security_event
from app.core.rbac import Principal, invalidate_scope, resolve_scope
from app.core.security import issue_access_token, new_refresh_token
from app.core.uow import CommitThenRaise, UoW, uow
from app.integrations import sms as sms_gw
from app.modules.comms.notify import render_template

OTP_TTL_S = 300
OTP_MAX_ATTEMPTS = 5
OTP_LOCK_S = 15 * 60
STAFF_ROLES = ("asha", "doctor", "facility_staff", "district_admin")


def _ph_hex(ph: bytes) -> str:
    return ph.hex()


async def request_otp(*, phone: str | None, staff_id: str | None, purpose: str, app_hash: str | None,
                      ip: str | None) -> dict[str, Any]:
    """Response is identical whether or not the phone / staff ID exists (SEC-ID-03)."""
    challenge_id = uuid7()
    expires_at = datetime.now(UTC) + timedelta(seconds=OTP_TTL_S)
    response = {"sent": True, "challengeId": str(challenge_id), "expiresAt": expires_at.isoformat(), "resendAfterS": 30}

    async with uow() as tx:
        target_phone: str | None = None
        exists = False
        if staff_id:
            u = T.users
            row = (await tx.conn.execute(select(u.c.id, u.c.phone_enc).where(
                u.c.staff_id == staff_id.strip(), u.c.deleted_at.is_(None)))).first()
            if row:
                target_phone = await decrypt(tx.conn, "user", row.id, "users", "phone_enc", row.id, row.phone_enc)
                exists = True
        elif phone:
            try:
                target_phone = normalise_phone(phone)
            except ValueError as exc:
                raise AppError("VALIDATION_FAILED", "Invalid phone", fields=[{"path": "phone", "code": "invalid"}]) from exc
            u = T.users
            exists = (await tx.conn.execute(select(u.c.id).where(
                u.c.phone_hash == phone_hash(target_phone), u.c.deleted_at.is_(None)).limit(1))).first() is not None
        if target_phone is None:
            ph_for_limits = blind_index(f"staff:{staff_id}")
        else:
            ph_for_limits = phone_hash(target_phone)

        # SEC-ID-02 limits: 3 / 10 min per phone, 20 / h per IP
        if not await rds.rate_limit("otp_phone", _ph_hex(ph_for_limits), 3, 600):
            raise AppError("RATE_LIMITED", headers={"Retry-After": "600"})
        if ip and not await rds.rate_limit("otp_ip", ip, 20, 3600):
            raise AppError("RATE_LIMITED", headers={"Retry-After": "3600"})

        should_send = target_phone is not None and (exists or purpose == "register")
        if not should_send:
            return response  # no oracle, no SMS pumping for unknown numbers
        assert target_phone is not None
        ph = phone_hash(target_phone)
        code = f"{secrets.randbelow(10**6):06d}"  # SEC-ID-01 CSPRNG
        await tx.conn.execute(insert(T.otp_challenges).values(
            id=challenge_id, phone_hash=ph, requested_ip=ip, expires_at=expires_at))
        await rds.r().set(rds.k(f"otp:{_ph_hex(ph)}"), json.dumps({
            "codeHash": base64.b64encode(blind_index(f"otp:{challenge_id}:{code}")).decode(),
            "attempts": 0, "challengeId": str(challenge_id), "purpose": purpose,
            # the verified phone is needed to create a self-registered account; kept encrypted, 5 min
            "phoneEnc": base64.b64encode(system_encrypt("otp", target_phone.encode(), str(challenge_id))).decode(),
        }), ex=OTP_TTL_S)
        text = await render_template(tx.conn, "otp", "en", {"otp": code, "app_hash": app_hash or ""})
    await sms_gw.gateway().send(target_phone, text)
    return response


async def _challenge(conn: Conn, challenge_id: uuid.UUID) -> Any:
    oc = T.otp_challenges
    return (await conn.execute(select(oc).where(oc.c.id == challenge_id))).first()


async def verify_otp(*, challenge_id: uuid.UUID, otp: str, device: dict[str, Any], role: str | None,
                     registration: dict[str, Any] | None, ip: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Returns (body, web_cookie_material)."""
    async with uow() as tx:
        ch = await _challenge(tx.conn, challenge_id)
        if ch is None:
            raise AppError("OTP_EXPIRED")
        ph = bytes(ch.phone_hash)
        if await rds.r().exists(rds.k(f"otp:lock:{_ph_hex(ph)}")):
            raise AppError("OTP_LOCKED", headers={"Retry-After": str(OTP_LOCK_S)})
        raw = await rds.r().get(rds.k(f"otp:{_ph_hex(ph)}"))
        if raw is None or ch.expires_at < datetime.now(UTC) or ch.outcome != "pending":
            raise AppError("OTP_EXPIRED")
        state = json.loads(raw)
        if state["challengeId"] != str(challenge_id):
            raise AppError("OTP_EXPIRED", "A newer code was sent")
        expected = base64.b64decode(state["codeHash"])
        if not constant_time_eq(expected, blind_index(f"otp:{challenge_id}:{otp.strip()}")):
            state["attempts"] += 1
            await tx.conn.execute(update(T.otp_challenges).where(T.otp_challenges.c.id == challenge_id)
                                  .values(attempts=min(state["attempts"], OTP_MAX_ATTEMPTS)))
            security_event("auth.otp_failed", challenge_id=str(challenge_id), attempts=state["attempts"])
            if state["attempts"] >= OTP_MAX_ATTEMPTS:
                await rds.r().set(rds.k(f"otp:lock:{_ph_hex(ph)}"), "1", ex=OTP_LOCK_S)
                await rds.r().delete(rds.k(f"otp:{_ph_hex(ph)}"))
                await tx.conn.execute(update(T.otp_challenges).where(T.otp_challenges.c.id == challenge_id)
                                      .values(outcome="locked", resolved_at=datetime.now(UTC)))
                security_event("auth.otp_locked", challenge_id=str(challenge_id))
                raise CommitThenRaise(AppError("OTP_LOCKED", headers={"Retry-After": str(OTP_LOCK_S)}))
            await rds.r().set(rds.k(f"otp:{_ph_hex(ph)}"), json.dumps(state), keepttl=True)
            raise CommitThenRaise(AppError("OTP_INVALID", f"{OTP_MAX_ATTEMPTS - state['attempts']} attempts left"))

        u = T.users
        accounts = (await tx.conn.execute(select(u).where(u.c.phone_hash == ph, u.c.deleted_at.is_(None)))).all()
        if role:
            accounts = [a for a in accounts if a.role == role]
        if len(accounts) > 1:
            return {"chooseRole": sorted(a.role for a in accounts), "serverTime": datetime.now(UTC).isoformat()}, None

        if not accounts:
            if registration is None or state.get("purpose") != "register" or role not in ("patient", "volunteer"):
                raise AppError("UNAUTHENTICATED", "No account for this phone. Register, or ask your ASHA.")
            phone_plain = system_decrypt("otp", base64.b64decode(state["phoneEnc"]), str(challenge_id)).decode()
            user_id = await _register(tx, ph, phone_plain, role, registration)
        else:
            user_id = accounts[0].id

        # code is single-use
        await rds.r().delete(rds.k(f"otp:{_ph_hex(ph)}"))
        await tx.conn.execute(update(T.otp_challenges).where(T.otp_challenges.c.id == challenge_id)
                              .values(outcome="verified", resolved_at=datetime.now(UTC)))

        user = (await tx.conn.execute(select(u).where(u.c.id == user_id))).first()
        assert user is not None
        if user.status in ("suspended", "deactivated"):
            raise AppError("ACCOUNT_SUSPENDED")
        if user.status == "pending_approval" and user.role != "volunteer":
            raise AppError("ACCOUNT_PENDING_APPROVAL")  # staff get no tokens until approved
        if user.role == "patient" and user.household_id is None:
            await _link_patient_household(tx, user)
            user = (await tx.conn.execute(select(u).where(u.c.id == user_id))).first()

        device_id = await _upsert_device(tx, user.id, device)
        platform = device.get("platform", "android")
        body, cookie = await _issue_tokens(tx, user, device_id, platform, family_id=uuid7())
        await tx.conn.execute(update(u).where(u.c.id == user.id).values(last_login_at=datetime.now(UTC)))
        tx.principal = Principal(user_id=user.id, role=user.role, status=user.status, device_id=device_id, jti="-", ip=ip)
        await tx.audit("auth.login", "user", user.id, purpose="security")
    await invalidate_scope(user_id)
    return body, cookie


async def _register(tx: UoW, ph: bytes, phone_plain: str, role: str | None, reg: dict[str, Any]) -> uuid.UUID:
    """Self sign-up for patient / volunteer (API-Guide §3.3). Staff are pre-provisioned (SEC-ID-07)."""
    user_id = uuid7()
    name = (reg.get("name") or "").strip()
    if not name:
        raise AppError("VALIDATION_FAILED", "registration.name is required", fields=[{"path": "registration.name", "code": "missing"}])
    home_village = reg.get("homeVillageId")
    status = "active" if role == "patient" else "pending_approval"
    await tx.conn.execute(insert(T.users).values(
        id=user_id, role=role, status=status,
        name_enc=await encrypt(tx.conn, "user", user_id, "users", "name_enc", user_id, name),
        phone_enc=await encrypt(tx.conn, "user", user_id, "users", "phone_enc", user_id, phone_plain),
        phone_hash=ph, preferred_language=reg.get("preferredLanguage", "hi"), home_village_id=home_village))
    if role == "volunteer":
        vol = reg.get("volunteer") or {}
        consent = vol.get("liabilityConsent") or {}
        if not home_village or not consent.get("acceptedAt"):
            raise AppError("VALIDATION_FAILED", "Volunteers need homeVillageId and liability consent (R-09)",
                           fields=[{"path": "registration.volunteer.liabilityConsent", "code": "missing"}])
        await tx.conn.execute(insert(T.volunteer_profiles).values(
            user_id=user_id, home_village_id=home_village, first_aid_trained=bool(vol.get("firstAidTrained")),
            liability_consent_at=datetime.fromisoformat(consent["acceptedAt"].replace("Z", "+00:00"))))
        v = vol.get("vehicle")
        if v:
            vid = uuid.UUID(v["id"]) if v.get("id") else uuid7()
            await tx.conn.execute(insert(T.vehicles).values(
                id=vid, owner_user_id=user_id, village_id=home_village, kind=v["kind"], seats=v.get("seats"),
                registration_enc=await encrypt(tx.conn, "user", user_id, "vehicles", "registration_enc", vid,
                                               v.get("registration")) if v.get("registration") else None))
    await tx.journal("user_self", user_id, [f"user:{user_id}"])
    return user_id


async def _link_patient_household(tx: UoW, user: Any) -> None:
    """A family login links to the household the ASHA registered with this phone (API-Guide §3.3)."""
    h, pt = T.households, T.patients
    hh = (await tx.conn.execute(select(h.c.id).where(h.c.registered_phone_hash == user.phone_hash,
                                                      h.c.deleted_at.is_(None)).limit(1))).first()
    member = (await tx.conn.execute(select(pt.c.id, pt.c.household_id).where(
        pt.c.phone_hash == user.phone_hash, pt.c.deleted_at.is_(None)).limit(1))).first()
    household_id = member.household_id if member else (hh.id if hh else None)
    if household_id is None:
        return
    patient_id = member.id if member else (await tx.conn.execute(
        select(h.c.head_member_id).where(h.c.id == household_id))).scalar()
    await tx.conn.execute(update(T.users).where(T.users.c.id == user.id).values(
        household_id=household_id, patient_id=patient_id))


async def _upsert_device(tx: UoW, user_id: uuid.UUID, device: dict[str, Any]) -> uuid.UUID:
    d = T.devices
    device_id = uuid.UUID(str(device["id"]))
    values: dict[str, Any] = dict(platform=device.get("platform", "android"), app_version=device.get("appVersion"),
                                  os_version=device.get("osVersion"), model=device.get("model"),
                                  last_seen_at=datetime.now(UTC))
    if device.get("publicKeyEd25519"):
        values["public_key_spki"] = base64.b64decode(device["publicKeyEd25519"])
    if device.get("fcmToken"):
        values["fcm_token_enc"] = await encrypt(tx.conn, "user", user_id, "devices", "fcm_token_enc", device_id,
                                                device["fcmToken"])
    existing = (await tx.conn.execute(select(d.c.user_id, d.c.revoked_at).where(d.c.id == device_id))).first()
    if existing is None:
        await tx.conn.execute(insert(d).values(id=device_id, user_id=user_id, **values))
    else:
        if existing.revoked_at is not None:
            raise AppError("DEVICE_REVOKED")
        if existing.user_id != user_id:
            # one install, another account on the same phone: re-bind, revoke the old account's sessions here
            await tx.conn.execute(update(T.refresh_tokens).where(and_(
                T.refresh_tokens.c.device_id == device_id, T.refresh_tokens.c.revoked_at.is_(None)))
                .values(revoked_at=datetime.now(UTC), revoke_reason="logout"))
            if "fcm_token_enc" not in values:
                values["fcm_token_enc"] = None
        await tx.conn.execute(update(d).where(d.c.id == device_id).values(user_id=user_id, **values))
    if existing is None:
        security_event("auth.new_device", user_id=str(user_id))
    return device_id


async def _issue_tokens(tx: UoW, user: Any, device_id: uuid.UUID, platform: str, family_id: uuid.UUID
                        ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    scope = await resolve_scope(tx.conn, user.id)
    p = Principal(user_id=user.id, role=user.role, status=user.status, device_id=device_id, jti="-",
                  villages=scope["villages"], household_id=scope["household_id"], facility_ids=scope["facility_ids"],
                  district_code=scope["district_code"], block_code=scope["block_code"])
    claim_scope = p.scope_claim() if user.status == "active" else {"restricted": "profile"}
    access, exp, _ = issue_access_token(user_id=user.id, role=user.role, status=user.status,
                                        device_id=device_id, scope=claim_scope)
    refresh = new_refresh_token()
    if platform == "web":
        ttl = settings.refresh_ttl_admin_web_s if user.role == "district_admin" else settings.refresh_ttl_web_s
    else:
        ttl = settings.refresh_ttl_android_s
    rt_exp = datetime.now(UTC) + timedelta(seconds=ttl)
    await tx.conn.execute(insert(T.refresh_tokens).values(
        id=uuid7(), user_id=user.id, device_id=device_id, family_id=family_id, token_hash=sha256(refresh),
        expires_at=rt_exp))
    body: dict[str, Any] = {
        "accessToken": access,
        "accessTokenExpiresAt": datetime.fromtimestamp(exp, UTC).isoformat(),
        "refreshTokenExpiresAt": rt_exp.isoformat(),
        "user": await user_out(tx.conn, user, scope),
        "mfaRequired": False,
        "serverTime": datetime.now(UTC).isoformat(),
    }
    if platform == "web":
        return body, {"refresh": refresh, "max_age": ttl, "csrf": secrets.token_urlsafe(24)}
    body["refreshToken"] = refresh
    return body, None


async def refresh(*, token: str, device_id: str | None, platform: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    rt, u = T.refresh_tokens, T.users
    reused_family = None
    async with uow() as tx:
        row = (await tx.conn.execute(select(rt).where(rt.c.token_hash == sha256(token)).with_for_update())).first()
        if row is None:
            raise AppError("REFRESH_INVALID")
        if row.rotated_at is not None:
            # reuse of a rotated token → revoke the whole family (SEC-ID-05). Committed BEFORE the error is raised.
            await tx.conn.execute(update(rt).where(and_(rt.c.family_id == row.family_id, rt.c.revoked_at.is_(None)))
                                  .values(revoked_at=datetime.now(UTC), revoke_reason="rotated_reuse"))
            reused_family = row.family_id
    if reused_family is not None:
        security_event("auth.refresh_reuse", user_id=str(row.user_id))
        raise AppError("REFRESH_REUSED")
    async with uow() as tx:
        row = (await tx.conn.execute(select(rt).where(rt.c.token_hash == sha256(token)).with_for_update())).first()
        if row is None or row.rotated_at is not None:
            raise AppError("REFRESH_INVALID")
        if row.revoked_at is not None:
            raise AppError("DEVICE_REVOKED" if row.revoke_reason == "device_revoked" else "REFRESH_INVALID")
        if row.expires_at < datetime.now(UTC) or (device_id and str(row.device_id) != device_id):
            raise AppError("REFRESH_INVALID")
        dev = (await tx.conn.execute(select(T.devices.c.revoked_at).where(T.devices.c.id == row.device_id))).first()
        if dev is None or dev.revoked_at is not None:
            raise AppError("DEVICE_REVOKED")
        user = (await tx.conn.execute(select(u).where(u.c.id == row.user_id, u.c.deleted_at.is_(None)))).first()
        if user is None or user.status in ("suspended", "deactivated"):
            raise AppError("ACCOUNT_SUSPENDED")
        await tx.conn.execute(update(rt).where(rt.c.id == row.id).values(rotated_at=datetime.now(UTC)))
        await tx.conn.execute(update(T.devices).where(T.devices.c.id == row.device_id).values(last_seen_at=datetime.now(UTC)))
        return await _issue_tokens(tx, user, row.device_id, platform, family_id=row.family_id)


async def logout(p: Principal, all_devices: bool) -> None:
    rt = T.refresh_tokens
    async with uow(p) as tx:
        cond = rt.c.user_id == p.user_id if all_devices else and_(rt.c.user_id == p.user_id, rt.c.device_id == p.device_id)
        await tx.conn.execute(update(rt).where(and_(cond, rt.c.revoked_at.is_(None)))
                              .values(revoked_at=datetime.now(UTC), revoke_reason="logout"))
        await tx.audit("auth.logout", "user", p.user_id, purpose="security")
    await rds.r().set(rds.k(f"jwt:deny:{p.jti}"), "1", ex=settings.access_token_ttl_s)


async def revoke_device(tx: UoW, device_id: uuid.UUID, reason: str, wipe: bool) -> None:
    now = datetime.now(UTC)
    await tx.conn.execute(update(T.devices).where(T.devices.c.id == device_id).values(
        revoked_at=now, wipe_requested_at=now if wipe else None))
    await tx.conn.execute(update(T.refresh_tokens).where(and_(T.refresh_tokens.c.device_id == device_id,
                                                              T.refresh_tokens.c.revoked_at.is_(None)))
                          .values(revoked_at=now, revoke_reason=reason))

    async def _deny() -> None:
        await rds.r().set(rds.k(f"devrev:{device_id}"), "1", ex=settings.access_token_ttl_s)

    tx.after_commit(_deny)


async def user_out(conn: Conn, user: Any, scope: dict[str, Any] | None = None) -> dict[str, Any]:
    name = await decrypt(conn, "user", user.id, "users", "name_enc", user.id, user.name_enc)
    phone = await decrypt(conn, "user", user.id, "users", "phone_enc", user.id, user.phone_enc)
    scope = scope or {}
    return {
        "id": user.id, "role": user.role, "status": user.status, "name": name, "phoneMasked": mask_phone(phone),
        "staffId": user.staff_id, "preferredLanguage": user.preferred_language, "textScale": user.text_scale,
        "districtCode": scope.get("district_code") or user.district_code, "homeVillageId": user.home_village_id,
        "householdId": user.household_id, "patientId": user.patient_id,
        "facilityIds": scope.get("facility_ids") or None, "villageIds": scope.get("villages") or None,
        "version": user.version,
    }

