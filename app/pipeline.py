"""Upload -> clean -> scan -> worklist -> reminders. Used by the web pages, the API and the daily run,
so a scan with nobody at the keyboard does exactly what a staff click does."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import audit, config, crypto, outreach, presets, services, worklist
from .importer import KINDS, guess_mapping, header_signature, missing_required, normalize, read_csv
from .models import Dataset, OutboxMessage, Practice, ReminderLog, ScanRun, utcnow
from .scanner import run_scan

AUTOMATIC = 0  # "user id" of the automatic run


def local_now(practice: Practice) -> datetime:
    return datetime.now(ZoneInfo(practice.timezone))


def local_today(practice: Practice):
    return local_now(practice).date()


def intake(db, practice, actor_id: int, ip: str, texts: dict, review: bool = False) -> tuple[Dataset, list[str]]:
    """Stores the uploaded files encrypted. Returns (dataset, what's missing); if nothing is missing and no review
    was asked for, the dataset is cleaned and ready."""
    raw, pending, problems = {}, {}, []
    for kind, text in texts.items():
        headers, rows = read_csv(text)
        sig = header_signature(headers)
        mapping, source = services.saved_mapping(db, practice.id, kind, sig), "saved"
        if not mapping:
            preset, mapping = presets.detect(kind, headers)
            source = preset or "guessed"
        if not mapping:
            mapping = guess_mapping(kind, headers)
        problems += [f"{kind}: {label}" for label in missing_required(kind, mapping)]
        raw[kind] = text
        pending[kind] = {"headers": headers, "mapping": mapping, "signature": sig, "rows": len(rows),
                         "matched_by": source}
    ds = Dataset(practice_id=practice.id, uploaded_by=actor_id, status="needs_mapping",
                 pending_json=json.dumps(pending), raw_enc=crypto.encrypt_json(practice, raw))
    db.add(ds)
    db.flush()
    audit.log(db, "upload", practice_id=practice.id, user_id=actor_id or None, ip=ip, dataset_id=ds.id,
              rows={k: v["rows"] for k, v in pending.items()})
    if not problems and not review:
        finalize(db, practice, ds, {k: v["mapping"] for k, v in pending.items()}, actor_id, ip)
    return ds, problems


def finalize(db, practice, ds: Dataset, mappings: dict, actor_id: int, ip: str):
    raw = crypto.decrypt_json(practice, ds.raw_enc)
    pending = json.loads(ds.pending_json)
    data, counts = {k: [] for k in KINDS}, {}
    for kind, text in raw.items():
        _, rows = read_csv(text)
        records, stats = normalize(kind, rows, mappings[kind])
        data[kind] = records
        counts[kind] = {"rows": len(records), "skipped": stats["missing_value"] + stats["bad_date"], **stats,
                        "matched_by": pending[kind].get("matched_by", "")}
        services.save_mapping(db, practice.id, kind, pending[kind]["signature"], mappings[kind])
    ds.data_enc = crypto.encrypt_json(practice, data)
    ds.raw_enc = None  # keep only the cleaned fields we need
    ds.status = "ready"
    ds.counts_json = json.dumps(counts)
    audit.log(db, "data_prepared", practice_id=practice.id, user_id=actor_id or None, ip=ip, dataset_id=ds.id,
              counts=counts)


def scan(db, practice, ds: Dataset, actor_id: int, ip: str, today=None) -> ScanRun:
    data = crypto.decrypt_json(practice, ds.data_enc)
    out = run_scan(data, services.rx_days(db, practice.id), services.lab_rules(db, practice.id),
                   today or local_today(practice), practice.lookahead_days)
    summary = out.pop("summary")
    summary["worklist"] = worklist.sync(db, practice, out)
    run = ScanRun(practice_id=practice.id, dataset_id=ds.id, run_by=actor_id, summary_json=json.dumps(summary),
                  results_enc=crypto.encrypt_json(practice, out))
    db.add(run)
    db.flush()
    audit.log(db, "scan_run", practice_id=practice.id, user_id=actor_id or None, ip=ip, scan_id=run.id,
              dataset_id=ds.id, **{k: v for k, v in summary.items() if isinstance(v, int)})
    return run


def load(db, practice, run: ScanRun, today=None) -> dict:
    payload = crypto.decrypt_json(practice, run.results_enc)
    return worklist.annotate(db, practice, payload, today or local_today(practice))


def latest_scan(db, practice_id: int) -> ScanRun | None:
    return db.scalar(select(ScanRun).where(ScanRun.practice_id == practice_id).order_by(ScanRun.id.desc()).limit(1))


def logged_stages(db, practice_id: int) -> set:
    rows = db.execute(select(ReminderLog.patient_hash, ReminderLog.visit_date, ReminderLog.stage)
                      .where(ReminderLog.practice_id == practice_id, ReminderLog.test_mode.is_(config.DRY_RUN)))
    return {tuple(r) for r in rows}


def send_due_reminders(db, practice, run: ScanRun, actor_id: int, ip: str, today=None) -> dict:
    today = today or local_today(practice)
    payload = load(db, practice, run, today)
    due, _ = outreach.due_reminders(practice, payload["results"], logged_stages(db, practice.id), today)
    counts = {}
    for draft, status, note in outreach.send_drafts(practice, due):
        counts[status] = counts.get(status, 0) + 1
        db.add(OutboxMessage(practice_id=practice.id, scan_id=run.id, status=status, note=note, kind="patient",
                             message_enc=crypto.encrypt_json(practice, {k: draft[k] for k in ("to", "subject", "body")})))
        if status in ("sent", "dry_run"):
            db.add(ReminderLog(practice_id=practice.id, patient_hash=draft["patient_hash"],
                               visit_date=draft["visit_date"], stage=draft["stage"], test_mode=status == "dry_run"))
    audit.log(db, "patient_emails", practice_id=practice.id, user_id=actor_id or None, ip=ip, scan_id=run.id,
              test_mode=config.DRY_RUN, results=counts)
    db.flush()
    return counts


def automatic_run(db, practice, ds: Dataset | None = None, ip: str = "auto", today=None) -> ScanRun | None:
    """What happens with nobody at the keyboard: tidy up, scan, and send due reminders if the practice allows it."""
    services.purge_expired(db, practice, ip)
    ds = ds or db.scalar(select(Dataset).where(Dataset.practice_id == practice.id, Dataset.status == "ready")
                         .order_by(Dataset.id.desc()).limit(1))
    if not ds or not practice.baa_signed_at:
        return None
    run = scan(db, practice, ds, AUTOMATIC, ip, today)
    if practice.auto_send:
        if (utcnow() - ds.created_at).days >= config.STALE_DATA_DAYS:
            # Old data may be wrong by now: never email patients from it without a person looking.
            audit.log(db, "auto_send_held_stale_data", practice_id=practice.id, ip=ip, dataset_id=ds.id)
        else:
            send_due_reminders(db, practice, run, AUTOMATIC, ip, today)
    return run
