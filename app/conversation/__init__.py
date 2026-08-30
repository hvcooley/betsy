"""The turn pipeline: the layer that joins protocol, LLM, safety and triage.

`session.py` holds one running conversation; `pipeline.py` processes one turn of it,
in the order `docs/architecture.md` specifies. Every module underneath is a pure
function of its inputs — this package is the only place the sequence lives.
"""
