# betsy

LLM-driven post-op patient check-in: scripted conversation state machine with deterministic safety/triage rules and a clinician review UI.

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
| `/static/chat.html` | Patient-side chat demo |
| `/static/review.html` | Clinician review queue demo |
| `/docs` | Auto-generated Swagger UI |

Note: `/` has no route defined and will return a 404 (`{"detail":"Not Found"}`) — use one of the URLs above.

## Tests

```bash
uv run pytest                                        # run all tests
uv run pytest tests/test_health.py::test_health       # run a single test
```

## Migrations

Alembic is configured against `app.db.models.Base.metadata`:

```bash
uv run alembic revision --autogenerate -m "message"
uv run alembic upgrade head
```
