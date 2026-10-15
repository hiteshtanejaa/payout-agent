#!/usr/bin/env python3
"""Treasurer: a buying agent that can only spend what the policy says.

The LLM proposes.  policy.evaluate() decides.  server.py's webhook is the final gate.
The model never calls a Stripe write API; its only tool is propose_purchase().

  python agent.py inbox                       # process requests dropped in inbox/ (autonomous trigger)
  python agent.py buy --merchant Figma --category computer_software_stores --amount 120 --desc "Figma seat"
  python agent.py chat "Buy a Figma Professional seat for £120"   # LLM mode (OpenAI Agents SDK)
  python agent.py pending                     # intents waiting for a human
  python agent.py approve int_xxxx --approver sarthak
  python agent.py execute int_xxxx            # use the card (test helper authorization + capture)
  python agent.py why "Why did you pay Google Ads £350?"
  python agent.py ingest-notes                # put notes/finance_notes.md into Cognee (C7 recalls from it)
  python agent.py sync-memory                 # push policy + decisions to Cognee
"""
import argparse
import asyncio
import glob
import json
import os
import shutil
import sys
import uuid

import stripe
from dotenv import load_dotenv

import ledger
import extractor
import memory
import recall
from policy import evaluate, load_policy

load_dotenv()
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
POLICY = load_policy()
CURRENCY = POLICY.get("currency", "gbp")
STATE_PATH = os.path.join(ledger.DIR, "stripe_state.json")


def state():
    with open(STATE_PATH) as f:
        return json.load(f)


def gbp(p):
    return f"£{int(p) / 100:,.2f}"


def clause_text(cid):
    c = POLICY["clauses"].get(cid.split(" ")[0]) if cid else None
    return f"{cid} — {c['title']}: {c['rule'].rstrip('.')}" if c else str(cid)


# ---------------------------------------------------------------- core flow

STATUS = {"approve": "cleared", "escalate": "awaiting_approval", "hold": "held", "decline": "declined"}


def propose(merchant, category, amount, description, origin=None):
    mem = recall.check(merchant, POLICY)  # C7: what does memory say about this merchant?
    res = evaluate(POLICY, amount=amount, category=category, merchant=merchant, month_spent=ledger.month_spent(),
                   stage="proposal", memory=mem)
    if origin:
        res["evidence"]["origin"] = origin
    status = STATUS[res["decision"]]
    it = ledger.new_intent(merchant=merchant, category=category, amount=amount, currency=CURRENCY, description=description, status=status)
    rec = ledger.record_decision(
        stage="proposal", decided_by="policy_evaluator", merchant=merchant, category=category, amount=amount,
        currency=CURRENCY, intent_id=it["id"], stripe={}, **res,
    )
    it = ledger.update_intent(it["id"], proposal_decision_id=rec["decision_id"])  # return the updated intent
    memory.remember_decision_sync(rec)
    return it, rec, res


def approve(intent_id, approver, note=""):
    it = ledger.get_intent(intent_id)
    if not it:
        sys.exit(f"no intent {intent_id}")
    approval = {"approver": approver, "ts": ledger.now(), "note": note, "token": "apr_" + uuid.uuid4().hex[:8]}
    mem = recall.check(it["merchant"], POLICY)  # re-check memory: a human cannot approve past an open hold
    res = evaluate(POLICY, amount=it["amount"], category=it["category"], merchant=it["merchant"],
                   month_spent=ledger.month_spent(), approval=approval, stage="proposal", memory=mem)
    rec = ledger.record_decision(
        stage="approval", decided_by="human:" + approver, merchant=it["merchant"], category=it["category"],
        amount=it["amount"], currency=it["currency"], intent_id=intent_id, stripe={}, **res,
    )
    new_status = STATUS[res["decision"]] if res["decision"] in ("approve", "hold") else it["status"]
    ledger.update_intent(intent_id, approval=approval, status=new_status)
    memory.remember_decision_sync(rec)
    return res, rec


