"""The seams between the layers, which no unit test can reach.

The protocol engine, the rule engine and the tiering module are each correct in
isolation and tested that way. What is left is the *order* they run in, and every
safety property of the turn pipeline lives there rather than in any one of them:
which topic's rules a turn is judged under, whether a drafted reply survives the
gate, what reason the halt records, and whether the audit row gets written when the
turn went wrong.

Each test here corresponds to one of the numbered decisions in the docstring of
`app/conversation/pipeline.py`.
"""

from __future__ import annotations

import pytest

from app.conversation.pipeline import (
    ESCALATED,
    ConversationClosed,
    Pipeline,
    TurnOutcome,
)
from app.conversation.session import Conversation
from app.domain.enums import (
    AnesthesiaType,
    BlockType,
    ConversationStatus,
    Presence,
    Route,
    Severity,
    SymptomCode,
    Tier,
)
from app.llm.fake import ScriptedTurn, ScriptedTurnEngine
from app.protocol.engine import CaseFacts
from app.protocol.loader import Protocol, load_default_protocol
from app.safety import templates
from app.safety.rules import RuleEngine
from app.summary.generator import build_summary
from app.triage.tiering import assign_tier

GA_CASE = CaseFacts(
    anesthesia_type=AnesthesiaType.GENERAL,
    procedure="Right knee arthroscopy",
    hours_post_op=20,
)
BLOCK_CASE = CaseFacts(
    anesthesia_type=AnesthesiaType.GENERAL,
    block_type=BlockType.INTERSCALENE,
    procedure="Rotator cuff repair",
    hours_post_op=30,
)

# The two topics every case opens with, answered in one turn each. Written as data so
# a test can say "get me to the pain topic" without naming how.
IDENTITY = ScriptedTurn(
    say="Jane Doe, third of March 1970, go ahead",
    extract={
        "slot_values": {
            "identity_confirmed": {"value": True},
            "is_proxy": {"value": False},
            "consent_to_continue": {"value": True},
        }
    },
)
OPEN_CHECKIN = ScriptedTurn(
    say="Not too bad. It was the right knee.",
    extract={
        "slot_values": {
            "patient_reported_concerns": {"value": "sore knee"},
            "procedure_confirmed": {"value": True},
        }
    },
)
PREAMBLE = [IDENTITY, OPEN_CHECKIN]


@pytest.fixture(scope="module")
def protocol() -> Protocol:
    return load_default_protocol()


def run(
    turns: list[ScriptedTurn], protocol: Protocol, case: CaseFacts = GA_CASE
) -> tuple[Pipeline, Conversation, list[TurnOutcome]]:
    """Replay `turns` and hand back everything a test might want to look at."""
    pipeline = Pipeline(
        protocol=protocol,
        rules=RuleEngine.load(protocol.rules_version),
        turn_engine=ScriptedTurnEngine(turns),
    )
    conversation = pipeline.open(case)
    outcomes = [
        pipeline.send(conversation, turn.say)
        for turn in turns
        if not conversation.state.finished
    ]
    return pipeline, conversation, outcomes


# --- 1. Rules see the topic that was active when the message arrived ---------


def test_a_turn_that_completes_a_topic_is_still_judged_by_that_topic(
    protocol: Protocol,
) -> None:
    """The ordering that would be easiest to get wrong, and worst to get wrong.

    This one turn fills the pain topic's last required slot *and* trips one of the
    pain topic's own rules. Advancing before evaluating would move the cursor to the
    next topic first, and the rule — which belongs to no other topic — would never be
    considered. The patient would have reported pain in the wrong place and nothing
    would have noticed.
    """
    atypical_pain = ScriptedTurn(
        say="It's an 8, and it's in my calf, not my knee",
        extract={
            "pain": {"score": 8, "controlled_by_medication": False},
            "slot_values": {
                "pain_med_taken": {"value": True},
                "pain_location_expected": {"value": False},
            },
        },
    )
    _, conversation, outcomes = run([*PREAMBLE, atypical_pain], protocol)
    turn = outcomes[-1]

    assert turn.topic_id == "pain"
    assert turn.topic_changed, "the turn filled the last required slot, so the topic closed"
    assert turn.next_topic_id != "pain"
    assert "PAIN_ATYPICAL_SITE" in {finding.rule_id for finding in turn.findings}
    assert conversation.state.slot_values["pain_location_expected"].value is False


