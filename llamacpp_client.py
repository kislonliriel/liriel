"""
Local-inference backend for motivation.py: same call signature as
llm_client.chat()/chat_json() (a `messages` list, `temperature`, `effort`,
`model`), but talks to a self-hosted llama-server instead of Anthropic/
litellm. This is the whole point of scripts/llamacpp/ -- every one of
Liriel's four calls per cycle runs on a model under your own control, no
closed external API involved (MS §17.6 "weight ownership").

Requires scripts/llamacpp/start_server.sh running first (it also warms
the prompt cache). config.py's LLM_BACKEND switches motivation.py
between this module and llm_client.py -- see its docstring.

`effort` and `model` are accepted but ignored: there's one local server
serving one model, and reasoning-effort tiers are an Anthropic-specific
concept (litellm's `reasoning_effort` -> Anthropic's `output_config.effort`)
with no llama.cpp equivalent. Keeping the same signature as llm_client.py
means motivation.py's call sites don't need an `if backend == ...` at
every one of the four calls -- only the import switches.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import requests

# scripts/llamacpp/client.py already has the working /v1/chat/completions
# logic (including the chat-template fix -- see its module docstring for
# why /completion alone isn't enough) and the repeat_penalty safety net
# confirmed necessary in testing. Reused here rather than forked, so a fix
# made once (e.g. against a real repetition case) applies to both the
# standalone smoke-test script and the real pipeline.
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts" / "llamacpp"))
from client import LlamaCppClient  # noqa: E402

from llm_client import LLMError, _extract_json  # noqa: E402
from config import settings

_client = LlamaCppClient(
    base_url=settings.llamacpp_base_url, timeout=settings.llm_request_timeout
)


def chat(
    messages: List[dict],
    temperature: float = 0.5,
    effort: Optional[str] = None,  # noqa: ARG001 - no local equivalent, kept for call-site parity
    model: Optional[str] = None,  # noqa: ARG001 - one local server/model for now
) -> str:
    """Same contract as llm_client.chat(): returns the raw text content,
    raises LLMError on failure or an empty response.

    Retries once, and only once, whenever `finish_reason: "length"` comes
    back at all -- regardless of whether `content` is empty. n_predict is
    set generously relative to what a real answer here ever needs (a
    structured JSON object or a short reply, not a huge deliverable), so
    the model getting cut off by the token budget is itself the signal
    something degenerate happened, in either of two confirmed-for-real
    shapes: (1) a reasoning-capable model spends the entire budget inside
    its own `reasoning_content` (a field separate from `message.content`,
    for a model that exposes its "thinking" as such), leaving `content`
    empty; (2) the model loops in `content` ITSELF -- seen for real
    emitting the same "deceased daughter" Person row over and over with
    only the vov_id number incrementing, dozens of times, until length cut
    it off mid-object with unbalanced braces. That second shape produces
    non-empty but truncated, unparseable JSON, which the first version of
    this retry (gated on content being empty) never caught. Generation is
    stochastic enough that a second attempt is not the same draw;
    retrying once is worth it before failing the whole ProcessMotivation
    cycle over what may be a one-off loop, but a second length-truncated
    or empty response in a row is treated as a real failure, not retried
    again -- whatever content survives at that point is still returned
    (not raised here) so chat_json's own parser gets the final say on
    whether it's usable."""
    for attempt in range(2):
        result = _client._post_chat(  # noqa: SLF001 - same package, avoids re-exposing a third public method
            messages=messages,
            n_predict=settings.llm_max_tokens,
            temperature=temperature,
        )
        try:
            choice = result["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected llama-server response shape: {result}") from exc
        if attempt == 0 and choice.get("finish_reason") == "length":
            continue
        if content and content.strip():
            return content
        raise LLMError(f"llama-server returned an empty response: {result}")


def chat_json(
    messages: List[dict],
    temperature: float = 0.3,
    effort: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Same contract as llm_client.chat_json()."""
    raw = chat(messages, temperature=temperature, effort=effort, model=model)
    return _extract_json(raw)
