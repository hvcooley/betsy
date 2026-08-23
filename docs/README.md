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

**`Route` loses the who-owns-this distinction.** Code has `self_care | call_clinic |
urgent_same_day | emergency_911`; the spec routes to `911 | ED_NOW | CALL_SURGEON |
CALL_ANESTHESIA | ROUTINE`. Bleeding goes to the surgeon, a prolonged block goes to anesthesia —
collapsing both into `call_clinic` is the refactor the spec explicitly warns against.

**`checkin` vs `case`/`conversation` naming.** `Summary.checkin_id` implies one entity; the data
model splits a durable `case` from a per-call `conversation`. Pick one before the SQLAlchemy
models are written.

**Tier semantics.** Code docstrings read Tier 1 as "emergent"; the spec defines it as "complex —
mandatory follow-up call", which also catches abandonment, proxy-reported yellows, low-confidence
extraction and schema hard-failures. The spec's definition is the one the ROI argument depends on.

**No length cap on `Summary.headline`.** The spec caps the one-liner at 140 characters, since it
is the only thing read for a Tier 3 case.

## Resolved divergences

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
