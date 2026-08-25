"""Tiering, and the end-to-end walk that ties every layer together.

The tier is what a clinician trusts to protect their attention, so each clause in
docs/triage-and-summary.md gets its own test — including the ones that have nothing
to do with how sick the patient is. An abandoned conversation and a failed extraction
are Tier 1 because the *record* is unreliable, and a Tier 3 that is not genuinely
approvable from its one-liner would undo the entire labour-saving argument.
"""

from __future__ import annotations

import pytest

from app.domain.enums import (
    AnesthesiaType,
    BlockType,
    Presence,
    Route,
    Severity,
    SymptomCode,
    Tier,
)
from app.domain.schemas import (
    Finding,
    PainReport,
    SlotValue,
    SymptomObservation,
    TurnExtraction,
)
from app.protocol import engine
from app.protocol.engine import CaseFacts, ProtocolState
from app.protocol.loader import Protocol, load_default_protocol
from app.safety.rules import RuleEngine, gate
from app.triage.tiering import assign_tier

GA_CASE = CaseFacts(anesthesia_type=AnesthesiaType.GENERAL, hours_post_op=20)
BLOCK_CASE = CaseFacts(
    anesthesia_type=AnesthesiaType.GENERAL, block_type=BlockType.INTERSCALENE, hours_post_op=20
)


@pytest.fixture(scope="module")
def protocol() -> Protocol:
    return load_default_protocol()


@pytest.fixture(scope="module")
def rules() -> RuleEngine:
    return RuleEngine.load()


def finding(rule_id: str, tier: Tier, route: Route = Route.CALL_SURGEON) -> Finding:
    return Finding(
        rule_id=rule_id,
        rules_version="postop_v1",
        label=rule_id,
        severity=Severity.MODERATE,
        tier=tier,
        routes=[route],
    )


def completed_state(protocol: Protocol, **overrides: object) -> ProtocolState:
    """A state that walked every applicable topic to satisfaction."""
    state = _walk(protocol, engine.start(protocol, GA_CASE))
    return state.model_copy(update=overrides)


# --- Tier 1 ---------------------------------------------------------------


def test_any_red_finding_is_tier_1(protocol: Protocol) -> None:
    decision = assign_tier([finding("CHEST_PAIN", Tier.TIER_1)], completed_state(protocol))
    assert decision.tier is Tier.TIER_1
    assert "CHEST_PAIN" in decision.reason


def test_a_validation_hard_failure_is_tier_1(protocol: Protocol) -> None:
    """Not because the patient is unwell — because the record cannot be believed."""
    decision = assign_tier([], completed_state(protocol), validation_hard_failure=True)
    assert decision.tier is Tier.TIER_1
    assert "schema validation" in decision.reason


def test_abandoned_with_an_unresolved_symptom_is_tier_1(protocol: Protocol) -> None:
    state = engine.start(protocol, GA_CASE)
    decision = assign_tier([], state, unresolved_symptom=True)
    assert decision.tier is Tier.TIER_1


def test_a_proxy_conversation_with_a_yellow_finding_is_tier_1(protocol: Protocol) -> None:
    """Second-hand answers plus something abnormal is not a read-the-transcript case."""
    decision = assign_tier(
        [finding("APAP_STACKING", Tier.TIER_2)], completed_state(protocol), proxy_reported=True
    )
    assert decision.tier is Tier.TIER_1
    assert "second-hand" in decision.reason


def test_a_proxy_conversation_with_no_findings_is_not_tier_1(protocol: Protocol) -> None:
    decision = assign_tier([], completed_state(protocol), proxy_reported=True)
    assert decision.tier is Tier.TIER_3


def test_two_low_confidence_topics_are_tier_1(protocol: Protocol) -> None:
    state = completed_state(protocol, low_confidence_topics=["pain", "ponv"])
    assert assign_tier([], state).tier is Tier.TIER_1


def test_one_low_confidence_topic_is_not(protocol: Protocol) -> None:
    state = completed_state(protocol, low_confidence_topics=["pain"])
    assert assign_tier([], state).tier is Tier.TIER_3


# --- Tier 2 ---------------------------------------------------------------


