"""Day-to-day pages: dashboard, upload (+ column matching), worklist, call list, patient and office outreach."""
import json
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from . import config, crypto, outreach, pipeline, services, worklist
from .auth import Ctx, require, require_post
from .importer import FIELDS, KINDS, decode_bytes, missing_required, read_csv
from .models import Dataset, OutboxMessage, ScanRun, User, utcnow
from .scanner import GROUP_LABELS, GROUPS
from .web import redirect, render

router = APIRouter()


def _get_owned(ctx: Ctx, model, obj_id: int):
    """Loads a record only if it belongs to the signed-in user's practice (404 otherwise)."""
    obj = ctx.db.get(model, obj_id)
    if not obj or obj.practice_id != ctx.practice.id:
        raise HTTPException(404, "Not found.")
    return obj


def _names(ctx: Ctx) -> dict:
    names = {u.id: u.name for u in ctx.db.scalars(select(User).where(User.practice_id == ctx.practice.id))}
    names[pipeline.AUTOMATIC] = "Automatic"
    return names


@router.get("/")
def dashboard(request: Request, ctx: Ctx = Depends(require("view"))):
    db, pid = ctx.db, ctx.practice.id
    services.purge_expired(db, ctx.practice, ctx.ip)
    db.commit()
    scans = db.scalars(select(ScanRun).where(ScanRun.practice_id == pid).order_by(ScanRun.id.desc()).limit(8)).all()
    latest = db.scalar(select(Dataset).where(Dataset.practice_id == pid, Dataset.status == "ready")
                       .order_by(Dataset.id.desc()).limit(1))
    stuck = db.scalar(select(Dataset).where(Dataset.practice_id == pid, Dataset.status == "needs_mapping")
                      .order_by(Dataset.id.desc()).limit(1))
    age = (utcnow() - latest.created_at).days if latest else None
    emails = db.scalar(select(func.count()).select_from(OutboxMessage).where(
        OutboxMessage.practice_id == pid, OutboxMessage.status.in_(["sent", "dry_run"])))
    return render(request, "dashboard.html", ctx, scans=[(s, json.loads(s.summary_json)) for s in scans],
                  latest=latest, latest_counts=json.loads(latest.counts_json) if latest else {}, names=_names(ctx),
                  stale=age is not None and age >= config.STALE_DATA_DAYS, age=age, stuck=stuck,
                  totals=worklist.totals(db, pid), emails=emails, KINDS=KINDS)


@router.post("/upload")
async def upload(request: Request, ctx: Ctx = Depends(require_post("upload"))):
    form = await request.form()
    texts = {}
    for kind in KINDS:
        f = form.get(kind)
        if f is None or not getattr(f, "filename", ""):
            continue
        data = await f.read()
        if len(data) > config.MAX_UPLOAD_BYTES:
            return _upload_error(request, ctx, f"{f.filename} is larger than 20 MB.")
        texts[kind] = decode_bytes(data)
        headers, rows = read_csv(texts[kind])
        if not headers or not rows:
            return _upload_error(request, ctx, f"{f.filename} looks empty. Export it again as CSV.")
    if "appointments" not in texts:
        return _upload_error(request, ctx, "Add the appointments file. Medications and labs are needed to find issues.")
    ds, problems = pipeline.intake(ctx.db, ctx.practice, ctx.user.id, ctx.ip, texts, review=form.get("review") == "1")
    if ds.status != "ready":
        ctx.db.commit()
        return redirect(f"/mapping/{ds.id}")
    run = pipeline.scan(ctx.db, ctx.practice, ds, ctx.user.id, ctx.ip)
    ctx.db.commit()
    return redirect(f"/scans/{run.id}")


def _upload_error(request, ctx, message):
    return render(request, "error.html", ctx, status=400, title="Upload problem", detail=message, back="/")


@router.get("/mapping/{ds_id}")
def mapping_page(request: Request, ds_id: int, ctx: Ctx = Depends(require("upload"))):
    ds = _get_owned(ctx, Dataset, ds_id)
    if ds.status != "needs_mapping":
        return redirect("/")
    return render(request, "mapping.html", ctx, ds=ds, pending=json.loads(ds.pending_json), FIELDS=FIELDS, error=None)


