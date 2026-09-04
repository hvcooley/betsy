"""The shape the model is allowed to return, and the adapter back to domain types.

Deliberately **not** `TurnExtraction`. That model carries provenance —
`protocol_version`, `prompt_version`, `topic_id`, `turn_index`, `raw_message` — and
provenance is the caller's to state, never the model's. An extraction that labelled
itself with the wrong topic would let one topic's answers be merged into another's
slots, so those five fields are stamped by `TurnRequest.provenance` after the call
and cannot be reached from the wire schema at all.

Everything else is reused from `app/domain/schemas.py` rather than restated here.
`PainReport`, `SymptomObservation` and `MedicationReport` are pure observations with
no provenance, so the model emits them directly and their validators run on the way
in — `check_severity_matches_presence` rejecting an incoherent extraction is a retry
trigger for free, before a safety rule can ever act on it.

The one field that cannot be reused is `slot_values`. On the domain model it is a
dict keyed by slot id; a JSON schema for an open-ended dict is an
`additionalProperties` map, which strict validation cannot constrain and which gives
the model no place to be told what the keys are. On the wire it is a **list** of
`SlotAnswer`, each naming its slot explicitly, and `to_extraction` turns it back into
the dict the protocol engine reads.

### Slot values are a union today, a generated schema later

`SlotAnswer.value` is a `bool | int | float | str | None` union, and every value is
put through the owning `Slot.accepts()` before it is kept. That is the MVP tradeoff:
one static schema, no dynamic generation, and the protocol YAML still has the final
say on what an answer may be, because `accepts()` is the same predicate
`app/protocol/engine.py` uses to decide whether a slot is filled.

The union is what a strict schema cannot pin down: it lets the model return a string
for an `int` slot and a number for a `bool` one, and the only thing standing between
that and a dropped answer is the `accepts()` check below. The intended replacement is
to **generate a topic-specific JSON schema from the YAML slot definitions** on each
turn, so the active topic's slots appear as named, correctly typed properties and the
model cannot express a wrong-typed answer in the first place. That keeps the YAML
authoritative without flattening everything to one union, at the cost of building a
schema per topic and losing a single cacheable schema across turns. The seam for it
is `slot_answers_schema()` below, which is where that generation would land; nothing
outside this module knows the schema is static.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.schemas import (
    MedicationReport,
    PainReport,
    SlotValue,
    SymptomObservation,
    TurnExtraction,
)

if TYPE_CHECKING:
    from app.llm.turn import TurnRequest
    from app.protocol.loader import Topic


class SlotAnswer(BaseModel):
    """One protocol slot answered, named explicitly so the wire form is a list.

    `value` is a union rather than a per-slot type — see the module docstring. It is
    never trusted: `to_extraction` drops any answer the owning slot's `accepts()`
    rejects, and records why.
    """

    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(description="Must be one of the active topic's declared slots.")
    value: bool | int | float | str | None = Field(
        default=None, description="The answer, in whatever JSON type the slot declared."
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    quote: str | None = Field(default=None, description="The patient's own words.")


class TurnResponse(BaseModel):
    """Exactly what one call may return: observations, and two proposed replies.

    No decisions. The model does not say which topic is active, whether to escalate,
    or whether the topic is finished — the first two are invariant 1, and the third
    is why there are two drafts instead of one.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Clinical content -------------------------------------------------
    pain: PainReport | None = None
    symptoms: list[SymptomObservation] = Field(default_factory=list)
    medications: list[MedicationReport] = Field(default_factory=list)
    temperature_f: float | None = None

    # --- Protocol answers -------------------------------------------------
    slot_answers: list[SlotAnswer] = Field(default_factory=list)

    # --- Conversation signals ---------------------------------------------
    question_answered: bool = False
    patient_question: str | None = None
    proxy_detected: bool = False
    off_topic: bool = False
    patient_requests_human: bool = False
    patient_distress: bool = False
    unparseable: bool = False
    extraction_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str | None = None

    # --- Proposed replies -------------------------------------------------
    # Both are proposals. The safety gate discards both on a RED finding, and
    # `app/conversation/pipeline.py` picks between them once the protocol engine has
    # said whether the topic closed — which is not known when this is written.
    draft_reply: str = Field(
        default="", description="Used when the current topic is still open after this turn."
    )
    draft_transition_reply: str = Field(
        default="", description="Used only when this turn closed the topic and the next one is as predicted."
    )


