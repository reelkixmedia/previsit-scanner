"""Outputs: the staff call list, patient reminders, and batched requests to prescribing offices.
All email leaves through the practice's OWN mailbox. Dry-run is the default.

Patient emails name the lab but never the medication (minimum necessary). Office requests do name
patient and drug: they go provider to provider for treatment, one batch per office."""
import csv
import hashlib
import io
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

from . import config, crypto
from .scanner import GROUP_LABELS, GROUPS, fmt
from .worklist import ACTIVE, STATUS_LABELS

DEFAULT_SUBJECT = "Before your visit on {appt_date}"
DEFAULT_BODY = (
    "Hi {first_name},\n\n"
    "You have an appointment with {provider} on {appt_date}. To keep your visit on time, please:\n\n"
    "{todo_list}\n\n"
    "If you have questions, reply to this email or call our office.\n\n"
    "{practice_name}"
)

CALL_LIST_HEADERS = ["Appointment", "Patient", "Patient ID", "Date of birth", "Phone", "Email", "Provider",
                     "What's missing", "What to do", "Prescriber", "Prescriber contact", "Status", "Group", "Done"]


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _cell(value) -> str:
    """Stops a value from an export being run as a formula when the file opens in Excel."""
    text = str(value or "")
    if text[:1] in ("=", "@", "\t") or (text[:1] in ("+", "-") and not text[1:2].isdigit()):
        return "'" + text
    return text


def call_list_rows(results: list[dict]) -> list[list[str]]:
    rows = []
    for r in results:
        issues = [i for i in r.get("issues", []) if i.get("work", "open") in ACTIVE]
        if r.get("issues") and not issues:
            continue  # everything for this patient is finished
        rx = [i for i in issues if i["type"] == "rx"]
        prescribers = sorted({i["prescriber"] for i in rx if i.get("prescriber")})
        contacts = sorted({i.get("prescriber_contact") or i.get("prescriber_email") or "" for i in rx} - {""})
        actions = list(dict.fromkeys(i["action"] for i in issues))
        status = "; ".join(sorted({STATUS_LABELS[i.get("work", "open")] for i in issues}))
        values = [fmt(date.fromisoformat(r["appt_date"])), r["patient_name"], r["patient_id"], r.get("dob", ""),
                  r["phone"], r["email"], r["provider"], "; ".join(i["reason"] for i in issues),
                  "; ".join(actions), ", ".join(prescribers), ", ".join(contacts), status,
                  GROUP_LABELS[r["group"]], ""]
        rows.append([_cell(v) for v in values])
    return rows


