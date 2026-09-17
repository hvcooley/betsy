# betsy

LLM-driven post-op patient check-in: scripted conversation state machine with deterministic safety/triage rules and a clinician review UI.

## Status

Early-stage MVP, synthetic data only. The deterministic core and the real-model turn engine run
end to end from the CLI; there is no persistence, HTTP API or UI yet. Phase-by-phase detail is in
[`docs/roadmap.md`](docs/roadmap.md).

**Built**

- Conversation protocol: a YAML-defined script (12 topics, 43 slots) walked by a state machine
  that decides which topic is active and when to advance. The model never chooses the topic.
- Safety rule engine: 37 YAML rules (15 red, 17 yellow, 5 green) evaluated against validated
  structured fields, never free text. A red rule discards the model's drafted reply and sends
  fixed escalation copy instead.
- Deterministic Tier 1/2/3 triage, with a one-sentence reason for every tier.
- Turn pipeline: extraction → rules → safety gate → protocol advance → reply, with a turn record
  kept on every turn, including failures.
- Anthropic turn engine: one non-streaming structured-output call per turn, a ≤2-retry
  validation ladder, and a recorded hard failure (never a silent fallback) when retries run out.
- Scripted scenario replay: 6 end-to-end check-ins asserting tier, routes, terminal status and
  which rules must and must not fire.
- Tests: `uv run pytest` — 308 passing, 4 intentionally skipped; no API key needed.

**Partial**

- Summary: tier, routes, findings and a template headline are rendered deterministically; the
  LLM-written one-liner and narrative are not started.
- Eval harness: scripted replay works. The LLM-simulated patient, runner, report and red-flag
  recall metrics are still stubs (`evals/patient_sim.py`, `evals/runner.py`, `evals/report.py`).
- Storage: conversations, turn records and escalations live in memory, shaped like the planned
  tables. No database tables or migrations exist yet.

**Planned**

- Database persistence and the HTTP API (only `/health` exists today).
- Patient chat and clinician review demo pages (`app/static/*.html` are placeholders).
- A larger scenario set (~30), and recorded latency and token-cost figures from the real model.
- Dockerfile and hosting.

**Not clinically reviewed.** Every rule and escalation template is a developer draft marked
`sme_reviewed: false`, pending sign-off by an anesthesiologist.

## Try it

```bash
uv sync
uv run python -m app.cli --all                    # replay every scenario, pass/fail table
uv run python -m app.cli --scenario pdph_spinal   # one check-in, layer by layer
uv run python -m app.cli --case spinal            # type patient answers yourself (no API key)
uv run python -m app.cli --case spinal --live     # same, against the real model (needs a key)
```

## Design docs

The condensed MVP specification lives in [`docs/`](docs/README.md) — scope, architecture, data
model, the conversation protocol, the safety rule set, triage/summary, evals, and the build
roadmap. `docs/README.md` is the index and also tracks where the spec and the current code
disagree.

## Setup

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY
uv sync
uv run uvicorn app.main:app --reload
```

By default the app runs at `http://127.0.0.1:8000`. Core URLs:

| URL | Purpose |
| --- | --- |
| `/health` | Health check — returns `{"status": "ok"}` |
| `/static/chat.html` | Patient-side chat demo — placeholder page, not built yet |
| `/static/review.html` | Clinician review queue demo — placeholder page, not built yet |
| `/docs` | Auto-generated Swagger UI |

Note: `/` has no route defined and will return a 404 (`{"detail":"Not Found"}`) — use one of the URLs above.

## Tests

```bash
uv run pytest                                        # run all tests
uv run pytest tests/test_health.py::test_health       # run a single test
```

## Migrations

Alembic is configured against `app.db.models.Base.metadata`, but no models or migrations exist
yet — these commands are for when persistence lands:

```bash
uv run alembic revision --autogenerate -m "message"
uv run alembic upgrade head
```
