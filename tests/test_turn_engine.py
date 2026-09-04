"""The LLM boundary: what is sent, what is trusted, and what happens when it fails.

No network and no API key. The Anthropic client is stubbed, because none of the
properties worth asserting here are properties of the model — they are properties of
the boundary around it: that provenance is stamped by the caller, that a response
which does not validate is retried a bounded number of times and then becomes a
recorded hard failure rather than an exception, that a slot answer the protocol
cannot hold is dropped rather than stored, and that the prompt still contains every
constraint and every vocabulary member it is supposed to.

The one thing deliberately not tested here is whether the model extracts well. That
is the eval harness's job, against scenarios, with a real key.
"""

from __future__ import annotations

import re
from pathlib import Path

import anthropic
import pydantic
import pytest

from app.conversation.session import Message
from app.llm import anthropic_engine
from app.domain.enums import (
    AnesthesiaType,
    MedAdherence,
    Presence,
    Severity,
    SymptomCode,
    Trend,
)
from app.llm.anthropic_engine import AnthropicTurnEngine
from app.llm.client import MissingAPIKey, get_client
from app.llm.context import (
    CONVERSATIONAL_CONSTRAINT_MARKERS,
    PromptError,
    render_system,
    render_turn,
)
from app.llm.turn import TurnDraft, TurnEngine, TurnRequest
from app.llm.wire import SlotAnswer, TurnResponse, to_extraction
from app.protocol import engine
from app.protocol.engine import CaseFacts
from app.protocol.loader import Protocol, load_default_protocol

CASE = CaseFacts(
    anesthesia_type=AnesthesiaType.GENERAL,
    procedure="Right knee arthroscopy",
    hours_post_op=20,
)


@pytest.fixture(scope="module")
def protocol() -> Protocol:
    return load_default_protocol()


@pytest.fixture
def request_(protocol: Protocol) -> TurnRequest:
    state = engine.start(protocol, CASE)
    topic = engine.active_topic(protocol, state)
    assert topic is not None
    return TurnRequest(
        protocol=protocol,
        case=CASE,
        state=state,
        topic=topic,
        history=(Message(seq=0, role="assistant", content="Hello."),),
        patient_message="Jane Doe, third of March 1970, go ahead",
        turn_index=0,
        next_topic=engine.upcoming_topic(protocol, state),
    )


# --- The stub ----------------------------------------------------------------


class _Usage:
    input_tokens = 1200
    output_tokens = 300


class _Block:
    type = "text"

    def __init__(self, parsed: TurnResponse | None, text: str = "{}") -> None:
        self.parsed_output = parsed
        self.text = text


class _Message:
    def __init__(self, parsed: TurnResponse | None, stop_reason: str = "end_turn") -> None:
        self.content = [_Block(parsed)] if parsed is not None else []
        self.model = "claude-sonnet-5"
        self.stop_reason = stop_reason
        self.usage = _Usage()


class StubMessages:
    """Stands in for `client.messages`. Records every call and replays outcomes.

    `stream` and `create` raise rather than returning: a patient-facing reply may
    never be streamed — the safety gate has to be able to discard it — so an engine
    reaching for either is a bug this stub should surface loudly rather than a
    difference in style.
    """

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def stream(self, **kwargs: object) -> object:
        raise AssertionError("patient-facing replies are never streamed (invariant 2)")

    def create(self, **kwargs: object) -> object:
        raise AssertionError("the engine must go through the validated `parse` path")


class StubClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.messages = StubMessages(outcomes)


def make_engine(outcomes: list[object]) -> AnthropicTurnEngine:
    return AnthropicTurnEngine(client=StubClient(outcomes))  # type: ignore[arg-type]


def validation_error() -> pydantic.ValidationError:
    try:
        TurnResponse.model_validate_json('{"extraction_confidence": 7}')
    except pydantic.ValidationError as error:
        return error
    raise AssertionError("expected that payload to be rejected")


def response(**kwargs: object) -> TurnResponse:
    return TurnResponse.model_validate({"draft_reply": "Thanks.", **kwargs})


# --- The interface -----------------------------------------------------------


def test_the_real_engine_satisfies_the_same_protocol_as_the_doubles() -> None:
    """The seam is only worth having if both sides really fit it."""
    assert isinstance(make_engine([_Message(response())]), TurnEngine)


def test_a_missing_key_fails_at_construction_not_mid_conversation() -> None:
    with pytest.raises(MissingAPIKey):
        get_client(api_key="")


