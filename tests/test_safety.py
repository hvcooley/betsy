"""The rule engine is the product. These tests are the clinical contract.

Two families of test here, and the second matters as much as the first. The firing
tests say each rule catches what it is supposed to catch. The fail-closed tests say
no rule can be fooled by an absence: a symptom nobody asked about, a slot nobody
answered, or a negation over a missing value. Escalation logic that under-fires on
silence is the failure mode this whole architecture exists to prevent.
"""

from __future__ import annotations

import pytest

from app.domain.enums import (
    AnesthesiaType,
    BlockType,
    MedAdherence,
    Presence,
    Route,
    RuleBand,
    Severity,
    SymptomCode,
    Tier,
    Trend,
)
from app.domain.schemas import (
    MedicationReport,
    PainReport,
    SlotValue,
    SymptomObservation,
    TurnExtraction,
)
from app.protocol.engine import CaseFacts
from app.protocol.loader import load_default_protocol
from app.safety import templates
from app.safety.rules import RuleEngine, RuleSet, gate, load_rules

GA_CASE = CaseFacts(anesthesia_type=AnesthesiaType.GENERAL, hours_post_op=20)
BLOCK_CASE = CaseFacts(
    anesthesia_type=AnesthesiaType.GENERAL, block_type=BlockType.INTERSCALENE, hours_post_op=20
)
SPINAL_CASE = CaseFacts(
    anesthesia_type=AnesthesiaType.SPINAL, block_type=BlockType.SPINAL, hours_post_op=20
)
LOCAL_CASE = CaseFacts(anesthesia_type=AnesthesiaType.LOCAL, hours_post_op=20)


@pytest.fixture(scope="module")
def rule_set() -> RuleSet:
    return load_rules()


@pytest.fixture(scope="module")
def rules(rule_set: RuleSet) -> RuleEngine:
    return RuleEngine(rule_set)


def present(code: SymptomCode, severity: Severity | None = None, **kwargs: object) -> SymptomObservation:
    return SymptomObservation(code=code, presence=Presence.PRESENT, severity=severity, **kwargs)


def absent(code: SymptomCode) -> SymptomObservation:
    return SymptomObservation(code=code, presence=Presence.ABSENT)


def turn(**overrides: object) -> TurnExtraction:
    defaults: dict[str, object] = {
        "protocol_version": "postop_v1",
        "prompt_version": "v1",
        "topic_id": "pain",
        "turn_index": 1,
        "raw_message": "…",
        "extraction_confidence": 0.9,
    }
    if "slot_values" in overrides:
        overrides["slot_values"] = {
            key: value if isinstance(value, SlotValue) else SlotValue(value=value, confidence=0.9)
            for key, value in overrides["slot_values"].items()  # type: ignore[union-attr]
        }
    return TurnExtraction(**(defaults | overrides))


def fired(engine: RuleEngine, extraction: TurnExtraction, case: CaseFacts = GA_CASE) -> set[str]:
    """Rule ids that fire with every rule in scope, so scoping is not what is tested."""
    every_rule = list(engine.rule_set.by_id)
    return {finding.rule_id for finding in engine.evaluate(extraction, case, topic_rules=every_rule)}


# --- The shipped rule file is coherent ------------------------------------


def test_the_rule_file_loads(rule_set: RuleSet) -> None:
    assert rule_set.rules_version == "postop_v1"
    assert rule_set.rules


def test_every_red_rule_from_the_spec_exists(rule_set: RuleSet) -> None:
    """The spec's RED table is a floor, not a suggestion."""
    required = {
        "RESP_DISTRESS",
        "CHEST_PAIN",
        "LAST_SYMPTOMS",
        "NEURAXIAL_HEMATOMA",
        "COMPARTMENT_SYNDROME",
        "PDPH_SUSPECTED",
        "PONV_INTRACTABLE",
        "OVERSEDATION",
        "ANAPHYLAXIS_LATE",
        "MH_SUSPECTED",
        "DVT_SUSPECTED",
        "SURGICAL_BLEEDING",
        "SUICIDAL_IDEATION",
    }
    red = {rule.id for rule in rule_set.rules if rule.band is RuleBand.RED}
    assert required <= red


