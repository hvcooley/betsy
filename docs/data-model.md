# Data model and API surface

Design principle: store PHI-shaped fields in as few places as possible, so the eventual compliance
pass has a small surface area. Use Postgres-safe types (UUID strings, JSON columns) throughout even
though MVP runs on SQLite.

## Tables

```
patient          id, external_ref, first_name, last_name, dob, phone, is_synthetic

case             id, patient_id, procedure_name, procedure_category,
                 anesthesia_type, block_type, block_laterality,
                 surgery_at, discharge_at, surgeon_name,
                 clinical_notes, expected_block_duration_hours

conversation     id, case_id, protocol_id, protocol_version,
                 status (in_progress|completed|abandoned|escalated|terminated),
                 state_json, started_at, ended_at, hours_post_op

message          id, conversation_id, seq, role (assistant|patient|system),
                 content, is_templated, template_id, created_at

turn_analysis    id, message_id, extraction_json, model, prompt_version,
                 latency_ms, input_tokens, output_tokens,
                 raw_response, validation_retries
                 -- audit + eval substrate. Never skip this table.

finding          id, conversation_id, source_message_id, code, value_json,
                 severity (green|yellow|red), confidence, created_at

escalation       id, conversation_id, rule_id, rule_version, severity,
                 route (call_911|ed_now|call_surgeon|call_anesthesia|routine),
                 triggered_at, template_id, message_shown
                 -- one row per route: a rule owing two owners writes two.

summary          id, conversation_id, tier (1|2|3), one_liner,
                 structured_json, narrative, model, prompt_version,
                 generated_at

review           id, conversation_id, reviewer_name, action (approve|needs_call|
                 escalated), note, reviewed_at
```

### Two notes on future-proofing without over-building

- `summary.tier` and the `review` table are the only concessions to the long-term dashboard. They
  cost ~20 lines now and save a schema migration later. Everything else in the long-term plan
  stays unbuilt.
- `escalation.route` matters more than it looks. Not every red flag goes to anesthesia — a
  bleeding wound goes to the surgeon, chest pain goes to 911. Getting routing into the model on
  day one prevents a painful refactor when the dashboard needs to sort by who owns the problem.
  The `Route` enum carries that owner, and a rule that owes two owners records both routes rather
  than collapsing to the more urgent one — see "`Route` loses the who-owns-this distinction" under
  resolved divergences in [README.md](README.md).

### `case` carries the block-regression inputs

`anesthesia_type` records the **primary technique only**; `block_type` records **which block has
to wear off**, null when there is none. The two are independent, so a general with an interscalene
block is `general` + `interscalene` rather than a fused enum member.

`block_type` is a closed vocabulary (the `BlockType` enum) because it keys the block-regression
windows in the safety rules YAML — that is what makes `BLOCK_PROLONGED` a data lookup rather than
hardcoded clinical knowledge. `block_laterality` and `expected_block_duration_hours` complete the
picture; the latter is the per-case override when an adjuvant or an unusual agent means the
default window does not apply. See the block table in [safety-rules.md](safety-rules.md).

When this table is written, move the fail-closed check currently on `Summary` here: a spinal, or a
block used as the primary anesthetic, must record a `block_type`.

## API surface

```
POST   /v1/cases                              create synthetic case
POST   /v1/conversations                      {case_id} → conversation + opening message
POST   /v1/conversations/{id}/messages        {text} → reply, state, escalation?
POST   /v1/conversations/{id}/close           triggers summary + tiering
GET    /v1/conversations/{id}                 transcript + findings + summary
GET    /v1/conversations?tier=&status=        review queue (dashboard-ready)
POST   /v1/conversations/{id}/review          {reviewer, action, note}
GET    /v1/protocols/{id}/versions
GET    /health
```

Auth: a single shared bearer token from `.env`. Nothing more until real data is involved.
