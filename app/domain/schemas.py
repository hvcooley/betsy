"""Structured types passed between the protocol, LLM, safety and summary layers.

The governing rule of this project: **the LLM produces observations, deterministic
code produces decisions.** `TurnExtraction` is therefore designed to be rich enough
that `app/safety/rules.py` can evaluate every rule against its fields alone, without
ever re-reading the patient's free text. Correspondingly, no field on `Summary` that
drives a clinical decision (`tier`, `route`, `findings`) originates from the model.

These models are stored as JSON by the DB layer, so field names are part of the
persisted format and change with a version bump, not in place.
"""

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.domain.enums import (
    AnesthesiaType,
    BlockType,
    CheckinStatus,
    MedAdherence,
    Presence,
    Route,
    Severity,
    SymptomCode,
    Tier,
    Trend,
)


class PainReport(BaseModel):
    """Pain as described by the patient in one turn.

    The single source of truth for pain. Pain is kept on its native 0-10 numeric
    rating scale rather than the coarse `Severity` ordinal used for other symptoms,
    because patients report that scale reliably and clinicians chart it — banding a
    4 and a 6 together as "moderate" would discard real signal. Deliberately there
    is no `SymptomCode` for pain; a second representation could drift from this one.
    """

    score: int | None = Field(default=None, ge=0, le=10, description="Current 0-10 pain score.")
    worst_score: int | None = Field(
        default=None, ge=0, le=10, description="Worst 0-10 score since the last check-in."
    )
    location: str | None = None
    trend: Trend = Trend.UNKNOWN
    controlled_by_medication: bool | None = Field(
        default=None, description="None when the patient did not say."
    )
    quote: str | None = Field(default=None, description="Patient's own words, for the audit trail.")

    @property
    def severity(self) -> Severity | None:
        """`score` banded onto the shared `Severity` scale, or None if unscored.

        Derived on read, never stored, so it cannot disagree with `score`. Lets a
        rule compare pain against other symptoms in one currency; rules that care
        about the exact number should read `score` directly.
        """
        if self.score is None:
            return None
        return Severity.from_pain_score(self.score)


class SymptomObservation(BaseModel):
    """One symptom, as reported, denied, or never raised.

    `presence` and `severity` are independent: a symptom can be PRESENT with an
    unstated severity (`None`), which is common when a patient mentions something
    in passing.
    """

    code: SymptomCode
    presence: Presence
    severity: Severity | None = Field(
        default=None, description="None when present but ungraded, or when not applicable."
    )
    onset_hours_ago: float | None = Field(default=None, ge=0)
    quote: str | None = None

    @model_validator(mode="after")
    def check_severity_matches_presence(self) -> Self:
        """Reject incoherent extractions before a safety rule can act on them."""
        if self.severity is None:
            return self
        if self.presence is not Presence.PRESENT and self.severity.rank > 0:
            raise ValueError(
                f"symptom {self.code.value} is {self.presence.value} "
                f"but carries severity {self.severity.value}"
            )
        if self.presence is Presence.PRESENT and self.severity is Severity.NONE:
            raise ValueError(
                f"symptom {self.code.value} is present but graded severity none; "
                "use severity=None for a present-but-ungraded symptom"
            )
        return self


class MedicationReport(BaseModel):
    """Adherence to one medication, as described by the patient."""

    name: str | None = None
    adherence: MedAdherence = MedAdherence.UNKNOWN
    last_dose_hours_ago: float | None = Field(default=None, ge=0)
    missed_doses: int | None = Field(default=None, ge=0)
    side_effects: list[SymptomCode] = Field(default_factory=list)
    quote: str | None = None


