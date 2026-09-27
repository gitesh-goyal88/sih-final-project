"""Authorisation: RBAC + scope + DPDP purpose in one policy module (TRD §14.2, API-Guide §4, SEC-AZ-01..05).

Deny by default: every route declares `policy(...)` or `public()`; a CI test fails if one doesn't.
Scope is resolved from the DATABASE per request (cached ≤ 60 s, invalidated on assignment change) —
the JWT scope claim is a routing hint only (SEC-AZ-04, SF-05).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from fastapi import Depends, Request
from sqlalchemy import and_, exists, func, or_, select

from app.core import redis as rds
from app.core.db import Conn, T, engine
from app.core.errors import AppError
from app.core.logging import security_event
from app.core.security import TokenError, decode_access_token

ROLES = ("patient", "asha", "volunteer", "doctor", "facility_staff", "district_admin")
PURPOSES = ("emergency_care", "continuity_of_care", "programme_reporting", "abdm_sharing",
            "administration", "security")


@dataclass
class Principal:
    user_id: uuid.UUID
    role: str
    status: str
    device_id: uuid.UUID | None
    jti: str
    villages: list[str] = field(default_factory=list)
    household_id: str | None = None
    patient_id: str | None = None
    facility_ids: list[str] = field(default_factory=list)
    district_code: str | None = None
    block_code: str | None = None
    home_village_id: str | None = None
    purpose: str | None = None
    request_id: str | None = None
    ip: str | None = None
    break_glass_grant_id: str | None = None

    def scope_claim(self) -> dict[str, Any]:
        return {"villages": self.villages, "blockCode": self.block_code, "districtCode": self.district_code,
                "facilityIds": self.facility_ids, "householdId": self.household_id}

    def sync_scopes(self) -> set[str]:
        """Scope strings a device may request in /sync (API-Guide §8.4, database.md §9.4)."""
        s = {"global", f"user:{self.user_id}"}
        if self.role == "asha":
            s |= {f"village:{v}" for v in self.villages}
        if self.role == "patient" and self.household_id:
            s.add(f"household:{self.household_id}")
        if self.block_code and self.role in ("asha", "volunteer"):
            s.add(f"block:{self.block_code}")
        if self.district_code and self.role == "asha":
            s.add(f"district:{self.district_code}")
        return s


SCOPE_CACHE_TTL_S = 60


async def invalidate_scope(user_id: uuid.UUID | str) -> None:
    try:
        await rds.r().delete(rds.k(f"scope:{user_id}"))
    except Exception:  # noqa: BLE001, S110
        pass


async def resolve_scope(conn: Conn, user_id: uuid.UUID) -> dict[str, Any]:
    u = T.users
    user = (await conn.execute(select(u).where(u.c.id == user_id, u.c.deleted_at.is_(None)))).first()
    if user is None:
        raise AppError("UNAUTHENTICATED", "Unknown user")
    scope: dict[str, Any] = {"status": user.status, "role": user.role, "district_code": user.district_code,
                             "villages": [], "facility_ids": [], "household_id": None, "patient_id": None,
                             "block_code": None, "home_village_id": str(user.home_village_id) if user.home_village_id else None}
    v = T.villages
    if user.role == "asha":
        a = T.asha_village_assignments
        rows = (await conn.execute(select(a.c.village_id, v.c.block_code, v.c.district_code)
                                   .join(v, v.c.id == a.c.village_id)
                                   .where(a.c.asha_id == user_id, a.c.assigned_to.is_(None)))).all()
        scope["villages"] = [str(r.village_id) for r in rows]
        if rows:
            scope["block_code"], scope["district_code"] = rows[0].block_code, rows[0].district_code
    elif user.role == "patient":
        scope["household_id"] = str(user.household_id) if user.household_id else None
        scope["patient_id"] = str(user.patient_id) if user.patient_id else None
        if user.household_id:
            h = T.households
            row = (await conn.execute(select(v.c.block_code, v.c.district_code, v.c.id).join(h, h.c.village_id == v.c.id)
                                      .where(h.c.id == user.household_id))).first()
            if row:
                scope["block_code"], scope["district_code"] = row.block_code, row.district_code
    elif user.role == "volunteer":
        vp = T.volunteer_profiles
        home = (await conn.execute(select(vp.c.home_village_id).where(vp.c.user_id == user_id))).scalar()
        home = home or user.home_village_id
        if home:
            scope["home_village_id"] = str(home)
            row = (await conn.execute(select(v.c.block_code, v.c.district_code).where(v.c.id == home))).first()
            if row:
                scope["block_code"], scope["district_code"] = row.block_code, row.district_code
    elif user.role in ("doctor", "facility_staff"):
        fm, f = T.facility_memberships, T.facilities
        rows = (await conn.execute(select(fm.c.facility_id, f.c.district_code, f.c.block_code)
                                   .join(f, f.c.id == fm.c.facility_id)
                                   .where(fm.c.user_id == user_id, fm.c.active_to.is_(None)))).all()
        scope["facility_ids"] = [str(r.facility_id) for r in rows]
        if rows:
            scope["district_code"] = rows[0].district_code
            scope["block_code"] = rows[0].block_code
    return scope


async def _cached_scope(user_id: uuid.UUID) -> dict[str, Any]:
    key = rds.k(f"scope:{user_id}")
    try:
        raw = await rds.r().get(key)
        if raw:
            return json.loads(raw)
    except Exception:  # noqa: BLE001, S110
        pass
    async with engine().connect() as conn:
        scope = await resolve_scope(conn, user_id)
    try:
        await rds.r().set(key, json.dumps(scope), ex=SCOPE_CACHE_TTL_S)
    except Exception:  # noqa: BLE001, S110
        pass
    return scope


async def current_principal(request: Request) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise AppError("UNAUTHENTICATED", "Missing bearer token")
    try:
        claims = decode_access_token(auth[7:].strip())
    except TokenError as exc:
        raise AppError(exc.code) from exc
    try:
        if await rds.r().exists(rds.k(f"jwt:deny:{claims['jti']}")):
            raise AppError("UNAUTHENTICATED", "Token revoked")
        if claims.get("deviceId") and await rds.r().exists(rds.k(f"devrev:{claims['deviceId']}")):
            raise AppError("DEVICE_REVOKED")
    except AppError:
        raise
    except Exception:  # noqa: BLE001, S110 - Redis down: tokens are short-lived anyway
        pass
    header_device = request.headers.get("x-device-id")
    if header_device and claims.get("deviceId") and header_device != claims["deviceId"]:
        raise AppError("UNAUTHENTICATED", "Device mismatch")
    user_id = uuid.UUID(claims["sub"])
    scope = await _cached_scope(user_id)
    if scope["status"] in ("suspended", "deactivated"):
        raise AppError("ACCOUNT_SUSPENDED")
    p = Principal(
        user_id=user_id, role=scope["role"], status=scope["status"],
        device_id=uuid.UUID(claims["deviceId"]) if claims.get("deviceId") else None, jti=claims["jti"],
        villages=scope["villages"], household_id=scope["household_id"], patient_id=scope["patient_id"],
        facility_ids=scope["facility_ids"], district_code=scope["district_code"], block_code=scope["block_code"],
        home_village_id=scope.get("home_village_id"),
        request_id=getattr(request.state, "request_id", None),
        ip=request.client.host if request.client else None,
    )
    return p


PolicyDep = Callable[..., Any]


def policy(*roles: str, purpose: str, allow_pending: bool = False) -> PolicyDep:
    """Route guard: role decides the verb; purpose is the DPDP purpose (API-Guide §2.10)."""
    assert purpose in PURPOSES, purpose
    for role in roles:
        assert role in ROLES, role

    async def dep(request: Request, principal: Principal = Depends(current_principal)) -> Principal:
        if principal.role not in roles:
            security_event("authz.denied", reason="role", route=request.url.path, role=principal.role,
                           user_id=str(principal.user_id))
            raise AppError("FORBIDDEN_ROLE")
        if principal.status == "pending_approval" and not allow_pending:
            raise AppError("ACCOUNT_PENDING_APPROVAL")
        principal.purpose = purpose
        request.state.principal = principal
        return principal

    dep.__am_policy__ = {"roles": roles, "purpose": purpose}  # type: ignore[attr-defined]
    return dep


def public() -> PolicyDep:
    async def dep() -> None:
        return None

    dep.__am_policy__ = {"public": True}  # type: ignore[attr-defined]
    return dep


def deny(p: Principal, code: str, detail: str, **ctx: Any) -> AppError:
    security_event("authz.denied", reason=code, user_id=str(p.user_id), role=p.role, **ctx)
    return AppError(code, detail)


# ---------------- scope checks (enforced in queries, never by post-filtering) ----------------

async def asha_owns_village(p: Principal, village_id: Any) -> bool:
    return p.role == "asha" and str(village_id) in p.villages


async def active_break_glass(conn: Conn, p: Principal, patient_id: Any, grant_id: str | None) -> str | None:
    if not grant_id:
        return None
    g = T.break_glass_grants
    row = (await conn.execute(select(g.c.id).where(
        g.c.id == grant_id, g.c.user_id == p.user_id, g.c.patient_id == patient_id,
        g.c.revoked_at.is_(None), g.c.expires_at > func.now()))).first()
    return str(row.id) if row else None


async def can_access_patient(conn: Conn, p: Principal, patient_id: Any, *, write: bool = False,
                             grant_id: str | None = None) -> bool:
    """Patient-record scope per API-Guide §4.2–4.3 and SECURITY §9.2 (time-boxed facility access)."""
    pt, h = T.patients, T.households
    row = (await conn.execute(select(pt.c.id, pt.c.household_id, h.c.village_id).join(h, h.c.id == pt.c.household_id)
                              .where(pt.c.id == patient_id, pt.c.deleted_at.is_(None)))).first()
    if row is None:
        return False
    if p.role == "asha":
        return str(row.village_id) in p.villages
    if p.role == "patient":
        return not write and p.household_id is not None and str(row.household_id) == p.household_id
    c, o = T.cases, T.facility_offers
    if p.role == "facility_staff" or (p.role == "doctor" and p.facility_ids):
        # offered to / accepted by own facility, from offer until 30 days after closure
        fac_ids = p.facility_ids or ["00000000-0000-0000-0000-000000000000"]
        q = select(exists().where(and_(
            c.c.patient_id == patient_id,
            or_(c.c.current_facility_id.in_(fac_ids),
                exists().where(and_(o.c.case_id == c.c.id, o.c.facility_id.in_(fac_ids), o.c.result == "pending"))),
            or_(c.c.closed_at.is_(None), c.c.closed_at > func.now() - timedelta(days=30)),
        )))
        if (await conn.execute(q)).scalar():
            return True
    if p.role == "doctor":
        tc = T.teleconsult_sessions
        mine = tc.c.doctor_id == p.user_id
        if p.facility_ids:
            mine = or_(mine, and_(tc.c.status == "requested", tc.c.facility_id.in_(p.facility_ids)))
        q = select(exists().where(and_(
            tc.c.patient_id == patient_id, mine,
            tc.c.status.in_(("requested", "accepted", "in_call", "async")))))
        if (await conn.execute(q)).scalar():
            return True
        q = select(exists().where(and_(c.c.patient_id == patient_id, c.c.raised_by_id == p.user_id,
                                       c.c.status.notin_(("follow_up", "cancelled")))))
        if (await conn.execute(q)).scalar():
            return True
    if p.role in ("facility_staff", "district_admin", "doctor") and grant_id:
        gid = await active_break_glass(conn, p, patient_id, grant_id)
        if gid:
            p.break_glass_grant_id = gid
            return True
    return False


async def is_case_participant(conn: Conn, p: Principal, case: Any) -> str | None:
    """Returns the projection name if `p` may read the case (API-Guide §4.2, SEC-AZ-05), else None."""
    if p.role == "district_admin":
        return "admin" if case.district_code == p.district_code else None
    if case.raised_by_id is not None and case.raised_by_id == p.user_id:
        return p.role if p.role != "volunteer" else "raiser"
    if p.role == "asha" and case.village_id is not None and str(case.village_id) in p.villages:
        return "asha"
    if p.role == "patient" and case.household_id is not None and str(case.household_id) == p.household_id:
        return "patient"
    if p.role in ("facility_staff", "doctor") and p.facility_ids:
        if case.current_facility_id is not None and str(case.current_facility_id) in p.facility_ids:
            return "facility"
        o = T.facility_offers
        q = select(exists().where(and_(o.c.case_id == case.id, o.c.facility_id.in_(p.facility_ids),
                                       o.c.result == "pending")))
        if (await conn.execute(q)).scalar():
            return "facility"
    if p.role == "doctor" and case.raised_by_id == p.user_id:
        return "facility"
    if p.role == "volunteer":
        tl = T.transport_legs
        q = select(exists().where(and_(
            tl.c.case_id == case.id, tl.c.custodian_user_id == p.user_id,
            or_(tl.c.status.in_(("accepted", "picked_up")),
                and_(tl.c.status == "handed_over", tl.c.handed_over_at > func.now() - timedelta(hours=24))))))
        if (await conn.execute(q)).scalar():
            return "volunteer"
    return None


async def require_consent(conn: Conn, p: Principal, patient_id: Any, purpose: str) -> None:
    """Non-emergency processing needs an active consent for the purpose (API-Guide §2.10, SEC-PRV-01)."""
    if purpose == "emergency_care":
        return
    vc = T.v_active_consents
    row = (await conn.execute(select(vc.c.status, vc.c.expires_at).where(
        vc.c.patient_id == patient_id, vc.c.purpose == purpose))).first()
    ok = row is not None and row.status == "granted" and (row.expires_at is None or row.expires_at > _now_utc())
    if not ok:
        raise AppError("CONSENT_REQUIRED", f"No active consent for {purpose}")


def _now_utc() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)
