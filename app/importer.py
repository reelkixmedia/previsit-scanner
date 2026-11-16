"""CSV intake: read any practice export, match its columns to our fields, normalise rows."""
import csv
import hashlib
import io
import re
from datetime import date, datetime

KINDS = ["appointments", "medications", "labs", "patients"]
KIND_LABELS = {"appointments": "Appointments", "medications": "Medications", "labs": "Lab results",
               "patients": "Patient contact details"}

# kind -> [(field, label, required)]
FIELDS = {
    "appointments": [
        ("patient_id", "Patient ID / MRN", True),
        ("appt_date", "Appointment date", True),
        ("patient_name", "Patient name", False),
        ("provider", "Provider", False),
        ("email", "Patient email", False),
        ("phone", "Patient phone", False),
        ("visit_type", "Visit type", False),
        ("dob", "Date of birth", False),
        ("appt_status", "Appointment status", False),
        ("email_consent", "OK to email?", False),
    ],
    "medications": [
        ("patient_id", "Patient ID / MRN", True),
        ("drug_name", "Medication", True),
        ("last_prescribed", "Date last prescribed", True),
        ("refills_remaining", "Refills remaining", False),
        ("schedule", "Controlled-substance schedule", False),
        ("status", "Status (active/discontinued)", False),
        ("prescriber", "Prescriber", False),
        ("prescriber_contact", "Prescriber phone/fax", False),
        ("prescriber_email", "Prescriber email", False),
        ("stop_date", "Stop / end date", False),
    ],
    "labs": [
        ("patient_id", "Patient ID / MRN", True),
        ("test_name", "Test name", True),
        ("result_date", "Result / collection date", True),
    ],
    # Many systems keep phone, email and consent in a separate demographics report, not on appointments.
    "patients": [
        ("patient_id", "Patient ID / MRN", True),
        ("patient_name", "Patient name", False),
        ("dob", "Date of birth", False),
        ("email", "Patient email", False),
        ("phone", "Patient phone", False),
        ("email_consent", "OK to email?", False),
    ],
}
DATE_FIELDS = {"appt_date", "last_prescribed", "result_date"}

SYNONYMS = {
    "patient_id": ["patient id", "patientid", "mrn", "medical record number", "medical record", "chart",
                   "chart number", "chart #", "account", "account number", "pt id", "person id",
                   "patient number", "patient #", "pat id", "patient practice guid", "patient guid"],
    "patient_name": ["patient name", "patient", "name", "full name", "pt name", "patient full name"],
    "appt_date": ["appointment date", "appt date", "date", "appointment", "visit date", "scheduled date",
                  "appt", "appointment datetime", "start time", "contact date", "start date time", "appt time"],
    "provider": ["provider", "doctor", "physician", "rendering provider", "scheduled provider", "resource",
                 "provider name", "visit prov id prov name"],
    "email": ["email", "email address", "e mail", "patient email"],
    "phone": ["phone", "phone number", "mobile", "cell", "cell phone", "home phone", "telephone"],
    "visit_type": ["visit type", "appointment type", "appt type", "reason", "type"],
    "drug_name": ["drug", "drug name", "medication", "medication name", "med", "med name", "rx",
                  "description", "display name", "medication display name", "medication id medication name",
                  "generic name"],
    "last_prescribed": ["last prescribed", "last prescribed date", "prescribed date", "date prescribed",
                        "rx date", "written date", "date written", "last rx date", "start date", "ordering date",
                        "date of service", "effective date"],
    # Plain "Refills" usually means refills authorized, not remaining, so it is deliberately not matched.
    "refills_remaining": ["refills remaining", "refills left", "remaining refills"],
    "schedule": ["schedule", "dea schedule", "controlled schedule", "csa schedule", "controlled",
                 "controlled substance schedule"],
    "status": ["status", "med status", "medication status", "active", "order status c name"],
    "prescriber": ["prescriber", "prescribing provider", "prescribed by", "ordering provider",
                   "med presc prov id prov name", "authrzing prov id prov name", "ord prov id prov name"],
    "prescriber_contact": ["prescriber contact", "prescriber phone", "prescriber fax"],
    "prescriber_email": ["prescriber email", "prescriber e mail", "prescribing office email"],
    "dob": ["dob", "date of birth", "birth date", "birthdate", "patient dob"],
    "appt_status": ["appointment status", "appt status", "visit status", "status", "appt status c name"],
    "stop_date": ["stop date", "end date", "discontinued date", "discon time", "date stopped"],
    "email_consent": ["email consent", "ok to email", "email ok", "email opt in", "consent to email"],
    "test_name": ["test", "test name", "lab", "lab name", "lab test", "result name", "component",
                  "order name", "component id name", "compon lnc id lnc long name"],
    "result_date": ["result date", "collected", "collection date", "date collected", "lab date",
                    "resulted", "resulted date", "date", "comp obs inst tm"],
}

