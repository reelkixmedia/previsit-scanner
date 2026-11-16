"""Template rendering and small response helpers shared by the route modules."""
from datetime import timezone
from zoneinfo import ZoneInfo

from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config
from .auth import PRE_COOKIE, ROLE_LABELS, new_token
from .importer import KIND_LABELS
from .scanner import RX_LABELS

templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))
templates.env.globals.update(ROLE_LABELS=ROLE_LABELS, KIND_LABELS=KIND_LABELS, RX_LABELS=RX_LABELS,
                             DRY_RUN=config.DRY_RUN, IDLE_MINUTES=config.SESSION_IDLE_MINUTES)


def local_time(value, practice, fmt: str = "%b %d, %H:%M") -> str:
    """Stored times are UTC; staff should see their own clock."""
    return value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(practice.timezone)).strftime(fmt)


templates.env.filters["lt"] = local_time

# Only fixed messages can be shown from a ?msg= code, so links can't inject text into the page.
MESSAGES = {
    "signed_out_idle": f"You were signed out after {config.SESSION_IDLE_MINUTES} minutes without activity.",
    "signed_out": "You're signed out.",
    "mfa_on": "Two-step sign-in is on.",
    "password_changed": "Password changed. Other devices were signed out.",
    "saved": "Saved.",
    "rules_reviewed": "Rules marked as reviewed.",
    "user_updated": "User updated.",
    "mfa_reset": "Two-step sign-in was reset. They'll set it up again at their next sign-in.",
    "request_sent": "Request sent to Previsit support. You'll see its status below.",
    "baa_signed": "Agreement signed. You can upload and scan now.",
    "plan_changed": "Access plan changed.",
    "request_handled": "Request updated.",
    "sso_failed": "That Microsoft or Google sign-in didn't work. Try again or use your password.",
    "sso_no_account": "No Previsit account is set up for that email. Ask your practice admin for an invite.",
}


def render(request, name: str, ctx=None, status: int = 200, **kw):
    msg = kw.pop("msg", None) or MESSAGES.get(request.query_params.get("msg", ""))
    return templates.TemplateResponse(request, name, {"ctx": ctx, "msg": msg, **kw}, status_code=status)


def render_pre(request, name: str, status: int = 200, **kw):
    """Pages shown before sign-in carry a double-submit CSRF token (cookie + hidden field)."""
    token = request.cookies.get(PRE_COOKIE) or new_token()
    resp = render(request, name, pre_csrf=token, status=status, **kw)
    resp.set_cookie(PRE_COOKIE, token, httponly=True, samesite="strict", secure=config.SECURE_COOKIES, path="/")
    return resp


def redirect(url: str):
    return RedirectResponse(url, status_code=303)
