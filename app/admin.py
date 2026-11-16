"""Practice admin: rules, users and invites, settings, security page and audit export."""
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from . import audit, config, crypto, services, sso
from .auth import Ctx, new_token, require, require_post, sha
from .models import AccessRequest, AuditEvent, AuthSession, Dataset, LabRule, RxRule, ScanRun, User, utcnow
from .scanner import RX_CATEGORIES
from .web import redirect, render

router = APIRouter()
TIMEZONES = ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles",
             "America/Anchorage", "Pacific/Honolulu"]


def _int(value, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "Enter a whole number.")
    if not lo <= n <= hi:
        raise HTTPException(400, f"Enter a number from {lo} to {hi}.")
    return n


# ---------- rules ----------

@router.get("/rules")
def rules_page(request: Request, ctx: Ctx = Depends(require("view"))):
    rx = {r.category: r for r in ctx.db.scalars(select(RxRule).where(RxRule.practice_id == ctx.practice.id))}
    labs = ctx.db.scalars(select(LabRule).where(LabRule.practice_id == ctx.practice.id).order_by(LabRule.id)).all()
    return render(request, "rules.html", ctx, rx=rx, labs=labs, RX_CATEGORIES=RX_CATEGORIES)


@router.post("/rules/rx")
async def rules_rx(request: Request, ctx: Ctx = Depends(require_post("manage_rules"))):
    form = await request.form()
    for rule in ctx.db.scalars(select(RxRule).where(RxRule.practice_id == ctx.practice.id)):
        rule.max_days = _int(form.get(f"days.{rule.category}"), 1, 730)
    _rules_changed(ctx, "rx_rules_changed")
    return redirect("/rules?msg=saved")


@router.post("/rules/lab")
def rules_lab_save(rule_id: int = Form(0), name: str = Form(...), drug_keywords: str = Form(...),
                   lab_keywords: str = Form(...), max_age_days: str = Form(...), active: str = Form(""),
                   ctx: Ctx = Depends(require_post("manage_rules"))):
    if rule_id:
        rule = ctx.db.get(LabRule, rule_id)
        if not rule or rule.practice_id != ctx.practice.id:
            raise HTTPException(404, "Not found.")
    else:
        rule = LabRule(practice_id=ctx.practice.id)
        ctx.db.add(rule)
    if not name.strip() or not drug_keywords.strip() or not lab_keywords.strip():
        raise HTTPException(400, "Fill in the lab name, medications and lab test names.")
    rule.name, rule.drug_keywords, rule.lab_keywords = name.strip(), drug_keywords.strip(), lab_keywords.strip()
    rule.max_age_days = _int(max_age_days, 1, 1095)
    rule.active = active == "1" or not rule_id
    ctx.db.flush()
    _rules_changed(ctx, "lab_rule_saved", rule_id=rule.id)
    return redirect("/rules?msg=saved")


@router.post("/rules/lab/{rule_id}/delete")
def rules_lab_delete(rule_id: int, ctx: Ctx = Depends(require_post("manage_rules"))):
    rule = ctx.db.get(LabRule, rule_id)
    if not rule or rule.practice_id != ctx.practice.id:
        raise HTTPException(404, "Not found.")
    ctx.db.delete(rule)
    _rules_changed(ctx, "lab_rule_deleted", rule_id=rule_id)
    return redirect("/rules?msg=saved")


@router.post("/rules/reviewed")
def rules_reviewed(reviewer: str = Form(...), ctx: Ctx = Depends(require_post("manage_rules"))):
    if not reviewer.strip():
        raise HTTPException(400, "Enter the name of the clinician who reviewed the rules.")
    ctx.practice.rules_reviewed_by = reviewer.strip()[:200]
    ctx.practice.rules_reviewed_at = utcnow()
    ctx.audit("rules_reviewed", reviewer=ctx.practice.rules_reviewed_by)
    ctx.db.commit()
    return redirect("/rules?msg=rules_reviewed")


def _rules_changed(ctx: Ctx, action: str, **detail):
    # Any change means the clinician sign-off no longer covers the current rules.
    ctx.practice.rules_reviewed_by = None
    ctx.practice.rules_reviewed_at = None
    ctx.audit(action, **detail)
    ctx.db.commit()


# ---------- users ----------

def _assignable_roles(ctx: Ctx) -> list[str]:
    return ["admin", "staff", "readonly"] if ctx.user.role == "owner" else ["staff", "readonly"]


@router.get("/users")
def users_page(request: Request, ctx: Ctx = Depends(require("manage_users"))):
    return _users(request, ctx)