def test_a_topics_rule_does_not_fire_once_that_topic_has_gone_by(
    protocol: Protocol,
) -> None:
    """The mirror image: topic scope is real, not decorative.

    `PAIN_ATYPICAL_SITE` belongs to the pain topic alone. The same slot value seen
    while a later topic is active must not raise it — that is what the always-on
    `global_rules` list exists for, and a rule not on it is deliberately narrow.
    """
    pain = ScriptedTurn(
        say="A 2, taken my tablets, and it's right where they operated",
        extract={
            "pain": {"score": 2},
            "slot_values": {
                "pain_med_taken": {"value": True},
                "pain_location_expected": {"value": True},
            },
        },
    )
    # Re-asserting the same slot during the PONV topic. The value is in state, but
    # the rule that reads it is not in scope.
    later = ScriptedTurn(
        say="No sickness. Although the pain has moved, actually.",
        extract={
            "symptoms": [
                {"code": "nausea", "presence": "absent"},
                {"code": "vomiting", "presence": "absent"},
            ],
            "slot_values": {
                "tolerating_fluids": {"value": True},
                "pain_location_expected": {"value": False},
            },
        },
    )
    _, _, outcomes = run([*PREAMBLE, pain, later], protocol)

    assert outcomes[-1].topic_id == "ponv"
    assert "PAIN_ATYPICAL_SITE" not in {finding.rule_id for finding in outcomes[-1].findings}


def test_a_global_rule_fires_in_a_topic_that_declares_no_rules(
    protocol: Protocol,
) -> None:
    """The other half of scoping: `global_rules` outrank the topic list.

    The satisfaction topic is explicitly non-clinical and carries `rules: []`. A red
    flag volunteered there still has to land.
    """
    chest_pain = ScriptedTurn(
        say="Satisfied I suppose. My chest has been tight for an hour though.",
        extract={
            "symptoms": [
                {"code": "chest_pain", "presence": "present", "severity": "moderate"}
            ],
            "slot_values": {"satisfaction_response": {"value": 4}},
        },
    )
    pipeline = Pipeline(
        protocol=protocol,
        rules=RuleEngine.load(protocol.rules_version),
        turn_engine=ScriptedTurnEngine([chest_pain]),
    )
    conversation = pipeline.open(GA_CASE)
    # Jump the cursor to the satisfaction topic without pretending to answer 7 topics.
    conversation.state.cursor = conversation.state.topic_queue.index("satisfaction")
    outcome = pipeline.send(conversation, chest_pain.say)

    assert protocol.topic("satisfaction").rules == [], "premise: the topic has no rules"
    assert outcome.band == "red"
    assert "CHEST_PAIN" in {finding.rule_id for finding in outcome.findings}


# --- 2. A RED finding discards the drafted reply -----------------------------


def test_red_throws_the_draft_away_and_sends_clinician_copy(protocol: Protocol) -> None:
    draft = "Great, and has the swelling changed at all?"
    bleeding = ScriptedTurn(
        say="The dressing is soaked through, it's dripping",
        extract={
            "symptoms": [{"code": "dressing_soaked", "presence": "present"}],
        },
        reply=draft,
    )
    _, conversation, outcomes = run([*PREAMBLE, bleeding], protocol)
    turn = outcomes[-1]

    assert turn.band == "red"
    assert turn.reply.is_templated
    assert turn.reply.content == templates.TEMPLATES[turn.reply.template_id or ""].text
    assert draft not in [message.content for message in conversation.messages], (
        "the drafted reply must not reach the transcript at all"
    )


def test_every_red_finding_writes_one_escalation_row_per_route(
    protocol: Protocol,
) -> None:
    """A rule owing two owners writes two rows, per docs/data-model.md.

    Soaking through a dressing is `ED_NOW` *and* `CALL_SURGEON`: the patient has to go
    in, and the surgeon has to know. Collapsing that to the more urgent route is how a
    surgeon never gets paged.
    """
    bleeding = ScriptedTurn(
        say="The dressing is soaked through",
        extract={"symptoms": [{"code": "dressing_soaked", "presence": "present"}]},
    )
    _, conversation, outcomes = run([*PREAMBLE, bleeding], protocol)
    finding = next(
        item for item in outcomes[-1].findings if item.rule_id == "SURGICAL_BLEEDING"
    )

    assert len(finding.routes) == 2, "premise: this rule owes two owners"
    rows = [row for row in conversation.escalations if row.rule_id == "SURGICAL_BLEEDING"]
    assert {row.route for row in rows} == set(finding.routes)
    assert all(row.message_shown == conversation.messages[-1].content for row in rows)


# --- 3. A RED halt records the reason triage reads ---------------------------


def test_escalating_halts_with_the_literal_reason_tiering_looks_for(
    protocol: Protocol,
) -> None:
    """`tiering._abandoned` special-cases this exact string.

    If the halt reason drifted, an escalated conversation would also satisfy the
    abandonment clause — and while both are Tier 1, the *reason* on the tier would
    name the wrong thing, which is the one property a clinician judges the tier by.
    """
    chest_pain = ScriptedTurn(
        say="I've got a crushing pain in my chest",
        extract={
            "symptoms": [{"code": "chest_pain", "presence": "present", "severity": "severe"}]
        },
    )
    _, conversation, _ = run([*PREAMBLE, chest_pain], protocol)

    assert conversation.state.halted_reason == ESCALATED == "escalated"
    assert conversation.state.finished

    pipeline_summary = build_summary(conversation)
    assert pipeline_summary.tier is Tier.TIER_1
    assert "CHEST_PAIN" in assign_tier(
        conversation.findings, conversation.state
    ).reason, "the tier must name the rule, not the abandonment"


