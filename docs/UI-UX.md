# AapatMitra — UI / UX Specification

**Project:** AapatMitra — Rural Healthcare Access & Continuity ("Help. Reach. Save.")
**Team:** RescueX (SIH 2026, Team ID 128052) · **Problem Statement:** SIH26133
**Surfaces:** Android app (Kotlin + Jetpack Compose) · Web Console (Next.js + React + Tailwind) · SMS / IVR fallback

---

## 0. The one rule

> **Simple beats complete.** Our users are ASHA workers, village families and volunteer drivers. Many use a basic Android phone, on weak network, in bright sunlight, sometimes in a panic. If a screen needs explaining, it is too complicated.

Every screen in this document must pass the **Simplicity Checklist** (Section 12) before it is built.

---

## 1. Design Principles

| # | Principle | What it means in practice |
|---|-----------|---------------------------|
| 1 | **One screen = one job** | Each screen has one clear purpose and **one primary button**. |
| 2 | **Tap, don't type** | Use big buttons, pictures and number pads. Free-text typing is always optional. |
| 3 | **Icon + word, always** | Never an icon alone, never a colour alone. |
| 4 | **Works offline first** | Every action saves locally. The user always sees whether it was *Saved*, *Sent* or *Sent by SMS*. |
| 5 | **Emergency is one tap away** | The SOS button is on the patient home screen, always, in the same place. |
| 6 | **Speak the user's language** | Their own language, plain words, and a voice option. No acronyms on the patient side. |
| 7 | **Show the next step** | After every action, tell the user what happens next and who is responsible. |
| 8 | **Borrow what people already know** | Look like the government health apps they already use (ABHA, Ayushman, eSanjeevani) so the app feels official and trusted. |

---

## 2. Users and What They See

| User | Surface | Their main job | Home screen shows |
|------|---------|----------------|-------------------|
| **Patient / Family** | Android app + SMS/IVR | Get help, see health card, track a case | Big **SOS** button + My Family + Call ASHA |
| **ASHA / Frontline worker** | Android app | Onboard households, screen, follow up, raise referrals/SOS | **Today's tasks** list |
| **Transport Volunteer / Driver** | Android app + SMS | Accept a ride, pick up, hand over | **Available / Not available** switch + current ride |
| **Doctor** | Web Console | Teleconsult, refer | Teleconsult queue |
| **Healthcare Facility** | Web Console | Keep capability updated, accept cases | Incoming referrals + capability panel |
| **District Admin** | Web Console | Monitor, escalate | Open cases map + stuck cases |

> The Android app is **one app with role-based home screens**. The user picks their role once; they never see screens meant for another role.

---

## 3. Visual Foundations

### 3.1 Colour palette

The palette follows what is common across Indian government health products (deep blue base, saffron/green accents, a fixed status-colour set), so AapatMitra feels official and familiar. **Red is reserved for emergency only.**

| Token | Hex | Use | Never use for |
|-------|-----|-----|---------------|
| `primary` (Mitra Blue) | `#1E4FA3` | App bar, primary buttons, links, selected tab | Errors |
| `primary-dark` | `#163B7A` | Pressed state, headers on web | — |
| `primary-tint` | `#EAF1FB` | Card and section backgrounds | Text |
| `emergency` (SOS Red) | `#D32F2F` | **Only** the SOS button, active emergency banner, "Critical" | Normal errors, decoration |
| `success` (Green) | `#1E8E3E` | Accepted, arrived, completed, synced | — |
| `warning` (Amber) | `#F9A825` | Pending, waiting, due today | Text on white (low contrast) |
| `accent` (Saffron) | `#F7931E` | Small highlights, badges, incentive points | Large backgrounds |
| `offline` (Grey) | `#6B7280` | Offline / queued state | — |
| `surface` | `#F4F6FA` | Screen background | — |
| `card` | `#FFFFFF` | Cards | — |
| `text` | `#1F2937` | Body text | — |
| `text-muted` | `#5B6475` | Helper text | Important info |

**Status colour set (same everywhere — app, web, SMS icons):**