def test_every_red_rule_has_fixed_copy(rule_set: RuleSet) -> None:
    """A red flag discards the drafted reply, so there must be something to send instead."""
    for rule in rule_set.rules:
        if rule.band is not RuleBand.RED:
            continue
        assert rule.template_key, f"{rule.id} is RED with no template"
        assert rule.template_key in templates.TEMPLATES, f"{rule.id} names a missing template"


def test_every_template_matches_the_route_of_the_rule_that_uses_it(rule_set: RuleSet) -> None:
    """Copy telling a patient to call 911 must not be attached to a call-the-surgeon rule."""
    for rule in rule_set.rules:
        template = templates.TEMPLATES.get(rule.template_key or "")
        if template is None:
            continue
        assert set(template.routes) <= set(rule.routes), f"{rule.id} copy contradicts its routes"


def test_tier_cannot_disagree_with_band(rule_set: RuleSet) -> None:
    for rule in rule_set.rules:
        assert rule.tier is rule.band.implied_tier


def test_no_rule_is_signed_off_yet(rule_set: RuleSet) -> None:
    """A guard, not an aspiration: flipping these to true is a clinician's act.

    When the SME signs the file off this test is what has to be deliberately changed,
    which is the point — it makes review status a property of the repo rather than a
    thing someone remembers.
    """
    assert all(not rule.sme_reviewed for rule in rule_set.rules)
    assert templates.unreviewed_template_keys() == sorted(templates.TEMPLATES)


def test_every_block_type_still_has_a_window(rule_set: RuleSet) -> None:
    assert set(rule_set.block_regression) == set(BlockType)


# --- RED rules fire -------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_id", "extraction", "case"),
    [
        (
            "RESP_DISTRESS",
            turn(symptoms=[present(SymptomCode.SHORTNESS_OF_BREATH, Severity.SEVERE)]),
            GA_CASE,
        ),
        ("RESP_DISTRESS", turn(symptoms=[present(SymptomCode.STRIDOR)]), GA_CASE),
        ("CHEST_PAIN", turn(symptoms=[present(SymptomCode.CHEST_PAIN)]), GA_CASE),
        (
            "CHEST_PAIN",
            turn(symptoms=[present(SymptomCode.PALPITATIONS), present(SymptomCode.SYNCOPE)]),
            GA_CASE,
        ),
        (
            "LAST_SYMPTOMS",
            turn(symptoms=[present(SymptomCode.PERIORAL_NUMBNESS)]),
            BLOCK_CASE,
        ),
        (
            "LAST_SYMPTOMS",
            turn(symptoms=[present(SymptomCode.METALLIC_TASTE)]),
            BLOCK_CASE,
        ),
        (
            "NEURAXIAL_HEMATOMA",
            turn(symptoms=[present(SymptomCode.MOTOR_WEAKNESS)]),
            SPINAL_CASE,
        ),
        (
            "NEURAXIAL_HEMATOMA",
            turn(slot_values={"bladder_function": "unable"}),
            SPINAL_CASE,
        ),
        (
            "COMPARTMENT_SYNDROME",
            turn(symptoms=[present(SymptomCode.PAIN_ON_PASSIVE_STRETCH)]),
            GA_CASE,
        ),
        (
            "COMPARTMENT_SYNDROME",
            turn(
                pain=PainReport(score=9, controlled_by_medication=False),
                symptoms=[present(SymptomCode.LIMB_TIGHTNESS)],
            ),
            GA_CASE,
        ),
        (
            "PDPH_SUSPECTED",
            turn(symptoms=[present(SymptomCode.POSTURAL_HEADACHE)]),
            SPINAL_CASE,
        ),
        (
            "PDPH_WITH_RED_FLAGS",
            turn(
                symptoms=[
                    present(SymptomCode.POSTURAL_HEADACHE),
                    present(SymptomCode.VISUAL_CHANGES),
                ]
            ),
            SPINAL_CASE,
        ),
        (
            "PONV_INTRACTABLE",
            turn(
                symptoms=[present(SymptomCode.VOMITING)],
                slot_values={"tolerating_fluids": False},
            ),
            GA_CASE,
        ),
        ("PONV_INTRACTABLE", turn(slot_values={"last_urination": 14.0}), GA_CASE),
        (
            "OVERSEDATION",
            turn(symptoms=[present(SymptomCode.EXCESSIVE_SEDATION, Severity.SEVERE)]),
            GA_CASE,
        ),
        ("ANAPHYLAXIS_LATE", turn(symptoms=[present(SymptomCode.TONGUE_SWELLING)]), GA_CASE),
        (
            "MH_SUSPECTED",
            turn(symptoms=[present(SymptomCode.MUSCLE_RIGIDITY)], temperature_f=101.6),
            GA_CASE,
        ),
        (
            "DVT_SUSPECTED",
            turn(symptoms=[present(SymptomCode.CALF_PAIN), present(SymptomCode.CALF_SWELLING)]),
            GA_CASE,
        ),
        ("SURGICAL_BLEEDING", turn(symptoms=[present(SymptomCode.DRESSING_SOAKED)]), GA_CASE),
        (
            "SUICIDAL_IDEATION",
            turn(symptoms=[present(SymptomCode.SUICIDAL_IDEATION)]),
            GA_CASE,
        ),
        ("SYNCOPE", turn(slot_values={"lightheaded_syncope": True}), GA_CASE),
    ],
)
def test_red_rule_fires(
    rules: RuleEngine, rule_id: str, extraction: TurnExtraction, case: CaseFacts
) -> None:
    assert rule_id in fired(rules, extraction, case)


