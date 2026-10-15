"""Initialize persistent demo tables during Heroku's release phase."""
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ledger

if not os.getenv("DATABASE_URL"):
    raise SystemExit("Attach PostgreSQL before deploying: DATABASE_URL is required on Heroku.")
ledger.ensure_storage()
print("Treasurer persistent storage is ready.")
