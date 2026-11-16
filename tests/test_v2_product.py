"""v2: scan accuracy, patient groups, worklist across scans, reminders, office requests, automation."""
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import automation, importer, outreach, pipeline, worklist
from app.db import SessionLocal
from app.models import PatientState, Practice, ReminderLog, User, WorkItem
from app.scanner import DEFAULT_LAB_RULES, DEFAULT_RX_DAYS, keywords, run_scan
from test_platform import csrf, new_client, signup

T = date(2026, 11, 1)
RULES = [{"name": n, "drug_keywords": keywords(d), "lab_keywords": keywords(l), "max_age_days": days}
         for n, d, l, days in DEFAULT_LAB_RULES]


def iso(days):
    return (T + timedelta(days=days)).isoformat()


def appt(pid="1", days=7, **kw):
    return {"patient_id": pid, "patient_name": f"Doe, Pat{pid}", "appt_date": iso(days), "provider": "Dr. A", **kw}


def med(pid="1", drug="Sertraline 50 mg", ago=10, **kw):
    return {"patient_id": pid, "drug_name": drug, "last_prescribed": iso(-ago), "schedule": "non-controlled", **kw}


def lab(pid="1", test="TSH", ago=10):
    return {"patient_id": pid, "test_name": test, "result_date": iso(-ago)}


def scan(appts, meds, labs=None):
    return run_scan({"appointments": appts, "medications": meds, "labs": labs if labs is not None else [lab("0")]},
                    DEFAULT_RX_DAYS, RULES, T, 14)


def statuses(out, pid="1"):
    return {(i["type"], i["status"]) for r in out["results"] if r["patient_id"] == pid for i in r["issues"]}


# ---------------- accuracy ----------------

def test_renewed_script_replaces_old_row():
    out = scan([appt()], [med(drug="Sertraline 50 mg", ago=400), med(drug="Sertraline HCl 100 mg tablet", ago=5)])
    assert out["clear"] and not out["results"]


def test_different_drugs_are_not_merged():
    out = scan([appt()], [med(drug="Insulin glargine", ago=400), med(drug="Insulin lispro", ago=5)], [lab(test="A1c")])
    assert ("rx", "expired") in statuses(out)


def test_schedule_two_not_flagged_for_zero_refills():
    assert scan([appt()], [med(drug="Oxycodone 5 mg", ago=10, schedule="C-II", refills_remaining=0)])["clear"]


def test_blank_schedule_inferred_from_drug_name():
    out = scan([appt()], [med(drug="Alprazolam 0.5 mg", ago=200)])  # 1-year window would miss this
    assert ("rx", "expired") in statuses(out)
    assert out["summary"]["checks"]["schedules_inferred"] == 1


def test_brand_names_trigger_lab_rules():
    out = scan([appt()], [med(drug="Lipitor 20 mg"), med(drug="Ozempic 0.5 mg")])
    assert {i["item"] for i in out["results"][0]["issues"]} == {"Lipid panel", "A1c"}


def test_no_labs_file_means_labs_not_checked_not_all_missing():
    out = scan([appt()], [med(drug="Metformin 500 mg")], labs=[])
    assert out["clear"] and out["summary"]["checks"]["no_labs_file"]


def test_no_meds_file_is_unchecked_not_clear():
    out = run_scan({"appointments": [appt()], "medications": [], "labs": []}, DEFAULT_RX_DAYS, RULES, T, 14)
    assert out["unchecked"] and not out["clear"] and out["summary"]["checks"]["no_meds_file"]


def test_id_mismatch_warning():
    out = scan([appt("A1")], [med("B2")])
    assert out["summary"]["checks"]["ids_dont_match"]


def test_cancelled_visits_and_consent_on_import():
    mapping = {"patient_id": "ID", "patient_name": "Name", "appt_date": "Date", "appt_status": "Status",
               "email_consent": "OK to email"}
    rows = [{"ID": "1", "Name": "A", "Date": "11/08/2026", "Status": "Scheduled", "OK to email": "No"},
            {"ID": "2", "Name": "B", "Date": "11/08/2026", "Status": "Cancelled", "OK to email": ""},
            {"ID": "3", "Name": "C", "Date": "not a date", "Status": "", "OK to email": ""}]
    recs, stats = importer.normalize("appointments", rows, mapping)
    assert [r["patient_id"] for r in recs] == ["1"] and recs[0]["email_ok"] is False
    assert stats["cancelled_visit"] == 1 and stats["bad_date"] == 1