# --- YELLOW rules fire ----------------------------------------------------


@pytest.mark.parametrize(
    ("rule_id", "extraction", "case"),
    [
        (
            "PAIN_SEVERE_UNRESPONSIVE",
            turn(
                pain=PainReport(score=9, controlled_by_medication=False),
                slot_values={"pain_med_taken": True},
            ),
            GA_CASE,
        ),
        ("PAIN_ATYPICAL_SITE", turn(slot_values={"pain_location_expected": False}), GA_CASE),
        (
            "BLOCK_PROLONGED",
            turn(slot_values={"hours_since_block": 30.0, "sensation_returning": False}),
            BLOCK_CASE,
        ),
        (
            "BLOCK_NEW_DEFICIT",
            turn(slot_values={"sensation_returning": True, "motor_function": "weak"}),
            BLOCK_CASE,
        ),
        (
            "CATHETER_SITE_INFECTION",
            turn(slot_values={"catheter_site_status": "draining"}),
            BLOCK_CASE,
        ),
        (
            "PHRENIC_DYSPNEA",
            turn(symptoms=[present(SymptomCode.SHORTNESS_OF_BREATH, Severity.MILD)]),
            BLOCK_CASE,
        ),
        (
            "REBOUND_PAIN",
            turn(pain=PainReport(score=8), slot_values={"rebound_pain": True}),
            BLOCK_CASE,
        ),
        ("URINARY_RETENTION", turn(slot_values={"bladder_function": "difficulty"}), SPINAL_CASE),
        (
            "APAP_STACKING",
            turn(
                medications=[
                    MedicationReport(name="Tylenol"),
                    MedicationReport(name="Percocet"),
                ]
            ),
            GA_CASE,
        ),
        (
            "MED_NONADHERENCE",
            turn(medications=[MedicationReport(name="oxycodone", adherence=MedAdherence.NOT_TAKING)]),
            GA_CASE,
        ),
        (
            "OPIOID_SEDATIVE_COMBO",
            turn(
                medications=[
                    MedicationReport(name="oxycodone"),
                    MedicationReport(name="Ambien"),
                ]
            ),
            GA_CASE,
        ),
        (
            "POSTOP_DELIRIUM",
            turn(symptoms=[present(SymptomCode.CONFUSION, Severity.MODERATE)]),
            # Beyond POD1: on the day of surgery this same report is EXPECTED_DAY0_GROGGINESS.
            CaseFacts(anesthesia_type=AnesthesiaType.GENERAL, hours_post_op=30),
        ),
        ("DENTAL_INJURY", turn(symptoms=[present(SymptomCode.DENTAL_INJURY)]), GA_CASE),
        (
            "AIRWAY_INJURY",
            turn(symptoms=[present(SymptomCode.SORE_THROAT, Severity.SEVERE)]),
            GA_CASE,
        ),
        ("CORNEAL_ABRASION", turn(symptoms=[present(SymptomCode.EYE_IRRITATION)]), GA_CASE),
        ("DEHYDRATION_RISK", turn(slot_values={"last_urination": 9.0}), GA_CASE),
        ("PERSISTENT_LOCAL", turn(slot_values={"persistent_numbness": True}), LOCAL_CASE),
    ],
)
def test_yellow_rule_fires(
    rules: RuleEngine, rule_id: str, extraction: TurnExtraction, case: CaseFacts
) -> None:
    assert rule_id in fired(rules, extraction, case)


