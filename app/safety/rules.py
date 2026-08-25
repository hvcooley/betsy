"""Deterministic red-flag rules.

This is where the product's actual value lives. Every escalation decision is made
here, by evaluating a closed vocabulary of predicates against the validated
structured fields of one turn — never against the patient's free text, and never by
the model. The LLM's contribution is upstream: it turns a sentence into a
`TurnExtraction`. What that extraction *means* is decided by this file and the YAML
it loads.

Two properties matter more than anything else here, and both are tested directly:

**Rules fail closed.** Silence is not a denial. A symptom nobody asked about reads as
UNKNOWN, and UNKNOWN never satisfies `presence: absent`. `not` over a value that is
missing evaluates FALSE rather than true, so a negated condition cannot fire off an
empty extraction — otherwise "no shortness of breath" would be true of a patient who
was never asked.

**Rules are evaluated in two scopes.** A topic's own rules fire while that topic is
active, which is right for things the agent went looking for. The `global_rules` in
the YAML fire on every turn regardless, because a patient who volunteers chest pain
during the satisfaction survey cannot wait for a topic that has already gone by.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import (
    BlockType,
    MedAdherence,
    Presence,
    Route,
    RuleBand,
    Severity,
    SymptomCode,
    Tier,
)
from app.domain.schemas import Finding, TurnExtraction

if TYPE_CHECKING:
    from app.protocol.engine import CaseFacts, ProtocolState

RULES_DIR = Path(__file__).parent / "rules"


class RuleError(ValueError):
    """A rule definition that cannot be trusted to fire correctly. Raised at load."""


class BlockWindow(BaseModel):
    """How long a block is expected to last, and when a lingering one is a problem."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    typical_hours: tuple[float, float]
    flag_after_hours: float
    notes: str | None = None


class Condition(BaseModel):
    """One node of a rule's condition tree.

    Every predicate is a declared field rather than an expression to evaluate, so the
    loader can reject a symptom code or severity that does not exist before the rule
    ever runs. A node carries exactly one predicate or one combinator.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    # Combinators
    all_: list[Condition] | None = Field(default=None, alias="all")
    any_: list[Condition] | None = Field(default=None, alias="any")
    not_: Condition | None = Field(default=None, alias="not")

    # Symptom predicate
    symptom: SymptomCode | None = None
    presence: Presence | None = None
    min_severity: Severity | None = None
    onset_hours_within: float | None = None

    # Other predicates
    pain: dict[str, Any] | None = None
    medication: dict[str, Any] | None = None
    case: dict[str, Any] | None = None
    slot: str | None = None
    equals: Any = None
    in_: list[Any] | None = Field(default=None, alias="in")
    gte: float | None = None
    lte: float | None = None
    is_true: bool | None = None
    is_false: bool | None = None
    temperature_f_gte: float | None = None
    block_regression_exceeded: bool | None = None
    patient_distress: bool | None = None

    @model_validator(mode="after")
    def check_exactly_one_predicate(self) -> Self:
        forms = [
            self.all_ is not None,
            self.any_ is not None,
            self.not_ is not None,
            self.symptom is not None,
            self.pain is not None,
            self.medication is not None,
            self.case is not None,
            self.slot is not None,
            self.temperature_f_gte is not None,
            self.block_regression_exceeded is not None,
            self.patient_distress is not None,
        ]
        if sum(forms) != 1:
            raise RuleError("a condition must carry exactly one predicate or combinator")
        if self.symptom is not None and self.presence is None:
            raise RuleError(
                f"symptom predicate on {self.symptom.value} must state a `presence`; "
                "leaving it implicit is how a rule ends up treating silence as a denial"
            )
        for key in self.pain or {}:
            if key not in {"score_gte", "score_lte", "controlled_by_medication", "trend"}:
                raise RuleError(f"unknown pain predicate {key!r}")
        for key in self.medication or {}:
            if key not in {"name_any", "adherence", "last_dose_hours_lt"}:
                raise RuleError(f"unknown medication predicate {key!r}")
        for key in self.case or {}:
            if key not in {
                "anesthesia_type_in",
                "block_type_in",
                "has_block",
                "hours_post_op_gte",
                "hours_post_op_lt",
            }:
                raise RuleError(f"unknown case predicate {key!r}")
        return self


class Rule(BaseModel):
    """One clinician-reviewable safety rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    band: RuleBand
    severity: Severity
    tier: Tier
    routes: list[Route] = Field(min_length=1)
    label: str
    template_key: str | None = None
    sme_reviewed: bool = False
    when: Condition

    @model_validator(mode="after")
    def check_tier_matches_band(self) -> Self:
        """A red rule carrying tier_2 is a contradiction, not a nuance."""
        if self.tier is not self.band.implied_tier:
            raise RuleError(
                f"rule {self.id} is band {self.band.value} but declares {self.tier.value}; "
                f"band {self.band.value} implies {self.band.implied_tier.value}"
            )
        if self.band is RuleBand.RED and not self.template_key:
            raise RuleError(
                f"rule {self.id} is RED but names no template; a red flag discards the "
                "drafted reply, so it must have fixed copy to send instead"
            )
        return self