_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y", "%d-%b-%Y", "%b %d, %Y",
                 "%B %d, %Y", "%Y%m%d"]


def _norm(header: str) -> str:
    """"PatientPracticeGuid", "PAT_ID" and "Patient ID" all become plain lower-case words."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", header)
    return re.sub(r"[^a-z0-9#]+", " ", spaced.lower()).strip()


def decode_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _delimiter(text: str) -> str:
    """Comma, tab or pipe: whichever splits the first line most (exports use all three)."""
    first = text.split("\n", 1)[0]
    return max([",", "\t", "|"], key=first.count)


def _looks_like_data(cells: list[str]) -> bool:
    """A first row holding dates is data, not headers: the report was run with column headings switched off."""
    return any(parse_date(c) for c in cells if c.strip())


def read_csv(text: str) -> tuple[list[str], list[dict]]:
    reader = csv.reader(io.StringIO(text), delimiter=_delimiter(text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        return [], []
    if _looks_like_data(rows[0]):
        headers = [f"Column {i + 1}" for i in range(max(len(r) for r in rows))]
        body = rows
    else:
        headers, body = [h.strip() for h in rows[0]], rows[1:]
    records = [dict(zip(headers, (c.strip() for c in r))) for r in body]
    return headers, records


def header_signature(headers: list[str]) -> str:
    return hashlib.sha256("|".join(sorted(_norm(h) for h in headers)).encode()).hexdigest()


def guess_mapping(kind: str, headers: list[str]) -> dict:
    mapping, used = {}, set()
    for exact in (True, False):
        for field, _, _ in FIELDS[kind]:
            if field in mapping:
                continue
            syns = [_norm(s) for s in SYNONYMS.get(field, [])]
            for h in headers:
                if h in used:
                    continue
                nh = _norm(h)
                hit = nh in syns if exact else any(len(s) > 3 and s in nh for s in syns)
                if hit:
                    mapping[field] = h
                    used.add(h)
                    break
    return mapping


def missing_required(kind: str, mapping: dict) -> list[str]:
    return [label for field, label, req in FIELDS[kind] if req and not mapping.get(field)]


def parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    candidates = [value, value.split(" ")[0], value.split("T")[0]]
    for cand in candidates:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    return None


def normalize_schedule(value: str) -> str:
    v = re.sub(r"[^a-z0-9]", "", (value or "").lower()).replace("schedule", "").replace("sched", "")
    v = v.removeprefix("c")
    if v in ("ii", "2", "iin"):
        return "C-II"
    if v in ("iii", "iv", "v", "3", "4", "5", "iiin"):
        return "C-III-V"
    return "non-controlled"


def _int_or_none(value: str):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


INACTIVE = {"discontinued", "inactive", "stopped", "completed", "cancelled", "canceled", "expired-dc"}
GONE_VISITS = {"cancelled", "canceled", "no show", "no-show", "noshow", "rescheduled", "deleted", "void"}
NO_WORDS = {"n", "no", "false", "0", "opt out", "opted out", "declined"}
NULLS = {"?", "null", "none", "n/a", "na", "-"}  # Greenway Intergy writes "?" for empty


def normalize(kind: str, rows: list[dict], mapping: dict) -> tuple[list[dict], dict]:
    """Returns (clean records, stats). Stats say why rows were left out; they hold counts only."""
    out = []
    stats = {"missing_value": 0, "bad_date": 0, "cancelled_visit": 0, "discontinued": 0}
    required = {f for f, _, req in FIELDS[kind] if req}
    today = date.today()
    for row in rows:
        rec = {f: (row.get(h) or "").strip() for f, h in mapping.items() if h}
        rec = {f: "" if v.lower() in NULLS else v for f, v in rec.items()}
        if any(not rec.get(f) for f in required):
            stats["missing_value"] += 1
            continue
        dates = {f: parse_date(rec[f]) for f in DATE_FIELDS & rec.keys()}
        if None in dates.values():
            stats["bad_date"] += 1
            continue
        rec.update({f: d.isoformat() for f, d in dates.items()})
        if kind == "appointments":
            if rec.get("appt_status", "").lower() in GONE_VISITS:
                stats["cancelled_visit"] += 1
                continue
        if kind in ("appointments", "patients"):
            rec["email_ok"] = rec.pop("email_consent", "").lower() not in NO_WORDS
        if kind == "medications":
            stopped = parse_date(rec.pop("stop_date", ""))
            if rec.get("status", "").lower() in INACTIVE or (stopped and stopped <= today):
                stats["discontinued"] += 1
                continue
            rec["schedule"] = normalize_schedule(rec.get("schedule", ""))
            rec["refills_remaining"] = _int_or_none(rec.get("refills_remaining"))
        if "email" in rec:
            rec["email"] = rec["email"].lower()
        out.append(rec)
    return out, stats
