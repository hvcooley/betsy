from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.enums import (
    AnesthesiaType,
    CheckinStatus,
    MedAdherence,
    Presence,
    Route,
    Severity,
    SymptomCode,
    Tier,
    Trend,
)
from app.domain.schemas import (
    Finding,
    MedicationReport,
    PainReport,
    Summary,
    SymptomObservation,
    TurnExtraction,
)


def make_extraction(**overrides: object) -> TurnExtraction:
    defaults: dict[str, object] = {
        "protocol_version": "postop_v1",
        "prompt_version": "v1",
        "topic_id": "pain",
        "turn_index": 0,
        "raw_message": "I'm doing okay.",
    }
    return TurnExtraction(**(defaults | overrides))


# --- Enum value stability -------------------------------------------------
# These strings live in rule YAML and in stored rows. Renaming one silently
# breaks existing data, so pin the whole set.


@pytest.mark.parametrize(
    ("enum_cls", "expected"),
    [
        (
            AnesthesiaType,
            {"general", "spinal", "epidural", "peripheral_nerve_block", "mac_sedation", "local"},
        ),
        (Severity, {"none", "mild", "moderate", "severe"}),
        (Presence, {"present", "absent", "unknown"}),
        (Trend, {"better", "unchanged", "worse", "unknown"}),
        (MedAdherence, {"as_prescribed", "partial", "not_taking", "unknown"}),
        (Route, {"self_care", "call_clinic", "urgent_same_day", "emergency_911"}),
        (Tier, {"tier_1", "tier_2", "tier_3"}),
        (CheckinStatus, {"in_progress", "completed", "abandoned", "escalated"}),
        (
            SymptomCode,
            {
                "shortness_of_breath",
                "sore_throat",
                "hoarseness",
                "stridor",
                "chest_pain",
                "palpitations",
                "syncope",
                "dizziness",
                "postural_headache",
                "persistent_numbness",
                "motor_weakness",
                "back_pain_at_injection_site",
                "confusion",
                "nausea",
                "vomiting",
                "unable_to_tolerate_fluids",
                "constipation",
                "urinary_retention",
                "fever",
                "chills",
                "wound_redness",
                "wound_drainage",
                "bleeding_at_site",
                "excessive_sedation",
                "itching",
                "rash",
                "allergic_reaction",
            },
        ),
    ],
)
def test_enum_values_are_stable(enum_cls: type, expected: set[str]) -> None:
    assert {member.value for member in enum_cls} == expected


# --- Ordering -------------------------------------------------------------


def test_severity_ranks_ascend_with_badness() -> None:
    ranks = [Severity.NONE.rank, Severity.MILD.rank, Severity.MODERATE.rank, Severity.SEVERE.rank]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


def test_generalized_pain_has_no_symptom_code() -> None:
    """PainReport.score is the only representation of pain severity.

    `back_pain_at_injection_site` is intentionally still a code — it localizes a
    neuraxial complication rather than grading overall pain.
    """
    assert "uncontrolled_pain" not in {code.value for code in SymptomCode}


# --- Pain 0-10 scale to Severity banding ----------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, Severity.NONE),
        (1, Severity.MILD),
        (3, Severity.MILD),
        (4, Severity.MODERATE),
        (6, Severity.MODERATE),
        (7, Severity.SEVERE),
        (10, Severity.SEVERE),
    ],
)
def test_pain_score_bands_onto_severity(score: int, expected: Severity) -> None:
    assert Severity.from_pain_score(score) is expected


@pytest.mark.parametrize("score", [-1, 11])
def test_from_pain_score_rejects_off_scale_values(score: int) -> None:
    with pytest.raises(ValueError, match="0-10 scale"):
        Severity.from_pain_score(score)


def test_pain_report_severity_is_derived_from_score() -> None:
    assert PainReport(score=8).severity is Severity.SEVERE
    assert PainReport(score=2).severity is Severity.MILD


def test_pain_report_severity_is_none_when_unscored() -> None:
    assert PainReport(trend=Trend.WORSE).severity is None


def test_pain_report_severity_is_not_stored() -> None:
    """Derived on read, so it cannot drift from `score` in the DB."""
    assert "severity" not in PainReport(score=8).model_dump()


def test_route_rank_ascends_with_urgency() -> None:
    assert (
        Route.SELF_CARE.rank
        < Route.CALL_CLINIC.rank
        < Route.URGENT_SAME_DAY.rank
        < Route.EMERGENCY_911.rank
    )


def test_tier_rank_inverts_the_number() -> None:
    """Tier 1 is the most urgent despite the lowest number."""
    assert Tier.TIER_1.rank > Tier.TIER_2.rank > Tier.TIER_3.rank


def test_most_urgent_picks_the_worst() -> None:
    assert Tier.most_urgent([Tier.TIER_3, Tier.TIER_1, Tier.TIER_2]) is Tier.TIER_1
    assert Route.most_urgent([Route.CALL_CLINIC, Route.EMERGENCY_911]) is Route.EMERGENCY_911


def test_most_urgent_defaults_when_nothing_fired() -> None:
    assert Tier.most_urgent([]) is Tier.TIER_3
    assert Route.most_urgent([]) is Route.SELF_CARE


# --- SymptomObservation coherence ----------------------------------------


def test_denied_symptom_cannot_carry_a_severity() -> None:
    with pytest.raises(ValidationError):
        SymptomObservation(
            code=SymptomCode.CHEST_PAIN, presence=Presence.ABSENT, severity=Severity.SEVERE
        )