# --- Rules fail closed ----------------------------------------------------


def test_an_empty_turn_fires_nothing(rules: RuleEngine) -> None:
    """The single most important test here: silence is not evidence of anything."""
    assert fired(rules, turn()) == set()


def test_an_unmentioned_symptom_does_not_satisfy_a_negation(rules: RuleEngine) -> None:
    """`not present` must not be true of a patient nobody asked."""
    green = rules.green_matches(
        turn(symptoms=[present(SymptomCode.SORE_THROAT, Severity.MILD)]),
        GA_CASE,
        topic_rules=["EXPECTED_SORE_THROAT"],
    )
    assert [rule.id for rule in green] == ["EXPECTED_SORE_THROAT"]

    # Same turn, but the patient also reports trouble swallowing: the negation in the
    # green rule now excludes it, and the yellow airway rule takes over instead.
    worse = turn(
        symptoms=[
            present(SymptomCode.SORE_THROAT, Severity.MILD),
            present(SymptomCode.DIFFICULTY_SWALLOWING),
        ]
    )
    assert rules.green_matches(worse, GA_CASE, topic_rules=["EXPECTED_SORE_THROAT"]) == []
    assert "AIRWAY_INJURY" in fired(rules, worse)


def test_a_denied_symptom_is_not_a_present_one(rules: RuleEngine) -> None:
    assert "CHEST_PAIN" not in fired(rules, turn(symptoms=[absent(SymptomCode.CHEST_PAIN)]))


def test_an_unanswered_slot_does_not_read_as_false(rules: RuleEngine) -> None:
    """`tolerating_fluids: is_false` must not fire because nobody asked about fluids."""
    vomiting_only = turn(symptoms=[present(SymptomCode.VOMITING)])
    assert "PONV_INTRACTABLE" not in fired(rules, vomiting_only)


def test_a_present_but_ungraded_symptom_does_not_meet_a_severity_floor(rules: RuleEngine) -> None:
    """The finding would cite a severity the extraction never established."""
    assert "RESP_DISTRESS" not in fired(
        rules, turn(symptoms=[present(SymptomCode.SHORTNESS_OF_BREATH)])
    )
    assert "RESP_DISTRESS" in fired(
        rules, turn(symptoms=[present(SymptomCode.SHORTNESS_OF_BREATH, Severity.MODERATE)])
    )


def test_pain_predicates_need_an_actual_score(rules: RuleEngine) -> None:
    assert "PAIN_SEVERE_UNRESPONSIVE" not in fired(
        rules, turn(pain=PainReport(trend=Trend.WORSE), slot_values={"pain_med_taken": True})
    )


def test_unknown_medication_adherence_is_not_nonadherence(rules: RuleEngine) -> None:
    assert "MED_NONADHERENCE" not in fired(
        rules, turn(medications=[MedicationReport(name="oxycodone")])
    )


def test_a_block_rule_cannot_fire_without_a_block(rules: RuleEngine) -> None:
    stuck = turn(slot_values={"hours_since_block": 40.0, "sensation_returning": False})
    assert "BLOCK_PROLONGED" not in fired(rules, stuck, GA_CASE)
    assert "BLOCK_PROLONGED" in fired(rules, stuck, BLOCK_CASE)


def test_a_neuraxial_rule_cannot_fire_on_a_general(rules: RuleEngine) -> None:
    weak = turn(symptoms=[present(SymptomCode.MOTOR_WEAKNESS)])
    assert "NEURAXIAL_HEMATOMA" not in fired(rules, weak, GA_CASE)
    assert "NEURAXIAL_HEMATOMA" in fired(rules, weak, SPINAL_CASE)


