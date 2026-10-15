"""Replay a fixed set of transactions through the evaluator.
Same policy + same inputs must give the same decisions every time.

  python replay.py                 # prints decisions and a hash
  python replay.py --expect <hash> # exit 1 if the hash differs
"""
import hashlib
import json
import sys

from policy import evaluate, load_policy

POLICY = load_policy()

CASES = [
    {"merchant": "Figma", "category": "computer_software_stores", "amount": 12000, "month_spent": 0},
    {"merchant": "Apple", "category": "computers_peripherals_and_software", "amount": 90000, "month_spent": 0},
    {"merchant": "Pret", "category": "eating_places_restaurants", "amount": 1500, "month_spent": 0},
    {"merchant": "Google Ads", "category": "advertising_services", "amount": 35000, "month_spent": 12000},
    {"merchant": "Google Ads", "category": "advertising_services", "amount": 35000, "month_spent": 12000, "approval": {"approver": "sarthak"}},
    {"merchant": "Google Ads", "category": "advertising_services", "amount": 35000, "month_spent": 12000, "approval": {"approver": "intern"}},
    {"merchant": "Notion", "category": "computer_software_stores", "amount": 20000, "month_spent": 47000},
    {"merchant": "Notion", "category": "computer_software_stores", "amount": 20001, "month_spent": 47000},
    {"merchant": "Herman Miller", "category": "office_and_commercial_furniture", "amount": 45000, "month_spent": 160000, "approval": {"approver": "sarthak"}},
    {"merchant": "Figma", "category": "computer_software_stores", "amount": 12000, "month_spent": 47000, "stage": "authorization", "intent": {"id": "int_demo"}},
    {"merchant": "Figma", "category": "computer_software_stores", "amount": 12000, "month_spent": 47000, "stage": "authorization", "intent": None},
    {"merchant": "Meta Ads", "category": "advertising_services", "amount": 15000, "month_spent": 0, "memory": {"status": "flagged"}},
    {"merchant": "Figma", "category": "computer_software_stores", "amount": 12000, "month_spent": 0, "memory": {"status": "error"}},
    {"merchant": "Figma", "category": "computer_software_stores", "amount": 12000, "month_spent": 0, "memory": {"status": "clear"}},
]

results = []
for i, c in enumerate(CASES, 1):
    r = evaluate(
        POLICY,
        amount=c["amount"],
        category=c["category"],
        merchant=c["merchant"],
        month_spent=c["month_spent"],
        approval=c.get("approval"),
        intent=c.get("intent"),
        stage=c.get("stage", "proposal"),
        memory=c.get("memory"),
    )
    results.append({"decision": r["decision"], "clause_ids": r["clause_ids"], "failed_clause": r["failed_clause"]})
    print(f"#{i:02d} {r['decision']:8} {c['merchant']:14} {c['amount']:>7}  clauses={','.join(r['clause_ids']):12} failed={r['failed_clause']}")

digest = hashlib.sha256(json.dumps(results, sort_keys=True).encode()).hexdigest()[:16]
print("policy", POLICY["_version"])
print("replay", digest)

if "--expect" in sys.argv:
    want = sys.argv[sys.argv.index("--expect") + 1]
    if want != digest:
        print(f"MISMATCH: expected {want}")
        sys.exit(1)
    print("OK: identical to expected")
