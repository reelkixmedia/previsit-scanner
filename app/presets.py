"""Known report layouts from common practice systems. When an upload's headers match one, its columns are
matched automatically and the practice never sees the column-matching step.

A preset is only used when every required field is found under that system's own header names, so a
partial or customised report falls back to the general matcher (and, if needed, the matching page)."""
from .importer import FIELDS, _norm

# system -> kind -> field -> header names that system's report uses (see PRESET_SOURCES for where each came from)
# Only layouts whose column names a vendor publishes are here. Most built-in reports let each practice pick
# columns, so for everything else the general matcher (importer.SYNONYMS) does the work.
PRESETS: dict[str, dict[str, dict[str, list[str]]]] = {
    "athenahealth Patient Throughput report": {
        "appointments": {
            "patient_id": ["Patient ID"], "patient_name": ["Patient Name"], "appt_date": ["Appointment Date"],
            "provider": ["Rendering Provider", "Scheduled Provider"], "visit_type": ["Appointment Type"],
            "dob": ["DOB"],
        },
    },
    "Epic data export": {
        "appointments": {
            "patient_id": ["PAT_ID"], "appt_date": ["CONTACT_DATE"], "appt_status": ["APPT_STATUS_C_NAME"],
            "provider": ["VISIT_PROV_ID_PROV_NAME"],
        },
        "medications": {
            "patient_id": ["PAT_ID"], "drug_name": ["DISPLAY_NAME", "DESCRIPTION", "MEDICATION_ID_MEDICATION_NAME"],
            "last_prescribed": ["ORDERING_DATE", "START_DATE"], "refills_remaining": ["REFILLS_REMAINING"],
            "status": ["ORDER_STATUS_C_NAME"], "stop_date": ["END_DATE", "DISCON_TIME"],
            "prescriber": ["MED_PRESC_PROV_ID_PROV_NAME", "AUTHRZING_PROV_ID_PROV_NAME", "ORD_PROV_ID_PROV_NAME"],
        },
    },
    "Practice Fusion data export": {
        "appointments": {
            "patient_id": ["PatientPracticeGuid"], "appt_date": ["StartDateTime"], "visit_type": ["AppointmentType"],
            "appt_status": ["AppointmentStatus"],
        },
        "medications": {
            "patient_id": ["PatientPracticeGuid"], "drug_name": ["MedicationName", "GenericName"],
            "last_prescribed": ["StartDate"], "stop_date": ["StopDate"],
        },
    },
}
PRESET_SOURCES = {
    "athenahealth Patient Throughput report":
        "https://help.athenahealth.com/Ohelp/Content/Reports/Patient_Throughput_Report_A.htm",
    "Epic data export": "https://open.epic.com/EHITables (tables PAT_ENC, ORDER_MED)",
    "Practice Fusion data export": "https://www.practicefusion.com/ehi-export-documentation/v4/ "
                                   "(patient-appointments, patient-medications)",
}


def detect(kind: str, headers: list[str]) -> tuple[str | None, dict | None]:
    """(system name, mapping) for the first preset whose required fields are all present, else (None, None)."""
    by_norm = {_norm(h): h for h in headers}
    required = [f for f, _, req in FIELDS[kind] if req]
    for system, kinds in PRESETS.items():
        layout = kinds.get(kind)
        if not layout:
            continue
        mapping = {}
        for field, names in layout.items():
            hit = next((by_norm[_norm(n)] for n in names if _norm(n) in by_norm), None)
            if hit and hit not in mapping.values():
                mapping[field] = hit
        if all(f in mapping for f in required):
            return system, mapping
    return None, None
