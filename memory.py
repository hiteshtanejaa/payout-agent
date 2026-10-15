"""Cognee memory: the policy and every decision, so "why did you pay X?"
is answered from memory, not from the chat context.

Default is COGNEE_SYNC=batch: the webhook never waits on Cognee. Run
`python agent.py sync-memory` to push the policy + unsynced decisions.
Set COGNEE_SYNC=live to push each decision in a background task.
"""
import asyncio
import json
import os

DATASET = "treasurer"
NOTES_DATASET = "finance_notes"
SYNCED = os.path.join(os.getenv("LEDGER_DIR", "ledger"), ".cognee_synced")

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
    s += f"Policy version {r.get('policy_version')}. Evidence: {json.dumps(ev, sort_keys=True)}"
    return s


async def _add(text):
    await cognee.add(text, dataset_name=DATASET)


async def _cognify():
    await cognee.cognify(datasets=[DATASET])


async def remember_policy(policy):
    await _add(render_policy(policy))
    await _cognify()


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


async def remember_notes(path="notes/finance_notes.md"):
    with open(path, encoding="utf-8") as f:
        await cognee.add(f.read(), dataset_name=NOTES_DATASET)
    await cognee.cognify(datasets=[NOTES_DATASET])


async def recall_notes(merchant):
    """Raw note chunks about a merchant (CHUNKS = retrieval only, no LLM answer)."""
    res = await cognee.search(
        query_text=f"Finance notes about {merchant}: holds, disputes, reviews, approvals",
        query_type=SearchType.CHUNKS,
        datasets=[NOTES_DATASET],
        top_k=10,
    )
    return _texts(res)


async def why(question):
    return await cognee.search(query_type=SearchType.GRAPH_COMPLETION, query_text=question)
