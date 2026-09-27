"""Shared API shapes (API-Guide §5). camelCase JSON; request models forbid unknown fields (SEC-API-01).

Response models are declared on every route so no column can leak by accident (API-Guide §2.1): FastAPI
drops any key a serializer returns that is not declared here.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class In(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


class Out(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


Role = Literal["patient", "asha", "volunteer", "doctor", "facility_staff", "district_admin"]
CaseStatus = Literal["created", "matched", "accepted", "transport_assigned", "in_transit", "arrived_seen",
                     "closed", "follow_up", "cancelled"]
Category = Literal["pregnancy", "newborn", "injury", "breathing", "unconscious", "other"]


class GeoPoint(In):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: int | None = Field(default=None, ge=0, le=100000)


class GeoPointOut(Out):
    lat: float
    lng: float
    accuracy_m: int | None = None


# ---------------- identity ----------------
class UserOut(Out):
    id: uuid.UUID
    role: Role
    status: str
    name: str | None = None
    phone_masked: str | None = None
    staff_id: str | None = None
    preferred_language: str
    text_scale: int
    district_code: str | None = None
    home_village_id: uuid.UUID | None = None
    household_id: uuid.UUID | None = None
    patient_id: uuid.UUID | None = None
    facility_ids: list[uuid.UUID] | None = None
    village_ids: list[uuid.UUID] | None = None
    version: int


# ---------------- catalogs ----------------
class CapabilityOut(Out):
    code: str
    group: str
    label_en: str
    label_hi: str


class EmergencyCategoryOut(Out):
    code: str
    sms_letter: str
    ivr_digit: int | None = None
    label_en: str
    label_hi: str
    default_capabilities: list[str]


class SymptomOut(Out):
    code: str
    cohorts: list[str]
    label_en: str
    label_hi: str


class RiskRuleOut(Out):
    id: uuid.UUID
    code: str
    cohort: str
    rule_set_version: int
    expression: dict[str, Any]
    follow_up_days: int
    description_en: str | None = None
    description_hi: str | None = None


class WaypointOut(Out):
    id: uuid.UUID
    kind: str
    name: str
    point: GeoPointOut
    is_default: bool
    vehicle_kind_needed: str | None = None


class LinkedVillageOut(Out):
    village_id: uuid.UUID
    search_rank: int


class VillageOut(Out):
    id: uuid.UUID
    name: str
    block_code: str
    district_code: str
    location: GeoPointOut
    waypoints: list[WaypointOut] = []
    linked_villages: list[LinkedVillageOut] = []


# ---------------- households & patients ----------------
class PatientCohortOut(Out):
    id: uuid.UUID
    cohort: str
    started_on: date
    ended_on: date | None = None
    lmp_date: date | None = None
    edd_date: date | None = None


class ConsentBrief(Out):
    purpose: str
    status: str
    effective_at: datetime


class PatientOut(Out):
    id: uuid.UUID
    household_id: uuid.UUID
    short_code: str | None = None
    name: str | None = None
    sex: str
    date_of_birth: date | None = None
    dob_is_estimated: bool
    age_years: int | None = None
    relationship_to_head: str | None = None
    phone_masked: str | None = None
    blood_group: str | None = None
    abha_linked: bool = False
    cohorts: list[PatientCohortOut] = []
    high_risk: bool = False
    consents: list[ConsentBrief] = []
    recorded_at: datetime
    updated_at: datetime
    version: int


class HouseholdOut(Out):
    id: uuid.UUID
    village_id: uuid.UUID
    asha_id: uuid.UUID
    house_number: str | None = None
    head_member_id: uuid.UUID | None = None
    location: GeoPointOut | None = None
    location_source: str | None = None
    registered_phone_masked: str | None = None
    members: list[PatientOut] | None = None
    recorded_at: datetime
    created_at: datetime
    updated_at: datetime
    version: int


# ---------------- record ----------------
class Vitals(In):
    bp_systolic: int | None = Field(default=None, ge=50, le=280)
    bp_diastolic: int | None = Field(default=None, ge=20, le=180)
    pulse_bpm: int | None = Field(default=None, ge=20, le=260)
    resp_rate: int | None = Field(default=None, ge=4, le=120)
    spo2_pct: int | None = Field(default=None, ge=40, le=100)
    temp_c: float | None = Field(default=None, ge=28.0, le=44.0)
    weight_kg: float | None = Field(default=None, ge=0.30, le=250.0)
    height_cm: float | None = Field(default=None, ge=20.0, le=230.0)
    muac_cm: float | None = Field(default=None, ge=5.0, le=50.0)
    hb_g_dl: float | None = Field(default=None, alias="hbGDl", ge=2.0, le=25.0)
    rbs_mg_dl: int | None = Field(default=None, ge=20, le=900)
    fetal_hr_bpm: int | None = Field(default=None, ge=60, le=220)
    gestation_weeks: int | None = Field(default=None, ge=1, le=45)
    measured_with: Literal["manual", "digital_bp", "pulse_oximeter", "glucometer", "hb_strip", "thermometer",
                           "scale"] | None = None


class VitalsOut(Out):
    bp_systolic: int | None = None
    bp_diastolic: int | None = None
    pulse_bpm: int | None = None
    resp_rate: int | None = None
    spo2_pct: int | None = None
    temp_c: float | None = None
    weight_kg: float | None = None
    height_cm: float | None = None
    muac_cm: float | None = None
    hb_g_dl: float | None = Field(default=None, alias="hbGDl")
    rbs_mg_dl: int | None = None
    fetal_hr_bpm: int | None = None
    gestation_weeks: int | None = None
    measured_with: str | None = None


class SymptomAnswer(In):
    code: str
    present: bool


class SymptomAnswerOut(Out):
    code: str
    present: bool


class RiskFlagOut(Out):
    rule_code: str
    evaluated_by: str


class AttachmentBrief(Out):
    id: uuid.UUID
    purpose: str
    content_type: str


class HealthRecordEntryOut(Out):
    id: uuid.UUID
    patient_id: uuid.UUID
    kind: str
    author_id: uuid.UUID
    author_role: str
    author_name: str | None = None
    case_id: uuid.UUID | None = None
    teleconsult_session_id: uuid.UUID | None = None
    facility_id: uuid.UUID | None = None
    vitals: VitalsOut | None = None
    symptoms: list[SymptomAnswerOut] | None = None
    notes: str | None = None
    high_risk: bool
    risk_flags: list[RiskFlagOut] = []
    supersedes_entry_id: uuid.UUID | None = None
    entered_in_error: bool
    superseded: bool = False
    attachments: list[AttachmentBrief] | None = None
    recorded_at: datetime
    server_received_at: datetime | None = None


# ---------------- facilities ----------------
class FacilityCapabilityOut(Out):
    code: str
    available: bool
    flagged_for_review: bool


class FacilityOut(Out):
    id: uuid.UUID
    name: str
    level: str
    ownership: str
    district_code: str
    block_code: str | None = None
    location: GeoPointOut
    beds_total: int | None = None
    beds_available: int
    status: str
    status_note: str | None = None
    capabilities: list[FacilityCapabilityOut] = []
    capability_updated_at: datetime
    stale: bool
    version: int


class MatchFacility(Out):
    id: uuid.UUID
    name: str
    level: str
    location: GeoPointOut
    beds_available: int
    status: str


class MatchReasons(Out):
    capabilities_matched: list[str]
    capabilities_missing: list[str]
    eta_min: int
    eta_estimated: bool
    distance_km: float
    beds: int
    stale: bool
    capability_unconfirmed: bool


class FacilityMatchOut(Out):
    facility: MatchFacility
    rank: int
    reasons: MatchReasons


class MatchResponse(Out):
    data: list[FacilityMatchOut]
    no_capable_facility: bool
    needed_capabilities: list[str]
    computed_at: datetime


# ---------------- cases ----------------
class NeededCapabilityOut(Out):
    code: str
    source: str


class CurrentFacilityOut(Out):
    id: uuid.UUID
    name: str
    phone: str | None = None


class LegProgress(Out):
    current: int
    total: int


class CaseOut(Out):
    id: uuid.UUID
    short_code: str
    type: str
    channel: str
    patient_id: uuid.UUID | None = None
    household_id: uuid.UUID | None = None
    village_id: uuid.UUID | None = None
    district_code: str
    raised_by_id: uuid.UUID | None = None
    raised_by_role: str | None = None
    emergency_category: str | None = None
    needed_capabilities: list[NeededCapabilityOut] = []
    origin_facility_id: uuid.UUID | None = None
    override_reason: str | None = None
    status: CaseStatus
    status_changed_at: datetime
    version: int
    current_facility_id: uuid.UUID | None = None
    current_facility: CurrentFacilityOut | None = None
    current_leg_id: uuid.UUID | None = None
    transport_mode: str | None = None
    pickup_point: GeoPointOut | None = None
    location_source: str
    verified: bool
    verification_level: str
    escalation_level: int
    offer_mode: str
    cancel_reason: str | None = None
    recorded_at: datetime
    server_received_at: datetime
    accepted_at: datetime | None = None
    arrived_at: datetime | None = None
    closed_at: datetime | None = None
    leg_progress: LegProgress | None = None
    eta_min: int | None = None


class CaseEventOut(Out):
    id: uuid.UUID
    case_id: uuid.UUID
    action: str
    from_status: str | None = None
    to_status: str | None = None
    actor_id: uuid.UUID | None = None
    actor_role: str | None = None
    channel: str
    offer_id: uuid.UUID | None = None
    leg_id: uuid.UUID | None = None
    payload: dict[str, Any]
    occurred_at: datetime
    recorded_at: datetime | None = None


class FacilityOfferOut(Out):
    id: uuid.UUID
    case_id: uuid.UUID
    facility_id: uuid.UUID
    facility_name: str | None = None
    rank: int
    attempt: int
    slot: int
    source: str
    result: str
    decline_reason: str | None = None
    decline_note: str | None = None
    eta_seconds: int | None = None
    eta_estimated: bool
    distance_m: int | None = None
    stale_capability: bool
    capability_unconfirmed: bool
    match_reasons: dict[str, Any]
    offered_at: datetime
    expires_at: datetime
    opened_at: datetime | None = None
    responded_at: datetime | None = None


class CustodianOut(Out):
    user_id: uuid.UUID | None = None
    first_name: str | None = None
    phone_masked: str | None = None
    # full number only in the family / ASHA / facility projections of THIS case ("Call driver", API-Guide §2.9)
    phone: str | None = None
    public_key_ed25519: str | None = None
    device_id: uuid.UUID | None = None


class TransportLegOut(Out):
    id: uuid.UUID
    case_id: uuid.UUID
    leg_order: int
    from_kind: str
    from_point: GeoPointOut | None = None
    from_label: str | None = None
    to_kind: str
    to_point: GeoPointOut | None = None
    to_label: str | None = None
    to_facility_id: uuid.UUID | None = None
    mode: str | None = None
    vehicle_kind_needed: str | None = None
    custodian_kind: str | None = None
    custodian: CustodianOut | None = None
    vehicle_id: uuid.UUID | None = None
    status: str
    handover_state: str | None = None
    accepted_at: datetime | None = None
    picked_up_at: datetime | None = None
    handed_over_at: datetime | None = None
    eta_seconds: int | None = None
    distance_m: int | None = None
    version: int


class FacilityAdmissionOut(Out):
    case_id: uuid.UUID
    facility_id: uuid.UUID
    pre_registered_at: datetime | None = None
    facility_reg_no: str | None = None
    arrived_at: datetime | None = None
    seen_at: datetime | None = None
    outcome: str | None = None
    onward_case_id: uuid.UUID | None = None
    closed_at: datetime | None = None
    version: int


class EscalationOut(Out):
    id: uuid.UUID
    case_id: uuid.UUID
    level: int
    reason: str
    raised_at: datetime
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution: str | None = None


class CaseDetailOut(Out):
    case: CaseOut
    events: list[CaseEventOut]
    offers: list[FacilityOfferOut]
    legs: list[TransportLegOut]
    admission: FacilityAdmissionOut | None = None
    escalations: list[EscalationOut] | None = None


# ---------------- continuity ----------------
class FollowUpTaskOut(Out):
    id: uuid.UUID
    patient_id: uuid.UUID
    patient_name: str | None = None
    patient_short_code: str | None = None
    asha_id: uuid.UUID
    task_type: str
    title: str | None = None
    priority: str
    source_kind: str
    source_case_id: uuid.UUID | None = None
    due_date: date
    status: str
    done_at: datetime | None = None
    done_entry_id: uuid.UUID | None = None
    version: int


class CarePlanItemOut(Out):
    id: uuid.UUID
    kind: str
    medicine_name: str | None = None
    dose_text: str | None = None
    frequency_text: str | None = None
    duration_days: int | None = None
    due_offset_days: int | None = None
    task_type: str | None = None
    note: str | None = None
    sort_order: int


class CarePlanOut(Out):
    id: uuid.UUID
    patient_id: uuid.UUID
    case_id: uuid.UUID | None = None
    teleconsult_session_id: uuid.UUID | None = None
    author_id: uuid.UUID
    author_role: str
    facility_id: uuid.UUID | None = None
    summary: str | None = None
    next_visit_on: date | None = None
    status: str
    items: list[CarePlanItemOut]
    recorded_at: datetime


class IncentiveEntryOut(Out):
    id: uuid.UUID
    kind: str
    credits: int
    case_short_code: str | None = None
    village_name: str | None = None
    created_at: datetime
    state: str


class Page(Out):
    data: list[Any]
    next_cursor: str | None = None
    total: int | None = None
