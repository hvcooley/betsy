from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.domain.enums import (
    AnesthesiaType,
    BlockType,
    ConversationStatus,
    MedAdherence,
    Presence,
    Route,
    RouteOwner,
    RuleBand,
    Severity,
    SymptomCode,
    Tier,
    Trend,
)
from app.domain.schemas import (
    Finding,
    MedicationReport,
    PainReport,
    SlotValue,
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


def make_summary(**overrides: object) -> Summary:
    defaults: dict[str, object] = {
        "conversation_id": "conv_000",
        "anesthesia_type": AnesthesiaType.GENERAL,
        "headline": "No concerns reported.",
        "protocol_version": "postop_v1",
        "rules_version": "postop_v1",
        "prompt_version": "v1",
        "started_at": datetime(2026, 8, 19, 14, 0, tzinfo=UTC),
    }
    return Summary(**(defaults | overrides))


# --- Enum value stability -------------------------------------------------
# These strings live in rule YAML and in stored rows. Renaming one silently
# breaks existing data, so pin the whole set.


@pytest.mark.parametrize(
    ("enum_cls", "expected"),
    [
        (
            AnesthesiaType,
            {
                "general",
                "spinal",
                "epidural",
                "combined_spinal_epidural",
                "peripheral_nerve_block",
                "mac_sedation",
                "local",
            },
        ),
        (
            BlockType,
            {
                "interscalene",
                "supraclavicular",
                "infraclavicular",
                "adductor_canal",
                "popliteal_sciatic",
                "tap",
                "field_block",
                "spinal",
            },
        ),
        (Severity, {"none", "mild", "moderate", "severe"}),
        (Presence, {"present", "absent", "unknown"}),
        (Trend, {"better", "unchanged", "worse", "unknown"}),
        (MedAdherence, {"as_prescribed", "partial", "not_taking", "unknown"}),
        (Route, {"call_911", "ed_now", "call_surgeon", "call_anesthesia", "routine"}),
        (RouteOwner, {"ems", "emergency_dept", "surgeon", "anesthesia", "patient"}),
        (Tier, {"tier_1", "tier_2", "tier_3"}),
        (ConversationStatus, {"in_progress", "completed", "abandoned", "escalated"}),
        (
            SymptomCode,
            {
                "shortness_of_breath",
                "sore_throat",
                "hoarseness",
                "stridor",
                "difficulty_swallowing",
                "drooling",
                "facial_swelling",
                "tongue_swelling",
                "chest_pain",
                "palpitations",
                "syncope",
                "dizziness",
                "postural_headache",
                "persistent_numbness",
                "motor_weakness",
                "back_pain_at_injection_site",
                "confusion",
                "saddle_numbness",
                "visual_changes",
                "neck_stiffness",
                "perioral_numbness",
                "metallic_taste",
                "tinnitus",
                "nausea",
                "vomiting",
                "unable_to_tolerate_fluids",
                "constipation",
                "bowel_incontinence",
                "urinary_retention",
                "dark_urine",
                "no_urine_output",
                "calf_pain",
                "calf_swelling",
                "limb_tightness",
                "pain_on_passive_stretch",
                "muscle_rigidity",
                "fever",
                "chills",
                "wound_redness",
                "wound_drainage",
                "bleeding_at_site",
                "dressing_soaked",
                "expanding_hematoma",
                "catheter_site_drainage",
                "dental_injury",
                "tongue_laceration",
                "eye_irritation",
                "excessive_sedation",
                "itching",
                "rash",
                "allergic_reaction",
                "suicidal_ideation",
            },
        ),
        (RuleBand, {"red", "yellow", "green"}),
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


# --- Anesthesia type and block type stay orthogonal -----------------------
# A patient can have a general anesthetic *and* a block. That combination is
# expressed by two independent fields, never by a fused enum member.


def test_anesthesia_type_does_not_enumerate_block_combinations() -> None:
    values = {member.value for member in AnesthesiaType}
    assert not [value for value in values if "with_block" in value or value == "regional_block"]


def test_general_with_a_block_is_representable() -> None:
    summary = make_summary(
        anesthesia_type=AnesthesiaType.GENERAL, block_type=BlockType.INTERSCALENE
    )
    assert summary.anesthesia_type is AnesthesiaType.GENERAL
    assert summary.block_type is BlockType.INTERSCALENE


def test_general_without_a_block_records_no_block() -> None:
    assert make_summary(anesthesia_type=AnesthesiaType.GENERAL).block_type is None


def test_spinal_must_carry_its_block_type() -> None:
    """Otherwise BLOCK_PROLONGED has no window to check and fails open."""
    with pytest.raises(ValidationError, match="block_type=spinal"):
        make_summary(anesthesia_type=AnesthesiaType.SPINAL)


def test_block_as_the_primary_anesthetic_must_say_which_block() -> None:
    with pytest.raises(ValidationError, match="requires a block_type"):
        make_summary(anesthesia_type=AnesthesiaType.PERIPHERAL_NERVE_BLOCK)


# --- Block regression windows ---------------------------------------------

BLOCK_REGRESSION = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "app" / "safety" / "rules" / "postop_v1.yaml").read_text()
)["block_regression"]


def test_every_block_type_has_a_regression_window() -> None:
    """A block type without a window would let a prolonged block pass unflagged."""
    assert set(BLOCK_REGRESSION) == {member.value for member in BlockType}


@pytest.mark.parametrize("block", sorted(BLOCK_REGRESSION))
def test_regression_window_is_coherent(block: str) -> None:
    window = BLOCK_REGRESSION[block]
    earliest, latest = window["typical_hours"]
    assert 0 < earliest <= latest
    assert window["flag_after_hours"] >= latest, "flagging inside the typical window guarantees FPs"


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
    assert Route.ROUTINE.rank < Route.CALL_SURGEON.rank < Route.ED_NOW.rank < Route.CALL_911.rank


def test_tier_rank_inverts_the_number() -> None:
    """Tier 1 is the most urgent despite the lowest number."""
    assert Tier.TIER_1.rank > Tier.TIER_2.rank > Tier.TIER_3.rank


def test_most_urgent_picks_the_worst() -> None:
    assert Tier.most_urgent([Tier.TIER_3, Tier.TIER_1, Tier.TIER_2]) is Tier.TIER_1
    assert Route.most_urgent([Route.CALL_SURGEON, Route.CALL_911]) is Route.CALL_911


def test_most_urgent_defaults_when_nothing_fired() -> None:
    assert Tier.most_urgent([]) is Tier.TIER_3
    assert Route.most_urgent([]) is Route.ROUTINE


# --- Routes carry who owns the problem ------------------------------------
# A route is an urgency *and* an owner. Bleeding is the surgeon's; a block that
# will not wear off is anesthesia's. Anything that reduces a set of routes by
# urgency alone drops one of those owners on the floor.


def test_every_route_has_an_owner() -> None:
    assert {route.owner for route in Route} == set(RouteOwner)


def test_calling_the_surgeon_is_not_calling_anesthesia() -> None:
    """The distinction the old `call_clinic` member erased."""
    assert Route.CALL_SURGEON.owner is not Route.CALL_ANESTHESIA.owner


def test_equally_urgent_routes_tie_on_rank() -> None:
    """Neither owner outranks the other, so rank must not pretend otherwise."""
    assert Route.CALL_SURGEON.rank == Route.CALL_ANESTHESIA.rank


def test_combine_keeps_both_owners() -> None:
    combined = Route.combine([Route.CALL_ANESTHESIA, Route.CALL_SURGEON])
    assert set(combined) == {Route.CALL_SURGEON, Route.CALL_ANESTHESIA}


def test_combine_keeps_the_surgeon_alongside_a_more_urgent_route() -> None:
    """SURGICAL_BLEEDING is `CALL_SURGEON + ED_NOW`, not whichever is worse."""
    assert Route.combine([Route.CALL_SURGEON, Route.ED_NOW]) == (
        Route.ED_NOW,
        Route.CALL_SURGEON,
    )


def test_combine_orders_worst_first() -> None:
    combined = Route.combine([Route.CALL_ANESTHESIA, Route.CALL_911, Route.ED_NOW])
    assert combined == (Route.CALL_911, Route.ED_NOW, Route.CALL_ANESTHESIA)


def test_combine_keeps_one_route_per_owner() -> None:
    assert Route.combine([Route.CALL_SURGEON, Route.CALL_SURGEON]) == (Route.CALL_SURGEON,)


def test_combine_drops_routine_once_anything_fired() -> None:
    assert Route.combine([Route.ROUTINE, Route.CALL_ANESTHESIA]) == (Route.CALL_ANESTHESIA,)


def test_combine_defaults_to_routine() -> None:
    assert Route.combine([]) == (Route.ROUTINE,)
    assert Route.combine([Route.ROUTINE]) == (Route.ROUTINE,)


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


def test_unanswered_slot_reads_as_empty_at_zero_confidence() -> None:
    """A slot nobody asked must not read as a `false` answer to a rule."""
    empty = make_extraction().slot("tolerating_fluids")
    assert empty.value is None
    assert empty.confidence == 0.0


def test_slot_lookup_returns_the_recorded_answer() -> None:
    extraction = make_extraction(
        slot_values={"tolerating_fluids": SlotValue(value=False, confidence=0.9)}
    )
    assert extraction.slot("tolerating_fluids").value is False
    assert extraction.slot("never_asked").value is None


# --- Rule bands -----------------------------------------------------------


def test_band_implies_its_tier() -> None:
    """The loader checks a rule's declared tier against this, so they cannot drift."""
    assert RuleBand.RED.implied_tier is Tier.TIER_1
    assert RuleBand.YELLOW.implied_tier is Tier.TIER_2
    assert RuleBand.GREEN.implied_tier is Tier.TIER_3


def test_only_red_halts_the_conversation() -> None:
    assert RuleBand.RED.halts_conversation
    assert not RuleBand.YELLOW.halts_conversation
    assert not RuleBand.GREEN.halts_conversation


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
        routes=[Route.CALL_SURGEON],
        evidence=["fever present (mild)", "temperature_f=100.9"],
        quotes=["I feel warm and the dressing is damp"],
        turn_index=3,
        escalation_template_key="call_surgeon_infection",
    )


