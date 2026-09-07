"""The real `TurnEngine`: one Anthropic call per patient turn.

Implements the interface in `app/llm/turn.py`, which the deterministic pipeline has
already been built and tested against. Nothing downstream knows this is the real one
rather than a double — that is the point of the seam, and it is why steps 4 through 7
of `docs/architecture.md` did not have to be debugged through a model.

Three properties this module is responsible for, all of them load-bearing:

**One call, not two.** Extraction and reply drafting happen together because the
safety gate downstream can discard the reply anyway, so splitting them would double
latency and buy nothing. The call now returns *two* drafts rather than one — the
ordinary next question, and the transition used if this turn closed the topic — for
the same reason: step 2 cannot know which applies, and a second call to find out
would cost a round trip on every topic boundary.

**The response is never streamed.** Invariant 2: a RED rule discards the draft and
substitutes fixed copy, so nothing may reach a patient before the gate has run. The
SDK default is non-streaming and that is not an accident here.

**A failure is a record, not an exception.** Every exit from `analyze` is a
`TurnDraft` the pipeline can persist — invariant 4. Retries are bounded and counted;
exhausting them returns `hard_failure=True` with no extraction, which
`app/triage/tiering.py` already reads as Tier 1 because the record is untrustworthy.
`analyze` raises only for a programming error, never for a bad turn. The model's
literal response text is held on every one of those paths, valid or not: the request
carries an explicit schema from `app/llm/wire.py` and validation happens here rather
than inside the SDK, so nothing is lost on the turns where the record matters most.

**There is no fallback.** If this engine cannot produce an extraction, the
conversation hard-fails to a human. It never quietly degrades to the keyword double
in `app/llm/fake.py` — a check-in that silently switched to keyword matching would
look identical in the record to one that did not, which is worse than stopping.
`tests/test_turn_engine.py` asserts this module never reaches for that one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import anthropic
import pydantic

from app.config import settings
from app.llm.client import get_client
from app.llm.context import render_system, render_turn
from app.llm.turn import TurnDraft, TurnRequest
from app.llm.wire import TurnResponse, to_extraction, turn_response_schema

# Sent as a follow-up turn when a response did not satisfy the schema. Naming the
# failing field is what makes the retry worth spending: a bare "try again" gets the
# same answer back.
RETRY_INSTRUCTION = (
    "Your previous response did not match the required schema and was rejected:\n\n"
    "{errors}\n\n"
    "Reply again with the same observations, corrected. Change only what the errors "
    "name — do not re-interpret the patient's message."
)

# What is persisted as `raw_response` when a response failed to validate.
#
# Both halves matter, and the payload is the half that used to be missing. The errors
# say *what* was wrong; only the literal text the model produced can answer whether it
# was reasonable and a schema bug rejected it — which is exactly the question a hard
# failure raises, and the reason a hard failure is Tier 1. Keeping it is what makes
# the turn re-scorable later against a changed schema.
VALIDATION_FAILURE_NOTE = "schema validation failed\n{errors}\n\nlast response:\n{raw}"


@dataclass
class AnthropicTurnEngine:
    """One Anthropic call per turn. Implements `TurnEngine`.

    Holds no conversation state, like every implementation of the interface: the
    conversation lives in `ProtocolState` and the transcript, both of which arrive on
    the request, so this engine cannot accumulate a private view of the check-in that
    the stored record does not have.
    """

    client: anthropic.Anthropic
    model: str = ""
    effort: str = ""
    max_tokens: int = 0
    max_validation_retries: int = -1

    def __post_init__(self) -> None:
        self.model = self.model or settings.turn_model
        self.effort = self.effort or settings.turn_effort
        self.max_tokens = self.max_tokens or settings.turn_max_tokens
        if self.max_validation_retries < 0:
            self.max_validation_retries = settings.turn_max_validation_retries

    @classmethod
    def default(cls) -> AnthropicTurnEngine:
        """The configured engine. Raises `MissingAPIKey` rather than failing mid-call."""
        return cls(client=get_client())

    def analyze(self, request: TurnRequest) -> TurnDraft:
        """Interpret one patient message and propose both replies.

        The retry ladder from `docs/architecture.md` step 2, exactly: a response that
        does not satisfy the schema is retried with the validation errors fed back, at
        most `max_validation_retries` times, and then hard-fails to a human.
        """
        system = render_system(request.protocol)
        conversation: list[anthropic.types.MessageParam] = [
            {"role": "user", "content": render_turn(request)}
        ]
        started = time.monotonic()
        errors = ""
        raw = ""

        for retries in range(self.max_validation_retries + 1):
            if retries:
                conversation.append(
                    {"role": "user", "content": RETRY_INSTRUCTION.format(errors=errors)}
                )
            try:
                message = self._call(system, conversation)
            except anthropic.APIError as error:
                # The SDK has already retried whatever was worth retrying (429s, 5xx,
                # dropped connections) with backoff. Reaching here means the call is
                # not going to succeed, and a patient is waiting.
                return self._hard_failure(
                    started, retries, raw=f"{type(error).__name__}: {error}"
                )

            if message.stop_reason == "refusal":
                # A safety classifier declined. Not retryable by definition, and not
                # something to paper over with a generated apology — the check-in goes
                # to a human with the reason on the record.
                return self._hard_failure(
                    started, retries, raw="refusal", model=message.model
                )

            text = _response_text(message)
            if text is not None:
                raw = text

            if text is None or message.stop_reason == "max_tokens":
                # Either no text block at all, or one the model never finished. Both
                # are worth naming as themselves: a truncated payload fails to parse,
                # and telling the model its JSON was malformed would send it looking
                # for a mistake it did not make.
                errors = _no_payload_reason(message)
                continue

            try:
                payload = TurnResponse.model_validate_json(raw)
            except pydantic.ValidationError as error:
                # The ordinary schema-failure path. Retry with the errors named; the
                # model saw its own answer, so a bare "try again" gets it back.
                errors = _validation_errors(error)
                continue

            return self._draft(payload, request, message, started, retries, raw)

        return self._hard_failure(
            started,
            self.max_validation_retries,
            raw=VALIDATION_FAILURE_NOTE.format(errors=errors, raw=raw or "(none returned)"),
        )

    # --- The call -----------------------------------------------------------

    def _call(
        self, system: str, conversation: list[anthropic.types.MessageParam]
    ) -> anthropic.types.Message:
        """One request. Non-streaming, deliberately — see the module docstring.

        The system block carries the cache breakpoint: it is byte-identical on every
        turn of every conversation, so from the second turn onwards it is read from
        cache rather than re-billed. Everything that varies is in `conversation`,
        after the breakpoint. Adding anything per-conversation to the system block
        would silently cost that on every call — `usage.cache_read_input_tokens` is
        the number to watch.

        `create` with an explicit schema rather than `parse` with a Pydantic class:
        the schema is `app/llm/wire.py`'s to state, and validating the response here
        instead of inside the SDK is what keeps the model's literal text on the
        failing paths. The schema is constant across turns, so it caches alongside
        the system block rather than against it.
        """
        return self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=conversation,
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": turn_response_schema()},
            },
            thinking={"type": "adaptive"},
        )

    # --- Outcomes -----------------------------------------------------------

    def _draft(
        self,
        payload: TurnResponse,
        request: TurnRequest,
        message: anthropic.types.Message,
        started: float,
        retries: int,
        raw: str,
    ) -> TurnDraft:
        """A validated response. Provenance is stamped here, not by the model.

        `raw_response` is the text the model actually produced, not a re-dump of the
        parsed object: the two differ wherever a validator normalized something, and
        the audit trail wants what was said.
        """
        return TurnDraft(
            extraction=to_extraction(payload, request),
            draft_reply=payload.draft_reply,
            transition_reply=payload.draft_transition_reply,
            validation_retries=retries,
            model=message.model,
            latency_ms=_elapsed_ms(started),
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            raw_response=raw,
        )

    def _hard_failure(
        self, started: float, retries: int, *, raw: str, model: str | None = None
    ) -> TurnDraft:
        """The terminal rung. Still a row, and still a reply.

        `draft_reply` is left empty on purpose: the pipeline falls back to the
        protocol's own clinician-authored question, and this engine has no business
        inventing patient-facing text on the path where it has just demonstrated it
        cannot be trusted to produce any.
        """
        return TurnDraft(
            extraction=None,
            hard_failure=True,
            validation_retries=retries,
            model=model or self.model,
            latency_ms=_elapsed_ms(started),
            raw_response=raw,
        )


# --- Response reading -------------------------------------------------------


def _response_text(message: anthropic.types.Message) -> str | None:
    """The structured output as the model wrote it, or None if it wrote none.

    Only text blocks. An adaptive-thinking response also carries thinking blocks, and
    those are reasoning about the answer rather than the answer.
    """
    text = "".join(block.text for block in message.content if block.type == "text")
    return text or None


def _validation_errors(error: pydantic.ValidationError) -> str:
    """The failing fields, in the words the retry is given.

    Capped: a response that got the shape badly wrong can produce hundreds of errors,
    and pasting all of them back costs more than it corrects.
    """
    return "\n".join(
        f"- {'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}"
        for detail in error.errors()[:10]
    )


def _no_payload_reason(message: anthropic.types.Message) -> str:
    """Why a response carried no usable structured output, before parsing was tried."""
    if message.stop_reason == "max_tokens":
        return "the response was cut off at max_tokens before it was complete"
    return f"the response contained no structured output (stop_reason={message.stop_reason})"


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
