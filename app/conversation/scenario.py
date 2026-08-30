"""Replaying an authored conversation, and checking what it did.

A scenario is a whole check-in written down: the case, what the patient says on each
turn, what each of those messages *means* as a `TurnExtraction`, and what the
deterministic layers are expected to do about it. Replaying one exercises the entire
pipeline with no model in the loop, which is what makes an assertion about a tier or a
route a statement about this code rather than about a paraphrase.

The file format shares its `case:` and `assertions:` blocks with the LLM-driven
scenarios in `docs/evals.md`, and carries a `mode:` discriminator so both kinds can
live in one directory and be picked up by one runner. `mode: scripted` supplies
`turns:`; the simulated-patient mode will supply `persona:` instead and change nothing
else.

Authoring an extraction by hand is the point, not a shortcut. It fixes what the model
saw, so a failure can only mean the protocol engine, the rules, the gate, the tiering
or the summary did something different — never that the extraction drifted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.conversation.pipeline import Pipeline, TurnOutcome
from app.conversation.session import Conversation
from app.domain.enums import AnesthesiaType, BlockType, ConversationStatus, Route, Tier
from app.domain.schemas import Summary
from app.llm.fake import ScriptedTurn, ScriptedTurnEngine
from app.protocol.engine import CaseFacts
from app.protocol.loader import Protocol, load_default_protocol
from app.safety.rules import RuleEngine

SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "evals" / "scenarios"

_TIERS: dict[int, Tier] = {1: Tier.TIER_1, 2: Tier.TIER_2, 3: Tier.TIER_3}


class ScenarioError(ValueError):
    """A scenario file that cannot be run as written. Raised at load."""


@dataclass(frozen=True)
class Assertions:
    """What a scenario claims the deterministic layers will do.

    `must_not_trigger` matters as much as `must_trigger`: a rule set that escalates
    everything scores perfect recall, so the benign scenarios are the only thing
    keeping the precision honest.
    """

    must_trigger: tuple[str, ...] = ()
    must_not_trigger: tuple[str, ...] = ()
    must_reach_tier: Tier | None = None
    must_route: tuple[Route, ...] = ()
    must_reach_status: ConversationStatus | None = None
    max_turns_to_detection: int | None = None
    must_be_templated: bool | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Assertions:
        unknown = set(raw) - {
            "must_trigger",
            "must_not_trigger",
            "must_reach_tier",
            "must_route",
            "must_reach_status",
            "max_turns_to_detection",
            "must_be_templated",
        }
        if unknown:
            raise ScenarioError(f"unknown assertion(s) {sorted(unknown)}")
        tier = raw.get("must_reach_tier")
        if tier is not None and tier not in _TIERS:
            raise ScenarioError(f"must_reach_tier must be 1, 2 or 3; got {tier!r}")
        status = raw.get("must_reach_status")
        return cls(
            must_trigger=tuple(raw.get("must_trigger", [])),
            must_not_trigger=tuple(raw.get("must_not_trigger", [])),
            must_reach_tier=None if tier is None else _TIERS[tier],
            must_route=tuple(Route(value) for value in raw.get("must_route", [])),
            must_reach_status=None if status is None else ConversationStatus(status),
            max_turns_to_detection=raw.get("max_turns_to_detection"),
            must_be_templated=raw.get("must_be_templated"),
        )


@dataclass(frozen=True)
class Scenario:
    """One authored check-in, ready to replay."""

    scenario_id: str
    case: CaseFacts
    turns: tuple[ScriptedTurn, ...]
    assertions: Assertions = field(default_factory=Assertions)
    description: str | None = None
    path: Path | None = None


def load_scenario(path: Path) -> Scenario:
    """Load and validate one scenario file."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ScenarioError(f"{path} does not contain a scenario mapping")
    mode = raw.get("mode", "scripted")
    if mode != "scripted":
        raise ScenarioError(
            f"{path} declares mode {mode!r}; only 'scripted' runs without an LLM"
        )
    if not raw.get("turns"):
        raise ScenarioError(f"{path} has no turns, so there is nothing to replay")

    return Scenario(
        scenario_id=raw.get("scenario_id") or path.stem,
        case=_case(raw.get("case") or {}, path),
        turns=tuple(_turn(entry, path) for entry in raw["turns"]),
        assertions=Assertions.parse(raw.get("assertions") or {}),
        description=raw.get("description"),
        path=path,
    )


def load_scenarios(directory: Path = SCENARIOS_DIR) -> list[Scenario]:
    """Every scenario in `directory`, by filename."""
    return [load_scenario(path) for path in sorted(directory.glob("*.yaml"))]