# --- Block regression is a data lookup ------------------------------------


@pytest.mark.parametrize(
    ("block_type", "inside", "outside"),
    [
        (BlockType.INTERSCALENE, 20.0, 26.0),
        (BlockType.SPINAL, 6.0, 10.0),
        (BlockType.POPLITEAL_SCIATIC, 30.0, 40.0),
        (BlockType.TAP, 20.0, 26.0),
    ],
)
def test_block_prolonged_uses_the_window_for_that_block(
    rules: RuleEngine, block_type: BlockType, inside: float, outside: float
) -> None:
    """Different blocks flag at different hours because the table says so, not the code."""
    case = CaseFacts(anesthesia_type=AnesthesiaType.PERIPHERAL_NERVE_BLOCK, block_type=block_type)
    still_numb = {"sensation_returning": False}
    assert "BLOCK_PROLONGED" not in fired(
        rules, turn(slot_values={**still_numb, "hours_since_block": inside}), case
    )
    assert "BLOCK_PROLONGED" in fired(
        rules, turn(slot_values={**still_numb, "hours_since_block": outside}), case
    )


def test_a_per_case_duration_override_beats_the_default_window(rules: RuleEngine) -> None:
    """The case-level override exists for adjuvants and unusual agents."""
    case = CaseFacts(
        anesthesia_type=AnesthesiaType.PERIPHERAL_NERVE_BLOCK,
        block_type=BlockType.INTERSCALENE,
        expected_block_duration_hours=48,
    )
    late = turn(slot_values={"sensation_returning": False, "hours_since_block": 30.0})
    assert "BLOCK_PROLONGED" not in fired(rules, late, case)


# --- Routing --------------------------------------------------------------


def test_a_rule_can_owe_two_different_people(rules: RuleEngine) -> None:
    """Bleeding is ED_NOW *and* CALL_SURGEON; reducing it to one drops an owner."""
    findings = rules.evaluate(
        turn(symptoms=[present(SymptomCode.DRESSING_SOAKED)]),
        GA_CASE,
        topic_rules=["SURGICAL_BLEEDING"],
    )
    assert [finding.rule_id for finding in findings] == ["SURGICAL_BLEEDING"]
    assert set(findings[0].routes) == {Route.ED_NOW, Route.CALL_SURGEON}


def test_findings_come_back_most_urgent_first(rules: RuleEngine) -> None:
    findings = rules.evaluate(
        turn(
            symptoms=[present(SymptomCode.CHEST_PAIN), present(SymptomCode.EYE_IRRITATION)],
        ),
        GA_CASE,
        topic_rules=["CHEST_PAIN", "CORNEAL_ABRASION"],
    )
    assert [finding.rule_id for finding in findings] == ["CHEST_PAIN", "CORNEAL_ABRASION"]


def test_findings_carry_the_evidence_that_fired_them(rules: RuleEngine) -> None:
    """A finding a clinician cannot audit is a finding they will not trust."""
    findings = rules.evaluate(
        turn(
            symptoms=[
                SymptomObservation(
                    code=SymptomCode.CHEST_PAIN,
                    presence=Presence.PRESENT,
                    quote="it feels like something's sitting on my chest",
                )
            ]
        ),
        GA_CASE,
        topic_rules=["CHEST_PAIN"],
    )
    assert "chest_pain" in " ".join(findings[0].evidence)
    assert findings[0].quotes == ["it feels like something's sitting on my chest"]


# --- Scoping: global rules are always live --------------------------------


def test_global_rules_fire_outside_their_topic(rules: RuleEngine) -> None:
    """A patient volunteering chest pain during the survey cannot wait for cardioresp."""
    protocol = load_default_protocol()
    satisfaction = protocol.topic("satisfaction")
    assert satisfaction.rules == []

    findings = rules.evaluate(
        turn(topic_id="satisfaction", symptoms=[present(SymptomCode.CHEST_PAIN)]),
        GA_CASE,
        topic_rules=satisfaction.rules,
    )
    assert [finding.rule_id for finding in findings] == ["CHEST_PAIN"]


