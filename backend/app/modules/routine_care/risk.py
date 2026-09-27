"""Rule-based high-risk thresholds (TRD §12A, ADR-08, database.md §11.4).

Expression grammar (identical evaluator on Android, golden-file tested):
  {"any"|"all": [ {"field": <entry_vitals column>, "op": "<"|"<="|">"|">="|"=", "value": n}
                | {"symptom": <code>, "present": true} ]}
Rules are evaluated on the device (offline) and re-evaluated on the server; the server result wins.
A cohort-specific rule applies only to patients with that active cohort; `any` applies to everyone.
"""

from __future__ import annotations

import operator
from typing import Any

from sqlalchemy import and_, or_, select

from app.core.db import Conn, T

OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "=": operator.eq}

# API camelCase vitals → entry_vitals columns
VITAL_COLUMNS = {"bpSystolic": "bp_systolic", "bpDiastolic": "bp_diastolic", "pulseBpm": "pulse_bpm",
                 "respRate": "resp_rate", "spo2Pct": "spo2_pct", "tempC": "temp_c", "weightKg": "weight_kg",
                 "heightCm": "height_cm", "muacCm": "muac_cm", "hbGDl": "hb_g_dl", "rbsMgDl": "rbs_mg_dl",
                 "fetalHrBpm": "fetal_hr_bpm", "gestationWeeks": "gestation_weeks", "measuredWith": "measured_with"}


def _term(term: dict[str, Any], vitals: dict[str, Any], symptoms: dict[str, bool]) -> bool:
    if "symptom" in term:
        return symptoms.get(term["symptom"]) is True
    value = vitals.get(term.get("field", ""))
    if value is None:
        return False
    fn = OPS.get(term.get("op", ""))
    return bool(fn and fn(float(value), float(term["value"])))


def evaluate(expression: dict[str, Any], vitals: dict[str, Any], symptoms: dict[str, bool]) -> bool:
    if "all" in expression:
        terms = expression["all"]
        return bool(terms) and all(_term(t, vitals, symptoms) for t in terms)
    if "any" in expression:
        return any(_term(t, vitals, symptoms) for t in expression["any"])
    return False


async def active_rules(conn: Conn, district: str | None, cohorts: set[str]) -> list[Any]:
    rr = T.risk_rules
    rows = (await conn.execute(select(rr).where(and_(
        rr.c.retired_at.is_(None), or_(rr.c.district_code.is_(None), rr.c.district_code == district),
        rr.c.cohort.in_(list(cohorts | {"any"})))))).all()
    # district rule with the same code overrides the national one; highest version wins
    best: dict[str, Any] = {}
    for r in sorted(rows, key=lambda r: (r.district_code is not None, r.rule_set_version)):
        best[r.code] = r
    return list(best.values())


async def evaluate_patient(conn: Conn, *, patient_id: Any, district: str | None, vitals_cols: dict[str, Any],
                           symptoms: dict[str, bool]) -> list[Any]:
    pc = T.patient_cohorts
    cohorts = {r.cohort for r in (await conn.execute(select(pc.c.cohort).where(and_(
        pc.c.patient_id == patient_id, pc.c.ended_on.is_(None))))).all()}
    return [r for r in await active_rules(conn, district, cohorts) if evaluate(r.expression, vitals_cols, symptoms)]