@router.post("/mapping/{ds_id}")
async def mapping_save(request: Request, ds_id: int, ctx: Ctx = Depends(require_post("upload"))):
    ds = _get_owned(ctx, Dataset, ds_id)
    if ds.status != "needs_mapping":
        return redirect("/")
    form = await request.form()
    pending = json.loads(ds.pending_json)
    mappings, problems = {}, []
    for kind, info in pending.items():
        m = {}
        for field, _, _ in FIELDS[kind]:
            header = str(form.get(f"m.{kind}.{field}", ""))
            if header and header in info["headers"]:
                m[field] = header
        mappings[kind] = m
        info["mapping"] = m
        problems += [f"{kind.title()}: pick a column for {label}" for label in missing_required(kind, m)]
    if problems:
        return render(request, "mapping.html", ctx, status=400, ds=ds, pending=pending, FIELDS=FIELDS,
                      error=" | ".join(problems))
    ds.pending_json = json.dumps(pending)
    ctx.audit("mapping_confirmed", dataset_id=ds.id)
    pipeline.finalize(ctx.db, ctx.practice, ds, mappings, ctx.user.id, ctx.ip)
    run = pipeline.scan(ctx.db, ctx.practice, ds, ctx.user.id, ctx.ip)
    ctx.db.commit()
    return redirect(f"/scans/{run.id}")


@router.post("/scan")
async def rescan(request: Request, ctx: Ctx = Depends(require_post("scan"))):
    form = await request.form()
    try:
        ds = _get_owned(ctx, Dataset, int(form.get("dataset_id", 0)))
    except ValueError:
        raise HTTPException(404, "Not found.")
    if ds.status != "ready":
        return redirect(f"/mapping/{ds.id}")
    run = pipeline.scan(ctx.db, ctx.practice, ds, ctx.user.id, ctx.ip)
    ctx.db.commit()
    return redirect(f"/scans/{run.id}")


# ---------- worklist ----------

@router.get("/worklist")
def current_worklist(ctx: Ctx = Depends(require("view"))):
    run = pipeline.latest_scan(ctx.db, ctx.practice.id)
    return redirect(f"/scans/{run.id}" if run else "/")


@router.get("/scans/{scan_id}")
def scan_page(request: Request, scan_id: int, group: str = "", provider: str = "", show: str = "todo",
              ctx: Ctx = Depends(require("view"))):
    run = _get_owned(ctx, ScanRun, scan_id)
    payload = pipeline.load(ctx.db, ctx.practice, run)
    ctx.audit("scan_viewed", scan_id=run.id)
    ctx.db.commit()
    flagged = payload["results"]
    providers = sorted({r["provider"] for r in flagged + payload["clear"] + payload["unchecked"] if r["provider"]})
    if group not in GROUPS:
        group = "expired" if any(r["group"] == "expired" for r in flagged) else (
            "expiring" if any(r["group"] == "expiring" for r in flagged) else "clear")
    everyone = {"expired": [r for r in flagged if r["group"] == "expired"],
                "expiring": [r for r in flagged if r["group"] == "expiring"],
                "unchecked": payload["unchecked"], "clear": payload["clear"]}
    counts = {g: len(rows) for g, rows in everyone.items()}
    to_do = {g: sum(1 for r in rows if r.get("open_count")) for g, rows in everyone.items()}
    rows = everyone[group]
    if provider:
        rows = [r for r in rows if r["provider"] == provider]
    if show == "todo" and group in ("expired", "expiring"):
        rows = [r for r in rows if r["open_count"]]
    issues = [i for r in flagged for i in r["issues"]]
    handled = sum(i["work"] not in worklist.ACTIVE for i in issues)
    return render(request, "scan.html", ctx, scan=run, summary=json.loads(run.summary_json), rows=rows, group=group,
                  counts=counts, to_do=to_do, provider=provider, providers=providers, show=show, GROUPS=GROUPS,
                  GROUP_LABELS=GROUP_LABELS, handled=handled, total=len(issues),
                  percent=round(100 * handled / len(issues)) if issues else 100,
                  STATUS=worklist.STATUS_LABELS, OUTCOMES=worklist.OUTCOMES, ACTIVE=worklist.ACTIVE,
                  script=lambda r: outreach.call_script(ctx.practice.name, r))


def _back(scan_id: int, form, anchor: str):
    group, provider, show = form.get("group", ""), form.get("provider", ""), form.get("show", "todo")
    return redirect(f"/scans/{scan_id}?{urlencode({'group': group, 'provider': provider, 'show': show})}#{anchor}")


