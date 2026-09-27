"""RFC 9457 problem+json errors with stable codes (API-Guide §2.8, Appendix A)."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError

# code -> (http status, retryable, title en, title hi)
CODES: dict[str, tuple[int, bool, str, str]] = {
    "VALIDATION_FAILED": (422, False, "Some fields are not valid", "कुछ जानकारी सही नहीं है"),
    "FIELD_NOT_WRITABLE": (422, False, "This field cannot be changed", "यह जानकारी बदली नहीं जा सकती"),
    "IDEMPOTENCY_MISMATCH": (422, False, "Request key reused with different data", "अनुरोध कुंजी दोबारा इस्तेमाल हुई"),
    "IDEMPOTENCY_IN_PROGRESS": (409, True, "Request is still being processed", "अनुरोध पर काम चल रहा है"),
    "UNAUTHENTICATED": (401, False, "Please log in", "कृपया लॉग इन करें"),
    "TOKEN_EXPIRED": (401, True, "Session expired", "सत्र समाप्त हो गया"),
    "REFRESH_INVALID": (401, False, "Please log in again", "कृपया फिर से लॉग इन करें"),
    "REFRESH_REUSED": (401, False, "You were signed out for safety", "सुरक्षा के लिए आपको साइन आउट किया गया"),
    "DEVICE_REVOKED": (401, False, "This device was removed", "यह डिवाइस हटा दिया गया"),
    "OTP_INVALID": (401, False, "Wrong code", "गलत कोड"),
    "OTP_EXPIRED": (410, False, "Code expired", "कोड की समय सीमा समाप्त"),
    "OTP_LOCKED": (429, True, "Too many attempts. Try in 15 minutes", "बहुत प्रयास। 15 मिनट बाद कोशिश करें"),
    "ACCOUNT_PENDING_APPROVAL": (403, False, "Waiting for approval", "मंज़ूरी का इंतज़ार"),
    "ACCOUNT_SUSPENDED": (403, False, "Account suspended", "खाता निलंबित"),
    "DEVICE_APPROVAL_REQUIRED": (403, False, "Ask your supervisor to approve this phone", "अपने सुपरवाइज़र से फ़ोन मंज़ूर कराएँ"),
    "FORBIDDEN_ROLE": (403, False, "Not allowed for your role", "आपकी भूमिका के लिए अनुमति नहीं"),
    "FORBIDDEN_SCOPE": (403, False, "Outside your area", "आपके क्षेत्र से बाहर"),
    "CONSENT_REQUIRED": (403, False, "Consent needed", "सहमति चाहिए"),
    "VOLUNTEER_NOT_VERIFIED": (403, False, "Your ASHA will verify you", "आपकी ASHA आपको सत्यापित करेंगी"),
    "NOT_FOUND": (404, False, "Not found", "नहीं मिला"),
    "DUPLICATE_ID": (409, False, "Record already exists", "रिकॉर्ड पहले से मौजूद है"),
    "ALREADY_SUPERSEDED": (409, False, "Entry already corrected", "प्रविष्टि पहले ही सुधारी गई"),
    "CASE_STATE_CONFLICT": (409, False, "This case has already moved on", "यह केस आगे बढ़ चुका है"),
    "LEG_ALREADY_TAKEN": (409, False, "Already taken — thank you", "पहले ही ले ली गई — धन्यवाद"),
    "VOLUNTEER_BUSY": (409, False, "You already have a ride", "आपके पास पहले से एक सवारी है"),
    "OFFER_EXPIRED": (410, False, "Case moved to next facility", "केस अगली सुविधा को चला गया"),
    "CURSOR_EXPIRED": (410, False, "Full refresh needed", "पूरा डेटा फिर से लेना होगा"),
    "VERSION_MISMATCH": (412, False, "Someone else changed this", "किसी और ने इसे बदला"),
    "UPLOAD_TOO_LARGE": (413, False, "File too large", "फ़ाइल बहुत बड़ी है"),
    "PAYLOAD_TOO_LARGE": (413, False, "Request too large", "अनुरोध बहुत बड़ा है"),
    "UNSUPPORTED_MEDIA_TYPE": (415, False, "File type not allowed", "यह फ़ाइल प्रकार मान्य नहीं"),
    "HANDOVER_CODE_INVALID": (422, False, "Wrong handover code", "गलत हैंडओवर कोड"),
    "HANDOVER_LOCKED": (423, False, "Handover locked — call your ASHA", "हैंडओवर बंद — ASHA को कॉल करें"),
    "OVERRIDE_REASON_REQUIRED": (422, False, "Reason needed for this choice", "इस विकल्प का कारण चाहिए"),
    "UPGRADE_REQUIRED": (426, False, "Please update the app", "कृपया ऐप अपडेट करें"),
    "RATE_LIMITED": (429, True, "Too many requests", "बहुत अधिक अनुरोध"),
    "INTERNAL": (500, True, "Something went wrong", "कुछ गलत हो गया"),
    "DEPENDENCY_UNAVAILABLE": (503, True, "Service busy, retrying", "सेवा व्यस्त है"),
}


class AppError(Exception):
    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        current: Any = None,
        fields: list[dict[str, Any]] | None = None,
        status: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.current = current
        self.fields = fields
        self.status = status or CODES[code][0]
        self.headers = headers or {}


def _lang(request: Request) -> str:
    al = request.headers.get("accept-language", "en").lower()
    return "hi" if al.startswith("hi") else "en"


def problem(request: Request, code: str, detail: str | None = None, *, status: int | None = None,
            current: Any = None, fields: Any = None, headers: dict[str, str] | None = None) -> JSONResponse:
    st, retryable, title_en, title_hi = CODES.get(code, CODES["INTERNAL"])
    body = {
        "type": f"https://api.aapatmitra.in/errors/{code.lower().replace('_', '-')}",
        "title": title_hi if _lang(request) == "hi" else title_en,
        "status": status or st,
        "code": code,
        "detail": detail,
        "requestId": getattr(request.state, "request_id", None),
        "fields": fields,
        "current": current,
        "retryable": retryable,
    }
    return JSONResponse(body, status_code=status or st, media_type="application/problem+json", headers=headers)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return problem(request, exc.code, exc.detail, status=exc.status, current=exc.current,
                   fields=exc.fields, headers=exc.headers)


async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
        item: dict[str, Any] = {"path": ".".join(loc), "code": err.get("type", "invalid")}
        ctx = err.get("ctx") or {}
        for k in ("ge", "le", "gt", "lt"):
            if k in ctx:
                item["min" if k in ("ge", "gt") else "max"] = ctx[k]
        if err.get("type") == "extra_forbidden":
            item["code"] = "not_allowed"
        fields.append(item)
    return problem(request, "VALIDATION_FAILED", "Request failed validation", fields=fields)


async def db_error_handler(request: Request, exc: DBAPIError) -> JSONResponse:
    # enforce_case_transition raises check_violation with HINT CASE_STATE_CONFLICT (database.md §7.2)
    text = str(getattr(exc, "orig", exc))
    from app.core.logging import log

    # operators need the cause; the SQL text itself is not logged (it may carry bound identifiers)
    log.error("db_error", error_type=type(getattr(exc, "orig", exc)).__name__, reason=text.splitlines()[0][:300])
    if "CASE_STATE_CONFLICT" in text or "illegal case transition" in text:
        return problem(request, "CASE_STATE_CONFLICT", "Transition not allowed from the current state")
    if isinstance(exc, IntegrityError):
        return problem(request, "VALIDATION_FAILED", "Data violates a database rule",
                       fields=[{"path": "", "code": _constraint_name(text)}])
    return problem(request, "INTERNAL", None)


def _constraint_name(text: str) -> str:
    marker = 'constraint "'
    if marker in text:
        return text.split(marker, 1)[1].split('"', 1)[0]
    return "integrity"