| Meaning | Colour | Word shown | Icon |
|---------|--------|-----------|------|
| Done / OK / Accepted | Green | "Done", "Accepted", "Arrived" | ✔ tick |
| Waiting / Pending / Due | Amber | "Waiting", "Due today" | ⏳ clock |
| Problem / Declined / Overdue | Red | "Declined", "Overdue" | ✖ cross |
| Offline / Queued | Grey | "Saved – will send" | ☁ cloud-off |

Rules:
- Text on coloured backgrounds must meet **4.5 : 1 contrast** (WCAG 2.1 AA). Amber is used for chips and icons with dark text, never as white text.
- Test every screen in grayscale: it must still make sense.

### 3.2 Typography

| Role | Font | Size (app) | Weight |
|------|------|-----------|--------|
| Screen title | Noto Sans | 22 sp | Bold |
| Section title | Noto Sans | 18 sp | Semi-bold |
| Body | Noto Sans | 16 sp (**minimum**) | Regular |
| Button label | Noto Sans | 18 sp | Semi-bold |
| Big numbers (token, ETA, vitals) | Noto Sans | 32–40 sp | Bold |
| Helper text | Noto Sans | 14 sp | Regular |

- **Noto Sans / Noto Sans Devanagari** (and other Noto scripts) so every Indian language renders without broken boxes.
- Line height **1.5** for Latin, **1.7–1.8** for Indic scripts.
- Use `sp` on Android and `rem` on web so the user's phone font-size setting works.
- Web Console body text: 15–16 px.

### 3.3 Spacing, size and shape

| Item | Value |
|------|-------|
| Base spacing unit | 8 dp |
| Screen padding | 16 dp |
| **Minimum touch target** | **56 dp** tall (larger than the usual 48 dp — users may be in a moving vehicle or in a hurry) |
| Primary button | Full width, 56–64 dp tall, 12 dp corner radius |
| SOS button | Circle, **at least 160 dp** diameter |
| Cards | 12 dp radius, light shadow or 1 dp border |
| Icons | 28–32 dp, filled style, always with a label |

### 3.4 Iconography and images

- Filled, simple icons (Material Symbols filled). No thin line icons; they are hard to see in sunlight.
- Use **pictures for choices** where literacy may be low: pregnancy, accident, breathing trouble, newborn, fever, other.
- No decorative illustrations on task screens. Illustrations are allowed only on empty states and onboarding.

---

## 4. Language, Text and Voice

### 4.1 Rules for every string

1. Plain words, short sentences (max ~8 words for buttons and titles).
2. **No acronyms on patient, family and volunteer screens.** Write "Health card" not "ABHA", "Big hospital" / "Health centre" not "CHC/PHC". ASHA and doctor screens may show the acronym after the plain word: "Health ID (ABHA)".
3. Verbs on buttons: "Send help request", "Accept ride", "I have arrived".
4. Always say **what happens next**: "Request sent. Your ASHA and a driver are being informed."
5. Numbers as digits; time as "in 10 minutes", not timestamps, on patient screens.

### 4.2 Languages

- First launch: pick language from **large tiles showing the language in its own script** (हिन्दी, English, বাংলা, मराठी, తెలుగు, தமிழ், ગુજરાતી, ಕನ್ನಡ, ଓଡ଼ିଆ, ਪੰਜਾਬੀ, മലയാളം, অসমীয়া…).
- Language can be changed from the top bar on every screen (globe icon + current language name).
- All strings live in resource files (`strings.xml` / i18n JSON). No hard-coded text.

### 4.3 Voice and audio

- A **speaker icon** next to key instructions reads them aloud.
- **Voice note** option wherever typing is allowed (symptoms, notes).
- IVR menus mirror the app's choices in the same order, so "Press 1" means the same thing as the first tile.

### 4.4 Key phrases (English → Hindi reference)

| Purpose | English | Hindi |
|---------|---------|-------|
| SOS button | Need help now | अभी मदद चाहिए |
| Hold instruction | Hold for 3 seconds | 3 सेकंड दबाकर रखें |
| Request sent | Help request sent | मदद का अनुरोध भेजा गया |
| Sent by SMS | Sent by SMS (no internet) | SMS से भेजा गया (इंटरनेट नहीं) |
| Accept | Accept | स्वीकार करें |
| Decline | Can't go | नहीं जा सकते |
| Arrived | I have arrived | मैं पहुँच गया/गई |
| Cancel | Cancel | रद्द करें |
| Saved offline | Saved. Will send when network returns | सेव हो गया। नेटवर्क आने पर भेजेंगे |
| Call | Call | कॉल करें |

