"""Shared vocabulary for the protocol, LLM, safety, triage and summary layers.

Every enum here is a `str` enum so values serialize unchanged into YAML rule
definitions, LLM tool schemas, JSON columns and SQLite. The string values are
therefore part of the stored data format — renaming one silently invalidates
existing rows and rule files. `tests/test_domain.py` pins the exact value sets.

The `SymptomCode` vocabulary below is a v1 draft pending clinician sign-off.
Per the versioning convention in CLAUDE.md, revisions ship as a new `_v2` set
alongside this one rather than as edits in place, since review is tied to a
specific version.
"""

from collections.abc import Iterable
from enum import Enum
from typing import Self


class AnesthesiaType(str, Enum):
    """How the patient was anesthetized. Selects protocol branches and rules."""

    GENERAL = "general"
    SPINAL = "spinal"
    EPIDURAL = "epidural"
    PERIPHERAL_NERVE_BLOCK = "peripheral_nerve_block"
    MAC_SEDATION = "mac_sedation"
    LOCAL = "local"


class Severity(str, Enum):
    """How bad a reported symptom is. Ordered via `rank`.

    A coarse ordinal, because most post-op symptoms have no validated self-report
    instrument — there is no reliable "nausea 7/10". Pain is the exception and is
    captured on its native 0-10 scale in `PainReport.score`; use `from_pain_score`
    to bring that number into this scale when a rule needs to treat pain uniformly
    with other symptoms.
    """

    NONE = "none"
    MILD = "mild"
    MODERATE = "moderate"
    SEVERE = "severe"

    @property
    def rank(self) -> int:
        """Ordinal for comparisons; higher is worse."""
        return _SEVERITY_ORDER[self]

    @classmethod
    def from_pain_score(cls, score: int) -> Self:
        """Band a 0-10 numeric rating scale score into a severity.

        Uses the conventional NRS bands (0 none, 1-3 mild, 4-6 moderate,
        7-10 severe). This is a lossy view of `PainReport.score`, never a
        replacement for it: store the number, derive the band.
        """
        if not 0 <= score <= 10:
            raise ValueError(f"pain score {score} is outside the 0-10 scale")
        if score == 0:
            return cls.NONE
        if score <= 3:
            return cls.MILD
        if score <= 6:
            return cls.MODERATE
        return cls.SEVERE


class Presence(str, Enum):
    """Whether a symptom was reported, denied, or never came up.

    The distinction between ABSENT and UNKNOWN is safety-critical: "the patient
    did not mention chest pain" is not "the patient denies chest pain". Rules
    that treat UNKNOWN as a denial will under-escalate.
    """

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class Trend(str, Enum):
    """Direction of change since the last check-in or since surgery."""

    BETTER = "better"
    UNCHANGED = "unchanged"
    WORSE = "worse"
    UNKNOWN = "unknown"


class MedAdherence(str, Enum):
    """Whether the patient is taking a medication as prescribed."""

    AS_PRESCRIBED = "as_prescribed"
    PARTIAL = "partial"
    NOT_TAKING = "not_taking"
    UNKNOWN = "unknown"


class Route(str, Enum):
    """What the patient is told to do. Keys the fixed copy in safety/templates.py.

    Distinct from `Tier`: Tier says how urgent a finding is, Route says what
    action it triggers. Ordered via `rank`.
    """

    SELF_CARE = "self_care"
    CALL_CLINIC = "call_clinic"
    URGENT_SAME_DAY = "urgent_same_day"
    EMERGENCY_911 = "emergency_911"

    @property
    def rank(self) -> int:
        """Ordinal for comparisons; higher demands faster action."""
        return _ROUTE_ORDER[self]

    @classmethod
    def most_urgent(cls, values: Iterable[Self]) -> Self:
        """The most demanding route in `values`, or SELF_CARE if empty."""
        return max(values, key=lambda route: route.rank, default=cls.SELF_CARE)


class Tier(str, Enum):
    """Triage urgency, following clinical convention where Tier 1 is worst.

    Note the inversion: `TIER_1` has the *highest* `rank` despite the lowest
    number. Compare tiers by `rank`, never by member name or value.
    """

    TIER_1 = "tier_1"
    """Emergent — escalate now."""

    TIER_2 = "tier_2"
    """Urgent — clinician review today."""

    TIER_3 = "tier_3"
    """Routine — no action needed."""

    @property
    def rank(self) -> int:
        """Ordinal for comparisons; higher is more urgent, so TIER_1 is highest."""
        return _TIER_ORDER[self]

    @classmethod
    def most_urgent(cls, values: Iterable[Self]) -> Self:
        """The most urgent tier in `values`, or TIER_3 if empty."""
        return max(values, key=lambda tier: tier.rank, default=cls.TIER_3)


class CheckinStatus(str, Enum):
    """Lifecycle of a single check-in conversation."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    ESCALATED = "escalated"


class SymptomCode(str, Enum):
    """Closed vocabulary the LLM extracts into and the safety rules match against.

    Closed on purpose: it bounds the model's output space and lets the rules
    loader reject a YAML rule that references a symptom nothing can ever report.
    """

    # Respiratory / airway
    SHORTNESS_OF_BREATH = "shortness_of_breath"
    SORE_THROAT = "sore_throat"
    HOARSENESS = "hoarseness"
    STRIDOR = "stridor"

    # Cardiovascular
    CHEST_PAIN = "chest_pain"
    PALPITATIONS = "palpitations"
    SYNCOPE = "syncope"
    DIZZINESS = "dizziness"

    # Neurologic / neuraxial
    POSTURAL_HEADACHE = "postural_headache"  # post-dural-puncture headache
    PERSISTENT_NUMBNESS = "persistent_numbness"  # block not resolving
    MOTOR_WEAKNESS = "motor_weakness"  # neuraxial hematoma concern
    BACK_PAIN_AT_INJECTION_SITE = "back_pain_at_injection_site"
    CONFUSION = "confusion"

    # Gastrointestinal
    NAUSEA = "nausea"
    VOMITING = "vomiting"
    UNABLE_TO_TOLERATE_FLUIDS = "unable_to_tolerate_fluids"
    CONSTIPATION = "constipation"

    # Genitourinary
    URINARY_RETENTION = "urinary_retention"

    # Infection / wound
    FEVER = "fever"
    CHILLS = "chills"
    WOUND_REDNESS = "wound_redness"
    WOUND_DRAINAGE = "wound_drainage"
    BLEEDING_AT_SITE = "bleeding_at_site"

    # Medication effects
    # Pain deliberately has no code here: it is captured on its native 0-10 scale
    # in `PainReport`, which is the single source of truth. A second, coarser
    # representation could disagree with the number, so rules read `pain.score`
    # (or `pain.severity`) instead.
    EXCESSIVE_SEDATION = "excessive_sedation"
    ITCHING = "itching"
    RASH = "rash"
    ALLERGIC_REACTION = "allergic_reaction"


_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.NONE: 0,
    Severity.MILD: 1,
    Severity.MODERATE: 2,
    Severity.SEVERE: 3,
}

_ROUTE_ORDER: dict[Route, int] = {
    Route.SELF_CARE: 0,
    Route.CALL_CLINIC: 1,
    Route.URGENT_SAME_DAY: 2,
    Route.EMERGENCY_911: 3,
}

_TIER_ORDER: dict[Tier, int] = {
    Tier.TIER_3: 0,
    Tier.TIER_2: 1,
    Tier.TIER_1: 2,
}
