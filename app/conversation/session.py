"""One running conversation, held in memory.

These are the `conversation`, `message`, `turn_analysis`, `finding` and `escalation`
rows of `docs/data-model.md`, as objects. Field names match the columns deliberately:
when `app/db/models.py` is written, persistence becomes an adapter over this shape
rather than a redesign of it, and the pipeline does not change at all.

Nothing here decides anything. The conversation accumulates what happened — messages,
turn analyses, findings, escalations — and exposes the three flags
`app/triage/tiering.py` needs that are not derivable from `ProtocolState` alone. The
tier itself is computed at close, from this record, by the triage layer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from app.domain.enums import ConversationStatus, Presence, Route, Severity
from app.domain.schemas import Finding, TurnExtraction
from app.llm.turn import TurnDraft
from app.protocol import engine
from app.protocol.engine import CaseFacts, ProtocolState
from app.protocol.loader import Protocol, flatten

Role = Literal["assistant", "patient", "system"]

# How much transcript the turn engine is shown. Enough for the model to resolve "it"
# and "that one"; short enough that the prompt cost does not grow with the call.
HISTORY_WINDOW = 8


@dataclass(frozen=True)
class Message:
    """One line of the transcript.

    `is_templated` is not decoration: it records that the patient was shown fixed
    clinician-authored copy rather than generated text, which is the thing a
    liability review asks about first. `template_id` says exactly which copy.
    """

    seq: int
    role: Role
    content: str
    is_templated: bool = False
    template_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class TurnRecord:
    """The audit trail and the eval substrate — invariant 4.

    Written on **every** patient turn, including the ones where the response never
    validated. A conversation whose turn records are complete can be re-scored and
    re-tiered years later; one with gaps cannot be trusted at all, which is why a
    hard failure is Tier 1 rather than a skipped row.
    """

    message_seq: int
    extraction: TurnExtraction | None
    model: str
    prompt_version: str
    hard_failure: bool = False
    validation_retries: int = 0
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw_response: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class EscalationRecord:
    """One route of one fired rule, with the exact words the patient was shown.

    One row per route rather than per finding, per `docs/data-model.md`: a soaking
    dressing owes the surgeon *and* the emergency department, and a dashboard that
    stored the more urgent one would never page the surgeon.
    """

    rule_id: str
    rules_version: str
    severity: Severity
    route: Route
    template_id: str | None
    message_shown: str
    triggered_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Conversation:
    """A single check-in call in progress or finished."""

    id: str
    case: CaseFacts
    state: ProtocolState
    protocol_id: str
    protocol_version: str
    rules_version: str
    prompt_version: str
    patient_ref: str | None = None
    status: ConversationStatus = ConversationStatus.IN_PROGRESS
    messages: list[Message] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    escalations: list[EscalationRecord] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None

    # --- Recording ---------------------------------------------------------

    def add_message(
        self,
        role: Role,
        content: str,
        *,
        is_templated: bool = False,
        template_id: str | None = None,
    ) -> Message:
        message = Message(
            seq=len(self.messages),
            role=role,
            content=content,
            is_templated=is_templated,
            template_id=template_id,
        )
        self.messages.append(message)
        return message

    def add_turn_record(self, message_seq: int, draft: TurnDraft) -> TurnRecord:
        """Persist one turn's analysis. Called before the rules run, never skipped."""
        record = TurnRecord(
            message_seq=message_seq,
            extraction=draft.extraction,
            model=draft.model,
            prompt_version=self.prompt_version,
            hard_failure=draft.hard_failure,
            validation_retries=draft.validation_retries,
            latency_ms=draft.latency_ms,
            input_tokens=draft.input_tokens,
            output_tokens=draft.output_tokens,
            raw_response=draft.raw_response,
        )
        self.turns.append(record)
        return record

    # --- Views -------------------------------------------------------------

    @property
    def history(self) -> tuple[Message, ...]:
        """The transcript tail the turn engine is shown."""
        return tuple(self.messages[-HISTORY_WINDOW:])

    @property
    def extractions(self) -> list[TurnExtraction]:
        """Every turn that produced a usable extraction, in order."""
        return [turn.extraction for turn in self.turns if turn.extraction is not None]

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    # --- Triage inputs -----------------------------------------------------
    #
    # The three things `app/triage/tiering.py` needs that `ProtocolState` does not
    # carry. They are observations read back off the record, not decisions: tiering
    # decides what they mean.

    @property
    def proxy_reported(self) -> bool:
        """Whether anyone other than the patient answered at any point.

        Sticky across the conversation. A proxy who hands the phone back does not
        make the earlier answers first-hand.
        """
        return any(extraction.proxy_detected for extraction in self.extractions)

    @property
    def validation_hard_failure(self) -> bool:
        return any(turn.hard_failure for turn in self.turns)

    @property
    def unresolved_symptom(self) -> bool:
        """Whether any symptom was reported present during the conversation.

        Deliberately coarse, and deliberately fail-open toward escalation: this only
        matters via the tiering clause "ended early with a symptom still unresolved",
        and a conversation that stopped short cannot demonstrate that anything the
        patient raised was actually resolved. Read narrowly it would need to prove a
        negative; read this way the cost of being wrong is one extra callback.

        Anything that fired a rule is already Tier 1 or Tier 2 on its own clause, so
        this changes the outcome only for symptoms no rule covers.
        """
        return any(
            observation.presence is Presence.PRESENT
            for extraction in self.extractions
            for observation in extraction.symptoms
        )


def open_conversation(
    protocol: Protocol,
    case: CaseFacts,
    *,
    rules_version: str | None = None,
    patient_ref: str | None = None,
    conversation_id: str | None = None,
) -> Conversation:
    """Start a check-in and emit its opening message.

    The opening line is the active topic's `opening_question` verbatim. That is
    protocol data written by a clinician, not model output, so a conversation can
    always be opened — there is nothing yet for a turn engine to interpret.
    """
    state = engine.start(protocol, case)
    topic = engine.active_topic(protocol, state)
    if topic is None:  # pragma: no cover - `start` raises before this can happen
        raise ValueError("protocol produced an empty topic queue")

    conversation = Conversation(
        id=conversation_id or str(uuid.uuid4()),
        case=case,
        state=state,
        protocol_id=protocol.protocol_id,
        protocol_version=protocol.version_tag,
        rules_version=rules_version or protocol.rules_version,
        prompt_version=protocol.prompt_version,
        patient_ref=patient_ref,
    )
    conversation.add_message("assistant", flatten(topic.opening_question))
    return conversation
