"""The final gate: Stripe asks "may this authorization go through?" and this
handler answers inside 2 seconds, citing the clause.

Run:  uvicorn server:app --port 8000      (finance UI at http://localhost:8000/)
      stripe listen --forward-to localhost:8000/stripe/webhook

Nothing slow happens before the response (no LLM, no Cognee). Memory sync is
a background task after the JSON is already on its way back to Stripe.
"""
import os

import stripe
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

import ledger
import memory
from policy import evaluate, load_policy

load_dotenv()
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
API_VERSION = os.getenv("STRIPE_API_VERSION") or stripe.api_version
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
POLICY = load_policy()

# Reasons Stripe gives when IT declined, before our code ran.
STRIPE_DECLINE_CLAUSE = {
    "spending_controls": "C1/C2/C4 (card spending_controls, enforced by Stripe)",
    "authorization_controls": "C1/C2/C4 (card spending_controls, enforced by Stripe)",  # name in newer API versions
    "insufficient_funds": "issuing balance (fund it in Dashboard > Balances, test mode)",
    "webhook_timeout": "webhook did not answer within 2s",
    "webhook_error": "webhook returned an error",
    "card_inactive": "card inactive",
    "cardholder_inactive": "cardholder inactive",
}

app = FastAPI(title="Treasurer gate")


def _id(x):
    return x["id"] if isinstance(x, dict) else x


def decide_authorization(auth: dict):
    """Given an issuing_authorization.request object, return (response_body, decision_record)."""
    pr = auth.get("pending_request") or {}
    amount = int(pr.get("amount") or auth.get("amount") or 0)
    currency = pr.get("currency") or auth.get("currency")
    md = auth.get("merchant_data") or {}
    merchant = md.get("name") or ""
    category = md.get("category") or ""

    tolerance = POLICY["clauses"]["C5"]["amount_tolerance"]
    intent = ledger.find_open_intent(merchant, amount, tolerance)
    result = evaluate(
        POLICY,
        amount=amount,
        category=category,
        merchant=merchant,
        month_spent=ledger.month_spent(),
        approval=(intent or {}).get("approval"),
        intent=intent,
        stage="authorization",
    )
    rec = ledger.record_decision(
        stage="authorization",
        decided_by="treasurer_webhook",
        merchant=merchant,
        category=category,
        amount=amount,
        currency=currency,
        intent_id=intent["id"] if intent else None,
        stripe={"authorization_id": auth.get("id"), "card_id": _id(auth.get("card"))},
        **result,
    )
    approved = result["decision"] == "approve"
    if intent:
        ledger.update_intent(intent["id"], status="executed" if approved else "declined", authorization_id=auth.get("id"))
    body = {
        "approved": approved,
        "metadata": {
            "decision_id": rec["decision_id"],
            "clause_ids": ",".join(result["clause_ids"]),
            "failed_clause": result["failed_clause"] or "",
            "intent_id": intent["id"] if intent else "",
            "approver": ((intent or {}).get("approval") or {}).get("approver") or "",
            "policy_version": result["policy_version"],
        },
    }
    return body, rec


def record_stripe_decline(auth: dict):
    """Stripe said no before our webhook was consulted. Log it so the trail is complete."""
    hist = auth.get("request_history") or []
    reason = (hist[-1].get("reason") if hist else None) or "unknown"
    if reason == "webhook_declined":
        return None  # already logged by decide_authorization
    md = auth.get("merchant_data") or {}
    return ledger.record_decision(
        stage="authorization",
        decided_by="stripe:" + reason,
        merchant=md.get("name") or "",
        category=md.get("category") or "",
        amount=int(auth.get("amount") or 0),
        currency=auth.get("currency"),
        intent_id=None,
        decision="decline",
        clause_ids=[],
        failed_clause=STRIPE_DECLINE_CLAUSE.get(reason, reason),
        evidence={"stage": "authorization", "stripe_reason": reason, "request_history": hist},
        policy_version=POLICY["_version"],
        stripe={"authorization_id": auth.get("id"), "card_id": _id(auth.get("card"))},
    )