> Native speakers on the team must review every translation before the demo.

---

## 5. Global Layout (Android App)

```
┌──────────────────────────────────┐
│ [Logo] AapatMitra   🌐 हिन्दी  ●  │  ← Top bar: logo, language, network dot
├──────────────────────────────────┤
│ ☁ Offline – 2 items will send    │  ← Connectivity banner (only when needed)
├──────────────────────────────────┤
│                                  │
│        ONE MAIN THING            │  ← Hero area (SOS / today's task / current ride)
│                                  │
│  ┌──────────┐  ┌──────────┐      │
│  │  Tile 1  │  │  Tile 2  │      │  ← Max 4 tiles below the hero
│  └──────────┘  └──────────┘      │
│                                  │
├──────────────────────────────────┤
│  🏠 Home   📋 Tasks   👤 Me       │  ← Bottom bar: max 3–4 tabs
└──────────────────────────────────┘
```

**Layout rules**

- **Top bar:** logo + app name left; language switch + network status dot right (green = online, grey = offline).
- **Connectivity banner** appears only when offline or when items are waiting to sync. It never hides the SOS button.
- **Hero area:** one element that matches the role's main job.
- **Bottom navigation:** 3 tabs (4 at most). Labels always visible.
- **Primary button** sits at the bottom of the screen, within thumb reach.
- **Back** always works and never loses entered data (drafts auto-save).

### 5.1 Trust elements

- The AapatMitra logo appears on the splash and top bar.
- The **About** screen shows the project's association with public health services (ASHA / NHM workflows) and data-privacy information in plain words. Only show the State Emblem if the project is officially adopted, as its use is legally restricted.
- Patient-facing screens show **who** is helping: the ASHA's name, the driver's name and the facility's name, each with a **Call** button.

---

## 6. Screen Specifications — Android App

### 6.1 First launch (all roles)

**Flow:** Splash → Language → Role → Phone number + OTP → Home

```
 LANGUAGE                  ROLE                        LOGIN
┌──────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
│ Choose language  │   │ Who are you?         │   │ Enter mobile number  │
│ ┌──────┐┌──────┐ │   │ ┌──────────────────┐ │   │ ┌──────────────────┐ │
│ │हिन्दी ││English│ │   │ │ 👪 Patient/Family │ │   │ │ +91 ___________  │ │
│ └──────┘└──────┘ │   │ ├──────────────────┤ │   │ └──────────────────┘ │
│ ┌──────┐┌──────┐ │   │ │ 👩‍⚕️ ASHA worker   │ │   │                      │
│ │বাংলা ││मराठी │ │   │ ├──────────────────┤ │   │ [ Send OTP ]         │
│ └──────┘└──────┘ │   │ │ 🚗 Volunteer      │ │   │                      │
│      ...         │   │ │    driver         │ │   │ No phone? Ask your   │
│                  │   │ └──────────────────┘ │   │ ASHA to register you │
└──────────────────┘   └──────────────────────┘   └──────────────────────┘
```

- OTP screen: 6 large boxes, auto-read the SMS where allowed, "Resend in 30s".
- **ASHA-assisted registration:** an ASHA can register a family from her phone. The family doesn't need a smartphone.
- Staff roles (ASHA, volunteer) are verified by the district/facility before they get full access. Until then they see "Waiting for approval" with a Call button.
- Ask for permissions (location, SMS, notifications) **one at a time, at the moment they are needed**, with one sentence explaining why.

### 6.2 Patient / Family

**Bottom tabs:** Home · My Family · Me

#### Home

