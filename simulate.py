"""Bypass the agent and hit the card directly, like a rogue tool call would.
Use it to prove the guardrail lives in Stripe, not in the prompt.

  python simulate.py --merchant Apple --category computers_peripherals_and_software --amount 900
  → Stripe declines (spending_controls) before the webhook is even asked.
"""
import argparse

from agent import CURRENCY, capture_authorization, create_test_authorization, gbp, state

p = argparse.ArgumentParser()
p.add_argument("--merchant", required=True)
p.add_argument("--category", required=True)
p.add_argument("--amount", type=float, required=True, help="pounds")
p.add_argument("--capture", action="store_true")
a = p.parse_args()

card = state()["card_id"]
pence = int(round(a.amount * 100))
print(f"→ direct authorization on {card}: {a.merchant} {gbp(pence)} ({a.category})")
auth = create_test_authorization(amount=pence, currency=CURRENCY, card=card, merchant_name=a.merchant, category=a.category)
hist = auth.get("request_history") or []
reason = hist[-1].get("reason") if hist else None
if auth.get("approved"):
    print(f"✓ APPROVED {auth['id']} metadata={dict(auth.get('metadata') or {})}")
    if a.capture:
        capture_authorization(auth["id"])
        print("✓ captured")
else:
    print(f"✗ DECLINED {auth['id']} reason={reason}")
    if reason == "spending_controls":
        print("  → Stripe enforced the card limits; the webhook was never consulted.")
