"""Preflight: confirm every external service in .env is reachable before building.

Run from the project root:  python scripts/check_connections.py
Prints only key prefixes, never full secrets.
"""
import os
import sys
import urllib.request
import urllib.error

from dotenv import load_dotenv

load_dotenv()
ok = True


def report(name: str, passed: bool, detail: str) -> None:
    global ok
    ok &= passed
    print(f"[{'OK ' if passed else 'FAIL'}] {name}: {detail}")


# --- Stripe -----------------------------------------------------------------
try:
    import stripe

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key.startswith(("sk_test_", "rk_test_")):
        report("Stripe", False, "STRIPE_SECRET_KEY must be a TEST key (sk_test_...)")
    else:
        stripe.api_key = key
        acct = stripe.Account.retrieve()
        bal = stripe.Balance.retrieve()
        currencies = sorted({b["currency"] for b in bal["available"]})
        report("Stripe", acct.country == "GB",
               f"account {acct['id']} country={acct.country} "
               f"default_currency={acct.default_currency} balance currencies={currencies}")
        if key.startswith("rk_test_"):
            print("       note: restricted key — needs write access to Accounts (Connect), "
                  "Transfers, PaymentIntents and read on Balance. A standard sk_test_ key is simpler.")
except Exception as e:  # noqa: BLE001 - preflight reports every failure
    report("Stripe", False, f"{type(e).__name__}: {e}")

# --- LLM key ----------------------------------------------------------------
provider = os.environ.get("LLM_PROVIDER", "openai")
llm_key = os.environ.get("LLM_API_KEY", "")
try:
    if provider == "openai":
        req = urllib.request.Request("https://api.openai.com/v1/models",
                                     headers={"Authorization": f"Bearer {llm_key}"})
    else:
        req = urllib.request.Request("https://api.anthropic.com/v1/models",
                                     headers={"x-api-key": llm_key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=15) as r:
        report(f"LLM ({provider})", r.status == 200, f"HTTP {r.status}, model={os.environ.get('LLM_MODEL')}")
except urllib.error.HTTPError as e:
    report(f"LLM ({provider})", False, f"HTTP {e.code} (401 = bad key)")
except Exception as e:  # noqa: BLE001
    report(f"LLM ({provider})", False, f"{type(e).__name__}: {e}")

# --- Cognee Cloud -----------------------------------------------------------
url = os.environ.get("COGNEE_SERVICE_URL", "").rstrip("/")
ckey = os.environ.get("COGNEE_API_KEY", "")
if not url:
    report("Cognee Cloud", False, "COGNEE_SERVICE_URL not set")
else:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=15) as r:
            report("Cognee health", r.status == 200, f"HTTP {r.status}")
        req = urllib.request.Request(f"{url}/api/v1/datasets/", headers={"X-Api-Key": ckey})
        with urllib.request.urlopen(req, timeout=15) as r:
            report("Cognee API key", r.status == 200, f"HTTP {r.status}")
    except urllib.error.HTTPError as e:
        report("Cognee Cloud", False, f"HTTP {e.code} (401 = bad key)")
    except Exception as e:  # noqa: BLE001
        report("Cognee Cloud", False, f"{type(e).__name__}: {e}")

print("\nAll good — run: python setup_stripe.py" if ok else "\nFix the FAIL lines above first.")
sys.exit(0 if ok else 1)
