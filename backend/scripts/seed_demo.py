"""Demo seed (database.md §11.6, PRD next step 2, TRD §17.1 `demo`).

One district, one block, 5 villages (roadheads, 2 junctions, a ranked village_links ring), 3 facilities
(PHC delivery only; CHC with EmOC + C-section + blood + oxygen; DH everything, deliberately stale by 14 h),
4 volunteers (bike, auto, tractor, car — two in the same village for "Already taken"), 2 ASHAs, 1 doctor,
2 facility staff, 1 district admin, 30 households / ~120 patients with SYNTHETIC names (never real data —
SECURITY §12.4), including a woman at 36 weeks with a high-risk BP entry.

Fixed random seed → repeatable demo. Writes go through the real services (encryption, audit, sync journal).

    python scripts/seed_demo.py            # seed (fails if already seeded)
    python scripts/seed_demo.py --if-empty # used by the compose `migrate` job
"""

from __future__ import annotations

import asyncio
import random
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import insert, select  # noqa: E402

from app.core import db  # noqa: E402
from app.core.crypto import encrypt, phone_hash  # noqa: E402
from app.core.db import T, point  # noqa: E402
from app.core.ids import uuid5  # noqa: E402
from app.core.rbac import Principal  # noqa: E402
from app.core.uow import uow  # noqa: E402

RNG = random.Random(26133)  # SIH26133
DISTRICT, BLOCK = "0915", "BLK01"
NOW = datetime.now(UTC)


def sid(name: str) -> uuid.UUID:
    return uuid5(f"demo:{name}")


VILLAGES = [  # name, lat, lng, roadhead (label, lat, lng), junction or None
    ("Nagla", 27.6205, 77.4102, ("Main road near temple", 27.6112, 77.4230), ("Pond junction", 27.6160, 77.4155)),
    ("Kamai", 27.6391, 77.4520, ("Kamai bus stop", 27.6330, 77.4611), ("School crossing", 27.6362, 77.4570)),
    ("Rankauli", 27.5960, 77.3871, ("Canal bridge", 27.5902, 77.3990), None),
    ("Sanket", 27.6610, 77.4009, ("Sanket chowk", 27.6555, 77.4101), None),
    ("Karhela", 27.6025, 77.4705, ("Karhela mandi gate", 27.5980, 77.4801), None),
]
FACILITIES = [  # key, name, level, lat, lng, beds_total, beds_avail, caps, stale_hours, duty phone
    ("phc", "PHC Goverdhan", "PHC", 27.4973, 77.4632, 6, 2, ["delivery", "doctor_on_duty", "emergency_opd", "lab_basic"], 0,
     "+919000000053"),
    ("chc", "CHC Barsana", "CHC", 27.6479, 77.3769, 30, 3,
     ["obstetric_emergency", "c_section", "blood_bank", "oxygen", "delivery", "doctor_on_duty", "emergency_opd",
      "trauma_stabilisation", "x_ray", "lab_basic", "sncu"], 0, "+919000000051"),
    ("dh", "DH Mathura", "DH", 27.4924, 77.6737, 200, 12,
     ["doctor_on_duty", "specialist_obgyn", "specialist_paediatrics", "delivery", "obstetric_emergency", "c_section",
      "sncu", "nicu", "trauma_stabilisation", "emergency_opd", "icu", "oxygen", "ventilator", "x_ray", "blood_bank",
      "lab_basic", "ambulance_base"], 14, "+919000000052"),
]
STAFF = [  # key, role, staffId, name, phone, villages / facility
    ("admin", "district_admin", "ADM-0915-01", "Rajiv Verma", "+919000000001", None),
    ("asha1", "asha", "ASHA-0915-001", "Sunita Devi", "+919000000011", ["Nagla", "Kamai", "Rankauli"]),
    ("asha2", "asha", "ASHA-0915-002", "Meena Kumari", "+919000000012", ["Sanket", "Karhela"]),
    ("doctor", "doctor", "DOC-0915-01", "Dr. Anil Sharma", "+919000000021", "chc"),
    ("staff_chc", "facility_staff", "FAC-CHC-01", "Pooja Yadav", "+919000000031", "chc"),
    ("staff_dh", "facility_staff", "FAC-DH-01", "Imran Khan", "+919000000032", "dh"),
]
VOLUNTEERS = [  # key, name, phone, village, vehicle kind
    ("vol1", "Ramesh Singh", "+919000000041", "Nagla", "bike"),
    ("vol2", "Suresh Kumar", "+919000000042", "Nagla", "auto"),
    ("vol3", "Mahesh Jat", "+919000000043", "Kamai", "tractor"),
    ("vol4", "Dinesh Chaudhary", "+919000000044", "Rankauli", "car"),
]
FIRST_F = ["Kamla", "Geeta", "Rani", "Sita", "Poonam", "Rekha", "Sunita", "Anita", "Savitri", "Radha", "Kiran", "Lata",
           "Pushpa", "Usha", "Manju", "Guddi", "Bimla", "Asha", "Neelam", "Kusum"]
