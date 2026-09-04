"""Step 1 of the pipeline, rendered: everything the call is allowed to see.

Two prompts, matched to the protocol by version suffix (`prompt_version` in the
protocol YAML selects them, so a protocol and its prompts cannot drift apart):

`system_v1.md` is **constant** — the role, the eight non-negotiable conversational
constraints from `docs/safety-rules.md`, and the closed vocabularies. Constant is a
requirement, not an observation: it is the cached prefix of every call, and a
timestamp or a turn index anywhere in it would silently cost the cache on every turn.
Nothing per-conversation may be added to it.

`turn_v1.md` is the volatile half — case facts, the active topic, the predicted next
topic, the transcript tail and the patient's message — and is sent as the user turn,
after the cache breakpoint.

The vocabularies are **generated from `app/domain/enums.py`**, never typed into the
markdown. A closed vocabulary the model is asked to extract into is only closed if it
matches the enum the safety rules match against; a hand-copied list would drift the
first time a symptom code was added, and the failure mode is silent — the model keeps
returning a code that no longer exists, or never learns about one that does.
`tests/test_turn_engine.py` asserts every member of every rendered enum is present.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

from app.domain.enums import MedAdherence, Presence, Severity, SymptomCode, Trend
from app.protocol.loader import Protocol, Slot, Topic, flatten

if TYPE_CHECKING:
    from app.conversation.session import Message
    from app.llm.turn import TurnRequest
    from app.protocol.engine import CaseFacts

PROMPTS_DIR = Path(__file__).parent / "prompts"

# The eight constraints in `docs/safety-rules.md` are system-prompt content, and the
# doc says each one gets a test. These are the phrases that prove each is still there:
# the prose stays editable in the markdown, but deleting a constraint fails a test
# rather than quietly shipping. Keyed by the constraint number in the doc.
CONVERSATIONAL_CONSTRAINT_MARKERS: dict[int, str] = {
    1: "Never diagnose",
    2: "Never start, stop, or change the dose",
    3: "Never estimate risk or prognosis",
    4: "never improvise around a red flag",
    5: "Never claim or imply you are a human",
    6: "someone other than the patient is answering",
    7: "one question at a time",
    8: "say so plainly",
}


class PromptError(RuntimeError):
    """A prompt template that cannot be rendered. Raised at load, like a bad protocol."""


def load_template(name: str, version: str) -> Template:
    """Read a prompt file, minus its maintainer notes.

    The `<!-- -->` block at the top of each template is written for whoever edits it
    next — what may not be added, why the file is split where it is — and is stripped
    rather than sent. It is dead weight in the context window, and worse, it tells the
    model about machinery behind it that it has no business reasoning about.
    """
    path = PROMPTS_DIR / f"{name}_{version}.md"
    if not path.exists():
        raise PromptError(f"no prompt template {path.name!r} for prompt version {version!r}")
    return Template(_COMMENT.sub("", path.read_text()).strip())


_COMMENT = re.compile(r"<!--.*?-->\s*", re.DOTALL)


def _render(name: str, version: str, values: dict[str, str]) -> str:
    """Substitute, converting both ways a template can be wrong into a load error.

    An unsupplied placeholder would otherwise reach a patient as literal `$topic_id`,
    and a stray `$` in prose — a dollar amount, a shell snippet — would raise a bare
    `ValueError` naming a line number and nothing else.
    """
    try:
        return load_template(name, version).substitute(values)
    except KeyError as error:
        raise PromptError(
            f"prompt {name}_{version}.md references ${{{error.args[0]}}}, which is not supplied"
        ) from error
    except ValueError as error:
        raise PromptError(
            f"prompt {name}_{version}.md has a malformed placeholder ({error}); "
            "write a literal dollar sign as `$$`"
        ) from error


def render_system(protocol: Protocol) -> str:
    """The constant half. Identical for every turn of every conversation."""
    return _render(
        "system",
        protocol.prompt_version,
        {
            "symptom_codes": _enum_list(SymptomCode),
            "severities": _enum_values(Severity),
            "presences": _enum_values(Presence),
            "trends": _enum_values(Trend),
            "adherences": _enum_values(MedAdherence),
        },
    )


def render_turn(request: TurnRequest) -> str:
    """The volatile half: this case, this topic, this message."""
    return _render(
        "turn",
        request.protocol.prompt_version,
        {
            "case": _case_block(request.case),
            "topic_id": request.topic.id,
            "topic_goal": flatten(request.topic.goal),
            "opening_question": flatten(request.topic.opening_question),
            "slots": _slot_block(request.topic),
            "answered": _answered_block(request),
            "next_topic": _next_topic_block(request.next_topic),
            "transcript": _transcript_block(request.history),
            "patient_message": request.patient_message,
        },
    )


# --- Vocabularies -----------------------------------------------------------


def _enum_values(enum_cls: type[Enum]) -> str:
    return ", ".join(f"`{member.value}`" for member in enum_cls)


def _enum_list(enum_cls: type[Enum]) -> str:
    return "\n".join(f"- `{member.value}`" for member in enum_cls)


# --- Turn context blocks ----------------------------------------------------


def _case_block(case: CaseFacts) -> str:
    """What the model may know about the patient. Synthetic data only — invariant 5."""
    lines = [f"- Anesthesia: {case.anesthesia_type.value}"]
    if case.block_type is not None:
        lines.append(f"- Nerve block: {case.block_type.value}")
    if case.procedure:
        lines.append(f"- Procedure: {case.procedure}")
    if case.hours_post_op is not None:
        lines.append(f"- Hours since surgery: {case.hours_post_op:g}")
    if case.expected_block_duration_hours is not None:
        lines.append(
            f"- Expected block duration: about {case.expected_block_duration_hours:g} hours"
        )
    return "\n".join(lines)


def _slot_block(topic: Topic) -> str:
    """The active topic's slots — the only slot ids an answer may name.

    Anything else is dropped by `app/llm/wire.py`, so listing them here is what makes
    the difference between an answer and a dropped one legible to the model.
    """
    return "\n".join(_slot_line(slot) for slot in topic.slots)


def _slot_line(slot: Slot) -> str:
    parts = [f"- `{slot.id}` ({_slot_type(slot)})"]
    if slot.required:
        parts.append("**required**")
    if slot.prompt_hint:
        parts.append(f"— {flatten(slot.prompt_hint)}")
    return " ".join(parts)


def _slot_type(slot: Slot) -> str:
    if slot.type == "enum":
        return "one of " + ", ".join(f"`{value}`" for value in slot.values or [])
    bounds = [
        f"min {slot.min:g}" if slot.min is not None else "",
        f"max {slot.max:g}" if slot.max is not None else "",
    ]
    suffix = ", ".join(bound for bound in bounds if bound)
    return f"{slot.type}, {suffix}" if suffix else slot.type


def _answered_block(request: TurnRequest) -> str:
    """Slots of this topic already filled, so the model does not re-ask them."""
    answered = [
        f"- `{slot.id}` = {request.state.slot_values[slot.id].value!r}"
        for slot in request.topic.slots
        if slot.id in request.state.slot_values
    ]
    return "\n".join(answered) if answered else "- (nothing yet)"


def _next_topic_block(next_topic: Topic | None) -> str:
    """The lookahead the transition draft is written against.

    Explicitly labelled a prediction. The model is not being asked whether the topic
    closed — it does not decide that and is not told the answer; it writes both
    branches and `app/conversation/pipeline.py` picks after the protocol engine has
    ruled.
    """
    if next_topic is None:
        return (
            "There is no topic after this one. If this message completes the current "
            "topic, the check-in ends and fixed closing copy is used, so leave "
            "`draft_transition_reply` empty."
        )
    return (
        f"If this message completes the current topic, the next one will be "
        f"`{next_topic.id}`, whose goal is: {flatten(next_topic.goal)}\n"
        f"Its clinician-authored opening question is: "
        f"\"{flatten(next_topic.opening_question)}\""
    )


def _transcript_block(history: tuple[Message, ...]) -> str:
    if not history:
        return "(no messages yet)"
    return "\n".join(f"{message.role}: {message.content}" for message in history)
