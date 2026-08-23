# Triage and summary

## Tier assignment is deterministic

**Do not let the LLM assign the tier.** Tiering is the thing the anesthesiologist trusts to protect
their attention; it must be explainable in one sentence and reproducible from the findings alone.

**Tier 1 — Complex. Mandatory follow-up call, no dashboard approval path.**
- any RED escalation, **or**
- conversation abandoned with an unresolved symptom, **or**
- proxy-reported conversation with any yellow finding, **or**
- ≥2 topics with low-confidence extraction, **or**
- schema validation hard-failure

**Tier 2 — Medium. Read the transcript.**
- any YELLOW finding, **or**
- any unanswered patient question, **or**
- any topic exited on `max_turns` rather than slot satisfaction

**Tier 3 — Easy. Approve from the one-liner.**
- all applicable topics completed via slot satisfaction, **and**
- all findings green, **and**
- no unanswered questions

Tier 3 being genuinely approvable from a header is the entire ROI argument — see
[scope.md](scope.md). Note that the tier definitions above are broader than "how sick is the
patient": abandonment and extraction failure are Tier 1 because the *record* is unreliable, not
because the patient is unstable.

## Summary generation

One LLM call at conversation close, producing three parts:

1. **One-liner (≤140 chars).** e.g. *"POD1 shoulder scope, interscalene regressing on schedule,
   pain 4/10 controlled, no red flags."* For a Tier 3 case this is the only thing a doctor reads.
2. **Structured block.** Pain score, PONV status, block status, meds taken, findings list.
   **Rendered from the database, not from the LLM.**
3. **Narrative (3–5 sentences).** For Tier 1 and Tier 2 cases only.

### Anti-fabrication constraint

The summary prompt receives only the structured findings and the transcript, and every clinical
claim must be traceable to a finding row. An eval assertion checks for claims not backed by a
finding. Summary fabrication is the fastest way to lose a clinician's trust permanently, and
verbose/inconsistent summaries are the single most likely adoption killer.
