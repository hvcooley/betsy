"""State machine: which topic is active, and when the conversation moves on.

**Nothing in this module names a topic or a slot.** `start` builds a queue of the
topics whose `applicable_when` matches the case, and `record_turn` walks that queue,
popping a topic when its required slots are filled or when it runs out of turns. A
clinician adding a topic to the YAML gets it asked without any change here — which
is the whole reason the script is data. `tests/test_protocol.py` asserts the absence
of topic-id literals in this file, so the property is enforced rather than assumed.

The engine owns control flow and nothing else. It does not decide escalation (that is
`app/safety/rules.py`), does not decide the tier (`app/triage/tiering.py`), and does
not phrase anything (the LLM). It reads a `TurnExtraction` and updates state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.enums import AnesthesiaType, BlockType, MedAdherence, Presence
from app.domain.schemas import SlotValue, SymptomObservation, TurnExtraction
from app.protocol.loader import Protocol, Slot, Topic

ExitReason = Literal["satisfied", "max_turns", "terminated"]


@dataclass(frozen=True)
class CaseFacts:
    """What the protocol and the safety rules need to know about the case.

    A plain frozen dataclass, not a DB row: the engine is pure, and this keeps it
    testable with no database. It becomes a projection of the `case` table when that
    table is written.
    """

    anesthesia_type: AnesthesiaType
    block_type: BlockType | None = None
    procedure: str | None = None
    procedure_category: str | None = None
    hours_post_op: float | None = None
    expected_block_duration_hours: float | None = None
    block_adjuvant: str | None = None


class TopicOutcome(BaseModel):
    """How a topic ended. `max_turns` is a Tier 2 signal in its own right."""

    topic_id: str
    exit_reason: ExitReason
    turns_used: int


class ProtocolState(BaseModel):
    """Everything needed to resume a conversation. Serialized to `conversation.state_json`."""

    protocol_id: str
    protocol_version: str
    topic_queue: list[str] = Field(
        description="Applicable topic ids in order, fixed at conversation start."
    )
    cursor: int = 0
    turns_in_topic: int = 0
    total_turns: int = 0
    slot_values: dict[str, SlotValue] = Field(default_factory=dict)
    completed: list[TopicOutcome] = Field(default_factory=list)
    finished: bool = False
    halted_reason: str | None = Field(
        default=None, description="Set when a RED escalation or a failed topic stopped the script."
    )

    # Accumulated triage signals. Collected here because the engine is the only layer
    # that sees every turn in sequence; `app/triage/tiering.py` reads them.
    low_confidence_topics: list[str] = Field(default_factory=list)
    unanswered_questions: list[str] = Field(default_factory=list)
    slot_conflicts: list[str] = Field(default_factory=list)

    @property
    def active_topic_id(self) -> str | None:
        if self.finished or self.cursor >= len(self.topic_queue):
            return None
        return self.topic_queue[self.cursor]

    @property
    def exited_on_max_turns(self) -> list[str]:
        return [outcome.topic_id for outcome in self.completed if outcome.exit_reason == "max_turns"]


def start(protocol: Protocol, case: CaseFacts) -> ProtocolState:
    """Open a conversation: work out which topics this case gets, in order.

    Applicability is resolved once, here, rather than re-checked each turn. The case
    facts it branches on do not change mid-call, and a fixed queue means the set of
    topics a conversation was supposed to cover is recoverable from stored state.
    """
    queue = [topic.id for topic in protocol.applicable_topics(case)]
    if not queue:
        raise ValueError("no topics apply to this case; the protocol would have nothing to ask")
    return ProtocolState(
        protocol_id=protocol.protocol_id,
        protocol_version=protocol.version_tag,
        topic_queue=queue,
    )


def active_topic(protocol: Protocol, state: ProtocolState) -> Topic | None:
    """The topic being asked about, or None once the conversation is over."""
    topic_id = state.active_topic_id
    return None if topic_id is None else protocol.topic(topic_id)


def is_satisfied(topic: Topic, state: ProtocolState, threshold: float) -> bool:
    """Whether every required slot holds a usable answer at sufficient confidence."""
    return all(is_filled(slot, state.slot_values.get(slot.id), threshold) for slot in topic.required_slots)


def is_filled(slot: Slot, answer: SlotValue | None, threshold: float) -> bool:
    """Whether one slot holds a usable answer.

    Public because it is the definition of "answered" for the whole system, and a
    second copy of it elsewhere could disagree about what the threshold means.
    """
    if answer is None:
        return False
    return answer.confidence >= threshold and slot.accepts(answer.value)


def record_turn(
    protocol: Protocol, state: ProtocolState, extraction: TurnExtraction
) -> ProtocolState:
    """Fold one patient turn into the state and advance as far as it allows.

    Order matters: merge answers, then advance. A turn that fills the last required
    slot of a topic moves on in the same turn rather than asking one more question
    the patient has already answered.
    """
    if state.finished:
        return state

    updated = state.model_copy(deep=True)
    topic = active_topic(protocol, updated)
    if topic is None:
        updated.finished = True
        return updated

    updated.total_turns += 1
    updated.turns_in_topic += 1
    _merge_slot_values(protocol, topic, updated, extraction)
    _record_conversation_signals(topic, updated, extraction, protocol.slot_confidence_threshold)
    _advance(protocol, updated)

    if not updated.finished and updated.total_turns >= protocol.max_total_turns:
        updated.finished = True
        updated.halted_reason = "max_total_turns"
    return updated


def halt(state: ProtocolState, reason: str) -> ProtocolState:
    """Stop the script where it stands — a RED escalation, or a handoff to a human.

    The remaining topics are simply never asked. They are not recorded as failed
    outcomes, because not asking them was the correct behaviour, not a shortfall.
    """
    halted = state.model_copy(deep=True)
    halted.finished = True
    halted.halted_reason = reason
    return halted


def _merge_slot_values(
    protocol: Protocol, topic: Topic, state: ProtocolState, extraction: TurnExtraction
) -> None:
    """Take the model's answers for this topic's slots, then backfill from clinical fields.

    Only slots belonging to the active topic are accepted. The model is given the
    active topic's schema, so anything else is a stray key rather than an answer, and
    letting it through would let one topic write another topic's slot.
    """
    for slot_id, answer in extraction.slot_values.items():
        slot = topic.slot(slot_id)
        if slot is None or not slot.accepts(answer.value):
            continue
        state.slot_values[slot_id] = answer

    for slot in topic.slots:
        if slot.maps_to is None:
            continue
        derived = _resolve_maps_to(extraction, slot.maps_to)
        if derived is None or not slot.accepts(derived):
            continue
        existing = state.slot_values.get(slot.id)
        if existing is None or existing.value is None:
            state.slot_values[slot.id] = SlotValue(
                value=derived, confidence=extraction.extraction_confidence
            )
        elif existing.value != derived:
            # Two readings of the same message disagree. Neither is authoritative, so
            # record it and let the confidence drop feed the Tier 1 low-confidence
            # clause rather than silently preferring one.
            state.slot_conflicts.append(
                f"{topic.id}.{slot.id}: extracted {existing.value!r} but {slot.maps_to} "
                f"implies {derived!r}"
            )
            state.slot_values[slot.id] = SlotValue(
                value=existing.value,
                confidence=min(existing.confidence, protocol.slot_confidence_threshold) * 0.5,
                quote=existing.quote,
            )


def _resolve_maps_to(extraction: TurnExtraction, path: str) -> bool | int | float | str | None:
    """Read a slot's value out of the turn's clinical fields, or None if unavailable.

    None means "the clinical fields do not answer this", never "the answer is no" —
    an UNKNOWN symptom must not backfill a slot as False.
    """
    if path == "temperature_f":
        return extraction.temperature_f
    if path == "proxy_detected":
        # True only. The field defaults to False, so a False here means "no proxy was
        # noticed", which is not the same as "we established the patient is answering"
        # — backfilling it would mark the question answered without it being asked.
        return True if extraction.proxy_detected else None
    if path.startswith("pain."):
        if extraction.pain is None:
            return None
        return getattr(extraction.pain, path.removeprefix("pain."), None)

    parts = path.split(".")
    if parts[0] == "symptom":
        _, code, attribute = parts
        observation = _symptom_by_value(extraction, code)
        if observation is None or observation.presence is Presence.UNKNOWN:
            return None
        if attribute == "presence":
            return observation.presence is Presence.PRESENT
        if attribute == "severity":
            return None if observation.severity is None else observation.severity.value
        return observation.onset_hours_ago

    if parts[0] == "medication":
        _, name, _attribute = parts
        for report in extraction.medications:
            if report.name and report.name.lower() == name.lower():
                if report.adherence is MedAdherence.UNKNOWN:
                    return None
                return report.adherence.value
    return None


def _symptom_by_value(extraction: TurnExtraction, code_value: str) -> SymptomObservation | None:
    """Look a symptom up by its string value, since `maps_to` paths carry strings."""
    for observation in extraction.symptoms:
        if observation.code.value == code_value:
            return observation
    return None


def _record_conversation_signals(
    topic: Topic, state: ProtocolState, extraction: TurnExtraction, threshold: float
) -> None:
    """Accumulate the signals tiering reads, none of which are decisions."""
    if extraction.extraction_confidence < threshold and topic.id not in state.low_confidence_topics:
        state.low_confidence_topics.append(topic.id)
    if extraction.patient_question:
        state.unanswered_questions.append(extraction.patient_question)
    if extraction.question_answered:
        state.unanswered_questions.clear()


def _advance(protocol: Protocol, state: ProtocolState) -> None:
    """Pop topics off the queue for as long as they are done with.

    A loop rather than a single step because one turn can finish more than one topic:
    a patient who volunteers everything can satisfy the current topic and the next.
    """
    while state.cursor < len(state.topic_queue):
        topic = protocol.topic(state.topic_queue[state.cursor])

        if is_satisfied(topic, state, protocol.slot_confidence_threshold):
            _close_topic(state, topic, "satisfied")
            continue

        if state.turns_in_topic >= topic.max_turns:
            if topic.on_fail == "terminate_politely":
                _close_topic(state, topic, "terminated", advance=False)
                state.finished = True
                state.halted_reason = f"{topic.id}_failed"
                return
            _close_topic(state, topic, "max_turns")
            continue

        return

    state.finished = True


def _close_topic(
    state: ProtocolState, topic: Topic, reason: ExitReason, *, advance: bool = True
) -> None:
    state.completed.append(
        TopicOutcome(topic_id=topic.id, exit_reason=reason, turns_used=state.turns_in_topic)
    )
    state.turns_in_topic = 0
    if advance:
        state.cursor += 1
