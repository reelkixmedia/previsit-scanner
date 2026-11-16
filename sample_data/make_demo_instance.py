"""Builds a FAKE-DATA demo: one practice on Managed access (BAA not yet signed) with an open access request,
plus a Previsit support (operator) login. Logins and two-step keys are written to
<instance>/demo_accounts_FAKE_DATA_ONLY.txt.

Run:  $env:PREVISIT_INSTANCE_DIR="instance_demo"; .venv\\Scripts\\python sample_data\\make_demo_instance.py
"""
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyotp  # noqa: E402

from app import config, crypto  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.auth import new_token, sha  # noqa: E402
from app.models import AccessRequest, Practice, User, utcnow  # noqa: E402
from app.services import create_operator, create_practice, invite_user  # noqa: E402


def with_mfa(db, user: User) -> str:
    secret = pyotp.random_base32()
    user.totp_secret_enc = crypto.encrypt_text(db.get(Practice, user.practice_id), secret)
    user.mfa_enabled = True
    return secret


def main():
    init_db()
    lines = ["FAKE DATA ONLY. Add each two-step key to an authenticator app (or use pyotp).", ""]
    with SessionLocal() as db:
        pw = "Demo-" + secrets.token_urlsafe(12)
        owner = create_practice(db, "Demo Family Clinic", "Dana Owner", "owner@demo-clinic.test", pw,
                                ip="demo", iam_plan="managed")
        lines += ["Practice owner (Demo Family Clinic, Managed access)", "email: owner@demo-clinic.test",
                  f"password: {pw}", f"two-step key: {with_mfa(db, owner)}", ""]
        db.flush()
        practice = db.get(Practice, owner.practice_id)
        practice.baa_version, practice.baa_text_sha256 = "v0-draft", "demo"
        practice.baa_signed_name, practice.baa_signed_title = "Dana Owner", "Practice Manager"
        practice.baa_signed_by, practice.baa_signed_at, practice.baa_signed_ip = owner.id, utcnow(), "demo"
        practice.auto_scan = True
        key = "pvk_" + new_token()
        practice.api_key_hash = sha(key)
        lines += [f"Upload key for the agent: {key}", ""]
        staff, _ = invite_user(db, db.get(Practice, owner.practice_id), owner, "demo", "Sam Frontdesk",
                               "sam@demo-clinic.test", "staff")
        staff.password_hash, staff.invite_token_hash, staff.invite_expires_at = owner.password_hash, None, None
        db.add(AccessRequest(practice_id=owner.practice_id, requested_by=owner.id, kind="add",
                             target_email="nia@demo-clinic.test", target_name="Nia Newhire", role="staff",
                             note="Starts Monday at the front desk"))
        op_pw = "Op-" + secrets.token_urlsafe(14)
        op = create_operator(db, "Demo Operator", "operator@previsit-demo.test", op_pw)
        lines += ["Previsit support (you): operator console", "email: operator@previsit-demo.test", f"password: {op_pw}",
                  f"two-step key: {with_mfa(db, op)}"]
        db.commit()
    out = config.INSTANCE_DIR / "demo_accounts_FAKE_DATA_ONLY.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