def test_finding_round_trips_through_json() -> None:
    finding = make_finding()
    assert Finding.model_validate_json(finding.model_dump_json()) == finding


def test_finding_must_route_somewhere() -> None:
    """A rule that fired and told no one is a rule that did nothing."""
    with pytest.raises(ValidationError):
        Finding(
            rule_id="surgical_bleeding",
            rules_version="postop_v1",
            label="Soaking through the dressing",
            severity=Severity.SEVERE,
            tier=Tier.TIER_1,
            routes=[],
        )


def test_finding_routes_are_stored_canonically() -> None:
    """Same instruction, same rows — order and duplicates must not persist."""
    base = make_finding().model_dump()
    reordered = Finding.model_validate(base | {"routes": [Route.CALL_SURGEON, Route.ED_NOW]})
    duplicated = Finding.model_validate(
        base | {"routes": [Route.ED_NOW, Route.CALL_SURGEON, Route.ED_NOW]}
    )
    assert reordered.routes == [Route.ED_NOW, Route.CALL_SURGEON]
    assert duplicated.routes == reordered.routes


def test_summary_round_trips_through_json() -> None:
    summary = Summary(
        conversation_id="conv_001",
        patient_ref="pt_abc",
        anesthesia_type=AnesthesiaType.SPINAL,
        block_type=BlockType.SPINAL,
        procedure="knee arthroscopy",
        status=ConversationStatus.ESCALATED,
        tier=Tier.TIER_2,
        routes=[Route.CALL_SURGEON],
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
    summary = make_summary(conversation_id="conv_002")
    assert summary.tier is Tier.TIER_3
    assert summary.routes == [Route.ROUTINE]
    assert summary.findings == []


def test_summary_keeps_the_surgeon_and_anesthesia_separate() -> None:
    """A bleeding wound and a stuck block owe two different people."""
    summary = make_summary(routes=[Route.CALL_ANESTHESIA, Route.CALL_SURGEON])
    assert set(summary.routes) == {Route.CALL_SURGEON, Route.CALL_ANESTHESIA}
