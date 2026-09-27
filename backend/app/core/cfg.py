"""Per-district configuration as data (TRD §17.4, database.md §11.3).

District overrides sit on top of national defaults (`district_code IS NULL`); merged maps are cached in
Redis `cfg:{district|global}` for 300 s (database.md §15.2).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import or_, select

from app.core import redis as rds
from app.core.db import Conn, T

CFG_TTL_S = 300


async def merged(conn: Conn, district_code: str | None) -> dict[str, Any]:
    cache_key = rds.k(f"cfg:{district_code or 'global'}")
    try:
        raw = await rds.r().get(cache_key)
        if raw:
            return json.loads(raw)
    except Exception:  # noqa: BLE001, S110
        pass
    ce = T.config_entries
    cond = ce.c.district_code.is_(None)
    if district_code:
        cond = or_(cond, ce.c.district_code == district_code)
    rows = (await conn.execute(select(ce.c.district_code, ce.c.key, ce.c.value).where(cond))).all()
    out: dict[str, Any] = {}
    for row in sorted(rows, key=lambda r: r.district_code is not None):  # national first, district overrides
        out[row.key] = row.value
    try:
        await rds.r().set(cache_key, json.dumps(out), ex=CFG_TTL_S)
    except Exception:  # noqa: BLE001, S110
        pass
    return out


async def get(conn: Conn, key: str, district_code: str | None = None, default: Any = None) -> Any:
    return (await merged(conn, district_code)).get(key, default)


async def invalidate(district_code: str | None) -> None:
    try:
        await rds.r().delete(rds.k(f"cfg:{district_code or 'global'}"))
    except Exception:  # noqa: BLE001, S110
        pass
