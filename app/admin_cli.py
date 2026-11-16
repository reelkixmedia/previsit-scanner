"""Server-side admin commands (run on the host, not through the website).

  python -m app.admin_cli reset-access someone@practice.com   # emergency access: temp password, MFA cleared
  python -m app.admin_cli verify-audit                         # check every practice's audit chain
  python -m app.admin_cli create-operator you@previsit.com "Your Name"   # Previsit support login (operator console)
"""
import secrets
import sys

from sqlalchemy import select

from . import audit
from .auth import end_user_sessions, hash_password
from .db import SessionLocal, init_db
from .models import Practice, User
from .services import create_operator


def reset_access(email: str) -> int:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
        if not user:
            print("No user with that email.")
            return 1
        temp = secrets.token_urlsafe(12)
        user.password_hash = hash_password(temp)
        user.mfa_enabled, user.totp_secret_enc, user.totp_last_step = False, None, 0
        user.failed_logins, user.locked_until, user.active = 0, None, True
        end_user_sessions(db, user.id)
        audit.log(db, "emergency_access_reset", practice_id=user.practice_id, user_id=user.id, ip="cli")
        db.commit()
        print(f"Temporary password for {user.email}: {temp}")
        print("They must change it under Account and set up two-step sign-in again.")
        return 0


def new_operator(email: str, name: str) -> int:
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == email.strip().lower())):
            print("That email already has an account.")
            return 1
        temp = secrets.token_urlsafe(12)
        create_operator(db, name, email.strip(), temp)
        db.commit()
    print(f"Operator {email} created. Temporary password: {temp}")
    print("Sign in at /login, set up two-step sign-in, then change the password under Account.")
    return 0


def verify_all() -> int:
    bad = 0
    with SessionLocal() as db:
        for practice_id in [None] + [p.id for p in db.scalars(select(Practice))]:
            ok, n, bad_id = audit.verify_chain(db, practice_id)
            label = "system" if practice_id is None else f"practice {practice_id}"
            print(f"{label}: {'OK' if ok else f'BROKEN at event {bad_id}'} ({n} events)")
            bad += not ok
    return 1 if bad else 0


if __name__ == "__main__":
    init_db()
    if len(sys.argv) == 3 and sys.argv[1] == "reset-access":
        sys.exit(reset_access(sys.argv[2]))
    if len(sys.argv) == 4 and sys.argv[1] == "create-operator":
        sys.exit(new_operator(sys.argv[2], sys.argv[3]))
    if len(sys.argv) == 2 and sys.argv[1] == "verify-audit":
        sys.exit(verify_all())
    print(__doc__)
    sys.exit(2)