def test_escalating_stops_the_script_and_asks_nothing_more(protocol: Protocol) -> None:
    chest_pain = ScriptedTurn(
        say="Crushing chest pain",
        extract={"symptoms": [{"code": "chest_pain", "presence": "present"}]},
    )
    never_asked = ScriptedTurn(say="and I feel sick", extract={})
    pipeline, conversation, outcomes = run([*PREAMBLE, chest_pain, never_asked], protocol)

    assert len(outcomes) == 3, "the fourth turn is never taken"
    assert conversation.status is ConversationStatus.ESCALATED
    with pytest.raises(ConversationClosed):
        pipeline.send(conversation, never_asked.say)


# --- 4. The turn record is written whatever happens --------------------------


def test_a_validation_failure_still_writes_its_audit_row(protocol: Protocol) -> None:
    """Invariant 4. The row is the point, and a failure is when it matters most."""
    failed = ScriptedTurn(say="mmhmm", extract={}, hard_failure=True)
    _, conversation, _ = run([*PREAMBLE, failed], protocol)
    record = conversation.turns[-1]

    assert len(conversation.turns) == 3, "one row per patient turn, no gaps"
    assert record.extraction is None
    assert record.hard_failure
    assert conversation.validation_hard_failure


def test_a_failed_turn_still_consumes_a_protocol_turn(protocol: Protocol) -> None:
    """Otherwise a conversation that cannot parse anything never terminates.

    The stand-in extraction the pipeline synthesises fills nothing and carries zero
    confidence, so the topic runs out of turns exactly as it should.
    """
    failed = ScriptedTurn(say="mmhmm", extract={}, hard_failure=True)
    _, conversation, outcomes = run([*PREAMBLE, failed], protocol)

    assert conversation.state.total_turns == 3
    assert outcomes[-1].hard_failure
    assert "pain" in conversation.state.low_confidence_topics


def test_a_hard_failure_is_tier_1_on_its_own(protocol: Protocol) -> None:
    failed = ScriptedTurn(say="mmhmm", extract={}, hard_failure=True)
    _, conversation, _ = run([*PREAMBLE, failed], protocol)
    summary = build_summary(conversation)

    assert summary.findings == [], "nothing clinical fired"
    assert summary.tier is Tier.TIER_1, "the record is untrustworthy, which is enough"


# --- 5. GREEN reassures, and never reaches the queue -------------------------


def test_a_green_match_reassures_without_producing_a_finding(protocol: Protocol) -> None:
    sore_throat = ScriptedTurn(
        say="Throat's a bit scratchy, that's all",
        extract={
            "symptoms": [
                {"code": "sore_throat", "presence": "present", "severity": "mild"}
            ]
        },
    )
    pipeline = Pipeline(
        protocol=protocol,
        rules=RuleEngine.load(protocol.rules_version),
        turn_engine=ScriptedTurnEngine([sore_throat]),
    )
    conversation = pipeline.open(GA_CASE)
    conversation.state.cursor = conversation.state.topic_queue.index("anesthesia_recovery")
    outcome = pipeline.send(conversation, sore_throat.say)

    assert outcome.band == "none"
    assert outcome.findings == []
    assert outcome.reassurance, "an expected finding should offer approved wording"
    assert outcome.reassurance[0] in outcome.reply.content

    # A green match must leave no trace in the review queue. The tier is not asserted
    # here: this fixture jumps the cursor, so the conversation is truncated with a
    # symptom outstanding and is Tier 1 for that reason alone — which is correct, and
    # nothing to do with the green rule. `ga_knee_clean` covers the tier end to end.
    summary = build_summary(conversation)
    assert summary.findings == []
    assert summary.routes == [Route.ROUTINE]


