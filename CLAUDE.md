# Payout Agent — Briefcase × Stripe Hackathon (Track 01: Agents that spend)

Build window: 18:00–21:00, 15 Oct 2026. Demo video ≤ 2 min. Public GitHub repo. Stripe TEST MODE only.

## What we are building
An AI agent that pays contractor invoices via Stripe Connect transfers (test mode), under a written
policy, and explains every pound. Cognee is the agent's memory: policy grounding + decision history.

Judging: 35 works end-to-end (money moves in Stripe test mode) · 25 autonomy within explicit policy ·
20 explains every pound (audit trail) · 20 product/creativity. Side quest: best use of Cognee.

## Core design rules (do not break)
1. **Policy enforcement is deterministic Python**, never LLM-decided. The LLM only extracts invoice
   fields and writes explanations. Each check returns `{clause_id, passed, detail, evidence}`.
2. **Cognee grounds and remembers**: the policy (`data/policy.md`), contracts and finance notes are
   ingested and cognified into a graph; every decision is written back to Cognee.
3. **Every Stripe call uses an idempotency key** (`payout-{contractor_id}-{invoice_number}`) and
   metadata `{invoice_number, clause_ids, decision_id}`.
4. **SQLite audit log is the source of truth** (decisions, checks, Stripe IDs, approver). Cognee holds the
   same facts as memory so the agent can answer "why?" in natural language.
5. Fail closed: if memory lookup (P7) errors, HOLD rather than pay.
6. Secrets only in `.env` (gitignored). Commit `.env.example`.

## Stack
Python 3.10–3.13, FastAPI + uvicorn, `cognee==1.6.4`, `stripe`, `litellm` (already a Cognee dependency;
one LLM key for everything), `pydantic`, `pyyaml`, `python-dotenv`. A single static `ui/index.html` served by FastAPI.

