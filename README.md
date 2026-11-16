# Previsit Scanner

**A security-first web app that scans a medical practice's upcoming visits for expired prescriptions and overdue
lab work, then turns the results into a staff call list and patient reminders. Built as a demo with synthetic data
only.**

> **Disclaimer:** This is a portfolio demo. It runs on **synthetic (fake) data only**, it is **not a certified or
> audited HIPAA-compliant system**, and it is **not medical advice**. The scan rules are starter defaults that a
> clinician would need to review. Do not load real patient data into it.

## The problem

Patients arrive for a visit and only then does the practice discover a prescription has expired or a monitoring lab
(A1c for metformin, INR for warfarin, kidney function for lisinopril, and so on) is overdue. The visit stalls, staff
scramble, and wait times grow. The information needed to catch this already sits in the practice's EHR exports; nobody
has time to cross-check it by hand two weeks ahead.

## What it does

1. **Ingests EHR exports** (appointments, medications, labs, optional patient contacts) as comma, tab or pipe files,
   with or without headers. Exact column presets for documented athenahealth, Epic and Practice Fusion layouts, plus
   a fuzzy column matcher for everything else. Column mappings are remembered per file layout.
2. **Scans visits in a look-ahead window (default 14 days)** and judges each prescription and medication-linked lab
   *as of the visit date*:
   - Prescription validity by DEA schedule (defaults: non-controlled 365 days, C-III-V 180, C-II 90) and refills left.
   - Medication-linked labs (A1c, lipid panel, TSH, INR, lithium level, BMP/CMP) with per-rule intervals.
   - Newest script per drug wins; brand/generic names and blank schedules are handled.
3. **Puts every patient in exactly one group** so nobody is silently skipped: *expired now*, *runs out before the
   visit*, *can't check* (no medication records), *all clear*.
4. **Builds a persistent worklist**: finished items stay finished across scans, items fixed in newer data close
   themselves, and "done" items still present after 7 days reopen. Includes a call log and email opt-out.
5. **Outreach**: Excel call list (one sheet per group), patient email reminders at 14/7/3 days that name the lab but
   never the medication, and one batched refill request per prescribing office (email or printable fax sheet).
   **Dry-run is on by default**; an allowlist limits real sends.
6. **Automation**: a per-practice upload-only key, a PowerShell office agent (`agent/previsit_agent.ps1`) for nightly
   export upload, and a daily in-process scan with optional auto-reminders that pause when data is stale.

## Security and compliance design

Designed around HIPAA Security Rule technical safeguards (45 CFR 164.312). Design intent, not a certification.

| Control | Implementation |
|---|---|
| **Authentication** | argon2 password hashing, 12-char minimum, lockout after 5 failed tries (15 min) |
| **MFA** | TOTP (pyotp), required by default; a code cannot be replayed (last-used time step is stored) |
| **Sessions** | Server-side, token stored only as a hash; 15-minute idle sign-out, 12-hour hard cap; HttpOnly, SameSite cookies |
| **CSRF** | Per-session token checked on every form post (and a pre-login token for sign-in/sign-up forms) |
| **Role-based access** | Four roles (owner, admin, staff, read-only) mapped to explicit permissions (view, upload, scan, send, manage users, view security, ...) |
| **Tenant isolation** | Every record is fetched through the practice that owns it; tests check cross-practice access is refused |
| **Encryption at rest** | Envelope encryption: each practice has its own Fernet data key, wrapped by a master key. Patient data lives only in `*_enc` columns; TOTP secrets and SMTP passwords are encrypted too. Lookups use HMAC keyed hashes, not plaintext |
| **Key management** | Master key read from `PREVISIT_MASTER_KEY` (intended for a KMS / secret manager). For local demos only, a key is generated at runtime into the git-ignored `instance/` folder. No keys are in this repo |
| **Tamper-evident audit log** | See below |
| **Minimum necessary** | Raw uploads are dropped after mapping; reminders never name the medication; audit details hold counts and ids, never patient names |
| **Optional SSO** | Microsoft / Google OIDC with PKCE, signature, issuer and nonce checks; account pinned on first use; the app's own MFA is still required |
| **BAA gate** | Practice owner must e-sign a BAA (name, title, time, IP, SHA-256 of the text) before any upload. The included BAA is an unreviewed draft |
| **Operator console** | Support staff can manage access but have no route to patient data; their actions are logged in the practice's audit trail |
| **SIEM** | Audit events are mirrored to a JSONL feed with Wazuh rules (`wazuh/previsit_rules.xml`) mapped to HIPAA sections |

