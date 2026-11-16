"""Sign in with Microsoft or Google (OpenID Connect, authorization code + PKCE).

Turned on per provider by setting PREVISIT_<PROVIDER>_CLIENT_ID and _CLIENT_SECRET (free app registrations in
Microsoft Entra / Google Cloud). Each practice also chooses which providers it allows (Settings).
SSO replaces the password step only; our own two-step code is still required after it.
"""
import base64
import hashlib
import os
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select

from . import audit
from .auth import client_ip, new_token, set_session_cookie, sha, start_session
from .db import get_db
from .models import Practice, SsoState, User, utcnow
from .web import redirect

router = APIRouter(prefix="/sso")

PROVIDERS = {
    "microsoft": {
        "label": "Microsoft",
        "authorize": "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
        "jwks": "https://login.microsoftonline.com/common/discovery/v2.0/keys",
    },
    "google": {
        "label": "Google",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "jwks": "https://www.googleapis.com/oauth2/v3/certs",
    },
}
STATE_MINUTES = 10


def credentials(provider: str) -> tuple[str, str] | None:
    cid = os.getenv(f"PREVISIT_{provider.upper()}_CLIENT_ID", "")
    secret = os.getenv(f"PREVISIT_{provider.upper()}_CLIENT_SECRET", "")
    return (cid, secret) if cid and secret else None


def configured() -> list[str]:
    return [p for p in PROVIDERS if credentials(p)]


def _redirect_uri(request: Request, provider: str) -> str:
    base = os.getenv("PREVISIT_BASE_URL") or str(request.base_url).rstrip("/")
    return f"{base}/sso/{provider}/callback"


def _provider(provider: str) -> tuple[str, str]:
    creds = credentials(provider) if provider in PROVIDERS else None
    if not creds:
        raise HTTPException(404, "That sign-in option isn't set up.")
    return creds


@router.get("/{provider}/start")
def start(request: Request, provider: str, db=Depends(get_db)):
    client_id, _ = _provider(provider)
    db.execute(delete(SsoState).where(SsoState.created_at < utcnow() - timedelta(minutes=STATE_MINUTES)))
    state, nonce, verifier = new_token(), new_token(), secrets.token_urlsafe(64)
    db.add(SsoState(state_hash=sha(state), provider=provider, nonce=nonce, code_verifier=verifier))
    db.commit()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": client_id, "response_type": "code", "redirect_uri": _redirect_uri(request, provider),
              "scope": "openid email profile", "state": state, "nonce": nonce, "code_challenge": challenge,
              "code_challenge_method": "S256", "prompt": "select_account"}
    return redirect(PROVIDERS[provider]["authorize"] + "?" + urlencode(params))


def exchange_code(provider: str, code: str, verifier: str, redirect_uri: str) -> dict:
    client_id, secret = _provider(provider)
    r = httpx.post(PROVIDERS[provider]["token"], timeout=15, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": client_id, "client_secret": secret, "code_verifier": verifier})
    r.raise_for_status()
    return r.json()


def verify_id_token(provider: str, id_token: str, nonce: str) -> dict:
    """Checks signature (provider's published keys), audience, expiry, issuer and nonce."""
    client_id, _ = _provider(provider)
    key = jwt.PyJWKClient(PROVIDERS[provider]["jwks"]).get_signing_key_from_jwt(id_token).key
    claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=client_id,
                        options={"require": ["exp", "iat", "iss", "sub", "aud"]})
    if provider == "google":
        ok_iss = claims["iss"] in ("https://accounts.google.com", "accounts.google.com")
    else:
        ok_iss = claims["iss"] == f"https://login.microsoftonline.com/{claims.get('tid', '')}/v2.0"
    if not ok_iss or claims.get("nonce") != nonce:
        raise jwt.InvalidTokenError("issuer or nonce mismatch")
    return claims


def identity(provider: str, claims: dict) -> tuple[str, str]:
    """(email, stable subject id). Google must say the email is verified."""
    if provider == "google":
        if not claims.get("email_verified"):
            raise jwt.InvalidTokenError("email not verified")
        return claims["email"].lower(), claims["sub"]
    email = (claims.get("email") or claims.get("preferred_username") or "").lower()
    return email, f"{claims.get('tid')}:{claims.get('oid') or claims['sub']}"


@router.get("/{provider}/callback")
def callback(request: Request, provider: str, code: str = "", state: str = "", db=Depends(get_db)):
    _provider(provider)
    ip = client_ip(request)
    st = db.scalar(select(SsoState).where(SsoState.state_hash == sha(state or "-"), SsoState.provider == provider))
    if st:
        db.delete(st)  # single use
        db.commit()
    if not st or not code or st.created_at < utcnow() - timedelta(minutes=STATE_MINUTES):
        return redirect("/login?msg=sso_failed")
    try:
        tokens = exchange_code(provider, code, st.code_verifier, _redirect_uri(request, provider))
        email, subject = identity(provider, verify_id_token(provider, tokens["id_token"], st.nonce))
    except (httpx.HTTPError, jwt.PyJWTError, KeyError) as e:
        audit.log(db, "sso_failed", ip=ip, provider=provider, reason=type(e).__name__)
        db.commit()
        return redirect("/login?msg=sso_failed")

    user = db.scalar(select(User).where(User.email == email))
    if not user or not user.active:
        audit.log(db, "sso_no_account", ip=ip, provider=provider, email_hash=sha(email)[:16])
        db.commit()
        return redirect("/login?msg=sso_no_account")
    practice = db.get(Practice, user.practice_id)
    reason = None
    if provider not in practice.sso_providers.split(","):
        reason = "provider_not_allowed"
    elif user.locked_until and user.locked_until > utcnow():
        reason = "locked"
    elif user.sso_subject and (user.sso_provider, user.sso_subject) != (provider, subject):
        reason = "different_account"  # email matches but it's not the account first linked
    if reason:
        audit.log(db, "sso_refused", practice_id=user.practice_id, user_id=user.id, ip=ip, provider=provider,
                  reason=reason)
        db.commit()
        return redirect("/login?msg=sso_failed")

    if not user.sso_subject:
        user.sso_provider, user.sso_subject = provider, subject
    if user.invite_token_hash:  # signing in with SSO accepts the invite
        user.invite_token_hash = user.invite_expires_at = None
    user.failed_logins, user.last_login_at = 0, utcnow()
    token = start_session(db, request, user, mfa_passed=False)
    audit.log(db, "login_sso_ok", practice_id=user.practice_id, user_id=user.id, ip=ip, provider=provider)
    db.commit()
    resp = redirect("/")
    set_session_cookie(resp, token)
    return resp