def test_a_topic_scoped_rule_does_not_fire_elsewhere(rules: RuleEngine) -> None:
    """Scoping still means something — only the always-live set is global."""
    eye = turn(topic_id="pain", symptoms=[present(SymptomCode.EYE_IRRITATION)])
    assert rules.evaluate(eye, GA_CASE, topic_rules=["PAIN_ATYPICAL_SITE"]) == []
    assert rules.evaluate(eye, GA_CASE, topic_rules=["CORNEAL_ABRASION"])


def test_every_global_rule_is_red(rule_set: RuleSet) -> None:
    """Always-live is justified by catastrophe, not by convenience."""
    by_id = rule_set.by_id
    assert all(by_id[rule_id].band is RuleBand.RED for rule_id in rule_set.global_rules)


# --- Green rules never reach the queue ------------------------------------


def test_green_matches_produce_no_finding(rules: RuleEngine) -> None:
    mild = turn(symptoms=[present(SymptomCode.SORE_THROAT, Severity.MILD)])
    assert rules.evaluate(mild, GA_CASE, topic_rules=["EXPECTED_SORE_THROAT"]) == []
    assert [rule.id for rule in rules.green_matches(mild, GA_CASE, topic_rules=["EXPECTED_SORE_THROAT"])] == [
        "EXPECTED_SORE_THROAT"
    ]


def test_every_green_rule_has_reassurance_wording(rule_set: RuleSet) -> None:
    for rule in rule_set.rules:
        if rule.band is RuleBand.GREEN:
            assert rule.template_key in templates.GREEN_REASSURANCE, rule.id


def test_the_same_symptom_is_green_on_day_zero_and_yellow_after(rules: RuleEngine) -> None:
    """Time since surgery is what separates expected grogginess from delirium."""
    groggy = turn(symptoms=[present(SymptomCode.CONFUSION, Severity.MILD)])
    day_zero = CaseFacts(anesthesia_type=AnesthesiaType.GENERAL, hours_post_op=8)
    day_two = CaseFacts(anesthesia_type=AnesthesiaType.GENERAL, hours_post_op=40)

    assert fired(rules, groggy, day_zero) == set()
    assert [rule.id for rule in rules.green_matches(
        groggy, day_zero, topic_rules=["EXPECTED_DAY0_GROGGINESS"]
    )] == ["EXPECTED_DAY0_GROGGINESS"]
    assert "POSTOP_DELIRIUM" in fired(rules, groggy, day_two)


# --- The safety gate ------------------------------------------------------


def test_the_gate_is_a_function_of_findings_alone(rules: RuleEngine) -> None:
    assert gate([]) == "none"
    assert gate(rules.evaluate(turn(symptoms=[present(SymptomCode.CHEST_PAIN)]), GA_CASE,
                               topic_rules=["CHEST_PAIN"])) == "red"
    assert gate(rules.evaluate(turn(symptoms=[present(SymptomCode.EYE_IRRITATION)]), GA_CASE,
                               topic_rules=["CORNEAL_ABRASION"])) == "yellow"


def test_escalation_copy_is_fixed_text_for_the_rule_that_fired(rules: RuleEngine) -> None:
    findings = rules.evaluate(
        turn(symptoms=[present(SymptomCode.CHEST_PAIN)]), GA_CASE, topic_rules=["CHEST_PAIN"]
    )
    copy = templates.escalation_copy(findings[0])
    assert "911" in copy.text
    assert not copy.sme_reviewed


def test_escalation_copy_never_returns_nothing(rules: RuleEngine) -> None:
    """Silence is the one unacceptable outcome once a red flag has fired."""
    findings = rules.evaluate(
        turn(symptoms=[present(SymptomCode.EYE_IRRITATION)]), GA_CASE, topic_rules=["CORNEAL_ABRASION"]
    )
    assert templates.escalation_copy(findings[0]).text


def test_red_findings_are_tier_1(rules: RuleEngine) -> None:
    findings = rules.evaluate(
        turn(symptoms=[present(SymptomCode.CHEST_PAIN)]), GA_CASE, topic_rules=["CHEST_PAIN"]
    )
    assert findings[0].tier is Tier.TIER_1
