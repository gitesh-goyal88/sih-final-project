"""Reference data (API-Guide §6.1): catalogs, channel numbers, villages, facility list, tile manifests."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.core.config import settings
from app.core.db import T
from app.core.errors import AppError
from app.core.rbac import ROLES, Principal, policy
from app.core.shapes import FacilityOut, Out, VillageOut
from app.core.uow import uow
from app.integrations import objectstore
from app.modules.reference import serial

router = APIRouter(tags=["reference"])
STAFF = ("asha", "doctor", "facility_staff", "district_admin")


@router.get("/reference/catalog")
async def catalog(p: Principal = Depends(policy(*ROLES, purpose="administration", allow_pending=True))) -> dict[str, Any]:
    async with uow(p) as tx:
        return await serial.catalog(tx.conn, p.district_code)


class ChannelNumbersOut(Out):
    sms_number: str | None
    ivr_number: str | None
    helpline: str


@router.get("/reference/districts/{code}/channel-numbers", response_model=ChannelNumbersOut)
async def channel_numbers(code: str, p: Principal = Depends(policy(*ROLES, purpose="emergency_care", allow_pending=True))) -> dict[str, Any]:
    cn = T.channel_numbers
    async with uow(p) as tx:
        rows = (await tx.conn.execute(select(cn.c.number_e164, cn.c.kind).where(cn.c.district_code == code, cn.c.active))).all()
    by_kind = {r.kind: r.number_e164 for r in rows}
    return {"smsNumber": by_kind.get("sms"), "ivrNumber": by_kind.get("ivr"),
            "helpline": by_kind.get("helpline", settings.helpline_number)}


@router.get("/villages")
async def list_villages(blockCode: str | None = None, districtCode: str | None = None,  # noqa: N803
                        p: Principal = Depends(policy(*STAFF, purpose="administration"))) -> dict[str, list[VillageOut]]:
    async with uow(p) as tx:
        district = districtCode or p.district_code
        if p.role == "asha" and not blockCode and not districtCode:
            data = await serial.villages(tx.conn, p.villages)
        else:
            data = await serial.villages(tx.conn, block_code=blockCode, district_code=district)
    return {"data": data}  # type: ignore[dict-item]


@router.get("/villages/{village_id}", response_model=VillageOut)
async def get_village(village_id: uuid.UUID, p: Principal = Depends(policy(*ROLES, purpose="administration"))) -> dict[str, Any]:
    async with uow(p) as tx:
        rows = await serial.villages(tx.conn, [village_id])
    if not rows:
        raise AppError("NOT_FOUND")
    return rows[0]


@router.get("/facilities")
async def list_facilities(districtCode: str | None = None, blockCode: str | None = None,  # noqa: N803
                          level: str | None = None,
                          p: Principal = Depends(policy(*STAFF, purpose="emergency_care"))) -> dict[str, list[FacilityOut]]:
    async with uow(p) as tx:
        data = await serial.facilities(tx.conn, district_code=districtCode or p.district_code, block_code=blockCode,
                                       level=level)
    return {"data": data}  # type: ignore[dict-item]


class ManifestOut(Out):
    version: int
    url: str
    sha256: str | None = None
    size_bytes: int | None = None


@router.get("/reference/tiles/manifest", response_model=ManifestOut)
async def tiles_manifest(blockCode: str = Query(..., max_length=16),  # noqa: N803
                         p: Principal = Depends(policy("asha", "volunteer", purpose="administration"))) -> dict[str, Any]:
    """Offline MBTiles for one block (database.md §18). Tiles are packaged by the `package_tiles` bulk job."""
    return _manifest(f"blocks/{blockCode}/", ".mbtiles")


@router.get("/reference/lang/manifest", response_model=ManifestOut)
async def lang_manifest(lang: str = Query(..., pattern=r"^[a-z]{2,3}$"),
                        p: Principal = Depends(policy(*ROLES, purpose="administration", allow_pending=True))) -> dict[str, Any]:
    return _manifest(f"lang/{lang}/", ".zip")


def _manifest(prefix: str, suffix: str) -> dict[str, Any]:
    bucket = settings.bucket("tiles")
    best: tuple[int, str, int] | None = None
    try:
        client = objectstore._client(False)
        for obj in client.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", []):
            name = obj["Key"][len(prefix):]
            if name.startswith("v") and name.endswith(suffix) and name[1:-len(suffix)].isdigit():
                v = int(name[1:-len(suffix)])
                if best is None or v > best[0]:
                    best = (v, obj["Key"], obj["Size"])
    except Exception:  # noqa: BLE001
        best = None
    if best is None:
        raise AppError("NOT_FOUND", "No package published for this area yet")
    return {"version": best[0], "url": objectstore.presign_get(bucket, best[1]), "sha256": None, "sizeBytes": best[2]}
