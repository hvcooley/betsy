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
    """The patient's *primary* anesthetic technique. Selects protocol branches.

    Deliberately one technique, not a combination. A patient who had a general
    anesthetic with an interscalene block is `GENERAL` with `block_type` set to
    `BlockType.INTERSCALENE` — the block is recorded alongside, never fused into
    this enum. Enumerating combinations here would multiply the member list with
    every new block and still not express a third concurrent technique, and it
    would give the block-regression window two places to live.

    `PERIPHERAL_NERVE_BLOCK` means the block *was* the anesthetic (an awake case),
    which is a different clinical picture from a block used as an adjunct; both
    carry a `block_type`.
    """

    GENERAL = "general"
    SPINAL = "spinal"
    EPIDURAL = "epidural"
    COMBINED_SPINAL_EPIDURAL = "combined_spinal_epidural"
    PERIPHERAL_NERVE_BLOCK = "peripheral_nerve_block"
    MAC_SEDATION = "mac_sedation"
    LOCAL = "local"


class BlockType(str, Enum):
    """Which local-anesthetic block, if any, has to wear off.

    Orthogonal to `AnesthesiaType`: this says what needs to regress, independent
    of whether the block was the whole anesthetic or an adjunct to a general.
    `None` means no tracked block — a general or MAC on its own, or surgeon-placed
    local infiltration, neither of which has a regression window worth flagging.

    Closed vocabulary because it keys the block-regression windows in
    `app/safety/rules/postop_v1.yaml`, which drive `BLOCK_PROLONGED`. Adding a
    member without adding its window would let a prolonged block pass unflagged,
    so `tests/test_domain.py` requires the two to stay in step.

    `SPINAL` sits here as well as in `AnesthesiaType` on purpose — a spinal is a
    block that regresses on a clock like any other, and keeping it in the same
    lookup means one code path asks "has sensation come back yet?" for every
    patient who needs the question.
    """

    INTERSCALENE = "interscalene"
    SUPRACLAVICULAR = "supraclavicular"
    INFRACLAVICULAR = "infraclavicular"
    ADDUCTOR_CANAL = "adductor_canal"
    POPLITEAL_SCIATIC = "popliteal_sciatic"
    TAP = "tap"
    FIELD_BLOCK = "field_block"
    SPINAL = "spinal"


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


class RouteOwner(str, Enum):
    """Who has to act on a route.

    The second dimension of a disposition, and the one a single urgency ordinal
    cannot carry: soaking through a dressing and a block that will not wear off
    are both "call someone today", but they are not the same someone. The review
    dashboard sorts by this, so it has to be recoverable from a stored route.

    Derived from `Route` rather than stored beside it — see `Route.owner`. One
    route means exactly one owner; two routes are how a finding reaches two.
    """

    EMS = "ems"
    EMERGENCY_DEPT = "emergency_dept"
    SURGEON = "surgeon"
    ANESTHESIA = "anesthesia"
    PATIENT = "patient"


