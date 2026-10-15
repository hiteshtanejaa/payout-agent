"""The final gate: Stripe asks "may this authorization go through?" and this
handler answers inside 2 seconds, citing the clause.

Run:  uvicorn server:app --port 8000      (finance UI at http://localhost:8000/)
      stripe listen --forward-to localhost:8000/stripe/webhook

Nothing slow happens before the response (no LLM, no Cognee). Memory sync is
a background task after the JSON is already on its way back to Stripe.
"""
import os
import base64
import binascii
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import stripe
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

import ledger
import memory
from policy import evaluate, load_policy

load_dotenv()
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
API_VERSION = os.getenv("STRIPE_API_VERSION") or stripe.api_version
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
WEBHOOK_SECRETS = [WEBHOOK_SECRET]
if os.getenv("STRIPE_ISSUING_WEBHOOK_SECRET"):
    WEBHOOK_SECRETS.append(os.environ["STRIPE_ISSUING_WEBHOOK_SECRET"])
POLICY = load_policy()
DEMO_USERNAME = os.getenv("DEMO_USERNAME", "demo")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "")


def validate_hosted_config():
    if not stripe.api_key.startswith(("sk_test_", "rk_test_")):
        raise RuntimeError("Treasurer requires a Stripe test-mode API key.")
    if os.getenv("DYNO"):
        required = [name for name in ("DATABASE_URL", "DEMO_PASSWORD", "STRIPE_CARD_ID") if not os.getenv(name)]
        if required:
            raise RuntimeError("Missing hosted configuration: " + ", ".join(required))
        if os.getenv("MEMORY_CHECK", "cognee") == "cognee":
            if not os.getenv("COGNEE_SERVICE_URL") or not memory.HAVE_COGNEE:
                raise RuntimeError("Hosted Cognee mode requires COGNEE_SERVICE_URL and the Cognee package.")


@asynccontextmanager
async def lifespan(app):
    validate_hosted_config()
    ledger.ensure_storage()
    yield

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

app = FastAPI(title="Treasurer gate", lifespan=lifespan)
_WEBHOOK_LOCK = threading.RLock()


@app.middleware("http")
async def protect_demo(request: Request, call_next):
    if request.url.path in ("/stripe/webhook", "/health"):
        return await call_next(request)
    if DEMO_PASSWORD:
        valid = False
        try:
            scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
            name, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
            valid = (scheme.lower() == "basic"
                     and secrets.compare_digest(name.encode(), DEMO_USERNAME.encode())
                     and secrets.compare_digest(password.encode(), DEMO_PASSWORD.encode()))
        except (ValueError, UnicodeError, binascii.Error):
            pass
        if not valid:
            return JSONResponse({"detail": "Demo login required"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Treasurer demo"', "Cache-Control": "no-store"})
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
        origin_url = urlsplit(origin)
        if (origin_url.scheme, origin_url.netloc) != (request.url.scheme, request.url.netloc):
            return JSONResponse({"detail": "Use the Treasurer demo page to submit this action."}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def _id(x):
    return x["id"] if isinstance(x, dict) else x


def decide_authorization(auth: dict, event_id=None):
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
    if (intent or {}).get("approval"):
        result["evidence"]["approval"] = intent["approval"]
    rec = ledger.record_decision(
        stage="authorization",
        decided_by="treasurer_webhook",
        merchant=merchant,
        category=category,
        amount=amount,
        currency=currency,
        intent_id=intent["id"] if intent else None,
        stripe_event_id=event_id,
        stripe={"authorization_id": auth.get("id"), "card_id": _id(auth.get("card"))},
        **result,
    )
    approved = result["decision"] == "approve"
    if intent:
        ledger.update_intent(intent["id"], status="executed" if approved else "declined", authorization_id=auth.get("id"))
    return _authorization_body(rec), rec


def _authorization_body(rec):
    """Reconstruct the exact response from immutable audit evidence, including on retries."""
    return {
        "approved": rec["decision"] == "approve",
        "metadata": {
            "decision_id": rec["decision_id"],
            "clause_ids": ",".join(rec["clause_ids"]),
            "failed_clause": rec["failed_clause"] or "",
            "intent_id": rec.get("intent_id") or "",
            "approver": ((rec.get("evidence") or {}).get("approval") or {}).get("approver") or "",
            "policy_version": rec["policy_version"],
        },
    }


def record_stripe_decline(auth: dict, event_id=None):
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
        stripe_event_id=event_id,
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
    event = None
    for secret in WEBHOOK_SECRETS:
        try:
            event = stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), secret)
            break
        except (ValueError, stripe.SignatureVerificationError):
            continue
    if event is None or getattr(event, "livemode", False):
        return JSONResponse({"error": "Invalid test-mode webhook"}, status_code=400)

    etype = event["type"]
    obj = event["data"]["object"].to_dict()  # stripe 16 objects are not dicts

    with _WEBHOOK_LOCK:
        previous = next((r for r in ledger.decisions() if r.get("stripe_event_id") == event["id"]), None)
        if previous:
            if etype == "issuing_authorization.request":
                # Replay the original response, including metadata, rather than spending/counting twice.
                return JSONResponse(_authorization_body(previous), headers={"Stripe-Version": API_VERSION})
            return JSONResponse({"received": True})

        if etype == "issuing_authorization.request":
            body, rec = decide_authorization(obj, event_id=event["id"])
            bg.add_task(memory.remember_decision_sync, rec)
            print(f"[gate] {rec['decision'].upper():8} {rec['merchant']} {rec['amount']} clauses={body['metadata']['clause_ids']} failed={rec['failed_clause']}")
            return JSONResponse(body, headers={"Stripe-Version": API_VERSION})

        if etype == "issuing_authorization.created" and not obj.get("approved"):
            rec = record_stripe_decline(obj, event_id=event["id"])
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
                stripe_event_id=event["id"],
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
    return {"ok": True, "policy_version": POLICY["_version"], "api_version": API_VERSION,
            "storage": "postgres" if os.getenv("DATABASE_URL") else "file", "test_mode": True}


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
_JOB_LOCK = _threading.RLock()
_JOBS = {}
_JOB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="treasurer-demo")


