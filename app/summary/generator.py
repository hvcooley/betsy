"""The clinician-facing record of one finished conversation.

`docs/triage-and-summary.md` splits the summary in three, and this module builds the
two that must not come from a model:

- the **tier**, which comes from `app/triage/tiering.py` and nothing else, and
- the **structured block** — pain, trend, adherence, findings, routes — which is
  rendered from the record rather than described by the model.

The third part, the narrative, is prose and stays `None` until the LLM layer lands.
The headline is currently rendered from the same structured fields and marked
`headline_source="template"`, which is what that field exists to distinguish: a
reader can tell at a glance whether the one line they are trusting was generated or
derived. Nothing here is a decision — every clinical claim it renders is already a
`Finding` row, which is what makes the anti-fabrication constraint hold trivially for
a template and checkable later for a model.
"""

from __future__ import annotations

from app.conversation.session import Conversation
from app.domain.enums import MedAdherence, Route, Tier, Trend
from app.domain.schemas import Summary
from app.triage.tiering import TierDecision, assign_tier

HEADLINE_LIMIT = 140

# Worst-first, so a patient who took one dose and skipped three is not recorded as
# adherent because the adherent report came last.
_ADHERENCE_ORDER: dict[MedAdherence, int] = {
    MedAdherence.UNKNOWN: 0,
    MedAdherence.AS_PRESCRIBED: 1,
    MedAdherence.PARTIAL: 2,
    MedAdherence.NOT_TAKING: 3,
}


def build_summary(conversation: Conversation) -> Summary:
    """Render the summary for a closed conversation. Deterministic, no LLM."""
    decision = tier_for(conversation)
    findings = conversation.findings
    return Summary(
        conversation_id=conversation.id,
        patient_ref=conversation.patient_ref,
        anesthesia_type=conversation.case.anesthesia_type,
        block_type=conversation.case.block_type,
        procedure=conversation.case.procedure,
        status=conversation.status,
        tier=decision.tier,
        routes=list(Route.combine(route for finding in findings for route in finding.routes)),
        findings=findings,
        max_pain_score=_max_pain(conversation),
        pain_trend=_pain_trend(conversation),
        adherence=_adherence(conversation),
        headline=headline(conversation, decision),
        narrative=None,
        headline_source="template",
        protocol_version=conversation.protocol_version,
        rules_version=conversation.rules_version,
        prompt_version=conversation.prompt_version,
        started_at=conversation.started_at,
        completed_at=conversation.ended_at,
        turn_count=conversation.turn_count,
    )


def tier_for(conversation: Conversation) -> TierDecision:
    """The tier and the one sentence that accounts for it.

    Split out from `build_summary` so a caller mid-conversation — the CLI showing a
    running tier, a future review endpoint — reads exactly the same function the
    stored summary was built from, rather than a second approximation of it.
    """
    return assign_tier(
        conversation.findings,
        conversation.state,
        proxy_reported=conversation.proxy_reported,
        validation_hard_failure=conversation.validation_hard_failure,
        unresolved_symptom=conversation.unresolved_symptom,
    )


def headline(conversation: Conversation, decision: TierDecision) -> str:
    """The one line read for a Tier 3 case, so it has to carry the whole case.

    Assembled worst-detail-last and then truncated: if something has to be cut, the
    part that survives is the case and the pain score rather than the tail of a rule
    list. Capped at the model's own 140-character limit.
    """
    parts: list[str] = []
    hours = conversation.case.hours_post_op
    if hours is not None:
        parts.append(f"{hours:g}h post-op")
    if conversation.case.procedure:
        parts.append(conversation.case.procedure)

    # Current score first, worst only when it differs: "pain 6/10" for a patient now
    # sitting at 3 reads as a worse case than it is, and the headline is the only
    # thing a Tier 3 reviewer reads.
    latest, worst = _latest_pain(conversation), _max_pain(conversation)
    if latest is not None:
        parts.append(f"pain {latest}/10" + (f" (worst {worst})" if worst != latest else ""))
    elif worst is not None:
        parts.append(f"worst pain {worst}/10")

    rule_ids = sorted({finding.rule_id for finding in conversation.findings})
    if rule_ids:
        parts.append(", ".join(rule_ids))
    else:
        parts.append("no findings")

    tier_label = {Tier.TIER_1: "T1", Tier.TIER_2: "T2", Tier.TIER_3: "T3"}[decision.tier]
    line = f"[{tier_label}] " + " — ".join(parts)
    if conversation.status.value not in ("completed", "escalated"):
        line += f" ({conversation.status.value})"
    return line if len(line) <= HEADLINE_LIMIT else line[: HEADLINE_LIMIT - 1].rstrip() + "…"


def _max_pain(conversation: Conversation) -> int | None:
    """The worst pain reported, whether reported as current or as a worst-since.

    Both scores answer the same clinical question, so taking the maximum across them
    means a patient whose pain has since settled still surfaces the peak.
    """
    scores = [
        score
        for extraction in conversation.extractions
        if extraction.pain is not None
        for score in (extraction.pain.score, extraction.pain.worst_score)
        if score is not None
    ]
    return max(scores) if scores else None


def _latest_pain(conversation: Conversation) -> int | None:
    """The most recently stated current score. Where the patient is now, not their peak."""
    for extraction in reversed(conversation.extractions):
        if extraction.pain is not None and extraction.pain.score is not None:
            return extraction.pain.score
    return None


def _pain_trend(conversation: Conversation) -> Trend:
    """The most recent stated direction. Silence does not overwrite a stated trend."""
    for extraction in reversed(conversation.extractions):
        if extraction.pain is not None and extraction.pain.trend is not Trend.UNKNOWN:
            return extraction.pain.trend
    return Trend.UNKNOWN


def _adherence(conversation: Conversation) -> MedAdherence:
    """The worst adherence reported for any medication across the conversation."""
    reported = [
        report.adherence
        for extraction in conversation.extractions
        for report in extraction.medications
    ]
    return max(reported, key=lambda value: _ADHERENCE_ORDER[value], default=MedAdherence.UNKNOWN)
