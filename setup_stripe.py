"""One-time setup in Stripe TEST mode: a cardholder and a virtual card whose
spending_controls are generated from policy.yaml (C1, C2, C4).

  python setup_stripe.py            # create (idempotent)
  python setup_stripe.py --sync     # push updated policy limits onto the existing card
"""
import json
import os
import sys
import time

import stripe
from dotenv import load_dotenv

from policy import load_policy, spending_controls

load_dotenv()
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
if not stripe.api_key.startswith("sk_test_"):
    sys.exit("Refusing to run: STRIPE_SECRET_KEY is not a test key.")

STATE = os.path.join(os.getenv("LEDGER_DIR", "ledger"), "stripe_state.json")
COUNTRY = os.getenv("CARD_COUNTRY", "GB")
POLICY = load_policy()
CURRENCY = POLICY.get("currency", "gbp")

os.makedirs(os.path.dirname(STATE), exist_ok=True)
state = json.load(open(STATE)) if os.path.exists(STATE) else {}

if "cardholder_id" not in state:
    ch = stripe.issuing.Cardholder.create(
        name="Treasurer Agent",
        email="treasurer@example.com",
        phone_number="+447700900000",
        status="active",
        type="individual",
        billing={"address": {"line1": "1 Agent Street", "city": "London", "postal_code": "EC1A 1BB", "country": COUNTRY}},
        individual={
            "first_name": "Treasurer",
            "last_name": "Agent",
            "card_issuing": {"user_terms_acceptance": {"date": int(time.time()), "ip": "127.0.0.1"}},
        },
    )
    state["cardholder_id"] = ch["id"]
    print("cardholder", ch["id"])

controls = spending_controls(POLICY)

if "card_id" not in state:
    try:
        card = stripe.issuing.Card.create(
            cardholder=state["cardholder_id"],
            currency=CURRENCY,
            type="virtual",
            status="active",
            spending_controls=controls,
            metadata={"policy_version": POLICY["_version"], "owner": "treasurer-agent"},
        )
    except stripe.error.InvalidRequestError as e:
        # Most common cause: a category string in policy.yaml that Stripe does not know.
        sys.exit(f"Card create failed: {e.user_message or e}\nCheck allowed_categories in policy.yaml against Stripe's merchant category list.")
    state["card_id"] = card["id"]
    print("card", card["id"], "last4", card["last4"])
elif "--sync" in sys.argv:
    card = stripe.issuing.Card.modify(state["card_id"], spending_controls=controls, metadata={"policy_version": POLICY["_version"]})
    print("card controls synced", card["id"])

json.dump(state, open(STATE, "w"), indent=2)

bal = stripe.Balance.retrieve().to_dict()
issuing = bal.get("issuing") or {}
print("issuing balance:", issuing.get("available"))
print("If available is empty or 0: Dashboard (test mode) > Balances > Add funds to Issuing balance, or authorizations decline with insufficient_funds.")
print("state saved to", STATE)
