# CLAUDE.md

Guidance for coding agents working in this repository. (`AGENTS.md` is a symlink to this file, so
there is one copy to maintain.)

## What this is

Betsy: an LLM-driven post-op patient check-in system. A patient has a scripted chat conversation
driven by a deterministic protocol (state machine), with deterministic safety/triage rules layered
on top of LLM extraction, and a clinician-facing review UI for surfacing findings.

The project is an early-stage skeleton — most modules currently contain only `TODO` docstrings and
empty class/function bodies. When implementing a module, check its docstring for the intended
purpose, then check the governing doc in `docs/` before designing from scratch.

## Commands

```bash
uv sync                                # install deps
uv run uvicorn app.main:app --reload   # run dev server (http://127.0.0.1:8000)
uv run pytest                          # run all tests
```

See README.md for full setup instructions and migration commands.

## Design docs — read the relevant one before implementing

`docs/` holds the condensed MVP specification. Start at `docs/README.md` for the index and the
list of known spec-vs-code divergences.

| Working on | Read first |
| --- | --- |
| Anything | `docs/architecture.md` (the LLM/deterministic split, turn pipeline), `docs/scope.md` |
| `app/protocol/` | `docs/protocol.md` — topics, slots, branching |
| `app/safety/` | `docs/safety-rules.md` — RED/YELLOW/GREEN rules, block durations, conversational constraints |
| `app/triage/`, `app/summary/` | `docs/triage-and-summary.md` |
| `app/db/`, `app/main.py` | `docs/data-model.md` — tables and API surface |
| `evals/` | `docs/evals.md` |
| Sequencing, SME questions | `docs/roadmap.md` |

Those docs deliberately contain **no file paths** — the directory map below is the only place
layout is recorded, so restructuring the repo means editing this file and nothing else.

## Non-negotiable invariants

Violating any of these is a bug regardless of what the task asked for:

1. **The LLM never owns control flow or the escalation decision.** The protocol engine decides
   which topic is active; the rule engine decides escalation, evaluated against validated
   structured fields, never free text.
2. **A RED rule discards the LLM's drafted reply** and emits fixed clinician-authored copy.
   Consequently, patient-facing replies are **never streamed**.
3. **Tier is computed deterministically from findings**, never by the model.
4. **Turn analysis is persisted on every turn, including failures.** It is the audit trail and the
   eval substrate.
5. **Synthetic data only.** No real PHI enters this system during the MVP.
6. Full scope boundaries live in `docs/scope.md`; anything outside it needs the user to say so.

## Architecture

Strict separation between **LLM-driven conversation** and **deterministic clinical logic**, so
safety-critical decisions never depend solely on model output:

- `app/protocol/` — the check-in *script*. `definitions/*.yaml` (e.g. `postop_v1.yaml`) is a
  clinician-editable protocol defining conversation topics/questions; `loader.py` parses and
  validates it; `engine.py` is the state machine deciding which topic is active and when to
  advance. **`engine.py` names no topic and no slot** — it walks a queue built from the YAML, so
  adding a topic is a YAML edit and nothing else. A test asserts the absence of those literals.
- `app/llm/` — the LLM boundary. `client.py` wraps the Anthropic SDK; `turn.py` takes one patient
  message + protocol state and produces a `TurnExtraction` (structured data pulled from the
  message) plus a draft reply; `prompts/*.md` are versioned prompt templates (`system_v1.md`,
  `turn_v1.md`, `summary_v1.md`) matched to protocol/rules versions.
- `app/safety/` — deterministic, clinician-reviewable red-flag rules, independent of the LLM.
  `rules/postop_v1.yaml` defines rules plus the always-on `global_rules` list; `rules.py` evaluates
  them against extracted data; `templates.py` holds fixed escalation copy (not LLM-generated, for
  predictability). Every rule and template carries `sme_reviewed: false` until a clinician signs it
  off, and a test asserts none has been flipped.
- `app/triage/tiering.py` — deterministic Tier 1/2/3 assignment from safety rule outcomes.
- `app/summary/generator.py` — builds the clinician-facing summary of a completed check-in.
- `app/domain/` — shared types: `enums.py` (`AnesthesiaType`, `BlockType`, `Severity`, `Route`,
  `RouteOwner`, `Tier`) and
  `schemas.py` (Pydantic models: `TurnExtraction`, `Finding`, `Summary`) used across the protocol,
  LLM, safety, and summary layers.
- `app/db/` — SQLAlchemy models (`models.py`, currently just the `Base`) and session management
  (`session.py`, sqlite by default via `settings.database_url`).
- `app/main.py` — FastAPI app; mounts `app/static/` for the chat and review HTML demos; routes for
  protocol/turn/safety are not yet wired in (see TODO in the file).
- `evals/` — offline evaluation harness: `patient_sim.py` (LLM-simulated patient), `runner.py`
  (runs scenarios from `evals/scenarios/` against the protocol engine), `report.py` (reports
  results). Not yet implemented.
- `docs/` — condensed MVP specification (see the table above).

### Versioning convention

Protocol definitions, safety rules, and prompts are versioned in lockstep by filename suffix
(`_v1`, etc.) — `postop_v1.yaml` protocol pairs with `postop_v1.yaml` rules and the `_v1` prompt
set. When introducing a new version, add new files rather than mutating existing ones, since
clinician review/sign-off is tied to specific versions.

## Config

Settings (`app/config.py`) load from `.env` via `pydantic-settings`: `env`, `anthropic_api_key`,
`database_url` (defaults to `sqlite:///./betsy.db`).