class RuleSet(BaseModel):
    """A whole versioned rule file."""

    model_config = ConfigDict(extra="forbid")

    version: int
    rules_version: str
    block_regression: dict[BlockType, BlockWindow]
    block_adjuvants: dict[str, float] = Field(default_factory=dict)
    global_rules: list[str] = Field(default_factory=list)
    rules: list[Rule]

    @model_validator(mode="after")
    def check_references(self) -> Self:
        ids = [rule.id for rule in self.rules]
        if len(set(ids)) != len(ids):
            raise RuleError("duplicate rule id")
        unknown = [rule_id for rule_id in self.global_rules if rule_id not in set(ids)]
        if unknown:
            raise RuleError(f"global_rules references undefined rules {sorted(unknown)}")
        missing = set(BlockType) - set(self.block_regression)
        if missing:
            raise RuleError(
                f"block types with no regression window: {sorted(b.value for b in missing)}; "
                "a block without a window would let a prolonged block pass unflagged"
            )
        return self

    @property
    def by_id(self) -> dict[str, Rule]:
        return {rule.id: rule for rule in self.rules}

    def window_for(self, block_type: BlockType | None) -> BlockWindow | None:
        return None if block_type is None else self.block_regression.get(block_type)


def load_rules(rules_version: str = "postop_v1") -> RuleSet:
    """Load and validate a versioned rule file."""
    path = RULES_DIR / f"{rules_version}.yaml"
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise RuleError(f"{path} does not contain a rule mapping")
    return RuleSet.model_validate(raw)


