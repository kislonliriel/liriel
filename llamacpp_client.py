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
    raises LLMError on failure or an empty response."""
    result = _client._post_chat(  # noqa: SLF001 - same package, avoids re-exposing a third public method
        messages=messages,
        n_predict=settings.llm_max_tokens,
        temperature=temperature,
    )
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"Unexpected llama-server response shape: {result}") from exc
    if not content or not content.strip():
        raise LLMError(f"llama-server returned an empty response: {result}")
    return content


def chat_json(
    messages: List[dict],
    temperature: float = 0.3,
    effort: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Same contract as llm_client.chat_json()."""
    raw = chat(messages, temperature=temperature, effort=effort, model=model)
    return _extract_json(raw)
