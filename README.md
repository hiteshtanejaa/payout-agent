# Treasurer — a buying agent that can only spend what the policy says

Briefcase × Stripe hackathon, London, 15 Oct 2026 · Track 01 "Agents that spend".

**The LLM reads the request. Memory is consulted. A deterministic policy decides. Stripe is the final gate.**

- `extractor.py` — the LLM turns a free-text request email into `{merchant, category, amount}`. It never decides
  and never touches Stripe. If it can't extract, nothing is proposed (fail closed).
- `recall.py` + Cognee — **C7**: before anything is cleared, the agent recalls the finance team's notes about the
  merchant from Cognee. Cognee retrieves; plain Python matches hold/dispute/under-review, so the verdict is
  deterministic. If the memory lookup fails, the purchase is HELD.

- `policy.yaml` — seven numbered clauses. C1/C2/C4 are also pushed into the card's
  `spending_controls`, so **Stripe enforces them even if our code is dead**.
- `server.py` — answers `issuing_authorization.request` inside 2 s, citing the clause,
  and writes the clause ids into the authorization's metadata (visible in the Dashboard).
- `ledger/decisions.jsonl` — append-only audit trail of every decision.
- Cognee holds the policy + every decision, so "why did you pay X?" is answered from memory.

Demo video: _[paste link]_ · Replay hash: `8552ceded9b0ced2` (`python replay.py --expect 8552ceded9b0ced2`)

---

## First 10 minutes (the steps that eat hackathon time)

1. **Stripe Dashboard → toggle Test mode → Issuing → activate.** If your account's country
   doesn't offer Issuing, stop and use the Connect fallback at the bottom of this file.
2. **Fund the test Issuing balance**: Dashboard → Balances → Add funds (test mode).
   An empty balance looks like a policy bug (`insufficient_funds` declines).
3. `cp .env.example .env` → paste `sk_test_...`.
4. `pip install -r requirements.txt -r requirements-optional.txt` (one command: two parallel installs fight over litellm/pydantic; cognee is slow).
5. `stripe listen --api-key sk_test_... --events issuing_authorization.request,issuing_authorization.created,issuing_transaction.created --forward-to localhost:8000/stripe/webhook`
   → paste `whsec_...` into `.env` (CLI ≥1.5x requires `--events`; `--api-key` skips `stripe login`).
6. `python setup_stripe.py` → prints cardholder + card ids. If it fails on a merchant category
   name, fix `allowed_categories` in `policy.yaml` and rerun.
7. `python -m uvicorn server:app --port 8000` (terminal 3; plain `uvicorn` may not be on PATH on Windows).
7b. `python agent.py ingest-notes` — puts `notes/finance_notes.md` into Cognee (slow, once). With `COGNEE_SERVICE_URL` set it
   uses Cognee Cloud (local Cognee needs a native graph lib that is missing on Windows). If Cognee fights you,
   set `MEMORY_CHECK=file` in `.env` so C7 reads the notes file directly and keep building.
8. `python simulate.py --merchant Figma --category computers_peripherals_and_software --amount 120` — smoke test.
   Expect the gate to **decline it under C5** (nobody proposed it). (`stripe trigger issuing_authorization.request`
   uses a US/USD fixture, which a GB Issuing account rejects.) If you see that line in the server log, the whole pipe works.
9. Record a 10-second throwaway Loom now. macOS screen-recording permission prompts mid-demo cost 5 minutes.
10. `git init && git add -A && git commit -m "scaffold"` — fresh repo, timestamps after 18:00.

## Finance UI

With the server running, open **http://localhost:8000/**: card limits (which clauses Stripe enforces), month-to-date spend,
a *Needs a person* panel (Approve and pay for C3, recalled note for C7 holds), every decision with clause chips and
Stripe links (click a row for the evidence), and an *Ask why* box. **Process inbox** runs the same flow as
`python agent.py inbox`, but leaves escalations for the Approve button instead of prompting in the terminal.

