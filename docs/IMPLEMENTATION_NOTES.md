# AapatMitra — Implementation Notes

What was built from the seven specs in this folder, where the specs were silent or disagreed and what
this build chose, and what is **not** done yet. Section references (TRD §x, API-Guide §x, …) point at the
spec files next to this one.

---

## 1. Scope delivered (TRD §19 delivery plan)

| Milestone | Status |
|-----------|--------|
| **M0 — Foundations** (schema, roles, migrations, auth, audit, compose stack) | Done |
| **M1 — Emergency loop** (`/sos`, SMS webhook + parser, matching, cascade + durable timers + sweep, volunteer offers, custody hand-over, facility inbox, closure → follow-up, Android outbox + sync, offline household registration) | Done, verified end-to-end (§4) |
| **M2 — Continuity & channels** (teleconsult, IVR, care plans UI, reports) | Partial — see §5 |
| **M3 — Hardening** (ABDM, load tests, pen-test fixes, Play distribution) | Not started |

## 2. Where the specs were silent — choices made in this build

| Topic | Spec text | This build |
|-------|-----------|------------|
| Facility tie-break `level_rank` | TRD match sort key names `level_rank` without defining it | Higher level first on a tie: MC, DH, SDH, CHC, PHC, SC, private (`referral/matching.py`) |
| Hand-over GPS plausibility | SEC-CUS-02 "GPS plausible against the leg's handover point", no threshold | 2 000 m (`transport/legs.py` `HANDOVER_GPS_TOLERANCE_M`) |
| Leg planning | TRD §10.1 "create legs at T1" from village waypoints | House → junction → roadhead → facility when the village has a junction and a default roadhead; house → roadhead → facility with only a roadhead; one leg otherwise |
| Audit hook | TRD §3.2 names a SQLAlchemy `after_flush` hook | The backend uses SQLAlchemy **Core**, so every service writes `audit_log` through `UoW.audit` inside the same transaction — same guarantee (a failed audit fails the request) |
| Idempotent replay body | API-Guide §2.5 "stored replay bodies contain ids and status only" | PII-bearing keys are stripped before storing; replay returns the minimal body plus `Idempotent-Replayed: true` |
| Idempotency key scope | API-Guide App. F D5 asks for (actor, key) | Migration `0002`: unique index on (actor_id, key) with `NULLS NOT DISTINCT` (webhook keys have no actor) |
| Device public key storage | SF-01 / SEC-CUS-01 "server stores the public key on devices" | `devices.public_key_spki` (migration `0002`) |
| SOS merged into an existing case | API-Guide §6.4.1 dedupe (same key, or same household < 30 min) | Android re-keys its local case row to the server id and keeps an alias, so an open tracker follows the merged case |
| Ride offers without FCM | TRD §9 FCM data messages | FCM is implemented server-side but needs a Firebase project; the Android build polls offers every 10 s while the Ride switch is ON, and polls an open case every 5 s (`track.pollAfterS`) |

## 3. Where the specs disagreed — which one this build follows

| Conflict | This build | Why |
|----------|------------|-----|
| SOS hold time: TRD FR-A03 **1.5 s** vs UI-UX §4.4 / §6.2 **3 s** | **3 s** | The UI spec is the user-facing contract ("Hold for 3 seconds" is also the translated key phrase) |
| Device signing key: SECURITY §8.1 Ed25519, with ECDSA P-256 as the API < 33 fallback | **ECDSA P-256 on all Android devices** (`DeviceKeys.kt`) | The app supports API 24+; Android Keystore has Ed25519 only from API 33. The server accepts both (SPKI DER) and the API field keeps its name `publicKeyEd25519` |
| App version header | API-Guide uses `name+build` (e.g. `1.0.0+42`) | App sends `1.0.0+1`; the server's minimum is compared on the build number |
| `CaseDetailOut` vs sync projection | `GET /cases/{id}` wraps as `{case, events, legs, …}`; `/sync` sends the flat case | Android accepts both shapes (`ChangeApplier.upsertCase`) |
| FLAG_SECURE (SEC-MOB-07) | Release builds only | Debug builds allow screenshots so the flows can be tested on an emulator |

## 4. Verified end-to-end (local compose stack + Android emulator, Pixel 6 / API 34)

* **Patient**: OTP login → sync bootstrap (gzip NDJSON snapshot) → live case card replaces SOS → tracker with hospital name + Call → cancel (server state machine).
* **Online SOS**: hold 3 s → who → what → `POST /sos` → case created and matched within seconds.
* **Offline SOS** (Wi-Fi + data off): case + P0 outbox row saved, structured SMS sent by the phone
  (`SOS 27PN7A P #06GDQNC2`), "Sent by SMS" chip; after the SMS reached the backend and data returned, the
  app's queued CreateSOS **merged into the SMS case** by its idempotency tag — one case on the server.
* **Volunteer**: 45 s incoming request with coarse area only → accept (exact pin + family/ASHA contacts
  revealed) → pickup → leg 2 receiver shows a Keystore-signed QR → giver submits → server verified the
  signature, nonce, time window and GPS → custody moved to leg 2, case `in_transit`. The server also
  correctly **rejected** a first attempt whose GPS was 6 km off (`gps_implausible`); the app now takes a
  fresh fix for hand-overs.
* **Facility cascade**: the CHC offer timed out (180 s) and moved to the DH automatically; DH acceptance
  released the later legs to volunteers.
* **ASHA**: Today (urgent → due → done), offline FTS name search, patient record, offline screening with
  on-device risk feedback → synced; the server re-evaluated it as high-risk and created its re-check task;
  referral with capability-first facility matching → case created.
* **Hindi / English** switch at runtime; offline banner and grey dot; pending-item count with plurals.
* **Tests**: backend `pytest` 19 passing; Android JVM golden tests (SMS tag + risk evaluator match the
  server's own functions) 4 passing; web typecheck / lint / production build clean; Android debug and
  R8-minified release APKs build.

## 5. Not done / known gaps (honest list)

| Area | Gap |
|------|-----|
| IVR | `/webhooks/ivr*` return 501 (API-Guide marks IVR as Phase 2). The app still *places* the IVR call as the FR-A03 fallback, and shows the helpline button |
| Teleconsult | Backend endpoints + TURN credentials exist; the web console has the doctor queue; **no Android audio-call screen yet** (ASHA screening offers "Send to hospital" and follow-up only) |
| FCM push | Server code path exists (`integrations/push.py`), not configured — needs a Firebase project + `google-services.json`. Polling is used instead (§2) |
| "AM OK" SMS ack on the phone | Server sends it; the app does not read incoming SMS (would need `RECEIVE_SMS`) — server confirmation arrives through sync instead |
| Languages | Hindi + English packs only (UI-UX §4.2 lists more); translations need native-speaker review (UI-UX §4.4) |
| Voice | No speaker/TTS prompts or voice notes yet (UI-UX §4.3) |
| Fonts | Noto Sans is not bundled; Android's system font + its Noto Devanagari fallback are used |
| Member photos | Family tiles use icons, not photos |
| Attachments on Android | Upload/presign endpoints exist; no camera/upload UI on Android |
| Release networking | `network_security_config.xml` has **placeholder certificate pins**; set the real API host and pins before a release build can talk to a server (release forbids cleartext by design) |
| ABDM, load tests, pen-test, Play `SEND_SMS` declaration | M3 / risk R-07 — not started |
| OSRM | Optional compose profile; without it the router uses haversine × 1.4 at 30 km/h and marks ETAs `etaEstimated` |
