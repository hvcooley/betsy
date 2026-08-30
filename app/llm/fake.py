"""Deterministic stand-ins for the turn engine. **Test doubles, never a fallback.**

Nothing in this module may ever run in front of a patient. It exists so the whole
deterministic pipeline — protocol engine, rule engine, safety gate, tiering, summary —
can be run, replayed and asserted with no API key and no network, which is the only way
to test the seams between those layers rather than each of them alone.

Two doubles, for two jobs:

`ScriptedTurnEngine` replays a hand-authored `TurnExtraction` per turn. It is what the
functional tests and the scenario files use, because it takes the model's judgement out
of the picture entirely: a scenario asserts what the *deterministic* layers do with a
given extraction, and cannot fail because a paraphrase drifted.

`KeywordTurnEngine` parses free text with a keyword table so a developer can type at a
prompt and watch the state machine move. It is a crude, credulous extractor — no
inference, no ambiguity handling, and it will mis-read anything phrased sideways. That
is acceptable in a REPL and unacceptable anywhere else.

Both draft replies from the protocol YAML rather than inventing prose:
`opening_question` is designated there as the literal fallback when an LLM call fails,
and `prompt_hint` says what a follow-up is trying to learn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.domain.enums import Presence, Severity, SymptomCode, Trend
from app.domain.schemas import SlotValue, TurnExtraction
from app.llm.turn import TurnDraft, TurnRequest
from app.protocol.engine import is_filled
from app.protocol.loader import Protocol, Slot, Topic, flatten

# --- Reply drafting ---------------------------------------------------------


def draft_reply(
    protocol: Protocol,
    topic: Topic,
    filled: dict[str, SlotValue],
) -> str:
    """The next question to ask inside `topic`, given what is already answered.

    Only ever a *within-topic* follow-up. Moving to a new topic re-opens with that
    topic's `opening_question`, and `app/conversation/pipeline.py` owns that decision,
    because at the moment this runs the turn has not been folded into the state yet
    and so nothing here knows whether the topic is about to close.
    """
    for slot in topic.required_slots:
        if is_filled(slot, filled.get(slot.id), protocol.slot_confidence_threshold):
            continue
        return _as_question(slot, topic)
    return flatten(topic.opening_question)


def _as_question(slot: Slot, topic: Topic) -> str:
    """Turn a slot's `prompt_hint` into something addressed to a patient.

    A `prompt_hint` is written for the model — "has the person given a name and date
    of birth matching the case" — so it cannot be sent as-is. Survey questions
    expanded out of a question set are the exception: their hint *is* the patient-
    facing wording, and already ends in a question mark. Both readings are obvious
    scaffolding, which is the point: a real turn engine phrases this, and it should be
    visible in a transcript when one is not.
    """
    if slot.prompt_hint is None:
        return flatten(topic.opening_question)
    hint = flatten(slot.prompt_hint)
    if hint.endswith("?"):
        return hint
    # First sentence only. Several hints carry an instruction to the model after the
    # thing being asked — "…would be expected to hurt. Judge against the procedure on
    # file" — and only the first half is a question anyone could answer.
    return f"Can you tell me — {hint.split('. ')[0].rstrip('.')}?"


# --- Scripted double --------------------------------------------------------


@dataclass(frozen=True)
class ScriptedTurn:
    """One authored patient turn: what they said, and what it means."""

    say: str
    extract: dict[str, Any] = field(default_factory=dict)
    hard_failure: bool = False
    reply: str | None = None


DEFAULT_SCRIPTED_CONFIDENCE = 0.9


class ScriptedTurnEngine:
    """Replays authored extractions in order. Implements `TurnEngine`."""

    def __init__(self, script: list[ScriptedTurn]) -> None:
        self.script = script

    def analyze(self, request: TurnRequest) -> TurnDraft:
        if request.turn_index >= len(self.script):
            raise IndexError(
                f"scripted turn {request.turn_index} was requested but the script holds "
                f"{len(self.script)}; the conversation ran longer than the scenario"
            )
        turn = self.script[request.turn_index]

        if turn.hard_failure:
            # The terminal rung of the retry ladder. No extraction, but the pipeline
            # still writes a turn record for it — that is the whole point of the case.
            return TurnDraft(
                extraction=None,
                draft_reply=turn.reply or flatten(request.topic.opening_question),
                hard_failure=True,
                validation_retries=2,
                model="scripted",
                raw_response=turn.say,
            )

        extraction = TurnExtraction.model_validate(
            _with_defaults(turn.extract) | request.provenance
        )
        reply = turn.reply or draft_reply(
            request.protocol,
            request.topic,
            request.state.slot_values | extraction.slot_values,
        )
        return TurnDraft(extraction=extraction, draft_reply=reply, model="scripted")


def _with_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    """Fill in the confidences a scenario author should not have to keep restating.

    `TurnExtraction.extraction_confidence` and `SlotValue.confidence` both default to
    0.0, which is below every sane threshold — so an authored turn that omitted them
    would silently fill no slots and mark its topic low-confidence, and the scenario
    would fail for a reason that has nothing to do with what it was written to test.
    A scenario exercising low confidence states the number explicitly.
    """
    filled = dict(payload)
    confidence = filled.setdefault("extraction_confidence", DEFAULT_SCRIPTED_CONFIDENCE)
    slot_values = filled.get("slot_values")
    if isinstance(slot_values, dict):
        filled["slot_values"] = {
            slot_id: (
                {"confidence": confidence, **answer}
                if isinstance(answer, dict)
                else answer
            )
            for slot_id, answer in slot_values.items()
        }
    return filled


# --- Keyword double ---------------------------------------------------------

# Phrase -> symptom. Ordered longest-first at match time so "chest pain" is not read
# as an unqualified pain mention. Covers what the shipped rules can act on; anything
# absent here is simply never extracted, which fails closed (UNKNOWN, not ABSENT).
SYMPTOM_PHRASES: dict[str, SymptomCode] = {
    "short of breath": SymptomCode.SHORTNESS_OF_BREATH,
    "shortness of breath": SymptomCode.SHORTNESS_OF_BREATH,
    "trouble breathing": SymptomCode.SHORTNESS_OF_BREATH,
    "can't breathe": SymptomCode.SHORTNESS_OF_BREATH,
    "breathing": SymptomCode.SHORTNESS_OF_BREATH,
    "sore throat": SymptomCode.SORE_THROAT,
    "throat hurts": SymptomCode.SORE_THROAT,
    "hoarse": SymptomCode.HOARSENESS,
    "croaky": SymptomCode.HOARSENESS,
    "noisy breathing": SymptomCode.STRIDOR,
    "stridor": SymptomCode.STRIDOR,
    "trouble swallowing": SymptomCode.DIFFICULTY_SWALLOWING,
    "hard to swallow": SymptomCode.DIFFICULTY_SWALLOWING,
    "drooling": SymptomCode.DROOLING,
    "face is swollen": SymptomCode.FACIAL_SWELLING,
    "facial swelling": SymptomCode.FACIAL_SWELLING,
    "tongue is swollen": SymptomCode.TONGUE_SWELLING,
    "chest pain": SymptomCode.CHEST_PAIN,
    "chest tightness": SymptomCode.CHEST_PAIN,
    "pressure in my chest": SymptomCode.CHEST_PAIN,
    "palpitations": SymptomCode.PALPITATIONS,
    "heart racing": SymptomCode.PALPITATIONS,
    "fainted": SymptomCode.SYNCOPE,
    "passed out": SymptomCode.SYNCOPE,
    "blacked out": SymptomCode.SYNCOPE,
    "dizzy": SymptomCode.DIZZINESS,
    "lightheaded": SymptomCode.DIZZINESS,
    "headache when i sit up": SymptomCode.POSTURAL_HEADACHE,
    "worse when i sit up": SymptomCode.POSTURAL_HEADACHE,
    "better when i lie": SymptomCode.POSTURAL_HEADACHE,
    "positional headache": SymptomCode.POSTURAL_HEADACHE,
    "still numb": SymptomCode.PERSISTENT_NUMBNESS,
    "numbness": SymptomCode.PERSISTENT_NUMBNESS,
    "can't move": SymptomCode.MOTOR_WEAKNESS,
    "leg weakness": SymptomCode.MOTOR_WEAKNESS,
    "legs are weak": SymptomCode.MOTOR_WEAKNESS,
    "back pain": SymptomCode.BACK_PAIN_AT_INJECTION_SITE,
    "confused": SymptomCode.CONFUSION,
    "foggy": SymptomCode.CONFUSION,
    "groggy": SymptomCode.CONFUSION,
    "numb between my legs": SymptomCode.SADDLE_NUMBNESS,
    "saddle numbness": SymptomCode.SADDLE_NUMBNESS,
    "blurry vision": SymptomCode.VISUAL_CHANGES,
    "double vision": SymptomCode.VISUAL_CHANGES,
    "vision is blurry": SymptomCode.VISUAL_CHANGES,
    "stiff neck": SymptomCode.NECK_STIFFNESS,
    "neck stiffness": SymptomCode.NECK_STIFFNESS,
    "lips are numb": SymptomCode.PERIORAL_NUMBNESS,
    "mouth is numb": SymptomCode.PERIORAL_NUMBNESS,
    "metallic taste": SymptomCode.METALLIC_TASTE,
    "taste of metal": SymptomCode.METALLIC_TASTE,
    "ringing in my ears": SymptomCode.TINNITUS,
    "tinnitus": SymptomCode.TINNITUS,
    "nauseous": SymptomCode.NAUSEA,
    "nauseated": SymptomCode.NAUSEA,
    "queasy": SymptomCode.NAUSEA,
    "sick to my stomach": SymptomCode.NAUSEA,
    "throwing up": SymptomCode.VOMITING,
    "thrown up": SymptomCode.VOMITING,
    "vomiting": SymptomCode.VOMITING,
    "vomited": SymptomCode.VOMITING,
    "keep anything down": SymptomCode.UNABLE_TO_TOLERATE_FLUIDS,
    "keep fluids down": SymptomCode.UNABLE_TO_TOLERATE_FLUIDS,
    "constipated": SymptomCode.CONSTIPATION,
    "bowel accident": SymptomCode.BOWEL_INCONTINENCE,
    "can't pee": SymptomCode.URINARY_RETENTION,
    "can't urinate": SymptomCode.URINARY_RETENTION,
    "dark urine": SymptomCode.DARK_URINE,
    "haven't passed urine": SymptomCode.NO_URINE_OUTPUT,
    "calf pain": SymptomCode.CALF_PAIN,
    "calf hurts": SymptomCode.CALF_PAIN,
    "calf is swollen": SymptomCode.CALF_SWELLING,
    "leg is swollen": SymptomCode.CALF_SWELLING,
    "tight": SymptomCode.LIMB_TIGHTNESS,
    "hurts when i move it": SymptomCode.PAIN_ON_PASSIVE_STRETCH,
    "muscles are stiff": SymptomCode.MUSCLE_RIGIDITY,
    "muscle rigidity": SymptomCode.MUSCLE_RIGIDITY,
    "fever": SymptomCode.FEVER,
    "temperature": SymptomCode.FEVER,
    "chills": SymptomCode.CHILLS,
    "wound is red": SymptomCode.WOUND_REDNESS,
    "red around": SymptomCode.WOUND_REDNESS,
    "draining": SymptomCode.WOUND_DRAINAGE,
    "bleeding": SymptomCode.BLEEDING_AT_SITE,
    "dressing is soaked": SymptomCode.DRESSING_SOAKED,
    "soaked through": SymptomCode.DRESSING_SOAKED,
    "swelling is growing": SymptomCode.EXPANDING_HEMATOMA,
    "catheter is leaking": SymptomCode.CATHETER_SITE_DRAINAGE,
    "chipped a tooth": SymptomCode.DENTAL_INJURY,
    "broken tooth": SymptomCode.DENTAL_INJURY,
    "bit my tongue": SymptomCode.TONGUE_LACERATION,
    "eye is gritty": SymptomCode.EYE_IRRITATION,
    "eye hurts": SymptomCode.EYE_IRRITATION,
    "hard to wake": SymptomCode.EXCESSIVE_SEDATION,
    "very sleepy": SymptomCode.EXCESSIVE_SEDATION,
    "itching": SymptomCode.ITCHING,
    "itchy": SymptomCode.ITCHING,
    "rash": SymptomCode.RASH,
    "allergic": SymptomCode.ALLERGIC_REACTION,
    "hurt myself": SymptomCode.SUICIDAL_IDEATION,
    "end it all": SymptomCode.SUICIDAL_IDEATION,
    "kill myself": SymptomCode.SUICIDAL_IDEATION,
}

SEVERITY_WORDS: dict[str, Severity] = {
    "unbearable": Severity.SEVERE,
    "excruciating": Severity.SEVERE,
    "terrible": Severity.SEVERE,
    "severe": Severity.SEVERE,
    "awful": Severity.SEVERE,
    "really bad": Severity.SEVERE,
    "moderate": Severity.MODERATE,
    "a bit": Severity.MILD,
    "slight": Severity.MILD,
    "mild": Severity.MILD,
    "a little": Severity.MILD,
}

NEGATORS = ("no ", "not ", "n't", "never", "none", "denies", "without", "nothing")
HEDGES = ("not sure", "i don't know", "maybe", "i guess", "kind of", "hard to say", "dunno")
DISTRESS = ("scared", "terrified", "frightened", "panicking", "freaking out", "can't cope")
WANTS_HUMAN = ("real person", "a human", "talk to a nurse", "speak to someone", "a doctor please")

# Whichever of yes/no appears first wins, so "no, yes I did" reads as the correction
# it is. Anchored on word boundaries and kept to unambiguous tokens: an earlier draft
# accepted a bare "i have", which read "I have been ok" as a yes and answered a
# question the patient had not been asked.
_YES_NO = re.compile(
    r"\b(?P<yes>yes|yeah|yep|yup|sure|correct|absolutely|of course)\b"
    r"|\b(?P<no>no|nope|nah|not really|not at all|none|negative|nothing)\b"
)

_NUMBER = re.compile(r"\b(10|\d)\s*(?:/\s*10|out of 10)?\b")
_TEMPERATURE = re.compile(r"\b(9\d|1[0-1]\d)(?:\.\d)?\s*(?:degrees|deg\b|°|f\b)", re.IGNORECASE)
_HOURS = re.compile(r"\b(\d{1,3}(?:\.\d)?)\s*(?:hours?|hrs?)\b", re.IGNORECASE)


class KeywordTurnEngine:
    """Reads free text with a keyword table so the REPL can be driven by typing.

    Implements `TurnEngine`. Names no topic and no slot id: everything it fills is
    read off the active topic's declared slots, so a clinician adding a topic gets it
    exercised interactively without touching this file.
    """

    def analyze(self, request: TurnRequest) -> TurnDraft:
        text = request.patient_message
        lowered = text.lower()

        symptoms = _symptoms(lowered)
        pain = _pain(lowered, request.topic)
        temperature = _temperature(lowered)
        confidence = _confidence(lowered, symptoms, pain)

        extraction = TurnExtraction.model_validate(
            {
                **request.provenance,
                "pain": pain,
                "symptoms": symptoms,
                "temperature_f": temperature,
                "slot_values": _slot_values(request.topic, request.state.slot_values, lowered, text, confidence),
                "patient_question": text if "?" in text else None,
                "proxy_detected": _is_proxy(lowered),
                "patient_distress": any(word in lowered for word in DISTRESS),
                "patient_requests_human": any(word in lowered for word in WANTS_HUMAN),
                "extraction_confidence": confidence,
                "notes": "keyword test double; not a real extraction",
            }
        )
        return TurnDraft(
            extraction=extraction,
            draft_reply=draft_reply(
                request.protocol,
                request.topic,
                request.state.slot_values | extraction.slot_values,
            ),
            model="keyword",
        )


def _clauses(lowered: str) -> list[str]:
    """Split so a negation binds only to what it actually negates.

    "no chest pain but my throat is sore" has to read as one denial and one report,
    which a whole-message negation check would get exactly backwards.
    """
    return [clause for clause in re.split(r"[,;.]| but | and | although ", lowered) if clause.strip()]


def _symptoms(lowered: str) -> list[dict[str, Any]]:
    """Every symptom phrase found, marked present or absent by its own clause."""
    found: dict[SymptomCode, dict[str, Any]] = {}
    phrases = sorted(SYMPTOM_PHRASES, key=len, reverse=True)
    for clause in _clauses(lowered):
        negated = any(negator in clause for negator in NEGATORS)
        matched: set[SymptomCode] = set()
        for phrase in phrases:
            code = SYMPTOM_PHRASES[phrase]
            if code in matched or phrase not in clause:
                continue
            matched.add(code)
            presence = Presence.ABSENT if negated else Presence.PRESENT
            observation: dict[str, Any] = {
                "code": code,
                "presence": presence,
                "quote": clause.strip(),
            }
            if presence is Presence.PRESENT:
                severity = _severity(clause)
                if severity is not None:
                    observation["severity"] = severity
                hours = _HOURS.search(clause)
                if hours:
                    observation["onset_hours_ago"] = float(hours.group(1))
            # A present reading beats an absent one: a patient who denies something
            # and then describes it has described it.
            existing = found.get(code)
            if existing is None or existing["presence"] is Presence.ABSENT:
                found[code] = observation
    return list(found.values())


def _severity(clause: str) -> Severity | None:
    for word, severity in SEVERITY_WORDS.items():
        if word in clause:
            return severity
    return None


def _pain(lowered: str, topic: Topic) -> dict[str, Any] | None:
    """A pain report, when the message talks about pain or the topic is asking about it.

    The second case matters more than it looks. A patient asked "how bad is your pain
    out of ten" answers "about a 4" — a message with no pain word in it at all. Filling
    only the slot and leaving `pain` empty would hide the score from every rule that
    reads `pain.score`, and from the summary, while the transcript plainly shows it was
    given. So the topic's own `maps_to` declarations are read the other way round: if a
    slot here draws from `pain.*`, a bare number is a pain score.
    """
    score = _NUMBER.search(lowered)
    asking_about_pain = any(
        (slot.maps_to or "").startswith("pain.") for slot in topic.slots
    )
    mentions_pain = any(word in lowered for word in ("pain", "hurt", "sore", "ache", "aching"))
    if not mentions_pain and not (asking_about_pain and score):
        return None

    report: dict[str, Any] = {"quote": lowered}
    if score:
        report["score"] = int(score.group(1))
    if any(phrase in lowered for phrase in ("not helping", "isn't touching", "no relief", "not working")):
        report["controlled_by_medication"] = False
    elif any(phrase in lowered for phrase in ("helping", "takes the edge off", "under control", "controlled")):
        report["controlled_by_medication"] = True
    if "worse" in lowered:
        report["trend"] = Trend.WORSE
    elif "better" in lowered:
        report["trend"] = Trend.BETTER
    elif "same" in lowered:
        report["trend"] = Trend.UNCHANGED
    return report


def _temperature(lowered: str) -> float | None:
    match = _TEMPERATURE.search(lowered)
    return float(match.group(0).rstrip("f°degrs ").strip()) if match else None


def _is_proxy(lowered: str) -> bool:
    return any(
        phrase in lowered
        for phrase in ("i'm his", "i'm her", "on his behalf", "on her behalf", "calling for", "my husband", "my wife")
    )


def _confidence(lowered: str, symptoms: list[dict[str, Any]], pain: dict[str, Any] | None) -> float:
    if any(hedge in lowered for hedge in HEDGES):
        return 0.4
    if symptoms or pain or _NUMBER.search(lowered) or _yes_no(lowered) is not None:
        return 0.85
    return 0.3


def _yes_no(lowered: str) -> bool | None:
    match = _YES_NO.search(lowered)
    return None if match is None else match.group("yes") is not None


def _slot_values(
    topic: Topic,
    already: dict[str, SlotValue],
    lowered: str,
    raw: str,
    confidence: float,
) -> dict[str, dict[str, Any]]:
    """Answer the topic's unfilled slots from whatever evidence the message holds.

    Each piece of evidence is consumed once. A patient answering "no" has answered
    the one question they were asked, not every remaining yes/no in the topic — and
    the engine's `maps_to` backfill picks up anything the clinical fields already
    settled, so this only has to cover what has no clinical field of its own.
    """
    answers: dict[str, dict[str, Any]] = {}
    yes_no = _yes_no(lowered)
    number = _NUMBER.search(lowered)
    hours = _HOURS.search(lowered)

    for slot in topic.slots:
        if slot.id in already and already[slot.id].value is not None:
            continue
        value = _slot_value(slot, lowered, raw, yes_no, number, hours)
        if value is None:
            continue
        answers[slot.id] = {"value": value, "confidence": confidence, "quote": raw}
        if slot.type == "bool":
            yes_no = None
        elif slot.type in ("int", "float"):
            number = hours = None
    return answers


def _slot_value(
    slot: Slot,
    lowered: str,
    raw: str,
    yes_no: bool | None,
    number: re.Match[str] | None,
    hours: re.Match[str] | None,
) -> bool | int | float | str | None:
    if slot.type == "bool":
        return yes_no
    if slot.type == "enum":
        for option in slot.values or []:
            if option.replace("_", " ") in lowered:
                return option
        return None
    if slot.type == "text":
        return raw if slot.required else None
    source = hours if slot.type == "float" and hours else number
    if source is None:
        return None
    parsed = float(source.group(1))
    return int(parsed) if slot.type == "int" else parsed