def create_test_authorization(*, amount, currency, card, merchant_name, category):
    params = dict(amount=amount, currency=currency, card=card,
                  merchant_data={"name": merchant_name, "category": category, "city": "London", "country": "GB"})
    try:
        auth = stripe.issuing.Authorization.TestHelpers.create(**params)
    except AttributeError:
        auth = stripe.StripeClient(stripe.api_key).test_helpers.issuing.authorizations.create(params)
    return auth.to_dict()  # stripe 16 objects are not dicts; callers use .get()


def capture_authorization(auth_id):
    try:
        return stripe.issuing.Authorization.TestHelpers.capture(auth_id)
    except AttributeError:
        return stripe.StripeClient(stripe.api_key).test_helpers.issuing.authorizations.capture(auth_id)


def execute(intent_id):
    it = ledger.get_intent(intent_id)
    if not it or it["status"] != "cleared":
        sys.exit(f"intent {intent_id} is not cleared (status={it and it['status']})")
    card = state()["card_id"]
    print(f"→ using card {card}: {it['merchant']} {gbp(it['amount'])} ({it['category']})")
    auth = create_test_authorization(amount=it["amount"], currency=it["currency"], card=card,
                                     merchant_name=it["merchant"], category=it["category"])
    hist = auth.get("request_history") or []
    reason = hist[-1].get("reason") if hist else None
    ledger.update_intent(intent_id, authorization_id=auth["id"])
    if auth.get("approved"):
        print(f"✓ APPROVED authorization {auth['id']}  metadata={dict(auth.get('metadata') or {})}")
        capture_authorization(auth["id"])
        print("✓ captured → Issuing > Transactions")
    else:
        print(f"✗ DECLINED authorization {auth['id']}  reason={reason}")
    return auth


def handle_intent(it, approver=None):
    """Drive an intent to completion: execute, prompt for approval, or report the decline."""
    if it["status"] == "cleared":
        return execute(it["id"])
    if it["status"] == "awaiting_approval":
        print(f"⏸ {it['id']} needs approval (C3): {it['merchant']} {gbp(it['amount'])} — {it['description']}")
        name = approver or input("Approver name (Enter to leave pending): ").strip()
        if not name:
            return None
        res, _ = approve(it["id"], name)
        if res["decision"] == "approve":
            print(f"✓ approved by {name} → clause C3 satisfied")
            return execute(it["id"])
        print(f"✗ still {res['decision']}: {clause_text(res['failed_clause'])}")
        return None
    rec = next((r for r in ledger.decisions() if r["decision_id"] == it.get("proposal_decision_id")), None)
    if it["status"] == "held":
        mem = ((rec or {}).get("evidence") or {}).get("memory") or {}
        why_held = "; ".join(mem.get("notes") or []) or mem.get("detail") or "memory lookup failed"
        print(f"⏸ HELD under C7 — {POLICY['clauses']['C7']['title']}\n   memory ({mem.get('source')}): {why_held}")
        return None
    print(f"✗ DECLINED at proposal: {clause_text(rec['failed_clause']) if rec else it['status']}")
    return None


# ---------------------------------------------------------------- triggers

def parse_request(text):
    """key: value lines → dict, or None if it is free text."""
    d = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            d[k.strip().lower()] = v.strip()
    if {"merchant", "category", "amount"} <= set(d):
        return d
    return None


