# Safety rules

This is where the product's actual value lives. The LLM is a commodity; this rule set is not.
Every rule is deterministic, unit-tested, evaluated against validated structured fields (never
free text), and reviewed by the clinical SME.

The rule set below is **pending clinical sign-off** — do not build the summary, eval or UI phases
on top of an unvalidated rule file. See the SME questions in [roadmap.md](roadmap.md).

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
