"""Identity routes: /auth/*, /me*, JWKS (API-Guide §3)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field
from sqlalchemy import and_, func, select, update

from app.core.crypto import encrypt
from app.core.db import T
from app.core.errors import AppError
from app.core.rbac import ROLES, Principal, invalidate_scope, policy, public
from app.core.security import jwks
from app.core.shapes import In, Out, UserOut
from app.core.uow import uow
from app.modules.identity import service

router = APIRouter(tags=["identity"])
ANY = ROLES


class OtpRequestIn(In):
    phone: str | None = None
    staff_id: str | None = Field(default=None, max_length=40)
    purpose: Literal["login", "register", "recovery"] = "login"
    app_hash: str | None = Field(default=None, max_length=20)


class OtpRequestOut(Out):
    sent: bool
    challenge_id: uuid.UUID
    expires_at: str
    resend_after_s: int


class DeviceIn(In):
    id: uuid.UUID
    platform: Literal["android", "web"] = "android"
    app_version: str | None = Field(default=None, max_length=40)
    os_version: str | None = Field(default=None, max_length=40)
    model: str | None = Field(default=None, max_length=80)
    fcm_token: str | None = Field(default=None, max_length=4096)
    public_key_ed25519: str | None = Field(default=None, max_length=400)


class OtpVerifyIn(In):
    challenge_id: uuid.UUID
    otp: str = Field(pattern=r"^\d{6}$")
    device: DeviceIn
    role: Literal["patient", "asha", "volunteer", "doctor", "facility_staff", "district_admin"] | None = None
    registration: dict[str, Any] | None = None


class TokenOut(Out):
    access_token: str | None = None
    access_token_expires_at: str | None = None
    refresh_token: str | None = None
    refresh_token_expires_at: str | None = None
    user: UserOut | None = None
    mfa_required: bool = False
    choose_role: list[str] | None = None
    server_time: str


class RefreshIn(In):
    refresh_token: str | None = Field(default=None, max_length=200)
    device_id: str | None = None


class LogoutIn(In):
    all_devices: bool = False


def _set_web_cookies(response: Response, cookie: dict[str, Any]) -> None:
    # FR-W02 / SEC-WEB-03: refresh in HttpOnly Secure SameSite=Strict cookie scoped to the refresh path
    response.set_cookie("am_rt", cookie["refresh"], max_age=cookie["max_age"], httponly=True, secure=True,
                        samesite="strict", path="/api/v1/auth/refresh")
    response.set_cookie("am_csrf", cookie["csrf"], max_age=cookie["max_age"], httponly=False, secure=True,
                        samesite="strict", path="/")


@router.post("/auth/otp/request", response_model=OtpRequestOut, dependencies=[Depends(public())])
async def otp_request(body: OtpRequestIn, request: Request) -> dict[str, Any]:
    if not body.phone and not body.staff_id:
        raise AppError("VALIDATION_FAILED", "phone or staffId is required", fields=[{"path": "phone", "code": "missing"}])
    return await service.request_otp(phone=body.phone, staff_id=body.staff_id, purpose=body.purpose,
                                     app_hash=body.app_hash, ip=request.client.host if request.client else None)


@router.post("/auth/otp/verify", response_model=TokenOut, response_model_exclude_none=True,
             dependencies=[Depends(public())])
async def otp_verify(body: OtpVerifyIn, request: Request, response: Response) -> dict[str, Any]:
    out, cookie = await service.verify_otp(
        challenge_id=body.challenge_id, otp=body.otp, device=body.device.model_dump(by_alias=True),
        role=body.role, registration=body.registration, ip=request.client.host if request.client else None)
    if cookie:
        _set_web_cookies(response, cookie)
    return out


@router.post("/auth/refresh", response_model=TokenOut, response_model_exclude_none=True,
             dependencies=[Depends(public())])
async def refresh(request: Request, response: Response, body: RefreshIn | None = None) -> dict[str, Any]:
    if body and body.refresh_token:
        out, cookie = await service.refresh(token=body.refresh_token, device_id=body.device_id, platform="android")
    else:
        token = request.cookies.get("am_rt")
        csrf_cookie, csrf_header = request.cookies.get("am_csrf"), request.headers.get("x-csrf-token")
        if not token or not csrf_cookie or csrf_cookie != csrf_header:
            raise AppError("REFRESH_INVALID", "Missing refresh cookie or CSRF token")
        out, cookie = await service.refresh(token=token, device_id=None, platform="web")
    if cookie:
        _set_web_cookies(response, cookie)
    return out


@router.post("/auth/logout", status_code=204)
async def logout(body: LogoutIn | None = None, p: Principal = Depends(policy(*ANY, purpose="security", allow_pending=True))) -> Response:
    await service.logout(p, bool(body and body.all_devices))
    resp = Response(status_code=204)
    resp.delete_cookie("am_rt", path="/api/v1/auth/refresh")
    resp.delete_cookie("am_csrf", path="/")
    return resp


@router.get("/.well-known/jwks.json", dependencies=[Depends(public())])
async def jwks_route() -> dict[str, Any]:
    return jwks()


# ---------------- /me ----------------

class MePatchIn(In):
    preferred_language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")
    text_scale: Literal[100, 130, 160] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=80)


@router.get("/me", response_model=UserOut)
async def me(p: Principal = Depends(policy(*ANY, purpose="administration", allow_pending=True))) -> dict[str, Any]:
    from app.core.rbac import resolve_scope

    async with uow(p) as tx:
        user = (await tx.conn.execute(select(T.users).where(T.users.c.id == p.user_id))).first()
        return await service.user_out(tx.conn, user, await resolve_scope(tx.conn, p.user_id))


@router.patch("/me", response_model=UserOut)
async def patch_me(body: MePatchIn, p: Principal = Depends(policy(*ANY, purpose="administration", allow_pending=True))) -> dict[str, Any]:
    from app.core.rbac import resolve_scope

    values: dict[str, Any] = {}
    async with uow(p) as tx:
        if body.preferred_language:
            values["preferred_language"] = body.preferred_language
        if body.text_scale:
            values["text_scale"] = body.text_scale
        if body.name:
            if p.role not in ("patient", "volunteer"):
                raise AppError("FIELD_NOT_WRITABLE", "Staff names are managed by the district admin")
            values["name_enc"] = await encrypt(tx.conn, "user", p.user_id, "users", "name_enc", p.user_id, body.name)
        if values:
            await tx.conn.execute(update(T.users).where(T.users.c.id == p.user_id).values(**values))
            await tx.audit("user.update", "user", p.user_id, diff={"fields": sorted(values)})
            await tx.journal("user_self", p.user_id, [f"user:{p.user_id}"])
        user = (await tx.conn.execute(select(T.users).where(T.users.c.id == p.user_id))).first()
        return await service.user_out(tx.conn, user, await resolve_scope(tx.conn, p.user_id))


class DeviceOut(Out):
    id: uuid.UUID
    platform: str
    model: str | None = None
    app_version: str | None = None
    last_seen_at: str | None = None
    last_sync_at: str | None = None
    revoked: bool
    current: bool


@router.get("/me/devices")
async def my_devices(p: Principal = Depends(policy(*ANY, purpose="security", allow_pending=True))) -> dict[str, list[DeviceOut]]:
    d = T.devices
    async with uow(p) as tx:
        rows = (await tx.conn.execute(select(d).where(d.c.user_id == p.user_id).order_by(d.c.created_at.desc()))).all()
    return {"data": [DeviceOut(id=r.id, platform=r.platform, model=r.model, app_version=r.app_version,
                               last_seen_at=r.last_seen_at.isoformat() if r.last_seen_at else None,
                               last_sync_at=r.last_sync_at.isoformat() if r.last_sync_at else None,
                               revoked=r.revoked_at is not None, current=r.id == p.device_id) for r in rows]}


@router.delete("/me/devices/{device_id}", status_code=204)
async def revoke_my_device(device_id: uuid.UUID, p: Principal = Depends(policy(*ANY, purpose="security", allow_pending=True))) -> Response:
    async with uow(p) as tx:
        owner = (await tx.conn.execute(select(T.devices.c.user_id).where(T.devices.c.id == device_id))).scalar()
        if owner != p.user_id:
            raise AppError("NOT_FOUND")
        await service.revoke_device(tx, device_id, "logout", wipe=False)
        await tx.audit("device.revoke", "device", device_id, purpose="security")
    return Response(status_code=204)


class PushTokenIn(In):
    fcm_token: str = Field(max_length=4096)


@router.put("/me/devices/{device_id}/push-token", status_code=204)
async def push_token(device_id: uuid.UUID, body: PushTokenIn,
                     p: Principal = Depends(policy(*ANY, purpose="administration", allow_pending=True))) -> Response:
    d = T.devices
    async with uow(p) as tx:
        enc = await encrypt(tx.conn, "user", p.user_id, "devices", "fcm_token_enc", device_id, body.fcm_token)
        res = await tx.conn.execute(update(d).where(and_(d.c.id == device_id, d.c.user_id == p.user_id))
                                    .values(fcm_token_enc=enc, push_enabled=True))
        if not res.rowcount:
            raise AppError("NOT_FOUND")
    return Response(status_code=204)


@router.get("/me/access-log")
async def access_log(p: Principal = Depends(policy("patient", "asha", purpose="security"))) -> dict[str, Any]:
    """'Who looked at my record' through the narrow patient_access_history() function (database.md §10.2)."""
    if p.role == "patient" and not p.patient_id:
        return {"data": []}
    async with uow(p) as tx:
        pid = p.patient_id
        rows = (await tx.conn.execute(select(func.patient_access_history(pid, 50).table_valued(
            "occurred_at", "actor_role", "action", "purpose")))).all() if pid else []
    return {"data": [{"occurredAt": r.occurred_at, "actorRole": r.actor_role, "action": r.action,
                      "purpose": r.purpose} for r in rows]}


class OptOutIn(In):
    opt_out: bool


@router.post("/me/leaderboard-opt-out", status_code=204)
async def leaderboard_opt_out(body: OptOutIn, p: Principal = Depends(policy("volunteer", "asha", purpose="administration"))) -> Response:
    async with uow(p) as tx:
        await tx.conn.execute(update(T.users).where(T.users.c.id == p.user_id).values(leaderboard_opt_out=body.opt_out))
        await tx.audit("user.leaderboard_opt_out", "user", p.user_id, diff={"optOut": body.opt_out})
    await invalidate_scope(p.user_id)
    return Response(status_code=204)