def inbox(approver=None):
    os.makedirs("inbox/processed", exist_ok=True)
    files = sorted(glob.glob("inbox/*.txt"))
    if not files:
        print("inbox empty")
    for path in files:
        text = open(path, encoding="utf-8").read()
        print(f"\n📨 {os.path.basename(path)}")
        req = parse_request(text)
        origin = {"file": os.path.basename(path), "parsed_by": "form"}
        if not req:
            try:
                req = extractor.extract(text, POLICY)
                origin["parsed_by"] = "llm:" + os.getenv("LLM_MODEL", "")
                print(f"🧠 LLM extracted: {req['merchant']} £{req['amount']:,.2f} ({req['category']}) — {req['reason']}")
            except Exception as e:  # fail closed: if we cannot read it, we do not pay it
                print(f"✗ could not extract a purchase from this email ({type(e).__name__}: {e}) — nothing proposed")
                ledger.record_decision(stage="extraction", decided_by="extractor", merchant="", category="", amount=0,
                                       currency=CURRENCY, intent_id=None, decision="decline", clause_ids=[],
                                       failed_clause="extraction", evidence={"origin": origin, "error": str(e)[:300]},
                                       policy_version=POLICY["_version"], stripe={})
                shutil.move(path, os.path.join("inbox/processed", os.path.basename(path)))
                continue
        amount = int(round(float(str(req["amount"]).replace("£", "").replace(",", "")) * 100))
        it, rec, res = propose(req["merchant"], req["category"], amount, req.get("reason", ""), origin=origin)
        print(f"policy: {res['decision'].upper()} clauses={','.join(res['clause_ids'])} failed={res['failed_clause']}")
        handle_intent(it, approver)
        shutil.move(path, os.path.join("inbox/processed", os.path.basename(path)))


SYSTEM = f"""You are Treasurer, a buying agent for a small company. You cannot move money.
Your only way to buy anything is the propose_purchase tool; a deterministic policy decides.
Merchant category must be one of Stripe's merchant category codes; the policy allows: {', '.join(POLICY['clauses']['C2']['allowed_categories'])}.
If a request falls outside those, still propose it with the most accurate category (for example eating_places_restaurants or electronics_stores) so the policy can decline it on the record.
Amounts are in pence. After proposing, report the decision and the clause ids in one short paragraph."""


def chat(message, approver=None):
    from agents import Agent, Runner, function_tool

    created = []
    tools = []
    try:
        from stripe_agent_toolkit.openai.toolkit import StripeAgentToolkit

        # Read-only: the model may look at the balance, never create anything.
        toolkit = StripeAgentToolkit(secret_key=stripe.api_key, configuration={"actions": {"balance": {"read": True}}})
        tools += toolkit.get_tools()
    except Exception as e:
        print("(stripe agent toolkit not loaded:", e, ")")

    @function_tool
    def propose_purchase(merchant: str, category: str, amount_pence: int, description: str) -> str:
        """Propose a purchase for policy evaluation. Never moves money. Returns the decision and clause ids."""
        it, rec, res = propose(merchant, category, int(amount_pence), description)
        created.append(it["id"])
        return json.dumps({"intent_id": it["id"], "decision": res["decision"], "clause_ids": res["clause_ids"],
                           "failed_clause": res["failed_clause"], "decision_id": rec["decision_id"]})

    tools.append(propose_purchase)
    agent = Agent(name="Treasurer", instructions=SYSTEM, tools=tools)
    result = Runner.run_sync(agent, message)
    print(result.final_output)
    for iid in created:
        handle_intent(ledger.get_intent(iid), approver)


# ---------------------------------------------------------------- explain

def explain_from_ledger(question):
    q = question.lower()
    recs = [r for r in ledger.decisions() if r.get("stage") != "capture" and r.get("merchant") and r["merchant"].lower() in q]
    if not recs:
        recs = [r for r in ledger.decisions() if r.get("stage") != "capture"][-3:]
    out = []
    for r in recs[-4:]:
        ev = r.get("evidence") or {}
        line = f"{r['decision'].upper()} {r['merchant']} {gbp(r['amount'])} [{r['stage']}] at {r['ts']} by {r['decided_by']}."
        if r.get("clause_ids"):
            line += " Rules applied: " + "; ".join(clause_text(c) for c in r["clause_ids"]) + "."
        if r.get("failed_clause"):
            line += f" Stopped by: {clause_text(r['failed_clause'])}."
        if ev.get("approval"):
            a = ev["approval"]
            line += f" Approved by {a.get('approver')} (token {a.get('token')}, {a.get('ts')})."
        mem = ev.get("memory") or {}
        if mem:
            line += f" Memory check C7 ({mem.get('source')}): {mem.get('status')}"
            line += (" — " + "; ".join(mem["notes"])) if mem.get("notes") else ""
            line += "."
        if ev.get("month_spent_after") is not None:
            line += f" Month-to-date after this: {gbp(ev['month_spent_after'])} of {gbp(ev.get('monthly_max', 0))}."
        st = r.get("stripe") or {}
        if st.get("authorization_id"):
            line += f" Evidence: Stripe authorization {st['authorization_id']}, decision {r['decision_id']}, policy {r['policy_version']}."
        out.append(line)
    return "\n".join(out)


