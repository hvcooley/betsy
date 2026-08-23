# Scope

Text-only, post-operative anesthesia follow-up conversation agent. Backend + LLM stack. Synthetic
patient data only. Optimized for the shortest path to a working, demoable, clinically defensible
product.

## In scope

| Capability | Notes |
| --- | --- |
| Text-based post-op follow-up conversation | Patient types, BETSY responds |
| Deterministic conversation protocol | Anesthesia-specific script, versioned, data-driven |
| Structured extraction per turn | Pain scores, symptoms, med adherence, red flags |
| Deterministic red-flag rule engine | Escalation decisions are code, not LLM judgment |
| Templated escalation messages | Zero-hallucination on the highest-liability output |
| Stored transcript + structured findings | Full audit trail |
| One-line summary header + triage tier | Feeds the future review dashboard |
| Offline eval harness | Simulated patients, regression assertions, red-flag recall metrics |
| Minimal review API + thin demo UI | Two static pages, no build step |

## Out of scope

Voice/telephony · OMP or any EHR integration · RAG or vector search · Real PHI · HIPAA
infrastructure work · Authentication beyond a shared token · Multi-tenancy · Scheduled/automated
call triggering · Pre-op workflows · Streaming responses · Agent frameworks
(LangChain/LlamaIndex/CrewAI) · Docker/K8s until the very end · Notifications

Anything not in the first table is out of scope unless the user says otherwise. See
[architecture.md](architecture.md) for the seams that make each deferred item additive later.

## Hard rules

- **No real patient data during MVP.** Every case is synthetic. This removes 4–8 weeks of
  HIPAA/BAA/infrastructure work from the critical path and lets us validate the only thing that
  matters now: does the conversation logic work, and does it catch the things that hurt someone?
- **No streaming to the patient.** The safety gate can discard a drafted reply after it is
  generated, so tokens cannot be emitted as they are produced. Non-streaming request/response is
  required by the architecture, not a shortcut. Budget ~2–4s per turn.
- **The LLM never owns control flow or the escalation decision.** See
  [architecture.md](architecture.md).

## Risks being carried deliberately

| Risk | Mitigation in this design |
| --- | --- |
| "Mostly right" AI erodes clinician trust — 90–97% correct is worse than useless when rare misses matter and humans re-review everything anyway | Escalation is deterministic, not probabilistic. The LLM's judgment is never the last word on safety. |
| Summaries too verbose/inconsistent to trust — the #1 adoption killer | One-liner is length-capped; the structured block is rendered from DB rows, not generated; an eval assertion checks for claims not backed by a finding row |
| Human review destroys the labor savings | Deterministic tiering means Tier 3 cases genuinely can be approved from a header. That is the entire ROI argument. |
| Rare misses matter disproportionately | 100% red-flag recall on the golden set is a release gate, not a goal. Precision is explicitly sacrificed. |
| Liability ambiguity | Every conversation is reviewed; BETSY never diagnoses or prescribes; escalation copy is clinician-authored; full audit trail via the turn-analysis table. Positioning stays "intake augmentation". |
| Compliance/integration burden kills momentum | Synthetic data only. HIPAA and EHR integration stay off the critical path until the conversation logic is proven. |
