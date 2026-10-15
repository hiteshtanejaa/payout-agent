"""Cognee memory: the policy and every decision, so "why did you pay X?"
is answered from memory, not from the chat context.

Default is COGNEE_SYNC=batch: the webhook never waits on Cognee. Run
`python agent.py sync-memory` to push the policy + unsynced decisions.
Set COGNEE_SYNC=live to push each decision in a background task.
"""
import asyncio
import functools
import json
import os
import threading

from dotenv import load_dotenv

load_dotenv()  # read COGNEE_* before the constants below (agent.py imports this module first)

DATASET = os.getenv("COGNEE_DATASET", "treasurer")  # use a fresh name (e.g. treasurer_demo) for a clean memory
NOTES_DATASET = "finance_notes"
SYNCED = os.path.join(os.getenv("LEDGER_DIR", "ledger"), f".cognee_synced_{DATASET}")  # per dataset, so a new one re-sends the policy

try:
    import cognee  # noqa: F401
    try:
        from cognee.modules.search.types import SearchType
    except Exception:  # older layout
        from cognee.api.v1.search import SearchType
    HAVE_COGNEE = True
except Exception:
    HAVE_COGNEE = False


def gbp(p):
    return f"£{int(p) / 100:,.2f}"


def render_policy(policy):
    lines = [f"SPENDING POLICY '{policy['name']}' version {policy['_version']}."]
    for cid, c in policy["clauses"].items():
        lines.append(f"Clause {cid} ({c['title']}): {c['rule']}")
    return "\n".join(lines)


def render_decision(r):
    ev = r.get("evidence") or {}
    appr = ev.get("approval") or {}
    s = (
        f"DECISION {r['decision_id']} at {r['ts']}: {r['decision'].upper()} "
        f"{r.get('merchant')} {gbp(r.get('amount') or 0)} (merchant category {r.get('category')}). "
        f"Stage: {r.get('stage')}. Decided by: {r.get('decided_by')}. "
        f"Clauses applied: {', '.join(r.get('clause_ids') or []) or 'none'}. "
        f"Failed clause: {r.get('failed_clause') or 'none'}. "
    )
    if appr:
        s += f"Approved by {appr.get('approver')} with token {appr.get('token')} at {appr.get('ts')}. "
    st = r.get("stripe") or {}
    if st.get("authorization_id"):
        s += f"Stripe authorization {st['authorization_id']} on card {st.get('card_id')}. "
    if r.get("intent_id"):
        s += f"Intent {r['intent_id']}. "
    if ev.get("month_spent_after") is not None:
        s += f"Month-to-date spend after this: {gbp(ev['month_spent_after'])} of the {gbp(ev.get('monthly_max') or 0)} monthly cap. "
    s += f"Policy version {r.get('policy_version')}. Evidence (all amounts in pence): {json.dumps(ev, sort_keys=True)}"
    return s


_REMOTE_LOCK = threading.Lock()  # cognee keeps one global remote client; UI routes run in threads


def _remote(fn):
    """Run fn against Cognee Cloud when COGNEE_SERVICE_URL is set (local Cognee needs a native graph lib
    missing on Windows). Connect and close per call: each asyncio.run() is a new loop, so a client can't be reused."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        if not os.getenv("COGNEE_SERVICE_URL"):
            return await fn(*args, **kwargs)
        from cognee.api.v1.serve.state import get_remote_client, set_remote_client
        with _REMOTE_LOCK:
            await cognee.serve(url=os.environ["COGNEE_SERVICE_URL"], api_key=os.getenv("COGNEE_API_KEY", ""))
            try:
                return await fn(*args, **kwargs)
            finally:
                client = get_remote_client()
                if client:
                    await client.close()
                set_remote_client(None)
    return wrapper


async def _add(text):
    await cognee.add(text, dataset_name=DATASET)


async def _cognify():
    await cognee.cognify(datasets=[DATASET])


@_remote
async def remember_policy(policy):
    await _add(render_policy(policy))
    await _cognify()


@_remote
async def remember_decision(rec):
    await _add(render_decision(rec))
    await _cognify()


def remember_decision_sync(rec):
    """Safe to call from a FastAPI background task; never raises."""
    if not HAVE_COGNEE or os.getenv("COGNEE_SYNC", "batch") != "live":
        return
    try:
        asyncio.run(remember_decision(rec))
    except Exception as e:  # memory is best-effort; the ledger is the source of truth
        print("cognee live sync failed:", e)


def _synced_ids():
    if not os.path.exists(SYNCED):
        return set()
    with open(SYNCED) as f:
        return {line.strip() for line in f if line.strip()}


@_remote
async def sync_all(policy, records):
    """Push the policy once and every unsynced decision, then one cognify."""
    done = _synced_ids()
    if "policy:" + policy["_version"] not in done:
        await _add(render_policy(policy))
        done.add("policy:" + policy["_version"])
    n = 0
    for r in records:
        if r["decision_id"] in done:
            continue
        await _add(render_decision(r))
        done.add(r["decision_id"])
        n += 1
    await _cognify()
    os.makedirs(os.path.dirname(SYNCED), exist_ok=True)
    with open(SYNCED, "w") as f:
        f.write("\n".join(sorted(done)))
    return n


def _texts(x):
    """Flatten whatever cognee.search returns into plain strings."""
    if x is None:
        return []
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        if "search_result" in x:  # cloud results wrap the answer with dataset/tenant ids
            return _texts(x["search_result"])
        if isinstance(x.get("text"), str):
            return [x["text"]]
        return [t for v in x.values() for t in _texts(v)]
    if isinstance(x, (list, tuple, set)):
        return [t for v in x for t in _texts(v)]
    if hasattr(x, "model_dump"):
        return _texts(x.model_dump())
    if hasattr(x, "__dict__"):
        return _texts(vars(x))
    return [str(x)]


@_remote
async def remember_notes(path="notes/finance_notes.md"):
    with open(path, encoding="utf-8") as f:
        await cognee.add(f.read(), dataset_name=NOTES_DATASET)
    await cognee.cognify(datasets=[NOTES_DATASET])


@_remote
async def recall_notes(merchant):
    """Raw note chunks about a merchant (CHUNKS = retrieval only, no LLM answer)."""
    res = await cognee.search(
        query_text=f"Finance notes about {merchant}: holds, disputes, reviews, approvals",
        query_type=SearchType.CHUNKS,
        datasets=[NOTES_DATASET],
        top_k=10,
    )
    return _texts(res)


WHY_PROMPT = (
    "You explain a spending agent's decisions from its audit records. Answer in 2-4 sentences. Always cite the "
    "clause IDs (C1-C7) that applied or failed, the approver if any, the decision id (d_...) and the Stripe "
    "authorization id (iauth_...). Amounts in evidence JSON are in pence; state money in pounds. "
    "Use only facts in the records; if they don't cover the question, say so."
)


@_remote
async def why(question):
    # Decisions dataset only: searching everything lets the finance notes answer "why did you pay X?".
    # If it was never synced, search raises and agent.why falls back to the ledger.
    return _texts(await cognee.search(query_type=SearchType.GRAPH_COMPLETION, query_text=question, datasets=[DATASET],
                                      system_prompt=WHY_PROMPT))
