"""Serializers for reference entities (API-Guide §5) — shared by REST and /sync pulls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.db import Conn, T, lat_of, lng_of

STALE_AFTER = timedelta(hours=12)  # matching.stale_after_h default (database.md §11.3)


async def villages(conn: Conn, ids: list[Any] | None = None, *, block_code: str | None = None,
                   district_code: str | None = None) -> list[dict[str, Any]]:
    v, w, vl = T.villages, T.village_waypoints, T.village_links
    q = select(v.c.id, v.c.name, v.c.block_code, v.c.district_code, lat_of(v.c.location).label("lat"),
               lng_of(v.c.location).label("lng")).where(v.c.deleted_at.is_(None))
    if ids is not None:
        q = q.where(v.c.id.in_(ids))
    if block_code:
        q = q.where(v.c.block_code == block_code)
    if district_code:
        q = q.where(v.c.district_code == district_code)
    rows = (await conn.execute(q.order_by(v.c.name))).all()
    vids = [r.id for r in rows]
    wps: dict[Any, list[dict[str, Any]]] = {}
    links: dict[Any, list[dict[str, Any]]] = {}
    if vids:
        for r in (await conn.execute(select(w.c.id, w.c.village_id, w.c.kind, w.c.label, w.c.is_default,
                                            lat_of(w.c.location).label("lat"), lng_of(w.c.location).label("lng"))
                                     .where(w.c.village_id.in_(vids), w.c.deleted_at.is_(None)))).all():
            wps.setdefault(r.village_id, []).append({
                "id": r.id, "kind": r.kind, "name": r.label, "isDefault": r.is_default,
                "point": {"lat": float(r.lat), "lng": float(r.lng)},
                "vehicleKindNeeded": "two_wheeler_ok" if r.kind == "junction" else "four_wheeler"})
        for r in (await conn.execute(select(vl).where(vl.c.village_id.in_(vids)).order_by(vl.c.search_rank))).all():
            links.setdefault(r.village_id, []).append({"villageId": r.linked_village_id, "searchRank": r.search_rank})
    return [{"id": r.id, "name": r.name, "blockCode": r.block_code, "districtCode": r.district_code,
             "location": {"lat": float(r.lat), "lng": float(r.lng)}, "waypoints": wps.get(r.id, []),
             "linkedVillages": links.get(r.id, [])} for r in rows]


def facility_columns() -> list[Any]:
    f = T.facilities
    return [f.c.id, f.c.name, f.c.level, f.c.ownership, f.c.district_code, f.c.block_code,
            lat_of(f.c.location).label("lat"), lng_of(f.c.location).label("lng"), f.c.beds_total,
            f.c.beds_available, f.c.status, f.c.status_note, f.c.capability_updated_at, f.c.version]


async def facilities(conn: Conn, ids: list[Any] | None = None, *, district_code: str | None = None,
                     block_code: str | None = None, level: str | None = None) -> list[dict[str, Any]]:
    f, fc = T.facilities, T.facility_capabilities
    q = select(*facility_columns()).where(f.c.deleted_at.is_(None))
    if ids is not None:
        q = q.where(f.c.id.in_(ids))
    if district_code:
        q = q.where(f.c.district_code == district_code)
    if block_code:
        q = q.where(f.c.block_code == block_code)
    if level:
        q = q.where(f.c.level == level)
    rows = (await conn.execute(q.order_by(f.c.name))).all()
    caps: dict[Any, list[dict[str, Any]]] = {}
    if rows:
        for r in (await conn.execute(select(fc).where(fc.c.facility_id.in_([x.id for x in rows]))
                                     .order_by(fc.c.capability_code))).all():
            caps.setdefault(r.facility_id, []).append({"code": r.capability_code, "available": r.available,
                                                       "flaggedForReview": r.flagged_for_review})
    now = datetime.now(UTC)
    return [{"id": r.id, "name": r.name, "level": r.level, "ownership": r.ownership, "districtCode": r.district_code,
             "blockCode": r.block_code, "location": {"lat": float(r.lat), "lng": float(r.lng)},
             "bedsTotal": r.beds_total, "bedsAvailable": r.beds_available, "status": r.status,
             "statusNote": r.status_note, "capabilities": caps.get(r.id, []),
             "capabilityUpdatedAt": r.capability_updated_at, "stale": r.capability_updated_at < now - STALE_AFTER,
             "version": r.version} for r in rows]


async def catalog(conn: Conn, district_code: str | None) -> dict[str, Any]:
    from app.core import cfg

    cap, ec, ccd, sy, rr = (T.capabilities, T.emergency_categories, T.category_capability_defaults, T.symptoms,
                            T.risk_rules)
    capabilities = [{"code": r.code, "group": r.group_name, "labelEn": r.label_en, "labelHi": r.label_hi}
                    for r in (await conn.execute(select(cap).where(cap.c.active).order_by(cap.c.sort_order))).all()]
    defaults: dict[str, list[str]] = {}
    for r in (await conn.execute(select(ccd).where(ccd.c.condition_code.is_(None),
                                                   (ccd.c.district_code.is_(None)) | (ccd.c.district_code == district_code)))).all():
        defaults.setdefault(r.category_code, []).append(r.capability_code)
    categories = [{"code": r.code, "smsLetter": r.sms_letter, "ivrDigit": r.ivr_digit, "labelEn": r.label_en,
                   "labelHi": r.label_hi, "defaultCapabilities": sorted(defaults.get(r.code, []))}
                  for r in (await conn.execute(select(ec).where(ec.c.active).order_by(ec.c.sort_order))).all()]
    symptoms = [{"code": r.code, "cohorts": [r.cohort] if r.cohort else [], "labelEn": r.label_en, "labelHi": r.label_hi}
                for r in (await conn.execute(select(sy).where(sy.c.active).order_by(sy.c.cohort, sy.c.sort_order))).all()]
    rules_rows = (await conn.execute(select(rr).where(rr.c.retired_at.is_(None),
                                                      (rr.c.district_code.is_(None)) | (rr.c.district_code == district_code))
                                     .order_by(rr.c.code))).all()
    version = max((r.rule_set_version for r in rules_rows), default=1)
    rules = [{"id": r.id, "code": r.code, "cohort": r.cohort, "ruleSetVersion": r.rule_set_version,
              "expression": r.expression, "followUpDays": r.follow_up_days, "descriptionEn": r.description_en,
              "descriptionHi": r.description_hi} for r in rules_rows]
    config = await cfg.merged(conn, district_code)
    client_keys = ("cascade.offer_timeout_s", "volunteer.offer_expiry_s", "sms.dedupe_window_min", "i18n.languages",
                   "matching.stale_after_h", "facility.capability_reminder_after_h", "volunteer.round_timeout_s")
    import hashlib
    import json

    blob = json.dumps([capabilities, categories, symptoms, rules, config], sort_keys=True, default=str).encode()
    return {"capabilities": capabilities, "emergencyCategories": categories, "symptoms": symptoms, "riskRules": rules,
            "ruleSetVersion": version, "config": {k: config[k] for k in client_keys if k in config},
            "catalogVersion": hashlib.sha256(blob).hexdigest()[:16]}