# ---------------- groups ----------------

def test_every_patient_lands_in_one_group():
    out = scan([appt("1", 7), appt("2", 10), appt("3", 5), appt("4", 5), appt("5", 40)],
               [med("1", ago=400), med("2", ago=360), med("3", ago=10)])
    assert [r["group"] for r in out["results"]] == ["expired", "expiring"]
    assert [r["patient_id"] for r in out["clear"]] == ["3"]
    assert [r["patient_id"] for r in out["unchecked"]] == ["4"]  # no meds on file: not called clear
    assert out["summary"]["patients_in_window"] == 4  # patient 5 is outside the window


def test_lab_going_stale_by_the_visit_is_expiring_not_expired():
    out = scan([appt(days=10)], [med(drug="Warfarin 5 mg")], [lab(test="INR", ago=25)])
    assert statuses(out) == {("lab", "due_before_visit")} and out["results"][0]["group"] == "expiring"


# ---------------- worklist across scans ----------------

def practice_for(email) -> Practice:
    db = SessionLocal()
    return db, db.get(Practice, db.scalar(select(User).where(User.email == email)).practice_id)


def sync(db, practice, out):
    out = {k: out[k] for k in ("results", "clear", "unchecked")}
    return worklist.sync(db, practice, out), worklist.annotate(db, practice, out, T)


def test_worklist_keeps_work_and_clears_itself():
    email, _ = signup(new_client())
    db, p = practice_for(email)
    stale = scan([appt("1"), appt("2")], [med("1", ago=400), med("2", ago=400)])
    counts, payload = sync(db, p, stale)
    assert counts["new"] == 2
    done, other = payload["results"][0]["issues"][0], payload["results"][1]["issues"][0]
    worklist.set_status(db, p, done["work_hash"], "done", 1)

    _, payload = sync(db, p, stale)  # same data tomorrow: finished work stays finished
    assert payload["results"][0]["issues"][0]["work"] == "done"
    assert payload["results"][1]["issues"][0]["work"] == "open"

    fixed = scan([appt("1"), appt("2")], [med("1", ago=400), med("2", ago=5)])  # patient 2 was renewed
    counts, _ = sync(db, p, fixed)
    assert counts["cleared"] == 1
    w = db.scalar(select(WorkItem).where(WorkItem.item_hash == other["work_hash"]))
    assert w.status == "cleared" and w.closed_reason == "fixed_in_new_data"

    w = db.scalar(select(WorkItem).where(WorkItem.item_hash == done["work_hash"]))
    w.updated_at -= timedelta(days=worklist.RECHECK_DAYS + 1)  # marked done a week ago, export still shows it
    counts, payload = sync(db, p, fixed)
    assert counts["reopened"] == 1 and payload["results"][0]["issues"][0]["came_back"]
    db.close()


def test_new_visit_reopens_not_needed_item():
    email, _ = signup(new_client())
    db, p = practice_for(email)
    _, payload = sync(db, p, scan([appt(days=3)], [med(ago=400)]))
    worklist.set_status(db, p, payload["results"][0]["issues"][0]["work_hash"], "not_needed", 1)
    counts, _ = sync(db, p, scan([appt(days=12)], [med(ago=400)]))
    assert counts["reopened"] == 1
    db.close()