def _submit(fn):
    """One demo action at a time; return before the Heroku router's 30-second deadline."""
    with _JOB_LOCK:
        if any(j["status"] == "running" for j in _JOBS.values()):
            raise HTTPException(409, "Another action is processing. Wait for it to finish.")
        for key in list(_JOBS)[:-49]:
            del _JOBS[key]
        job_id = "job_" + uuid.uuid4().hex
        _JOBS[job_id] = {"id": job_id, "status": "running", "result": None, "log": ""}

    def go():
        try:
            result, log = _run(fn)
            with _JOB_LOCK:
                _JOBS[job_id].update(status="complete", result=result, log=log)
        except Exception:
            import traceback
            traceback.print_exc()
            with _JOB_LOCK:
                _JOBS[job_id].update(status="error", error="Processing stopped. Review the ledger before trying again.")

    _JOB_EXECUTOR.submit(go)
    return JSONResponse({"job_id": job_id, "status": "running", "log": ""}, status_code=202)


@app.get("/api/jobs/{job_id}")
def ui_job(job_id: str):
    with _JOB_LOCK:
        if job_id not in _JOBS:
            raise HTTPException(404, "Processing was interrupted. Review the ledger before submitting again.")
        return dict(_JOBS[job_id])


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
        "inbox_waiting": len(agent.inbox_files()),
        "intents": ledger.list_intents(),
        "decisions": ledger.decisions(),
        "busy": _ACTION_LOCK.locked() or any(j["status"] == "running" for j in list(_JOBS.values())),
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
    return [_inbox_preview(p) for p in agent.inbox_files()]


class InboxBody(BaseModel):
    files: list[str] | None = None   # None = everything in inbox/


@app.post("/api/inbox")
def ui_process_inbox(body: InboxBody | None = None):
    # interactive=False: escalations stay pending for the Approve button instead of input()
    only = body.files if body else None
    return _submit(lambda: agent.inbox(None, interactive=False, only=only))


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

    return _submit(go)


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
    return _submit(lambda: _answer_why(body.question))


def _answer_why(question):
    out = {"ledger": agent.explain_from_ledger(question), "records": _why_records(question),
           "memory": None, "memory_error": None}
    if memory.HAVE_COGNEE:
        try:
            ans = _asyncio.run(memory.why(question))
            out["memory"] = "\n".join(memory._texts(ans)) or None
        except Exception as e:
            out["memory_error"] = f"{type(e).__name__}: {e}"[:300]
    return out
