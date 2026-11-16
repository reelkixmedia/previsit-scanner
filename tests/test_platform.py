import itertools
import re
import time
from datetime import date, timedelta
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from app import config, importer
from app.audit import verify_chain
from app.db import SessionLocal
from app.main import app
from app.models import AuditEvent, AuthSession, Dataset, User
from app.scanner import DEFAULT_LAB_RULES, DEFAULT_RX_DAYS, keywords, run_scan

_n = itertools.count()
PASSWORD = "correct horse battery 9"
TODAY = date.today()


def csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def totp_now(html: str) -> str:
    secret = re.search(r'<code class="secret">([A-Z2-7]+)</code>', html).group(1)
    return secret, pyotp.TOTP(secret).now()


def signup(client: TestClient, practice="Test Clinic", plan="self", sign_baa=True):
    email = f"owner{next(_n)}@clinic.test"
    page = client.get("/signup")
    r = client.post("/signup", data={"csrf_token": csrf(page.text), "practice_name": practice, "name": "Olive Owner",
                                     "email": email, "password": PASSWORD, "password2": PASSWORD, "iam_plan": plan})
    assert r.status_code == 200 and "Set up two-step sign-in" in r.text  # followed redirect
    secret, code = totp_now(r.text)
    r = client.post("/mfa/setup", data={"csrf_token": csrf(r.text), "code": code})
    assert "Pre-visit scan" in r.text
    if sign_baa:
        page = client.get("/baa")
        r = client.post("/baa", data={"csrf_token": csrf(page.text), "full_name": "Olive Owner",
                                      "title": "Practice Manager", "agree": "1"})
        assert "Agreement signed" in r.text
    return email, secret


def new_client():
    return TestClient(app)


def fake_csvs(tmp: Path):
    from sample_data.make_fake_data import main
    main(tmp, TODAY)
    stamp = TODAY.isoformat()
    return {k: (tmp / f"fake_{k}_{stamp}.csv").read_bytes() for k in ("appointments", "medications", "labs")}


def upload(client, html, files, review=False):
    data = {"csrf_token": csrf(html)}
    if review:
        data["review"] = "1"
    return client.post("/upload", data=data,
                       files={k: (f"{k}.csv", v, "text/csv") for k, v in files.items()})


# ---------------- scanner rules ----------------

def _rules():
    return [{"name": n, "drug_keywords": keywords(d), "lab_keywords": keywords(l), "max_age_days": days}
            for n, d, l, days in DEFAULT_LAB_RULES]


def test_scanner_flags():
    t = date(2026, 11, 1)
    iso = lambda days: (t + timedelta(days=days)).isoformat()  # noqa: E731
    data = {
        "appointments": [
            {"patient_id": "1", "patient_name": "A", "appt_date": iso(7)},
            {"patient_id": "2", "patient_name": "B", "appt_date": iso(10)},
            {"patient_id": "3", "patient_name": "C", "appt_date": iso(30)},  # outside window
            {"patient_id": "4", "patient_name": "D", "appt_date": iso(3)},
        ],
        "medications": [
            {"patient_id": "1", "drug_name": "Metformin 500", "last_prescribed": iso(-400), "schedule": "non-controlled"},
            {"patient_id": "2", "drug_name": "Atorvastatin", "last_prescribed": iso(-360), "schedule": "non-controlled"},
            {"patient_id": "3", "drug_name": "Warfarin", "last_prescribed": iso(-500), "schedule": "non-controlled"},
            {"patient_id": "4", "drug_name": "Sertraline", "last_prescribed": iso(-10), "schedule": "non-controlled",
             "refills_remaining": 0},
            {"patient_id": "4", "drug_name": "Levothyroxine", "last_prescribed": iso(-10), "schedule": "non-controlled"},
        ],
        "labs": [
            {"patient_id": "2", "test_name": "Lipid Panel", "result_date": iso(-100)},
            {"patient_id": "4", "test_name": "TSH", "result_date": iso(-30)},
        ],
    }
    out = run_scan(data, DEFAULT_RX_DAYS, _rules(), t, 14)
    by = {r["patient_id"]: {(i["type"], i["status"]) for i in r["issues"]} for r in out["results"]}
    assert by["1"] == {("rx", "expired"), ("lab", "missing")}
    assert by["2"] == {("rx", "expires_before_visit")}  # lipid panel is recent enough
    assert "3" not in by  # visit outside the 14-day window
    assert by["4"] == {("rx", "no_refills")}  # TSH recent
    assert out["summary"]["patients_in_window"] == 3


