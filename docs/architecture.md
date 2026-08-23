# Architecture

## The central decision

> **The LLM never owns conversation control flow, and never owns the escalation decision.**

The system is "a decision tree with AI filling the gaps". Concretely:

- A **deterministic protocol engine** owns which topic we are on and when we move on.
- The **LLM does two bounded jobs** inside a topic: interpret the patient's natural-language
  answer into structured fields, and phrase the next question in plain, warm English.
- A **deterministic rule engine** evaluates escalation against the validated structured fields —
  never against free text.
- When a red flag fires, the LLM's proposed reply is **discarded** and replaced with fixed,
  clinician-authored text.

This buys three things nothing else does: escalation logic that is unit-testable, escalation
language that cannot hallucinate, and a system whose failure modes are enumerable when an
anesthesiologist asks "what happens if…".

Consequence: because the safety gate can discard a drafted reply, patient-facing responses cannot
be streamed.

## Stack

| Layer | Choice | Rationale |
| --- | --- | --- |
| Language | Python 3.11+ | LLM ecosystem is Python-first; Pydantic gives validated structured output nearly free |
| Web framework | FastAPI | Pydantic-native, auto OpenAPI docs, trivial to stand up |
| LLM | Anthropic API — Sonnet-class for turns, same or larger for summaries | Strong instruction-following and structured output; BAA available when eventually needed |
| LLM access | Direct SDK calls | **No agent framework.** The conversation is a scripted state machine; frameworks add abstraction debt and make failures harder to trace. The single biggest velocity decision in the spec. |
| Structured output | Pydantic models + tool-use / JSON schema | Validation failures become retries, not silent corruption |
| DB | SQLite via SQLAlchemy 2.x | Zero setup. Use Postgres-safe types (UUID strings, JSON columns) so the swap is a connection-string change. |
| Migrations | Alembic from day one | Cheap now, painful to retrofit |
| Config | pydantic-settings + `.env` | — |
| Testing | pytest | — |
| Demo UI | Two static HTML pages + vanilla JS served by FastAPI | No build step, no npm, no framework churn. Swap for React later if it ever matters. |

## Turn processing pipeline

One patient message in, one assistant message out:

```
patient message
  │
  ├─ 1. Build context: case facts, protocol topic + goal, filled slots,
  │                    last N messages, active findings
  │
  ├─ 2. ONE LLM call → validated Pydantic object:
  │        { extraction: {slot: value, confidence},
  │          signals: [symptom codes observed],
  │          patient_question: str|null,
  │          off_topic: bool, proxy_detected: bool,
  │          draft_reply: str }
  │        └─ schema validation fails → retry ≤2 → hard-fail to human handoff
  │
  ├─ 3. Persist turn analysis (always, including failures)
  │
  ├─ 4. Rule engine over extraction + case + accumulated findings
  │        → zero or more escalations with severity + route
  │
  ├─ 5. SAFETY GATE
  │        red escalation  → discard draft_reply, emit templated escalation copy,
  │                          mark conversation escalated, stop protocol
  │        yellow          → keep draft_reply, record finding, continue
  │        none            → keep draft_reply
  │
  ├─ 6. Protocol engine: slots satisfied or max_turns hit → advance topic
  │
  └─ 7. Persist assistant message, return reply + state
```

Step 2 is deliberately a **single** call rather than separate extract-then-generate calls: it
halves latency and cost, and safety is unaffected because step 5 can always throw the draft away.

Step 3 is non-negotiable — the turn-analysis rows are the audit trail and the substrate the eval
harness scores against. Persist them even when validation failed.

## Seams for the deferred roadmap

None of this gets built now. These are the places where the MVP is shaped so the next thing is
additive rather than a rewrite.

| Future capability | Seam already in place |
| --- | --- |
| Voice (Twilio/Retell) | The turn pipeline takes text in / text out with no transport assumptions. Voice becomes an adapter in front of the message endpoint; the protocol engine never changes. |
| EHR / patient data ingest | `case` is a plain table populated by an API call. A CSV batch importer writes to the same table. No prompt changes. |
| RAG over patient history | Step 1 is "build context". Retrieved history becomes another context block. |
| Review dashboard | `summary.tier`, `escalation.route`, the review-queue endpoint and the `review` table already exist. The dashboard is a frontend project, not a backend one. |
| Scheduled calls timed to block regression | The block-duration table already exists as data. Add a scheduler that reads it. |
| Pre-op protocol | The protocol is versioned YAML keyed by `protocol_id`. Pre-op is a new file, not a new system. |