```
┌──────────────────────────────────┐
│ AapatMitra            🌐  ●      │
├──────────────────────────────────┤
│                                  │
│           ┌────────┐             │
│          │   🆘     │            │
│          │ NEED HELP │           │  ← Red circle, 160 dp+
│          │   NOW     │           │
│           └────────┘             │
│      Hold for 3 seconds          │
│                                  │
│ ┌──────────────┐┌──────────────┐ │
│ │ 📞 Call ASHA ││ 🪪 Health card│ │
│ │  Sunita Devi ││              │ │
│ └──────────────┘└──────────────┘ │
│ ┌──────────────────────────────┐ │
│ │ ⏳ Next visit: Tomorrow       │ │  ← Only if a follow-up exists
│ └──────────────────────────────┘ │
├──────────────────────────────────┤
│  🏠 Home   👪 Family   👤 Me       │
└──────────────────────────────────┘
```

- If an emergency is active, the SOS button is replaced by the **Live case card** (6.2.3).

#### SOS flow (maximum 2 choices, then send)

1. **Hold SOS for 3 seconds.** A ring fills up and the phone vibrates. This stops accidental taps. Releasing early cancels.
2. **Who needs help?** Family member photos/names as big tiles, pre-selected to the phone owner.
3. **What happened?** 6 picture tiles: Pregnancy/Labour · Newborn/Child · Accident/Injury · Breathing trouble · Unconscious · Other.
4. Sent. Location attached automatically.

- Steps 2 and 3 each have a **"Skip – send now"** option. Help must never wait for a form.
- **No internet →** the request goes out as a formatted SMS automatically, and the screen says "Sent by SMS".
- **No SMS either →** show one big button: "Call helpline" (IVR number).

#### Live case card (patient view)

Plain-language version of the case lifecycle:

```
┌──────────────────────────────────┐
│ 🔴 Help is on the way            │
│                                  │
│ ✔ Request received       10:02   │
│ ✔ Hospital found: CHC Barsana    │
│ ✔ Hospital said YES              │
│ ⏳ Driver: Ramesh — 12 min away   │
│ ○ Reach hospital                  │
│                                  │
│ [ 📞 Call driver ]  [ 📞 Call ASHA]│
│                                  │
│ Cancel request                   │  ← small text link, asks to confirm
└──────────────────────────────────┘
```

- Show **names and a Call button**, not maps. A map is optional behind "See on map".
- ETA in minutes, updated when connected. Offline: "Last update 5 min ago".

#### My Family

- List of family members (photo, name, age), each opening a simple **Health card**: blood group, known conditions, current medicines, pregnancy/newborn status, last visit, next visit.
- Families view their record. Only ASHA and doctors edit it.

### 6.3 ASHA Worker

**Bottom tabs:** Today · Households · SOS · Me

The ASHA is the power user, but she is still in the field with one hand busy. Everything is a list of clear tasks.

#### Today (home)

```
┌──────────────────────────────────┐
│ Today · 6 tasks          🌐  ●   │
├──────────────────────────────────┤
│ 🔴 1 URGENT                       │
│ ┌──────────────────────────────┐ │
│ │ Referral waiting – Geeta (26) │ │
│ │ Hospital not answered 15 min  │ │
│ │               [ Open ]        │ │
│ └──────────────────────────────┘ │
│ ⏳ DUE TODAY                      │
│ ┌──────────────────────────────┐ │
│ │ 🤰 Follow-up · Kamla  (ANC)   │ │
│ │ 👶 Newborn check · Baby of Rani│ │
│ │ 💊 BP check · Mohan (61)      │ │
│ └──────────────────────────────┘ │
│ ✔ DONE (3)                ▼      │
├──────────────────────────────────┤
│ 📅 Today  🏠 Houses  🆘 SOS  👤 Me │
└──────────────────────────────────┘
```

- Grouped **Urgent → Due today → Done**. Urgent items sit on top with a red label.
- Tap a task → the patient screen with the task's action button already shown.

#### Households

- Search by name or house number, plus a village filter.
- **Add household** wizard: Head of family → Members (name, age, sex, phone optional) → Special status chips (Pregnant, Newborn, Chronic illness, Elderly) → Save. Each step is one screen.

#### Patient record (ASHA view)

- Top: name, age, photo, status chips (e.g. "Pregnant – 7 months").
- Three sections: **Health summary** · **Visits & screenings** (timeline) · **Referrals**.
- One primary button: **Start screening**.