def why(question, source="auto"):
    if source in ("auto", "cognee") and memory.HAVE_COGNEE:
        try:
            ans = asyncio.run(memory.why(question))
            print("Cognee memory says:")
            for a in (ans if isinstance(ans, list) else [ans]):
                print(" ", a)
            return
        except Exception as e:
            print("(cognee search failed:", e, ")")
    print("Ledger says:")
    print(explain_from_ledger(question))


# ---------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("buy"); s.add_argument("--merchant", required=True); s.add_argument("--category", required=True)
    s.add_argument("--amount", type=float, required=True, help="pounds"); s.add_argument("--desc", default=""); s.add_argument("--approver")
    s = sub.add_parser("chat"); s.add_argument("message"); s.add_argument("--approver")
    s = sub.add_parser("inbox"); s.add_argument("--approver")
    sub.add_parser("pending")
    sub.add_parser("ingest-notes")
    s = sub.add_parser("approve"); s.add_argument("intent_id"); s.add_argument("--approver", required=True); s.add_argument("--note", default="")
    s = sub.add_parser("execute"); s.add_argument("intent_id")
    s = sub.add_parser("why"); s.add_argument("question"); s.add_argument("--source", choices=["auto", "cognee", "ledger"], default="auto")
    sub.add_parser("sync-memory")
    a = p.parse_args()

    if a.cmd == "buy":
        it, rec, res = propose(a.merchant, a.category, int(round(a.amount * 100)), a.desc)
        print(f"policy: {res['decision'].upper()} clauses={','.join(res['clause_ids'])} failed={res['failed_clause']} decision={rec['decision_id']}")
        handle_intent(it, a.approver)
    elif a.cmd == "chat":
        chat(a.message, a.approver)
    elif a.cmd == "inbox":
        inbox(a.approver)
    elif a.cmd == "pending":
        for st in ("awaiting_approval", "held"):
            for it in ledger.list_intents(st):
                print(st, it["id"], it["merchant"], gbp(it["amount"]), it["description"])
    elif a.cmd == "ingest-notes":
        if not memory.HAVE_COGNEE:
            sys.exit("cognee not installed: pip install -r requirements-optional.txt (and set LLM_API_KEY)")
        asyncio.run(memory.remember_notes())
        print(f"finance notes ingested into Cognee dataset '{memory.NOTES_DATASET}'")
    elif a.cmd == "approve":
        res, rec = approve(a.intent_id, a.approver, a.note)
        print(f"{res['decision'].upper()} clauses={','.join(res['clause_ids'])} failed={res['failed_clause']} decision={rec['decision_id']}")
    elif a.cmd == "execute":
        execute(a.intent_id)
    elif a.cmd == "why":
        why(a.question, a.source)
    elif a.cmd == "sync-memory":
        if not memory.HAVE_COGNEE:
            sys.exit("cognee not installed: pip install cognee  (and set LLM_API_KEY)")
        n = asyncio.run(memory.sync_all(POLICY, ledger.decisions()))
        print(f"synced policy + {n} decisions to Cognee dataset '{memory.DATASET}'")


if __name__ == "__main__":
    main()
