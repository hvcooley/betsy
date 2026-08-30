"""The protocol is data, and these tests are what make that claim true.

The load-time checks matter as much as the state machine: a clinician editing YAML
has no type checker, so every way an edit can go wrong silently has to become a loud
error instead. And `test_a_new_topic_needs_no_code_change` is the one that guards the
design goal — if it ever fails, topics have stopped being data.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from app.conversation import pipeline
from app.domain.enums import AnesthesiaType, BlockType, Presence, SymptomCode
from app.domain.schemas import PainReport, SlotValue, SymptomObservation, TurnExtraction
from app.protocol import engine
from app.protocol.engine import CaseFacts, ProtocolState
from app.protocol.loader import (
    Protocol,
    ProtocolError,
    load_default_protocol,
    load_protocol,
    validate_protocol,
)

DEFINITION = Path(__file__).resolve().parents[1] / "app" / "protocol" / "definitions" / "postop_v1.yaml"


@pytest.fixture(scope="module")
def protocol() -> Protocol:
    return load_default_protocol()


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load(DEFINITION.read_text())


def make_turn(topic_id: str, confidence: float = 0.9, **overrides: object) -> TurnExtraction:
    defaults: dict[str, object] = {
        "protocol_version": "postop_v1",
        "prompt_version": "v1",
        "topic_id": topic_id,
        "turn_index": 0,
        "raw_message": "ok",
        "extraction_confidence": confidence,
    }
    return TurnExtraction(**(defaults | overrides))


def slots(**values: object) -> dict[str, SlotValue]:
    return {key: SlotValue(value=value, confidence=0.9) for key, value in values.items()}


GA_CASE = CaseFacts(anesthesia_type=AnesthesiaType.GENERAL, hours_post_op=20)
BLOCK_CASE = CaseFacts(
    anesthesia_type=AnesthesiaType.GENERAL, block_type=BlockType.INTERSCALENE, hours_post_op=20
)
SPINAL_CASE = CaseFacts(
    anesthesia_type=AnesthesiaType.SPINAL, block_type=BlockType.SPINAL, hours_post_op=20
)
LOCAL_CASE = CaseFacts(anesthesia_type=AnesthesiaType.LOCAL, hours_post_op=20)


# --- The shipped definition is coherent -----------------------------------


def test_the_shipped_protocol_loads(protocol: Protocol) -> None:
    assert protocol.protocol_id == "postop_followup"
    assert [topic.id for topic in protocol.topics] == [
        "identity_consent",
        "open_checkin",
        "pain",
        "ponv",
        "cardioresp",
        "block_regression",
        "neuraxial_screen",
        "anesthesia_recovery",
        "local_only_recovery",
        "patient_questions",
        "satisfaction",
        "close",
    ]


def test_every_referenced_rule_exists(protocol: Protocol) -> None:
    """The cross-file check: a topic cannot claim a safety rule nobody wrote."""
    from app.safety.rules import load_rules

    known = set(load_rules(protocol.rules_version).by_id)
    for topic in protocol.topics:
        assert not set(topic.rules) - known, f"{topic.id} references unknown rules"


def test_protocol_and_rules_versions_are_locked_together(protocol: Protocol) -> None:
    from app.safety.rules import load_rules

    assert load_rules(protocol.rules_version).rules_version == protocol.rules_version


def test_anesthesia_recovery_asks_everyone(protocol: Protocol) -> None:
    """Overrides the drafted anesthesia-type condition: MAC and GA share these risks."""
    topic = protocol.topic("anesthesia_recovery")
    assert topic.applicable_when.always is True


def test_anesthesia_recovery_never_asks_what_anesthetic_was_given(protocol: Protocol) -> None:
    """The doctor may not have told the patient, so the question is unanswerable."""
    topic = protocol.topic("anesthesia_recovery")
    assert "NEVER ask the patient what type of anesthesia" in topic.goal


# --- Branching -------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "expected_present", "expected_absent"),
    [
        (GA_CASE, [], ["block_regression", "neuraxial_screen", "local_only_recovery"]),
        (BLOCK_CASE, ["block_regression"], ["neuraxial_screen", "local_only_recovery"]),
        (LOCAL_CASE, ["local_only_recovery"], ["block_regression", "neuraxial_screen"]),
        (
            CaseFacts(anesthesia_type=AnesthesiaType.EPIDURAL),
            ["neuraxial_screen"],
            ["block_regression", "local_only_recovery"],
        ),
    ],
)
def test_topic_queue_branches_on_case_facts(
    protocol: Protocol, case: CaseFacts, expected_present: list[str], expected_absent: list[str]
) -> None:
    queue = [topic.id for topic in protocol.applicable_topics(case)]
    for topic_id in expected_present:
        assert topic_id in queue
    for topic_id in expected_absent:
        assert topic_id not in queue


def test_a_spinal_gets_both_neuraxial_and_block_regression(protocol: Protocol) -> None:
    """Not an accident of the encoding — a spinal is a block that regresses on a clock."""
    queue = [topic.id for topic in protocol.applicable_topics(SPINAL_CASE)]
    assert "neuraxial_screen" in queue
    assert "block_regression" in queue


def test_everyone_gets_the_always_topics(protocol: Protocol) -> None:
    for case in (GA_CASE, BLOCK_CASE, SPINAL_CASE, LOCAL_CASE):
        queue = [topic.id for topic in protocol.applicable_topics(case)]
        assert {"identity_consent", "anesthesia_recovery", "satisfaction", "close"} <= set(queue)


# --- Loader rejects edits that would fail silently -------------------------


def test_duplicate_slot_id_across_topics_is_rejected(raw: dict) -> None:
    """All slots share one answer dict, so a reused id lets one topic clobber another."""
    raw["topics"][1]["slots"].append(
        {"id": "pain_score_now", "required": False, "type": "int", "min": 0, "max": 10}
    )
    with pytest.raises(ProtocolError, match="share one namespace"):
        validate_protocol(raw)


def test_unknown_case_field_is_rejected(raw: dict) -> None:
    raw["topics"][2]["applicable_when"] = {"case_field": "favourite_colour", "equals": "blue"}
    with pytest.raises(ProtocolError, match="unknown case field"):
        validate_protocol(raw)


def test_invalid_enum_value_in_a_branch_is_rejected(raw: dict) -> None:
    """Otherwise the topic simply never matches, and nobody finds out."""
    raw["topics"][2]["applicable_when"] = {"case_field": "anesthesia_type", "equals": "twilight"}
    with pytest.raises(ProtocolError, match="not a valid anesthesia_type"):
        validate_protocol(raw)


def test_unresolvable_maps_to_is_rejected(raw: dict) -> None:
    raw["topics"][2]["slots"][0]["maps_to"] = "pain.vibes"
    with pytest.raises(ProtocolError, match="not a recognised extraction path"):
        validate_protocol(raw)


def test_maps_to_an_unknown_symptom_is_rejected(raw: dict) -> None:
    raw["topics"][3]["slots"][0]["maps_to"] = "symptom.the_ick.presence"
    with pytest.raises(ProtocolError, match="unknown symptom code"):
        validate_protocol(raw)


def test_topic_with_no_required_slot_is_rejected(raw: dict) -> None:
    """It could never be satisfied, so it would always burn its full turn budget."""
    for slot in raw["topics"][2]["slots"]:
        slot["required"] = False
    with pytest.raises(ProtocolError, match="no required slot"):
        validate_protocol(raw)


def test_unknown_question_set_is_rejected(raw: dict) -> None:
    satisfaction = next(topic for topic in raw["topics"] if topic["id"] == "satisfaction")
    satisfaction["question_set"] = "site_atlantis"
    with pytest.raises(ProtocolError, match="unknown question set"):
        validate_protocol(raw)


def test_undefined_rule_reference_is_rejected(tmp_path: Path, raw: dict) -> None:
    raw["topics"][2]["rules"].append("PAIN_VIBES_OFF")
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ProtocolError, match="undefined safety rules"):
        load_protocol(path)


def test_a_topic_cannot_declare_both_slots_and_a_question_set(raw: dict) -> None:
    satisfaction = next(topic for topic in raw["topics"] if topic["id"] == "satisfaction")
    satisfaction["slots"] = [{"id": "extra", "required": True, "type": "bool"}]
    with pytest.raises(ProtocolError, match="must be the only source"):
        validate_protocol(raw)


# --- The satisfaction survey ----------------------------------------------


def test_question_set_expands_into_ordinary_slots(protocol: Protocol) -> None:
    """The engine must see normal slots, so it needs no survey-specific code path."""
    topic = protocol.topic("satisfaction")
    assert [slot.id for slot in topic.slots] == [
        "satisfaction_response",
        "anesthesia_options_explained",
        "anesthesia_risks_explained",
        "satisfaction_comment",
    ]
    assert [slot.id for slot in topic.required_slots] == [
        "satisfaction_response",
        "anesthesia_options_explained",
        "anesthesia_risks_explained",
    ]


def test_survey_response_types_map_onto_slot_types(protocol: Protocol) -> None:
    topic = protocol.topic("satisfaction")
    by_id = {slot.id: slot for slot in topic.slots}
    assert by_id["satisfaction_response"].type == "int"
    assert (by_id["satisfaction_response"].min, by_id["satisfaction_response"].max) == (1, 5)
    assert by_id["anesthesia_options_explained"].type == "bool"
    assert by_id["satisfaction_comment"].type == "text"


def test_survey_slots_are_marked_as_survey(protocol: Protocol) -> None:
    """So summary and tiering can tell survey answers from clinical ones by data."""
    assert all(slot.survey for slot in protocol.topic("satisfaction").slots)
    assert not any(slot.survey for slot in protocol.topic("pain").slots)


def test_satisfaction_carries_no_safety_rules(protocol: Protocol) -> None:
    """Experience feedback is not clinical. The global rules still cover a red flag."""
    assert protocol.topic("satisfaction").rules == []


def test_swapping_the_question_set_swaps_the_whole_survey(raw: dict) -> None:
    """The per-site configurability claim, exercised end to end with no code change."""
    raw["question_sets"]["site_northside"] = {
        "version": 1,
        "label": "Northside survey",
        "questions": [
            {
                "id": "would_recommend",
                "required": True,
                "response_type": "scale_0_10",
                "text": "How likely are you to recommend us?",
            }
        ],
    }
    satisfaction = next(topic for topic in raw["topics"] if topic["id"] == "satisfaction")
    satisfaction["question_set"] = "site_northside"

    protocol = validate_protocol(raw)
    topic = protocol.topic("satisfaction")
    assert [slot.id for slot in topic.slots] == ["would_recommend"]
    assert (topic.slots[0].min, topic.slots[0].max) == (0, 10)


def test_satisfaction_has_enough_turns_for_its_required_questions(protocol: Protocol) -> None:
    """One question at a time (constraint 7) makes turns >= required questions a floor."""
    topic = protocol.topic("satisfaction")
    assert topic.max_turns >= len(topic.required_slots)


# --- The state machine ----------------------------------------------------


def test_start_builds_a_queue_and_opens_on_the_first_topic(protocol: Protocol) -> None:
    state = engine.start(protocol, GA_CASE)
    assert state.active_topic_id == "identity_consent"
    assert state.cursor == 0
    assert not state.finished


def test_filling_required_slots_advances(protocol: Protocol) -> None:
    state = engine.start(protocol, GA_CASE)
    state = engine.record_turn(
        protocol,
        state,
        make_turn(
            "identity_consent",
            slot_values=slots(identity_confirmed=True, is_proxy=False, consent_to_continue=True),
        ),
    )
    assert state.active_topic_id == "open_checkin"
    assert state.completed[-1].exit_reason == "satisfied"
    assert state.turns_in_topic == 0


def test_a_partially_answered_topic_stays_active(protocol: Protocol) -> None:
    state = engine.start(protocol, GA_CASE)
    state = engine.record_turn(
        protocol, state, make_turn("identity_consent", slot_values=slots(identity_confirmed=True))
    )
    assert state.active_topic_id == "identity_consent"
    assert state.turns_in_topic == 1


def test_low_confidence_answers_do_not_fill_a_slot(protocol: Protocol) -> None:
    """Below the threshold the answer is not trusted, so the topic is not satisfied."""
    state = engine.start(protocol, GA_CASE)
    state = engine.record_turn(
        protocol,
        state,
        make_turn(
            "identity_consent",
            confidence=0.2,
            slot_values={
                "identity_confirmed": SlotValue(value=True, confidence=0.2),
                "is_proxy": SlotValue(value=False, confidence=0.2),
                "consent_to_continue": SlotValue(value=True, confidence=0.2),
            },
        ),
    )
    assert state.active_topic_id == "identity_consent"
    assert state.low_confidence_topics == ["identity_consent"]


def test_running_out_of_turns_advances_and_is_recorded(protocol: Protocol) -> None:
    """Exiting on max_turns rather than satisfaction is itself a Tier 2 signal."""
    state = engine.start(protocol, GA_CASE)
    state = state.model_copy(update={"cursor": 1, "topic_queue": state.topic_queue})
    for _ in range(protocol.topic("open_checkin").max_turns):
        state = engine.record_turn(protocol, state, make_turn("open_checkin"))
    assert "open_checkin" in state.exited_on_max_turns
    assert state.active_topic_id == "pain"


def test_terminate_politely_ends_the_conversation(protocol: Protocol) -> None:
    """identity_consent is the one topic whose failure means we must not continue."""
    state = engine.start(protocol, GA_CASE)
    for _ in range(protocol.topic("identity_consent").max_turns):
        state = engine.record_turn(protocol, state, make_turn("identity_consent"))
    assert state.finished
    assert state.halted_reason == "identity_consent_failed"
    assert state.completed[-1].exit_reason == "terminated"


def test_one_turn_can_finish_more_than_one_topic(protocol: Protocol) -> None:
    """A patient who volunteers everything should not be re-asked."""
    state = engine.start(protocol, GA_CASE)
    state = engine.record_turn(
        protocol,
        state,
        make_turn(
            "identity_consent",
            slot_values=slots(identity_confirmed=True, is_proxy=False, consent_to_continue=True),
        ),
    )
    state = engine.record_turn(
        protocol,
        state,
        make_turn("open_checkin", slot_values=slots(patient_reported_concerns="none really")),
    )
    assert state.active_topic_id == "pain"
    assert [outcome.topic_id for outcome in state.completed] == [
        "identity_consent",
        "open_checkin",
    ]


def test_halt_stops_the_script_where_it_stands(protocol: Protocol) -> None:
    state = engine.start(protocol, GA_CASE)
    halted = engine.halt(state, "escalated")
    assert halted.finished
    assert halted.halted_reason == "escalated"
    assert halted.active_topic_id is None


def test_exhausting_the_queue_finishes_the_conversation(protocol: Protocol) -> None:
    state = engine.start(protocol, LOCAL_CASE)
    state = _walk_to_completion(protocol, state)
    assert state.finished
    assert state.active_topic_id is None
    assert {outcome.topic_id for outcome in state.completed} == set(state.topic_queue)


def test_the_global_turn_cap_stops_a_runaway_conversation(protocol: Protocol) -> None:
    state = engine.start(protocol, GA_CASE)
    state = state.model_copy(update={"total_turns": protocol.max_total_turns - 1})
    state = engine.record_turn(
        protocol,
        state,
        make_turn(
            "identity_consent",
            slot_values=slots(identity_confirmed=True, is_proxy=False, consent_to_continue=True),
        ),
    )
    assert state.finished
    assert state.halted_reason == "max_total_turns"


# --- maps_to backfill -----------------------------------------------------


def test_a_slot_is_backfilled_from_the_clinical_fields(protocol: Protocol) -> None:
    """The model reported a pain score but not the slot; do not re-ask for it."""
    state = _state_at(protocol, GA_CASE, "pain")
    state = engine.record_turn(
        protocol, state, make_turn("pain", pain=PainReport(score=4), slot_values=slots())
    )
    assert state.slot_values["pain_score_now"].value == 4


def test_a_symptom_presence_backfills_a_bool_slot(protocol: Protocol) -> None:
    state = _state_at(protocol, GA_CASE, "ponv")
    state = engine.record_turn(
        protocol,
        state,
        make_turn(
            "ponv",
            symptoms=[
                SymptomObservation(code=SymptomCode.NAUSEA, presence=Presence.PRESENT),
                SymptomObservation(code=SymptomCode.VOMITING, presence=Presence.ABSENT),
            ],
        ),
    )
    assert state.slot_values["nausea_present"].value is True
    assert state.slot_values["vomiting_present"].value is False


def test_an_unmentioned_symptom_does_not_backfill_a_slot_as_false(protocol: Protocol) -> None:
    """Silence is not a denial — the slot must stay unanswered, not become False."""
    state = _state_at(protocol, GA_CASE, "ponv")
    state = engine.record_turn(protocol, state, make_turn("ponv"))
    assert "nausea_present" not in state.slot_values


def test_a_conflict_between_slot_and_clinical_field_is_recorded(protocol: Protocol) -> None:
    """Neither reading is authoritative, so confidence drops and the topic is not satisfied."""
    state = _state_at(protocol, GA_CASE, "pain")
    state = engine.record_turn(
        protocol,
        state,
        make_turn("pain", pain=PainReport(score=9), slot_values=slots(pain_score_now=3)),
    )
    assert state.slot_conflicts
    assert state.slot_values["pain_score_now"].confidence < protocol.slot_confidence_threshold


def test_a_slot_from_another_topic_is_ignored(protocol: Protocol) -> None:
    """The model is only given the active topic's schema; anything else is a stray key."""
    state = _state_at(protocol, GA_CASE, "pain")
    state = engine.record_turn(
        protocol, state, make_turn("pain", slot_values=slots(understanding_confirmed=True))
    )
    assert "understanding_confirmed" not in state.slot_values


