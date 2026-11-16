"""Makes FAKE practice exports (no real people) dated around today, with messy EHR-style headers.

Run:  .venv\\Scripts\\python sample_data\\make_fake_data.py
Writes fake_appointments_<date>.csv, fake_medications_<date>.csv, fake_labs_<date>.csv here.
"""
import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path

FIRST = ["Avery", "Jordan", "Riley", "Casey", "Morgan", "Quinn", "Taylor", "Rowan", "Skyler", "Emerson",
         "Hayden", "Parker", "Reese", "Sawyer", "Dakota", "Finley", "Harper", "Jules", "Kendall", "Logan"]
LAST = ["Testwell", "Sample", "Fakerson", "Mockley", "Demo", "Placeholder", "Exampleton", "Dummyfield"]
PROVIDERS = ["Dr. A. Rivera", "Dr. K. Osei", "N. Brooks, NP"]
MEDS = [  # (name, schedule)
    ("Metformin 500 mg", ""), ("Atorvastatin 20 mg", ""), ("Levothyroxine 50 mcg", ""),
    ("Lisinopril 10 mg", ""), ("Warfarin 5 mg", ""), ("Lithium carbonate 300 mg", ""),
    ("Sertraline 50 mg", ""), ("Alprazolam 0.5 mg", "IV"), ("Oxycodone 5 mg", "CII"),
    ("Amlodipine 5 mg", ""), ("Semaglutide 0.5 mg", ""), ("Losartan 50 mg", ""),
    ("Lipitor 40 mg", ""), ("Ozempic 1 mg", ""), ("Lorazepam 1 mg", ""),  # brand names, blank schedule
]
OFFICES = {"Dr. A. Rivera": ("555-0199", "rivera.office@example.com"), "Dr. K. Osei": ("555-0198", ""),
           "N. Brooks, NP": ("555-0197", "brooks.office@example.com")}
LABS_FOR = {"Metformin": "Hemoglobin A1c", "Semaglutide": "HbA1c", "Ozempic": "HbA1c", "Lipitor": "Lipid Panel",
            "Atorvastatin": "Lipid Panel",
            "Levothyroxine": "TSH", "Lisinopril": "BMP", "Losartan": "Comprehensive Metabolic Panel",
            "Warfarin": "PT/INR", "Lithium": "Lithium Level"}


def main(out_dir: Path, today: date, seed: int = 7, patients: int = 40):
    rnd = random.Random(seed)
    appts, meds, labs = [], [], []
    for n in range(patients):
        pid = f"MRN{10400 + n}"
        first, last = rnd.choice(FIRST), rnd.choice(LAST)
        visit = today + timedelta(days=rnd.randint(-3, 21))  # some outside the 14-day window
        appts.append({"MRN": pid, "Patient Name": f"{last}, {first}",
                      "Appt Date": f"{visit:%m/%d/%Y} {rnd.choice(['9:00 AM', '10:30 AM', '2:15 PM'])}",
                      "DOB": f"{rnd.randint(1,12):02d}/{rnd.randint(1,28):02d}/{rnd.randint(1940,2000)}",
                      "Appt Status": "Cancelled" if rnd.random() < 0.08 else "Scheduled",
                      "Rendering Provider": rnd.choice(PROVIDERS), "Visit Type": rnd.choice(["Follow-up", "Annual", "Med check"]),
                      "Cell Phone": f"555-01{n:02d}", "Email Address": f"{first.lower()}.{last.lower()}{n}@example.com"
                      if rnd.random() < 0.8 else ""})
        for name, sched in rnd.sample(MEDS, rnd.randint(1, 3)):
            written = today - timedelta(days=rnd.choice([30, 100, 170, 200, 340, 360, 380, 420]))
            who = rnd.choice(PROVIDERS)
            row = {"Chart #": pid, "Medication Name": name, "Date Written": f"{written:%Y-%m-%d}",
                   "Refills Left": rnd.choice(["0", "1", "2", "3", "5"]), "DEA Schedule": sched,
                   "Med Status": "Active" if rnd.random() < 0.9 else "Discontinued", "Prescribed By": who,
                   "Prescriber Fax": OFFICES[who][0], "Prescriber Email": OFFICES[who][1]}
            meds.append(row)
            if rnd.random() < 0.15:  # already renewed: the old row stays in the export
                meds.append({**row, "Date Written": f"{today - timedelta(days=20):%Y-%m-%d}", "Refills Left": "3"})
            lab = next((v for k, v in LABS_FOR.items() if name.startswith(k)), None)
            if lab and rnd.random() < 0.8:
                done = today - timedelta(days=rnd.choice([10, 20, 60, 150, 200, 300, 400, 500]))
                labs.append({"MRN": pid, "Component": lab, "Collected": f"{done:%m/%d/%y}"})
    stamp = today.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("appointments", appts), ("medications", meds), ("labs", labs)):
        path = out_dir / f"fake_{name}_{stamp}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {path} ({len(rows)} rows)")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent, date.today())
