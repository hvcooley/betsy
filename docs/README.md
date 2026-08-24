# Betsy design docs

Condensed from the BETSY MVP Technical Specification & Implementation Plan (v0.1). These docs
are the **intent** of the system; the code is the implementation of that intent. Where the two
disagree, that is a bug or an unresolved decision — see [Open divergences](#open-divergences)
below.

## Index

| Doc | Covers | Code it governs |
| --- | --- | --- |
| [scope.md](scope.md) | In/out of scope, hard rules, carried risks | everything |
| [architecture.md](architecture.md) | The central LLM-vs-deterministic split, stack, turn pipeline, future seams | everything |
| [data-model.md](data-model.md) | Tables and the HTTP API surface | `app/db/`, `app/main.py` |
| [protocol.md](protocol.md) | Check-in topics, slots, branching | `app/protocol/` |
| [safety-rules.md](safety-rules.md) | RED/YELLOW/GREEN rules, block-regression data, conversational constraints | `app/safety/` |
| [triage-and-summary.md](triage-and-summary.md) | Tier assignment, summary generation | `app/triage/`, `app/summary/` |
| [evals.md](evals.md) | Simulated patients, scenario set, metrics and gates | `evals/` |
| [roadmap.md](roadmap.md) | Build phases, clinical SME open questions | — |

## Conventions for keeping these current

- **No file paths in these docs.** Directory layout lives in exactly one place — the Architecture
  section of `CLAUDE.md` (mirrored in `AGENTS.md`). Restructuring the repo should never require
  editing `docs/`. The table above is the one exception; it maps docs to layers, so it needs a
  touch-up if a layer is renamed.
- **Docs describe intent, artifacts hold truth.** Once `app/protocol/definitions/postop_v1.yaml`
  and `app/safety/rules/postop_v1.yaml` are populated, those files are authoritative for topic and
  rule *content*; `protocol.md` and `safety-rules.md` keep the rationale, thresholds and the list
  of what must exist. Until then the docs carry the content, because the YAML is still a stub.
- **Versioning.** Protocol, rules and prompts are versioned in lockstep by `_v1` filename suffix.
  A new version is a new file, not an edit, because clinician sign-off attaches to a version.
  When `_v2` lands, note per-version differences in the relevant doc rather than overwriting.
- **Record decisions where they bind.** A clinical decision goes in `safety-rules.md` or
  `protocol.md`; an implementation decision goes in `architecture.md`. Resolved SME answers move
  out of `roadmap.md` into the doc they affect.

## Open divergences

Points where the shipped skeleton and the spec do not yet agree. Each needs a decision, not just
a rename. Referred to by name elsewhere in these docs, so the names are stable.

_None currently._

## Resolved divergences

**No length cap on `Summary.headline`** — *resolved: added the spec's 140-character cap.*
`headline` now sets `max_length=140` in [`app/domain/schemas.py`](../app/domain/schemas.py), since
it is the only thing read for a Tier 3 case.

**`checkin` vs `case`/`conversation` naming** — *resolved: adopt the spec's split.* A patient can
have more than one post-op call for the same episode (e.g. separate POD1 and POD3 check-ins), so a
single `checkin` entity would conflate the durable per-patient episode with each individual call.
The domain layer now names things the way [data-model.md](data-model.md) does: `Summary` — the
record of one completed call — carries `conversation_id`, not `checkin_id`, and `CheckinStatus`
is renamed `ConversationStatus`. There is no `Case` or `Conversation` Pydantic model yet since
`app/db/models.py` is still a stub; when those SQLAlchemy models are written, `case` owns the
patient/procedure/anesthesia facts and has many `conversation` rows, each with its own status and
its own `Summary`. The case-level fields still living on `Summary`
(`anesthesia_type`, `block_type`, `procedure`) are the pre-existing carry-over noted in
[data-model.md](data-model.md#case-carries-the-block-regression-inputs) — they move to `case` once
that table exists, not part of this rename.

**`Route` loses the who-owns-this distinction** — *resolved: the spec's five routes, plus an owner
and a set-valued disposition.* `Route` is now `call_911 | ed_now | call_surgeon | call_anesthesia |
routine`, so bleeding reaches the surgeon and a prolonged block reaches anesthesia instead of both
landing in one `call_clinic` bucket. Stored values stay lowercase snake_case like every other enum;
`call_911` rather than the spec's bare `911` because an unquoted `911` in a rules YAML parses as an
integer.

Two consequences fell out of the change, and both are load-bearing:

- **A route is an urgency *and* an owner.** `CALL_SURGEON` and `CALL_ANESTHESIA` are equally urgent,
  so `rank` deliberately ties them and `Route.owner` (a `RouteOwner`) carries the difference — that
  is what the review dashboard sorts by, per [data-model.md](data-model.md). Nothing may pick
  between the two by rank.
- **Dispositions therefore do not reduce to a maximum.** `SURGICAL_BLEEDING` is `CALL_SURGEON +
  ED_NOW`, and a check-in with both a bleeding wound and a stuck block owes two different people, so
  `Finding.routes` and `Summary.routes` are lists, not single values. `Route.combine` merges them:
  the most urgent route per owner, worst first, with `routine` dropping out as soon as anything else
  fires and standing alone when nothing does. `Route.most_urgent` still returns one route, but only
  for choosing which single template the patient is shown — never for deciding who gets told.

An escalation row stores one route, so a rule with two routes writes one row per route.

**Anesthesia type vs. block type** — *resolved: two orthogonal fields.* The spec branched on fused
values (`general_with_block`, `regional_block`, `cse`, `local_only`) that a single flat enum could
not express. `AnesthesiaType` now means the **primary technique** only (and gained
`combined_spinal_epidural`); a new `BlockType` records **which block has to wear off**, `None` when
there isn't one. A GA with an interscalene block is `general` + `interscalene`, and matches both
the recovery topic and the block-regression topic.

The payoff beyond representability: `BlockType` is the key for the block-regression windows in
`app/safety/rules/postop_v1.yaml`, so `BLOCK_PROLONGED` is a data lookup, the `block_regression`
topic's applicability collapses to "is there a block?", and the deferred regression-timed
scheduler reads the same table. A test requires every `BlockType` member to have a window, so a
block type cannot ship without one and fail open. `Summary` rejects a spinal or a
block-as-primary-anesthetic that records no `block_type`, for the same fail-closed reason.

Still to do when the `case` table lands: move `block_type` (plus `block_laterality` and
`expected_block_duration_hours`) onto it, and move that validator along with it.