def _users(request, ctx, status=200, **kw):
    users = ctx.db.scalars(select(User).where(User.practice_id == ctx.practice.id).order_by(User.id)).all()
    requests_ = ctx.db.scalars(select(AccessRequest).where(AccessRequest.practice_id == ctx.practice.id)
                               .order_by(AccessRequest.id.desc()).limit(20)).all()
    return render(request, "users.html", ctx, status=status, users=users, assignable=_assignable_roles(ctx),
                  managed=ctx.practice.iam_plan == "managed", access_requests=requests_, now=utcnow(), **kw)


def _self_managed(ctx: Ctx):
    if ctx.practice.iam_plan == "managed":
        raise HTTPException(403, "Your practice is on Managed access, so Previsit support makes user changes. "
                                 "Send a request from the Users page.")


@router.post("/users/invite")
def users_invite(request: Request, email: str = Form(...), name: str = Form(...), role: str = Form(...),
                 ctx: Ctx = Depends(require_post("manage_users"))):
    _self_managed(ctx)
    if role not in _assignable_roles(ctx):
        raise HTTPException(403, "You can't give that role.")
    user, token = services.invite_user(ctx.db, ctx.practice, ctx.user, ctx.ip, name, email, role)
    if not user:
        return _users(request, ctx, status=400, error=token)
    ctx.db.commit()
    link = str(request.base_url).rstrip("/") + f"/invite/{token}"
    return _users(request, ctx, invite_link=link, invite_email=user.email)


def _target(ctx: Ctx, user_id: int) -> User:
    _self_managed(ctx)
    user = ctx.db.get(User, user_id)
    if not user or user.practice_id != ctx.practice.id:
        raise HTTPException(404, "Not found.")
    if user.id == ctx.user.id or user.role == "owner":
        raise HTTPException(403, "You can't change your own account or the owner's here.")
    if ctx.user.role != "owner" and user.role == "admin":
        raise HTTPException(403, "Only the owner can change an admin.")
    return user


@router.post("/users/{user_id}/update")
def users_update(user_id: int, role: str = Form(...), active: str = Form(""),
                 ctx: Ctx = Depends(require_post("manage_users"))):
    user = _target(ctx, user_id)
    if role not in _assignable_roles(ctx):
        raise HTTPException(403, "You can't give that role.")
    services.update_user(ctx.db, user, ctx.user, ctx.ip, role, active == "1")
    ctx.db.commit()
    return redirect("/users?msg=user_updated")


@router.post("/users/{user_id}/reset-mfa")
def users_reset_mfa(user_id: int, ctx: Ctx = Depends(require_post("manage_users"))):
    user = _target(ctx, user_id)
    services.reset_user_mfa(ctx.db, user, ctx.user, ctx.ip)
    ctx.db.commit()
    return redirect("/users?msg=mfa_reset")


REQUEST_KINDS = {"add": "Add a user", "change_role": "Change a role", "remove": "Remove a user",
                 "reset_mfa": "Reset two-step sign-in (lost phone)"}


@router.post("/users/request")
def users_request(request: Request, kind: str = Form(...), target_email: str = Form(...),
                  target_name: str = Form(""), role: str = Form(""), note: str = Form(""),
                  ctx: Ctx = Depends(require_post("manage_users"))):
    if ctx.practice.iam_plan != "managed":
        raise HTTPException(400, "Your practice manages its own users. Make the change directly.")
    if kind not in REQUEST_KINDS or "@" not in target_email:
        return _users(request, ctx, status=400, error="Pick what you need and enter the person's work email.")
    if role and role not in ("admin", "staff", "readonly"):
        raise HTTPException(400, "Unknown role.")
    req = AccessRequest(practice_id=ctx.practice.id, requested_by=ctx.user.id, kind=kind,
                        target_email=target_email.strip().lower(), target_name=target_name.strip()[:200],
                        role=role, note=note.strip()[:1000])
    ctx.db.add(req)
    ctx.db.flush()
    ctx.audit("access_request_created", request_id=req.id, kind=kind)
    ctx.db.commit()
    return redirect("/users?msg=request_sent")


# ---------- settings ----------

def _timezone(value) -> str:
    if str(value) not in TIMEZONES:
        raise HTTPException(400, "Pick a time zone from the list.")
    return str(value)


def _reminder_days(value) -> str:
    days = sorted({_int(x.strip(), 0, 60) for x in str(value or "").split(",") if x.strip()}, reverse=True)
    if not days:
        raise HTTPException(400, "Enter at least one reminder day, like 14,7,3.")
    return ",".join(str(d) for d in days)


@router.post("/settings/upload-key")
def new_upload_key(request: Request, ctx: Ctx = Depends(require_post("manage_settings"))):
    """Shown once. Replaces any earlier key."""
    key = "pvk_" + new_token()
    ctx.practice.api_key_hash = sha(key)
    ctx.audit("upload_key_created")
    ctx.db.commit()
    return _settings(request, ctx, new_key=key)


