"""Inbound SMS grammar (TRD §7.1, API-Guide §10.1, §10.3).

    SOS <patientShortCode|-> <category> <lat>,<lng> [#<tag>]
    category := P | N | I | B | U | O  (full words, case-insensitive, Hindi transliterations, Devanagari)

Tolerant (extra spaces, lower case, missing coordinates, Devanagari keywords) and bounded (≤ 480 chars).
The parser never evaluates or templates its input. The UI-UX §8 format with a patient name is NOT
accepted as a source of identity (API decision D1) — only the tokens below are read.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.core.ids import normalise_crockford

MAX_LEN = 480
CATEGORY_WORDS = {
    "pregnancy": ("P", "PREGNANCY", "PREGNANT", "LABOUR", "LABOR", "DELIVERY", "PRASAV", "PRASAW", "प्रसव", "गर्भ"),
    "newborn": ("N", "NEWBORN", "BABY", "BACHCHA", "BACCHA", "NAVJAT", "बच्चा", "नवजात", "शिशु"),
    "injury": ("I", "INJURY", "ACCIDENT", "CHOT", "चोट", "दुर्घटना"),
    "breathing": ("B", "BREATHING", "BREATH", "SAANS", "SANS", "साँस", "सांस"),
    "unconscious": ("U", "UNCONSCIOUS", "FITS", "BEHOSH", "बेहोश", "दौरा"),
    "other": ("O", "OTHER", "ANYA", "अन्य"),
}
_WORD_TO_CAT = {w: c for c, words in CATEGORY_WORDS.items() for w in words}
SOS_WORDS = ("SOS", "MADAD", "मदद", "HELP")
COORD = re.compile(r"(-?\d{1,2}(?:\.\d{1,7})?)\s*,\s*(-?\d{1,3}(?:\.\d{1,7})?)")
TAG = re.compile(r"#\s*([0-9A-Za-z]{8})\b")
CODE = re.compile(r"^[0-9A-Z]{6}$")


@dataclass
class Parsed:
    intent: str                     # sos | ride_reply | bed_update | status_query | stop | other
    status: str                     # parsed | partial | unparseable
    short_code: str | None = None
    category: str | None = None
    lat: float | None = None
    lng: float | None = None
    tag: str | None = None
    digit: int | None = None
    case_code: str | None = None
    beds: int | None = None
    facility_status: str | None = None
    text: str | None = None

    def as_json(self) -> dict[str, object]:
        """Codes and numbers only — safe to store in inbound_messages.parsed."""
        return {k: v for k, v in {"shortCode": self.short_code, "category": self.category, "lat": self.lat,
                                  "lng": self.lng, "tag": self.tag, "digit": self.digit, "caseCode": self.case_code,
                                  "beds": self.beds, "facilityStatus": self.facility_status}.items() if v is not None}


def _norm(body: str) -> str:
    body = unicodedata.normalize("NFC", body or "")[:MAX_LEN]
    return re.sub(r"\s+", " ", body).strip()


def parse(body: str | None) -> Parsed:
    text = _norm(body or "")
    if not text:
        return Parsed("other", "unparseable")
    upper = text.upper()
    tokens = upper.replace("|", " ").split(" ")
    if tokens and tokens[0] == "AM" and len(tokens) > 1:  # tolerate an "AM " prefix
        tokens = tokens[1:]
        upper = " ".join(tokens)
    first = tokens[0]

    if first in SOS_WORDS or first.startswith("SOS"):
        return _parse_sos(upper, tokens)
    m = re.fullmatch(r"([12])(?:\s+([0-9A-Z]{6}))?", upper)
    if m:
        return Parsed("ride_reply", "parsed", digit=int(m.group(1)),
                      case_code=normalise_crockford(m.group(2)) if m.group(2) else None)
    if first == "STATUS":
        code = normalise_crockford(tokens[1]) if len(tokens) > 1 and CODE.match(normalise_crockford(tokens[1])) else None
        return Parsed("status_query", "parsed" if code else "partial", case_code=code)
    if first in ("BED", "BEDS", "B") and len(tokens) > 1 and tokens[1].isdigit():
        return Parsed("bed_update", "parsed", beds=min(int(tokens[1]), 5000))
    if upper in ("FULL", "OPEN", "CLOSED"):
        return Parsed("bed_update", "parsed", facility_status=upper.lower())
    if upper in ("STOP", "UNSUBSCRIBE"):
        return Parsed("stop", "parsed")
    return Parsed("other", "partial", text=text[:60])


def _parse_sos(upper: str, tokens: list[str]) -> Parsed:
    p = Parsed("sos", "parsed", category=None)
    rest = upper.split(" ", 1)[1] if " " in upper else ""
    tag = TAG.search(rest)
    if tag:
        p.tag = normalise_crockford(tag.group(1))
        rest = TAG.sub(" ", rest)
    coord = COORD.search(rest)
    if coord:
        lat, lng = float(coord.group(1)), float(coord.group(2))
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            p.lat, p.lng = lat, lng
        rest = COORD.sub(" ", rest)
    for tok in rest.split():
        if tok == "-":
            continue
        cat = _WORD_TO_CAT.get(tok)
        if cat and p.category is None:
            p.category = cat
            continue
        norm = normalise_crockford(tok)
        if p.short_code is None and CODE.match(norm):
            p.short_code = norm
    if p.category is None:
        p.category = "other"
        if rest.strip():
            p.status = "partial"
    return p