def test_worklist_buttons(tmp_path):
    from test_platform import fake_csvs, upload
    c = new_client()
    signup(c)
    page = upload(c, c.get("/").text, fake_csvs(tmp_path))
    assert "Expired or overdue now" in page.text and "All clear" in page.text
    scan_id = re.search(r"/scans/(\d+)/calllist", page.text).group(1)
    item = re.search(r'name="item" value="([0-9a-f]{64})"', page.text).group(1)
    r = c.post(f"/scans/{scan_id}/work", data={"csrf_token": csrf(page.text), "item": item, "status": "done"})
    assert r.status_code == 200
    assert c.post(f"/scans/{scan_id}/work", data={"csrf_token": csrf(page.text), "item": "f" * 64,
                                                  "status": "done"}).status_code == 404
    patient = re.search(r'name="patient" value="([0-9a-f]{64})"', page.text).group(1)
    r = c.post(f"/scans/{scan_id}/contact", data={"csrf_token": csrf(page.text), "patient": patient,
                                                  "outcome": "left_message", "note": "Back at 3"})
    assert r.status_code == 200
    assert "Back at 3" in c.get(f"/scans/{scan_id}?show=all&group=expired").text + c.get(
        f"/scans/{scan_id}?show=all&group=expiring").text
    with SessionLocal() as db:  # the note is stored encrypted
        assert b"Back at 3" not in b"".join(s for (s,) in db.execute(select(PatientState.log_enc)) if s)


# ---------------- reminders ----------------

def person(days=7, email="pat@example.com", **kw):
    return {"patient_hash": "h", "patient_name": "Doe, Pat", "appt_date": iso(days), "provider": "Dr. A",
            "email": email, "issues": [{"type": "lab", "item": "A1c", "work": "open"}], **kw}


class P:
    name, reminder_days = "Clinic", "14,7,3"
    email_subject, email_body = "Visit {appt_date}", "Hi {first_name}\n{todo_list}"


def test_reminder_stages_never_repeat():
    assert outreach.stage_for([14, 7, 3], 20) is None
    assert outreach.stage_for([14, 7, 3], 14) == 14
    assert outreach.stage_for([14, 7, 3], 5) == 7
    assert outreach.stage_for([14, 7, 3], 1) == 3
    due, _ = outreach.due_reminders(P, [person(5)], set(), T)
    assert due[0]["stage"] == 7 and "Hi Pat" in due[0]["body"] and "A1c" in due[0]["body"]
    due, held = outreach.due_reminders(P, [person(5)], {("h", iso(5), 7)}, T)
    assert not due and held["already_sent"] == 1
    due, _ = outreach.due_reminders(P, [person(2)], {("h", iso(2), 7)}, T)  # next stage still goes out
    assert due[0]["stage"] == 3


def test_reminders_held_back_for_good_reasons():
    cases = {"no_email": person(email=""), "opted_out": person(do_not_email=True),
             "reached_by_phone": person(reached_recently=True), "not_yet": person(30)}
    for reason, r in cases.items():
        due, held = outreach.due_reminders(P, [r], set(), T)
        assert not due and held[reason] == 1, reason
    consent = person(email_ok=False)
    assert outreach.due_reminders(P, [consent], set(), T)[1]["opted_out"] == 1
    finished = person()
    finished["issues"][0]["work"] = "done"
    assert outreach.due_reminders(P, [finished], set(), T)[1]["nothing_for_patient"] == 1


def test_patient_email_never_names_drug_and_skips_requested_renewals():
    r = person()
    r["issues"] = [{"type": "rx", "item": "Oxycodone 5 mg", "work": "open"}]
    body = outreach.due_reminders(P, [r], set(), T)[0][0]["body"]
    assert "Oxycodone" not in body and "prescription renewal" in body
    r["issues"][0]["work"] = "waiting"  # office already asked: nothing for the patient to do
    assert outreach.due_reminders(P, [r], set(), T)[1]["nothing_for_patient"] == 1


# ---------------- office requests ----------------

def rx_issue(prescriber="Dr. K", email="k@office.test", work="open", drug="Lisinopril 10 mg", controlled=False):
    return {"type": "rx", "item": drug, "work": work, "work_hash": drug, "prescriber": prescriber,
            "prescriber_email": email, "prescriber_contact": "555-0100", "last_prescribed": iso(-400),
            "controlled": controlled, "reason": "x", "action": "y"}


