"""Hands-off running: the upload API for the office-PC agent, and the daily scan loop."""
import hmac
import logging
import threading
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from . import audit, config, pipeline
from .auth import client_ip, sha
from .db import SessionLocal, get_db
from .importer import KINDS, decode_bytes, read_csv
from .models import Practice

router = APIRouter(prefix="/api/v1")
log = logging.getLogger("previsit.automation")


def _practice_for_key(db, key: str) -> Practice:
    practice = db.scalar(select(Practice).where(Practice.api_key_hash == sha(key or "-")))
    if not practice or not hmac.compare_digest(practice.api_key_hash, sha(key)):
        raise HTTPException(401, "Bad upload key.")
    return practice


@router.post("/import")
async def api_import(request: Request, x_previsit_key: str = Header(""), db=Depends(get_db)):
    """The office PC posts its nightly exports here. The key can upload and nothing else."""
    practice = _practice_for_key(db, x_previsit_key)
    ip = client_ip(request)
    if not practice.baa_signed_at:
        return JSONResponse({"ok": False, "error": "The practice owner must sign the BAA first."}, status_code=403)
    form = await request.form()
    texts = {}
    for kind in KINDS:
        f = form.get(kind)
        if f is None or not getattr(f, "filename", ""):
            continue
        data = await f.read()
        if len(data) > config.MAX_UPLOAD_BYTES:
            return JSONResponse({"ok": False, "error": f"{kind} file is larger than 20 MB."}, status_code=413)
        texts[kind] = decode_bytes(data)
        if not read_csv(texts[kind])[1]:
            return JSONResponse({"ok": False, "error": f"{kind} file is empty."}, status_code=422)
    if "appointments" not in texts:
        return JSONResponse({"ok": False, "error": "The appointments file is required."}, status_code=422)
    ds, problems = pipeline.intake(db, practice, pipeline.AUTOMATIC, ip, texts)
    if ds.status != "ready":
        db.commit()  # staff see a "match your columns" prompt on the dashboard
        return JSONResponse({"ok": False, "error": "Columns need matching in Previsit.", "missing": problems},
                            status_code=422)
    run = pipeline.automatic_run(db, practice, ds, ip)
    db.commit()
    return {"ok": True, "scan_id": run.id}


def run_due_practices(db, now_for=pipeline.local_now) -> int:
    """Runs today's scan for every practice whose daily hour has arrived. Safe to call as often as you like."""
    ran = 0
    for practice in db.scalars(select(Practice).where(Practice.auto_scan.is_(True), Practice.is_platform.is_(False))):
        now = now_for(practice)
        today = now.date().isoformat()
        if practice.last_auto_run == today or now.hour < practice.auto_scan_hour:
            continue
        practice.last_auto_run = today
        try:
            if pipeline.automatic_run(db, practice, today=now.date()):
                ran += 1
            db.commit()
        except Exception:  # one practice's bad day must not stop the others
            db.rollback()
            log.exception("daily run failed for practice %s", practice.id)
            audit.log(db, "daily_run_failed", practice_id=practice.id, ip="auto")
            db.commit()
    return ran


def _loop():
    while True:
        try:
            with SessionLocal() as db:
                run_due_practices(db)
        except Exception:
            log.exception("daily scan loop error")
        time.sleep(300)


def start_scheduler():
    if config.SCHEDULER:
        threading.Thread(target=_loop, name="previsit-daily", daemon=True).start()
