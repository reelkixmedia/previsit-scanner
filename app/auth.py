"""Identity and access: passwords (argon2), TOTP MFA, server-side sessions, roles, CSRF.

Algorithms come from vetted libraries (argon2-cffi, pyotp, secrets); the flows are ours.
"""
import hashlib
import hmac
import secrets
import time
from datetime import timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select

from . import config, crypto
from .db import get_db
from .models import AuthSession, Practice, User, utcnow

_ph = PasswordHasher()
_DUMMY_HASH = _ph.hash("timing-equaliser-not-a-password")

SESSION_COOKIE = "pv_session"
PRE_COOKIE = "pv_pre"  # CSRF token for forms shown before sign-in

ROLES = ["owner", "admin", "staff", "readonly"]
ROLE_LABELS = {"owner": "Owner", "admin": "Admin", "staff": "Staff", "readonly": "Read-only",
               "operator": "Previsit support"}
ROLE_PERMS = {
    "operator": {"operate"},  # platform staff: manage practices' access, never patient data
    "owner": {"view", "upload", "scan", "export", "send", "work", "manage_rules", "manage_users",
              "view_security", "manage_settings"},
    "admin": {"view", "upload", "scan", "export", "send", "work", "manage_rules", "manage_users", "view_security"},
    "staff": {"view", "upload", "scan", "export", "send", "work"},
    "readonly": {"view"},
}

_COMMON = {"password1234", "password12345", "123456789012", "qwertyuiop12", "letmein12345", "welcome12345"}


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    try:
        return _ph.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(password: str) -> str | None:
    if len(password) < config.MIN_PASSWORD_LEN:
        return f"Use at least {config.MIN_PASSWORD_LEN} characters."
    if len(set(password)) < 5 or password.lower() in _COMMON:
        return "That password is too easy to guess."
    return None


# ---- TOTP ----

def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Previsit")


def check_totp(db, user: User, practice: Practice, code: str) -> bool:
    """Accepts the current 30s code or one step either side; refuses a code that was already used."""
    if not user.totp_secret_enc:
        return False
    code = "".join(ch for ch in code if ch.isdigit())
    totp = pyotp.TOTP(crypto.decrypt_text(practice, user.totp_secret_enc))
    now_step = int(time.time() // 30)
    for step in (now_step - 1, now_step, now_step + 1):
        if step > user.totp_last_step and hmac.compare_digest(totp.at(step * 30), code):
            user.totp_last_step = step
            db.flush()
            return True
    return False


# ---- sessions ----

def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def start_session(db, request: Request, user: User, mfa_passed: bool) -> str:
    token = new_token()
    db.add(AuthSession(token_hash=sha(token), user_id=user.id, csrf_token=new_token(), mfa_passed=mfa_passed,
                       ip=client_ip(request), user_agent=request.headers.get("user-agent", "")[:300]))
    db.flush()
    return token


def set_session_cookie(response, token: str):
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=config.SECURE_COOKIES, path="/")


def end_user_sessions(db, user_id: int):
    db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))


class Ctx:
    def __init__(self, db, user: User, practice: Practice, sess: AuthSession, ip: str):
        self.db, self.user, self.practice, self.sess, self.ip = db, user, practice, sess, ip

    def can(self, perm: str) -> bool:
        return perm in ROLE_PERMS.get(self.user.role, set())

    @property
    def fully_signed_in(self) -> bool:
        """Password done, and MFA done or not required."""
        if self.user.mfa_enabled:
            return self.sess.mfa_passed
        return not self.practice.require_mfa

    @property
    def csrf(self) -> str:
        return self.sess.csrf_token

    def audit(self, action, **detail):
        from . import audit
        audit.log(self.db, action, practice_id=self.practice.id, user_id=self.user.id, ip=self.ip, **detail)


class AuthRedirect(Exception):
    def __init__(self, url: str):
        self.url = url


def load_ctx(request: Request, db) -> Ctx | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == sha(token)))
    if not sess:
        return None
    now = utcnow()
    user = db.get(User, sess.user_id)
    idle = sess.last_seen_at < now - timedelta(minutes=config.SESSION_IDLE_MINUTES)
    too_old = sess.created_at < now - timedelta(hours=config.SESSION_MAX_HOURS)
    if idle or too_old or not user or not user.active:
        db.delete(sess)
        db.commit()
        request.state.session_expired = True
        return None
    sess.last_seen_at = now
    db.commit()
    return Ctx(db, user, db.get(Practice, user.practice_id), sess, client_ip(request))


def _gate(request: Request, db, perm, allow_mfa_pending) -> Ctx:
    ctx = load_ctx(request, db)
    if not ctx:
        expired = getattr(request.state, "session_expired", False)
        raise AuthRedirect("/login?msg=signed_out_idle" if expired else "/login")
    if not allow_mfa_pending:
        if ctx.user.mfa_enabled and not ctx.sess.mfa_passed:
            raise AuthRedirect("/mfa/verify")
        if ctx.practice.require_mfa and not ctx.user.mfa_enabled:
            raise AuthRedirect("/mfa/setup")
    if ctx.practice.is_platform and perm and perm != "operate":
        raise AuthRedirect("/operator")  # operators have no practice pages of their own
    if perm and not ctx.can(perm):
        raise HTTPException(status_code=403, detail="Your role doesn't allow this.")
    if perm in ("upload", "scan") and not ctx.practice.baa_signed_at:
        raise AuthRedirect("/baa")  # no patient data until the BAA is signed
    return ctx


def require(perm: str | None = None, allow_mfa_pending: bool = False):
    """GET pages: signed in (+ MFA done) and role has `perm`."""
    def dep(request: Request, db=Depends(get_db)) -> Ctx:
        return _gate(request, db, perm, allow_mfa_pending)
    return dep


def require_post(perm: str | None = None, allow_mfa_pending: bool = False):
    """Form posts: same as require(), plus the per-session CSRF token must match."""
    async def dep(request: Request, db=Depends(get_db)) -> Ctx:
        ctx = _gate(request, db, perm, allow_mfa_pending)
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf_token", "")), ctx.csrf):
            raise HTTPException(status_code=403, detail="Form expired. Go back, reload the page and try again.")
        return ctx
    return dep


async def check_pre_csrf(request: Request):
    """Double-submit check for sign-in / sign-up / invite forms."""
    form = await request.form()
    cookie = request.cookies.get(PRE_COOKIE, "")
    if not cookie or not hmac.compare_digest(str(form.get("csrf_token", "")), cookie):
        raise HTTPException(status_code=403, detail="Form expired. Go back, reload the page and try again.")