# --- Provenance --------------------------------------------------------------


def test_the_model_cannot_state_its_own_provenance(request_: TurnRequest) -> None:
    """The wire schema has no provenance fields, and could not accept them.

    An extraction labelled with the wrong topic would let one topic's answers be
    merged into another's slots, so this is a boundary the model is not given a way
    to cross rather than one it is asked not to.
    """
    for field in ("protocol_version", "prompt_version", "topic_id", "turn_index", "raw_message"):
        assert field not in TurnResponse.model_fields
        with pytest.raises(pydantic.ValidationError):
            TurnResponse.model_validate({"draft_reply": "hi", field: "forged"})


def test_provenance_is_stamped_from_the_request(request_: TurnRequest) -> None:
    extraction = to_extraction(response(), request_)

    assert extraction.topic_id == request_.topic.id
    assert extraction.turn_index == request_.turn_index
    assert extraction.raw_message == request_.patient_message
    assert extraction.protocol_version == request_.protocol.version_tag
    assert extraction.prompt_version == request_.protocol.prompt_version


# --- Slot answers ------------------------------------------------------------


def test_an_answer_for_another_topics_slot_is_dropped_and_recorded(
    request_: TurnRequest,
) -> None:
    """Fail closed, and leave a trace. The slot namespace is global, so a plausible
    id from a later topic would otherwise be stored as if this topic had asked."""
    payload = response(
        slot_answers=[SlotAnswer(slot_id="pain_med_taken", value=True, confidence=0.9)]
    )
    extraction = to_extraction(payload, request_)

    assert extraction.slot_values == {}
    assert "pain_med_taken" in (extraction.notes or "")


def test_a_wrongly_typed_answer_is_dropped_rather_than_coerced(
    request_: TurnRequest,
) -> None:
    """`Slot.accepts` is the same predicate that decides whether a slot is filled.

    Storing a string in a bool slot would let the protocol advance past a question
    nobody actually answered, which is worse than asking it again.
    """
    payload = response(
        slot_answers=[SlotAnswer(slot_id="identity_confirmed", value="probably", confidence=0.9)]
    )
    extraction = to_extraction(payload, request_)

    assert extraction.slot_values == {}
    assert "identity_confirmed" in (extraction.notes or "")


def test_a_well_formed_answer_survives_with_its_confidence_and_quote(
    request_: TurnRequest,
) -> None:
    payload = response(
        slot_answers=[
            SlotAnswer(
                slot_id="identity_confirmed",
                value=True,
                confidence=0.95,
                quote="Jane Doe, third of March 1970",
            )
        ]
    )
    extraction = to_extraction(payload, request_)

    assert extraction.slot("identity_confirmed").value is True
    assert extraction.slot("identity_confirmed").confidence == 0.95
    assert extraction.slot("identity_confirmed").quote == "Jane Doe, third of March 1970"
    assert extraction.notes is None


def test_an_integral_json_number_is_narrowed_for_an_int_slot(protocol: Protocol) -> None:
    """JSON has one number type, so a likert answer arrives as 4.0.

    The only normalization performed on a slot value anywhere. Everything else is
    accepted as sent or dropped.
    """
    state = engine.start(protocol, CASE)
    survey = next(topic for topic in protocol.topics if any(s.survey for s in topic.slots))
    slot = survey.required_slots[0]
    request = TurnRequest(
        protocol=protocol,
        case=CASE,
        state=state,
        topic=survey,
        history=(),
        patient_message="four",
        turn_index=0,
    )
    payload = response(slot_answers=[SlotAnswer(slot_id=slot.id, value=4.0, confidence=0.9)])

    stored = to_extraction(payload, request).slot(slot.id).value
    assert stored == 4
    assert isinstance(stored, int)


# --- The retry ladder --------------------------------------------------------


def test_a_valid_response_becomes_a_draft_with_both_replies(
    request_: TurnRequest,
) -> None:
    payload = response(draft_reply="And your date of birth?", draft_transition_reply="Thanks. Next —")
    draft = make_engine([_Message(payload)]).analyze(request_)

    assert not draft.hard_failure
    assert draft.validation_retries == 0
    assert draft.draft_reply == "And your date of birth?"
    assert draft.transition_reply == "Thanks. Next —"
    assert draft.extraction is not None
    assert draft.model == "claude-sonnet-5"
    assert draft.input_tokens == 1200
    assert draft.latency_ms is not None


