"""One-off Stripe TEST-MODE setup for the payout agent.

1. Funds the platform balance in GBP (transfers draw on *available* funds).
2. Creates one GB connected account per contractor in data/contractors.yaml, using
   Stripe's documented test identity/bank values, with the `transfers` capability.
3. Saves {contractor_id: acct_id} to data/stripe_accounts.json (gitignored).

Safe to re-run: existing accounts are reused, and funding uses an idempotency key per run-day.
Test values source: https://docs.stripe.com/connect/testing
"""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import stripe
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
ACCOUNTS_FILE = ROOT / "data" / "stripe_accounts.json"
FUND_PENCE = 1_000_000  # £10,000 of test money

load_dotenv(ROOT.parent / ".env")
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
if not stripe.api_key.startswith(("sk_test_", "rk_test_")):
    sys.exit("Refusing to run: STRIPE_SECRET_KEY is not a TEST key.")


def fund_platform() -> None:
    today = dt.date.today().isoformat()
    pi = stripe.PaymentIntent.create(
        amount=FUND_PENCE,
        currency="gbp",
        payment_method="pm_card_bypassPending",  # funds land as *available* immediately
        confirm=True,
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
        description="Test-mode float for contractor payouts",
        idempotency_key=f"platform-float-{today}",
    )
    print(f"Funded platform: {pi.id} status={pi.status} £{FUND_PENCE / 100:,.2f}")


def create_account(c: dict) -> str:
    first, _, last = c["name"].partition(" ")
    acct = stripe.Account.create(
        country="GB",
        email=c["email"],
        business_type="individual",
        controller={
            "fees": {"payer": "application"},
            "losses": {"payments": "application"},
            "stripe_dashboard": {"type": "none"},
            "requirement_collection": "application",
        },
        capabilities={"transfers": {"requested": True}},
        business_profile={
            "mcc": "7392",  # consulting / management services
            "url": f"https://{c['email'].split('@')[1]}",
            "product_description": f"Contract services under {c['contract_id']}",
        },
        individual={
            "first_name": first or c["name"],
            "last_name": last or "Contractor",
            "email": c["email"],
            "phone": "+447700900123",
            "dob": {"day": 1, "month": 1, "year": 1901},          # test: DOB match
            "address": {
                "line1": "address_full_match",                     # test: address match
                "city": "London",
                "postal_code": "EC1A 1BB",
                "country": "GB",
            },
            "verification": {"document": {"front": "file_identity_document_success"}},
        },
        external_account={
            "object": "bank_account",
            "country": "GB",
            "currency": "gbp",
            "routing_number": "108800",                            # test sort code
            "account_number": "00012345",                          # test: payout succeeds
        },
        tos_acceptance={"date": int(time.time()), "ip": "8.8.8.8"},
        metadata={"contractor_id": c["id"], "contract_id": c["contract_id"]},
        idempotency_key=f"connect-account-{c['id']}-v1",
    )
    return acct.id


def main() -> None:
    platform = stripe.Account.retrieve()
    print(f"Platform {platform.id} country={platform.country}")
    if platform.country != "GB":
        sys.exit("Platform account is not GB — switch to the UK account's test key.")

    fund_platform()

    contractors = yaml.safe_load((ROOT / "data" / "contractors.yaml").read_text())["contractors"]
    accounts = json.loads(ACCOUNTS_FILE.read_text()) if ACCOUNTS_FILE.exists() else {}
    for c in contractors:
        if c["id"] not in accounts:
            accounts[c["id"]] = create_account(c)
        acct = stripe.Account.retrieve(accounts[c["id"]])
        due = acct.requirements.currently_due if acct.requirements else []
        print(f"  {c['name']:<22} {acct.id}  transfers={getattr(acct.capabilities, 'transfers', None)}  "
              f"currently_due={due or 'none'}")
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2))
    print(f"Saved {ACCOUNTS_FILE.relative_to(ROOT)}")

    bal = stripe.Balance.retrieve()
    avail = {b["currency"]: b["amount"] / 100 for b in bal["available"]}
    print(f"Available balance: {avail}")


if __name__ == "__main__":
    main()
