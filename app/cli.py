"""Drive a check-in from a terminal, with or without an LLM.

Ways in, for different questions:

    uv run python -m app.cli --list
    uv run python -m app.cli --scenario pdph_spinal   # replay an authored scenario
    uv run python -m app.cli --all                    # replay every scenario
    uv run python -m app.cli --case spinal            # type answers yourself
    uv run python -m app.cli --case spinal --live     # ...against the real model

Replaying answers "does the deterministic pipeline do the right thing with a known
extraction?" and is the same code path `tests/test_flows.py` asserts against. The REPL
answers "what does this feel like, and where does the script go if I say *this*?" — by
default it runs on `KeywordTurnEngine`, a credulous keyword matcher, so read its
extractions as a demonstration of the pipeline rather than of comprehension.

`--live` swaps in the real Anthropic engine, which needs a key and costs money. It is
opt-in for both reasons, and because the difference between a real extraction and a
keyword one must always be visible in the trace rather than inferred — every turn
prints the model that produced it.

Every turn prints what each layer decided, in the order the pipeline decided it: the
extraction, the rules that fired and in which band, what the gate did to the drafted
reply, and where the protocol went next. That trace is the point — a tier at the end
with no visible derivation is exactly what this system is designed not to produce.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.conversation.pipeline import Pipeline, TurnOutcome
from app.conversation.scenario import (
    SCENARIOS_DIR,
    ScenarioRun,
    load_scenario,
    load_scenarios,
    run_scenario,
)
from app.conversation.session import Conversation
from app.config import settings
from app.domain.enums import AnesthesiaType, BlockType
from app.llm.client import MissingAPIKey
from app.llm.fake import KeywordTurnEngine
from app.llm.turn import TurnEngine
from app.protocol import engine
from app.protocol.engine import CaseFacts
from app.summary.generator import tier_for

# Demo cases for the REPL, one per branch the protocol takes. Synthetic, per invariant
# 5 — no real patient data enters this system.
DEMO_CASES: dict[str, CaseFacts] = {
    "general": CaseFacts(
        anesthesia_type=AnesthesiaType.GENERAL,
        procedure="Right knee arthroscopy",
        procedure_category="orthopedic",
        hours_post_op=20,
    ),
    "block": CaseFacts(
        anesthesia_type=AnesthesiaType.GENERAL,
        block_type=BlockType.INTERSCALENE,
        procedure="Right rotator cuff repair",
        procedure_category="orthopedic",
        hours_post_op=30,
    ),
    "spinal": CaseFacts(
        anesthesia_type=AnesthesiaType.SPINAL,
        block_type=BlockType.SPINAL,
        procedure="Cesarean section",
        procedure_category="obstetric",
        hours_post_op=26,
    ),
    "local": CaseFacts(
        anesthesia_type=AnesthesiaType.LOCAL,
        procedure="Skin lesion excision",
        procedure_category="dermatology",
        hours_post_op=6,
    ),
}

BANDS = {"red": "RED  ", "yellow": "YELLOW", "none": "ok   "}


# --- Rendering --------------------------------------------------------------

# Whose words the patient actually saw. Worth printing every turn: "generated" and
# "authored by a clinician" carry very different liability, and a transcript that
# does not distinguish them cannot be reviewed.
REPLY_SOURCES: dict[str, str] = {
    "draft": "engine's drafted reply, kept",
    "transition": "engine's transition into the next topic, kept",
    "protocol": "protocol YAML — the authored opening question for the topic",
    "template": "safety/templates.py — fixed clinician-authored escalation copy",
    "closing": "pipeline constant — the check-in is over",
}


def show_turn(index: int, outcome: TurnOutcome, text: str) -> None:
    """One turn, layer by layer, in pipeline order.

    Reads only the outcome, never the live conversation: a replay renders after the
    run has finished, and reaching into the conversation would paint the final cursor
    and the final tier onto every turn.
    """
    print(f"\n[{index}] topic={outcome.topic_id}")
    print(f"  patient  {text!r}")

    if outcome.hard_failure:
        print("  extract  <validation hard-failure — turn recorded, nothing extracted>")
    else:
        print(f"  extract  {_describe(outcome)}")

    if outcome.findings:
        for finding in outcome.findings:
            routes = "/".join(route.value for route in finding.routes)
            print(f"  rules    {finding.rule_id}  {finding.tier.value}  {routes}  — {finding.label}")
    else:
        print("  rules    none fired")

    gate = {
        "red": "RED — drafted reply discarded, templated copy sent, script halted",
        "yellow": "yellow — finding recorded, conversation continues",
        "none": "clear — drafted reply kept",
    }[outcome.band]
    print(f"  gate     {gate}")
    if outcome.reassurance:
        print(f"  green    {len(outcome.reassurance)} approved reassurance line(s) added")

    print(f"  betsy    {outcome.reply.content}")
    print(f"  words    {REPLY_SOURCES[outcome.reply_source]}")
    print(
        f"  state    cursor={outcome.cursor}/{outcome.topic_count}"
        f"  next={outcome.next_topic_id or '—'}"
        f"  slots={outcome.slots_total}"
        f"  findings={outcome.findings_total}"
        f"  tier-so-far={outcome.tier_so_far.value}"
    )


def _describe(outcome: TurnOutcome) -> str:
    extraction = outcome.extraction
    if extraction is None:
        return "—"
    parts: list[str] = []
    if extraction.pain and extraction.pain.score is not None:
        parts.append(f"pain={extraction.pain.score}")
    for observation in extraction.symptoms:
        parts.append(f"{observation.code.value}:{observation.presence.value}")
    if extraction.temperature_f is not None:
        parts.append(f"temp={extraction.temperature_f}")
    if extraction.patient_question:
        parts.append(f"question={extraction.patient_question!r}")
    filled = f" | filled {', '.join(outcome.slots_filled)}" if outcome.slots_filled else ""
    return (", ".join(parts) or "nothing clinical") + f" (conf {extraction.extraction_confidence})" + filled


def show_summary(run_or_conversation: ScenarioRun | Conversation, pipeline: Pipeline | None = None) -> None:
    """The clinician-facing record, plus the sentence that accounts for the tier."""
    if isinstance(run_or_conversation, ScenarioRun):
        conversation, summary = run_or_conversation.conversation, run_or_conversation.summary
    else:
        assert pipeline is not None
        conversation = run_or_conversation
        summary = pipeline.close(conversation)

    decision = tier_for(conversation)
    print("\n" + "─" * 78)
    print(f"  status    {conversation.status.value}")
    print(f"  tier      {summary.tier.value} — {decision.reason}")
    print(f"  routes    {', '.join(route.value for route in summary.routes)}")
    print(f"  headline  {summary.headline}")
    print(f"  turns     {summary.turn_count}   pain max {summary.max_pain_score}   trend {summary.pain_trend.value}")
    if summary.findings:
        print("  findings")
        for finding in summary.findings:
            print(f"    - {finding.rule_id} ({finding.tier.value}) {finding.label}")
            print(f"        evidence: {'; '.join(finding.evidence)}")
    if conversation.escalations:
        print("  escalations")
        for escalation in conversation.escalations:
            print(f"    - {escalation.rule_id} -> {escalation.route.value} ({escalation.template_id})")
    topics = ", ".join(
        f"{outcome.topic_id}:{outcome.exit_reason}" for outcome in conversation.state.completed
    )
    print(f"  topics    {topics or '—'}")
    if conversation.state.halted_reason:
        print(f"  halted    {conversation.state.halted_reason}")
    print("─" * 78)


# --- Modes ------------------------------------------------------------------


def replay(scenario_id: str) -> int:
    path = SCENARIOS_DIR / f"{scenario_id}.yaml"
    if not path.exists():
        print(f"no scenario {scenario_id!r} in {SCENARIOS_DIR}", file=sys.stderr)
        return 2
    scenario = load_scenario(path)
    print(f"── {scenario.scenario_id} " + "─" * (74 - len(scenario.scenario_id)))
    if scenario.description:
        print(f"   {scenario.description}")
    run = run_scenario(scenario)
    print(f"\n  betsy    {run.conversation.messages[0].content}")
    for index, (turn, outcome) in enumerate(zip(scenario.turns, run.outcomes), start=1):
        show_turn(index, outcome, turn.say)

    unsaid = len(scenario.turns) - len(run.outcomes)
    if unsaid:
        print(f"\n  ({unsaid} authored turn(s) never said — the script stopped first)")
    show_summary(run)

    if run.failures:
        print("\n  ASSERTION FAILURES")
        for failure in run.failures:
            print(f"    - {failure}")
        return 1
    print("\n  all assertions passed")
    return 0


def replay_all() -> int:
    scenarios = load_scenarios()
    if not scenarios:
        print(f"no scenarios in {SCENARIOS_DIR}", file=sys.stderr)
        return 2
    failed = 0
    print(f"{'':6}{'scenario':26}{'tier':8}{'status':12}rules")
    for scenario in scenarios:
        run = run_scenario(scenario)
        ok = not run.failures
        failed += not ok
        print(
            f"{'PASS  ' if ok else 'FAIL  '}"
            f"{scenario.scenario_id:26}{run.summary.tier.value:8}"
            f"{run.conversation.status.value:12}{', '.join(sorted(run.fired)) or '—'}"
        )
        for failure in run.failures:
            print(f"        {failure}")
    print(f"\n{len(scenarios) - failed}/{len(scenarios)} passed")
    return 1 if failed else 0


def interactive(case_name: str, *, live: bool = False) -> int:
    case = DEMO_CASES.get(case_name)
    if case is None:
        print(f"unknown case {case_name!r}; try {', '.join(DEMO_CASES)}", file=sys.stderr)
        return 2

    try:
        turn_engine = _live_engine() if live else KeywordTurnEngine()
    except MissingAPIKey as error:
        print(error, file=sys.stderr)
        return 2

    pipeline = Pipeline.default(turn_engine)
    conversation = pipeline.open(case)
    print(f"engine    {'anthropic ' + settings.turn_model if live else 'keyword double'}")
    queue = ", ".join(conversation.state.topic_queue)
    print(f"case      {case.anesthesia_type.value}"
          f"{'/' + case.block_type.value if case.block_type else ''}"
          f" — {case.procedure}, {case.hours_post_op}h post-op")
    print(f"topics    {queue}")
    print("commands  :state  :findings  :transcript  :summary  :quit")
    print(f"\n  betsy    {conversation.messages[0].content}")

    index = 0
    while not conversation.state.finished:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.startswith(":"):
            if _command(text, conversation, pipeline):
                break
            continue
        index += 1
        show_turn(index, pipeline.send(conversation, text), text)

    show_summary(conversation, pipeline)
    return 0


def _live_engine() -> TurnEngine:
    """Import the real engine only when asked for it.

    Deferred so the no-key paths — every scenario replay, every test, the keyword
    REPL — stay runnable and importable on a machine that has never been configured
    with a credential.
    """
    from app.llm.anthropic_engine import AnthropicTurnEngine

    return AnthropicTurnEngine.default()


def _command(text: str, conversation: Conversation, pipeline: Pipeline) -> bool:
    """Run a REPL command. Returns True to quit."""
    command = text[1:].strip()
    if command in ("q", "quit", "exit"):
        return True
    if command == "state":
        state = conversation.state
        topic = engine.active_topic(pipeline.protocol, state)
        print(f"  active   {topic.id if topic else '—'} (turn {state.turns_in_topic} of "
              f"{topic.max_turns if topic else 0}), total {state.total_turns}")
        print(f"  queue    {' > '.join(state.topic_queue)}")
        for slot_id, answer in state.slot_values.items():
            print(f"    {slot_id:32} = {answer.value!r} (conf {answer.confidence})")
        if state.unanswered_questions:
            print(f"  unanswered {state.unanswered_questions}")
        if state.low_confidence_topics:
            print(f"  low conf   {state.low_confidence_topics}")
    elif command == "findings":
        for finding in conversation.findings or []:
            print(f"    {finding.rule_id} ({finding.tier.value}) {finding.label}")
        if not conversation.findings:
            print("    none")
    elif command == "transcript":
        for message in conversation.messages:
            marker = " [templated]" if message.is_templated else ""
            print(f"    {message.seq:>3} {message.role:9} {message.content}{marker}")
    elif command == "summary":
        show_summary(conversation, pipeline)
    else:
        print(f"    unknown command {command!r}")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="app.cli", description="Run a Betsy check-in with no LLM."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--scenario", metavar="ID", help="replay one authored scenario")
    group.add_argument("--all", action="store_true", help="replay every scenario")
    group.add_argument("--case", metavar="NAME", help=f"REPL on a demo case: {', '.join(DEMO_CASES)}")
    group.add_argument("--list", action="store_true", help="list scenarios and demo cases")
    parser.add_argument(
        "--live",
        action="store_true",
        help="drive --case with the real Anthropic engine (needs a key, costs money)",
    )
    args = parser.parse_args(argv)

    if args.live and not args.case:
        parser.error("--live applies to --case; scenario replay is deterministic by design")
    if args.scenario:
        return replay(args.scenario)
    if args.all:
        return replay_all()
    if args.case:
        return interactive(args.case, live=args.live)

    print("scenarios (--scenario ID)")
    for path in sorted(Path(SCENARIOS_DIR).glob("*.yaml")):
        scenario = load_scenario(path)
        print(f"  {scenario.scenario_id:26} {scenario.description or ''}")
    print("\ndemo cases (--case NAME)")
    for name, case in DEMO_CASES.items():
        block = f" + {case.block_type.value}" if case.block_type else ""
        print(f"  {name:26} {case.anesthesia_type.value}{block} — {case.procedure}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
