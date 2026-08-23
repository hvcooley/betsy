# Build sequence

Sized in focused working sessions (~4h), not calendar weeks. ~14 sessions total; realistically
6–10 weeks of nights and weekends.

| Phase | Deliverable | Sessions | Done when | Status |
| --- | --- | --- | --- | --- |
| 0. Skeleton | Repo, FastAPI, SQLAlchemy models, Alembic, `/health`, Anthropic client wrapper with logging | 1 | `POST /v1/cases` round-trips to DB | In progress — app, Alembic, `/health`, domain schemas and a bare client exist; models and the cases route do not |
| 1. Single-topic loop | Pain topic hardcoded. One LLM call → extraction + reply. Transcript persists. | 2 | You can have a 6-turn pain conversation in curl | Not started |
| 2. Protocol engine | YAML loader, state machine, all topics, conditional branching on anesthesia type | 2 | Full script runs end to end for a GA case and a block case | Not started |
| 3. Safety layer | Rule engine, YAML rule definitions, escalation templates, safety gate, tiering | 2 | PDPH scenario triggers, discards the draft, emits the template, marks Tier 1 | Not started |
| 4. Summary | Summary generator, one-liner, structured block | 1 | Every closed conversation has a readable header | Not started |
| 5. Eval harness | Patient sim, runner, 30 scenarios, report | 3 | `python -m evals.runner` prints a pass/fail table | Not started |
| 6. Thin UI | `chat.html` + `review.html`, review endpoint | 2 | You can demo to an anesthesiologist without a terminal | Not started |
| 7. Hardening | Dockerfile, README, seed script, prompt version pinning | 1 | Someone else can run it | Not started |

## Checkpoints

- **After Phase 3** you have something worth showing a clinician.
- **After Phase 6** you have something worth showing anyone.
- **Get clinical review of the YAML rule file after Phase 3.** Do not build Phases 4–6 on top of
  an unvalidated rule set.

## Open questions for the clinical SME

Answer these before Phase 3 — they are the inputs to the rule file, and only an anesthesiologist
can settle them. As each is answered, move the answer into
[safety-rules.md](safety-rules.md) or [protocol.md](protocol.md) and strike it here.

1. Confirm the RED rule list — anything missing, anything that should be YELLOW instead?
2. For each RED rule, what is the correct routing (911 / ED / surgeon / anesthesia on-call)?
3. Exact escalation script wording. These are the highest-liability sentences in the product and
   should be written by a clinician, not by the developer or the model.
4. Do the block-duration flag thresholds match practice, and how are adjuvants (dexamethasone,
   buprenorphine, clonidine) handled? The `block_adjuvants` section of the rules file is
   deliberately empty until this is answered. Related gaps in the same table: epidural and CSE
   have no regression window at all, and the spinal window assumes bupivacaine — what should
   lidocaine, ropivacaine and chloroprocaine use?
5. At what post-op hour should the call happen for the MVP's default scenario? (Everything points
   to POD1.)
6. What false-positive rate makes the tool annoying rather than useful? This sets the precision
   target once recall is locked at 100%.
7. Should BETSY ever tell a patient to take an already-prescribed medication, or is even that too
   close to prescribing?