@app.post("/stripe/webhook")
async def webhook(request: Request, bg: BackgroundTasks):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), WEBHOOK_SECRET)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    etype = event["type"]
    obj = event["data"]["object"].to_dict()  # stripe 16 objects are not dicts

    if etype == "issuing_authorization.request":
        body, rec = decide_authorization(obj)
        bg.add_task(memory.remember_decision_sync, rec)
        print(f"[gate] {rec['decision'].upper():8} {rec['merchant']} {rec['amount']} clauses={body['metadata']['clause_ids']} failed={rec['failed_clause']}")
        return JSONResponse(body, headers={"Stripe-Version": API_VERSION})

    if etype == "issuing_authorization.created" and not obj.get("approved"):
        rec = record_stripe_decline(obj)
        if rec:
            bg.add_task(memory.remember_decision_sync, rec)
            print(f"[stripe] DECLINED {rec['merchant']} {rec['amount']} reason={rec['decided_by']}")

    if etype == "issuing_transaction.created":
        ledger.record_decision(
            stage="capture",
            decided_by="stripe",
            merchant=(obj.get("merchant_data") or {}).get("name") or "",
            category=(obj.get("merchant_data") or {}).get("category") or "",
            amount=abs(int(obj.get("amount") or 0)),
            currency=obj.get("currency"),
            intent_id=None,
            decision="captured",
            clause_ids=[],
            failed_clause=None,
            evidence={"stage": "capture"},
            policy_version=POLICY["_version"],
            stripe={"transaction_id": obj.get("id"), "authorization_id": _id(obj.get("authorization")), "card_id": _id(obj.get("card"))},
        )

    return JSONResponse({"received": True})


@app.get("/health")
def health():
    return {"ok": True, "policy_version": POLICY["_version"], "api_version": API_VERSION}


@app.get("/decisions")
def decisions():
    return ledger.decisions()


@app.get("/intents")
def intents():
    return ledger.list_intents()


# ---------------------------------------------------------------- finance UI (ui/index.html)
# Thin layer over the same functions the CLI uses. Routes are plain `def`, so FastAPI runs
# them in a worker thread and the async Stripe webhook above can still answer while
# agent.execute() is waiting on the test-helper authorization.
import asyncio as _asyncio
import contextlib as _contextlib
import glob as _glob
import io as _io
import sys as _sys
import threading as _threading

from fastapi.responses import FileResponse

import agent
from pydantic import BaseModel

_ACTION_LOCK = _threading.Lock()
_CARD_CACHE = {}


class _Tee(_io.TextIOBase):
    """Keep printing to the server terminal while also capturing for the UI."""

    def __init__(self, a, b):
        self.a, self.b = a, b

    def write(self, s):
        self.a.write(s)
        self.b.write(s)
        return len(s)

    def flush(self):
        self.a.flush()


def _run(fn, *args, **kwargs):
    buf = _io.StringIO()
    with _ACTION_LOCK, _contextlib.redirect_stdout(_Tee(_sys.__stdout__, buf)):
        try:
            result = fn(*args, **kwargs)
        except SystemExit as e:  # agent.py uses sys.exit for "not cleared" etc.
            print(f"✗ {e}")
            result = None
    return result, buf.getvalue()


def _card():
    if not _CARD_CACHE:
        try:
            st = agent.state()
            _CARD_CACHE.update(id=st.get("card_id"))
            card = stripe.issuing.Card.retrieve(st["card_id"]).to_dict()
            _CARD_CACHE.update(last4=card.get("last4"), status=card.get("status"))
        except Exception as e:  # the page still works without the card details
            _CARD_CACHE.setdefault("error", str(e)[:200])
    return dict(_CARD_CACHE)


@app.get("/")
def ui_page():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "index.html"))


