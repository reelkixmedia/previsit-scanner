"""Business associate agreement: the owner signs electronically before any patient data can be uploaded."""
import hashlib

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from . import config
from .auth import Ctx, require, require_post
from .models import utcnow
from .web import redirect, render

router = APIRouter()

BAA_VERSION = "v0-draft"
BAA_TEXT = (config.BASE_DIR / "app" / "legal" / f"baa_{BAA_VERSION}.md").read_text(encoding="utf-8")
BAA_SHA256 = hashlib.sha256(BAA_TEXT.encode()).hexdigest()


@router.get("/baa")
def baa_page(request: Request, ctx: Ctx = Depends(require("view"))):
    return render(request, "baa.html", ctx, text=BAA_TEXT, version=BAA_VERSION, error=None)


@router.post("/baa")
def baa_sign(request: Request, full_name: str = Form(...), title: str = Form(...), agree: str = Form(""),
             ctx: Ctx = Depends(require_post("view"))):
    if ctx.user.role != "owner":
        raise HTTPException(403, "Only the practice owner can sign the agreement.")
    if ctx.practice.baa_signed_at:
        return redirect("/baa")
    if not full_name.strip() or not title.strip() or agree != "1":
        return render(request, "baa.html", ctx, status=400, text=BAA_TEXT, version=BAA_VERSION,
                      error="Type your full name and title, and tick the box to agree.")
    p = ctx.practice
    p.baa_version, p.baa_text_sha256 = BAA_VERSION, BAA_SHA256
    p.baa_signed_name, p.baa_signed_title = full_name.strip()[:200], title.strip()[:200]
    p.baa_signed_by, p.baa_signed_at, p.baa_signed_ip = ctx.user.id, utcnow(), ctx.ip
    ctx.audit("baa_signed", version=BAA_VERSION, text_sha256=BAA_SHA256)
    ctx.db.commit()
    return redirect("/?msg=baa_signed")
