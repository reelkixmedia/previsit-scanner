"""Operator console, Managed vs Self-managed access, BAA gate, Microsoft/Google sign-in."""
import re
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest

from app import sso
from app.db import SessionLocal
from app.models import AuditEvent, User
from app.services import create_operator
from test_platform import PASSWORD, csrf, fake_csvs, new_client, signup, totp_now, upload, _n


def operator_client():
    email = f"op{next(_n)}@previsit.test"
    with SessionLocal() as db:
        create_operator(db, "Demo Operator", email, PASSWORD)
        db.commit()
    c = new_client()
    page = c.get("/login")
    r = c.post("/login", data={"csrf_token": csrf(page.text), "email": email, "password": PASSWORD})
    assert "Set up two-step sign-in" in r.text  # operators always need MFA
    _, code = totp_now(r.text)
    r = c.post("/mfa/setup", data={"csrf_token": csrf(r.text), "code": code})
    assert "Operator console" in r.text
    return c


def practice_id_of(email):
    with SessionLocal() as db:
        return db.query(User).filter_by(email=email).one().practice_id


def test_baa_required_before_upload(tmp_path):
    c = new_client()
    signup(c, sign_baa=False)
    dash = c.get("/")
    assert "business associate agreement" in dash.text
    r = upload(c, dash.text, fake_csvs(tmp_path))
    assert r.url.path == "/baa"  # bounced to the agreement
    page = c.get("/baa")
    bad = c.post("/baa", data={"csrf_token": csrf(page.text), "full_name": "Olive", "title": "Owner"})  # no tick
    assert bad.status_code == 400
    ok = c.post("/baa", data={"csrf_token": csrf(page.text), "full_name": "Olive Owner", "title": "Owner",
                              "agree": "1"})
    assert "Agreement signed" in ok.text


def test_managed_plan_request_flow():
    owner = new_client()
    owner_email, _ = signup(owner, "Managed Clinic", plan="managed")
    pid = practice_id_of(owner_email)
    users = owner.get("/users")
    assert "Managed access" in users.text and "Create invite" not in users.text
    # Direct changes are blocked on the managed plan.
    blocked = owner.post("/users/invite", data={"csrf_token": csrf(users.text), "name": "X",
                                                "email": "x@clinic.test", "role": "staff"})
    assert blocked.status_code == 403
    new_email = f"newstaff{next(_n)}@clinic.test"
    r = owner.post("/users/request", data={"csrf_token": csrf(users.text), "kind": "add", "target_email": new_email,
                                           "target_name": "Nia Newstaff", "role": "staff", "note": "Starts Monday"})
    assert "Request sent" in r.text and "Waiting" in r.text

    op = operator_client()
    console = op.get("/operator")
    assert new_email in console.text and "Managed Clinic" in console.text
    page = op.get(f"/operator/practices/{pid}")
    r = op.post(f"/operator/practices/{pid}/invite", data={"csrf_token": csrf(page.text), "name": "Nia Newstaff",
                                                           "email": new_email, "role": "staff"})
    assert "/invite/" in r.text
    req_id = re.search(r'action="/operator/requests/(\d+)"', r.text).group(1)
    r = op.post(f"/operator/requests/{req_id}", data={"csrf_token": csrf(r.text), "status": "done",
                                                      "reply": "Invite sent to Nia"})
    assert "Request updated" in r.text

    users = owner.get("/users")
    assert "Done" in users.text and "Invite sent to Nia" in users.text and new_email in users.text
    sec = owner.get("/security")
    assert "Previsit support" in sec.text  # the practice sees who changed its access
    with SessionLocal() as db:
        assert db.query(AuditEvent).filter_by(practice_id=pid, action="user_invited").count() == 1


def test_operator_cannot_see_patient_data(tmp_path):
    owner = new_client()
    signup(owner, "Private Clinic")
    r = upload(owner, owner.get("/").text, fake_csvs(tmp_path))
    scan_id = re.search(r"/scans/(\d+)/calllist", r.text).group(1)
    op = operator_client()
    assert op.get("/", follow_redirects=False).headers["location"] == "/operator"
    assert op.get(f"/scans/{scan_id}", follow_redirects=False).headers["location"] == "/operator"
    assert op.get(f"/scans/{scan_id}/calllist.xlsx", follow_redirects=False).status_code == 303
    assert op.get("/security", follow_redirects=False).headers["location"] == "/operator"
    # Practice users can't reach the operator console.
    assert owner.get("/operator").status_code == 403


def test_self_managed_cannot_send_requests():
    c = new_client()
    signup(c, plan="self")
    page = c.get("/users")
    assert "Create invite" in page.text
    r = c.post("/users/request", data={"csrf_token": csrf(page.text), "kind": "add", "target_email": "a@b.test"})
    assert r.status_code == 400


def test_operator_switches_plan():
    owner = new_client()
    email, _ = signup(owner, plan="self")
    pid = practice_id_of(email)
    op = operator_client()
    page = op.get(f"/operator/practices/{pid}")
    op.post(f"/operator/practices/{pid}/plan", data={"csrf_token": csrf(page.text), "iam_plan": "managed"})
    assert "Managed access" in owner.get("/users").text


# ---------------- Microsoft / Google sign-in ----------------

@pytest.fixture
def fake_google(monkeypatch):
    monkeypatch.setenv("PREVISIT_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("PREVISIT_GOOGLE_CLIENT_SECRET", "test-secret")
    claims = {}
    monkeypatch.setattr(sso, "exchange_code", lambda *a: {"id_token": "fake"})
    monkeypatch.setattr(sso, "verify_id_token", lambda provider, token, nonce: claims)
    return claims


def sso_round_trip(client):
    r = client.get("/sso/google/start", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["nonce"]
    return client.get(f"/sso/google/callback?code=abc&state={q['state'][0]}", follow_redirects=False)


def allow_google(client):
    page = client.get("/settings")
    form = {"csrf_token": csrf(page.text), "name": "Test Clinic", "lookahead_days": 14, "retention_days": 30,
            "require_mfa": "1", "sso.google": "1", "timezone": "America/New_York", "reminder_days": "14,7,3"}
    client.post("/settings", data=form)


def test_sso_sign_in_still_needs_mfa(fake_google):
    owner = new_client()
    email, secret = signup(owner)
    assert "Sign in with Google" in new_client().get("/login").text
    fake_google.update(email=email, email_verified=True, sub="g-123")
    c = new_client()
    assert sso_round_trip(c).headers["location"] == "/login?msg=sso_failed"  # practice hasn't allowed Google yet
    allow_google(owner)
    r = sso_round_trip(c)
    assert r.headers["location"] == "/"
    assert c.get("/", follow_redirects=False).headers["location"] == "/mfa/verify"
    # Same email but a different Google account than the one first linked: refused.
    fake_google.update(sub="g-999")
    assert sso_round_trip(new_client()).headers["location"] == "/login?msg=sso_failed"


def test_sso_rejects_unknown_email_and_reused_state(fake_google):
    fake_google.update(email="nobody@nowhere.test", email_verified=True, sub="x")
    c = new_client()
    assert sso_round_trip(c).headers["location"] == "/login?msg=sso_no_account"
    r = c.get("/sso/google/start", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    c.get(f"/sso/google/callback?code=a&state={state}", follow_redirects=False)
    again = c.get(f"/sso/google/callback?code=a&state={state}", follow_redirects=False)
    assert again.headers["location"] == "/login?msg=sso_failed"  # state works once


def test_sso_off_without_credentials():
    assert new_client().get("/sso/google/start").status_code == 404