def test_a_schema_failure_is_retried_and_the_retry_counted(request_: TurnRequest) -> None:
    engine_ = make_engine([validation_error(), _Message(response())])
    draft = engine_.analyze(request_)

    assert not draft.hard_failure
    assert draft.validation_retries == 1
    assert len(engine_.client.messages.calls) == 2  # type: ignore[attr-defined]


def test_the_retry_names_the_failing_fields_back_to_the_model(
    request_: TurnRequest,
) -> None:
    """A bare 'try again' gets the same answer back."""
    engine_ = make_engine([validation_error(), _Message(response())])
    engine_.analyze(request_)

    retry = engine_.client.messages.calls[1]["messages"][-1]  # type: ignore[attr-defined,index]
    assert retry["role"] == "user"
    assert "extraction_confidence" in retry["content"]


def test_exhausting_the_ladder_is_a_recorded_hard_failure(request_: TurnRequest) -> None:
    """Invariant 4, at the point it matters most.

    Three attempts — one plus two retries, per docs/architecture.md step 2 — and then
    a row rather than an exception. `app/triage/tiering.py` reads `hard_failure` as
    Tier 1 on its own, because a record nobody can trust is its own kind of urgent.
    """
    engine_ = make_engine([validation_error()])
    draft = engine_.analyze(request_)

    assert draft.hard_failure
    assert draft.extraction is None
    assert draft.validation_retries == 2
    assert len(engine_.client.messages.calls) == 3  # type: ignore[attr-defined]
    assert draft.raw_response and "extraction_confidence" in draft.raw_response


def test_a_transport_failure_hard_fails_without_burning_the_ladder(
    request_: TurnRequest,
) -> None:
    """The SDK has already retried what was worth retrying, with backoff.

    Spending the validation budget on a connection that is down would only make the
    patient wait longer for the same answer.
    """
    error = anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]
    engine_ = make_engine([error])
    draft = engine_.analyze(request_)

    assert draft.hard_failure
    assert draft.validation_retries == 0
    assert len(engine_.client.messages.calls) == 1  # type: ignore[attr-defined]
    assert "APIConnectionError" in (draft.raw_response or "")


def test_a_refusal_hard_fails_rather_than_being_papered_over(
    request_: TurnRequest,
) -> None:
    engine_ = make_engine([_Message(None, stop_reason="refusal")])
    draft = engine_.analyze(request_)

    assert draft.hard_failure
    assert draft.raw_response == "refusal"
    assert len(engine_.client.messages.calls) == 1  # type: ignore[attr-defined]


def test_a_truncated_response_is_retried_then_hard_fails(request_: TurnRequest) -> None:
    engine_ = make_engine([_Message(None, stop_reason="max_tokens")])
    draft = engine_.analyze(request_)

    assert draft.hard_failure
    assert "max_tokens" in (draft.raw_response or "")


def test_a_hard_failure_proposes_no_words_of_its_own(request_: TurnRequest) -> None:
    """On the turn the engine has just proved untrustworthy, it writes nothing.

    The pipeline falls back to the protocol's clinician-authored question.
    """
    draft = make_engine([validation_error()]).analyze(request_)

    assert draft.draft_reply == ""
    assert draft.transition_reply == ""


# --- The request itself ------------------------------------------------------


def test_the_system_block_carries_the_cache_breakpoint(request_: TurnRequest) -> None:
    """It is byte-identical on every turn, so it is the only thing worth caching."""
    engine_ = make_engine([_Message(response())])
    engine_.analyze(request_)
    call = engine_.client.messages.calls[0]  # type: ignore[attr-defined]

    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}  # type: ignore[index]
    assert call["thinking"] == {"type": "adaptive"}
    assert "stream" not in call
    assert "budget_tokens" not in str(call["thinking"])


def test_the_system_prompt_does_not_vary_between_turns(protocol: Protocol) -> None:
    """A single varying byte would cost the cache on every turn of every call.

    Not a micro-optimisation: the system block is the largest part of the request and
    is re-sent on every turn of every conversation.
    """
    assert render_system(protocol) == render_system(protocol)


def test_the_last_message_is_the_patient_turn(request_: TurnRequest) -> None:
    engine_ = make_engine([_Message(response())])
    engine_.analyze(request_)
    messages = engine_.client.messages.calls[0]["messages"]  # type: ignore[attr-defined,index]

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert request_.patient_message in messages[0]["content"]


