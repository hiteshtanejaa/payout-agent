"""Deterministic policy evaluator. No I/O, no LLM, no clock.

evaluate() is a pure function of its inputs so the same transaction always
produces the same decision (see replay.py).
"""
import hashlib

import yaml


def load_policy(path="policy.yaml"):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    policy = yaml.safe_load(raw)
    policy["_version"] = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return policy


def spending_controls(policy):
    """The subset of the policy that Stripe itself can enforce on the card."""
    c = policy["clauses"]
    return {
        "spending_limits": [
            {"amount": c["C1"]["per_transaction_max"], "interval": "per_authorization"},
            {"amount": c["C4"]["monthly_max"], "interval": "monthly"},
        ],
        "allowed_categories": list(c["C2"]["allowed_categories"]),
    }


def evaluate(policy, *, amount, category, merchant, month_spent, approval=None, intent=None, stage="proposal", memory=None):
    """Return {decision, clause_ids, failed_clause, evidence, policy_version}.

    decision is one of: approve | escalate | hold | decline
    memory is the result of recall.check() for C7: {"status": clear|flagged|error, ...}.
    It is passed in (not fetched here) so this function stays pure.
    stage is "proposal" (before the card is used) or "authorization"
    (inside the issuing_authorization.request webhook).
    """
    c = policy["clauses"]
    applied = []
    ev = {
        "stage": stage,
        "merchant": merchant,
        "category": category,
        "amount": amount,
        "month_spent_before": month_spent,
    }

    def out(decision, failed=None):
        return {
            "decision": decision,
            "clause_ids": list(applied),
            "failed_clause": failed,
            "evidence": ev,
            "policy_version": policy["_version"],
        }

    # C1 per-transaction cap
    applied.append("C1")
    ev["per_transaction_max"] = c["C1"]["per_transaction_max"]
    if amount > c["C1"]["per_transaction_max"]:
        return out("decline", "C1")

    # C2 allowed categories
    applied.append("C2")
    ev["category_allowed"] = category in c["C2"]["allowed_categories"]
    if not ev["category_allowed"]:
        return out("decline", "C2")

    # C4 monthly cap
    applied.append("C4")
    ev["monthly_max"] = c["C4"]["monthly_max"]
    ev["month_spent_after"] = month_spent + amount
    if month_spent + amount > c["C4"]["monthly_max"]:
        return out("decline", "C4")

    # C7 memory check (proposal/approval only; the webhook relies on C5 instead)
    if memory is not None:
        applied.append("C7")
        ev["memory"] = memory
        if memory.get("status") != "clear":
            return out("hold", "C7")

    # C3 human approval above threshold
    if amount > c["C3"]["escalate_above"]:
        applied.append("C3")
        ev["escalate_above"] = c["C3"]["escalate_above"]
        ev["approval"] = approval
        if not approval or approval.get("approver") not in c["C3"]["approvers"]:
            return out("escalate", "C3")

    # C5 only proposed purchases (authorization stage only)
    if stage == "authorization":
        applied.append("C5")
        ev["intent_id"] = intent["id"] if intent else None
        if intent is None:
            return out("decline", "C5")

    return out("approve")