class RuleEngine:
    """Evaluates a rule set against one turn.

    Stateless with respect to the conversation — everything it needs arrives as
    arguments — so the same engine instance serves every conversation and every test.
    """

    def __init__(self, rule_set: RuleSet) -> None:
        self.rule_set = rule_set

    @classmethod
    def load(cls, rules_version: str = "postop_v1") -> RuleEngine:
        return cls(load_rules(rules_version))

    def evaluate(
        self,
        extraction: TurnExtraction,
        case: CaseFacts,
        state: ProtocolState | None = None,
        topic_rules: list[str] | None = None,
    ) -> list[Finding]:
        """Every rule that fires on this turn, most urgent first.

        GREEN rules match but produce no `Finding` — they supply approved reassurance
        wording, and a green result must never be able to reach the review queue.
        """
        findings = [
            self._to_finding(rule, extraction)
            for rule in self._applicable_rules(topic_rules)
            if rule.band is not RuleBand.GREEN
            and self._matches(rule.when, extraction, case, state)
        ]
        return sorted(findings, key=lambda finding: (-finding.routes[0].rank, finding.rule_id))

    def green_matches(
        self,
        extraction: TurnExtraction,
        case: CaseFacts,
        state: ProtocolState | None = None,
        topic_rules: list[str] | None = None,
    ) -> list[Rule]:
        """Expected findings that matched, for approved reassurance language."""
        return [
            rule
            for rule in self._applicable_rules(topic_rules)
            if rule.band is RuleBand.GREEN and self._matches(rule.when, extraction, case, state)
        ]

    def _applicable_rules(self, topic_rules: list[str] | None) -> list[Rule]:
        """Global rules plus the active topic's, deduped, in file order."""
        by_id = self.rule_set.by_id
        wanted = list(self.rule_set.global_rules) + list(topic_rules or [])
        seen: set[str] = set()
        ordered: list[Rule] = []
        for rule_id in wanted:
            if rule_id in seen or rule_id not in by_id:
                continue
            seen.add(rule_id)
            ordered.append(by_id[rule_id])
        return ordered

    def _to_finding(self, rule: Rule, extraction: TurnExtraction) -> Finding:
        return Finding(
            rule_id=rule.id,
            rules_version=self.rule_set.rules_version,
            label=rule.label,
            severity=rule.severity,
            tier=rule.tier,
            routes=rule.routes,
            evidence=_evidence(rule, extraction),
            quotes=[quote for quote in _quotes(extraction) if quote],
            turn_index=extraction.turn_index,
            escalation_template_key=rule.template_key,
        )

    # --- Predicate evaluation ------------------------------------------------

    def _matches(
        self,
        condition: Condition,
        extraction: TurnExtraction,
        case: CaseFacts,
        state: ProtocolState | None,
    ) -> bool:
        if condition.all_ is not None:
            return all(self._matches(child, extraction, case, state) for child in condition.all_)
        if condition.any_ is not None:
            return any(self._matches(child, extraction, case, state) for child in condition.any_)
        if condition.not_ is not None:
            # Fail closed: `not` inverts a *positive* match only. A predicate that is
            # unevaluable because its input is missing stays false either way, so a
            # negation cannot fire off an empty extraction.
            return not self._matches(condition.not_, extraction, case, state)
        if condition.symptom is not None:
            return self._symptom_matches(condition, extraction)
        if condition.pain is not None:
            return self._pain_matches(condition.pain, extraction)
        if condition.medication is not None:
            return self._medication_matches(condition.medication, extraction)
        if condition.case is not None:
            return self._case_matches(condition.case, case)
        if condition.slot is not None:
            return self._slot_matches(condition, extraction, state)
        if condition.temperature_f_gte is not None:
            return (
                extraction.temperature_f is not None
                and extraction.temperature_f >= condition.temperature_f_gte
            )
        if condition.block_regression_exceeded is not None:
            return self._block_window_exceeded(extraction, case, state) is condition.block_regression_exceeded
        if condition.patient_distress is not None:
            return extraction.patient_distress is condition.patient_distress
        return False

    def _symptom_matches(self, condition: Condition, extraction: TurnExtraction) -> bool:
        observation = extraction.symptom(condition.symptom)  # type: ignore[arg-type]
        if observation.presence is not condition.presence:
            return False
        if condition.min_severity is not None:
            if observation.severity is None:
                # Present but ungraded. The rule asked for a floor and there is no
                # grade to compare, so it does not fire — the finding it would raise
                # could not cite the severity it claims.
                return False
            if observation.severity.rank < condition.min_severity.rank:
                return False
        if condition.onset_hours_within is not None:
            if observation.onset_hours_ago is None:
                return False
            if observation.onset_hours_ago > condition.onset_hours_within:
                return False
        return True

    def _pain_matches(self, predicate: dict[str, Any], extraction: TurnExtraction) -> bool:
        pain = extraction.pain
        if pain is None:
            return False
        if "score_gte" in predicate:
            if pain.score is None or pain.score < predicate["score_gte"]:
                return False
        if "score_lte" in predicate:
            if pain.score is None or pain.score > predicate["score_lte"]:
                return False
        if "controlled_by_medication" in predicate:
            # None means the patient did not say, which is not a match either way.
            if pain.controlled_by_medication is not predicate["controlled_by_medication"]:
                return False
        if "trend" in predicate and pain.trend.value != predicate["trend"]:
            return False
        return True

    def _medication_matches(self, predicate: dict[str, Any], extraction: TurnExtraction) -> bool:
        names = {name.lower() for name in predicate.get("name_any", [])}
        for report in extraction.medications:
            if names and not (report.name and report.name.lower() in names):
                continue
            if "adherence" in predicate:
                if report.adherence is MedAdherence.UNKNOWN:
                    continue
                if report.adherence.value != predicate["adherence"]:
                    continue
            if "last_dose_hours_lt" in predicate:
                if report.last_dose_hours_ago is None:
                    continue
                if report.last_dose_hours_ago >= predicate["last_dose_hours_lt"]:
                    continue
            return True
        return False

    def _case_matches(self, predicate: dict[str, Any], case: CaseFacts) -> bool:
        if "anesthesia_type_in" in predicate:
            if case.anesthesia_type.value not in predicate["anesthesia_type_in"]:
                return False
        if "block_type_in" in predicate:
            if case.block_type is None or case.block_type.value not in predicate["block_type_in"]:
                return False
        if "has_block" in predicate and (case.block_type is not None) is not predicate["has_block"]:
            return False
        if "hours_post_op_gte" in predicate:
            if case.hours_post_op is None or case.hours_post_op < predicate["hours_post_op_gte"]:
                return False
        if "hours_post_op_lt" in predicate:
            if case.hours_post_op is None or case.hours_post_op >= predicate["hours_post_op_lt"]:
                return False
        return True

    def _slot_matches(
        self, condition: Condition, extraction: TurnExtraction, state: ProtocolState | None
    ) -> bool:
        """Read a protocol answer, from this turn or from what the conversation holds.

        Slots persist across turns, so a rule combining an answer given three turns
        ago with a symptom mentioned now has to see both. This turn's value wins when
        the patient has just revised it.
        """
        value = extraction.slot(condition.slot or "").value
        if value is None and state is not None:
            stored = state.slot_values.get(condition.slot or "")
            value = None if stored is None else stored.value
        if value is None:
            return False

        if condition.is_true is not None:
            return (value is True) is condition.is_true
        if condition.is_false is not None:
            return (value is False) is condition.is_false
        if condition.equals is not None:
            return value == condition.equals
        if condition.in_ is not None:
            return value in condition.in_
        if isinstance(value, bool) or not isinstance(value, int | float):
            return False
        if condition.gte is not None:
            return value >= condition.gte
        if condition.lte is not None:
            return value <= condition.lte
        return False

    def _block_window_exceeded(
        self, extraction: TurnExtraction, case: CaseFacts, state: ProtocolState | None
    ) -> bool:
        """Whether the block has outlasted its expected window.

        A data lookup against the `block_regression` table, not clinical knowledge in
        code — which is what lets a clinician revise a threshold without a developer,
        and what the deferred regression-timed scheduler will read.
        """
        window = self.rule_set.window_for(case.block_type)
        if window is None:
            return False
        limit = case.expected_block_duration_hours or window.flag_after_hours
        if case.block_adjuvant:
            limit += self.rule_set.block_adjuvants.get(case.block_adjuvant, 0.0)

        elapsed = extraction.slot("hours_since_block").value
        if elapsed is None and state is not None:
            stored = state.slot_values.get("hours_since_block")
            elapsed = None if stored is None else stored.value
        if elapsed is None:
            elapsed = case.hours_post_op
        if not isinstance(elapsed, int | float) or isinstance(elapsed, bool):
            return False
        return elapsed > limit