def test_stale_lab_and_keyword_boundaries():
    t = date(2026, 11, 1)
    data = {"appointments": [{"patient_id": "9", "patient_name": "Z", "appt_date": "2026-11-05"}],
            "medications": [{"patient_id": "9", "drug_name": "Insulin glargine", "last_prescribed": "2026-10-01"}],
            "labs": [{"patient_id": "9", "test_name": "Hemoglobin A1c", "result_date": "2026-01-02"},
                     {"patient_id": "9", "test_name": "Insulin level", "result_date": "2026-10-30"}]}
    issues = run_scan(data, DEFAULT_RX_DAYS, _rules(), t, 14)["results"][0]["issues"]
    assert [i["status"] for i in issues] == ["stale"]


def test_mapping_guess_and_dates():
    headers = ["MRN", "Patient Name", "Appt Date", "Rendering Provider", "Email Address", "Cell Phone", "Visit Type"]
    m = importer.guess_mapping("appointments", headers)
    assert m == {"patient_id": "MRN", "patient_name": "Patient Name", "appt_date": "Appt Date",
                 "provider": "Rendering Provider", "email": "Email Address", "phone": "Cell Phone",
                 "visit_type": "Visit Type"}
    assert importer.parse_date("11/08/2026 2:15 PM") == date(2026, 11, 8)
    assert importer.parse_date("11/08/26") == date(2026, 11, 8)
    assert importer.normalize_schedule("CII") == "C-II"
    assert importer.normalize_schedule("Schedule IV") == "C-III-V"
    assert importer.normalize_schedule("") == "non-controlled"


# ---------------- full flow ----------------

def test_end_to_end_upload_scan_export_send(tmp_path):
    c = new_client()
    signup(c)
    dash = c.get("/")
    r = upload(c, dash.text, fake_csvs(tmp_path))
    assert r.status_code == 200 and "<h1>Worklist</h1>" in r.text, r.text[:500]
    scan_id = int(re.search(r"/scans/(\d+)/calllist.xlsx", r.text).group(1))
    assert "Expired or overdue now" in r.text

    x = c.get(f"/scans/{scan_id}/calllist.xlsx")
    assert x.status_code == 200 and x.content[:2] == b"PK"
    csv_text = c.get(f"/scans/{scan_id}/calllist.csv").content.decode("utf-8-sig")
    assert csv_text.startswith("Appointment,Patient")

    emails = c.get(f"/scans/{scan_id}/emails")
    assert "Run test send" in emails.text
    assert "Metformin" not in emails.text and "Oxycodone" not in emails.text  # never name meds
    r = c.post(f"/scans/{scan_id}/send", data={"csrf_token": csrf(emails.text)})
    assert "Test only" in r.text and "Sent<" not in r.text

    with SessionLocal() as db:
        ds = db.query(Dataset).order_by(Dataset.id.desc()).first()
        assert ds.raw_enc is None and b"MRN104" not in ds.data_enc  # raw dropped, data encrypted
        pid = ds.practice_id
        assert verify_chain(db, pid)[0]
    log = config.AUDIT_JSONL.read_text()
    assert "Testwell" not in log and "MRN104" not in log  # no patient identifiers in audit


def test_review_mapping_step(tmp_path):
    c = new_client()
    signup(c)
    files = fake_csvs(tmp_path)
    r = upload(c, c.get("/").text, files, review=True)
    assert "Match your columns" in r.text
    ds_id = int(re.search(r'action="/mapping/(\d+)"', r.text).group(1))
    form = {"csrf_token": csrf(r.text)}
    for kind, pick in (("appointments", {"patient_id": "MRN", "patient_name": "Patient Name", "appt_date": "Appt Date"}),
                       ("medications", {"patient_id": "Chart #", "drug_name": "Medication Name",
                                        "last_prescribed": "Date Written"}),
                       ("labs", {"patient_id": "MRN", "test_name": "Component"})):  # result date left out
        form.update({f"m.{kind}.{f}": h for f, h in pick.items()})
    bad = c.post(f"/mapping/{ds_id}", data=form)
    assert bad.status_code == 400 and "Result / collection date" in bad.text
    form["m.labs.result_date"] = "Collected"
    ok = c.post(f"/mapping/{ds_id}", data=form)
    assert "<h1>Worklist</h1>" in ok.text
    # Same headers next time: no matching step.
    again = upload(c, c.get("/").text, files)
    assert "<h1>Worklist</h1>" in again.text


