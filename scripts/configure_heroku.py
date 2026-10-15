"""Configure an existing Heroku demo without printing credentials.

  python scripts/configure_heroku.py --app YOUR_APP --register-webhook

Reads .env, optional .env.heroku overrides, and the local Stripe card state.
Does not create an app/database, purchase resources, commit, push, or scale dynos.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = (
    "STRIPE_SECRET_KEY", "STRIPE_CARD_ID", "STRIPE_CARDHOLDER_ID", "STRIPE_API_VERSION",
    "LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "EMBEDDING_PROVIDER", "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS", "COGNEE_SERVICE_URL", "COGNEE_API_KEY", "COGNEE_DATASET",
    "MEMORY_CHECK", "COGNEE_SYNC", "DEMO_USERNAME", "DEMO_PASSWORD",
)
WEBHOOK_EVENTS = ["issuing_authorization.request", "issuing_authorization.created", "issuing_transaction.created"]


def source_config():
    values = {**dotenv_values(ROOT / ".env"), **dotenv_values(ROOT / ".env.heroku"), **os.environ}
    config = {key: str(values[key]) for key in ALLOWLIST if values.get(key)}
    if not config.get("STRIPE_CARD_ID"):
        state_path = ROOT / "ledger" / "stripe_state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            for local_key, env_key in (("card_id", "STRIPE_CARD_ID"), ("cardholder_id", "STRIPE_CARDHOLDER_ID")):
                if state.get(local_key):
                    config[env_key] = state[local_key]
    config.setdefault("DEMO_USERNAME", "demo")
    config.setdefault("MEMORY_CHECK", "cognee")
    config.setdefault("COGNEE_DATASET", "treasurer_heroku")
    config.setdefault("COGNEE_SYNC", "batch")
    config["PYTHONUNBUFFERED"] = "1"
    # Never copy the local Stripe CLI signing secret into a public endpoint.
    hosted = dotenv_values(ROOT / ".env.heroku")
    for key in ("STRIPE_WEBHOOK_SECRET", "STRIPE_ISSUING_WEBHOOK_SECRET"):
        if hosted.get(key):
            config[key] = hosted[key]
    return config


def validate_config(config):
    if not config.get("STRIPE_SECRET_KEY", "").startswith(("sk_test_", "rk_test_")):
        raise ValueError("Set a Stripe TEST key locally before configuring the demo.")
    required = ["STRIPE_CARD_ID", "LLM_MODEL", "LLM_API_KEY"]
    if config.get("MEMORY_CHECK") == "cognee":
        required += ["COGNEE_SERVICE_URL", "COGNEE_API_KEY"]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError("Missing local configuration: " + ", ".join(missing))
    if config.get("MEMORY_CHECK") not in ("file", "cognee"):
        raise ValueError("MEMORY_CHECK must be file or cognee.")


def auth_token():
    if os.getenv("HEROKU_API_KEY"):
        return os.environ["HEROKU_API_KEY"]
    cli = shutil.which("heroku")
    if not cli:
        raise RuntimeError("Install the Heroku CLI and sign in with heroku login.")
    proc = subprocess.run([cli, "auth:token"], capture_output=True, text=True)
    if proc.returncode or not proc.stdout.strip():
        raise RuntimeError("Sign in locally with heroku login before configuring the demo.")
    return proc.stdout.strip().splitlines()[-1]


def heroku_api(token, path, method="GET", payload=None):
    request = Request(
        "https://api.heroku.com" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.heroku+json; version=3",
                 "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        # Heroku responses can include private config. Never dump their bodies.
        if exc.code == 401:
            raise RuntimeError("Heroku sign-in expired. Run heroku login locally.") from None
        raise RuntimeError(f"Heroku request failed with HTTP {exc.code}.") from None


def configure(app_name, register_webhook=False):
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,28}[a-z0-9]", app_name):
        raise ValueError("Provide the existing Heroku app name (3–30 lowercase letters, numbers, hyphens).")
    config = source_config()
    validate_config(config)
    token = auth_token()
    path = "/apps/" + quote(app_name, safe="")
    app = heroku_api(token, path)
    existing = heroku_api(token, path + "/config-vars")
    if not existing.get("DATABASE_URL"):
        raise RuntimeError("Attach PostgreSQL to this app first; no database has been purchased by this script.")
    web = urlsplit(app["web_url"])
    url = "https://" + web.netloc + "/stripe/webhook"

    if register_webhook:
        import stripe
        stripe.api_key = config["STRIPE_SECRET_KEY"]
        matches = [endpoint for endpoint in stripe.WebhookEndpoint.list(limit=100).auto_paging_iter()
                   if endpoint.url == url and endpoint.status == "enabled"]
        for endpoint in matches:
            subscribed = set(getattr(endpoint, "enabled_events", []))
            if not set(WEBHOOK_EVENTS) <= subscribed:
                raise RuntimeError("An existing webhook targets this app without all required events. Add issuing_authorization.request, issuing_authorization.created and issuing_transaction.created in Stripe, then rerun.")
        if matches and not (config.get("STRIPE_WEBHOOK_SECRET") or existing.get("STRIPE_WEBHOOK_SECRET")):
            raise RuntimeError("A webhook already targets this app. Put its signing secret in .env.heroku and rerun; no duplicate endpoint was created.")
        if not matches:
            endpoint = stripe.WebhookEndpoint.create(
                url=url,
                enabled_events=WEBHOOK_EVENTS,
                api_version=config.get("STRIPE_API_VERSION") or stripe.api_version,
                description="Treasurer Heroku test-mode demo",
            )
            config["STRIPE_WEBHOOK_SECRET"] = endpoint.secret
    if not (config.get("STRIPE_WEBHOOK_SECRET") or existing.get("STRIPE_WEBHOOK_SECRET")):
        raise RuntimeError("Register a hosted webhook with --register-webhook or set its signing secret in .env.heroku.")

    config.setdefault("DEMO_PASSWORD", existing.get("DEMO_PASSWORD") or secrets.token_urlsafe(24))
    # Save the generated login locally before changing the cloud configuration.
    # Keep any manually supplied hosted signing-secret overrides intact.
    private_path = ROOT / ".env.heroku"
    private = {k: v for k, v in dotenv_values(private_path).items() if v is not None}
    private.update({key: config[key] for key in ("DEMO_USERNAME", "DEMO_PASSWORD")})
    if config.get("STRIPE_WEBHOOK_SECRET"):
        private["STRIPE_WEBHOOK_SECRET"] = config["STRIPE_WEBHOOK_SECRET"]
    private_path.write_text("".join(f"{key}={json.dumps(value)}\n" for key, value in private.items()), encoding="utf-8")
    heroku_api(token, path + "/config-vars", method="PATCH", payload=config)
    print(f"Configured {app_name}. Private values were not printed.")
    print("The demo login is stored locally in .env.heroku.")
    print(f"Webhook URL: {url}")
    print("Before processing purchases, select this authorization URL in Stripe TEST Issuing settings and set timeout fallback to DECLINE.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--register-webhook", action="store_true")
    args = parser.parse_args()
    try:
        configure(args.app, args.register_webhook)
    except Exception as exc:
        # Avoid printing SDK exception strings, which can contain request details.
        if isinstance(exc, (ValueError, RuntimeError)):
            raise SystemExit(str(exc)) from None
        raise SystemExit(f"Configuration failed ({type(exc).__name__}); no credentials were printed.") from None


if __name__ == "__main__":
    main()
