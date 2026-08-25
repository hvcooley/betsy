"""Parses and validates YAML protocol definitions into typed objects.

The protocol is data so that a clinician can add a topic without a developer. That
only holds if a malformed edit fails loudly at startup rather than producing a topic
that silently never applies, or a rule reference that silently never fires. Every
check in this module exists to convert one such silent failure into a load error:

- unknown `applicable_when` field or enum value -> a topic that would never match
- duplicate slot id -> two topics overwriting each other's answers, since all slots
  share one answer dict
- unresolvable `maps_to` path -> a backfill that quietly never happens
- rule id with no definition -> a safety rule the topic thinks it has
- `rules_version` mismatch -> a half-upgraded protocol/rules pair

The rule-reference check reads the paired safety rules file, so the two versioned
artifacts are validated against each other rather than in isolation.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.domain.enums import AnesthesiaType, BlockType, SymptomCode

DEFINITIONS_DIR = Path(__file__).parent / "definitions"

# Case fields a topic is allowed to branch on, with the enum that constrains each.
# `None` means the field is not enum-valued and only null checks apply.
BRANCHABLE_CASE_FIELDS: dict[str, type[Enum] | None] = {
    "anesthesia_type": AnesthesiaType,
    "block_type": BlockType,
    "procedure_category": None,
}

# Survey response types, and the slot each expands into. Keeping the mapping here is
# what lets the satisfaction topic reach the engine as ordinary slots, so the engine
# needs no survey-specific code path.
RESPONSE_TYPE_SLOTS: dict[str, dict[str, Any]] = {
    "yes_no": {"type": "bool"},
    "likert_5": {"type": "int", "min": 1, "max": 5},
    "scale_0_10": {"type": "int", "min": 0, "max": 10},
    "free_text": {"type": "text"},
}

# Top-level `TurnExtraction` fields a slot may be backfilled from. Symptom and
# medication paths are validated separately since they carry a code or a name.
SCALAR_MAPS_TO_PATHS: dict[str, str] = {
    "pain.score": "int",
    "pain.worst_score": "int",
    "pain.location": "text",
    "pain.controlled_by_medication": "bool",
    "temperature_f": "float",
    "proxy_detected": "bool",
}


class ProtocolError(ValueError):
    """A protocol definition that cannot be trusted to run. Raised at load time."""


class Applicability(BaseModel):
    """Whether a topic is in this conversation at all.

    A structured predicate rather than an expression string: there is no evaluator
    to sandbox, and every field name and value can be checked against the real case
    fields and enums when the file loads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    always: bool | None = None
    case_field: str | None = None
    equals: str | None = None
    in_: list[str] | None = Field(default=None, alias="in")
    is_null: bool | None = None

    @model_validator(mode="after")
    def check_exactly_one_form(self) -> Self:
        if self.always is not None:
            if self.case_field is not None:
                raise ProtocolError("`always` cannot be combined with a case_field test")
            return self
        if self.case_field is None:
            raise ProtocolError("applicable_when needs either `always` or a `case_field`")
        if self.case_field not in BRANCHABLE_CASE_FIELDS:
            raise ProtocolError(
                f"unknown case field {self.case_field!r}; "
                f"branchable fields are {sorted(BRANCHABLE_CASE_FIELDS)}"
            )
        tests = [self.equals, self.in_, self.is_null]
        if sum(test is not None for test in tests) != 1:
            raise ProtocolError(
                f"applicable_when on {self.case_field!r} needs exactly one of "
                "`equals`, `in` or `is_null`"
            )
        enum_cls = BRANCHABLE_CASE_FIELDS[self.case_field]
        if enum_cls is not None:
            allowed = {member.value for member in enum_cls}
            for value in [self.equals] if self.equals is not None else (self.in_ or []):
                if value not in allowed:
                    raise ProtocolError(
                        f"{value!r} is not a valid {self.case_field}; expected one of "
                        f"{sorted(allowed)}"
                    )
        return self

    def matches(self, case_value: object) -> bool:
        """Whether a case with this field value gets the topic.

        Compares by value rather than by enum identity so the same predicate works
        against a stored string or an enum member.
        """
        if self.always is not None:
            return self.always
        value = case_value.value if isinstance(case_value, Enum) else case_value
        if self.is_null is not None:
            return (value is None) == self.is_null
        if self.equals is not None:
            return value == self.equals
        return value in (self.in_ or [])


