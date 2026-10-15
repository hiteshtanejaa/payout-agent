"""The final gate: Stripe asks "may this authorization go through?" and this
handler answers inside 2 seconds, citing the clause.

Run:  uvicorn server:app --port 8000
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


@app.get("/")
def health():
    return {"ok": True, "policy_version": POLICY["_version"], "api_version": API_VERSION}


@app.get("/decisions")
def decisions():
    return ledger.decisions()


@app.get("/intents")
def intents():
    return ledger.list_intents()