# ---------------- security ----------------

def test_tenant_isolation(tmp_path):
    a = new_client()
    signup(a, "Clinic A")
    r = upload(a, a.get("/").text, fake_csvs(tmp_path))
    scan_id = int(re.search(r"/scans/(\d+)/calllist", r.text).group(1))
    b = new_client()
    signup(b, "Clinic B")
    for url in (f"/scans/{scan_id}", f"/scans/{scan_id}/calllist.xlsx", f"/scans/{scan_id}/emails"):
        assert b.get(url).status_code == 404


def test_requires_login_and_csrf():
    c = new_client()
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    signup(c)
    assert c.post("/settings", data={"csrf_token": "wrong", "lookahead_days": 14}).status_code == 403
    assert c.post("/login", data={"email": "x@y.z", "password": "nope"}).status_code in (403, 422)


def test_lockout_and_mfa_on_login():
    c = new_client()
    email, secret = signup(c)
    c.get("/logout")
    c2 = new_client()
    for _ in range(config.LOCKOUT_THRESHOLD):
        page = c2.get("/login")
        c2.post("/login", data={"csrf_token": csrf(page.text), "email": email, "password": "wrong password 123"})
    page = c2.get("/login")
    r = c2.post("/login", data={"csrf_token": csrf(page.text), "email": email, "password": PASSWORD})
    assert "locked" in r.text
    with SessionLocal() as db:
        u = db.query(User).filter_by(email=email).one()
        u.locked_until = None
        db.commit()
    page = c2.get("/login")
    r = c2.post("/login", data={"csrf_token": csrf(page.text), "email": email, "password": PASSWORD})
    assert "Enter your code" in r.text  # password alone isn't enough
    assert c2.get("/", follow_redirects=False).headers["location"] == "/mfa/verify"
    assert c2.get("/mfa/setup", follow_redirects=False).headers["location"] == "/mfa/verify"  # can't swap authenticator
    # The code used at sign-up can't be replayed; wait for a fresh one via the next step.
    replay = c2.post("/mfa/verify", data={"csrf_token": csrf(r.text), "code": pyotp.TOTP(secret).now()})
    assert replay.status_code == 400
    next_code = pyotp.TOTP(secret).at(time.time() + 30)
    r = c2.post("/mfa/verify", data={"csrf_token": csrf(r.text), "code": next_code})
    assert "Pre-visit scan" in r.text


def test_idle_timeout():
    c = new_client()
    signup(c)
    with SessionLocal() as db:
        for s in db.query(AuthSession).all():
            s.last_seen_at -= timedelta(minutes=config.SESSION_IDLE_MINUTES + 1)
        db.commit()
    r = c.get("/", follow_redirects=False)
    assert r.headers["location"] == "/login?msg=signed_out_idle"


def test_roles_and_invites():
    owner = new_client()
    signup(owner)
    users = owner.get("/users")
    r = owner.post("/users/invite", data={"csrf_token": csrf(users.text), "name": "Sam Staff",
                                          "email": f"staff{next(_n)}@clinic.test", "role": "readonly"})
    link = re.search(r'value="(http[^"]+/invite/[^"]+)"', r.text).group(1)
    path = link.split("testserver")[1]
    ro = new_client()
    page = ro.get(path)
    r = ro.post(path, data={"csrf_token": csrf(page.text), "password": PASSWORD, "password2": PASSWORD})
    assert "Set up two-step sign-in" in r.text
    _, code = totp_now(r.text)
    r = ro.post("/mfa/setup", data={"csrf_token": csrf(r.text), "code": code})
    assert "Upload &amp; scan" not in r.text  # read-only: no upload form
    assert ro.get("/users").status_code == 403
    assert ro.get("/settings").status_code == 403
    assert ro.post("/scan", data={"csrf_token": csrf(ro.get("/account").text), "dataset_id": 1}).status_code == 403
    assert ro.get(path).status_code == 404  # invite link is single-use


def test_audit_tamper_detected():
    c = new_client()
    signup(c)
    with SessionLocal() as db:
        ev = db.query(AuditEvent).filter(AuditEvent.practice_id.isnot(None)).order_by(AuditEvent.id.desc()).first()
        pid = ev.practice_id
        assert verify_chain(db, pid)[0]
        ev.action = "something_else"
        db.commit()
        ok, _, bad = verify_chain(db, pid)
        assert not ok and bad == ev.id


def test_security_headers():
    r = new_client().get("/login")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