@router.get("/settings")
def settings_page(request: Request, ctx: Ctx = Depends(require("manage_settings"))):
    return _settings(request, ctx)


def _settings(request, ctx, **kw):
    return render(request, "settings.html", ctx, error=None, SSO=sso.PROVIDERS, sso_ready=sso.configured(),
                  TIMEZONES=TIMEZONES, **kw)


@router.post("/settings")
async def settings_save(request: Request, ctx: Ctx = Depends(require_post("manage_settings"))):
    form = await request.form()
    p = ctx.practice
    new = {
        "name": str(form.get("name", "")).strip() or p.name,
        "lookahead_days": _int(form.get("lookahead_days"), 1, 60),
        "retention_days": _int(form.get("retention_days"), 1, 365),
        "require_mfa": form.get("require_mfa") == "1",
        "email_subject": str(form.get("email_subject", "")).strip() or p.email_subject,
        "email_body": str(form.get("email_body", "")).replace("\r\n", "\n").strip() or p.email_body,
        "smtp_host": str(form.get("smtp_host", "")).strip(),
        "smtp_port": _int(form.get("smtp_port") or 587, 1, 65535),
        "smtp_user": str(form.get("smtp_user", "")).strip(),
        "smtp_from": str(form.get("smtp_from", "")).strip(),
        "timezone": _timezone(form.get("timezone")),
        "reminder_days": _reminder_days(form.get("reminder_days")),
        "auto_scan": form.get("auto_scan") == "1",
        "auto_scan_hour": _int(form.get("auto_scan_hour") or 6, 0, 23),
        "auto_send": form.get("auto_send") == "1",
        "sso_providers": ",".join(p for p in sso.PROVIDERS if form.get(f"sso.{p}") == "1"),
    }
    changed = [k for k, v in new.items() if getattr(p, k) != v]
    for k, v in new.items():
        setattr(p, k, v)
    password = str(form.get("smtp_password", ""))
    if password:
        p.smtp_password_enc = crypto.encrypt_text(p, password)
        changed.append("smtp_password")
    ctx.audit("settings_changed", fields=changed)
    ctx.db.commit()
    return redirect("/settings?msg=saved")


# ---------- security ----------

@router.get("/security")
def security_page(request: Request, ctx: Ctx = Depends(require("view_security"))):
    db, pid = ctx.db, ctx.practice.id
    users = db.scalars(select(User).where(User.practice_id == pid).order_by(User.id)).all()
    names = {u.id: u.email for u in users}
    week_ago = utcnow() - timedelta(days=7)
    failed = db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.practice_id == pid, AuditEvent.ts >= week_ago,
        AuditEvent.action.in_(["login_failed", "mfa_failed", "login_blocked_locked"])))
    locks = db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.practice_id == pid, AuditEvent.ts >= week_ago, AuditEvent.action == "account_locked"))
    sessions = db.scalar(select(func.count()).select_from(AuthSession).join(User, User.id == AuthSession.user_id)
                         .where(User.practice_id == pid))
    datasets = db.scalar(select(func.count()).select_from(Dataset).where(Dataset.practice_id == pid))
    scans = db.scalar(select(func.count()).select_from(ScanRun).where(ScanRun.practice_id == pid))
    oldest = db.scalar(select(func.min(Dataset.created_at)).where(Dataset.practice_id == pid))
    chain_ok, checked, bad_id = audit.verify_chain(db, pid)
    events = db.scalars(select(AuditEvent).where(AuditEvent.practice_id == pid)
                        .order_by(AuditEvent.id.desc()).limit(100)).all()
    return render(request, "security.html", ctx, users=users, names=names, failed=failed, locks=locks,
                  sessions=sessions, datasets=datasets, scans=scans, oldest=oldest, chain_ok=chain_ok,
                  checked=checked, bad_id=bad_id, events=[(e, json.loads(e.detail)) for e in events],
                  now=utcnow(), allowlist=sorted(config.EMAIL_ALLOWLIST))


@router.get("/security/audit.jsonl")
def audit_export(ctx: Ctx = Depends(require("view_security"))):
    ctx.audit("audit_exported")
    ctx.db.commit()
    lines = []
    for e in ctx.db.scalars(select(AuditEvent).where(AuditEvent.practice_id == ctx.practice.id).order_by(AuditEvent.id)):
        lines.append(json.dumps({"event_id": e.id, "ts": e.ts.isoformat(), "user_id": e.user_id, "ip": e.ip,
                                 "action": e.action, "detail": json.loads(e.detail), "prev_hash": e.prev_hash,
                                 "hash": e.hash}))
    stamp = utcnow().strftime("%Y-%m-%d_%H%M")
    return Response("\n".join(lines) + "\n", media_type="application/x-ndjson",
                    headers={"Content-Disposition": f'attachment; filename="audit_log_{stamp}.jsonl"'})
