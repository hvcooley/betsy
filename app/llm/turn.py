"""The LLM boundary: one patient message in, one structured turn out.

This module defines the *seam*, not an implementation. `docs/architecture.md` makes
the whole turn pipeline one LLM call — extract structured fields and draft a reply
together — because the safety gate downstream can always throw the draft away, so
splitting it into two calls would double the latency and buy nothing.

Expressing that call as a `TurnEngine` interface rather than a function is what lets
the entire deterministic pipeline run and be tested with no API key: `app/llm/fake.py`
supplies deterministic stand-ins, and `AnthropicTurnEngine` will land here later
implementing the same three-line protocol. Nothing downstream of this file knows which
one it is holding.

The engine produces **observations only**. It never decides which topic is active, it
never decides whether to escalate, and its `draft_reply` is a proposal that
`app/conversation/pipeline.py` is free to discard.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.domain.schemas import TurnExtraction

if typing.TYPE_CHECKING:
    from app.conversation.session import Message
    from app.protocol.engine import CaseFacts, ProtocolState
    from app.protocol.loader import Protocol, Topic


@dataclass(frozen=True)
class TurnRequest:
    """Step 1 of the pipeline: everything the call gets to see.

    A frozen dataclass rather than a Pydantic model because it carries live objects
    (the loaded protocol, the rule-free engine state) rather than a wire format, and
    because assembling it must never be able to mutate the conversation.
    """

    protocol: Protocol
    case: CaseFacts
    state: ProtocolState
    topic: Topic
    history: tuple[Message, ...]
    patient_message: str
    turn_index: int

    @property
    def provenance(self) -> dict[str, object]:
        """The fields every `TurnExtraction` must carry, whoever built it.

        Provenance is the caller's to state, not the model's: an extraction that
        labelled itself with the wrong topic would let one topic's answers be merged
        into another's slots.
        """
        return {
            "protocol_version": self.protocol.version_tag,
            "prompt_version": self.protocol.prompt_version,
            "topic_id": self.topic.id,
            "turn_index": self.turn_index,
            "raw_message": self.patient_message,
        }


class TurnDraft(BaseModel):
    """What one turn's call returned, successful or not.

    Field names mirror the `turn_analysis` columns in `docs/data-model.md`, because
    this object is what gets persisted on every turn — including the failures. A
    `TurnDraft` with `extraction=None` and `hard_failure=True` is the terminal state
    of the retry ladder in `docs/architecture.md` step 2, and it is still a row.
    """

    extraction: TurnExtraction | None = Field(
        default=None, description="None when the response never validated. Still persisted."
    )
    draft_reply: str = Field(
        default="",
        description=(
            "Proposed patient-facing text. A RED finding discards this, so nothing "
            "here may be shown to a patient before the safety gate has run."
        ),
    )
    hard_failure: bool = Field(
        default=False,
        description="Retries exhausted. Tier 1 on its own — the record is untrustworthy.",
    )
    validation_retries: int = Field(default=0, ge=0)

    # --- Call metadata, for the audit trail --------------------------------
    model: str = "none"
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw_response: str | None = None


@typing.runtime_checkable
class TurnEngine(typing.Protocol):
    """The one call the pipeline makes into the model layer.

    Deliberately a single method with no state: the conversation lives in
    `ProtocolState` and the transcript, both of which arrive on the request, so an
    engine cannot accumulate a private view of the conversation that the stored
    record does not have.
    """

    def analyze(self, request: TurnRequest) -> TurnDraft:
        """Interpret one patient message and propose a reply."""
        ...