class TurnExtraction(BaseModel):
    """Everything one LLM call pulled out of a single patient message.

    Contains no decisions — only what the patient said, normalized. The protocol
    engine and safety rules read this and decide what happens next.
    """

    # --- Provenance -------------------------------------------------------
    protocol_version: str
    prompt_version: str
    topic_id: str = Field(description="Protocol topic this turn was answering.")
    turn_index: int = Field(ge=0)

    # --- Clinical content -------------------------------------------------
    pain: PainReport | None = None
    symptoms: list[SymptomObservation] = Field(default_factory=list)
    medications: list[MedicationReport] = Field(default_factory=list)
    temperature_f: float | None = Field(default=None, description="Self-reported, if given.")

    # --- Conversation signals ---------------------------------------------
    # The model reports these; the protocol engine decides what to do about them.
    question_answered: bool = False
    patient_requests_human: bool = False
    patient_distress: bool = False
    unparseable: bool = False
    extraction_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # --- Audit trail ------------------------------------------------------
    raw_message: str
    notes: str | None = None

    def symptom(self, code: SymptomCode) -> SymptomObservation:
        """The observation for `code`, or an UNKNOWN one if it never came up.

        Returning UNKNOWN rather than ABSENT for an unmentioned symptom is what
        makes rules fail closed: a rule that forgets to check `presence` cannot
        mistake silence for a denial.
        """
        for observation in self.symptoms:
            if observation.code is code:
                return observation
        return SymptomObservation(code=code, presence=Presence.UNKNOWN)


class Finding(BaseModel):
    """One deterministic safety rule that fired, with the evidence that fired it.

    Produced by `app/safety/rules.py`, never by the LLM.
    """

    rule_id: str
    rules_version: str
    label: str = Field(description="Clinician-facing one-liner, from the rule definition.")
    severity: Severity
    tier: Tier
    route: Route
    evidence: list[str] = Field(
        default_factory=list, description="Which extracted values matched, in readable form."
    )
    quotes: list[str] = Field(default_factory=list, description="Patient's own words.")
    turn_index: int | None = Field(default=None, ge=0)
    escalation_template_key: str | None = Field(
        default=None, description="Key into safety/templates.py; None means no patient-facing copy."
    )
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Summary(BaseModel):
    """The clinician-facing record of a completed check-in.

    The deterministic and LLM-generated blocks are kept visibly separate: the
    triage decision must remain reproducible from the findings alone, with the
    narrative treated as unreviewed prose.
    """

    # --- Identity ---------------------------------------------------------
    checkin_id: str
    patient_ref: str | None = Field(default=None, description="Opaque patient identifier.")
    anesthesia_type: AnesthesiaType
    block_type: BlockType | None = Field(
        default=None, description="The block that has to wear off. None when there isn't one."
    )
    procedure: str | None = None
    status: CheckinStatus = CheckinStatus.IN_PROGRESS

    # --- Deterministic: derived from rules, reproducible, decision-bearing --
    tier: Tier = Tier.TIER_3
    route: Route = Route.SELF_CARE
    findings: list[Finding] = Field(default_factory=list)
    max_pain_score: int | None = Field(default=None, ge=0, le=10)
    pain_trend: Trend = Trend.UNKNOWN
    adherence: MedAdherence = MedAdherence.UNKNOWN

    # --- LLM-generated: prose only, never drives triage ---------------------
    headline: str = Field(description="One-line header for the review queue.")
    narrative: str | None = None
    headline_source: Literal["llm", "template"] = "llm"

    # --- Provenance -------------------------------------------------------
    protocol_version: str
    rules_version: str
    prompt_version: str
    started_at: datetime
    completed_at: datetime | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    turn_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def check_block_recorded_when_the_technique_implies_one(self) -> Self:
        """Fail closed when a case that must have a block has none recorded.

        `BLOCK_PROLONGED` can only fire against a `block_type`, so a missing one
        is a silent safety gap rather than a visible error. Only the two
        techniques that always imply a block are enforced; a general with an
        adjunct block, or an epidural, stay permissive because absence there is
        a legitimate answer.

        This belongs on the `case` row once that table exists — Summary is
        currently the only model carrying case facts.
        """
        if (
            self.anesthesia_type is AnesthesiaType.SPINAL
            and self.block_type is not BlockType.SPINAL
        ):
            raise ValueError(
                "spinal anesthesia requires block_type=spinal so its regression window applies"
            )
        if (
            self.anesthesia_type is AnesthesiaType.PERIPHERAL_NERVE_BLOCK
            and self.block_type is None
        ):
            raise ValueError("peripheral_nerve_block anesthesia requires a block_type")
        return self
