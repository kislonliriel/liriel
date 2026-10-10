"""
What every backend shares and nothing else needs: the error type and the reading of a model's JSON (fences, stray text, two narrow repairs,
one retry). Standard library only -- the Claude-session embodiment (claude_session_client.py, liriel.py) imports this, never llm_client.py,
so it needs no litellm and no model package. llm_client.py and llamacpp_client.py re-export these names.
"""
from __future__ import annotations

import json
import re


class LLMError(RuntimeError):
    """Raised when the LLM call fails or returns something we can't parse."""


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _repair_json_glitches(text: str) -> str:
    """A couple of narrow, conservative fixups for JSON syntax slips seen
    from large models too, not just small local ones — e.g. Claude Sonnet 5
    once emitted `"priority": 2"` (a stray closing quote glued onto a
    number) deep inside an otherwise well-formed ~2000-token JSON object.
    Applied only as a last resort, after a straight parse has already
    failed on the un-repaired text."""
    # A quote glued directly onto a bare numeric *value* (never onto the
    # end of a quoted string, e.g. "VOV_0006" — the number there must be
    # immediately preceded by `:`, `,` or `[`, which a quoted string's
    # content never is): `"priority": 2"` -> `"priority": 2`.
    text = re.sub(r'([:\[,]\s*-?\d+(?:\.\d+)?)"(\s*[,\]}])', r"\1\2", text)
    # A trailing comma before a closing brace/bracket.
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def _extract_json(raw: str) -> dict:
    """Best-effort extraction of a JSON object from a model's raw text.

    Models sometimes wrap JSON in markdown fences or add a stray sentence
    before/after it, even when told not to — strip fences, then fall back
    to grabbing the outermost {...} block, then to repairing a couple of
    common syntax glitches, before giving up.
    """
    text = _CODE_FENCE_RE.sub("", raw).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    candidate = text[start : end + 1] if start != -1 and end != -1 and end > start else text
    if candidate != text:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(_repair_json_glitches(candidate))
    except json.JSONDecodeError:
        pass

    raise LLMError(
        "Could not parse a JSON object out of the LLM response. "
        f"Raw response was:\n{raw}"
    )


def _chat_json_with_retry(chat_fn, messages, temperature, effort, model) -> dict:
    """Retries the whole completion once when the model's JSON comes back
    unparseable — the same "generation is stochastic enough that a second
    attempt is not the same draw" reasoning chat()'s own length-truncation
    retry already relies on, applied to the other way a completion can
    come back unusable. Confirmed for real: BEST_PREY_GUESS (the largest
    of the cycle's four JSON outputs) came back with one closing brace
    missing inside `accompanying_objectives`, deep enough into an
    otherwise well-formed ~3000-token object that _repair_json_glitches'
    narrow, conservative fixups (a stray character, a trailing comma)
    can't safely address — auto-balancing brace counts risks silently
    producing a structurally valid but semantically WRONG object (a field
    nested one level off from where the model meant it), which is worse
    than failing loudly. Shared by both backends' own chat_json (each
    passes its own chat() in as `chat_fn`) rather than duplicated, since
    the retry policy itself has nothing backend-specific in it."""
    raw = chat_fn(messages, temperature=temperature, effort=effort, model=model)
    try:
        return _extract_json(raw)
    except LLMError:
        raw = chat_fn(messages, temperature=temperature, effort=effort, model=model)
        return _extract_json(raw)
