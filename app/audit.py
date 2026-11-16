"""Hash-chained audit log. Each event's hash covers the previous one, so edits or deletions
break the chain. Events are also appended to instance/audit.jsonl for a Wazuh agent.
Rule: never put patient names or details in `detail` -- counts and record ids only."""
import hashlib
import json

from sqlalchemy import select

from . import config
from .models import AuditEvent, utcnow

GENESIS = "0" * 64


def _payload(practice_id, user_id, ts, ip, action, detail: dict) -> dict:
    return {"practice_id": practice_id, "user_id": user_id, "ts": ts.isoformat(),
            "ip": ip, "action": action, "detail": detail}


def _hash(prev: str, payload: dict) -> str:
    return hashlib.sha256((prev + json.dumps(payload, sort_keys=True)).encode()).hexdigest()


def _chain_filter(practice_id):
    return AuditEvent.practice_id.is_(None) if practice_id is None else AuditEvent.practice_id == practice_id


def log(db, action, *, practice_id=None, user_id=None, ip="", **detail):
    prev = db.scalar(select(AuditEvent.hash).where(_chain_filter(practice_id))
                     .order_by(AuditEvent.id.desc()).limit(1)) or GENESIS
    ts = utcnow()
    payload = _payload(practice_id, user_id, ts, ip, action, detail)
    h = _hash(prev, payload)
    ev = AuditEvent(practice_id=practice_id, user_id=user_id, ts=ts, ip=ip, action=action,
                    detail=json.dumps(detail, sort_keys=True), prev_hash=prev, hash=h)
    db.add(ev)
    db.flush()
    with open(config.AUDIT_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps({"app": "previsit", "event_id": ev.id, **payload, "hash": h}) + "\n")
    return ev


def verify_chain(db, practice_id):
    """Returns (ok, events_checked, first_bad_event_id)."""
    prev = GENESIS
    n = 0
    for ev in db.scalars(select(AuditEvent).where(_chain_filter(practice_id)).order_by(AuditEvent.id)):
        n += 1
        payload = _payload(ev.practice_id, ev.user_id, ev.ts, ev.ip, ev.action, json.loads(ev.detail))
        if ev.prev_hash != prev or _hash(prev, payload) != ev.hash:
            return False, n, ev.id
        prev = ev.hash
    return True, n, None