#### Screening (one question per screen)

```
┌──────────────────────────────────┐
│ ← Screening · Kamla     Step 2/6 │
│ ▓▓▓▓▓▓░░░░░░░░░░░░               │
│                                  │
│ Blood pressure                   │
│                                  │
│   ┌──────┐   /   ┌──────┐        │
│   │ 150  │       │  96  │        │  ← Number pad input, big digits
│   └──────┘       └──────┘        │
│   ⚠ Higher than normal           │  ← Instant plain feedback
│                                  │
│ [ Skip ]            [ Next → ]   │
└──────────────────────────────────┘
```

- Vitals use **number pads**. Symptoms use **Yes / No** tiles.
- Normal/abnormal feedback shown immediately with colour + words.
- Last screen: **Result** → "Care needed?"
  - **No** → set follow-up date (preset chips: 1 week, 2 weeks, 1 month).
  - **Yes** → choose **Talk to doctor** (teleconsult) or **Send to hospital** (referral).

#### Teleconsult (low bandwidth)

- **Audio first.** Video is an optional toggle.
- Before the call, the doctor automatically receives the patient summary and latest vitals.
- Call screen: doctor name, large Mute / Speaker / End buttons, connection bar ("Weak network – audio only").
- After the call, the doctor's care plan appears as **tasks** in the ASHA's Today list.

#### Referral — facility matching

```
┌──────────────────────────────────┐
│ ← Send to hospital · Geeta       │
├──────────────────────────────────┤
│ Need: Delivery care + Blood      │  ← From screening, editable chips
│                                  │
│ ┌──────────────────────────────┐ │
│ │ CHC Barsana        14 km      │ │
│ │ ✔ Delivery ✔ Blood ✔ Doctor now│ │
│ │ [ Send request ]              │ │
│ ├──────────────────────────────┤ │
│ │ District Hospital   32 km     │ │
│ │ ✔ Delivery ✔ Blood ✔ ICU      │ │
│ └──────────────────────────────┘ │
│ Updated 20 min ago               │
└──────────────────────────────────┘
```

- The system ranks facilities by **capability first, then distance**. At most 3 are shown.
- Capability shown as ✔ chips in plain words. A missing capability shows as ✖ in grey.
- After sending: "Waiting for CHC Barsana to accept". If there is no answer in the set time, the next facility is asked automatically (acceptance cascade), and the ASHA sees each step.
- **Referral tracker** uses the same stepper as the patient view, with more detail (times, who accepted).

#### SOS (ASHA)

- The same one-tap SOS, but the ASHA first picks the **patient from her households** (search or recent).
- ASHA-raised SOS shows the patient's health summary to the facility and driver.

### 6.4 Transport Volunteer / Driver

**Bottom tabs:** Ride · My Points · Me

#### Ride (home)

```
┌──────────────────────────────────┐
│ AapatMitra              🌐  ●    │
├──────────────────────────────────┤
│   I am available  [ ●━━━ ON ]    │  ← One big switch
│                                  │
│  No ride right now.              │
│  We will ring when someone       │
│  needs help.                     │
├──────────────────────────────────┤
│  🚗 Ride   ⭐ Points   👤 Me       │
└──────────────────────────────────┘
```

#### Incoming request (full screen, loud ringtone, also sent by SMS)

```
┌──────────────────────────────────┐
│ 🔴 EMERGENCY RIDE                │
│                                  │
│ Pregnancy – labour               │
│ Pickup: Nagla village, near      │
│ temple · 3 km                    │
│ Take to: Roadhead (Main road)    │
│                                  │
│ [      ✔ ACCEPT      ]  ← green, 64 dp
│ [    ✖ Can't go      ]  ← outline
│                                  │
│ Auto-skip in 0:45                │
└──────────────────────────────────┘
```

