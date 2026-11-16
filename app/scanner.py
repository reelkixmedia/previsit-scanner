"""The scan: for every patient with a visit in the look-ahead window, check each prescription and
each medication-linked lab against the practice's rules *as of the visit date*.

Every patient lands in exactly one group, so nobody is silently skipped:
  expired    something is already out of date today
  expiring   fine today, but will be out of date by the visit (or no refills left)
  clear      checked, nothing needed
  unchecked  no medication records to check, so we can't say they're clear
"""
import re
from collections import defaultdict
from datetime import date, timedelta

RX_CATEGORIES = ["non-controlled", "C-III-V", "C-II"]
RX_LABELS = {"non-controlled": "Non-controlled", "C-III-V": "Controlled, Schedule III-V",
             "C-II": "Controlled, Schedule II"}
GROUPS = ["expired", "expiring", "unchecked", "clear"]
GROUP_LABELS = {"expired": "Expired or overdue now", "expiring": "Runs out before the visit",
                "unchecked": "Can't check", "clear": "All clear"}
NOW_STATUSES = {"expired", "stale", "missing"}

# Starter defaults only. Every practice's clinicians must review and own these.
DEFAULT_RX_DAYS = {"non-controlled": 365, "C-III-V": 180, "C-II": 90}
DEFAULT_LAB_RULES = [
    ("A1c", "metformin, glucophage, glipizide, glucotrol, glimepiride, amaryl, glyburide, insulin, lantus, "
            "humalog, novolog, sitagliptin, januvia, empagliflozin, jardiance, dapagliflozin, farxiga, "
            "semaglutide, ozempic, rybelsus, liraglutide, victoza, dulaglutide, trulicity, tirzepatide, "
            "mounjaro, pioglitazone, actos",
     "a1c, hba1c, hemoglobin a1c, glycated hemoglobin", 180),
    ("Lipid panel", "atorvastatin, lipitor, simvastatin, zocor, rosuvastatin, crestor, pravastatin, "
                    "pravachol, lovastatin, pitavastatin",
     "lipid panel, lipid, cholesterol, ldl", 365),
    ("TSH", "levothyroxine, synthroid, levoxyl, liothyronine, cytomel, armour thyroid, methimazole",
     "tsh, thyroid stimulating hormone", 365),
    ("INR", "warfarin, coumadin, jantoven", "inr, pt/inr, prothrombin time", 30),
    ("Lithium level", "lithium, lithobid", "lithium level, lithium", 180),
    ("Kidney function (BMP/CMP)", "lisinopril, zestril, prinivil, enalapril, vasotec, ramipril, altace, "
                                  "benazepril, lotensin, losartan, cozaar, valsartan, diovan, olmesartan, "
                                  "benicar, irbesartan, avapro, spironolactone, aldactone",
     "bmp, cmp, basic metabolic, comprehensive metabolic, creatinine", 365),
]

# Used only when the export leaves the schedule blank, so a controlled drug doesn't get the 1-year window.
KNOWN_C2 = ["oxycodone", "percocet", "oxycontin", "hydrocodone", "norco", "vicodin", "hydromorphone", "dilaudid",
            "morphine", "fentanyl", "methadone", "oxymorphone", "tapentadol", "methylphenidate", "ritalin",
            "concerta", "amphetamine", "dextroamphetamine", "adderall", "lisdexamfetamine", "vyvanse"]
KNOWN_C345 = ["alprazolam", "xanax", "lorazepam", "ativan", "clonazepam", "klonopin", "diazepam", "valium",
              "temazepam", "zolpidem", "ambien", "eszopiclone", "tramadol", "ultram", "testosterone",
              "buprenorphine", "suboxone", "pregabalin", "lyrica", "carisoprodol", "soma", "phentermine",
              "modafinil", "provigil"]
_FORM_WORDS = {"er", "xr", "sr", "xl", "cr", "dr", "hcl", "tablet", "tablets", "tab", "tabs", "capsule", "capsules",
               "cap", "caps", "oral", "solution"}


def keywords(text: str) -> list[str]:
    return [k.strip().lower() for k in (text or "").split(",") if k.strip()]