class Slot(BaseModel):
    """One thing a topic has to learn from the patient."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    required: bool = False
    type: Literal["bool", "int", "float", "enum", "text"]
    min: float | None = None
    max: float | None = None
    values: list[str] | None = None
    prompt_hint: str | None = None
    maps_to: str | None = None
    # Set by the loader for slots expanded out of a question set, so the summary and
    # tiering layers can tell survey answers from clinical ones without a topic list.
    survey: bool = False

    @model_validator(mode="after")
    def check_shape_matches_type(self) -> Self:
        if self.type == "enum" and not self.values:
            raise ProtocolError(f"slot {self.id!r} is an enum but declares no `values`")
        if self.type != "enum" and self.values:
            raise ProtocolError(f"slot {self.id!r} declares `values` but is not an enum")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ProtocolError(f"slot {self.id!r} has min above max")
        if self.maps_to is not None:
            _check_maps_to(self.id, self.maps_to, self.type)
        return self

    def accepts(self, value: object) -> bool:
        """Whether `value` is a usable answer to this slot.

        `None` is never acceptable: an unfilled slot must not satisfy its topic.
        """
        if value is None:
            return False
        if self.type == "bool":
            return isinstance(value, bool)
        if self.type == "text":
            return isinstance(value, str) and bool(value.strip())
        if self.type == "enum":
            return value in (self.values or [])
        if isinstance(value, bool) or not isinstance(value, int | float):
            return False
        if self.min is not None and value < self.min:
            return False
        return not (self.max is not None and value > self.max)


def _check_maps_to(slot_id: str, path: str, slot_type: str) -> None:
    """Reject a backfill path that does not resolve, or resolves to the wrong type."""
    if path in SCALAR_MAPS_TO_PATHS:
        expected = SCALAR_MAPS_TO_PATHS[path]
        if expected != slot_type and not (expected == "float" and slot_type == "int"):
            raise ProtocolError(
                f"slot {slot_id!r} is {slot_type} but {path!r} holds {expected}"
            )
        return

    parts = path.split(".")
    if len(parts) == 3 and parts[0] == "symptom":
        _, code, attribute = parts
        if code not in {member.value for member in SymptomCode}:
            raise ProtocolError(f"slot {slot_id!r} maps to unknown symptom code {code!r}")
        if attribute not in {"presence", "severity", "onset_hours_ago"}:
            raise ProtocolError(
                f"slot {slot_id!r} maps to unknown symptom attribute {attribute!r}"
            )
        if attribute == "presence" and slot_type != "bool":
            raise ProtocolError(
                f"slot {slot_id!r} maps to a symptom presence, so it must be a bool"
            )
        return

    if len(parts) == 3 and parts[0] == "medication" and parts[2] == "adherence":
        return

    raise ProtocolError(
        f"slot {slot_id!r} maps to {path!r}, which is not a recognised extraction path"
    )


class SurveyQuestion(BaseModel):
    """One question in a per-site experience survey."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    text: str
    response_type: Literal["yes_no", "likert_5", "scale_0_10", "free_text"]
    required: bool = False

    def to_slot(self) -> Slot:
        return Slot(
            id=self.id,
            required=self.required,
            prompt_hint=self.text,
            survey=True,
            **RESPONSE_TYPE_SLOTS[self.response_type],
        )