- **Accept** → a simple step list for **their leg only**: Go to pickup → Picked up → Reached handover point → Handed over.
- Each step is **one big button**: "I have picked up", "I have handed over". Each tap records custody for that leg.
- "Open in Maps" button for directions (OpenStreetMap / phone's maps app).
- Call patient/ASHA buttons are always visible.
- **SMS mode:** a volunteer without data replies `1` (accept) or `2` (can't go) by SMS.

#### My Points

- Total points and "Rides helped this month" as big numbers.
- A short list of **verified** rides (date, village, ✔ Verified / ⏳ Pending).
- Leaderboard: **village-level top 5 only.** Keep it friendly, not competitive.

### 6.5 "Me" tab (all roles)

- Name, phone, role, village.
- Language.
- Text size (A / A+ / A++).
- Help: short videos + "Call support".
- Privacy: "Who can see my records" in plain words, with consent history for patients.
- Log out.

---

## 7. Web Console (Doctor · Facility · District Admin)

Desktop-first but responsive. It follows the government-portal pattern users already know, **without** the clutter.

### 7.1 Global layout

```
┌──────────────────────────────────────────────────────────────┐
│ Skip to content · A- A A+ · Contrast · 🌐 हिन्दी/English      │ ← thin utility strip
├──────────────────────────────────────────────────────────────┤
│ [Logo] AapatMitra · CHC Barsana          🔔 3   Dr. Sharma ▾ │ ← header
├───────────────┬──────────────────────────────────────────────┤
│ ▸ Incoming    │                                              │
│ ▸ In transit  │            MAIN WORK AREA                    │
│ ▸ Patients    │                                              │
│ ▸ Capability  │                                              │
│ ▸ Reports     │                                              │
└───────────────┴──────────────────────────────────────────────┘
```

- Left sidebar has a **maximum of 5 items** per role.
- The notification bell shows new referrals and escalations, and new emergencies make a sound.
- Tables have large rows (48 px+), status chips, and one action per row.

### 7.2 Facility

**Incoming referrals (home)**

| Patient | Need | From | ETA | Waiting | Action |
|---------|------|------|-----|---------|--------|
| Geeta, 26 | Delivery + Blood | ASHA Sunita · Nagla | 25 min | 🔴 4 min | **Accept** · Decline |

- Accept/Decline is **one click + a confirmation**. Declining asks for a reason from preset options: No doctor, No bed, No equipment, Other.
- A countdown shows how long the request has been waiting. After the time limit, the case moves to the next facility and the row greys out.

**Capability panel.** This must be quick to update, because matching is only as good as this data.
- Toggle switches: Doctor on duty · Delivery · C-section/OT · Blood available · Oxygen · Newborn care · ICU · Beds free (number stepper).
- "Last updated" is shown prominently. Remind staff if it hasn't been updated for more than 6 hours.

**In transit.** Cards for patients on the way, showing the current leg, driver name and ETA, plus the **Handoff packet** (summary, vitals, ASHA notes) so staff can prepare. Buttons: "Patient arrived" → "Seen by doctor" → "Close case".

### 7.3 Doctor

- **Teleconsult queue:** patient, ASHA, reason, waiting time, "Start call".
- **Patient view** during a call: summary card on the left, and a care-plan form on the right built from pick-lists (medicines, next visit, refer yes/no). Free text is optional.
- "Refer" opens the same capability-matching list the ASHA sees.

### 7.4 District Admin

- **Top row:** 4 numbers only — Open emergencies · Referrals waiting · Cases closed today · Facilities not updated.
- **Map** of open cases, coloured by status.
- **"Stuck cases"** list (e.g. no facility accepted, driver not moving) with an **Escalate** button.
- Simple weekly charts: response time and referral closure rate.

---

## 8. SMS and IVR Design

SMS/IVR is part of the UI, not a backup afterthought.

**SOS SMS (auto-sent by the app when offline):**
```
AM SOS 7F3K | P:Kamla 26F | T:LABOUR | LOC:27.60,77.43 | ASHA:Sunita
```

**Status SMS to the family (plain language, in their language):**
```
AapatMitra: CHC Barsana accepted. Driver Ramesh (98xxxxxx21) coming in 15 min.
```

**Volunteer ride SMS:**
```
AapatMitra RIDE: Labour case, Nagla near temple, 3km. Reply 1=Accept 2=Can't go
```

Rules:
- Messages stay under 160 characters where possible, and use regional-language SMS when the user's language isn't English.
- Every SMS carries a **short case code** (e.g. `7F3K`) that also appears in the app, so staff can match them.
- **IVR:** a maximum of 4 options per menu, and the options come in the same order as the app tiles.

---

## 9. Offline and Connectivity UX

| State | What the user sees | Where |
|-------|--------------------|-------|
| Online, all synced | Green dot in the top bar | Top bar |
| Offline | Grey dot + banner: "No network. Your work is saved." | Top bar + banner |
| Items waiting | Banner: "3 items will send when network returns" (tap to see the list) | Banner |
| Sent by SMS | Chip on the item: "Sent by SMS" | On the case/task |
| Sync failed | Amber chip: "Could not send – Try again" + button | On the item |
| Data may be old | "Updated 20 min ago" under live info (ETA, capability) | Below the data |

- **Emergency items sync first** (matches the backend's priority queue).
- Never show a blank screen or spinner with no text. Use skeleton cards, and after 5 seconds show a text message ("Slow network – still trying").
- Keep images small. Prefer text-first screens. Assets should be vector.

---

## 10. Accessibility

- WCAG 2.1 AA: 4.5:1 contrast for text, 3:1 for large text and icons.
- All controls have labels for TalkBack/screen readers, and icons have content descriptions.
- Supports the system font size up to 200% without breaking layouts.
- Never uses colour alone (every status has a word and an icon).
- Hold-to-SOS has an accessibility alternative: double-tap, then confirm.
- Haptic feedback on SOS, accept and completion actions.
- Readable outdoors: high contrast, no light-grey text for important info.
- Web: keyboard navigation, visible focus, a "skip to content" link, and correct heading order.

---

## 11. What We Adopt and Avoid (from the UI study of Govt & Medical apps)

| Common finding from the study | AapatMitra decision |
|-------------------------------|---------------------|
| Same status colours everywhere (green / amber / red) | **Adopt** — the same set across app, web and SMS icons, always with words. |
| Clear top-to-bottom order: header → one hero → tiles → content | **Adopt** — one hero per role (SOS / Today's tasks / Ride switch), max 4 tiles. |
| Bottom tab bar in apps | **Adopt** — 3 tabs per role (4 max). |
| Mobile number + OTP login | **Adopt** — plus ASHA-assisted registration for families without phones. |
| Visible trust signals (emblem/ministry, brand, ratings) | **Adapt** — show real names (ASHA, driver, facility) + Call buttons; official marks only when authorised. |
| Blue/teal/green "health" palette with one saturated action colour | **Adopt** — Mitra Blue base; red reserved only for SOS. |
| Private-app card order: who → how good → how soon → how much → act | **Adapt** — facility card: name → capability ✔ → distance/ETA → Send request. |
| Govt apps: long forms and jargon | **Avoid** — one question per screen, no acronyms for patients. |
| Govt websites: dense pages, PDF lists | **Avoid** — the web console shows tasks, not documents. |
| Private apps: promo clutter, carousels | **Avoid** — no banners, no carousels, no ads. |
| GIGW-style accessibility strip on web | **Adopt** — slim utility strip on the Web Console. |

---

## 12. Simplicity Checklist (every screen must pass)

- [ ] The screen has **one purpose** and **one primary button**.
- [ ] A new user can finish the task **without instructions**.
- [ ] **No typing required** (typing only optional, or number pad).
- [ ] **Max 4 tiles / 6 choices** visible at once.
- [ ] Every icon has a **word**, and every status has a **word + colour + icon**.
- [ ] Body text **≥ 16 sp**, touch targets **≥ 56 dp**.
- [ ] Works **offline** and shows Saved / Sent / Sent by SMS.
- [ ] No acronyms on patient, family and volunteer screens.
- [ ] Translated and **checked by a native speaker**.
- [ ] Tested in **grayscale**, **at 200% font size**, and **in sunlight**.
- [ ] The SOS path takes **≤ 3 taps** (hold + 2 optional choices).
- [ ] After any action, the screen says **what happens next**.

---

## 13. Component Library (build once, reuse everywhere)

| Component | Android (Compose) | Web (React + Tailwind) | Notes |
|-----------|-------------------|------------------------|-------|
| `SosButton` | ✔ | — | Hold-to-send ring, haptics, SMS fallback |
| `PrimaryButton` / `SecondaryButton` | ✔ | ✔ | 56–64 dp, full width on mobile |
| `StatusChip` | ✔ | ✔ | Done / Waiting / Problem / Offline |
| `CaseStepper` | ✔ | ✔ | Vertical lifecycle stepper (shared wording) |
| `PersonCallCard` | ✔ | ✔ | Name + role + Call button |
| `FacilityCard` | ✔ | ✔ | Capability ✔ chips + distance + action |
| `PictureChoiceGrid` | ✔ | — | 2-column picture tiles for choices |
| `NumberPadField` | ✔ | — | Vitals input with normal-range feedback |
| `TaskRow` | ✔ | ✔ | Icon + title + due + action |
| `ConnectivityBanner` | ✔ | ✔ | Offline / pending sync |
| `EmptyState` | ✔ | ✔ | Simple illustration + one sentence + one button |
| `ConfirmDialog` | ✔ | ✔ | For Cancel SOS, Decline, Close case |

### 13.1 Design tokens

**Android (`Color.kt`)**
```kotlin
val MitraBlue      = Color(0xFF1E4FA3)
val MitraBlueDark  = Color(0xFF163B7A)
val MitraBlueTint  = Color(0xFFEAF1FB)
val SosRed         = Color(0xFFD32F2F)
val SuccessGreen   = Color(0xFF1E8E3E)
val WarningAmber   = Color(0xFFF9A825)
val AccentSaffron  = Color(0xFFF7931E)
val OfflineGrey    = Color(0xFF6B7280)
val Surface        = Color(0xFFF4F6FA)
val TextPrimary    = Color(0xFF1F2937)
val TextMuted      = Color(0xFF5B6475)
```

**Web (`tailwind.config.js`)**
```js
theme: {
  extend: {
    colors: {
      primary:   { DEFAULT: '#1E4FA3', dark: '#163B7A', tint: '#EAF1FB' },
      emergency: '#D32F2F',
      success:   '#1E8E3E',
      warning:   '#F9A825',
      accent:    '#F7931E',
      offline:   '#6B7280',
      surface:   '#F4F6FA',
      ink:       { DEFAULT: '#1F2937', muted: '#5B6475' },
    },
    fontFamily: { sans: ['"Noto Sans"', '"Noto Sans Devanagari"', 'system-ui', 'sans-serif'] },
    minHeight:  { touch: '56px' },
  },
}
```

---

## 14. Case Lifecycle — One Vocabulary Everywhere

Backend states map to **one set of words** shown in the app, web, SMS and IVR:

| Backend state | ASHA / Facility wording | Patient / Family wording | Colour |
|---------------|-------------------------|--------------------------|--------|
| `CREATED` | SOS / Referral created | Request received | Amber |
| `FACILITY_MATCHED` | Facility matched | Hospital found | Amber |
| `FACILITY_ACCEPTED` | Facility accepted | Hospital said YES | Green |
| `TRANSPORT_ASSIGNED` | Transport assigned | Driver is coming | Amber |
| `IN_TRANSIT` | In transit (leg X of Y) | On the way to hospital | Amber |
| `ARRIVED_SEEN` | Arrived & seen | Reached hospital | Green |
| `CLOSED` | Closed | Treatment done | Green |
| `FOLLOW_UP` | Follow-up task → ASHA | Next visit on … | Blue |
| *Declined → next facility* | Declined – trying next | Finding another hospital | Amber (never red for patients) |

> Patients never see "Declined" in red. They see "Finding another hospital" so they stay calm while the system retries.

---

## 15. Demo Priorities (for SIH judging)

Build and polish these flows first. They tell the whole story in under 3 minutes:

1. **Patient SOS offline** → Sent by SMS → Live case card.
2. **Volunteer** receives the ride → Accept → "Picked up" → "Handed over".
3. **Facility console** → Accept referral → sees Handoff packet → Close case.
4. **ASHA** → Screening → Referral with capability matching → Follow-up task appears.
5. **Admin** → 4 numbers + map.

Everything else (leaderboards, reports, detailed charts) can stay basic.
