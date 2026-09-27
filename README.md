# AapatMitra

Rural emergency referral and continuity-of-care platform: a family, ASHA worker or volunteer raises an SOS
(app, or SMS when there is no internet); the system finds a capable facility, runs an acceptance cascade,
relays the patient through volunteer drivers with verified custody hand-overs, and closes the loop with a
follow-up task for the ASHA.

The specifications this code implements are in [`docs/`](docs/) (PRD, TRD, API-Guide, database, SECURITY,
UI-UX). What was decided where the specs were silent or conflicting, what was verified and what is still
missing: [`docs/IMPLEMENTATION_NOTES.md`](docs/IMPLEMENTATION_NOTES.md).

| Folder | What | Stack |
|--------|------|-------|
| [`backend/`](backend/) | API, workers, timers, SMS channel, sync | Python 3.12, FastAPI, SQLAlchemy Core, PostgreSQL 16 + PostGIS, Redis, RabbitMQ + Celery |
| [`web/`](web/) | Web console — facility, doctor, district admin | Next.js 15, React 19, Tailwind, TanStack Query, MapLibre |
| [`android/`](android/) | App — patient/family, ASHA, volunteer driver (offline-first) | Kotlin, Jetpack Compose, Room + SQLCipher, WorkManager, Hilt |
| [`infra/`](infra/) | Local/demo stack | Docker Compose, NGINX, MinIO, coturn, Prometheus |

## Run the whole stack locally

Prerequisites: Docker Desktop, [uv](https://docs.astral.sh/uv/).

1. Generate local secrets (writes `infra/.env` and `backend/.env`, both git-ignored):

   ```bash
   cd backend && uv sync && uv run python scripts/setup_local_env.py
   ```

2. Start everything (migrations and the demo seed run automatically on first start):

   ```bash
   docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --build
   ```

3. Open the web console at <http://localhost:8080>. Codes are not really texted in the local stack — read
   every outgoing SMS (OTP codes, ride requests, "AM OK" replies) at <http://localhost:8080/api/v1/dev/sms>,
   where you can also send an inbound SMS such as `SOS 27PN7A P 27.6025,77.4705`.

Optional compose profiles: `--profile teleconsult` (coturn), `--profile routing` (OSRM; otherwise ETAs are
estimated), `--profile monitoring` (Prometheus).

## Demo logins (synthetic seed data)

| Who | Log in with |
|-----|-------------|
| District admin (web) | Staff ID `ADM-0915-01` |
| Facility staff (web) | `FAC-CHC-01` (CHC Barsana), `FAC-DH-01` (DH Mathura — seeded 14 h stale) |
| Doctor (web) | `DOC-0915-01` |
| ASHA (app) | `ASHA-0915-001`, `ASHA-0915-002` |
| Volunteer drivers (app) | phones `9000000041` … `9000000044` |
| Family (app) | phone `9812345678` — Kamla, 36 weeks, high-risk BP, health ID `27PN7A` |

The SMS number for district 0915 is `+911204567890`.

## Android app

Prerequisites: Android Studio (its bundled JDK is used), an emulator or a device.

```bash
cd android && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew assembleDebug
```

```bash
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
```

The debug build talks to `http://10.0.2.2:8080/api/v1/` (the host's NGINX as seen from the emulator). For a
phone on the same network pass `-PapiBaseUrl=http://<your-machine-ip>:8080/api/v1/`. Release builds refuse
cleartext and pin certificates — set the real host and pins in
`app/src/main/res/xml/network_security_config.xml` first.

To try the no-internet path: turn off Wi-Fi and mobile data, hold SOS for 3 seconds; the phone sends the
structured SMS itself. In the local stack paste that SMS text into the dev SMS console to deliver it, then
turn data back on — the app's queued request merges into the same case.

## Tests

```bash
cd backend && uv run pytest -q
```

```bash
cd web && npm ci && npm run typecheck && npm run lint && npm run build
```

```bash
cd android && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew testDebugUnitTest
```

Backend tests need the compose data stores running (they use a separate `aapatmitra_test` database).

## Security notes

* Every secret in `.env` files is for local/demo use only; staging and production secrets come from a
  vault (SECURITY SEC-INF-09). Never commit `.env` files.
* The seed contains synthetic people only. Do not load real patient data into a demo stack.