def _evidence(rule: Rule, extraction: TurnExtraction) -> list[str]:
    """Which extracted values a clinician should look at, in readable form."""
    parts: list[str] = []
    if extraction.pain and extraction.pain.score is not None:
        parts.append(f"pain {extraction.pain.score}/10")
    for observation in extraction.symptoms:
        if observation.presence is Presence.PRESENT:
            grade = f" ({observation.severity.value})" if observation.severity else ""
            parts.append(f"{observation.code.value}{grade}")
    if extraction.temperature_f is not None:
        parts.append(f"temperature {extraction.temperature_f}F")
    for slot_id, answer in extraction.slot_values.items():
        if answer.value is not None:
            parts.append(f"{slot_id}={answer.value!r}")
    return parts or [f"{rule.id} matched on turn {extraction.turn_index}"]


def _quotes(extraction: TurnExtraction) -> list[str]:
    quotes = [extraction.pain.quote] if extraction.pain else []
    quotes += [observation.quote for observation in extraction.symptoms]
    quotes += [answer.quote for answer in extraction.slot_values.values()]
    seen: set[str] = set()
    unique: list[str] = []
    for quote in quotes:
        if quote and quote not in seen:
            seen.add(quote)
            unique.append(quote)
    return unique


SafetyBand = Literal["red", "yellow", "green", "none"]


def gate(findings: list[Finding]) -> SafetyBand:
    """What the turn pipeline should do with the drafted reply.

    `red` means discard it and send the template instead. This is the one call the
    architecture forbids the model from making, so it is a function of the findings
    alone.
    """
    if any(finding.tier is Tier.TIER_1 for finding in findings):
        return "red"
    if findings:
        return "yellow"
    return "none"