def test_office_requests_batch_per_prescriber():
    a = {"patient_name": "A", "dob": "1950-01-01", "appt_date": iso(5), "provider": "Dr. K",
         "issues": [rx_issue(), {"type": "lab", "item": "BMP", "work": "open", "work_hash": "l", "reason": "old"}]}
    b = {"patient_name": "B", "appt_date": iso(6), "provider": "Dr. Z",
         "issues": [rx_issue(drug="Oxycodone 5 mg", controlled=True), rx_issue("Dr. Z", "", drug="Amlodipine"),
                    rx_issue(work="waiting", drug="Already asked")]}
    groups = {g["name"]: g for g in outreach.office_groups([a, b])}
    assert set(groups) == {"Dr. K", "Dr. Z"}
    k = groups["Dr. K"]
    assert len(k["rx"]) == 2 and len(k["labs"]) == 1 and k["email"] == "k@office.test"
    body = outreach.office_draft(P, k)
    assert "1950-01-01" in body["body"] and "CONTROLLED" in body["body"] and "Already asked" not in body["body"]
    assert "Lisinopril" not in body["subject"] and "A" != body["subject"]  # no PHI in the subject


def test_office_send_marks_items_waiting(tmp_path):
    from test_platform import fake_csvs, upload
    c = new_client()
    signup(c)
    page = upload(c, c.get("/").text, fake_csvs(tmp_path))
    scan_id = re.search(r"/scans/(\d+)/calllist", page.text).group(1)
    offices = c.get(f"/scans/{scan_id}/offices")
    assert "Fax sheet" in offices.text and "Run test send" in offices.text
    gid = re.search(r"/offices/([0-9a-f]{12})/print", offices.text).group(1)
    sheet = c.get(f"/scans/{scan_id}/offices/{gid}/print")
    assert "Approved?" in sheet.text or "Ordered?" in sheet.text
    r = c.post(f"/scans/{scan_id}/offices/send-all", data={"csrf_token": csrf(offices.text)})
    assert "Office" in r.text and "Test only" in r.text
    left = c.get(f"/scans/{scan_id}/offices")
    assert left.text.count("Fax sheet") < offices.text.count("Fax sheet")  # emailed offices leave the queue


# ---------------- automation ----------------

def _files():
    return {"appointments": ("a.csv", f"MRN,Patient Name,Appt Date\n1,\"Doe, Pat\",{(date.today() + timedelta(days=5)).isoformat()}\n"),
            "medications": ("m.csv", "MRN,Medication,Date Written\n1,Sertraline,2020-01-01\n")}


def test_upload_key_api(tmp_path):
    c = new_client()
    signup(c, sign_baa=False)
    page = c.get("/settings")
    r = c.post("/settings/upload-key", data={"csrf_token": csrf(page.text)})
    key = re.search(r'value="(pvk_[^"]+)"', r.text).group(1)
    api = new_client()
    assert api.post("/api/v1/import", headers={"X-Previsit-Key": "wrong"}, files=_files()).status_code == 401
    assert api.post("/api/v1/import", files=_files()).status_code == 401
    assert api.post("/api/v1/import", headers={"X-Previsit-Key": key}, files=_files()).status_code == 403  # no BAA
    page = c.get("/baa")
    c.post("/baa", data={"csrf_token": csrf(page.text), "full_name": "O", "title": "O", "agree": "1"})
    r = api.post("/api/v1/import", headers={"X-Previsit-Key": key}, files=_files())
    assert r.status_code == 200 and r.json()["ok"]
    assert "Automatic" in c.get("/").text
    assert "prescription expired" in c.get("/worklist").text
    assert api.get("/worklist", headers={"X-Previsit-Key": key}, follow_redirects=False).status_code == 303  # upload only
    bad = {"appointments": ("a.csv", "Foo,Bar\n1,2\n")}
    r = api.post("/api/v1/import", headers={"X-Previsit-Key": key}, files=bad)
    assert r.status_code == 422 and "Match columns" in c.get("/").text


def test_daily_run_once_per_day_and_respects_hour_and_auto_send(tmp_path):
    from test_platform import fake_csvs, upload
    c = new_client()
    email, _ = signup(c)
    upload(c, c.get("/").text, fake_csvs(tmp_path))
    db, p = practice_for(email)
    p.auto_scan, p.auto_scan_hour, p.auto_send = True, 6, True
    db.commit()

    def at(hour):
        return lambda practice: datetime.combine(date.today(), datetime.min.time(), ZoneInfo(practice.timezone)).replace(hour=hour)

    others = db.scalars(select(Practice).where(Practice.id != p.id)).all()
    for o in others:
        o.auto_scan = False
    db.commit()
    assert automation.run_due_practices(db, at(5)) == 0  # too early
    assert automation.run_due_practices(db, at(7)) == 1
    assert automation.run_due_practices(db, at(8)) == 0  # already ran today
    sent = db.scalars(select(ReminderLog).where(ReminderLog.practice_id == p.id)).all()
    assert sent and all(r.test_mode for r in sent)
    db.close()


