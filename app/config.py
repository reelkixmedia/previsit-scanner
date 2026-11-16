"""Settings, read from environment variables (PREVISIT_*). Safe defaults: dry-run on."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = Path(os.getenv("PREVISIT_INSTANCE_DIR", BASE_DIR / "instance"))
INSTANCE_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("PREVISIT_DATABASE_URL", f"sqlite:///{(INSTANCE_DIR / 'previsit.db').as_posix()}")

# Outreach safety: nothing is emailed unless PREVISIT_DRY_RUN=0. When an allowlist is set,
# real sends only go to those addresses.
DRY_RUN = os.getenv("PREVISIT_DRY_RUN", "1") != "0"
EMAIL_ALLOWLIST = {e.strip().lower() for e in os.getenv("PREVISIT_EMAIL_ALLOWLIST", "").split(",") if e.strip()}

SECURE_COOKIES = os.getenv("PREVISIT_SECURE_COOKIES", "0") == "1"  # set to 1 behind HTTPS
SESSION_IDLE_MINUTES = int(os.getenv("PREVISIT_SESSION_IDLE_MINUTES", "15"))
SESSION_MAX_HOURS = 12
LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 15
INVITE_HOURS = 72
MIN_PASSWORD_LEN = 12
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

AUDIT_JSONL = INSTANCE_DIR / "audit.jsonl"  # for a Wazuh agent (see wazuh/)

SCHEDULER = os.getenv("PREVISIT_SCHEDULER", "1") != "0"  # daily scan loop; run in one process only
STALE_DATA_DAYS = 2  # warn when the newest export is older than this