def matches(kws: list[str], text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", t) for k in kws)


def drug_key(name: str) -> str:
    """"Metformin ER 500 mg" and "metformin 1000mg" are the same drug: a renewal, not a second prescription."""
    m = re.match(r"[a-z][a-z\-/ ]*", (name or "").lower().strip())
    words = [w for w in (m.group(0) if m else (name or "").lower()).split() if w not in _FORM_WORDS]
    return " ".join(words) or (name or "").lower().strip()


def schedule_of(med: dict) -> tuple[str, bool]:
    """(category, was it inferred from the drug name because the export left it blank?)"""
    given = med.get("schedule") or "non-controlled"
    if given != "non-controlled":
        return given, False
    if matches(KNOWN_C2, med["drug_name"]):
        return "C-II", True
    if matches(KNOWN_C345, med["drug_name"]):
        return "C-III-V", True
    return given, False


def fmt(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _rx_issue(med: dict, category: str, rx_days: dict, scan_date: date, visit: date) -> dict | None:
    valid_until = _d(med["last_prescribed"]) + timedelta(days=rx_days.get(category, 365))
    name = med["drug_name"]
    if valid_until < scan_date:
        status, reason = "expired", f"{name}: prescription expired {fmt(valid_until)}"
    elif valid_until < visit:
        status, reason = "expires_before_visit", f"{name}: prescription expires {fmt(valid_until)}, before the visit"
    elif med.get("refills_remaining") == 0 and category != "C-II":  # Schedule II never has refills
        status, reason = "no_refills", f"{name}: no refills left"
    else:
        return None
    who = med.get("prescriber") or "the prescriber"
    return {"type": "rx", "key": "rx:" + drug_key(name), "item": name, "status": status, "reason": reason,
            "action": f"Request renewal from {who}", "prescriber": med.get("prescriber", ""),
            "prescriber_contact": med.get("prescriber_contact", ""), "prescriber_email": med.get("prescriber_email", ""),
            "last_prescribed": med["last_prescribed"], "controlled": category != "non-controlled"}


def _lab_issue(rule: dict, meds: list[dict], labs: list[dict], scan_date: date, visit: date) -> dict | None:
    triggers = sorted({m["drug_name"] for m in meds if matches(rule["drug_keywords"], m["drug_name"])})
    if not triggers:
        return None
    done = [_d(x["result_date"]) for x in labs
            if matches(rule["lab_keywords"], x["test_name"]) and _d(x["result_date"]) <= visit]
    name, limit, on = rule["name"], rule["max_age_days"], ", ".join(triggers)
    base = {"type": "lab", "key": "lab:" + name.lower(), "item": name, "triggered_by": on,
            "action": f"Order {name} before the visit"}
    if not done:
        return {**base, "status": "missing", "reason": f"No {name} on record (on {on})"}
    last = max(done)
    age = (visit - last).days
    if age <= limit:
        return None
    status = "stale" if (scan_date - last).days > limit else "due_before_visit"
    return {**base, "status": status,
            "reason": f"Last {name} {fmt(last)}, {age} days old at visit (rule: every {limit} days)"}


def run_scan(data: dict, rx_days: dict, lab_rules: list[dict], scan_date: date, lookahead_days: int) -> dict:
    end = scan_date + timedelta(days=lookahead_days)
    meds_by, labs_by = defaultdict(list), defaultdict(list)
    for m in data.get("medications", []):
        meds_by[m["patient_id"]].append(m)
    for lab in data.get("labs", []):
        labs_by[lab["patient_id"]].append(lab)
    have_meds, have_labs = bool(data.get("medications")), bool(data.get("labs"))
    contacts = {p["patient_id"]: p for p in data.get("patients", [])}
    ids_with_meds = set(meds_by)

    first_visit = {}
    in_window = [a for a in data.get("appointments", []) if scan_date <= _d(a["appt_date"]) <= end]
    for a in sorted(in_window, key=lambda a: a["appt_date"]):
        first_visit.setdefault(a["patient_id"], a)

    results, clear, unchecked = [], [], []
    inferred = 0
    for pid, appt in first_visit.items():
        visit = _d(appt["appt_date"])
        info = contacts.get(pid, {})

        def pick(field, default=""):
            return appt.get(field) or info.get(field) or default
        person = {"patient_id": pid, "patient_name": pick("patient_name", f"Patient {pid}"), "dob": pick("dob"),
                  "appt_date": appt["appt_date"], "days_to_visit": (visit - scan_date).days,
                  "provider": appt.get("provider", ""), "email": pick("email"), "phone": pick("phone"),
                  "visit_type": appt.get("visit_type", ""),
                  "email_ok": appt.get("email_ok", True) and info.get("email_ok", True)}
        if not meds_by[pid]:
            unchecked.append({**person, "group": "unchecked"})
            continue

        latest = {}  # a renewed script replaces the older row for the same drug
        for m in sorted(meds_by[pid], key=lambda m: m["last_prescribed"]):
            latest[drug_key(m["drug_name"])] = m
        issues = []
        for m in latest.values():
            category, was_inferred = schedule_of(m)
            inferred += was_inferred
            issue = _rx_issue(m, category, rx_days, scan_date, visit)
            if issue:
                issues.append(issue)
        if have_labs:  # without a labs file every patient would look like they have no labs
            for rule in lab_rules:
                issue = _lab_issue(rule, list(latest.values()), labs_by[pid], scan_date, visit)
                if issue:
                    issues.append(issue)

        if not issues:
            clear.append({**person, "group": "clear"})
            continue
        group = "expired" if any(i["status"] in NOW_STATUSES for i in issues) else "expiring"
        results.append({**person, "group": group, "issues": issues})

    def order(r):
        return (r["appt_date"], r["patient_name"])
    results.sort(key=lambda r: (GROUPS.index(r["group"]),) + order(r))
    clear.sort(key=order)
    unchecked.sort(key=order)

    rx_issues = [i for r in results for i in r["issues"] if i["type"] == "rx"]
    lab_count = sum(i["type"] == "lab" for r in results for i in r["issues"])
    checks = {
        "no_meds_file": not have_meds,
        "no_labs_file": not have_labs,
        "ids_dont_match": have_meds and bool(first_visit) and not ids_with_meds & set(first_visit),
        "no_way_to_contact": sum(1 for r in results if not r["phone"] and not r["email"]),
        "no_prescriber_contact": sum(1 for i in rx_issues if not i["prescriber_contact"] and not i["prescriber_email"]),
        "future_dates": sum(1 for m in data.get("medications", []) if _d(m["last_prescribed"]) > scan_date)
        + sum(1 for x in data.get("labs", []) if _d(x["result_date"]) > scan_date),
        "schedules_inferred": inferred,
    }
    summary = {"scan_date": scan_date.isoformat(), "window_end": end.isoformat(),
               "patients_in_window": len(first_visit), "patients_flagged": len(results),
               "expired": sum(r["group"] == "expired" for r in results),
               "expiring": sum(r["group"] == "expiring" for r in results),
               "clear": len(clear), "unchecked": len(unchecked),
               "rx_issues": len(rx_issues), "lab_issues": lab_count, "checks": checks}
    return {"summary": summary, "results": results, "clear": clear, "unchecked": unchecked}
