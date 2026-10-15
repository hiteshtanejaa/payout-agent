"""Append-only ledger.

intents.json   - what the agent proposed, and its status
decisions.jsonl - every policy decision, in order, never edited
"""
import json
import os
import uuid
from datetime import datetime, timezone

DIR = os.getenv("LEDGER_DIR", "ledger")
INTENTS = os.path.join(DIR, "intents.json")
DECISIONS = os.path.join(DIR, "decisions.jsonl")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure():
    os.makedirs(DIR, exist_ok=True)
    if not os.path.exists(INTENTS):
        with open(INTENTS, "w") as f:
            json.dump({}, f)
    if not os.path.exists(DECISIONS):
        open(DECISIONS, "a").close()


def _load_intents():
    _ensure()
    with open(INTENTS) as f:
        return json.load(f)


def _save_intents(d):
    with open(INTENTS, "w") as f:
        json.dump(d, f, indent=2)


def new_intent(**fields):
    d = _load_intents()
    iid = "int_" + uuid.uuid4().hex[:8]
    d[iid] = {
        "id": iid,
        "ts": now(),
        "status": "awaiting_approval",
        "approval": None,
        "authorization_id": None,
        "proposal_decision_id": None,
        **fields,
    }
    _save_intents(d)
    return d[iid]


def get_intent(iid):
    return _load_intents().get(iid)


def update_intent(iid, **fields):
    d = _load_intents()
    d[iid].update(fields)
    _save_intents(d)
    return d[iid]


def list_intents(status=None):
    items = list(_load_intents().values())
    return [i for i in items if status is None or i["status"] == status]


def find_open_intent(merchant, amount, tolerance):
    """A cleared intent for this merchant whose amount is within tolerance."""
    for it in _load_intents().values():
        if it["status"] != "cleared":
            continue
        if it["merchant"].strip().lower() != (merchant or "").strip().lower():
            continue
        if abs(it["amount"] - amount) <= it["amount"] * tolerance:
            return it
    return None


def record_decision(**rec):
    _ensure()
    rec = {"decision_id": "d_" + uuid.uuid4().hex[:8], "ts": now(), **rec}
    with open(DECISIONS, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def decisions():
    _ensure()
    with open(DECISIONS) as f:
        return [json.loads(line) for line in f if line.strip()]


def month_spent(when=None):
    """Pence approved at authorization stage this calendar month."""
    when = when or datetime.now(timezone.utc)
    ym = when.strftime("%Y-%m")
    total = 0
    for r in decisions():
        if r.get("stage") == "authorization" and r.get("decision") == "approve" and r["ts"].startswith(ym):
            total += int(r.get("amount") or 0)
    return total
