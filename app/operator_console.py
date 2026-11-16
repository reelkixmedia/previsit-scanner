"""Operator console for Previsit support (you): every practice's plan, BAA status, users and access requests.

Operators manage *access* only. They have no route to any practice's uploads, scans or emails.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select

from . import services
from .auth import Ctx, require, require_post
from .models import AccessRequest, Practice, ScanRun, User, utcnow
from .web import redirect, render

router = APIRouter(prefix="/operator")
OPERATOR_ROLES = ["owner", "admin", "staff", "readonly"]


def _practice(ctx: Ctx, practice_id: int) -> Practice:
    p = ctx.db.get(Practice, practice_id)
    if not p or p.is_platform:
        raise HTTPException(404, "Not found.")
    return p


@router.get("")
def console(request: Request, ctx: Ctx = Depends(require("operate"))):
    db = ctx.db
    practices = db.scalars(select(Practice).where(Practice.is_platform.is_(False)).order_by(Practice.id)).all()
    users = dict(db.execute(select(User.practice_id, func.count()).group_by(User.practice_id)).all())
    open_reqs = dict(db.execute(select(AccessRequest.practice_id, func.count())
                                .where(AccessRequest.status == "open").group_by(AccessRequest.practice_id)).all())
    last_scan = dict(db.execute(select(ScanRun.practice_id, func.max(ScanRun.created_at))
                                .group_by(ScanRun.practice_id)).all())
    queue = db.scalars(select(AccessRequest).where(AccessRequest.status == "open").order_by(AccessRequest.id)).all()
    names = {p.id: p.name for p in practices}
    return render(request, "operator.html", ctx, practices=practices, users=users, open_reqs=open_reqs,
                  last_scan=last_scan, queue=queue, names=names, PLANS=services.IAM_PLANS)


@router.get("/practices/{practice_id}")
def practice_page(request: Request, practice_id: int, ctx: Ctx = Depends(require("operate"))):
    return _practice_view(request, ctx, _practice(ctx, practice_id))


def _practice_view(request, ctx, p, status=200, **kw):
    users = ctx.db.scalars(select(User).where(User.practice_id == p.id).order_by(User.id)).all()
    reqs = ctx.db.scalars(select(AccessRequest).where(AccessRequest.practice_id == p.id)
                          .order_by(AccessRequest.status != "open", AccessRequest.id.desc()).limit(50)).all()
    return render(request, "operator_practice.html", ctx, status=status, p=p, users=users, reqs=reqs,
                  roles=OPERATOR_ROLES, now=utcnow(), PLANS=services.IAM_PLANS, **kw)


@router.post("/practices/{practice_id}/plan")
def set_plan(practice_id: int, iam_plan: str = Form(...), ctx: Ctx = Depends(require_post("operate"))):
    p = _practice(ctx, practice_id)
    if iam_plan not in services.IAM_PLANS:
        raise HTTPException(400, "Unknown plan.")
    was, p.iam_plan = p.iam_plan, iam_plan
    services.log_access_change(ctx.db, p.id, ctx.user, ctx.ip, "iam_plan_changed", plan_from=was, plan_to=iam_plan)
    ctx.db.commit()
    return redirect(f"/operator/practices/{p.id}?msg=plan_changed")


@router.post("/practices/{practice_id}/invite")
def invite(request: Request, practice_id: int, name: str = Form(...), email: str = Form(...), role: str = Form(...),
           ctx: Ctx = Depends(require_post("operate"))):
    p = _practice(ctx, practice_id)
    if role not in ("admin", "staff", "readonly"):
        raise HTTPException(400, "Unknown role.")
    user, token = services.invite_user(ctx.db, p, ctx.user, ctx.ip, name, email, role)
    if not user:
        return _practice_view(request, ctx, p, status=400, error=token)
    ctx.db.commit()
    link = str(request.base_url).rstrip("/") + f"/invite/{token}"
    return _practice_view(request, ctx, p, invite_link=link, invite_email=user.email)


def _user(ctx: Ctx, p: Practice, user_id: int) -> User:
    u = ctx.db.get(User, user_id)
    if not u or u.practice_id != p.id:
        raise HTTPException(404, "Not found.")
    return u


@router.post("/practices/{practice_id}/users/{user_id}/update")
def update(practice_id: int, user_id: int, role: str = Form(...), active: str = Form(""),
           ctx: Ctx = Depends(require_post("operate"))):
    p = _practice(ctx, practice_id)
    u = _user(ctx, p, user_id)
    if u.role == "owner" and (role != "owner" or active != "1"):
        raise HTTPException(403, "The owner can't be demoted or disabled here. Transfer ownership first.")
    if role == "owner" and u.role != "owner":
        raise HTTPException(403, "Ownership transfer isn't built yet.")
    if role not in OPERATOR_ROLES:
        raise HTTPException(400, "Unknown role.")
    services.update_user(ctx.db, u, ctx.user, ctx.ip, role, active == "1")
    ctx.db.commit()
    return redirect(f"/operator/practices/{p.id}?msg=user_updated")


@router.post("/practices/{practice_id}/users/{user_id}/reset-mfa")
def reset_mfa(practice_id: int, user_id: int, ctx: Ctx = Depends(require_post("operate"))):
    p = _practice(ctx, practice_id)
    services.reset_user_mfa(ctx.db, _user(ctx, p, user_id), ctx.user, ctx.ip)
    ctx.db.commit()
    return redirect(f"/operator/practices/{p.id}?msg=mfa_reset")


@router.post("/requests/{request_id}")
def handle_request(request_id: int, status: str = Form(...), reply: str = Form(""),
                   ctx: Ctx = Depends(require_post("operate"))):
    r = ctx.db.get(AccessRequest, request_id)
    if not r:
        raise HTTPException(404, "Not found.")
    if status not in ("done", "declined"):
        raise HTTPException(400, "Unknown status.")
    r.status, r.reply, r.handled_by, r.handled_at = status, reply.strip()[:1000], ctx.user.id, utcnow()
    services.log_access_change(ctx.db, r.practice_id, ctx.user, ctx.ip, "access_request_" + status, request_id=r.id)
    ctx.db.commit()
    return redirect(f"/operator/practices/{r.practice_id}?msg=request_handled")
