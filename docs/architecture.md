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
| LLM | Anthropic API — Sonnet-class for turns, same or larger for summaries | Strong instruction-following and structured output; BAA available when eventually needed. The turn model is a **setting**, not a constant: it starts Sonnet-class, and whether it should stay there is a question for the eval harness — extraction accuracy is what the safety rules run on, so the choice needs measuring against the scenario set rather than assuming. Upgrade once token usage is characterised and the end-to-end scenarios pass on a real key. |
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
  │          draft_reply: str,
  │          draft_transition_reply: str }
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
harness scores against. Persist them even when validation failed. The row written on a *failed*
turn does not currently carry the model's literal response, only the reason it was rejected; that
is a known gap against this step rather than an accepted design — see "`raw_response` is lossy on
exactly the turns it matters most" in [README.md](README.md).

### Step 2 is an interface, and the model is one implementation of it

The call in step 2 is expressed as a one-method turn-engine interface rather than a function. The
deterministic layers are therefore runnable, demonstrable and testable end to end with no API key,
against hand-authored extractions — which is how a scenario can assert that a *tier* is correct
rather than that a paraphrase was.

That matters beyond convenience. Steps 4 through 7 are the safety-critical half of the pipeline,
and their properties are properties of the **ordering**: rules must be evaluated against the topic
that was active when the message arrived, not the one the patient is about to be moved into; a red
halt must record a reason distinguishable from abandonment; the audit row must be written before
the rules run. None of that can be tested by testing the rule engine, and none of it depends on a
model. Building it first means the LLM arrives as an implementation of a contract the rest of the
system has already been proven against, instead of the thing everything is debugged through.

Three implementations exist. Two are deterministic doubles — one replays authored extractions, one
parses free text with a keyword table so the flow can be driven by hand — and neither may ever be
used as a fallback when a real call fails: a check-in that silently degrades to keyword matching is
worse than one that stops, because the record would not show which it was. The third is the real
Anthropic call, and it is the only one that may speak to a patient.

Two consequences of the ordering are worth stating, since neither is obvious from the diagram:

- **A drafted reply is stale once its topic closes, so the call drafts twice.** The reply is
  written in step 2, before step 6 knows whether the turn satisfied the topic. When it did, the
  ordinary draft asks something already answered. Rather than fall back to canned text, the engine
  is given the *predicted* next topic — read one step down the protocol queue, before the cursor
  moves — and returns a second draft phrased as a transition into it. Step 7 picks between them,
  and uses the transition **only if the topic that actually became active is the one it was written
  for**. Because step 6 closes at most one topic per turn (see below) that check passes whenever a
  topic closed; it is verified anyway, so relaxing the one-topic rule later degrades the wording
  rather than misleading a patient. Anything unverified falls back to the next topic's
  `opening_question`, which the protocol already designates as the clinician-authored fallback. The
  model phrases both branches, the protocol engine still decides which one happened, and the
  ordering does not change.
- **Green reassurance is scoped like the rules.** It is matched against the topic the turn was
  judged under, for the same reason, and suppressed entirely once any rule has fired — including on
  an earlier turn. See conversational constraint 3 in [safety-rules.md](safety-rules.md).

### What the model returns, and what is done to it before it is believed

The response schema is not `TurnExtraction`. Provenance — protocol version, prompt version, topic
id, turn index, the raw message — is stamped by the caller after the call, and does not appear on
the wire schema at all, so a response cannot label its own turn. An extraction that named the wrong
topic would let one topic's answers be merged into another's slots.

Slot answers arrive as a list naming their slot rather than as an open dict, and **every value is
put through the owning slot's `accepts()` predicate** — the same check the protocol engine uses to
decide whether a slot is filled — before it is stored. An answer for a slot the active topic did
not declare, or one whose value the slot cannot hold, is dropped and the reason recorded on the
turn. Dropping is the fail-closed choice: an unusable answer that was stored anyway would let the
script advance past a question nobody answered.

*Known tradeoff, deliberately taken for the MVP.* A slot value is typed on the wire as a
`bool | int | float | str | null` union, because one static schema can be generated once and cached
across every turn. The union is wide enough for the model to return the wrong type for a slot, and
`accepts()` is the only thing that catches it — a caught failure, but a failure that costs a turn.
The intended replacement is to **generate a topic-specific JSON schema from the YAML slot
definitions on each turn**, so the active topic's slots appear as named, correctly typed properties
and a wrong-typed answer becomes unexpressible rather than rejected. That keeps the YAML
authoritative without flattening every slot to one union; the costs are dynamic schema generation
per topic and the loss of a single schema shared across turns. Revisit once the eval harness can
measure how often the union is actually being mis-filled — if the answer is "never", the simpler
schema has earned its place.

## Seams for the deferred roadmap

None of this gets built now. These are the places where the MVP is shaped so the next thing is
additive rather than a rewrite.

| Future capability | Seam already in place |
| --- | --- |
| Voice (Twilio/Retell) | The turn pipeline takes text in / text out with no transport assumptions. Voice becomes an adapter in front of the message endpoint; the protocol engine never changes. |
| EHR / patient data ingest | `case` is a plain table populated by an API call. A CSV batch importer writes to the same table. No prompt changes. |
| RAG over patient history | Step 1 is "build context". Retrieved history becomes another context block. |
| Review dashboard | `summary.tier`, `escalation.route`, the review-queue endpoint and the `review` table already exist. The dashboard is a frontend project, not a backend one. |
| Raising what a patient volunteered early, when its topic opens | One topic closes per turn and off-topic answers are discarded from protocol progress, so the behaviour to change is additive: retain the volunteered observation on the protocol state, surface it into the turn prompt for the topic it belongs to, and let the model open by asking the patient to confirm or expand on it. The clinical fields already survive the turn, so nothing about safety evaluation moves. See [protocol.md](protocol.md). |
| Scheduled calls timed to block regression | The block-duration table already exists as data. Add a scheduler that reads it. |
| Pre-op protocol | The protocol is versioned YAML keyed by `protocol_id`. Pre-op is a new file, not a new system. |
