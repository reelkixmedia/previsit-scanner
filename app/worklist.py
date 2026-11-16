"""The staff worklist. Each issue becomes a WorkItem that survives from scan to scan:
finished work stays finished, and issues the new data shows as fixed close themselves."""
from datetime import timedelta

from sqlalchemy import func, select

from . import crypto
from .models import PatientState, WorkItem, utcnow

ACTIVE = ("open", "waiting")
STATUS_LABELS = {"open": "To do", "waiting": "Waiting on reply", "done": "Done", "not_needed": "Not needed",
                 "cleared": "Fixed in new data"}
OUTCOMES = {"reached": "Reached patient", "left_message": "Left message", "no_answer": "No answer",
            "wrong_number": "Wrong number"}
RECHECK_DAYS = 7  # marked done but the export still shows it after this long: put it back on the list


def patient_hash(practice, patient_id: str) -> str:
    return crypto.keyed_hash(practice, "p:" + patient_id)


def item_hash(practice, patient_id: str, issue_key: str) -> str:
    return crypto.keyed_hash(practice, f"i:{patient_id}:{issue_key}")


def sync(db, practice, payload: dict) -> dict:
    """Brings the worklist in line with a new scan. Returns counts only."""
    now = utcnow()
    items = {w.item_hash: w for w in db.scalars(select(WorkItem).where(WorkItem.practice_id == practice.id))}
    counts = {"new": 0, "reopened": 0, "cleared": 0}
    seen = set()
    for r in payload["results"]:
        ph = patient_hash(practice, r["patient_id"])
        for i in r["issues"]:
            ih = item_hash(practice, r["patient_id"], i["key"])
            seen.add(ih)
            w = items.get(ih)
            if not w:
                db.add(WorkItem(practice_id=practice.id, item_hash=ih, patient_hash=ph, visit_date=r["appt_date"],
                                kind=i["type"]))
                counts["new"] += 1
                continue
            new_visit = w.visit_date != r["appt_date"]
            never_fixed = w.status == "done" and w.updated_at < now - timedelta(days=RECHECK_DAYS)
            if w.status == "cleared" or (w.status not in ACTIVE and new_visit) or never_fixed:
                w.closed_reason = "came_back" if never_fixed or w.status == "cleared" else ""
                w.status, w.updated_at, w.updated_by = "open", now, None
                counts["reopened"] += 1
            w.visit_date = r["appt_date"]

    checked = {patient_hash(practice, r["patient_id"]) for r in payload["results"] + payload["clear"]}
    can_not_tell = {patient_hash(practice, r["patient_id"]) for r in payload["unchecked"]}
    for ih, w in items.items():
        if w.status in ACTIVE and ih not in seen and w.patient_hash not in can_not_tell:
            w.closed_reason = "fixed_in_new_data" if w.patient_hash in checked else "visit_gone"
            w.status, w.updated_at, w.updated_by = "cleared", now, None
            counts["cleared"] += 1
    db.flush()
    return counts


def annotate(db, practice, payload: dict, today) -> dict:
    """Adds live worklist status and contact history to decrypted scan results."""
    items = {w.item_hash: w for w in db.scalars(select(WorkItem).where(WorkItem.practice_id == practice.id))}
    states = {s.patient_hash: s for s in db.scalars(select(PatientState).where(PatientState.practice_id == practice.id))}
    for r in payload["results"] + payload["clear"] + payload["unchecked"]:
        r["patient_hash"] = patient_hash(practice, r["patient_id"])
        state = states.get(r["patient_hash"])
        r["do_not_email"] = bool(state and state.do_not_email)
        r["contact_log"] = crypto.decrypt_json(practice, state.log_enc) if state and state.log_enc else []
        recent = (today - timedelta(days=3)).isoformat()
        r["reached_recently"] = any(c["outcome"] == "reached" and c["date"] >= recent for c in r["contact_log"])
    for r in payload["results"]:
        for i in r["issues"]:
            ih = item_hash(practice, r["patient_id"], i["key"])
            w = items.get(ih)
            i["work_hash"] = ih
            i["work"] = w.status if w else "open"
            i["came_back"] = bool(w and w.closed_reason == "came_back" and w.status == "open")
        r["open_count"] = sum(i["work"] in ACTIVE for i in r["issues"])
        r["todo_count"] = sum(i["work"] == "open" for i in r["issues"])
    return payload


def set_status(db, practice, item_hash_: str, status: str, user_id) -> WorkItem | None:
    w = db.scalar(select(WorkItem).where(WorkItem.practice_id == practice.id, WorkItem.item_hash == item_hash_))
    if not w or status not in ("open", "waiting", "done", "not_needed"):
        return None
    w.status, w.updated_by, w.updated_at, w.closed_reason = status, user_id, utcnow(), ""
    return w


def _state(db, practice, patient_hash_: str) -> PatientState:
    s = db.scalar(select(PatientState).where(PatientState.practice_id == practice.id,
                                             PatientState.patient_hash == patient_hash_))
    if not s:
        s = PatientState(practice_id=practice.id, patient_hash=patient_hash_)
        db.add(s)
    s.updated_at = utcnow()
    return s


def log_contact(db, practice, patient_hash_: str, outcome: str, note: str, who: str, today):
    s = _state(db, practice, patient_hash_)
    log = crypto.decrypt_json(practice, s.log_enc) if s.log_enc else []
    log.append({"date": today.isoformat(), "outcome": outcome, "note": note[:300], "who": who})
    s.log_enc = crypto.encrypt_json(practice, log[-20:])
    db.flush()


def set_do_not_email(db, practice, patient_hash_: str, value: bool):
    _state(db, practice, patient_hash_).do_not_email = value
    db.flush()


def totals(db, practice_id: int) -> dict:
    rows = db.execute(select(WorkItem.status, func.count()).where(WorkItem.practice_id == practice_id)
                      .group_by(WorkItem.status)).all()
    return {k: 0 for k in STATUS_LABELS} | dict(rows)
