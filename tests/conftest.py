import os
import sys
import tempfile
from pathlib import Path

# Isolated instance (DB, keys, audit file) per test run, set before the app is imported.
os.environ["PREVISIT_INSTANCE_DIR"] = tempfile.mkdtemp(prefix="previsit_test_")
os.environ["PREVISIT_DRY_RUN"] = "1"
os.environ["PREVISIT_SCHEDULER"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