class QuestionSet(BaseModel):
    """A named, versioned survey. Swapping the reference swaps the whole survey."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    label: str
    questions: list[SurveyQuestion]

    @model_validator(mode="after")
    def check_questions_are_unique(self) -> Self:
        ids = [question.id for question in self.questions]
        if len(set(ids)) != len(ids):
            raise ProtocolError(f"question set {self.label!r} repeats a question id")
        return self


class Topic(BaseModel):
    """One section of the check-in."""

    model_config = ConfigDict(extra="forbid")

    id: str
    applicable_when: Applicability
    goal: str
    max_turns: int = Field(ge=1)
    opening_question: str
    rules: list[str] = Field(default_factory=list)
    on_fail: Literal["advance", "terminate_politely"] = "advance"
    slots: list[Slot] = Field(default_factory=list)
    question_set: str | None = None

    @model_validator(mode="after")
    def check_slots_come_from_exactly_one_source(self) -> Self:
        if self.question_set and self.slots:
            raise ProtocolError(
                f"topic {self.id!r} declares both `slots` and a `question_set`; "
                "a question set expands into the topic's slots, so it must be the only source"
            )
        if not self.question_set and not self.slots:
            raise ProtocolError(f"topic {self.id!r} has no slots and no question_set")
        ids = [slot.id for slot in self.slots]
        if len(set(ids)) != len(ids):
            raise ProtocolError(f"topic {self.id!r} repeats a slot id")
        return self

    @property
    def required_slots(self) -> list[Slot]:
        return [slot for slot in self.slots if slot.required]

    def slot(self, slot_id: str) -> Slot | None:
        for slot in self.slots:
            if slot.id == slot_id:
                return slot
        return None

    def applies_to(self, case: object) -> bool:
        """Whether this topic is in the conversation for `case`."""
        predicate = self.applicable_when
        if predicate.always is not None:
            return predicate.always
        return predicate.matches(getattr(case, predicate.case_field or "", None))


class Protocol(BaseModel):
    """A whole versioned check-in script."""

    model_config = ConfigDict(extra="forbid")

    protocol_id: str
    version: int
    rules_version: str
    prompt_version: str
    slot_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    max_total_turns: int = Field(default=60, ge=1)
    question_sets: dict[str, QuestionSet] = Field(default_factory=dict)
    topics: list[Topic]

    @model_validator(mode="after")
    def expand_and_check(self) -> Self:
        if not self.topics:
            raise ProtocolError("a protocol with no topics has nothing to ask")

        topic_ids = [topic.id for topic in self.topics]
        if len(set(topic_ids)) != len(topic_ids):
            raise ProtocolError("duplicate topic id")

        seen_slots: dict[str, str] = {}
        for topic in self.topics:
            if topic.question_set:
                question_set = self.question_sets.get(topic.question_set)
                if question_set is None:
                    raise ProtocolError(
                        f"topic {topic.id!r} references unknown question set "
                        f"{topic.question_set!r}"
                    )
                topic.slots = [question.to_slot() for question in question_set.questions]
            if not topic.required_slots:
                raise ProtocolError(
                    f"topic {topic.id!r} has no required slot, so it can never be satisfied "
                    "and would always exit on max_turns"
                )
            for slot in topic.slots:
                # Global rather than per-topic: every answer lands in one dict keyed by
                # slot id, so a reused id would let one topic overwrite another's answer.
                if slot.id in seen_slots:
                    raise ProtocolError(
                        f"slot id {slot.id!r} is used by both {seen_slots[slot.id]!r} "
                        f"and {topic.id!r}; slot ids share one namespace"
                    )
                seen_slots[slot.id] = topic.id
        return self

    @property
    def version_tag(self) -> str:
        return f"{self.protocol_id}_v{self.version}"

    def topic(self, topic_id: str) -> Topic:
        for topic in self.topics:
            if topic.id == topic_id:
                return topic
        raise KeyError(topic_id)

    def slot(self, slot_id: str) -> Slot | None:
        for topic in self.topics:
            found = topic.slot(slot_id)
            if found is not None:
                return found
        return None

    def applicable_topics(self, case: object) -> list[Topic]:
        """The topics this case gets, in file order."""
        return [topic for topic in self.topics if topic.applies_to(case)]


def validate_protocol(raw: dict[str, Any]) -> Protocol:
    """Build a `Protocol` from parsed YAML, reporting failures as `ProtocolError`.

    Pydantic wraps whatever a validator raises in a `ValidationError`, which would
    make a clinician's YAML mistake surface as a schema traceback with the actual
    explanation buried inside it. Unwrapping here keeps the message the validators
    wrote — the one that says which topic and which field — as the message callers
    see.
    """
    try:
        return Protocol.model_validate(raw)
    except ValidationError as error:
        for detail in error.errors():
            cause = detail.get("ctx", {}).get("error")
            if isinstance(cause, ProtocolError):
                raise cause from error
        raise ProtocolError(str(error)) from error


def load_protocol(path: Path, *, known_rule_ids: set[str] | None = None) -> Protocol:
    """Load and validate a protocol definition.

    `known_rule_ids` defaults to the ids in the paired safety rules file, which is
    what makes a reference to a nonexistent rule a startup error. Pass an explicit
    set only in tests that are exercising the protocol in isolation.
    """
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ProtocolError(f"{path} does not contain a protocol mapping")
    protocol = validate_protocol(raw)

    if known_rule_ids is None:
        from app.safety.rules import load_rules  # circular at module scope

        rule_set = load_rules(protocol.rules_version)
        if rule_set.rules_version != protocol.rules_version:
            raise ProtocolError(
                f"protocol {protocol.version_tag} expects rules {protocol.rules_version} "
                f"but the file declares {rule_set.rules_version}"
            )
        known_rule_ids = set(rule_set.by_id)

    for topic in protocol.topics:
        unknown = [rule_id for rule_id in topic.rules if rule_id not in known_rule_ids]
        if unknown:
            raise ProtocolError(
                f"topic {topic.id!r} references undefined safety rules {sorted(unknown)}"
            )
    return protocol


def load_default_protocol() -> Protocol:
    """The protocol the MVP runs: `postop_v1`."""
    return load_protocol(DEFINITIONS_DIR / "postop_v1.yaml")
