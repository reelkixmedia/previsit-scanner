# EHR export formats: what's public (researched 2026-11-07)

**Bottom line:** vendors rarely publish the column names of their built-in reports, and most of those reports
let each practice pick its own columns. The reliable public sources are the federally required data-export
dictionaries (ONC §170.315(b)(10)), which use machine-style names. So Previsit uses a few exact presets where
names are documented, plus a broad matcher for everything else.

## What became a preset (exact, documented column names)

| System | File | Source |
| --- | --- | --- |
| athenahealth | Patient Throughput report (appointments) | https://help.athenahealth.com/Ohelp/Content/Reports/Patient_Throughput_Report_A.htm |
| Epic | PAT_ENC (appointments), ORDER_MED (medications) | https://open.epic.com/EHITables |
| Practice Fusion | patient-appointments, patient-medications (tab-separated) | https://www.practicefusion.com/ehi-export-documentation/v4/ |

## What did not, and why

| System | Status |
| --- | --- |
| eClinicalWorks | Reports are configurable; header lists are behind the customer portal |
| NextGen | EHI schema is behind a Success Community login |
| Tebra (Kareo) | No public report headers; SOAP API needs a customer key |
| ModMed EMA | Data dictionary is public but marked not to be shared without ModMed's permission, so not used |
| Elation | Appointment CSV contents described but exact headers not published |
| DrChrono | Export file names documented, column names not visible |
| Greenway Intergy / Prime Suite | Dictionary site renders by script; Intergy writes `?` for empty (handled) |
| Veradigm / Allscripts | EHR guide is an image PDF; PM export is JSON |
| Epic labs | ORDER_RESULTS has no patient ID column (needs an encounter join), so not a preset |

## Format rules now handled by the importer

- Comma, tab and pipe separated files.
- Files with no header row (athenaOne can suppress headings): columns become "Column 1, 2..." and staff match them once.
- `?`, `null`, `n/a` read as empty.
- A medication with a past stop or end date counts as stopped.
- Plain "Refills" is not used as refills remaining: in most systems it means refills authorized.
- Phone, email and consent often live in a separate patient demographics report, so there is an optional
  "patients" file joined by patient ID. Appointments no longer need a name column.

## Still to confirm with a real practice

The export from the practice's own system, with patient rows deleted. That decides which of the above
actually applies to them.
