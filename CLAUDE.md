# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Betsy: an LLM-driven post-op patient check-in system. A patient has a scripted chat conversation
driven by a deterministic protocol (state machine), with deterministic safety/triage rules layered
on top of LLM extraction, and a clinician-facing review UI for surfacing findings.

The project is an early-stage skeleton — most modules currently contain only `TODO` docstrings and
empty class/function bodies. When implementing a module, check its docstring for the intended
purpose before designing from scratch.

## Commands

```bash
cp .env.example .env          # fill in ANTHROPIC_API_KEY
uv sync                       # install deps
uv run uvicorn app.main:app --reload   # run dev server (http://127.0.0.1:8000)
uv run pytest                 # run all tests
uv run pytest tests/test_health.py::test_health   # run a single test
```

Migrations (Alembic, configured against `app.db.models.Base.metadata`):
```bash
uv run alembic revision --autogenerate -m "message"
uv run alembic upgrade head
```

Core URLs once running:
| URL | Purpose |
| --- | --- |
| `/health` | Health check |
| `/static/chat.html` | Patient-side chat demo |
| `/static/review.html` | Clinician review queue demo |
| `/docs` | Swagger UI |

Note: `/` has no route and returns 404 — use one of the URLs above.

## Architecture

The system is organized around a strict separation between **LLM-driven conversation** and
**deterministic clinical logic**, so that safety-critical decisions (triage, red-flag detection)
never depend solely on model output:

- `app/protocol/` — the check-in *script*. `definitions/*.yaml` (e.g. `postop_v1.yaml`) is a
  clinician-editable protocol defining conversation topics/questions; `loader.py` parses it;
  `engine.py` is the state machine deciding which topic is active and when to advance.
- `app/llm/` — the LLM boundary. `client.py` wraps the Anthropic SDK; `turn.py` takes one patient
  message + protocol state and produces a `TurnExtraction` (structured data pulled from the
  message) plus a draft reply; `prompts/*.md` are versioned prompt templates (`system_v1.md`,
  `turn_v1.md`, `summary_v1.md`) matched to protocol/rules versions.
- `app/safety/` — deterministic, clinician-reviewable red-flag rules, independent of the LLM.
  `rules/postop_v1.yaml` defines rules; `rules.py` evaluates them against extracted data;
  `templates.py` holds fixed escalation copy (not LLM-generated, for predictability).
- `app/triage/tiering.py` — deterministic Tier 1/2/3 assignment from safety rule outcomes.
- `app/summary/generator.py` — builds the clinician-facing summary of a completed check-in.
- `app/domain/` — shared types: `enums.py` (`AnesthesiaType`, `Severity`, `Route`, `Tier`) and
  `schemas.py` (Pydantic models: `TurnExtraction`, `Finding`, `Summary`) used across the protocol,
  LLM, safety, and summary layers.
- `app/db/` — SQLAlchemy models (`models.py`, currently just the `Base`) and session management
  (`session.py`, sqlite by default via `settings.database_url`).
- `app/main.py` — FastAPI app; mounts `app/static/` for the chat and review HTML demos; routes for
  protocol/turn/safety are not yet wired in (see TODO in the file).
- `evals/` — offline evaluation harness: `patient_sim.py` (LLM-simulated patient), `runner.py`
  (runs scenarios from `evals/scenarios/` against the protocol engine), `report.py` (reports
  results). Not yet implemented.

### Versioning convention

Protocol definitions, safety rules, and prompts are versioned in lockstep by filename suffix
(`_v1`, etc.) — `postop_v1.yaml` protocol pairs with `postop_v1.yaml` rules and the `_v1` prompt
set. When introducing a new version, add new files rather than mutating existing ones, since
clinician review/sign-off is tied to specific versions.

## Config

Settings (`app/config.py`) load from `.env` via `pydantic-settings`: `env`, `anthropic_api_key`,
`database_url` (defaults to `sqlite:///./betsy.db`).