# --- Patient questions ----------------------------------------------------


def test_a_patient_question_is_held_open_until_answered(protocol: Protocol) -> None:
    state = _state_at(protocol, GA_CASE, "pain")
    state = engine.record_turn(
        protocol, state, make_turn("pain", patient_question="Is it normal to feel this tired?")
    )
    assert state.unanswered_questions == ["Is it normal to feel this tired?"]
    state = engine.record_turn(protocol, state, make_turn("pain", question_answered=True))
    assert state.unanswered_questions == []


# --- The load-bearing property: topics are data ---------------------------


def test_a_new_topic_needs_no_code_change(raw: dict) -> None:
    """The reason the script is YAML at all.

    A clinician adds a topic and a slot to the definition. It must be queued, asked
    and satisfied by exactly the code that shipped, with nothing in Python naming it.
    """
    raw = copy.deepcopy(raw)
    raw["topics"].insert(
        -1,
        {
            "id": "bowel_function",
            "applicable_when": {"always": True},
            "max_turns": 3,
            "goal": "Screen for post-operative constipation, which opioids reliably cause.",
            "opening_question": "Have your bowels moved since the operation?",
            "rules": [],
            "slots": [
                {
                    "id": "bowels_opened",
                    "required": True,
                    "type": "bool",
                    "maps_to": "symptom.constipation.presence",
                    "prompt_hint": "have the patient's bowels moved since surgery",
                }
            ],
        },
    )
    protocol = validate_protocol(raw)

    state = engine.start(protocol, GA_CASE)
    assert "bowel_function" in state.topic_queue

    state = _advance_to(protocol, state, "bowel_function")
    assert engine.active_topic(protocol, state).id == "bowel_function"
    state = engine.record_turn(
        protocol, state, make_turn("bowel_function", slot_values=slots(bowels_opened=True))
    )
    assert state.active_topic_id == "close"
    assert state.completed[-1].exit_reason == "satisfied"


