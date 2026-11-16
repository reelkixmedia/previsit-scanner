"""Tables. Patient data only ever lives in the *_enc columns, encrypted with the practice's own key."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Practice(Base):
    __tablename__ = "practices"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)  # data key, encrypted by the master key
    require_mfa: Mapped[bool] = mapped_column(Boolean, default=True)
    lookahead_days: Mapped[int] = mapped_column(Integer, default=14)
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    email_subject: Mapped[str] = mapped_column(Text)
    email_body: Mapped[str] = mapped_column(Text)
    smtp_host: Mapped[str] = mapped_column(String(200), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_user: Mapped[str] = mapped_column(String(200), default="")
    smtp_password_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    smtp_from: Mapped[str] = mapped_column(String(200), default="")
    rules_reviewed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rules_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The platform operator's own account lives in one practice flagged is_platform.
    is_platform: Mapped[bool] = mapped_column(Boolean, default=False)
    # "managed": Previsit support adds users and changes roles on request. "self": the practice's admins do.
    iam_plan: Mapped[str] = mapped_column(String(20), default="self")
    sso_providers: Mapped[str] = mapped_column(String(100), default="")  # e.g. "microsoft,google"
    timezone: Mapped[str] = mapped_column(String(60), default="America/New_York")
    reminder_days: Mapped[str] = mapped_column(String(40), default="14,7,3")  # patient emails, days before the visit
    auto_scan: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_scan_hour: Mapped[int] = mapped_column(Integer, default=6)  # local hour for the daily run
    auto_send: Mapped[bool] = mapped_column(Boolean, default=False)  # daily run also sends due patient reminders
    last_auto_run: Mapped[str] = mapped_column(String(10), default="")  # local date of the last daily run
    api_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    baa_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    baa_text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    baa_signed_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    baa_signed_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    baa_signed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baa_signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    baa_signed_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practices.id"), index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # owner | admin | staff | readonly
    password_hash: Mapped[str | None] = mapped_column(String(300), nullable=True)
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    totp_last_step: Mapped[int] = mapped_column(Integer, default=0)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sso_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sso_subject: Mapped[str | None] = mapped_column(String(200), nullable=True)  # bound on first SSO sign-in


class AccessRequest(Base):
    """Managed plan: a practice asks Previsit support to add, change or remove a user."""
    __tablename__ = "access_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practices.id"), index=True)
    requested_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(20))  # add | change_role | remove | reset_mfa
    target_email: Mapped[str] = mapped_column(String(254))
    target_name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(20), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | done | declined
    handled_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reply: Mapped[str] = mapped_column(Text, default="")


class SsoState(Base):
    """One-time state for a Microsoft/Google sign-in round trip (10-minute life)."""
    __tablename__ = "sso_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(20))
    nonce: Mapped[str] = mapped_column(String(100))
    code_verifier: Mapped[str] = mapped_column(String(150))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    mfa_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")


class AuditEvent(Base):
    """Append-only, hash-chained per practice. Never holds patient names or details."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    practice_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(60), index=True)
    detail: Mapped[str] = mapped_column(Text, default="{}")
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class ColumnMapping(Base):
    __tablename__ = "column_mappings"
    id: Mapped[int] = mapped_column(primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practices.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    header_signature: Mapped[str] = mapped_column(String(64))
    mapping_json: Mapped[str] = mapped_column(Text)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[int] = mapped_column(primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practices.id"), index=True)
    uploaded_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20))  # needs_mapping | ready
    counts_json: Mapped[str] = mapped_column(Text, default="{}")  # row counts only
    pending_json: Mapped[str] = mapped_column(Text, default="{}")  # headers + guessed mapping (no PHI)
    raw_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # dropped once mapped
    data_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class RxRule(Base):
    __tablename__ = "rx_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practices.id"), index=True)
    category: Mapped[str] = mapped_column(String(20))  # non-controlled | C-III-V | C-II
    max_days: Mapped[int] = mapped_column(Integer)


class LabRule(Base):
    __tablename__ = "lab_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practices.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))  # patient-facing lab name, e.g. "A1c"
    drug_keywords: Mapped[str] = mapped_column(Text)
    lab_keywords: Mapped[str] = mapped_column(Text)
    max_age_days: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ScanRun(Base):
    __tablename__ = "scan_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practices.id"), index=True)
    dataset_id: Mapped[int] = mapped_column(Integer)
    run_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")  # counts only
    results_enc: Mapped[bytes] = mapped_column(LargeBinary)


class OutboxMessage(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practices.id"), index=True)
    scan_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20))  # dry_run | sent | skipped | failed
    kind: Mapped[str] = mapped_column(String(20), default="patient")  # patient | office
    message_enc: Mapped[bytes] = mapped_column(LargeBinary)  # to/subject/body
    note: Mapped[str] = mapped_column(String(300), default="")


class WorkItem(Base):
    """One thing to fix for one patient. Survives from scan to scan, so staff never redo finished work.
    Keys are keyed hashes: nothing here identifies a patient without the practice's data key."""
    __tablename__ = "work_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practices.id"), index=True)
    item_hash: Mapped[str] = mapped_column(String(64), index=True)
    patient_hash: Mapped[str] = mapped_column(String(64), index=True)
    visit_date: Mapped[str] = mapped_column(String(10))
    kind: Mapped[str] = mapped_column(String(10))  # rx | lab
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | waiting | done | not_needed | cleared
    closed_reason: Mapped[str] = mapped_column(String(40), default="")
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PatientState(Base):
    """Per-patient contact history and email opt-out, encrypted."""
    __tablename__ = "patient_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practices.id"), index=True)
    patient_hash: Mapped[str] = mapped_column(String(64), index=True)
    do_not_email: Mapped[bool] = mapped_column(Boolean, default=False)
    log_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReminderLog(Base):
    """Which reminder stage (14/7/3 days out) a patient already got for a visit, so nobody gets repeats."""
    __tablename__ = "reminder_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practices.id"), index=True)
    patient_hash: Mapped[str] = mapped_column(String(64), index=True)
    visit_date: Mapped[str] = mapped_column(String(10))
    stage: Mapped[int] = mapped_column(Integer)
    test_mode: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
