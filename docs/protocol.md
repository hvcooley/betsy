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
| `applicable_when` | `always`, or an expression over case fields; decides whether the topic is in this conversation at all |
| `goal` | Plain-English intent, passed to the LLM as context for phrasing |
| `required_slots` | Topic is not satisfied until all are filled |
| `optional_slots` | Filled if the patient volunteers or a follow-up is cheap |
| `rules` | Safety rule IDs evaluated while this topic is active (see [safety-rules.md](safety-rules.md)) |
| `max_turns` | Hard cap; exiting on this rather than slot satisfaction is itself a Tier 2 signal |
| `on_fail` | What to do when the topic cannot be satisfied (e.g. `terminate_politely`) |

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

### 11. `satisfaction` — always, max 2 turns
Capture experience feedback. **More important than it first appears** — this is the QA/QC and
quality-metrics channel, and the question set should be configurable per site.
**Required:** `satisfaction_response`, `anesthesia_options_explained`, `anesthesia_risks_explained`

### 12. `close` — always, max 3 turns
Deliver safety-netting instructions and confirm the patient can repeat them back.
**Required:** `understanding_confirmed`

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

## Open items

- Topic 9 (`local_only_recovery`) originally repeated the airway/dental/ocular rules from topic 8.
  Since topic 8 now runs for everyone, topic 9 keeps only `PERSISTENT_LOCAL`. Confirm with the SME
  that nothing else is local-specific.
- The satisfaction question set needs its configurable shape defined (per-site YAML block vs.
  fixed slots).