@pytest.mark.parametrize("module", [engine, pipeline], ids=lambda m: Path(m.__file__).name)
def test_the_control_flow_layers_name_no_topic_or_slot(
    protocol: Protocol, module: ModuleType
) -> None:
    """Guards the property above against a well-meaning special case creeping in.

    Both modules that own control flow are checked. The engine decides which topic is
    active; the pipeline decides what happens to a turn. Neither may know what any of
    the topics are *about*, or adding a topic stops being a YAML edit.

    `hours_since_block` is the one deliberate exception, and it lives in the rules
    engine rather than either of these: the block-regression window lookup has to
    know which slot holds the elapsed time.
    """
    source = (Path(module.__file__)).read_text()
    code = "\n".join(
        line for line in source.splitlines() if not re.match(r"\s*#", line)
    ).split('"""')
    body = "".join(code[::2])  # drop docstrings, keep code

    names = {topic.id for topic in protocol.topics}
    names |= {slot.id for topic in protocol.topics for slot in topic.slots}
    leaked = {name for name in names if f'"{name}"' in body or f"'{name}'" in body}
    assert not leaked, (
        f"{Path(module.__file__).name} hardcodes protocol content: {sorted(leaked)}"
    )


# --- Helpers --------------------------------------------------------------


def _satisfy(protocol: Protocol, state: ProtocolState) -> ProtocolState:
    """Answer every required slot of the active topic in one turn."""
    topic = engine.active_topic(protocol, state)
    assert topic is not None
    answers: dict[str, SlotValue] = {}
    for slot in topic.required_slots:
        if slot.type == "bool":
            value: object = True
        elif slot.type == "enum":
            value = (slot.values or ["x"])[0]
        elif slot.type == "text":
            value = "answered"
        else:
            value = slot.min if slot.min is not None else 1
        answers[slot.id] = SlotValue(value=value, confidence=0.95)
    return engine.record_turn(protocol, state, make_turn(topic.id, slot_values=answers))


def _advance_to(protocol: Protocol, state: ProtocolState, topic_id: str) -> ProtocolState:
    while state.active_topic_id not in (topic_id, None):
        state = _satisfy(protocol, state)
    return state


def _state_at(protocol: Protocol, case: CaseFacts, topic_id: str) -> ProtocolState:
    """A fresh conversation wound forward to `topic_id`."""
    return _advance_to(protocol, engine.start(protocol, case), topic_id)


def _walk_to_completion(protocol: Protocol, state: ProtocolState) -> ProtocolState:
    while not state.finished:
        state = _satisfy(protocol, state)
    return state
