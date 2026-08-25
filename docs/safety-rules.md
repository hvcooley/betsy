# Safety rules

This is where the product's actual value lives. The LLM is a commodity; this rule set is not.
Every rule is deterministic, unit-tested, evaluated against validated structured fields (never
free text), and reviewed by the clinical SME.

The rule set below is **pending clinical sign-off** — do not build the summary, eval or UI phases
on top of an unvalidated rule file. See the SME questions in [roadmap.md](roadmap.md).

## Two scopes: topic rules and global rules

A topic's `rules` fire while that topic is active. That is right for questions the agent went
looking for — `PDPH_SUSPECTED` belongs to the neuraxial screen, and evaluating it against a turn
about nausea would only add false positives.

It is wrong for a patient who **volunteers** something catastrophic. Read strictly, a patient who
mentions chest pain while answering the satisfaction survey triggers nothing, because `cardioresp`
went by four topics ago and is never coming back. Four RED rules in the table below —
`ANAPHYLAXIS_LATE`, `MH_SUSPECTED`, `SURGICAL_BLEEDING`, `SUICIDAL_IDEATION` — also belong to no
topic at all, so under topic scoping alone they could never fire.

So the rules file carries a **`global_rules` list, evaluated on every turn regardless of topic**,
with the topic's own rules layered on top:

```yaml
global_rules: [RESP_DISTRESS, CHEST_PAIN, ANAPHYLAXIS_LATE, MH_SUSPECTED,
               SURGICAL_BLEEDING, SUICIDAL_IDEATION, OVERSEDATION]
```

Membership is justified by catastrophe, not convenience: the test suite requires every global rule
to be RED. Everything else stays topic-scoped, so scoping still does real work.

## Rule shape

Each rule declares its `band` (`red`/`yellow`/`green`), `severity`, `tier`, `routes`, a
clinician-facing `label`, a `template_key`, and a `when` condition. The band implies the tier
(red → 1, yellow → 2, green → 3) and the loader rejects a rule where the two disagree, so a RED
rule silently carrying Tier 2 is impossible. A RED rule with no template is likewise a load error:
a red flag discards the drafted reply, so there must be fixed copy to send instead.

Conditions are a closed vocabulary of predicates combined with `all`/`any`/`not`:

| Predicate | Reads |
| --- | --- |
| `{symptom: chest_pain, presence: present}` (+ `min_severity`, `onset_hours_within`) | the extraction's symptom list |
| `{pain: {score_gte: 8, controlled_by_medication: false}}` | the 0–10 pain report |
| `{medication: {name_any: [...], adherence: ..., last_dose_hours_lt: ...}}` | reported medications |
| `{temperature_f_gte: 100.4}` | self-reported temperature |
| `{slot: tolerating_fluids, is_false: true}` (+ `equals`, `in`, `gte`, `lte`, `is_true`) | protocol answers, from this turn or earlier |
| `{case: {anesthesia_type_in: [...], block_type_in: [...], has_block: true, hours_post_op_gte: 24}}` | case facts |
| `{block_regression_exceeded: true}` | the block window table below |

Predicates are declared fields rather than expressions, so a rule naming a symptom code or a
severity that does not exist fails at load rather than never matching.

### Rules fail closed

The three rules that make silence safe, each with its own test:

1. **`presence: absent` matches only an explicit denial.** A symptom nobody asked about is
   UNKNOWN, and UNKNOWN satisfies neither `present` nor `absent`. "The patient did not mention
   chest pain" is not "the patient denies chest pain".
2. **`not` over a missing value is FALSE, not true.** Otherwise a negated condition would be
   satisfied by an empty extraction, and a rule reading "no shortness of breath" would be true of
   every patient who was never asked.
3. **An unanswered slot reads as `None`, never as false.** A rule testing `is_false` cannot fire
   because the question went unasked.

There is a fourth, subtler one: a symptom that is **present but ungraded** does not meet a
`min_severity` floor. The finding it would raise would cite a severity the extraction never
established, so the rule declines to fire and the grading question gets asked instead.

## RED — stop the conversation, escalate immediately

The drafted LLM reply is discarded and replaced with fixed clinician-authored copy; the
conversation is marked escalated and the protocol stops.

