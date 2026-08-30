"""One patient message in, one assistant message out.

This is `docs/architecture.md`'s turn pipeline, in the order it specifies. Every layer
it calls is already unit-tested on its own; what lives here — and what nothing else
could test — is the *sequence*. Four ordering decisions carry the safety properties,
and each is asserted directly in `tests/test_pipeline.py`:

1. **Rules are evaluated against the topic that was active when the message arrived**,
   before the protocol engine advances. A patient who answers the last slot of one
   topic while reporting something alarming must be judged under that topic's rules,
   not under the rules of whichever topic they are about to be moved into.

2. **A RED finding discards the drafted reply.** The patient is shown fixed
   clinician-authored copy from `app/safety/templates.py` instead. This is the reason
   patient-facing replies are never streamed: nothing may be emitted before the gate
   has run.

3. **A RED halt records the reason `"escalated"`, exactly.** `app/triage/tiering.py`
   reads that literal to tell a correct escalation apart from an abandoned call —
   stopping the script on a red flag is the right behaviour, not a shortfall, and
   double-counting it as abandonment would attribute the tier to the wrong thing.

4. **The turn record is written before the rules run, and whatever happens.**
   Invariant 4. A response that never validated is still a row; skipping it would
   leave a hole in the audit trail exactly where the record is least trustworthy.

Nothing in this module names a topic or a slot, for the same reason
`app/protocol/engine.py` does not: the script is data. `tests/test_protocol.py`
enforces that for both files.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.conversation.session import (
    Conversation,
    EscalationRecord,
    Message,
    open_conversation,
)
from app.domain.enums import ConversationStatus, RuleBand, Tier
from app.domain.schemas import Finding, Summary, TurnExtraction
from app.llm.turn import TurnDraft, TurnEngine, TurnRequest
from app.protocol import engine
from app.protocol.engine import CaseFacts
from app.protocol.loader import Protocol, Topic, flatten, load_default_protocol
from app.safety import templates
from app.safety.rules import RuleEngine, SafetyBand, gate
from app.summary.generator import build_summary, tier_for

# The reason string `app/triage/tiering.py` looks for. Shared as a constant so the two
# modules cannot drift apart on a typo — a mismatch here would silently re-classify
# every escalation as an abandoned conversation.
ESCALATED = "escalated"

# Placeholder sign-off lines, on the same footing as everything in
# `app/safety/templates.py`: scaffolding that says the right kind of thing, written by
# a developer, not signed off by a clinician. The safety-netting instructions the
# patient actually needs are delivered by the final topic's own question in the
# protocol YAML, where a clinician can edit them.
#
# Two lines, not one: a script that ran to the end and a script that gave up on a
# topic it could not get through are different events, and telling a patient the
# check-in is complete when it was cut short would misrepresent what happened to them
# as much as to the reviewer.
CLOSING_MESSAGE = "That's everything I needed — thank you. Your care team will review this."
TERMINATED_MESSAGE = (
    "I'm not able to finish this check-in over chat, so I'm going to pass it to your "
    "care team for someone to follow up with you directly. Thank you for your time."
)


@dataclass(frozen=True)
class TurnOutcome:
    """What one turn did, for a caller that wants to show or assert on it.

    Carries a snapshot of where the conversation stood *after this turn* rather than a
    reference to the live conversation, so a caller replaying a finished run renders
    each turn as it happened instead of painting the final state onto all of them.
    """

    topic_id: str
    next_topic_id: str | None
    extraction: TurnExtraction | None
    findings: list[Finding]
    band: SafetyBand
    reply: Message
    reassurance: list[str] = field(default_factory=list)
    slots_filled: tuple[str, ...] = ()
    topic_changed: bool = False
    hard_failure: bool = False

    # --- Snapshot ----------------------------------------------------------
    cursor: int = 0
    topic_count: int = 0
    slots_total: int = 0
    findings_total: int = 0
    tier_so_far: Tier = Tier.TIER_3
    tier_reason: str = ""

    @property
    def escalated(self) -> bool:
        return self.band == "red"


class ConversationClosed(RuntimeError):
    """A message arrived for a conversation the protocol has already finished."""


@dataclass
class Pipeline:
    """The wiring: a protocol, its paired rules, and a turn engine.

    Holding the three together is what makes the version lockstep checkable in one
    place — `default` loads the rules the protocol names, so a half-upgraded pair
    fails at construction rather than on the turn where the missing rule mattered.
    """

    protocol: Protocol
    rules: RuleEngine
    turn_engine: TurnEngine

    @classmethod
    def default(cls, turn_engine: TurnEngine) -> Pipeline:
        protocol = load_default_protocol()
        return cls(
            protocol=protocol,
            rules=RuleEngine.load(protocol.rules_version),
            turn_engine=turn_engine,
        )

    # --- Lifecycle ---------------------------------------------------------

    def open(self, case: CaseFacts, **kwargs: object) -> Conversation:
        """Start a check-in. The opening line comes from the protocol, not the model."""
        return open_conversation(
            self.protocol,
            case,
            rules_version=self.rules.rule_set.rules_version,
            **kwargs,  # type: ignore[arg-type]
        )

    def send(self, conversation: Conversation, text: str) -> TurnOutcome:
        """Process one patient message. The seven steps, in order."""
        if conversation.state.finished:
            raise ConversationClosed(
                f"conversation {conversation.id} finished "
                f"({conversation.state.halted_reason or 'all topics covered'})"
            )
        topic = engine.active_topic(self.protocol, conversation.state)
        if topic is None:  # pragma: no cover - `finished` covers this
            raise ConversationClosed(f"conversation {conversation.id} has no active topic")

        # 1. Build context, and record what the patient said before anything can fail.
        patient_message = conversation.add_message("patient", text)
        request = TurnRequest(
            protocol=self.protocol,
            case=conversation.case,
            state=conversation.state,
            topic=topic,
            history=conversation.history,
            patient_message=text,
            turn_index=conversation.turn_count,
        )

        # 2. One call to the model layer.
        draft = self.turn_engine.analyze(request)

        # 3. Persist the turn analysis. Always — invariant 4.
        conversation.add_turn_record(patient_message.seq, draft)

        extraction = draft.extraction or _unparseable(request)

        # 4. Rules, against this turn's topic — before the cursor moves.
        findings = self.rules.evaluate(
            extraction, conversation.case, conversation.state, topic.rules
        )
        conversation.findings.extend(findings)

        # 5. Safety gate.
        band = gate(findings)

        # 6. Advance the protocol. The turn is folded in first even when it escalated,
        #    so the answers the patient gave on the way out are not lost from the record.
        before = set(conversation.state.slot_values)
        conversation.state = engine.record_turn(self.protocol, conversation.state, extraction)
        filled = tuple(sorted(set(conversation.state.slot_values) - before))

        if band == "red":
            return self._escalate(conversation, topic, extraction, findings, filled)

        # 7. Compose and persist the reply.
        return self._continue(conversation, topic, extraction, findings, band, draft, filled)

    def close(self, conversation: Conversation) -> Summary:
        """Finish the conversation and produce the clinician-facing record."""
        conversation.status = _terminal_status(conversation)
        if conversation.ended_at is None:
            conversation.ended_at = conversation.messages[-1].created_at
        return build_summary(conversation)

    # --- Gate outcomes -----------------------------------------------------

    def _escalate(
        self,
        conversation: Conversation,
        topic: Topic,
        extraction: TurnExtraction,
        findings: list[Finding],
        filled: tuple[str, ...],
    ) -> TurnOutcome:
        """Discard the draft, send fixed copy, stop the script.

        `RuleEngine.evaluate` already returns findings worst-route-first, so the
        first red finding is the one whose copy the patient is shown. The rest are
        still recorded — a chest pain that fires alongside a bleeding wound owes two
        different people, and only the message is singular.
        """
        red = [finding for finding in findings if finding.tier is Tier.TIER_1]
        template = templates.escalation_copy(red[0])
        reply = conversation.add_message(
            "assistant", template.text, is_templated=True, template_id=template.key
        )

        # One row per route, per docs/data-model.md: a rule owing two owners writes two.
        for finding in red:
            for route in finding.routes:
                conversation.escalations.append(
                    EscalationRecord(
                        rule_id=finding.rule_id,
                        rules_version=finding.rules_version,
                        severity=finding.severity,
                        route=route,
                        template_id=template.key,
                        message_shown=template.text,
                    )
                )

        conversation.state = engine.halt(conversation.state, ESCALATED)
        conversation.status = ConversationStatus.ESCALATED
        return _snapshot(
            conversation,
            TurnOutcome(
                topic_id=topic.id,
                next_topic_id=None,
                extraction=extraction,
                findings=findings,
                band="red",
                reply=reply,
                slots_filled=filled,
            ),
        )

    def _continue(
        self,
        conversation: Conversation,
        topic: Topic,
        extraction: TurnExtraction,
        findings: list[Finding],
        band: SafetyBand,
        draft: TurnDraft,
        filled: tuple[str, ...],
    ) -> TurnOutcome:
        """Keep the drafted reply, unless the conversation has moved past it."""
        next_topic = engine.active_topic(self.protocol, conversation.state)
        moved = next_topic is None or next_topic.id != topic.id

        reassurance = self._reassurance(conversation, topic, extraction, findings, band)

        if conversation.state.finished:
            # `halted_reason` is set only when something stopped the script early; a
            # script that simply ran out of topics leaves it None.
            text = (
                CLOSING_MESSAGE
                if conversation.state.halted_reason is None
                else TERMINATED_MESSAGE
            )
        elif moved and next_topic is not None:
            # The draft was written for a topic that is now finished, so it would ask
            # a question the patient has already answered. The protocol's own opening
            # line for the new topic is the clinician-authored fallback for exactly
            # this. A real turn engine phrases the transition; the ordering does not
            # change.
            text = flatten(next_topic.opening_question)
        else:
            text = draft.draft_reply or flatten(topic.opening_question)

        if reassurance:
            text = " ".join([*reassurance, text])

        reply = conversation.add_message("assistant", text)
        return _snapshot(
            conversation,
            TurnOutcome(
                topic_id=topic.id,
                next_topic_id=None if next_topic is None else next_topic.id,
                extraction=extraction,
                findings=findings,
                band=band,
                reply=reply,
                reassurance=reassurance,
                slots_filled=filled,
                topic_changed=moved,
                hard_failure=draft.hard_failure,
            ),
        )

    def _reassurance(
        self,
        conversation: Conversation,
        topic: Topic,
        extraction: TurnExtraction,
        findings: list[Finding],
        band: SafetyBand,
    ) -> list[str]:
        """Approved wording for expected findings, or nothing.

        Scoped to the topic the turn was judged under, for the same reason the rules
        are: by the time this runs the cursor may already have moved on, and matching
        green rules against the *next* topic's list would silently drop the
        reassurance for the very answer that prompted it.

        Suppressed the moment anything has fired — this turn or any earlier one.
        Conversational constraint 3 in `docs/safety-rules.md`: once a rule has fired,
        the conversation does not reassure. A GREEN rule produces no `Finding` by
        construction (`RuleEngine.evaluate` filters them out), so nothing here can
        reach the review queue either way.
        """
        if band != "none" or conversation.findings:
            return []
        matched = self.rules.green_matches(
            extraction, conversation.case, conversation.state, topic.rules
        )
        # Keyed by `template_key`, not by rule id — the same indirection the RED copy
        # uses, so two rules can share one approved sentence.
        return [
            templates.GREEN_REASSURANCE[rule.template_key]
            for rule in matched
            if rule.band is RuleBand.GREEN
            and rule.template_key in templates.GREEN_REASSURANCE
        ]


def _snapshot(conversation: Conversation, outcome: TurnOutcome) -> TurnOutcome:
    """Stamp the outcome with where the conversation stands, including a running tier.

    The tier is recomputed from scratch rather than accumulated, because that is the
    property `app/triage/tiering.py` is built for: the same findings and the same
    state always give the same tier, so a mid-conversation reading and the stored one
    are the same function of the same inputs, not two approximations.
    """
    decision = tier_for(conversation)
    return replace(
        outcome,
        cursor=conversation.state.cursor,
        topic_count=len(conversation.state.topic_queue),
        slots_total=len(conversation.state.slot_values),
        findings_total=len(conversation.findings),
        tier_so_far=decision.tier,
        tier_reason=decision.reason,
    )


def _unparseable(request: TurnRequest) -> TurnExtraction:
    """A stand-in extraction for a turn whose response never validated.

    The turn record stores the truth — `extraction=None`. But the protocol engine
    still has to see the turn, or a conversation that keeps failing to parse would
    loop on the same topic forever instead of running it out of turns. Confidence 0
    is honest and load-bearing: it marks the topic low-confidence, which is itself a
    triage signal, and it fills nothing.
    """
    return TurnExtraction.model_validate(
        {**request.provenance, "unparseable": True, "extraction_confidence": 0.0}
    )


def _terminal_status(conversation: Conversation) -> ConversationStatus:
    """Where the conversation ended up. Read off the state, never chosen."""
    state = conversation.state
    if state.halted_reason == ESCALATED:
        return ConversationStatus.ESCALATED
    if state.finished and state.cursor >= len(state.topic_queue):
        return ConversationStatus.COMPLETED
    return ConversationStatus.ABANDONED
