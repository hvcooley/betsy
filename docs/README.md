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

**`raw_response` is lossy on exactly the turns it matters most** — *needs a decision; no change
made.* Invariant 4 says turn analysis is persisted on every turn including the failures, because
that row is the audit trail and the eval substrate. On the failure path it is currently *thinner*
than that intends. The turn engine sends its request through the SDK's `messages.parse`, which
validates the response inside the SDK and raises, so the model's literal JSON never comes back to
us. What is stored in `raw_response` on a schema failure is Pydantic's report — the failing field
path and the value it rejected — rather than the payload that produced it.

That is enough to answer "what was wrong with it", and enough for a reviewer to see why the turn
hard-failed. It is *not* enough to re-score the turn later against a changed schema, or to tell
whether the model produced something reasonable that a schema bug rejected — which is precisely
the question a hard failure raises. So the audit row is weakest at the one point where the record
is least trustworthy, which is the same reasoning that makes a hard failure Tier 1 in the first
place.

The clean fix is to send the call as `messages.create` with an explicit `output_config` format and
validate in our own code, holding the raw text either way. That needs the SDK's JSON-schema
transform to be public API; it is a private module path today, and depending on a private path
from the module that owns the audit trail was judged the worse of the two trades. Either of two
things settles it: the transform becoming public, or measuring how often schema failures actually
occur once the engine runs against a real key — if the answer is "effectively never", the gap
costs nothing and the current choice stands on its own. Commented at the point it matters in the
turn engine; the affected column is `turn_analysis.raw_response` in
[data-model.md](data-model.md).

## Resolved divergences

**Every GREEN rule was attached to no topic, so the reassurance band could never fire** —
*resolved: added the five rule references to the topics they belong to.* Rules are evaluated as
`global_rules` plus the active topic's list, and all five `EXPECTED_*` rules appeared in neither.
They were reachable only by a caller passing `topic_rules` by hand, which is exactly what the
safety unit tests do — so the band was fully covered by tests and fully dead in the product. The
first end-to-end run of a check-in found it in one turn.

Fixed by editing the protocol definition rather than the rules file: the rules were correct, the
script simply never asked for them. `EXPECTED_MILD_NAUSEA` goes to the PONV topic,
`EXPECTED_BLOCK_TINGLING` to block regression, and the sore-throat, hoarseness and grogginess rules
to anesthesia recovery. This is not a clinical change and did not need a version bump: a GREEN rule
produces no `Finding` by construction, so it cannot escalate, cannot route, and cannot move a tier
— it can only supply approved wording that was already written and already unreviewed.

`tests/test_flows.py` now asserts that no GREEN rule is orphaned, so the band cannot go dead again.
The general lesson is the reason the flow tests exist at all: a rule can be individually correct,
individually tested, and unreachable.

**`SymptomCode` could not express half the RED rules** — *resolved: extended v1 in place.* Writing
the rule file surfaced that the vocabulary had no way to say perioral numbness, metallic taste or
tinnitus (`LAST_SYMPTOMS`), calf swelling (`DVT_SUSPECTED`), saddle numbness or bowel incontinence
(`NEURAXIAL_HEMATOMA`), facial and tongue swelling (`ANAPHYLAXIS_LATE`), muscle rigidity or dark
urine (`MH_SUSPECTED`), or a soaked dressing (`SURGICAL_BLEEDING`) — so those rules could not have
been written at all, and the RED set would have shipped knowingly incomplete against a 100%
recall gate.

The versioning convention says a revision ships as a `_v2` set rather than an edit. It was extended
in place anyway, because that convention exists to protect **clinician sign-off**, and this
vocabulary has never been signed off — its own docstring calls it a v1 draft pending review — nor
is there a single stored row to invalidate. Shipping a parallel `_v2` enum with a discriminator on
`TurnExtraction` would have bought version safety for content nobody has reviewed. The convention
starts binding at sign-off. `tests/test_domain.py` pins the full value set, so the change had to be
deliberate.

Two of the new groups are split on cause rather than anatomy, which is why `eye_irritation` does
not sit with the dental codes: **procedural instrumentation injury** (`dental_injury`,
`tongue_laceration`) is caused by the airway device, while **exposure / positioning injury**
(`eye_irritation`) is caused by incomplete lid closure and a lost blink reflex under anesthesia.
That is why a corneal abrasion turns up after cases involving no airway device at all, and the
split keeps the vocabulary honest about which mechanism a finding implicates.

**Topic-scoped rules could not catch a volunteered red flag** — *resolved: added `global_rules`.*
See [safety-rules.md](safety-rules.md#two-scopes-topic-rules-and-global-rules). Four RED rules
belonged to no topic and could never have fired; a patient volunteering chest pain during the
satisfaction survey would have been missed entirely.

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
