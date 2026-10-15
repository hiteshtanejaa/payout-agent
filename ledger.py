"""Intent state and an append-only decision ledger.

Local runs use JSON/JSONL files. DATABASE_URL selects persistent PostgreSQL
storage, so a hosted demo retains its history across dyno restarts.
"""
import json
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DIR = os.getenv("LEDGER_DIR", "ledger")
INTENTS = os.path.join(DIR, "intents.json")
DECISIONS = os.path.join(DIR, "decisions.jsonl")
PROCESSED_INBOX = os.path.join(DIR, "processed_inbox.json")
_LOCK = threading.RLock()
_DB_READY_URL = None


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connection():
    """One transaction per operation; load psycopg only for hosted storage."""
    global _DB_READY_URL
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("DATABASE_URL requires the psycopg[binary] dependency") from exc
    url = os.environ["DATABASE_URL"]
    with psycopg.connect(url, connect_timeout=5) as conn:
        with _LOCK:
            if _DB_READY_URL != url:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS treasurer_intents (
                        sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        id TEXT UNIQUE NOT NULL,
                        payload JSONB NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS treasurer_decisions (
                        sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        payload JSONB NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS treasurer_processed_inbox (
                        filename TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        processed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (filename, fingerprint)
                    )
                """)
                conn.commit()
                _DB_READY_URL = url
        yield conn


def _save_json(path, data):
    """Replace the complete JSON file so readers never see a partial write."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DIR,
                                         prefix=".ledger-", suffix=".tmp", delete=False) as f:
            temporary = f.name
            json.dump(data, f, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _ensure():
    with _LOCK:
        os.makedirs(DIR, exist_ok=True)
        for path in (INTENTS, PROCESSED_INBOX):
            if not os.path.exists(path):
                _save_json(path, {})
        with open(DECISIONS, "a", encoding="utf-8"):
            pass


def ensure_storage():
    """Initialize/validate the selected store before serving requests."""
    if os.getenv("DATABASE_URL"):
        with _connection():
            pass
    else:
        _ensure()


def _load_intents():
    _ensure()
    with open(INTENTS, encoding="utf-8") as f:
        return json.load(f)


def new_intent(**fields):
    iid = "int_" + uuid.uuid4().hex[:8]
    intent = {
        "id": iid,
        "ts": now(),
        "status": "awaiting_approval",
        "approval": None,
        "authorization_id": None,
        "proposal_decision_id": None,
        **fields,
    }
    if os.getenv("DATABASE_URL"):
        with _connection() as conn:
            conn.execute("INSERT INTO treasurer_intents (id, payload) VALUES (%s, %s::jsonb)",
                         (iid, json.dumps(intent)))
    else:
        with _LOCK:
            intents = _load_intents()
            intents[iid] = intent
            _save_json(INTENTS, intents)
    return intent


def get_intent(iid):
    if os.getenv("DATABASE_URL"):
        with _connection() as conn:
            row = conn.execute("SELECT payload FROM treasurer_intents WHERE id = %s", (iid,)).fetchone()
            return row[0] if row else None
    with _LOCK:
        return _load_intents().get(iid)


def update_intent(iid, **fields):
    if os.getenv("DATABASE_URL"):
        with _connection() as conn:
            row = conn.execute("SELECT payload FROM treasurer_intents WHERE id = %s FOR UPDATE",
                               (iid,)).fetchone()
            if row is None:
                raise KeyError(iid)
            intent = row[0]
            intent.update(fields)
            conn.execute("UPDATE treasurer_intents SET payload = %s::jsonb WHERE id = %s",
                         (json.dumps(intent), iid))
            return intent
    with _LOCK:
        intents = _load_intents()
        intents[iid].update(fields)
        _save_json(INTENTS, intents)
        return intents[iid]


def list_intents(status=None):
    if os.getenv("DATABASE_URL"):
        with _connection() as conn:
            items = [row[0] for row in conn.execute(
                "SELECT payload FROM treasurer_intents ORDER BY sequence").fetchall()]
    else:
        with _LOCK:
            items = list(_load_intents().values())
    return [i for i in items if status is None or i["status"] == status]


def find_open_intent(merchant, amount, tolerance):
    """A cleared intent for this merchant whose amount is within tolerance."""
    for it in list_intents("cleared"):
        if it["merchant"].strip().lower() != (merchant or "").strip().lower():
            continue
        if abs(it["amount"] - amount) <= it["amount"] * tolerance:
            return it
    return None


def record_decision(**rec):
    rec = {"decision_id": "d_" + uuid.uuid4().hex[:8], "ts": now(), **rec}
    if os.getenv("DATABASE_URL"):
        with _connection() as conn:
            conn.execute("INSERT INTO treasurer_decisions (payload) VALUES (%s::jsonb)",
                         (json.dumps(rec),))
    else:
        with _LOCK:
            _ensure()
            with open(DECISIONS, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
    return rec


def decisions():
    if os.getenv("DATABASE_URL"):
        with _connection() as conn:
            return [row[0] for row in conn.execute(
                "SELECT payload FROM treasurer_decisions ORDER BY sequence").fetchall()]
    with _LOCK:
        _ensure()
        with open(DECISIONS, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


def month_spent(when=None):
    """Pence approved at authorization stage this calendar month."""
    when = when or datetime.now(timezone.utc)
    ym = when.strftime("%Y-%m")
    return sum(int(r.get("amount") or 0) for r in decisions()
               if r.get("stage") == "authorization" and r.get("decision") == "approve"
               and r["ts"].startswith(ym))


def inbox_processed(filename, fingerprint):
    """Whether this exact inbox file was handled in an earlier run."""
    if os.getenv("DATABASE_URL"):
        with _connection() as conn:
            return conn.execute("""
                SELECT 1 FROM treasurer_processed_inbox
                WHERE filename = %s AND fingerprint = %s
            """, (filename, fingerprint)).fetchone() is not None
    with _LOCK:
        _ensure()
        with open(PROCESSED_INBOX, encoding="utf-8") as f:
            return fingerprint in json.load(f).get(filename, [])


def mark_inbox_processed(filename, fingerprint):
    if os.getenv("DATABASE_URL"):
        with _connection() as conn:
            conn.execute("""
                INSERT INTO treasurer_processed_inbox (filename, fingerprint)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
            """, (filename, fingerprint))
    else:
        with _LOCK:
            _ensure()
            with open(PROCESSED_INBOX, encoding="utf-8") as f:
                processed = json.load(f)
            fingerprints = processed.setdefault(filename, [])
            if fingerprint not in fingerprints:
                fingerprints.append(fingerprint)
                _save_json(PROCESSED_INBOX, processed)