| Rule ID | Trigger | Route |
| --- | --- | --- |
| `RESP_DISTRESS` | Shortness of breath, stridor, difficulty swallowing/drooling | CALL_911 |
| `CHEST_PAIN` | Chest pain or pressure; palpitations with syncope | CALL_911 |
| `LAST_SYMPTOMS` | Perioral numbness, metallic taste, tinnitus, dizziness in a patient with a block or catheter | CALL_911 |
| `NEURAXIAL_HEMATOMA` | New/progressive leg weakness, saddle numbness, bowel or bladder incontinence, or severe new back pain after neuraxial | ED_NOW — time-critical surgical window |
| `COMPARTMENT_SYNDROME` | Pain out of proportion, worse on passive stretch, tightness in a casted/splinted limb | ED_NOW |
| `PDPH_SUSPECTED` | Postural headache (worse upright / better supine) after neuraxial, ± visual changes, tinnitus, neck stiffness | ED_NOW or CALL_ANESTHESIA per severity |
| `PONV_INTRACTABLE` | Vomiting with inability to tolerate any PO fluids, or no urine output >12h | ED_NOW |
| `OVERSEDATION` | Caregiver reports difficulty rousing the patient, especially with opioid + OSA history | CALL_911 |
| `ANAPHYLAXIS_LATE` | Spreading hives, facial/tongue swelling, breathing change | CALL_911 |
| `MH_SUSPECTED` | Fever with muscle rigidity / dark urine within 24h of triggering agents | CALL_911 |
| `DVT_SUSPECTED` | Unilateral calf pain with swelling/warmth | ED_NOW |
| `SURGICAL_BLEEDING` | Soaking through a dressing, expanding hematoma | CALL_SURGEON + ED_NOW |
| `SUICIDAL_IDEATION` | Any expression of self-harm | Human handoff, crisis resources |

The Route column holds `Route` members, and a rule may carry more than one — `SURGICAL_BLEEDING`
is `CALL_SURGEON` *and* `ED_NOW`, because who owns the problem is a separate question from how fast
it has to be seen. A rule must never be reduced to whichever of its routes is most urgent; that
drops an owner. See the resolved `Route` divergence in [README.md](README.md).

### Rules the table did not fully specify

Four rules needed a decision the table above did not supply. Each is shipped with `sme_reviewed:
false` and an entry in the SME question list.

- **`PDPH_SUSPECTED` is two rules.** "ED_NOW or CALL_ANESTHESIA per severity" cannot be expressed
  in one row without letting something other than the rule engine decide urgency. An isolated
  postural headache is `PDPH_SUSPECTED` → CALL_ANESTHESIA; the same headache with visual changes,
  neck stiffness or fever is `PDPH_WITH_RED_FLAGS` → ED_NOW, since that combination's differential
  is no longer just a dural puncture.
- **`SYNCOPE`** is referenced by the `cardioresp` topic but appears in neither the RED nor the
  YELLOW list — the RED table only covers *palpitations with syncope*, under `CHEST_PAIN`. Shipped
  standalone RED / ED_NOW on the recall-over-precision principle, pending an SME decision on band
  and route.
- **`SUICIDAL_IDEATION`** is specified as "human handoff, crisis resources", which names no route
  and there is no handoff route in the vocabulary. Routed CALL_911 as the fail-closed choice; the
  template leads with the 988 crisis line, which is the part that actually matters.
- **Three YELLOW rules belonged to no topic**: `REBOUND_PAIN` went to `block_regression` (which
  already had the matching slot), `MED_NONADHERENCE` and `OPIOID_SEDATIVE_COMBO` to `pain`.

## YELLOW — record a finding, continue the conversation, Tier 2

`PAIN_SEVERE_UNRESPONSIVE` (≥8/10 despite meds) · `PAIN_ATYPICAL_SITE` · `BLOCK_PROLONGED`
(sensory/motor deficit beyond the expected window for the block type) · `BLOCK_NEW_DEFICIT`
(weakness after the block had resolved) · `CATHETER_SITE_INFECTION` (erythema, drainage, leaking) ·
`PHRENIC_DYSPNEA` (mild dyspnea after interscalene — usually expected, still flag) ·
`REBOUND_PAIN` · `URINARY_RETENTION` · `APAP_STACKING` (taking Tylenol alongside Percocet/Norco —
genuinely common and genuinely dangerous) · `MED_NONADHERENCE` · `OPIOID_SEDATIVE_COMBO` ·
`POSTOP_DELIRIUM` · `DENTAL_INJURY` · `AIRWAY_INJURY` · `CORNEAL_ABRASION` · `DEHYDRATION_RISK` ·
`PERSISTENT_LOCAL`

## GREEN — reassure with scripted language, no flag