@router.post("/scans/{scan_id}/work")
async def work_update(request: Request, scan_id: int, ctx: Ctx = Depends(require_post("work"))):
    run = _get_owned(ctx, ScanRun, scan_id)
    form = await request.form()
    w = worklist.set_status(ctx.db, ctx.practice, str(form.get("item", "")), str(form.get("status", "")), ctx.user.id)
    if not w:
        raise HTTPException(404, "Not found.")
    ctx.audit("work_item_updated", work_item_id=w.id, status=w.status)
    ctx.db.commit()
    return _back(run.id, form, "p" + w.patient_hash[:12])


@router.post("/scans/{scan_id}/contact")
async def contact_log(request: Request, scan_id: int, ctx: Ctx = Depends(require_post("work"))):
    run = _get_owned(ctx, ScanRun, scan_id)
    form = await request.form()
    ph, outcome = str(form.get("patient", "")), str(form.get("outcome", ""))
    payload = crypto.decrypt_json(ctx.practice, run.results_enc)
    known = {worklist.patient_hash(ctx.practice, r["patient_id"]) for r in payload["results"]}
    if ph not in known or outcome not in worklist.OUTCOMES:
        raise HTTPException(404, "Not found.")
    worklist.log_contact(ctx.db, ctx.practice, ph, outcome, str(form.get("note", "")).strip(), ctx.user.name,
                         pipeline.local_today(ctx.practice))
    ctx.audit("patient_contact_logged", scan_id=run.id, outcome=outcome)
    ctx.db.commit()
    return _back(run.id, form, "p" + ph[:12])


@router.post("/scans/{scan_id}/optout")
async def email_optout(request: Request, scan_id: int, ctx: Ctx = Depends(require_post("work"))):
    run = _get_owned(ctx, ScanRun, scan_id)
    form = await request.form()
    ph = str(form.get("patient", ""))
    payload = crypto.decrypt_json(ctx.practice, run.results_enc)
    if ph not in {worklist.patient_hash(ctx.practice, r["patient_id"]) for r in payload["results"]}:
        raise HTTPException(404, "Not found.")
    value = form.get("value") == "1"
    worklist.set_do_not_email(ctx.db, ctx.practice, ph, value)
    ctx.audit("patient_email_preference", scan_id=run.id, do_not_email=value)
    ctx.db.commit()
    return _back(run.id, form, "p" + ph[:12])


