<!--
turn_v1 — the volatile half, sent as the user turn on every call.

Everything here changes per turn, which is why it is here and not in `system_v1.md`:
it sits after the cache breakpoint, so none of it invalidates the cached prefix.

Substitutions come from `app/llm/context.py:render_turn`. Adding a placeholder here
means adding it there too — an unsupplied one is a `PromptError` at render time,
not a literal placeholder shown to a patient. Write a literal dollar sign as `$$`.
-->

## This patient

${case}

## Current topic: `${topic_id}`

What this topic needs to establish: ${topic_goal}

The clinician-authored question for it: "${opening_question}"

### Slots this topic can record

${slots}

### Already answered on this topic

${answered}

### What comes next

${next_topic}

## Conversation so far

${transcript}

## The message to interpret

The patient has just said:

"${patient_message}"

Record what this message tells you, and propose both replies.