Expected sore throat · Hoarseness post-GA · Horner's syndrome after interscalene (ptosis, miosis —
benign, and patients find it terrifying) · Tingling during normal block regression · Grogginess on
day 0 · Mild queasiness that is resolving

A green rule matches but produces **no `Finding`**, so it can never reach the review queue. What it
produces is approved wording, offered to the model for the drafted reply rather than sent verbatim,
since a green result does not stop the conversation and the reply still has to fit the flow around
it. Nothing here may be used once a rule has fired — see conversational constraint 3.

Note that the same report can be green or yellow depending on the case: mild confusion is
`EXPECTED_DAY0_GROGGINESS` at eight hours post-op and `POSTOP_DELIRIUM` at forty. Time since surgery
is what separates them, which is why `hours_post_op` is a case predicate.

**Gap: Horner's syndrome has no rule.** It needs `ptosis` and `miosis` symptom codes, which the
`SymptomCode` vocabulary does not have. Left unwritten rather than approximated — a patient who
describes a drooping eyelid after an interscalene block currently gets no scripted reassurance,
which is a missed opportunity rather than a safety gap, but it is the one item in this list that
does not exist in the artifact.

## Block regression expectations

Drives `BLOCK_PROLONGED`. Held as **data** in the safety rules YAML under `block_regression`, keyed
by `block_type` — the deferred "auto-scheduled calls timed to block regression" feature reads off
this same table, so keeping it as data now makes that feature a scheduler rather than a rewrite.

| Block | Typical duration | Flag if unresolved beyond |
| --- | --- | --- |
| Interscalene | 12–18h | 24h |
| Supraclavicular / infraclavicular | 12–24h | 30h |
| Adductor canal | 12–24h | 30h |
| Popliteal sciatic | 12–24h (longer with adjuvants) | 36h |
| Spinal (bupivacaine) | 2–6h | 8h |
| TAP / field blocks | 8–16h | 24h |

The keys are a closed vocabulary (the `BlockType` enum), and a test requires every member to have
a window — a block type without one would let a prolonged block pass unflagged.

Two gaps pending SME input, both left empty rather than guessed:

- **Adjuvants** (dexamethasone, buprenorphine, clonidine) extend these windows by an unknown
  amount. The `block_adjuvants` section exists but is empty; until it is filled, an adjuvant case
  will flag early.
- **Epidural and CSE have no row**, though `neuraxial_screen` applies to them. Only the spinal
  window is specified, and it assumes bupivacaine.

## Non-negotiable conversational constraints

System-prompt content, and each one gets a test.

1. **Never diagnose.** Describe patterns, escalate to humans.
2. **Never start, stop, or change the dose of any medication.** The single permitted exception,
   phrased as encouragement rather than prescription: taking an already-prescribed antiemetic the
   patient is holding back on. (Whether even this is acceptable is an open SME question.)
3. **Never estimate risk or prognosis**, and never reassure that something is "nothing to worry
   about" once a rule has fired.
4. **On any red flag, deliver the fixed template and end.** No improvisation, no continued
   questioning.
5. **Disclose AI status in the opening message, every time.**
6. **Detect proxies** ("this is his wife") and record it — answers become second-hand and
   reliability drops. A proxy conversation with any yellow finding is Tier 1.
7. **Plain language, ~6th-grade reading level, one question at a time.**
8. **If the patient asks something the protocol cannot answer**, say so plainly and log it as an
   unanswered question — which by itself bumps the conversation to Tier 2.

## Escalation copy

Fixed, clinician-authored templates keyed by rule/route. These are the highest-liability sentences
in the product and must be written by a clinician, not by the developer and not by the model. Copy
is never LLM-generated, and the exact text shown is persisted on the escalation row for audit.

What ships now is **scaffolding, not signed-off copy**: it says the right kind of thing in the
right register — never diagnosing, never naming a probable cause, never estimating risk, never
changing a medication, never reassuring once a rule has fired — and every template carries
`sme_reviewed=False`. A rule with no template of its own falls back to route-level copy, which is
deliberately vaguer because it cannot name what triggered it; the test suite requires every RED
rule to resolve a template of its own, so that fallback is a safety net rather than a routine path.

## Review status

Every rule and every template carries `sme_reviewed: false`, and a test asserts that none of them
has been flipped. That is a guard rather than an aspiration: sign-off becomes a deliberate,
reviewable commit that has to change a test, instead of something someone remembers having done.
The same flag is what a release gate reads.