def test_a_yellow_finding_is_tier_2(protocol: Protocol) -> None:
    decision = assign_tier([finding("APAP_STACKING", Tier.TIER_2)], completed_state(protocol))
    assert decision.tier is Tier.TIER_2
    assert "APAP_STACKING" in decision.reason


def test_an_unanswered_question_is_tier_2(protocol: Protocol) -> None:
    """The agent admitting it cannot answer is correct behaviour, and still needs a human."""
    state = completed_state(protocol, unanswered_questions=["When can I drive again?"])
    assert assign_tier([], state).tier is Tier.TIER_2


def test_a_topic_that_ran_out_of_turns_is_tier_2(protocol: Protocol) -> None:
    state = engine.start(protocol, GA_CASE)
    for _ in range(protocol.topic("identity_consent").max_turns):
        state = engine.record_turn(protocol, state, _turn("identity_consent"))
    # identity_consent terminates rather than advancing, so use a topic that advances.
    state = _walk_to(protocol, engine.start(protocol, GA_CASE), "open_checkin")
    for _ in range(protocol.topic("open_checkin").max_turns):
        state = engine.record_turn(protocol, state, _turn("open_checkin"))
    state = _walk(protocol, state)
    assert assign_tier([], state).tier is Tier.TIER_2


# --- Tier 3 ---------------------------------------------------------------


def test_a_clean_conversation_is_tier_3(protocol: Protocol) -> None:
    decision = assign_tier([], completed_state(protocol))
    assert decision.tier is Tier.TIER_3
    assert decision.reason == "All topics answered, no findings, no unanswered questions."


def test_the_reason_is_always_one_sentence(protocol: Protocol) -> None:
    """A tier a clinician cannot account for at a glance is a tier they stop trusting."""
    for decision in [
        assign_tier([finding("CHEST_PAIN", Tier.TIER_1)], completed_state(protocol)),
        assign_tier([finding("APAP_STACKING", Tier.TIER_2)], completed_state(protocol)),
        assign_tier([], completed_state(protocol)),
    ]:
        assert decision.reason.endswith(".")
        assert decision.reason.count(".") == 1


# --- Survey answers are not clinical --------------------------------------


def test_a_dissatisfied_patient_is_still_tier_3(protocol: Protocol) -> None:
    """The survey is a quality channel, not a triage input. It must never move the tier."""
    state = _walk(
        protocol,
        engine.start(protocol, GA_CASE),
        survey_answers={
            "satisfaction_response": 1,
            "anesthesia_options_explained": False,
            "anesthesia_risks_explained": False,
            "satisfaction_comment": "Nobody told me anything and it was awful.",
        },
    )
    assert state.slot_values["satisfaction_response"].value == 1
    assert assign_tier([], state).tier is Tier.TIER_3


# --- End to end -----------------------------------------------------------


def test_a_clean_checkin_walks_every_topic_and_lands_tier_3(
    protocol: Protocol, rules: RuleEngine
) -> None:
    """The whole stack, in the shape the turn pipeline will drive it."""
    state = engine.start(protocol, BLOCK_CASE)
    findings: list[Finding] = []

    while not state.finished:
        topic = engine.active_topic(protocol, state)
        assert topic is not None
        extraction = _answer(topic)
        findings += rules.evaluate(extraction, BLOCK_CASE, state, topic.rules)
        assert gate(findings) == "none"
        state = engine.record_turn(protocol, state, extraction)

    assert {outcome.topic_id for outcome in state.completed} == set(state.topic_queue)
    assert all(outcome.exit_reason == "satisfied" for outcome in state.completed)
    assert "block_regression" in state.topic_queue
    assert assign_tier(findings, state).tier is Tier.TIER_3


def test_chest_pain_at_the_open_question_halts_the_whole_checkin(
    protocol: Protocol, rules: RuleEngine
) -> None:
    """The safety gate outranks the script: nothing after open_checkin gets asked."""
    state = _walk_to(protocol, engine.start(protocol, GA_CASE), "open_checkin")
    topic = engine.active_topic(protocol, state)
    assert topic is not None

    extraction = _turn(
        "open_checkin",
        symptoms=[
            SymptomObservation(
                code=SymptomCode.CHEST_PAIN,
                presence=Presence.PRESENT,
                quote="my chest feels really tight",
            )
        ],
    )
    findings = rules.evaluate(extraction, GA_CASE, state, topic.rules)

    assert gate(findings) == "red"
    assert [f.rule_id for f in findings] == ["CHEST_PAIN"]
    assert findings[0].routes == [Route.CALL_911]

    state = engine.halt(engine.record_turn(protocol, state, extraction), "escalated")
    assert state.finished
    assert "cardioresp" not in {outcome.topic_id for outcome in state.completed}

    decision = assign_tier(findings, state)
    assert decision.tier is Tier.TIER_1
    assert "CHEST_PAIN" in decision.reason