### How the tamper evidence works

`app/audit.py` keeps a **hash chain per practice**. Each event stores `prev_hash` and
`hash = SHA-256(prev_hash + canonical JSON of {practice, user, timestamp, IP, action, detail})`, starting from a
genesis value of 64 zeros. `verify_chain()` replays the chain from the start: editing any field, deleting an event, or
reordering events breaks every hash after that point, and the verifier reports the first bad event id. It runs from the
Security page or `python -m app.admin_cli verify-audit`. A test edits a stored event and checks that detection fires.
Honest limit: a hash chain detects tampering; it does not stop someone with full database access from rewriting the
whole chain. Shipping the JSONL feed to an external SIEM (Wazuh) is what anchors it.

## AI usage

**The app itself uses no AI or LLMs.** The scan is deterministic and rules-based (keyword-matched drug-to-lab rules and
date math), which is a deliberate choice for an auditable clinical-adjacent workflow. Where AI could plug in later,
with a human in the loop and no PHI sent to a model without a BAA:

- Mapping unfamiliar EHR export columns (suggest a mapping for the user to confirm).
- Normalizing messy free-text medication names to RxNorm before rule matching.
- Drafting patient-friendly reminder wording for staff approval.

The code was written with AI-assisted development (Claude Code).

## Architecture

```
EHR exports (CSV/TSV/pipe) --> office agent (PowerShell, upload-only key) or manual upload
        |
        v
FastAPI app (app/main.py, pages.py, admin.py)  <-- auth.py: sessions, MFA, CSRF, roles
        |
  importer.py + presets.py  -> column mapping -> encrypted data (crypto.py, per-practice key)
        |
  scanner.py (rules as of visit date) -> worklist.py (persistent items)
        |
  outreach.py (Excel call list, dry-run email, fax sheets)    pipeline.py / automation.py (daily scan)
        |
  audit.py: hash-chained log  -> DB + audit.jsonl -> Wazuh rules
```

Storage is SQLAlchemy (SQLite by default; Postgres via `PREVISIT_DATABASE_URL`). Server-rendered Jinja2 templates.

## Tech stack

Python 3.11, FastAPI, Uvicorn, SQLAlchemy 2, Jinja2, argon2-cffi, pyotp, cryptography (Fernet), PyJWT (OIDC),
openpyxl, qrcode, pytest + httpx. PowerShell for the office agent. Wazuh rules (XML) for SIEM.
About 3,200 lines of application Python plus about 1,000 lines of tests.

## Run the demo (fake data only)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
cp .env.example .env              # optional; defaults are safe (dry-run on)

# Build a demo practice with MFA-enabled logins (written to instance_demo/demo_accounts_FAKE_DATA_ONLY.txt)
set PREVISIT_INSTANCE_DIR=instance_demo        # PowerShell: $env:PREVISIT_INSTANCE_DIR="instance_demo"
python sample_data/make_demo_instance.py

# Start the app
python -m uvicorn app.main:app --port 8791
```

Open http://localhost:8791/login (or `/signup` to create a fresh practice). Add the two-step key from the generated
accounts file to an authenticator app. Then upload the synthetic exports from `sample_data/`, or generate fresh ones
dated around today: `python sample_data/make_fake_data.py <output-folder>`.

All sample patients are invented (surnames like "Fakerson" and "Testwell", `555-01xx` phone numbers, `example.com`
emails). Demo passwords, MFA secrets and upload keys are generated at runtime and never committed.

## Tests

```bash
python -m pytest -q
```

**58 tests pass** (Python 3.11, fresh venv). Coverage includes: EHR import presets and formats, scan accuracy, patient
grouping, worklist persistence across scans, reminders, office requests, automation, access plans, operator console,
BAA gate, Microsoft/Google sign-in, column matching, the full upload-to-scan flow, tenant isolation, CSRF, lockout,
MFA replay, idle sign-out, roles and invites, audit tamper detection, and security headers.

## What I'd do next

- Deploy on HIPAA-eligible hosting under a BAA, with Postgres, HTTPS, and the master key in a KMS.
- Add Alembic migrations, rate limiting, and a written risk assessment and policies.
- Validate the rules with a clinician and the import presets against one real practice's export layouts.
- Add the AI-assisted column mapping and medication normalization described above, behind human review.
- Containerize, add CI (tests + dependency and secret scanning), and email-based invites.

## Credits

Built by **Nikko Echevarria** with AI-assisted development (Claude Code).
