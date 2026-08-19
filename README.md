# betsy

LLM-driven post-op patient check-in: scripted conversation state machine with deterministic safety/triage rules and a clinician review UI.

## Setup

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY
uv sync
uv run uvicorn app.main:app --reload
```

Demo pages: `/static/chat.html` (patient), `/static/review.html` (clinician).

## Tests

```bash
uv run pytest
```