def test_unmentioned_symptom_cannot_carry_a_severity() -> None:
    with pytest.raises(ValidationError):
        SymptomObservation(
            code=SymptomCode.FEVER, presence=Presence.UNKNOWN, severity=Severity.MILD
        )


def test_present_symptom_cannot_be_graded_none() -> None:
    with pytest.raises(ValidationError):
        SymptomObservation(
            code=SymptomCode.NAUSEA, presence=Presence.PRESENT, severity=Severity.NONE
        )


def test_present_symptom_may_be_ungraded() -> None:
    observation = SymptomObservation(code=SymptomCode.NAUSEA, presence=Presence.PRESENT)
    assert observation.severity is None


def test_denied_symptom_may_be_graded_none() -> None:
    observation = SymptomObservation(
        code=SymptomCode.CHEST_PAIN, presence=Presence.ABSENT, severity=Severity.NONE
    )
    assert observation.presence is Presence.ABSENT


# --- Absent is not the same as unmentioned -------------------------------


def test_unmentioned_symptom_reads_as_unknown_not_absent() -> None:
    """Silence must never be mistaken for a denial — rules have to fail closed."""
    extraction = make_extraction()
    observation = extraction.symptom(SymptomCode.CHEST_PAIN)
    assert observation.presence is Presence.UNKNOWN
    assert observation.presence is not Presence.ABSENT


def test_symptom_lookup_returns_the_reported_observation() -> None:
    reported = SymptomObservation(
        code=SymptomCode.URINARY_RETENTION,
        presence=Presence.PRESENT,
        severity=Severity.MODERATE,
    )
    extraction = make_extraction(symptoms=[reported])
    assert extraction.symptom(SymptomCode.URINARY_RETENTION) == reported
    assert extraction.symptom(SymptomCode.FEVER).presence is Presence.UNKNOWN


# --- Field bounds ---------------------------------------------------------


@pytest.mark.parametrize("score", [-1, 11])
def test_pain_score_is_bounded_to_the_zero_to_ten_scale(score: int) -> None:
    with pytest.raises(ValidationError):
        PainReport(score=score)


@pytest.mark.parametrize("confidence", [-0.1, 1.5])
def test_extraction_confidence_is_bounded_to_a_probability(confidence: float) -> None:
    with pytest.raises(ValidationError):
        make_extraction(extraction_confidence=confidence)


def test_turn_index_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        make_extraction(turn_index=-1)


# --- JSON round-trips (the DB layer stores these as JSON) -----------------


def test_turn_extraction_round_trips_through_json() -> None:
    extraction = make_extraction(
        pain=PainReport(score=7, worst_score=9, location="incision", trend=Trend.WORSE),
        symptoms=[
            SymptomObservation(
                code=SymptomCode.FEVER, presence=Presence.PRESENT, severity=Severity.MILD
            ),
            SymptomObservation(code=SymptomCode.CHEST_PAIN, presence=Presence.ABSENT),
        ],
        medications=[
            MedicationReport(
                name="oxycodone",
                adherence=MedAdherence.PARTIAL,
                missed_doses=2,
                side_effects=[SymptomCode.NAUSEA],
            )
        ],
        temperature_f=100.9,
        question_answered=True,
        extraction_confidence=0.82,
        raw_message="Pain is about a 7 and I feel warm.",
    )
    assert TurnExtraction.model_validate_json(extraction.model_dump_json()) == extraction


def make_finding() -> Finding:
    return Finding(
        rule_id="fever_with_wound_drainage",
        rules_version="postop_v1",
        label="Fever with wound drainage — possible surgical site infection",
        severity=Severity.MODERATE,
        tier=Tier.TIER_2,
        route=Route.URGENT_SAME_DAY,
        evidence=["fever present (mild)", "temperature_f=100.9"],
        quotes=["I feel warm and the dressing is damp"],
        turn_index=3,
        escalation_template_key="urgent_same_day_infection",
    )


def test_finding_round_trips_through_json() -> None:
    finding = make_finding()
    assert Finding.model_validate_json(finding.model_dump_json()) == finding


def test_summary_round_trips_through_json() -> None:
    summary = Summary(
        checkin_id="chk_001",
        patient_ref="pt_abc",
        anesthesia_type=AnesthesiaType.SPINAL,
        procedure="knee arthroscopy",
        status=CheckinStatus.ESCALATED,
        tier=Tier.TIER_2,
        route=Route.URGENT_SAME_DAY,
        findings=[make_finding()],
        max_pain_score=7,
        pain_trend=Trend.WORSE,
        adherence=MedAdherence.PARTIAL,
        headline="POD 2 spinal: fever with wound drainage, pain 7/10 and worsening.",
        narrative="Patient reports a warm feeling and damp dressing.",
        protocol_version="postop_v1",
        rules_version="postop_v1",
        prompt_version="v1",
        started_at=datetime(2026, 8, 19, 14, 0, tzinfo=UTC),
        completed_at=datetime(2026, 8, 19, 14, 6, tzinfo=UTC),
        turn_count=8,
    )
    assert Summary.model_validate_json(summary.model_dump_json()) == summary


def test_summary_triage_defaults_to_the_least_urgent_disposition() -> None:
    """An empty summary must not imply escalation."""
    summary = Summary(
        checkin_id="chk_002",
        anesthesia_type=AnesthesiaType.GENERAL,
        headline="No concerns reported.",
        protocol_version="postop_v1",
        rules_version="postop_v1",
        prompt_version="v1",
        started_at=datetime(2026, 8, 19, 14, 0, tzinfo=UTC),
    )
    assert summary.tier is Tier.TIER_3
    assert summary.route is Route.SELF_CARE
    assert summary.findings == []