FIRST_M = ["Mohan", "Ram", "Shyam", "Raju", "Vijay", "Suraj", "Hari", "Govind", "Kishan", "Balram", "Naresh", "Ajay",
           "Deepak", "Pappu", "Satish", "Om", "Prem", "Lakhan", "Bhola", "Gopal"]
LAST = ["Devi", "Singh", "Kumar", "Jatav", "Yadav", "Sharma", "Chaudhary", "Prajapati", "Kushwaha", "Baghel"]


def dob(age: int) -> date:
    return date(NOW.year - age, RNG.randint(1, 12), RNG.randint(1, 28))


async def main(if_empty: bool) -> None:
    await db.reflect()
    async with uow() as tx:
        if (await tx.conn.execute(select(T.districts.c.code).where(T.districts.c.code == DISTRICT))).first():
            if if_empty:
                print("demo seed already present — skipping")
                return
            raise SystemExit("already seeded")

    ids: dict[str, uuid.UUID] = {}
    async with uow() as tx:
        c = tx.conn
        await c.execute(insert(T.districts).values(code=DISTRICT, name="Mathura (demo)", state_code="09"))
        await c.execute(insert(T.blocks).values(code=BLOCK, district_code=DISTRICT, name="Nandgaon (demo)"))
        for name, lat, lng, rh, jn in VILLAGES:
            vid = ids[name] = sid(f"village:{name}")
            await c.execute(insert(T.villages).values(id=vid, name=name, block_code=BLOCK, district_code=DISTRICT,
                                                      location=point(lat, lng), population=RNG.randint(600, 2400)))
            await c.execute(insert(T.village_waypoints).values(id=sid(f"rh:{name}"), village_id=vid, kind="roadhead",
                                                               label=rh[0], location=point(rh[1], rh[2]), is_default=True))
            if jn:
                await c.execute(insert(T.village_waypoints).values(id=sid(f"jn:{name}"), village_id=vid, kind="junction",
                                                                   label=jn[0], location=point(jn[1], jn[2])))
        names = [v[0] for v in VILLAGES]
        for i, name in enumerate(names):  # ranked ring: next two villages
            for rank, off in enumerate((1, 2), start=1):
                await c.execute(insert(T.village_links).values(village_id=ids[name], linked_village_id=ids[names[(i + off) % 5]],
                                                               search_rank=rank))
        await c.execute(insert(T.channel_numbers).values(number_e164="+911204567890", kind="sms", district_code=DISTRICT,
                                                         provider="fake"))
        await c.execute(insert(T.channel_numbers).values(number_e164="+911204567891", kind="ivr", district_code=DISTRICT,
                                                         provider="fake"))
        for key, name, level, lat, lng, total, avail, caps, stale_h, duty in FACILITIES:
            fid = ids[key] = sid(f"facility:{key}")
            await c.execute(insert(T.facilities).values(
                id=fid, name=name, level=level, district_code=DISTRICT, block_code=BLOCK, location=point(lat, lng),
                beds_total=total, beds_available=avail,
                capability_updated_at=NOW - timedelta(hours=stale_h),
                duty_phone_enc=await encrypt(c, "external_contact", fid, "facilities", "duty_phone_enc", fid, duty),
                duty_phone_hash=phone_hash(duty)))
            for cap in caps:
                await c.execute(insert(T.facility_capabilities).values(facility_id=fid, capability_code=cap))

    async with uow() as tx:
        c = tx.conn
        admin_id = sid("user:admin")
        for key, role, staff_id, name, phone, extra in STAFF:
            uid = ids[key] = sid(f"user:{key}")
            await c.execute(insert(T.users).values(
                id=uid, role=role, status="active", staff_id=staff_id,
                name_enc=await encrypt(c, "user", uid, "users", "name_enc", uid, name),
                phone_enc=await encrypt(c, "user", uid, "users", "phone_enc", uid, phone), phone_hash=phone_hash(phone),
                district_code=DISTRICT, approved_by=None if key == "admin" else admin_id, approved_at=NOW,
                preferred_language="hi"))
        for key, role, staff_id, name, phone, extra in STAFF:
            if role == "asha":
                for v in extra:
                    await c.execute(insert(T.asha_village_assignments).values(asha_id=ids[key], village_id=ids[v],
                                                                              assigned_by=admin_id))
            elif role in ("doctor", "facility_staff"):
                await c.execute(insert(T.facility_memberships).values(user_id=ids[key], user_role=role, facility_id=ids[extra]))
        for key, name, phone, village, kind in VOLUNTEERS:
            uid = ids[key] = sid(f"user:{key}")
            await c.execute(insert(T.users).values(
                id=uid, role="volunteer", status="active", name_enc=await encrypt(c, "user", uid, "users", "name_enc", uid, name),
                phone_enc=await encrypt(c, "user", uid, "users", "phone_enc", uid, phone), phone_hash=phone_hash(phone),
                district_code=DISTRICT, home_village_id=ids[village], approved_by=ids["asha1"], approved_at=NOW))
            vlat, vlng = next((v[1], v[2]) for v in VILLAGES if v[0] == village)
            await c.execute(insert(T.volunteer_profiles).values(
                user_id=uid, home_village_id=ids[village], available=True, first_aid_trained=True,
                liability_consent_at=NOW - timedelta(days=30), verified_by=ids["asha1"], verified_at=NOW - timedelta(days=29),
                last_location=point(round(vlat, 2), round(vlng, 2)), last_location_at=NOW))
            await c.execute(insert(T.vehicles).values(id=sid(f"vehicle:{key}"), owner_user_id=uid, village_id=ids[village],
                                                      kind=kind, seats={"bike": 1, "auto": 3, "tractor": 2, "car": 4}[kind],
                                                      registration_enc=await encrypt(c, "user", uid, "vehicles", "registration_enc",
                                                                                     sid(f"vehicle:{key}"), f"UP85 D{RNG.randint(1000, 9999)}")))

    from app.core.rbac import resolve_scope
    from app.modules.continuity.service import make_task
    from app.modules.onboarding.service import create_household
    from app.modules.routine_care.service import insert_entry

    async def asha_principal(key: str) -> Principal:
        async with uow() as tx:
            s = await resolve_scope(tx.conn, ids[key])
        return Principal(user_id=ids[key], role="asha", status="active", device_id=None, jti="seed", villages=s["villages"],
                         district_code=s["district_code"], block_code=s["block_code"], purpose="continuity_of_care")

    principals = {"asha1": await asha_principal("asha1"), "asha2": await asha_principal("asha2")}
    kamla_id = sid("patient:kamla")
    family_phone = "+919812345678"
    hh_count = 0
    for vname, *_ in VILLAGES:
        asha = "asha1" if vname in ("Nagla", "Kamai", "Rankauli") else "asha2"
        p = principals[asha]
        vlat, vlng = next((v[1], v[2]) for v in VILLAGES if v[0] == vname)
        for n in range(6):
            hh_count += 1
            hid = sid(f"household:{vname}:{n}")
            last = RNG.choice(LAST)
            members = []
            head_age = RNG.randint(38, 70)
            head_sex = RNG.choice("FM")
            head = {"id": str(sid(f"patient:{vname}:{n}:0")), "name": f"{RNG.choice(FIRST_F if head_sex == 'F' else FIRST_M)} {last}",
                    "sex": head_sex, "dateOfBirth": dob(head_age).isoformat(), "dobIsEstimated": True, "relationshipToHead": "self"}
            if head_age >= 60:
                head["cohorts"] = [{"id": str(sid(f"cohort:{vname}:{n}:0")), "cohort": "elderly", "startedOn": "2026-01-01"}]
            if head_age >= 50 and RNG.random() < 0.4:
                head.setdefault("cohorts", []).append({"id": str(sid(f"cohort:{vname}:{n}:0c")), "cohort": "chronic",
                                                        "startedOn": "2025-06-01"})
            members.append(head)
            for m in range(1, RNG.randint(3, 5)):
                sex = RNG.choice("FM")
                age = RNG.randint(1, 35)
                mem = {"id": str(sid(f"patient:{vname}:{n}:{m}")), "name": f"{RNG.choice(FIRST_F if sex == 'F' else FIRST_M)} {last}",
                       "sex": sex, "dateOfBirth": dob(age).isoformat(), "dobIsEstimated": age > 5,
                       "relationshipToHead": RNG.choice(["child", "spouse", "in_law", "grandchild"])}
                if sex == "F" and 19 <= age <= 32 and RNG.random() < 0.35:
                    lmp = date.today() - timedelta(weeks=RNG.randint(8, 34))
                    mem["cohorts"] = [{"id": str(sid(f"cohort:{vname}:{n}:{m}")), "cohort": "pregnant",
                                       "startedOn": (lmp + timedelta(weeks=6)).isoformat(), "lmpDate": lmp.isoformat(),
                                       "eddDate": (lmp + timedelta(days=280)).isoformat()}]
                if age <= 1:
                    mem["cohorts"] = [{"id": str(sid(f"cohort:{vname}:{n}:{m}nb")), "cohort": "newborn",
                                       "startedOn": date.today().isoformat()}]
                members.append(mem)
            data = {"id": str(hid), "villageId": str(ids[vname]), "houseNumber": str(10 + n * 3),
                    "location": {"lat": vlat + RNG.uniform(-0.004, 0.004), "lng": vlng + RNG.uniform(-0.004, 0.004)},
                    "locationSource": "gps", "headMemberId": head["id"], "members": members,
                    "recordedAt": (NOW - timedelta(days=RNG.randint(20, 200))).isoformat()}
            if vname == "Nagla" and n == 0:
                # the demo family (API-Guide examples): Kamla, 26, 36 weeks, registered family phone
                lmp = date.today() - timedelta(weeks=36)
                members.append({"id": str(kamla_id), "name": f"Kamla {last}", "sex": "F", "dateOfBirth": dob(26).isoformat(),
                                "relationshipToHead": "in_law", "phone": "+919812345679",
                                "cohorts": [{"id": str(sid("cohort:kamla")), "cohort": "pregnant",
                                             "startedOn": (lmp + timedelta(weeks=6)).isoformat(), "lmpDate": lmp.isoformat(),
                                             "eddDate": (lmp + timedelta(days=280)).isoformat()}]})
                data["registeredPhone"] = family_phone
                data["houseNumber"] = "42"
            data["consents"] = [{"id": str(sid(f"consent:{mm['id']}")), "patientId": mm["id"], "purpose": "continuity_of_care",
                                 "status": "granted", "method": "verbal_witnessed", "language": "hi",
                                 "noticeVersion": "cc-2026-09", "effectiveAt": data["recordedAt"]} for mm in members]
            async with uow(p) as tx:
                await create_household(tx, p, data)

    # Kamla's high-risk screening (BP 146/94 at 36 weeks) → server flags preg_bp_high → urgent recheck task
    async with uow(principals["asha1"]) as tx:
        await insert_entry(tx, principals["asha1"], kamla_id, {
            "id": str(sid("entry:kamla:1")), "kind": "screening",
            "vitals": {"bpSystolic": 146, "bpDiastolic": 94, "pulseBpm": 92, "spo2Pct": 97, "tempC": 37.1, "hbGDl": 9.8,
                       "gestationWeeks": 36, "measuredWith": "digital_bp"},
            "symptoms": [{"code": "severe_headache", "present": True}, {"code": "bleeding", "present": False}],
            "notes": "Complains of swelling since 2 days", "recordedAt": (NOW - timedelta(hours=20)).isoformat()})
        pc = T.patient_conditions
        await tx.conn.execute(insert(pc).values(id=sid("cond:kamla"), patient_id=kamla_id, condition_code="anaemia",
                                                noted_on=date.today() - timedelta(days=40), recorded_by=ids["asha1"],
                                                recorded_at=NOW - timedelta(days=40)))
        await tx.conn.execute(insert(T.patient_medications).values(
            id=sid("med:kamla"), patient_id=kamla_id, medicine_name="IFA", frequency_text="1 daily",
            started_on=date.today() - timedelta(days=40), recorded_by=ids["asha1"], recorded_at=NOW - timedelta(days=40)))
    # a few routine tasks due today for the ASHA "Today" list — each task on a patient of the matching cohort
    pt, pc2 = T.patients, T.patient_cohorts
    async with uow(principals["asha1"]) as tx:
        for task_type, cohort in (("anc_visit", "pregnant"), ("newborn_check", "newborn"), ("bp_check", "chronic")):
            pid = (await tx.conn.execute(select(pt.c.id).join(T.households, T.households.c.id == pt.c.household_id)
                                         .join(pc2, pc2.c.patient_id == pt.c.id)
                                         .where(T.households.c.village_id.in_([ids["Nagla"], ids["Kamai"]]),
                                                pc2.c.cohort == cohort, pc2.c.ended_on.is_(None))
                                         .order_by(pt.c.id).limit(1))).scalar()
            if pid is not None:
                await make_task(tx, patient_id=pid, asha_id=ids["asha1"], task_type=task_type, due=date.today(),
                                source_kind="schedule", dedupe_key=f"demo:schedule:{task_type}:{pid}")

    # the family login (patient role) for the demo household
    async with uow() as tx:
        c = tx.conn
        uid = ids["family"] = sid("user:family")
        hid = sid("household:Nagla:0")
        await c.execute(insert(T.users).values(
            id=uid, role="patient", status="active", name_enc=await encrypt(c, "user", uid, "users", "name_enc", uid, "Kamla"),
            phone_enc=await encrypt(c, "user", uid, "users", "phone_enc", uid, family_phone), phone_hash=phone_hash(family_phone),
            district_code=DISTRICT, household_id=hid, patient_id=kamla_id, preferred_language="hi"))

    async with uow() as tx:
        n_pat = len((await tx.conn.execute(select(pt.c.id))).all())
    print(f"demo seed done: 5 villages, 3 facilities, {hh_count} households, {n_pat} patients")
    print("\nDemo logins (OTP appears at /api/v1/dev/sms — fake SMS gateway):")
    for key, role, staff_id, name, phone, _ in STAFF:
        print(f"  {role:15s} staffId={staff_id:14s} phone={phone}  ({name})")
    for key, name, phone, village, kind in VOLUNTEERS:
        print(f"  {'volunteer':15s} phone={phone}  ({name}, {kind}, {village})")
    print(f"  {'patient':15s} phone={family_phone}  (Kamla's family, house 42, Nagla)")
    print("  SMS SOS number: +911204567890 · IVR: +911204567891")


if __name__ == "__main__":
    asyncio.run(main("--if-empty" in sys.argv))