## Data (already in repo)
- `data/policy.yaml` — machine rules P1–P7 with limits and `on_fail` (REJECT / HOLD).
- `data/policy.md` — same clauses in prose, ingested into Cognee.
- `data/contractors.yaml` — approved register (rate, monthly cap, contract id).
- TODO: `data/contracts/*.md` (one per contractor, prose restating rate/cap/scope),
  `data/finance_notes.md` (include: "Bright Pixel Ltd has an OPEN DISPUTE over September deliverables;
  hold all payments until resolved." — this drives P7 via Cognee),
  `data/inbox/*.txt` (invoice emails, see Demo script).

## Modules (suggested)
```
app/config.py      settings from .env
app/models.py      Invoice, Check, Decision (pydantic)
app/extractor.py   LLM (litellm, JSON output) email text -> Invoice{payee_name, invoice_number, date, hours, rate, amount_gbp, description}
app/policy.py      load policy.yaml; evaluate(invoice, contractor, history) -> list[Check]; decide(): any REJECT -> REJECTED, else any HOLD -> HELD, else APPROVED
app/memory.py      Cognee wrapper (see API below): ingest_knowledge(), recall_notes(payee), remember_decision(decision), ask(question)
app/payments.py    Stripe: fund_balance(), create_transfer(decision)
app/audit.py       SQLite: decisions table, monthly/daily totals, duplicate lookup
app/agent.py       process(email_text): extract -> match contractor -> Cognee grounding -> checks -> act -> audit -> remember
app/api.py         FastAPI routes (below) + serve ui/
scripts/setup_stripe.py   create 3 GB connected accounts (test data), fund platform balance, save ids to data/stripe_accounts.json
scripts/ingest.py         ingest policy/contracts/notes into Cognee and cognify
```

## API
- `POST /inbox/process` — process all files in `data/inbox/` (or one: `POST /invoices` with text)
- `GET /decisions` — audit log
- `POST /decisions/{id}/approve` `{approver}` — Finance Lead approves a HELD (P2 only) decision; re-run checks then pay; record approver
- `GET /decisions/{id}/why` — deterministic explanation: clauses, evidence, Stripe transfer id
- `POST /ask` `{question}` — natural-language "why did you pay X?" answered via Cognee over policy + decisions, must cite clause IDs and Stripe IDs

## Cognee 1.6.4 — verified API (from the 1.6.4 wheel)
```python
import cognee
from cognee import SearchType

await cognee.add(text_or_path, dataset_name="policy", node_set=["policy"])     # also accepts list
await cognee.cognify(datasets=["policy"])                                       # builds the graph (LLM calls; slow, do once)
results = await cognee.search(query_text="...", query_type=SearchType.GRAPH_COMPLETION,
                              datasets=["policy", "decisions"], system_prompt="...",
                              top_k=10, include_references=True)
await cognee.prune.prune_data(); await cognee.prune.prune_system(metadata=True)  # reset (verify before use)
```
SearchType values include GRAPH_COMPLETION, RAG_COMPLETION, CHUNKS, GRAPH_COMPLETION_COT, TEMPORAL.
V2 API also exists (`cognee.remember`, `cognee.recall`) — optional; V1 above is enough.
After each decision: `cognee.add(decision_text, dataset_name="decisions")` then cognify that dataset in a
background asyncio task (behind an asyncio.Lock) so the API stays responsive.

Cognee config is read from `.env` in the working directory:
```
# Option A: OpenAI (Cognee default)
LLM_PROVIDER=openai
LLM_MODEL=openai/gpt-5.6-luna      # Cognee 1.6.4 default; change if your key lacks it
LLM_API_KEY=sk-...
# embeddings default to openai/text-embedding-3-large with the same key

# Option B: Anthropic + local embeddings (pip install "cognee[anthropic]==1.6.4")
# LLM_PROVIDER=anthropic
# LLM_MODEL=anthropic/claude-sonnet-5-5
# LLM_API_KEY=sk-ant-...
# EMBEDDING_PROVIDER=fastembed
# EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
# EMBEDDING_DIMENSIONS=384

STRIPE_SECRET_KEY=sk_test_...
```
Untested on Windows by us — run `scripts/ingest.py` FIRST to flush out Cognee/env issues early.

## Stripe test-mode plan
1. **Fund the platform balance** (transfers need *available* funds):
   `stripe.PaymentIntent.create(amount=1000000, currency="gbp", payment_method="pm_card_bypassPending",
   confirm=True, automatic_payment_methods={"enabled": True, "allow_redirects": "never"})`
2. **Create connected accounts** (GB, Custom-style via controller props), request `transfers` capability,
   fill test identity data: dob 1901-01-01, address line1 `address_full_match`, UK test bank
   sort code `108800`, account `00012345`, `tos_acceptance={date, ip}`. Check
   `account.capabilities.transfers == "active"` before the demo.
3. **Pay**: `stripe.Transfer.create(amount=pence, currency="gbp", destination=acct_id,
   transfer_group=invoice_number, metadata={...}, idempotency_key=...)`.
4. Show in demo: Stripe Dashboard → test mode → Connect → transfers.
If Connect account activation fights us, verify in the Dashboard first; do not silently fake payments.

## Demo script (inbox files → expected outcome)
| File | Invoice | Expected |
|---|---|---|
| 01 | Acme INV-1001, 12h × £75 = £900 | PAID (all pass) |
| 02 | Northwind INV-2001, 20h × £60 = £1,200 | HELD P2 → approve in UI → PAID |
| 03 | Bright Pixel INV-3001, 8h × £50 = £400 | HELD P7 (open dispute recalled from Cognee) |
| 04 | Acme INV-1001 again | REJECTED P5 duplicate |
| 05 | "Quick Cash Consulting" £950 | REJECTED P1 unknown payee |
| 06 | Acme INV-1002, 30h × £75 = £2,250 | REJECTED P3 (900 + 2,250 > £2,500 cap) |
| 07 | Northwind INV-2002, 10h, £800 | HELD P4 rate mismatch (expected £600) |
Finish: ask "Why did you pay Acme £900?" → cites P1–P7 passes, contract CTR-ACME-2026, Stripe `tr_...`.

## Build order (time-boxed)
1. 18:20 setup_stripe.py + ingest.py working (biggest risks first)
2. 19:00 policy.py + audit.py + agent.py on inbox (CLI) — money moving
3. 19:45 FastAPI + ui/index.html (inbox, decisions table w/ status badges + Stripe links, approve button, Ask box)
4. 20:15 README (architecture, policy, how to run), push public repo
5. 20:30 record demo, submit by 21:00