@app.get("/api/state")
def ui_state():
    c = POLICY["clauses"]
    return {
        "policy": {
            "version": POLICY["_version"],
            "clauses": {k: {"title": v["title"], "rule": v["rule"]} for k, v in c.items()},
            "per_transaction_max": c["C1"]["per_transaction_max"],
            "allowed_categories": c["C2"]["allowed_categories"],
            "escalate_above": c["C3"]["escalate_above"],
            "approvers": c["C3"]["approvers"],
            "monthly_max": c["C4"]["monthly_max"],
        },
        "card": _card(),
        "month_spent": ledger.month_spent(),
        "inbox_waiting": len(_glob.glob("inbox/*.txt")),
        "intents": ledger.list_intents(),
        "decisions": ledger.decisions(),
        "busy": _ACTION_LOCK.locked(),
    }


def _inbox_preview(path):
    """Enough of an email to recognise it: sender, subject, and a short snippet of the body."""
    text = open(path, encoding="utf-8", errors="replace").read()
    head, body = {}, []
    for line in text.splitlines():
        k, _, v = line.partition(":")
        if v and k.strip().lower() in ("from", "to", "subject", "merchant", "category", "amount", "reason"):
            head.setdefault(k.strip().lower(), v.strip())
        elif line.strip():
            body.append(line.strip())
    subject = head.get("subject") or (f"{head['merchant']} · £{head.get('amount', '?')}" if "merchant" in head else "")
    snippet = " ".join(body) or head.get("reason", "")
    return {"file": os.path.basename(path), "from": head.get("from", "Purchase form" if "merchant" in head else ""),
            "subject": subject or os.path.basename(path), "snippet": snippet[:140]}


@app.get("/api/inbox")
def ui_inbox_list():
    return [_inbox_preview(p) for p in sorted(_glob.glob("inbox/*.txt"))]


class InboxBody(BaseModel):
    files: list[str] | None = None   # None = everything in inbox/


@app.post("/api/inbox")
def ui_process_inbox(body: InboxBody | None = None):
    # interactive=False: escalations stay pending for the Approve button instead of input()
    only = body.files if body else None
    _, log = _run(agent.inbox, None, interactive=False, only=only)
    return {"log": log}


class ApproveBody(BaseModel):
    approver: str


@app.post("/api/intents/{intent_id}/approve")
def ui_approve(intent_id: str, body: ApproveBody):
    def go():
        res, _ = agent.approve(intent_id, body.approver.strip())
        if res["decision"] == "approve":
            print(f"✓ approved by {body.approver} → clause C3 satisfied")
            agent.execute(intent_id)
        else:
            print(f"✗ still {res['decision']}: {agent.clause_text(res['failed_clause'])}")
        return res

    res, log = _run(go)
    return {"decision": (res or {}).get("decision"), "failed_clause": (res or {}).get("failed_clause"), "log": log}


def _why_records(question):
    """The ledger rows a "why" question is about, so the UI can lay them out (same matching as the CLI)."""
    q, ds = question.lower(), ledger.decisions()
    hits = [d for d in ds if d.get("stage") != "capture" and d.get("merchant") and d["merchant"].lower() in q]
    if not hits:
        hits = [d for d in ds if d.get("stage") != "capture"][-3:]
    auths = {(d.get("stripe") or {}).get("authorization_id") for d in hits} - {None}
    keep = {d["decision_id"] for d in hits}
    return [d for d in ds if d["decision_id"] in keep or
            (d.get("stage") == "capture" and (d.get("stripe") or {}).get("authorization_id") in auths)]


class WhyBody(BaseModel):
    question: str


@app.post("/api/why")
def ui_why(body: WhyBody):
    out = {"ledger": agent.explain_from_ledger(body.question), "records": _why_records(body.question),
           "memory": None, "memory_error": None}
    if memory.HAVE_COGNEE:
        try:
            ans = _asyncio.run(memory.why(body.question))
            out["memory"] = "\n".join(memory._texts(ans)) or None
        except Exception as e:
            out["memory_error"] = f"{type(e).__name__}: {e}"[:300]
    return out
