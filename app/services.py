"""Practice-level operations shared by the web routes and the admin CLI."""
import json
from datetime import timedelta

from sqlalchemy import delete, select

from . import audit, config, crypto
from .auth import end_user_sessions, hash_password, new_token, sha
from .models import (ColumnMapping, Dataset, LabRule, OutboxMessage, PatientState, Practice, ReminderLog, RxRule,
                     ScanRun, User, WorkItem, utcnow)
from .outreach import DEFAULT_BODY, DEFAULT_SUBJECT
from .scanner import DEFAULT_LAB_RULES, DEFAULT_RX_DAYS, keywords


IAM_PLANS = {
    "managed": "Managed access: Previsit support adds users and changes roles for you",
    "self": "Self-managed: your own admins add users and change roles",
}


def create_practice(db, name: str, owner_name: str, owner_email: str, password: str, ip: str = "",
                    iam_plan: str = "self") -> User:
    practice = Practice(name=name, wrapped_key=crypto.new_wrapped_data_key(), iam_plan=iam_plan,
                        email_subject=DEFAULT_SUBJECT, email_body=DEFAULT_BODY)
    db.add(practice)
    db.flush()
    for cat, days in DEFAULT_RX_DAYS.items():
        db.add(RxRule(practice_id=practice.id, category=cat, max_days=days))
    for lab_name, drugs, labs, days in DEFAULT_LAB_RULES:
        db.add(LabRule(practice_id=practice.id, name=lab_name, drug_keywords=drugs, lab_keywords=labs,
                       max_age_days=days))
    owner = User(practice_id=practice.id, email=owner_email.lower(), name=owner_name, role="owner",
                 password_hash=hash_password(password))
    db.add(owner)
    db.flush()
    audit.log(db, "practice_created", practice_id=practice.id, user_id=owner.id, ip=ip, iam_plan=iam_plan)
    return owner


def create_operator(db, name: str, email: str, password: str) -> User:
    """Platform staff account (you). Lives in a hidden platform practice; MFA always required."""
    platform = db.scalar(select(Practice).where(Practice.is_platform.is_(True)))
    if not platform:
        platform = Practice(name="Previsit support", wrapped_key=crypto.new_wrapped_data_key(), is_platform=True,
                            require_mfa=True, email_subject="", email_body="")
        db.add(platform)
        db.flush()
    user = User(practice_id=platform.id, email=email.lower(), name=name, role="operator",
                password_hash=hash_password(password))
    db.add(user)
    db.flush()
    audit.log(db, "operator_created", practice_id=platform.id, user_id=user.id, ip="cli")
    return user


# ---- user management, shared by practice admins (self-managed) and Previsit support (managed) ----

def log_access_change(db, practice_id: int, actor, ip: str, action: str, **detail):
    """Logged in the practice's own audit trail; operator actions are also logged in the platform's."""
    if actor.role == "operator":
        detail["by"] = "previsit_support"
        audit.log(db, action, practice_id=actor.practice_id, user_id=actor.id, ip=ip,
                  target_practice_id=practice_id, **detail)
    audit.log(db, action, practice_id=practice_id, user_id=actor.id, ip=ip, **detail)


def invite_user(db, practice: Practice, actor, ip: str, name: str, email: str, role: str) -> tuple[User | None, str]:
    """Returns (user, token) or (None, error message)."""
    email = email.strip().lower()
    if "@" not in email or not name.strip():
        return None, "Enter a name and a valid email."
    if db.scalar(select(User).where(User.email == email)):
        return None, "That email already has an account."
    token = new_token()
    user = User(practice_id=practice.id, email=email, name=name.strip(), role=role,
                invite_token_hash=sha(token), invite_expires_at=utcnow() + timedelta(hours=config.INVITE_HOURS))
    db.add(user)
    db.flush()
    log_access_change(db, practice.id, actor, ip, "user_invited", target_user_id=user.id, role=role)
    return user, token


def update_user(db, user: User, actor, ip: str, role: str, active: bool):
    was = user.role
    user.role, user.active = role, active
    if not active or role != was:
        end_user_sessions(db, user.id)  # new permissions apply from their next sign-in
    log_access_change(db, user.practice_id, actor, ip, "user_updated", target_user_id=user.id, role_from=was,
                      role_to=role, active=active)


def reset_user_mfa(db, user: User, actor, ip: str):
    user.mfa_enabled, user.totp_secret_enc, user.totp_last_step = False, None, 0
    end_user_sessions(db, user.id)
    log_access_change(db, user.practice_id, actor, ip, "mfa_reset", target_user_id=user.id)


def rx_days(db, practice_id: int) -> dict:
    return {r.category: r.max_days for r in db.scalars(select(RxRule).where(RxRule.practice_id == practice_id))}


def lab_rules(db, practice_id: int) -> list[dict]:
    rules = db.scalars(select(LabRule).where(LabRule.practice_id == practice_id, LabRule.active.is_(True)))
    return [{"name": r.name, "drug_keywords": keywords(r.drug_keywords), "lab_keywords": keywords(r.lab_keywords),
             "max_age_days": r.max_age_days} for r in rules]


def saved_mapping(db, practice_id: int, kind: str, signature: str) -> dict | None:
    row = db.scalar(select(ColumnMapping).where(ColumnMapping.practice_id == practice_id,
                                                ColumnMapping.kind == kind,
                                                ColumnMapping.header_signature == signature))
    return json.loads(row.mapping_json) if row else None


def save_mapping(db, practice_id: int, kind: str, signature: str, mapping: dict):
    row = db.scalar(select(ColumnMapping).where(ColumnMapping.practice_id == practice_id,
                                                ColumnMapping.kind == kind,
                                                ColumnMapping.header_signature == signature))
    if row:
        row.mapping_json = json.dumps(mapping)
    else:
        db.add(ColumnMapping(practice_id=practice_id, kind=kind, header_signature=signature,
                             mapping_json=json.dumps(mapping)))


def purge_expired(db, practice: Practice, ip: str = "") -> int:
    """Deletes uploads, scans and outbox entries older than the practice's retention period."""
    cutoff = utcnow() - timedelta(days=practice.retention_days)
    n = 0
    for model in (OutboxMessage, ScanRun, Dataset, ReminderLog):
        n += db.execute(delete(model).where(model.practice_id == practice.id, model.created_at < cutoff)).rowcount
    for model in (WorkItem, PatientState):
        n += db.execute(delete(model).where(model.practice_id == practice.id, model.updated_at < cutoff)).rowcount
    if n:
        audit.log(db, "data_purged", practice_id=practice.id, ip=ip, records=n,
                  retention_days=practice.retention_days)
    return n
