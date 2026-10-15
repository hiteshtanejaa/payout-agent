# Heroku live demo

The deployment files are prepared locally. The empty app `treasurer-briefcase-demo` has been created in the EU region; no paid resources have been provisioned and a hosted demo has not been verified yet.

## What runs

- `.python-version` selects Python 3.12 on `heroku-24`.
- `Procfile` runs the database initializer during release, then FastAPI on `$PORT` with **one Uvicorn worker**.
- `DATABASE_URL`, supplied by Heroku Postgres, selects PostgreSQL for intents, decisions, and processed-email receipts. Without it, local development keeps JSON/JSONL storage. Existing local history is **not migrated**.
- Use **exactly one always-on Basic web dyno**. UI actions run as jobs in that process, return HTTP 202, and are polled by the browser. Do not add workers or web dynos: job state and action locks are process-local.
- Cognee is in `requirements.txt`; the optional requirements contain the interactive chat SDKs. Use Cognee Cloud for hosted memory.

Basic costs **US$7/month** and Postgres Essential-0 **US$5/month**, approximately **US$12 for a full month**, before external AI usage and any one-off dynos. Both resources are prorated to the second. [Heroku pricing](https://www.heroku.com/pricing/) · [Usage and billing](https://devcenter.heroku.com/articles/usage-and-billing)

GitHub Student credits cover at most **US$13 per month across the account**, rather than making each app free. Check existing apps and subscriptions before provisioning; the combined monthly usage must remain within the allowance to avoid out-of-pocket Heroku charges. [Student credit rules](https://help.heroku.com/Z3RHNRHD/how-does-the-heroku-for-github-students-program-work)

## Deploy

Run from the repository root. Replace `APP` with a unique Heroku app name. The creation and scaling commands below provision paid resources.

Before configuring, ensure local `.env` contains working Stripe **test** and extraction-model keys plus `COGNEE_SERVICE_URL` and `COGNEE_API_KEY`. The existing test card must have its spending controls installed and its Issuing balance funded. Stop any local `stripe listen` session before using the cloud gate.

```powershell
heroku login
heroku auth:whoami
heroku create APP --region eu --stack heroku-24
heroku addons:create heroku-postgresql:essential-0 --app APP
python scripts/configure_heroku.py --app APP --register-webhook
```

The configuration helper reads local `.env` and the card ID from `ledger/`, sends only allowlisted configuration to Heroku, and registers the hosted Stripe test webhook. It does not print API keys. Generated demo credentials are saved in ignored `.env.heroku`; keep that file private. The helper does not provision resources, commit files, or push code.

Review and commit the deployment files explicitly so local ledger changes, processed inbox deletions, secrets, and video artifacts are not accidentally included:

```powershell
git add -- .python-version .slugignore .gitignore .env.example Procfile app.json requirements.txt requirements-optional.txt server.py agent.py ledger.py ui/index.html scripts/heroku_release.py scripts/configure_heroku.py tests/test_ledger.py tests/test_hosted_server.py tests/test_heroku_config.py tests/test_hosted_agent.py docs/heroku.md README.md
git diff --cached --stat
git commit -m "Prepare persistent Heroku demo"
git push heroku HEAD:main
heroku ps:scale web=1:basic --app APP
heroku open --app APP
```

`heroku create` sets the `heroku` Git remote. For an existing app, use `heroku git:remote --app APP` before pushing. `app.json` also describes this deployment; creating from that manifest provisions the same paid resources.

## Stripe routing

Check the hosted URL with `heroku info --app APP`. In Stripe **test mode**, verify the registered endpoint points to that URL's `/stripe/webhook` and receives:

- `issuing_authorization.request`
- `issuing_authorization.created`
- `issuing_transaction.created`

In Stripe's **Issuing authorization settings**, verify the real-time authorization URL also points to the hosted gate and set timeout/error fallback to **DECLINE**. Creating a regular event endpoint does not replace checking this setting. Stripe requires an authorization response within two seconds. [Stripe real-time authorizations](https://docs.stripe.com/issuing/controls/real-time-authorizations)

`STRIPE_WEBHOOK_SECRET` must be the **hosted endpoint's** signing secret; the secret printed by a local Stripe CLI listener belongs to a different endpoint. If the account uses a separate Issuing authorization endpoint, supply its secret as `STRIPE_ISSUING_WEBHOOK_SECRET` as well. Never publish either secret. Keep the local listener off while cloud authorizations are active so two independent gates do not handle the same test purchase.

## Load memory and demonstrate

```powershell
heroku run --app APP python agent.py ingest-notes
```

Open the app and sign in with the credentials in `.env.heroku`. Process the sample inbox, then sync the policy and resulting decisions before the Cognee explanation:

```powershell
heroku run --app APP python agent.py sync-memory
```

One-off commands share `DATABASE_URL` with the web dyno. Cognee's local sync-marker files are ephemeral and are not shared across dynos, so a later sync can upload the same policy/history again. The PostgreSQL ledger remains the audit source of truth. `MEMORY_CHECK=file` can use packaged finance notes as a fallback, but a Cognee demo should use `MEMORY_CHECK=cognee` with reachable cloud memory.

## Verify before sharing

Local preparation checks: 68 tests pass, the deterministic policy replay matches `8552ceded9b0ced2`, and Python compilation/whitespace checks pass. Six PostgreSQL integration tests are pending a disposable `TEST_DATABASE_URL`; no real database or hosted payment flow has been tested yet.

1. Without credentials, the dashboard and `/api/state` return **401**; `/health` returns **200** with `storage: "postgres"` and `test_mode: true`.
2. A permitted request creates an approved **and captured** Stripe test transaction. Inspect authorization metadata for its decision ID, clauses, and policy version.
3. The Google Ads request waits for the named C3 approver before payment; the Meta Ads request is held under C7 with the recalled finance note; a disallowed category is declined.
4. After `sync-memory`, “Why did you pay Figma?” returns the applicable clauses and evidence; verify the Cognee section is populated.
5. Once all jobs finish, run `heroku ps:restart --app APP`. Sign in again and confirm intent/decision history remains and already processed inbox files do not return as new requests.

Use `heroku logs --tail --app APP` to diagnose build, startup, memory, or webhook failures. These are deployment checks to perform, not claims that the hosted flow has already passed.

## Restarts and shutdown

A restart or deployment interrupts running jobs; they are **not resumed**. Their job status may return 404. Saved decisions, intents, and processed-email receipts survive in Postgres; the browser never automatically resubmits the POST. Inspect the ledger and Stripe authorization/capture state before a manual retry. A receipt prevents a processed file from automatically replaying after a restart, but does not guarantee an interrupted payment completed.

To pause the web demo without deleting audit data:

```powershell
heroku ps:scale web=0 --app APP
```

The database continues to accrue charges while attached. Resume with `web=1:basic` before using the demo again.