def _case(raw: dict[str, Any], path: Path) -> CaseFacts:
    known = {
        "anesthesia_type",
        "block_type",
        "procedure",
        "procedure_category",
        "hours_post_op",
        "expected_block_duration_hours",
        "block_adjuvant",
    }
    unknown = set(raw) - known
    if unknown:
        raise ScenarioError(f"{path}: unknown case field(s) {sorted(unknown)}")
    if "anesthesia_type" not in raw:
        raise ScenarioError(f"{path}: case must state an anesthesia_type")
    block = raw.get("block_type")
    return CaseFacts(
        anesthesia_type=AnesthesiaType(raw["anesthesia_type"]),
        block_type=None if block is None else BlockType(block),
        procedure=raw.get("procedure"),
        procedure_category=raw.get("procedure_category"),
        hours_post_op=raw.get("hours_post_op"),
        expected_block_duration_hours=raw.get("expected_block_duration_hours"),
        block_adjuvant=raw.get("block_adjuvant"),
    )


def _turn(raw: dict[str, Any], path: Path) -> ScriptedTurn:
    unknown = set(raw) - {"say", "extract", "hard_failure", "reply"}
    if unknown:
        raise ScenarioError(f"{path}: unknown turn field(s) {sorted(unknown)}")
    if "say" not in raw:
        raise ScenarioError(f"{path}: every turn needs a `say`")
    return ScriptedTurn(
        say=raw["say"],
        extract=raw.get("extract") or {},
        hard_failure=bool(raw.get("hard_failure", False)),
        reply=raw.get("reply"),
    )


@dataclass
class ScenarioRun:
    """The result of replaying one scenario."""

    scenario: Scenario
    conversation: Conversation
    outcomes: list[TurnOutcome]
    summary: Summary
    script_exhausted: bool = False

    @property
    def fired(self) -> set[str]:
        return {finding.rule_id for finding in self.conversation.findings}

    def turn_of(self, rule_id: str) -> int | None:
        """1-based turn on which `rule_id` first fired, or None."""
        for index, outcome in enumerate(self.outcomes, start=1):
            if any(finding.rule_id == rule_id for finding in outcome.findings):
                return index
        return None

    @property
    def failures(self) -> list[str]:
        """Every assertion this run broke, in plain English. Empty means it passed."""
        expected = self.scenario.assertions
        problems: list[str] = []

        missing = [rule for rule in expected.must_trigger if rule not in self.fired]
        if missing:
            problems.append(f"expected to fire but did not: {', '.join(sorted(missing))}")

        fired_anyway = [rule for rule in expected.must_not_trigger if rule in self.fired]
        if fired_anyway:
            problems.append(f"fired but should not have: {', '.join(sorted(fired_anyway))}")

        if expected.must_reach_tier and self.summary.tier is not expected.must_reach_tier:
            problems.append(
                f"tier was {self.summary.tier.value}, expected {expected.must_reach_tier.value} "
                f"({self.summary.headline})"
            )

        for route in expected.must_route:
            if route not in self.summary.routes:
                problems.append(
                    f"route {route.value} missing; got "
                    f"{[value.value for value in self.summary.routes]}"
                )

        if expected.must_reach_status and self.conversation.status is not expected.must_reach_status:
            problems.append(
                f"status was {self.conversation.status.value}, "
                f"expected {expected.must_reach_status.value}"
            )

        limit = expected.max_turns_to_detection
        if limit is not None:
            for rule in expected.must_trigger:
                turn = self.turn_of(rule)
                if turn is not None and turn > limit:
                    problems.append(f"{rule} fired on turn {turn}, limit was {limit}")

        if expected.must_be_templated is not None:
            templated = self.conversation.messages[-1].is_templated
            if templated is not expected.must_be_templated:
                problems.append(
                    f"final reply is_templated={templated}, "
                    f"expected {expected.must_be_templated}"
                )

        # Not a declared assertion, but a scenario that stopped because it ran out of
        # authored turns proves less than it looks like it proves: the protocol was
        # still mid-topic, and the tier it reached is the tier of a truncated call.
        if self.script_exhausted and not self.conversation.state.finished:
            problems.append(
                "script ran out before the conversation ended; the scenario covers "
                "less than it claims to"
            )
        return problems


def run_scenario(scenario: Scenario, protocol: Protocol | None = None) -> ScenarioRun:
    """Replay a scenario end to end and close it out.

    Stops early when the protocol stops — a RED escalation halts the script, and any
    remaining authored turns are simply never said, which is the correct behaviour
    rather than a truncated run.
    """
    loaded = protocol or load_default_protocol()
    pipeline = Pipeline(
        protocol=loaded,
        rules=RuleEngine.load(loaded.rules_version),
        turn_engine=ScriptedTurnEngine(list(scenario.turns)),
    )
    conversation = pipeline.open(scenario.case)

    outcomes: list[TurnOutcome] = []
    for turn in scenario.turns:
        if conversation.state.finished:
            break
        outcomes.append(pipeline.send(conversation, turn.say))

    summary = pipeline.close(conversation)
    return ScenarioRun(
        scenario=scenario,
        conversation=conversation,
        outcomes=outcomes,
        summary=summary,
        script_exhausted=len(outcomes) == len(scenario.turns),
    )
