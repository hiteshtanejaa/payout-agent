# Payout Agent

An AI agent that pays contractor invoices via Stripe Connect (test mode) under a written policy, and explains every pound.
Built for the Briefcase × Stripe Hackathon (Track 01: Agents that spend), 15 Oct 2026.

- Deterministic policy engine (clauses P1–P7) decides; the LLM only extracts invoice fields and writes explanations.
- Cognee memory grounds the agent in policy, contracts and finance notes, and remembers every decision.
- Every payment is a Stripe test-mode transfer with an idempotency key and clause-level metadata.

Setup and run instructions: coming soon.