# --- The prompts -------------------------------------------------------------


@pytest.mark.parametrize(("number", "marker"), sorted(CONVERSATIONAL_CONSTRAINT_MARKERS.items()))
def test_every_conversational_constraint_is_in_the_system_prompt(
    protocol: Protocol, number: int, marker: str
) -> None:
    """docs/safety-rules.md: "System-prompt content, and each one gets a test."

    Rewording a constraint is fine — update the marker. Deleting one fails here.
    """
    assert marker in render_system(protocol), f"constraint {number} is missing"


@pytest.mark.parametrize(
    "enum_cls", [SymptomCode, Severity, Presence, Trend, MedAdherence]
)
def test_every_vocabulary_member_reaches_the_model(
    protocol: Protocol, enum_cls: type
) -> None:
    """A closed vocabulary is only closed if it matches the enum the rules match on.

    These lists are generated from `app/domain/enums.py` precisely so that adding a
    symptom code cannot leave the prompt behind — this test is what makes that
    generation load-bearing rather than incidental.
    """
    prompt = render_system(protocol)
    missing = [member.value for member in enum_cls if f"`{member.value}`" not in prompt]
    assert not missing, f"{enum_cls.__name__} members absent from the prompt: {missing}"


def test_the_turn_prompt_carries_the_active_topic_and_its_slots(
    request_: TurnRequest,
) -> None:
    prompt = render_turn(request_)

    assert request_.topic.id in prompt
    for slot in request_.topic.slots:
        assert f"`{slot.id}`" in prompt


def test_the_turn_prompt_names_the_next_topic_as_a_prediction(
    request_: TurnRequest,
) -> None:
    """The lookahead is context for phrasing, never a statement of what happened."""
    prompt = render_turn(request_)
    assert request_.next_topic is not None
    assert request_.next_topic.id in prompt
    assert "If this message completes the current topic" in prompt


def test_the_last_topic_is_told_there_is_nothing_after_it(protocol: Protocol) -> None:
    state = engine.start(protocol, CASE)
    state.cursor = len(state.topic_queue) - 1
    last = engine.active_topic(protocol, state)
    assert last is not None
    prompt = render_turn(
        TurnRequest(
            protocol=protocol,
            case=CASE,
            state=state,
            topic=last,
            history=(),
            patient_message="no, nothing else",
            turn_index=9,
            next_topic=engine.upcoming_topic(protocol, state),
        )
    )

    assert "no topic after this one" in prompt


def test_maintainer_notes_are_not_sent_to_the_model(
    protocol: Protocol, request_: TurnRequest
) -> None:
    assert "<!--" not in render_system(protocol)
    assert "<!--" not in render_turn(request_)


def test_an_unknown_prompt_version_fails_loudly(protocol: Protocol) -> None:
    stale = protocol.model_copy(update={"prompt_version": "v99"})
    with pytest.raises(PromptError):
        render_system(stale)


# --- No fallback -------------------------------------------------------------


def test_the_real_engine_cannot_reach_for_a_test_double() -> None:
    """docs/architecture.md: the doubles "may never be used as a fallback".

    A check-in that silently degraded to keyword matching would be indistinguishable
    in the record from one that did not, which is worse than a check-in that stopped.
    Enforced by reading the source, the same way `engine.py` is held to naming no
    topic — a runtime test cannot prove the absence of a branch nobody took.
    Docstrings and comments are stripped first, as they are there: this module
    explains at length why it has no fallback, and saying so must stay allowed.
    """
    body = _code(Path(anthropic_engine.__file__))

    leaked = [name for name in ("fake", "ScriptedTurnEngine", "KeywordTurnEngine") if name in body]
    assert not leaked, f"the real engine reaches for a test double: {leaked}"


def _code(path: Path) -> str:
    """Source with comments and docstrings removed, as `tests/test_protocol.py` does."""
    source = "\n".join(
        line for line in path.read_text().splitlines() if not re.match(r"\s*#", line)
    )
    return "".join(source.split('"""')[::2])


def test_a_hard_failure_is_a_draft_not_an_exception(request_: TurnRequest) -> None:
    """Every exit from `analyze` is something the pipeline can persist."""
    for outcome in ([validation_error()], [anthropic.APIConnectionError(request=None)]):  # type: ignore[arg-type]
        assert isinstance(make_engine(outcome).analyze(request_), TurnDraft)