class Route(str, Enum):
    """One action a finding demands. Keys the fixed copy in safety/templates.py.

    Distinct from `Tier`: Tier says how urgent a finding is, Route says what
    action it triggers *and who owns it* (`owner`). Ordered by urgency via
    `rank`, which deliberately does **not** totally order the members —
    `CALL_SURGEON` and `CALL_ANESTHESIA` are equally urgent and differ only in
    owner. Collapsing them into one "call the clinic" member is the loss this
    vocabulary exists to prevent: bleeding is the surgeon's problem, a prolonged
    block is anesthesia's, and a dashboard that cannot tell them apart routes
    every escalation to the wrong pager.

    Because of that tie, a set of routes does not reduce to its maximum. Combine
    dispositions with `combine`, which keeps one route per owner; use
    `most_urgent` only to pick which single template the patient is shown.
    """

    CALL_911 = "call_911"
    ED_NOW = "ed_now"
    CALL_SURGEON = "call_surgeon"
    CALL_ANESTHESIA = "call_anesthesia"
    ROUTINE = "routine"

    @property
    def owner(self) -> RouteOwner:
        """Who has to act. Distinct owners survive `combine` independently."""
        return _ROUTE_OWNER[self]

    @property
    def rank(self) -> int:
        """Urgency ordinal; higher demands faster action. Ties are meaningful.

        `CALL_SURGEON` and `CALL_ANESTHESIA` share a rank because neither is
        more urgent than the other. Never use this to pick between them.
        """
        return _ROUTE_ORDER[self]

    @classmethod
    def combine(cls, values: Iterable[Self]) -> tuple[Self, ...]:
        """Every distinct action `values` demands, worst first.

        The reduction is per owner, not global: the most urgent route for each
        owner survives, so `{CALL_SURGEON, ED_NOW}` (soaking dressing — go in,
        and the surgeon needs to know) stays two routes, while
        `{CALL_ANESTHESIA, CALL_ANESTHESIA}` is one. `ROUTINE` drops out as soon
        as anything else fired, since "no action needed" alongside an action is
        noise; an empty input is `(ROUTINE,)`, so the default disposition is
        always the least urgent one.

        Ties within a rank are ordered by declaration order. That is a
        presentation choice for the dashboard, never a clinical claim that one
        owner outranks the other.
        """
        by_owner: dict[RouteOwner, Self] = {}
        for route in values:
            incumbent = by_owner.get(route.owner)
            if incumbent is None or route.rank > incumbent.rank:
                by_owner[route.owner] = route
        actionable = [route for route in by_owner.values() if route is not cls.ROUTINE]
        if not actionable:
            return (cls.ROUTINE,)
        return tuple(sorted(actionable, key=lambda route: (-route.rank, _ROUTE_TIEBREAK[route])))

    @classmethod
    def most_urgent(cls, values: Iterable[Self]) -> Self:
        """The single route whose copy the patient is shown, or ROUTINE if empty.

        For patient-facing text only, where one message has to be chosen. Any
        caller deciding *who gets told* must use `combine` instead — this drops
        every route but one, and the one it keeps on a tie is arbitrary.
        """
        return cls.combine(values)[0]


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


class ConversationStatus(str, Enum):
    """Lifecycle of a single conversation — one check-in call within a case.

    A `case` is the durable per-patient episode; a `case` can have many
    `conversation`s (e.g. a POD1 call and a separate POD3 call), each with its
    own lifecycle tracked here. See the `checkin` vs `case`/`conversation`
    naming divergence in docs/README.md.
    """

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

_ROUTE_OWNER: dict[Route, RouteOwner] = {
    Route.CALL_911: RouteOwner.EMS,
    Route.ED_NOW: RouteOwner.EMERGENCY_DEPT,
    Route.CALL_SURGEON: RouteOwner.SURGEON,
    Route.CALL_ANESTHESIA: RouteOwner.ANESTHESIA,
    Route.ROUTINE: RouteOwner.PATIENT,
}

# Urgency only. CALL_SURGEON and CALL_ANESTHESIA tie on purpose: they are the
# same clinical urgency and differ only in `owner`, so anything comparing them
# by rank is asking the wrong question.
_ROUTE_ORDER: dict[Route, int] = {
    Route.ROUTINE: 0,
    Route.CALL_SURGEON: 1,
    Route.CALL_ANESTHESIA: 1,
    Route.ED_NOW: 2,
    Route.CALL_911: 3,
}

# Display tiebreak for equally urgent routes. Presentation, not clinical order.
_ROUTE_TIEBREAK: dict[Route, int] = {route: index for index, route in enumerate(Route)}

_TIER_ORDER: dict[Tier, int] = {
    Tier.TIER_3: 0,
    Tier.TIER_2: 1,
    Tier.TIER_1: 2,
}