def test_today_follows_the_practice_clock():
    class Pacific:
        timezone = "Pacific/Honolulu"

    class Eastern:
        timezone = "America/New_York"
    assert pipeline.local_now(Pacific).utcoffset() != pipeline.local_now(Eastern).utcoffset()


def test_excel_formula_injection_blocked():
    r = {"group": "expired", "appt_date": iso(5), "patient_name": "=HYPERLINK(\"http://x\")", "patient_id": "1",
         "phone": "+1 555 0100", "email": "", "provider": "@evil", "issues": [
             {"type": "lab", "item": "A1c", "reason": "r", "action": "a", "work": "open"}]}
    row = outreach.call_list_rows([r])[0]
    assert row[1].startswith("'=") and row[6].startswith("'@") and row[4] == "+1 555 0100"


# ---------------- real send path (mail server faked) ----------------

class FakeSMTP:
    sent, tls = [], []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        FakeSMTP.tls.append(context)

    def login(self, user, password):
        assert password == "mailbox-pass"

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class Mailbox:
    name = "Clinic"
    smtp_host, smtp_port, smtp_user, smtp_from = "mail.test", 587, "front@clinic.test", "front@clinic.test"

    def __init__(self):
        from app import crypto
        self.wrapped_key = crypto.new_wrapped_data_key()
        self.smtp_password_enc = crypto.encrypt_text(self, "mailbox-pass")


def test_live_send_uses_tls_and_respects_allowlist(monkeypatch):
    from app import config
    monkeypatch.setattr(outreach.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "EMAIL_ALLOWLIST", {"ok@test.test"})
    FakeSMTP.sent.clear()
    FakeSMTP.tls.clear()
    drafts = [{"to": "ok@test.test", "subject": "s", "body": "b"}, {"to": "other@test.test", "subject": "s", "body": "b"}]
    out = {d["to"]: status for d, status, _ in outreach.send_drafts(Mailbox(), drafts)}
    assert out == {"ok@test.test": "sent", "other@test.test": "skipped"}
    assert len(FakeSMTP.sent) == 1 and FakeSMTP.tls and FakeSMTP.sent[0]["From"] == "front@clinic.test"


def test_mail_server_down_is_reported_not_crashed(monkeypatch):
    from app import config

    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(outreach.smtplib, "SMTP", boom)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "EMAIL_ALLOWLIST", set())
    (_, status, note), = outreach.send_drafts(Mailbox(), [{"to": "a@b.test", "subject": "s", "body": "b"}])
    assert status == "failed" and "Mail server" in note


def test_read_only_cannot_touch_the_worklist():
    from app.auth import ROLE_PERMS
    assert "work" not in ROLE_PERMS["readonly"] and "work" in ROLE_PERMS["staff"]


def test_same_day_visit_is_a_call_not_an_email():
    due, held = outreach.due_reminders(P, [person(0)], set(), T)
    assert not due and held["call_instead"] == 1


def test_stale_data_never_auto_emails_patients(tmp_path):
    from app.models import Dataset
    from test_platform import fake_csvs, upload
    c = new_client()
    email, _ = signup(c)
    upload(c, c.get("/").text, fake_csvs(tmp_path))
    db, p = practice_for(email)
    p.auto_send = True
    ds = db.scalar(select(Dataset).where(Dataset.practice_id == p.id))
    ds.created_at -= timedelta(days=3)  # the office PC stopped sending
    db.commit()
    assert pipeline.automatic_run(db, p) is not None  # staff still get a fresh worklist
    assert not db.scalars(select(ReminderLog).where(ReminderLog.practice_id == p.id)).all()
    ds.created_at += timedelta(days=3)
    pipeline.automatic_run(db, p)
    assert db.scalars(select(ReminderLog).where(ReminderLog.practice_id == p.id)).all()
    db.close()
