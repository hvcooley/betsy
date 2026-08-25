"""Deterministic Tier 1/2/3 assignment.

The tier is the thing an anesthesiologist trusts to protect their attention, so it is
computed here from the findings and the conversation record — never by the model. Two
properties follow from that and are the point of this module:

**Reproducible.** The same findings and the same state always give the same tier. A
stored conversation can be re-tiered years later and land in the same place.

**Explainable in one sentence.** `TierDecision.reason` is that sentence. A tier a
clinician cannot immediately account for is a tier they will stop trusting.

Note that the tiers are broader than "how sick is the patient". An abandoned
conversation and a schema hard-failure are Tier 1 because the *record* is unreliable,
not because the patient is unstable — an unreviewable check-in is not a safe one.
Tier 3 has to be genuinely approvable from the one-liner alone, which is the entire
labour-saving argument for the product, so anything that undermines that confidence
must land in Tier 2 or above.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.enums import Tier
from app.domain.schemas import Finding
from app.protocol.engine import ProtocolState

# A conversation whose extraction was this shaky in this many topics is Tier 1: the
# record cannot be trusted even if nothing clinical fired.
LOW_CONFIDENCE_TOPIC_LIMIT = 2


@dataclass(frozen=True)
class TierDecision:
    """A tier and the one sentence that accounts for it."""

    tier: Tier
    reason: str
    contributing_rule_ids: tuple[str, ...] = field(default_factory=tuple)


def assign_tier(
    findings: list[Finding],
    state: ProtocolState,
    *,
    proxy_reported: bool = False,
    validation_hard_failure: bool = False,
    unresolved_symptom: bool = False,
) -> TierDecision:
    """The triage tier for a completed or abandoned conversation.

    Clauses are checked worst-first and the first match wins, so the reason names the
    most serious thing that happened rather than an incidental one.
    """
    red = [finding for finding in findings if finding.tier is Tier.TIER_1]
    yellow = [finding for finding in findings if finding.tier is Tier.TIER_2]

    # --- Tier 1 ---------------------------------------------------------
    if red:
        return TierDecision(
            tier=Tier.TIER_1,
            reason=f"Red-flag rule fired: {_names(red)}.",
            contributing_rule_ids=tuple(finding.rule_id for finding in red),
        )
    if validation_hard_failure:
        return TierDecision(
            tier=Tier.TIER_1,
            reason="Extraction failed schema validation, so the record is not trustworthy.",
        )
    if _abandoned(state) and unresolved_symptom:
        return TierDecision(
            tier=Tier.TIER_1,
            reason="Conversation ended early with a symptom still unresolved.",
        )
    if proxy_reported and yellow:
        return TierDecision(
            tier=Tier.TIER_1,
            reason=(
                f"Proxy-reported conversation with a yellow finding ({_names(yellow)}); "
                "answers are second-hand."
            ),
            contributing_rule_ids=tuple(finding.rule_id for finding in yellow),
        )
    if len(state.low_confidence_topics) >= LOW_CONFIDENCE_TOPIC_LIMIT:
        return TierDecision(
            tier=Tier.TIER_1,
            reason=(
                f"Low-confidence extraction across {len(state.low_confidence_topics)} topics "
                f"({', '.join(state.low_confidence_topics)})."
            ),
        )

    # --- Tier 2 ---------------------------------------------------------
    if yellow:
        return TierDecision(
            tier=Tier.TIER_2,
            reason=f"Yellow finding: {_names(yellow)}.",
            contributing_rule_ids=tuple(finding.rule_id for finding in yellow),
        )
    if state.unanswered_questions:
        return TierDecision(
            tier=Tier.TIER_2,
            reason=(
                f"{len(state.unanswered_questions)} patient question(s) went unanswered."
            ),
        )
    if state.exited_on_max_turns:
        return TierDecision(
            tier=Tier.TIER_2,
            reason=(
                f"Topic(s) {', '.join(state.exited_on_max_turns)} ran out of turns without "
                "getting an answer."
            ),
        )
    if _abandoned(state):
        return TierDecision(
            tier=Tier.TIER_2,
            reason="Conversation ended before all topics were covered.",
        )

    # --- Tier 3 ---------------------------------------------------------
    return TierDecision(
        tier=Tier.TIER_3,
        reason="All topics answered, no findings, no unanswered questions.",
    )


def _abandoned(state: ProtocolState) -> bool:
    """Whether the script stopped short of covering every applicable topic.

    A halt on a RED escalation is not abandonment — stopping was the correct
    behaviour there, and that case is already Tier 1 on its own clause.
    """
    return state.cursor < len(state.topic_queue) and state.halted_reason != "escalated"


def _names(findings: list[Finding]) -> str:
    return ", ".join(sorted({finding.rule_id for finding in findings}))
