# The conversation protocol

## Shape

Every one of the reference conversations follows the same arc:

> **open → closed-ended localize/quantify → red-flag check → clear plan → confirm understanding**

Encode that at the topic level and the whole agent becomes a loop over topics.

Each topic is **YAML, not Python**, so a clinical SME can read and edit it without touching code.
This is a velocity decision as much as a correctness one — every round-trip through the developer
as translator costs a week of calendar time.

## Topic fields

| Field | Meaning |
| --- | --- |
| `id` | Stable identifier; referenced by findings, evals and turn analysis |
| `applicable_when` | A predicate over case fields; decides whether the topic is in this conversation at all |
| `goal` | Plain-English intent, passed to the LLM as context for phrasing |
| `opening_question` | Clinician-authored seed the LLM paraphrases, and the literal fallback when an LLM call fails, so a topic can always be asked |
| `slots` | What the topic has to learn; each carries its own `required` flag |
| `question_set` | For survey topics: a named question set that expands into `slots` (see [Satisfaction](#the-satisfaction-question-set)) |
| `rules` | Safety rule IDs evaluated while this topic is active (see [safety-rules.md](safety-rules.md)). The `global_rules` there fire on every turn regardless, so a topic never needs to repeat them |
| `max_turns` | Hard cap; exiting on this rather than slot satisfaction is itself a Tier 2 signal |
| `on_fail` | `advance` (default) or `terminate_politely` |

### One `slots` list, not two

The topic tables below name required and optional slots separately, and that is how
this document reads. The artifact uses **one list with a `required` flag per slot**,
because a slot needs a type, bounds and a phrasing hint anyway: splitting the ids into
a second list lets a definition drift out of the required set, and there is nothing to
catch it. Each slot also carries:

- `type` — `bool`, `int`, `float`, `enum` (with `values`) or `text`, plus `min`/`max`
- `prompt_hint` — what the model is being asked to determine
- `maps_to` — an optional path into the turn's clinical fields (`pain.score`,
  `symptom.<code>.presence`, …). One direction only: when the model filled the
  clinical field but left the slot empty, the engine backfills the slot rather than
  re-asking a question it already has the answer to. When both are present and
  disagree, the conflict is recorded and extraction confidence drops, so the topic is
  not satisfied on a reading nobody can adjudicate.

A slot only counts as filled at or above the protocol's `slot_confidence_threshold`.
A topic whose extraction keeps falling short exits on `max_turns` instead, which is
already a triage signal — so low confidence degrades into "a human should look" rather
than into a confidently wrong answer.

### `applicable_when` is a predicate, not an expression

Three forms, covering every branch the script needs:

```yaml
applicable_when: {always: true}
applicable_when: {case_field: block_type, is_null: false}
applicable_when: {case_field: anesthesia_type, equals: local}
applicable_when: {case_field: anesthesia_type, in: [spinal, epidural, combined_spinal_epidural]}
```

There is no expression evaluator to sandbox, and field names and values are checked
against the real case fields and enums when the file loads. A typo becomes a startup
error rather than a topic that silently never applies — which is the failure mode that
matters, because a topic that never runs looks exactly like a healthy patient.

## Topics — `postop_followup` v1

In order. `app/protocol/definitions/postop_v1.yaml` is the authoritative artifact once populated;
this table is the specification it must satisfy.

### 1. `identity_consent` — always, max 3 turns, `on_fail: terminate_politely`
Confirm we are speaking with the patient or an authorized proxy, disclose AI status, set
expectations. Asking the patient to state their name and date of birth is sufficient.
**Required:** `identity_confirmed`, `is_proxy`, `consent_to_continue`

### 2. `open_checkin` — always, max 2 turns
Open-ended "how are you doing" so the patient's own concern surfaces first. Also confirm which
operation they had.
**Required:** `patient_reported_concerns`

### 3. `pain` — always, max 6 turns
Quantify pain, assess medication effectiveness, screen for atypical pain.
**Required:** `pain_score_now`, `pain_med_taken`, `pain_location_expected`
**Optional:** `pain_score_worst`, `hours_since_last_dose`, `med_effectiveness`
**Rules:** `PAIN_SEVERE_UNRESPONSIVE`, `PAIN_ATYPICAL_SITE`, `COMPARTMENT_SYNDROME`,
`DVT_SUSPECTED`, `APAP_STACKING`

### 4. `ponv` — always, max 5 turns
Distinguish queasiness from emesis; assess PO tolerance and hydration.
**Required:** `nausea_present`, `vomiting_present`, `tolerating_fluids`
**Optional:** `antiemetic_available`, `antiemetic_taken`, `last_urination`
**Rules:** `PONV_INTRACTABLE`, `DEHYDRATION_RISK`

### 5. `cardioresp` — always, max 4 turns
Screen for respiratory and cardiac complications.
**Required:** `breathing_difficulty`, `chest_pain`, `lightheaded_syncope`
**Rules:** `RESP_DISTRESS`, `CHEST_PAIN`, `SYNCOPE`, `OVERSEDATION`

### 6. `block_regression` — when `block_type is not null`, max 6 turns
Assess sensory/motor return against the expected window; catch prolonged deficit and LAST.
**Required:** `sensation_returning`, `motor_function`, `hours_since_block`
**Optional:** `tingling_present`, `catheter_site_status`, `rebound_pain`
**Rules:** `BLOCK_PROLONGED`, `BLOCK_NEW_DEFICIT`, `LAST_SYMPTOMS`, `CATHETER_SITE_INFECTION`,
`PHRENIC_DYSPNEA`

### 7. `neuraxial_screen` — when `anesthesia_type in ['spinal','epidural','combined_spinal_epidural']`, max 6 turns
Screen for PDPH and for epidural hematoma/abscess.
**Required:** `headache_present`, `leg_weakness`, `bladder_function`
**Optional:** `headache_positional`, `visual_changes`, `back_pain_new`, `fever`
**Rules:** `PDPH_SUSPECTED`, `NEURAXIAL_HEMATOMA`, `URINARY_RETENTION`

### 8. `anesthesia_recovery` — **always**, max 5 turns
Screen for airway/dental/ocular sequelae and prolonged cognitive effects.
**Required:** `sore_throat`, `grogginess_confusion`
**Optional:** `dental_injury`, `hoarseness`, `eye_irritation`, `swallowing_difficulty`,
`tongue_laceration`
**Rules:** `AIRWAY_INJURY`, `DENTAL_INJURY`, `POSTOP_DELIRIUM`, `CORNEAL_ABRASION`

> **Clinical decision (overrides the drafted `anesthesia_type in ['general','general_with_block']`
> condition):** ask everyone, regardless of anesthesia type. If any of these things happened it
> needs to be known. MAC does not involve an airway device and GA does, but the complications for
> both are the same — so MAC and GA fall in the same category here. The agent must **not** ask the
> patient what type of anesthesia they received, because the doctor may not have told them.

### 9. `local_only_recovery` — when `anesthesia_type == 'local'`, max 5 turns
Confirm the local anesthetic wore off — that is the main thing.
**Required:** `persistent_numbness`
**Rules:** `PERSISTENT_LOCAL`

### 10. `patient_questions` — always, max 5 turns
Answer patient questions about how the anesthesia wore off; defer anything clinical.
**Required:** `questions_answered_or_deferred`

### 11. `satisfaction` — always, max 4 turns
Capture experience feedback. **More important than it first appears** — this is the QA/QC and
quality-metrics channel, and the question set is configurable per site.
**Required:** `satisfaction_response`, `anesthesia_options_explained`, `anesthesia_risks_explained`
— supplied by the `site_default` question set, not hand-listed. See
[the satisfaction question set](#the-satisfaction-question-set).

> **Raised from the drafted 2 turns to 4.** Three required questions cannot be asked in two turns
> while honouring conversational constraint 7 (*one question at a time*). The alternative — a
> per-topic batching exemption — buys two turns by weakening a safety constraint everywhere it is
> quoted, so the turn budget moved instead.

### 12. `close` — always, max 3 turns
Deliver safety-netting instructions and confirm the patient can repeat them back.
**Required:** `understanding_confirmed`

## One topic closes per turn

A turn advances the queue by at most one step, and a topic's slots are only filled by messages
sent while that topic was active. A patient who answers ahead — "my pain's a 4 and no, I haven't
been sick at all" while the pain topic is open — closes the pain topic and nothing else; the PONV
answer is discarded for the purposes of protocol progress, and PONV is asked normally on a later
turn.

The alternative was letting one message satisfy several topics, which is what the state machine
originally allowed. It was rejected: a topic closed by a message that was never asked for it is
recorded as satisfied with no question behind it, and neither the transcript nor the turn analysis
shows which message was taken as its answer. For a record whose whole purpose is to be re-read by
a clinician, a question asked twice is much cheaper than an answer with no question.

**This discards protocol progress only, never clinical content.** The pain, symptom, medication and
temperature fields extracted from a message are evaluated by the safety rules on the turn they
arrive, whatever topic is active — a volunteered chest pain escalates immediately, which is the
whole point of the always-on `global_rules` list. Only the answer's effect on *which question comes
next* is dropped.

### Deferred: carry volunteered answers forward as something to raise

The visible cost is that a patient who volunteered an answer is asked the question again anyway,
as if they had said nothing. The intended fix is not to re-enable multi-topic advance, but to
**remember what was volunteered and raise it when its topic opens** — the model would be given
the earlier statement and its quote as context for that topic, and would open with an
acknowledgement and a request to confirm or expand ("you mentioned earlier that you'd been a bit
queasy — has that settled?") rather than a cold opening question.

That keeps the properties this rule exists to protect: the question is still asked, the topic is
still satisfied by a message sent while it was active, and the transcript still shows both. It is
a real feature rather than a tweak — it needs volunteered observations retained on the protocol
state and keyed by the topic they belong to, surfaced into the turn prompt, and enough eval
coverage to show that a re-raised answer is not being led. Not in the MVP.

## Branching and case fields

`applicable_when` reads two independent case fields:

- **`anesthesia_type`** — the *primary* technique: `general`, `spinal`, `epidural`,
  `combined_spinal_epidural`, `peripheral_nerve_block`, `mac_sedation`, `local`.
- **`block_type`** — which block has to wear off, or null when there is none. Independent of
  whether the block was the whole anesthetic or an adjunct to a general.

This replaces the spec's fused values: `general_with_block` is `general` + a `block_type`,
`regional_block` is `peripheral_nerve_block` + a `block_type`, `local_only` is `local`, and `cse`
is `combined_spinal_epidural`. A patient with a GA *and* a block correctly matches both
`anesthesia_recovery` (always) and `block_regression` (has a block). See "Resolved divergences" in
[README.md](README.md).

Note that `block_type` includes `spinal`, so a spinal patient gets both `neuraxial_screen` (PDPH,
hematoma) and `block_regression` (has sensation come back on schedule?) — which is the intent, not
an accident of the encoding.

## The satisfaction question set

*Resolves the open item on the configurable shape: a per-site question set block, not fixed slots.*

Satisfaction is the QA/QC and quality-metrics channel, and the questions a site wants to ask are
not the questions another site wants to ask. Fixed slots would make every such change a protocol
version bump — and therefore a re-review of the clinical topics, which have not changed. So the
survey is a **named, versioned block**, referenced by the topic:

```yaml
question_sets:
  site_default:
    version: 1
    label: Default post-op anesthesia experience survey
    questions:
      - {id: satisfaction_response, required: true, response_type: likert_5,
         text: "Overall, how satisfied were you with the anesthesia care you received?"}
      - {id: anesthesia_options_explained, required: true, response_type: yes_no, text: "…"}
      - {id: anesthesia_risks_explained, required: true, response_type: yes_no, text: "…"}
      - {id: satisfaction_comment, required: false, response_type: free_text, text: "…"}

topics:
  - id: satisfaction
    question_set: site_default      # in place of an inline `slots:` list
```

`response_type` is a closed vocabulary that expands into ordinary slots — `yes_no` → bool,
`likert_5` → int 1–5, `scale_0_10` → int 0–10, `free_text` → text. **The engine therefore has no
survey-specific code path**: it walks the satisfaction topic exactly as it walks the pain topic. A
site swaps the `question_set` reference and the whole survey changes with no code edit.

Three properties hold, and each has a test:

- **Survey answers are never clinical.** No safety rules attach to the topic, and no answer can
  move the triage tier. A maximally dissatisfied patient with no findings is still Tier 3 —
  dissatisfaction is a quality signal, not a clinical one, and conflating them would flood the
  review queue with cases that need no clinician.
- **A red flag volunteered mid-survey still escalates**, via the always-on `global_rules` in
  [safety-rules.md](safety-rules.md). This is precisely the case topic-scoped rules would miss,
  since every clinical topic has already gone by.
- **Escalation skips the survey.** A RED finding halts the protocol before topic 11, and a missing
  satisfaction block is not itself a finding.

Slots expanded from a question set are marked `survey: true`, so the summary and triage layers can
tell survey answers from clinical ones without hardcoding the topic's name.

## Open items

- Topic 9 (`local_only_recovery`) originally repeated the airway/dental/ocular rules from topic 8.
  Since topic 8 now runs for everyone, topic 9 keeps only `PERSISTENT_LOCAL`. Confirm with the SME
  that nothing else is local-specific.
- The `site_default` question set is a developer's draft of what a quality survey asks. The
  question *wording* needs the same review the escalation copy does, from whoever owns quality
  metrics at the site.
