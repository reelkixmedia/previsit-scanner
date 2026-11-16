"""Import presets for documented EHR layouts, and the formats real exports come in."""
from datetime import date, timedelta

from app import importer, presets
from app.scanner import DEFAULT_RX_DAYS, run_scan

TODAY = date.today()


def soon(days=5):
    return (TODAY + timedelta(days=days)).isoformat()


def test_athenahealth_throughput_layout_is_recognised():
    headers = ["Patient ID", "Patient Name", "Sex", "DOB", "Age", "Appointment ID", "Appointment Scheduled",
               "Appointment Date", "Appointment Time", "Appointment Type", "Scheduled Provider",
               "Rendering Provider", "Department"]
    name, mapping = presets.detect("appointments", headers)
    assert name == "athenahealth Patient Throughput report"
    assert mapping["appt_date"] == "Appointment Date" and mapping["provider"] == "Rendering Provider"


def test_epic_medication_export_is_recognised_and_stopped_meds_dropped():
    text = ("PAT_ID,DISPLAY_NAME,ORDERING_DATE,START_DATE,END_DATE,REFILLS,REFILLS_REMAINING,ORDER_STATUS_C_NAME,"
            "MED_PRESC_PROV_ID_PROV_NAME\n"
            "Z1,Lisinopril 10 mg,2025-01-02,2025-01-02,,3,0,Sent,\"OSEI, KWAME\"\n"
            "Z1,Atorvastatin 20 mg,2025-01-02,2025-01-02,2025-06-01,3,1,Sent,\"OSEI, KWAME\"\n"
            "Z2,Metformin 500 mg,2025-02-02,2025-02-02,,1,1,Discontinued,\"OSEI, KWAME\"\n")
    headers, rows = importer.read_csv(text)
    name, mapping = presets.detect("medications", headers)
    assert name == "Epic data export"
    assert mapping["refills_remaining"] == "REFILLS_REMAINING"  # never the authorized count
    recs, stats = importer.normalize("medications", rows, mapping)
    assert [r["drug_name"] for r in recs] == ["Lisinopril 10 mg"]
    assert stats["discontinued"] == 2  # one by status, one by a past stop date
    assert recs[0]["prescriber"] == "OSEI, KWAME"


def test_practice_fusion_tab_separated_files():
    appts = f"PatientPracticeGuid\tAppointmentGuid\tStartDateTime\tAppointmentType\tAppointmentStatus\n" \
            f"g-1\ta-1\t{soon()}T09:30:00\tFollow up\tScheduled\n" \
            f"g-2\ta-2\t{soon()}T10:00:00\tFollow up\tCancelled\n"
    meds = "PatientPracticeGuid\tMedicationName\tStartDate\tStopDate\n" \
           "g-1\tWarfarin 5 mg\t2025-01-01\t\n"
    a_headers, a_rows = importer.read_csv(appts)
    m_headers, m_rows = importer.read_csv(meds)
    a_name, a_map = presets.detect("appointments", a_headers)
    m_name, m_map = presets.detect("medications", m_headers)
    assert a_name == m_name == "Practice Fusion data export"
    a_recs, a_stats = importer.normalize("appointments", a_rows, a_map)
    m_recs, _ = importer.normalize("medications", m_rows, m_map)
    assert len(a_recs) == 1 and a_stats["cancelled_visit"] == 1
    out = run_scan({"appointments": a_recs, "medications": m_recs, "labs": []}, DEFAULT_RX_DAYS, [], TODAY, 14)
    assert out["results"][0]["patient_name"] == "Patient g-1"  # no name in this file: shown by ID, not dropped


def test_pipe_separated_and_question_mark_nulls():
    text = f"Patient ID|Patient Name|Appointment Date|Email\n1|Doe, Pat|{soon()}|?\n"
    headers, rows = importer.read_csv(text)
    assert headers == ["Patient ID", "Patient Name", "Appointment Date", "Email"]
    recs, _ = importer.normalize("appointments", rows, importer.guess_mapping("appointments", headers))
    assert recs[0]["email"] == ""


def test_file_without_header_row_goes_to_matching():
    text = f"1,\"Doe, Pat\",{soon()}\n2,\"Roe, Sam\",{soon(6)}\n"
    headers, rows = importer.read_csv(text)
    assert headers == ["Column 1", "Column 2", "Column 3"] and len(rows) == 2  # first row kept as data
    assert presets.detect("appointments", headers) == (None, None)
    assert importer.missing_required("appointments", importer.guess_mapping("appointments", headers))


def test_camel_case_and_underscore_headers_match():
    assert importer._norm("PatientPracticeGuid") == "patient practice guid"
    assert importer._norm("PAT_ID") == "pat id"
    m = importer.guess_mapping("medications", ["MRN", "MedicationDisplayName", "DateOfService", "Refills"])
    assert m == {"patient_id": "MRN", "drug_name": "MedicationDisplayName", "last_prescribed": "DateOfService"}


def test_customised_report_falls_back_to_general_matcher():
    headers = ["Patient ID", "Appointment Date", "Phone"]  # athena-like but trimmed
    name, mapping = presets.detect("appointments", headers)
    assert name == "athenahealth Patient Throughput report"  # required fields are all there
    assert presets.detect("appointments", ["Chart #", "Visit Date"]) == (None, None)


def test_patients_file_fills_contact_details():
    appts = [{"patient_id": "1", "appt_date": soon(), "patient_name": ""}]
    meds = [{"patient_id": "1", "drug_name": "Sertraline", "last_prescribed": "2020-01-01", "schedule": "non-controlled"}]
    contacts = [{"patient_id": "1", "patient_name": "Doe, Pat", "phone": "555-0100", "email": "pat@example.com",
                 "email_ok": False}]
    r = run_scan({"appointments": appts, "medications": meds, "labs": [], "patients": contacts},
                 DEFAULT_RX_DAYS, [], TODAY, 14)["results"][0]
    assert (r["patient_name"], r["phone"], r["email"], r["email_ok"]) == ("Doe, Pat", "555-0100", "pat@example.com", False)


def test_every_preset_cites_a_source():
    assert set(presets.PRESETS) == set(presets.PRESET_SOURCES)
    for layouts in presets.PRESETS.values():
        for kind, fields in layouts.items():
            assert set(fields) <= {f for f, _, _ in importer.FIELDS[kind]}


def test_preset_upload_through_the_website_skips_matching():
    from test_platform import csrf, new_client, signup
    c = new_client()
    signup(c)
    files = {
        "appointments": ("pf_appointments.tsv",
                         f"PatientPracticeGuid\tStartDateTime\tAppointmentType\tAppointmentStatus\ng-1\t{soon()}T09:30:00\tFollow up\tScheduled\n"),
        "medications": ("pf_medications.tsv", "PatientPracticeGuid\tMedicationName\tStartDate\tStopDate\ng-1\tWarfarin 5 mg\t2024-01-01\t\n"),
        "patients": ("contacts.csv", "MRN,Patient Name,Cell Phone,Email Address\ng-1,\"Doe, Pat\",555-0100,pat@example.com\n"),
    }
    r = c.post("/upload", data={"csrf_token": csrf(c.get("/").text)}, files=files)
    assert "<h1>Worklist</h1>" in r.text and "Doe, Pat" in r.text and "Warfarin 5 mg" in r.text
    assert "Practice Fusion data export" in c.get("/").text