def slot_answers_schema() -> dict[str, Any]:
    """The JSON schema fragment describing `slot_answers`.

    Exposed as a function, and only used for the prompt's schema notes, so that the
    move to per-topic generated schemas described in the module docstring has one
    place to happen rather than being spread through the engine.
    """
    return TurnResponse.model_json_schema()["properties"]["slot_answers"]


def to_extraction(response: TurnResponse, request: TurnRequest) -> TurnExtraction:
    """Merge one wire response with caller-owned provenance into a `TurnExtraction`.

    `request.provenance` is applied **last** and therefore wins: the model cannot
    label its own turn, whatever it returned.
    """
    slot_values, rejected = accepted_slot_values(response, request.topic)
    return TurnExtraction.model_validate(
        {
            "pain": response.pain,
            "symptoms": response.symptoms,
            "medications": response.medications,
            "temperature_f": response.temperature_f,
            "slot_values": slot_values,
            "question_answered": response.question_answered,
            "patient_question": response.patient_question,
            "proxy_detected": response.proxy_detected,
            "off_topic": response.off_topic,
            "patient_requests_human": response.patient_requests_human,
            "patient_distress": response.patient_distress,
            "unparseable": response.unparseable,
            "extraction_confidence": response.extraction_confidence,
            "notes": _notes(response.notes, rejected),
            **request.provenance,
        }
    )


def accepted_slot_values(
    response: TurnResponse, topic: Topic
) -> tuple[dict[str, SlotValue], list[str]]:
    """Keep the answers this topic can actually hold; report the rest.

    Two filters, both fail-closed. A `slot_id` the active topic did not declare is
    dropped, because the model is only shown this topic's schema and anything else is
    a stray key rather than an answer — letting it through would let one topic write
    another topic's slot. A value the slot's own `accepts()` rejects is dropped for
    the same reason it would be rejected anywhere else: an unusable answer must not
    satisfy a required slot and let the protocol advance past a question nobody
    answered.

    Dropped answers are returned rather than swallowed, so they reach `notes` and the
    turn record. A slot that keeps being rejected shows up as a topic exiting on
    `max_turns`, which is already a triage signal — but only if the reason is
    legible when someone reads the row.
    """
    accepted: dict[str, SlotValue] = {}
    rejected: list[str] = []
    for answer in response.slot_answers:
        slot = topic.slot(answer.slot_id)
        if slot is None:
            rejected.append(f"{answer.slot_id!r} is not a slot of topic {topic.id!r}")
            continue
        value = _whole_number(answer.value) if slot.type == "int" else answer.value
        if not slot.accepts(value):
            rejected.append(
                f"{answer.slot_id!r} rejected value {answer.value!r} for a {slot.type} slot"
            )
            continue
        accepted[answer.slot_id] = SlotValue(
            value=value, confidence=answer.confidence, quote=answer.quote
        )
    return accepted, rejected


def _whole_number(value: object) -> object:
    """Narrow an integral JSON number to an `int` for an `int` slot.

    The only normalization performed anywhere on a slot value. JSON has one number
    type, so a likert answer arrives as `4.0` and would be stored as a float — which
    `accepts()` allows, but which then reads back as a different type than the YAML
    declared. Anything not exactly integral is left alone and fails `accepts()`.
    """
    if isinstance(value, bool) or not isinstance(value, float):
        return value
    return int(value) if value.is_integer() else value


def _notes(model_notes: str | None, rejected: list[str]) -> str | None:
    if not rejected:
        return model_notes
    dropped = "dropped slot answers: " + "; ".join(rejected)
    return f"{model_notes} | {dropped}" if model_notes else dropped
