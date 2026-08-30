"""Whole conversations, start to finish, asserted against their own scenario files.

`tests/test_pipeline.py` proves the individual ordering properties. These prove that
a complete check-in ends up where a clinician would expect: the right tier, the right
disposition, the right terminal status, and the right rules fired — or, just as
importantly, not fired.

The assertions live in `evals/scenarios/*.yaml` beside the transcript they belong to,
not here. That is deliberate: a scenario is a clinical statement about what should
happen to a patient who says these things, and it should be reviewable by someone who
does not read Python. This file only makes them run.

These are also the seed of the eval corpus in `docs/evals.md`. Nothing here needs an
API key, so they run on every commit rather than on demand.
"""

from __future__ import annotations

import pytest

from app.conversation.scenario import Scenario, load_scenarios, run_scenario
from app.domain.enums import ConversationStatus, RuleBand, Tier
from app.safety import templates
from app.safety.rules import load_rules

SCENARIOS = load_scenarios()


def test_there_are_scenarios_to_run() -> None:
    """A parametrized suite over an empty list passes silently. This is the guard."""
    assert SCENARIOS, "no scenario files found; the flow suite would pass vacuously"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_scenario(scenario: Scenario) -> None:
    run = run_scenario(scenario)
    assert not run.failures, "\n".join(
        [f"{scenario.scenario_id}:", *(f"  - {failure}" for failure in run.failures)]
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_every_turn_leaves_an_audit_row(scenario: Scenario) -> None:
    """Invariant 4, checked over every conversation rather than one contrived turn."""
    run = run_scenario(scenario)
    patient_messages = [
        message for message in run.conversation.messages if message.role == "patient"
    ]
    assert len(run.conversation.turns) == len(patient_messages)
    assert [turn.message_seq for turn in run.conversation.turns] == [
        message.seq for message in patient_messages
    ]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_an_escalated_conversation_ends_on_clinician_copy(scenario: Scenario) -> None:
    """Whenever the gate escalates, the last thing said is fixed, attributable text."""
    run = run_scenario(scenario)
    if run.conversation.status is not ConversationStatus.ESCALATED:
        pytest.skip("this scenario does not escalate")

    last = run.conversation.messages[-1]
    assert last.is_templated
    assert last.template_id is not None
    assert last.content == templates.TEMPLATES[last.template_id].text
    assert run.conversation.escalations, "an escalation must be recorded, not just sent"
    assert run.summary.tier is Tier.TIER_1


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_only_a_red_finding_stops_the_script(scenario: Scenario) -> None:
    """A yellow finding records and continues; that is the whole band distinction."""
    run = run_scenario(scenario)
    for index, outcome in enumerate(run.outcomes):
        stopped = index == len(run.outcomes) - 1 and run.conversation.state.finished
        if outcome.band == "yellow":
            assert outcome.findings
            assert not outcome.reply.is_templated
        if outcome.band == "red":
            assert stopped, "a red finding must be the last turn of the conversation"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_a_green_match_never_reaches_the_review_queue(scenario: Scenario) -> None:
    """GREEN supplies wording and nothing else. No finding, no route, no tier."""
    run = run_scenario(scenario)
    green_ids = {
        rule.id for rule in load_rules().rules if rule.band is RuleBand.GREEN
    }
    assert not (green_ids & run.fired)


def test_the_corpus_covers_each_tier() -> None:
    """A suite that only ever reaches one tier is not testing the triage boundary."""
    reached = {run_scenario(scenario).summary.tier for scenario in SCENARIOS}
    assert reached == {Tier.TIER_1, Tier.TIER_2, Tier.TIER_3}


def test_the_corpus_covers_each_terminal_status() -> None:
    reached = {run_scenario(scenario).conversation.status for scenario in SCENARIOS}
    assert {
        ConversationStatus.COMPLETED,
        ConversationStatus.ESCALATED,
        ConversationStatus.ABANDONED,
    } <= reached


def test_every_green_rule_is_attached_to_a_topic() -> None:
    """A GREEN rule reachable from no topic is dead copy.

    Rules are evaluated as `global_rules` plus the active topic's list, so a rule
    named by neither can never match. This was true of every GREEN rule until the
    end-to-end flow was built and the reassurance band turned out to be unreachable —
    a gap no unit test could see, because the unit tests pass `topic_rules` by hand.
    """
    rule_set = load_rules()
    from app.protocol.loader import load_default_protocol

    attached = set(rule_set.global_rules)
    for topic in load_default_protocol().topics:
        attached |= set(topic.rules)

    orphaned = sorted(
        rule.id
        for rule in rule_set.rules
        if rule.band is RuleBand.GREEN and rule.id not in attached
    )
    assert not orphaned, f"green rules attached to no topic, so they can never fire: {orphaned}"