def call_list_xlsx(payload: dict, practice_name: str) -> bytes:
    """One sheet per group, so expired and not-expired are never mixed."""
    wb = Workbook()
    wb.remove(wb.active)
    everyone = payload["results"] + payload["unchecked"] + payload["clear"]
    for group in GROUPS:
        ws = wb.create_sheet(GROUP_LABELS[group].replace("'", "")[:31])
        ws.append(CALL_LIST_HEADERS)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in call_list_rows([r for r in everyone if r["group"] == group]):
            ws.append(row)
        for i, w in enumerate([14, 22, 12, 12, 14, 26, 18, 60, 40, 20, 18, 16, 22, 8], start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A2"
        ws.oddFooter.center.text = f"{practice_name} - contains patient information - do not leave unattended"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def call_list_csv(payload: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CALL_LIST_HEADERS)
    w.writerows(call_list_rows(payload["results"] + payload["unchecked"]))
    return buf.getvalue().encode("utf-8-sig")


def first_name(full_name: str) -> str:
    """Handles both "Jane Doe" and "Doe, Jane"."""
    if "," in full_name:
        full_name = full_name.split(",", 1)[1]
    parts = full_name.split()
    return parts[0] if parts else "there"


def patient_todos(issues: list[dict]) -> list[str]:
    """Only what the patient can still do: a renewal already requested isn't their job any more."""
    todos = [f"Complete your {i['item']} lab work" for i in issues
             if i["type"] == "lab" and i.get("work", "open") in ACTIVE]
    if any(i["type"] == "rx" and i.get("work", "open") == "open" for i in issues):
        todos.append("Contact our office about a prescription renewal")
    return list(dict.fromkeys(todos))


def call_script(practice_name: str, r: dict) -> str:
    return (f"Hi, this is [your name] from {practice_name}, calling for {first_name(r['patient_name'])}. "
            f"You have a visit on {fmt(date.fromisoformat(r['appt_date']))} and there are a couple of things "
            f"to take care of first so it runs on time.")


def build_draft(practice, r: dict, todos: list[str]) -> dict:
    fields = _Safe(first_name=first_name(r["patient_name"]), patient_name=r["patient_name"],
                   provider=r["provider"] or "your provider", appt_date=fmt(date.fromisoformat(r["appt_date"])),
                   practice_name=practice.name, todo_list="\n".join(f"  - {t}" for t in todos))
    return {"to": r["email"], "subject": practice.email_subject.format_map(fields),
            "body": practice.email_body.format_map(fields), "patient_hash": r["patient_hash"],
            "visit_date": r["appt_date"]}


def reminder_stages(practice) -> list[int]:
    return sorted({int(x) for x in practice.reminder_days.split(",") if x.strip().isdigit()}, reverse=True)


def stage_for(stages: list[int], days_to_visit: int) -> int | None:
    """Smallest stage that has been reached: with 14,7,3 and a visit 5 days away, that's the 7-day reminder."""
    reached = [s for s in stages if s >= days_to_visit]
    return min(reached) if reached else None


def due_reminders(practice, results: list[dict], logged: set, today: date) -> tuple[list[dict], dict]:
    """Returns (drafts due now, why others are held back)."""
    stages = reminder_stages(practice)
    due, held = [], {"no_email": 0, "opted_out": 0, "reached_by_phone": 0, "already_sent": 0, "not_yet": 0,
                     "nothing_for_patient": 0, "call_instead": 0}
    for r in results:
        days = (date.fromisoformat(r["appt_date"]) - today).days
        stage = stage_for(stages, days)
        todos = patient_todos(r["issues"])
        if days < 0 or not todos:
            held["nothing_for_patient"] += 1
        elif days == 0:
            held["call_instead"] += 1  # an email won't help a visit that's today
        elif not r.get("email"):
            held["no_email"] += 1
        elif r.get("do_not_email") or not r.get("email_ok", True):
            held["opted_out"] += 1
        elif r.get("reached_recently"):
            held["reached_by_phone"] += 1
        elif stage is None:
            held["not_yet"] += 1
        elif (r["patient_hash"], r["appt_date"], stage) in logged:
            held["already_sent"] += 1
        else:
            due.append({**build_draft(practice, r, todos), "stage": stage})
    return due, held


def office_groups(results: list[dict]) -> list[dict]:
    """One batch per office: open renewals by prescriber, open lab orders by the visit's provider."""
    groups = {}

    def group(name: str) -> dict:
        name = name or "Prescriber not listed"
        key = name.strip().lower()
        if key not in groups:
            groups[key] = {"gid": hashlib.sha256(key.encode()).hexdigest()[:12], "name": name, "email": "",
                           "contact": "", "rx": [], "labs": []}
        return groups[key]

    for r in results:
        person = {"patient": r["patient_name"], "dob": r.get("dob", ""), "visit": r["appt_date"],
                  "provider": r["provider"]}
        for i in r["issues"]:
            if i.get("work", "open") != "open":
                continue
            if i["type"] == "rx":
                g = group(i.get("prescriber", ""))
                g["email"] = g["email"] or i.get("prescriber_email", "")
                g["contact"] = g["contact"] or i.get("prescriber_contact", "")
                g["rx"].append({**person, "drug": i["item"], "last_prescribed": i["last_prescribed"],
                                "controlled": i["controlled"], "work_hash": i["work_hash"]})
            else:
                g = group(r["provider"])
                g["labs"].append({**person, "lab": i["item"], "reason": i["reason"], "work_hash": i["work_hash"]})
    return sorted(groups.values(), key=lambda g: g["name"].lower())


def office_draft(practice, g: dict) -> dict:
    lines = [f"From {practice.name}. These patients have visits coming up. Please review each request.", ""]
    if g["rx"]:
        lines.append("PRESCRIPTION RENEWALS")
        for x in g["rx"]:
            flag = " [CONTROLLED: please send through e-prescribing]" if x["controlled"] else ""
            dob = f", DOB {x['dob']}" if x["dob"] else ""
            lines.append(f"  - {x['patient']}{dob}: {x['drug']}, last prescribed {x['last_prescribed']}, "
                         f"visit {x['visit']}{flag}")
        lines.append("")
    if g["labs"]:
        lines.append("LAB ORDERS TO REVIEW")
        for x in g["labs"]:
            dob = f", DOB {x['dob']}" if x["dob"] else ""
            lines.append(f"  - {x['patient']}{dob}: {x['lab']}, visit {x['visit']}. {x['reason']}")
        lines.append("")
    lines += ["Reply to this email or call our office with any questions.", "", practice.name]
    n = len(g["rx"]) + len(g["labs"])
    return {"to": g["email"], "subject": f"{practice.name}: {n} request{'s' if n != 1 else ''} before upcoming visits",
            "body": "\n".join(lines)}


def send_drafts(practice, drafts: list[dict]) -> list[tuple[dict, str, str]]:
    """Returns [(draft, status, note)]. Status: dry_run | sent | skipped | failed."""
    out = []
    if config.DRY_RUN:
        return [(d, "dry_run", "Test mode: not sent") for d in drafts]
    if not (practice.smtp_host and practice.smtp_from):
        return [(d, "failed", "No practice mailbox set up in Settings") for d in drafts]
    password = crypto.decrypt_text(practice, practice.smtp_password_enc) if practice.smtp_password_enc else ""
    to_send = []
    for d in drafts:
        if config.EMAIL_ALLOWLIST and d["to"].lower() not in config.EMAIL_ALLOWLIST:
            out.append((d, "skipped", "Not on the test allowlist"))
        else:
            to_send.append(d)
    if not to_send:
        return out
    try:
        with smtplib.SMTP(practice.smtp_host, practice.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())  # refuse to send in the clear
            if practice.smtp_user:
                smtp.login(practice.smtp_user, password)
            for d in to_send:
                msg = EmailMessage()
                msg["From"], msg["To"], msg["Subject"] = practice.smtp_from, d["to"], d["subject"]
                msg.set_content(d["body"])
                try:
                    smtp.send_message(msg)
                    out.append((d, "sent", ""))
                except smtplib.SMTPException as e:
                    out.append((d, "failed", type(e).__name__))
    except (OSError, smtplib.SMTPException) as e:
        out.extend((d, "failed", f"Mail server error: {type(e).__name__}") for d in to_send)
    return out