## Run order

```
python agent.py inbox                 # processes inbox/*.txt → proposes → executes or escalates
python simulate.py --merchant Apple --category computers_peripherals_and_software --amount 900
python agent.py pending               # intents waiting for a human
python agent.py approve int_xxxx --approver sarthak && python agent.py execute int_xxxx
python agent.py sync-memory           # push policy + decisions into Cognee (once, before the "why" shot)
python agent.py why "Why did you pay Google Ads £350?"
python agent.py chat "Buy a Figma seat for £120"   # LLM mode, needs OPENAI_API_KEY + openai-agents
```

Dashboard view to keep open while recording: **Issuing → Authorizations** (test mode).

## The 2-minute demo, shot by shot

Two windows side by side: left = terminal, right = Stripe Dashboard → Issuing → Authorizations.

| Time | On screen | Say |
|---|---|---|
| 0:00 | `policy.yaml` scrolled to the six clauses; then `setup_stripe.py` output showing the card and its spending_controls | "Treasurer can only spend what this policy says. Three of the six clauses live in the card itself — Stripe enforces them even if our code is down." |
| 0:15 | `python agent.py inbox` picks up `01-figma.txt` (£120, software). Server log: `APPROVED … clauses=C1,C2,C4,C5`. Dashboard: new authorization **Approved**, open it, metadata shows `clause_ids`, `decision_id`, `policy_version`; then the captured transaction. | "A request lands in the inbox. Nobody touches Stripe. The gate approved it in under a second and wrote the clauses into Stripe's own record." |
| 0:45 | `python simulate.py … --amount 900` → `DECLINED reason=authorization_controls` (Stripe's name for spending controls in API 2026-09-30). Dashboard: **Declined**, reason spending controls. Server log shows **no** `[gate]` line for it, only `[stripe] DECLINED`. | "Now I bypass the agent and hit the card directly, like a rogue tool call. Stripe declined it before our webhook was even asked. The guardrail is in the network, not the prompt." |
| 1:05 | Inbox file 2: Google Ads £350 → `needs approval (C3)` prompt → type `sarthak` → `approved by sarthak → clause C3 satisfied` → executes → Dashboard **Approved**, metadata shows `approver: sarthak`. | "Over £200 needs a named human. The approval is recorded with a token, then the card is used — and the authorization carries the approver's name." |
| 1:25 | Same inbox run, `04-meta-ads.txt` (free-text email). `🧠 LLM extracted: Meta Ads £150` → `HOLD … failed=C7`, with the recalled note printed. No authorization appears in the Dashboard. | "The LLM read a plain email, but before spending, the agent checked its memory: finance flagged Meta Ads as under review. Held — and if memory is unreachable, it holds too." |
| 1:35 | Kill the server (Ctrl-C), restart it, then `python agent.py why "Why did you pay Google Ads £350?"` → answer names C3, the approver, the token, the authorization id, month-to-date £470 of £2,000. | "Restarted from scratch. The answer comes from memory, not from the chat: the clause, the approver, the Stripe id." |
| 1:55 | Repo URL + `python replay.py --expect 8552ceded9b0ced2` → `OK`. | "Same policy, same transactions, same decisions, every time." |

Record at 20:15 after one dry run at 20:00. Judges stop at 2:00 — put the Stripe-level decline before 1:05.

## Time budget (London time) and cut order

- 18:35–18:50 first-10-minutes list above
- 18:50–19:20 approve path end to end (inbox → gate → Dashboard metadata → capture)
- 19:20–19:35 Stripe-level decline shot + `issuing_authorization.created` logging
- 19:35–20:00 escalation path (C3) end to end
- 20:00–20:15 Cognee: `sync-memory`, `why`, restart test
- 20:15–20:30 dry run, then record
- 20:30–21:00 submit; paste video link at the top of this README

Cut in this order if behind: Cognee (the ledger `why` still works) → LLM chat mode (the inbox trigger is the
autonomy story; the model is cosmetic) → the restart shot. **Never cut the Stripe-level decline or the metadata close-up.**

## Verification that doesn't rely on anyone's word

1. **Dashboard → Issuing → Authorizations**: each demo authorization is clickable; approved ones show
   `clause_ids`, `decision_id`, `approver`, `policy_version` in metadata; the £900 one shows *Declined — spending controls*.
2. **Dashboard → Developers → Events**: `issuing_authorization.request` events with our response; timing visible.
   Miss 2 s and Stripe decides without us (`webhook_timeout` shows up in the trail).
3. **Stripe-level decline**: `simulate.py --amount 900` is declined with reason `authorization_controls` and the server
   log shows no `[gate]` line — the webhook was never consulted.
4. **Replay**: `python replay.py --expect 8552ceded9b0ced2` passes on any machine with this `policy.yaml`.
5. **Cold viewer**: someone who hasn't seen the project watches the video and answers
   "which clause authorised the £350 payment, and who approved it?" from the video alone.
6. **Memory survives restart**: kill the server, run `why`, the clause + approver + authorization id still come back.
7. **Audit trail is the source of truth**: `ledger/decisions.jsonl` is append-only and committed; `GET /decisions` serves it.

## How the pieces fit

```
inbox/*.txt ──► agent.py propose() ──► policy.evaluate(stage=proposal) ──► intent {cleared | awaiting_approval | declined}
                                                                              │
                                   human: agent.py approve ──► evaluate(approval) ──► cleared
                                                                              │
                        agent.py execute ──► test_helpers.issuing.authorizations.create ──► Stripe
                                                                              │
          Stripe spending_controls (C1/C2/C4) ──► decline, or ──► issuing_authorization.request ──► server.py
                                                                              │
                             evaluate(stage=authorization): C5 match intent ──► {approved, metadata: clauses}
                                                                              │
                       ledger/decisions.jsonl (append-only) ──► Cognee (policy + decisions) ──► agent.py why
```

## Earlier plan kept in the repo

`fallback_connect/` holds the first design (contractor invoices paid by Stripe Connect transfers, clauses P1–P7).
It is the fallback below if Issuing is not available.

## Fallback if Issuing is not available on your account: contractor payouts via Connect

Same evaluator, same ledger, same `why`. Replace `execute()` with:

```python
acct = stripe.Account.create(type="express", country="GB", capabilities={"transfers": {"requested": True}})
stripe.Transfer.create(amount=it["amount"], currency="gbp", destination=acct["id"],
                       metadata={"decision_id": rec["decision_id"], "clause_ids": ",".join(res["clause_ids"])})
```

The "Stripe-level guardrail" story is weaker (no network-enforced limits), so compensate: create a **restricted API key**
with only Transfers write access for the agent process, and show that key being used. Money still visibly moves:
Dashboard → Connect → Transfers.

## Stripe calls used (test mode)

- `stripe.issuing.Cardholder.create(type="individual", status="active", billing.address.country="GB", individual.card_issuing.user_terms_acceptance={…})`
- `stripe.issuing.Card.create(cardholder, currency="gbp", type="virtual", status="active", spending_controls={spending_limits:[{amount:50000, interval:"per_authorization"},{amount:200000, interval:"monthly"}], allowed_categories:[…]})`
- Real-time authorization: respond to `issuing_authorization.request` with `200`, header `Stripe-Version`, body `{"approved": bool, "metadata": {...}}` within 2 s
- `stripe.issuing.Authorization.TestHelpers.create(amount, currency, card, merchant_data={name, category, city, country})` then `.capture(id)`
- CLI: `stripe listen --events issuing_authorization.request,issuing_authorization.created,issuing_transaction.created --forward-to localhost:8000/stripe/webhook`