def test_a_prolonged_block_is_tier_2_and_reaches_anesthesia(
    protocol: Protocol, rules: RuleEngine
) -> None:
    """The conversation continues — a yellow finding is a callback, not an emergency."""
    late_case = CaseFacts(
        anesthesia_type=AnesthesiaType.GENERAL, block_type=BlockType.INTERSCALENE, hours_post_op=30
    )
    state = _walk_to(protocol, engine.start(protocol, late_case), "block_regression")
    topic = engine.active_topic(protocol, state)
    assert topic is not None

    extraction = _turn(
        "block_regression",
        slot_values={
            "sensation_returning": False,
            "motor_function": "weak",
            "hours_since_block": 30.0,
        },
    )
    findings = rules.evaluate(extraction, late_case, state, topic.rules)

    assert "BLOCK_PROLONGED" in {f.rule_id for f in findings}
    assert gate(findings) == "yellow"
    assert Route.CALL_ANESTHESIA in findings[0].routes

    state = engine.record_turn(protocol, state, extraction)
    assert not state.finished, "a yellow finding must not stop the conversation"
    assert assign_tier(findings, _walk(protocol, state)).tier is Tier.TIER_2


# --- Helpers --------------------------------------------------------------


def _turn(topic_id: str, **overrides: object) -> TurnExtraction:
    defaults: dict[str, object] = {
        "protocol_version": "postop_v1",
        "prompt_version": "v1",
        "topic_id": topic_id,
        "turn_index": 0,
        "raw_message": "…",
        "extraction_confidence": 0.9,
    }
    if "slot_values" in overrides:
        overrides["slot_values"] = {
            key: SlotValue(value=value, confidence=0.95)
            for key, value in overrides["slot_values"].items()  # type: ignore[union-attr]
        }
    return TurnExtraction(**(defaults | overrides))


def _answer(topic, survey_answers: dict | None = None) -> TurnExtraction:
    """A turn answering every required slot of `topic` in a way nothing flags on.

    The clinical fields have to agree with the slot answers, or the engine records a
    conflict and refuses to count the slot as filled — which is the behaviour under
    test elsewhere, and would silently defeat this walk if the helper contradicted
    itself.
    """
    answers: dict[str, object] = {}
    for slot in topic.required_slots:
        if survey_answers and slot.id in survey_answers:
            answers[slot.id] = survey_answers[slot.id]
        elif slot.type == "bool":
            # Polarity differs by slot: a symptom-backed slot is benign when False,
            # a confirmation when True. `maps_to` is what tells the two apart.
            answers[slot.id] = not slot.maps_to
        elif slot.type == "enum":
            answers[slot.id] = (slot.values or ["normal"])[0]
        elif slot.type == "text":
            answers[slot.id] = "nothing in particular"
        else:
            answers[slot.id] = slot.min if slot.min is not None else 0

    extraction = _turn(topic.id, slot_values=answers)
    if "pain_score_now" in answers:
        extraction = extraction.model_copy(
            update={"pain": PainReport(score=int(answers["pain_score_now"]))}  # type: ignore[arg-type]
        )
    return extraction


def _walk(
    protocol: Protocol, state: ProtocolState, survey_answers: dict | None = None
) -> ProtocolState:
    while not state.finished:
        topic = engine.active_topic(protocol, state)
        assert topic is not None
        state = engine.record_turn(protocol, state, _answer(topic, survey_answers=survey_answers))
    return state


def _walk_to(protocol: Protocol, state: ProtocolState, topic_id: str) -> ProtocolState:
    while state.active_topic_id not in (topic_id, None):
        topic = engine.active_topic(protocol, state)
        assert topic is not None
        state = engine.record_turn(protocol, state, _answer(topic))
    return state
