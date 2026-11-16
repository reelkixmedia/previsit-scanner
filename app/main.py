"""App entry point + sign-in routes (sign-up, login, MFA, invites, logout, account)."""
from datetime import timedelta

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import admin, audit, automation, config, crypto, legal, operator_console, pages, sso
from .auth import (SESSION_COOKIE, AuthRedirect, Ctx, check_pre_csrf, check_totp, client_ip, end_user_sessions,
                   hash_password, load_ctx, new_totp_secret, password_problem, require, require_post, set_session_cookie,
                   sha, start_session, totp_uri, verify_password)
from .db import get_db, init_db
from .models import AuthSession, Practice, User, utcnow
from .services import IAM_PLANS, create_practice
from .web import redirect, render, render_pre

app = FastAPI(title="Previsit", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")
init_db()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                               "script-src 'self'; frame-ancestors 'none'; form-action 'self'; "
                                               "base-uri 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if not request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store"  # patient data must not sit in browser caches
    if config.SECURE_COOKIES:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


@app.exception_handler(AuthRedirect)
async def _auth_redirect(request: Request, exc: AuthRedirect):
    return redirect(exc.url)


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    titles = {403: "Not allowed", 404: "Not found"}
    return render(request, "error.html", status=exc.status_code,
                  title=titles.get(exc.status_code, "Something went wrong"), detail=exc.detail)


for _r in (pages.router, admin.router, legal.router, operator_console.router, sso.router, automation.router):
    app.include_router(_r)
automation.start_scheduler()


# ---------- sign-up ----------

@app.get("/signup")
def signup_page(request: Request):
    return render_pre(request, "signup.html", PLANS=IAM_PLANS, form={"iam_plan": "managed"})


@app.post("/signup", dependencies=[Depends(check_pre_csrf)])
def signup(request: Request, practice_name: str = Form(...), name: str = Form(...), email: str = Form(...),
           password: str = Form(...), password2: str = Form(...), iam_plan: str = Form("managed"),
           db=Depends(get_db)):
    email = email.strip().lower()
    error = password_problem(password)
    if iam_plan not in IAM_PLANS:
        error = "Pick how you want user access handled."
    if password != password2:
        error = "The two passwords don't match."
    if not practice_name.strip() or not name.strip() or "@" not in email:
        error = "Fill in every field."
    if not error and db.scalar(select(User).where(User.email == email)):
        error = "That email already has an account. Sign in instead."
    if error:
        return render_pre(request, "signup.html", status=400, error=error, PLANS=IAM_PLANS,
                          form={"practice_name": practice_name, "name": name, "email": email, "iam_plan": iam_plan})
    owner = create_practice(db, practice_name.strip(), name.strip(), email, password, ip=client_ip(request),
                            iam_plan=iam_plan)
    token = start_session(db, request, owner, mfa_passed=False)
    db.commit()
    resp = redirect("/mfa/setup")
    set_session_cookie(resp, token)
    return resp


# ---------- login ----------

@app.get("/login")
def login_page(request: Request, db=Depends(get_db)):
    if load_ctx(request, db):
        return redirect("/")
    return render_pre(request, "login.html", sso=sso.configured(), SSO=sso.PROVIDERS)


@app.post("/login", dependencies=[Depends(check_pre_csrf)])
def login(request: Request, email: str = Form(...), password: str = Form(...), db=Depends(get_db)):
    email = email.strip().lower()
    ip = client_ip(request)
    user = db.scalar(select(User).where(User.email == email))
    now = utcnow()
    generic = "That email and password don't match."
    if user and user.locked_until and user.locked_until > now:
        verify_password(None, password)  # same work either way
        audit.log(db, "login_blocked_locked", practice_id=user.practice_id, user_id=user.id, ip=ip)
        db.commit()
        return render_pre(request, "login.html", status=400, email=email, sso=sso.configured(), SSO=sso.PROVIDERS,
                          error=f"Too many tries. This account is locked for {config.LOCKOUT_MINUTES} minutes, "
                                f"or ask your admin.")
    ok = verify_password(user.password_hash if user else None, password)
    if not ok or not user or not user.active:
        if user:
            user.failed_logins += 1
            audit.log(db, "login_failed", practice_id=user.practice_id, user_id=user.id, ip=ip,
                      attempt=user.failed_logins)
            if user.failed_logins >= config.LOCKOUT_THRESHOLD:
                user.locked_until = now + timedelta(minutes=config.LOCKOUT_MINUTES)
                user.failed_logins = 0
                audit.log(db, "account_locked", practice_id=user.practice_id, user_id=user.id, ip=ip,
                          minutes=config.LOCKOUT_MINUTES)
        else:
            audit.log(db, "login_failed_unknown_email", ip=ip, email_hash=sha(email)[:16])
        db.commit()
        return render_pre(request, "login.html", status=400, email=email, error=generic, sso=sso.configured(),
                          SSO=sso.PROVIDERS)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    token = start_session(db, request, user, mfa_passed=False)
    audit.log(db, "login_password_ok", practice_id=user.practice_id, user_id=user.id, ip=ip)
    db.commit()
    resp = redirect("/mfa/verify" if user.mfa_enabled else "/")
    set_session_cookie(resp, token)
    return resp


@app.get("/logout")
def logout(request: Request, idle: int = 0, db=Depends(get_db)):
    ctx = load_ctx(request, db)
    if ctx:
        ctx.audit("logout", reason="idle" if idle else "user")
        db.delete(ctx.sess)
        db.commit()
    resp = redirect("/login?msg=" + ("signed_out_idle" if idle else "signed_out"))
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ---------- MFA ----------

@app.get("/mfa/setup")
def mfa_setup_page(request: Request, ctx: Ctx = Depends(require(allow_mfa_pending=True))):
    if ctx.user.mfa_enabled:
        # Never let a password-only session replace an existing authenticator.
        return redirect("/" if ctx.sess.mfa_passed else "/mfa/verify")
    if not ctx.user.totp_secret_enc:
        ctx.user.totp_secret_enc = crypto.encrypt_text(ctx.practice, new_totp_secret())
        ctx.db.commit()
    secret = crypto.decrypt_text(ctx.practice, ctx.user.totp_secret_enc)
    return render(request, "mfa_setup.html", ctx, secret=secret, qr_svg=_qr_svg(totp_uri(secret, ctx.user.email)),
                  error=None)


@app.post("/mfa/setup")
def mfa_setup(request: Request, code: str = Form(...), ctx: Ctx = Depends(require_post(allow_mfa_pending=True))):
    if ctx.user.mfa_enabled:
        return redirect("/")
    if not check_totp(ctx.db, ctx.user, ctx.practice, code):
        secret = crypto.decrypt_text(ctx.practice, ctx.user.totp_secret_enc)
        return render(request, "mfa_setup.html", ctx, status=400, secret=secret,
                      qr_svg=_qr_svg(totp_uri(secret, ctx.user.email)),
                      error="That code didn't match. Check the time on your phone and try the newest code.")
    ctx.user.mfa_enabled = True
    ctx.sess.mfa_passed = True
    ctx.audit("mfa_enabled")
    ctx.db.commit()
    return redirect("/?msg=mfa_on")


@app.get("/mfa/verify")
def mfa_verify_page(request: Request, ctx: Ctx = Depends(require(allow_mfa_pending=True))):
    if ctx.sess.mfa_passed or not ctx.user.mfa_enabled:
        return redirect("/")
    return render(request, "mfa_verify.html", ctx, error=None)


@app.post("/mfa/verify")
def mfa_verify(request: Request, code: str = Form(...), ctx: Ctx = Depends(require_post(allow_mfa_pending=True))):
    if check_totp(ctx.db, ctx.user, ctx.practice, code):
        ctx.sess.mfa_passed = True
        ctx.user.failed_logins = 0
        ctx.audit("login_success")
        ctx.db.commit()
        return redirect("/")
    ctx.user.failed_logins += 1
    ctx.audit("mfa_failed", attempt=ctx.user.failed_logins)
    if ctx.user.failed_logins >= config.LOCKOUT_THRESHOLD:
        ctx.user.locked_until = utcnow() + timedelta(minutes=config.LOCKOUT_MINUTES)
        ctx.user.failed_logins = 0
        ctx.audit("account_locked", minutes=config.LOCKOUT_MINUTES)
        end_user_sessions(ctx.db, ctx.user.id)
        ctx.db.commit()
        return redirect("/login")
    ctx.db.commit()
    return render(request, "mfa_verify.html", ctx, status=400, error="That code didn't match. Try the newest one.")


def _qr_svg(uri: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    return img.to_string(encoding="unicode")


# ---------- invites ----------

def _invited_user(db, token: str) -> User:
    user = db.scalar(select(User).where(User.invite_token_hash == sha(token)))
    if not user or not user.invite_expires_at or user.invite_expires_at < utcnow() or not user.active:
        raise HTTPException(404, "This invite link has expired or was already used. Ask your admin for a new one.")
    return user


@app.get("/invite/{token}")
def invite_page(request: Request, token: str, db=Depends(get_db)):
    user = _invited_user(db, token)
    return render_pre(request, "invite.html", invitee=user, practice=db.get(Practice, user.practice_id), token=token)


@app.post("/invite/{token}", dependencies=[Depends(check_pre_csrf)])
def invite_accept(request: Request, token: str, password: str = Form(...), password2: str = Form(...),
                  db=Depends(get_db)):
    user = _invited_user(db, token)
    error = "The two passwords don't match." if password != password2 else password_problem(password)
    if error:
        return render_pre(request, "invite.html", status=400, invitee=user,
                          practice=db.get(Practice, user.practice_id), token=token, error=error)
    user.password_hash = hash_password(password)
    user.invite_token_hash = None
    user.invite_expires_at = None
    user.last_login_at = utcnow()
    audit.log(db, "invite_accepted", practice_id=user.practice_id, user_id=user.id, ip=client_ip(request))
    session_token = start_session(db, request, user, mfa_passed=False)
    db.commit()
    resp = redirect("/")
    set_session_cookie(resp, session_token)
    return resp


# ---------- account ----------

@app.get("/account")
def account_page(request: Request, ctx: Ctx = Depends(require())):
    return render(request, "account.html", ctx, error=None)


@app.post("/account/password")
def change_password(request: Request, current: str = Form(...), password: str = Form(...),
                    password2: str = Form(...), ctx: Ctx = Depends(require_post())):
    error = None
    if not verify_password(ctx.user.password_hash, current):
        error = "Your current password isn't right."
    elif password != password2:
        error = "The two new passwords don't match."
    else:
        error = password_problem(password)
    if error:
        return render(request, "account.html", ctx, status=400, error=error)
    ctx.user.password_hash = hash_password(password)
    ctx.db.query(AuthSession).filter(AuthSession.user_id == ctx.user.id, AuthSession.id != ctx.sess.id).delete()
    ctx.audit("password_changed")
    ctx.db.commit()
    return redirect("/account?msg=password_changed")


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/favicon.svg")