@router.get("/scans/{scan_id}/calllist.{ext}")
def call_list(scan_id: int, ext: str, ctx: Ctx = Depends(require("export"))):
    if ext not in ("xlsx", "csv"):
        raise HTTPException(404, "Not found.")
    run = _get_owned(ctx, ScanRun, scan_id)
    payload = pipeline.load(ctx.db, ctx.practice, run)
    ctx.audit("call_list_exported", scan_id=run.id, format=ext, patients=len(payload["results"]))
    ctx.db.commit()
    if ext == "xlsx":
        body = outreach.call_list_xlsx(payload, ctx.practice.name)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        body, media = outreach.call_list_csv(payload), "text/csv"
    stamp = run.created_at.strftime("%Y-%m-%d_%H%M")
    return Response(body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="call_list_scan{run.id}_{stamp}.{ext}"'})


# ---------- patient reminders ----------

@router.get("/scans/{scan_id}/emails")
def emails_page(request: Request, scan_id: int, ctx: Ctx = Depends(require("view"))):
    run = _get_owned(ctx, ScanRun, scan_id)
    payload = pipeline.load(ctx.db, ctx.practice, run)
    today = pipeline.local_today(ctx.practice)
    drafts, held = outreach.due_reminders(ctx.practice, payload["results"],
                                          pipeline.logged_stages(ctx.db, ctx.practice.id), today)
    return render(request, "emails.html", ctx, scan=run, drafts=drafts, held=held,
                  mailbox_ready=bool(ctx.practice.smtp_host and ctx.practice.smtp_from))


@router.post("/scans/{scan_id}/send")
def emails_send(scan_id: int, ctx: Ctx = Depends(require_post("send"))):
    run = _get_owned(ctx, ScanRun, scan_id)
    pipeline.send_due_reminders(ctx.db, ctx.practice, run, ctx.user.id, ctx.ip)
    ctx.db.commit()
    return redirect(f"/scans/{run.id}/outbox")


@router.get("/scans/{scan_id}/outbox")
def outbox_page(request: Request, scan_id: int, ctx: Ctx = Depends(require("view"))):
    run = _get_owned(ctx, ScanRun, scan_id)
    rows = ctx.db.scalars(select(OutboxMessage).where(OutboxMessage.scan_id == run.id)
                          .order_by(OutboxMessage.id.desc())).all()
    messages = [(m, crypto.decrypt_json(ctx.practice, m.message_enc)) for m in rows]
    return render(request, "outbox.html", ctx, scan=run, messages=messages)


# ---------- requests to prescribing offices ----------

def _office_groups(ctx: Ctx, run: ScanRun) -> list[dict]:
    return outreach.office_groups(pipeline.load(ctx.db, ctx.practice, run)["results"])


def _office_group(ctx: Ctx, run: ScanRun, gid: str) -> dict:
    for g in _office_groups(ctx, run):
        if g["gid"] == gid:
            return g
    raise HTTPException(404, "Nothing is waiting for this office any more.")


def _mark_waiting(ctx: Ctx, g: dict):
    for x in g["rx"] + g["labs"]:
        worklist.set_status(ctx.db, ctx.practice, x["work_hash"], "waiting", ctx.user.id)


@router.get("/scans/{scan_id}/offices")
def offices_page(request: Request, scan_id: int, ctx: Ctx = Depends(require("view"))):
    run = _get_owned(ctx, ScanRun, scan_id)
    groups = _office_groups(ctx, run)
    previews = {g["gid"]: outreach.office_draft(ctx.practice, g) for g in groups}
    return render(request, "offices.html", ctx, scan=run, groups=groups, previews=previews)


@router.post("/scans/{scan_id}/offices/send-all")
def offices_send_all(scan_id: int, ctx: Ctx = Depends(require_post("send"))):
    run = _get_owned(ctx, ScanRun, scan_id)
    groups = [g for g in _office_groups(ctx, run) if g["email"]]
    counts = {}
    for g in groups:
        (draft, status, note), = outreach.send_drafts(ctx.practice, [outreach.office_draft(ctx.practice, g)])
        counts[status] = counts.get(status, 0) + 1
        ctx.db.add(OutboxMessage(practice_id=ctx.practice.id, scan_id=run.id, status=status, note=note, kind="office",
                                 message_enc=crypto.encrypt_json(ctx.practice, draft)))
        if status in ("sent", "dry_run"):
            _mark_waiting(ctx, g)
    ctx.audit("office_requests_sent", scan_id=run.id, test_mode=config.DRY_RUN, results=counts)
    ctx.db.commit()
    return redirect(f"/scans/{run.id}/outbox")


@router.get("/scans/{scan_id}/offices/{gid}/print")
def office_print(request: Request, scan_id: int, gid: str, ctx: Ctx = Depends(require("export"))):
    run = _get_owned(ctx, ScanRun, scan_id)
    g = _office_group(ctx, run, gid)
    ctx.audit("office_sheet_printed", scan_id=run.id, requests=len(g["rx"]) + len(g["labs"]))
    ctx.db.commit()
    return render(request, "office_print.html", ctx, scan=run, g=g, today=pipeline.local_today(ctx.practice))


@router.post("/scans/{scan_id}/offices/{gid}/mark-sent")
def office_mark_sent(scan_id: int, gid: str, ctx: Ctx = Depends(require_post("work"))):
    run = _get_owned(ctx, ScanRun, scan_id)
    g = _office_group(ctx, run, gid)
    _mark_waiting(ctx, g)
    ctx.audit("office_request_marked_sent", scan_id=run.id, requests=len(g["rx"]) + len(g["labs"]))
    ctx.db.commit()
    return redirect(f"/scans/{run.id}/offices")


@router.get("/insights")
def insights(request: Request, ctx: Ctx = Depends(require("view"))):
    """Counts only: what the platform did for the practice over the last 30 days."""
    db, pid = ctx.db, ctx.practice.id
    since = utcnow() - timedelta(days=30)
    runs = db.scalars(select(ScanRun).where(ScanRun.practice_id == pid, ScanRun.created_at >= since)
                      .order_by(ScanRun.id)).all()
    summaries = [json.loads(r.summary_json) for r in runs]
    by_kind = dict(db.execute(select(OutboxMessage.kind, func.count()).where(
        OutboxMessage.practice_id == pid, OutboxMessage.created_at >= since,
        OutboxMessage.status.in_(["sent", "dry_run"])).group_by(OutboxMessage.kind)).all())
    return render(request, "insights.html", ctx, runs=len(runs), totals=worklist.totals(db, pid), by_kind=by_kind,
                  automatic=sum(r.run_by == pipeline.AUTOMATIC for r in runs),
                  peak=max((s["patients_in_window"] for s in summaries), default=0),
                  latest=summaries[-1] if summaries else None)