def test_nothing_reassures_once_a_rule_has_fired(protocol: Protocol) -> None:
    """Conversational constraint 3, applied across turns rather than within one.

    A yellow finding earlier in the call does not stop the conversation, so a later
    green match would otherwise still emit "that's expected" — after the system has
    already decided something is not.
    """
    atypical = ScriptedTurn(
        say="A 3, taken my tablets, but it's in my calf",
        extract={
            "pain": {"score": 3},
            "slot_values": {
                "pain_med_taken": {"value": True},
                "pain_location_expected": {"value": False},
            },
        },
    )
    ponv = ScriptedTurn(
        say="No sickness, drinking fine",
        extract={
            "symptoms": [
                {"code": "nausea", "presence": "absent"},
                {"code": "vomiting", "presence": "absent"},
            ],
            "slot_values": {"tolerating_fluids": {"value": True}},
        },
    )
    cardioresp = ScriptedTurn(
        say="Breathing fine, no chest pain, no fainting",
        extract={
            "symptoms": [
                {"code": "shortness_of_breath", "presence": "absent"},
                {"code": "chest_pain", "presence": "absent"},
                {"code": "syncope", "presence": "absent"},
            ]
        },
    )
    sore_throat = ScriptedTurn(
        say="Throat's a bit scratchy",
        extract={
            "symptoms": [
                {"code": "sore_throat", "presence": "present", "severity": "mild"}
            ]
        },
    )
    _, conversation, outcomes = run(
        [*PREAMBLE, atypical, ponv, cardioresp, sore_throat], protocol
    )

    assert conversation.findings, "premise: something fired earlier"
    assert outcomes[-1].topic_id == "anesthesia_recovery"
    assert outcomes[-1].reassurance == []


# --- Reply composition -------------------------------------------------------


def test_finishing_a_topic_opens_the_next_one_rather_than_re_asking(
    protocol: Protocol,
) -> None:
    """The drafted reply was written before the cursor moved, so it is stale.

    A turn that satisfies its topic makes the model's follow-up a question the
    patient has already answered. The next topic's clinician-authored opening line is
    what the protocol designates for exactly this.
    """
    stale = "And how many doses have you had today?"
    pain = ScriptedTurn(
        say="A 2, taken my tablets, right where they operated",
        extract={
            "pain": {"score": 2},
            "slot_values": {
                "pain_med_taken": {"value": True},
                "pain_location_expected": {"value": True},
            },
        },
        reply=stale,
    )
    _, _, outcomes = run([*PREAMBLE, pain], protocol)
    turn = outcomes[-1]

    assert turn.topic_changed
    assert turn.reply.content != stale
    assert turn.reply.content == " ".join(
        protocol.topic(turn.next_topic_id or "").opening_question.split()
    )


def test_the_reply_is_kept_while_the_topic_is_still_open(protocol: Protocol) -> None:
    draft = "How many out of ten would you say?"
    partial = ScriptedTurn(
        say="It's sore",
        extract={"slot_values": {"pain_med_taken": {"value": True}}},
        reply=draft,
    )
    _, _, outcomes = run([*PREAMBLE, partial], protocol)

    assert outcomes[-1].topic_id == "pain"
    assert not outcomes[-1].topic_changed
    assert outcomes[-1].reply.content == draft


# --- Case branching ----------------------------------------------------------


def test_the_topic_queue_branches_on_the_case(protocol: Protocol) -> None:
    """A block case gets a topic a general-anesthetic case never sees."""
    pipeline = Pipeline(
        protocol=protocol,
        rules=RuleEngine.load(protocol.rules_version),
        turn_engine=ScriptedTurnEngine([]),
    )
    assert "block_regression" not in pipeline.open(GA_CASE).state.topic_queue
    assert "block_regression" in pipeline.open(BLOCK_CASE).state.topic_queue


# --- Record shape ------------------------------------------------------------


def test_the_transcript_alternates_and_opens_with_the_protocol(protocol: Protocol) -> None:
    _, conversation, _ = run(list(PREAMBLE), protocol)
    roles = [message.role for message in conversation.messages]

    assert roles == ["assistant", "patient", "assistant", "patient", "assistant"]
    assert conversation.messages[0].content == " ".join(
        protocol.topic(conversation.state.topic_queue[0]).opening_question.split()
    )


def test_proxy_detection_is_sticky_across_the_conversation(protocol: Protocol) -> None:
    """A proxy who hands the phone back does not make the earlier answers first-hand."""
    proxy = ScriptedTurn(
        say="I'm her husband, she's asleep",
        extract={
            "proxy_detected": True,
            "slot_values": {
                "identity_confirmed": {"value": True},
                "consent_to_continue": {"value": True},
            },
        },
    )
    _, conversation, _ = run([proxy, OPEN_CHECKIN], protocol)

    assert conversation.proxy_reported
    assert conversation.state.slot_values["is_proxy"].value is True, "backfilled from maps_to"


def test_an_unresolved_symptom_is_read_off_the_extractions(protocol: Protocol) -> None:
    symptomatic = ScriptedTurn(
        say="My eye's been gritty",
        extract={"symptoms": [{"code": "eye_irritation", "presence": "present"}]},
    )
    _, conversation, _ = run([IDENTITY, symptomatic], protocol)

    assert conversation.unresolved_symptom
    observation = conversation.extractions[-1].symptom(SymptomCode.EYE_IRRITATION)
    assert observation.presence is Presence.PRESENT
    assert observation.severity is None or observation.severity is not Severity.NONE
