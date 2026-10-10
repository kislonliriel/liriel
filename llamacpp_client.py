"""
Local-inference backend for motivation.py: same call signature as
llm_client.chat()/chat_json() (a `messages` list, `temperature`, `effort`,
`model`), but talks to a self-hosted llama-server instead of Anthropic/
litellm. This is the whole point of scripts/llamacpp/ -- every one of
Liriel's four calls per cycle runs on a model under your own control, no
closed external API involved (MS §17.6 "weight ownership").

Requires a llama-server you started yourself (see llama.cpp's docs);
scripts/llamacpp/warm_cache.py warms its prompt cache. config.py's LLM_BACKEND switches motivation.py
between this module and llm_client.py -- see its docstring.

`effort` and `model` are accepted but ignored: there's one local server
serving one model, and reasoning-effort tiers are an Anthropic-specific
concept (litellm's `reasoning_effort` -> Anthropic's `output_config.effort`)
with no llama.cpp equivalent. Keeping the same signature as llm_client.py
means motivation.py's call sites don't need an `if backend == ...` at
every one of the four calls -- only the import switches.
"""
from __future__ import annotations

import os
import sys
import time
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

from llm_common import LLMError, _chat_json_with_retry  # noqa: E402
from config import settings
import finetune_capture  # noqa: E402 - raw material for a future LoRA fine-tune; never raises

_client = LlamaCppClient(
    base_url=settings.llamacpp_base_url, timeout=settings.llm_request_timeout
)

# How many tokens each query is ever allowed to write -- a resource cap, not a judgment of content. The local model
# sometimes falls into a repetition loop (QA wave 1, step 3: RELATIONS_UPDATE wrote the same 17 bonds 190 times until the
# 16384-token budget cut it, ~390 s for one call; the retry then finished in 35 s). The loop costs time in proportion to
# the cap, so each query gets about twice the longest answer MEASURED for it (finetune_capture's timings, 2026-10-08); a
# query not listed keeps settings.llm_max_tokens. Raise an entry if a legitimate answer ever shows finish_reason=length.
# The model sometimes THINKS before answering (the server returns it apart, as `reasoning_content`). Measured on the captured
# corpus (2026-10-08): in ~1/3 of the calls it spent 1,000-4,000 tokens there for an answer of 100-500 tokens, and once it spent the
# whole budget and returned an EMPTY answer (which crashed a cycle). Switching thinking off altogether is not free: on
# MAINMEMORY_FILING, the query that decides what to archive, the thinking version archived rows and the non-thinking one answered
# `[]` every time. So thinking is kept but bounded, and the retry after a length cut does not think at all.
# A BOUNDED budget turned out to be fragile too (QA, clean battery step 1): when the budget ends the thinking mid-plan the model goes on
# writing its bulleted plan as the ANSWER, and the JSON extractor then pulled a nested fragment out of it -> a validation error that killed
# the cycle. The answer-first fields (interlocutor_brought, character_says, evidence, ...) now carry the step-by-step reasoning IN the
# output, where it is visible and checkable, so hidden thinking is OFF (budget 0) for every query except MAINMEMORY_FILING, the one whose
# judgment (what leaves focus) measurably depended on it: with thinking it archives, without it it answered `[]` every time.
_THINKING_BUDGET = int(os.environ.get("LLAMA_THINKING_BUDGET", "0"))     # tokens for every query not listed below; -1 = unbounded
_THINKING_BUDGET_BY_QUERY = {"MAINMEMORY_FILING": 2048}


def _thinking_budget_for(messages: List[dict]) -> int:
    q = finetune_capture.header(messages).get("query") or ""
    return _THINKING_BUDGET_BY_QUERY.get(q, _THINKING_BUDGET)

_LOOP_BREAKING_SAMPLING = {"repeat_penalty": 1.2, "repeat_last_n": 256, "thinking_budget_tokens": 0}

_MAX_TOKENS_BY_QUERY = {
    "RELATIONS_UPDATE": 7168,
    "ANCHOR_REVIEW": 6144,
    "HUNTER_READING": 4096,
    "GRAPH_REQUEST": 4096,
    "TACTICAL_SCENE_INTERPRETATION": 4096,
    "MAINMEMORY_FILING": 3072,
}


def _n_predict_for(messages: List[dict]) -> int:
    cap = _MAX_TOKENS_BY_QUERY.get(finetune_capture.header(messages).get("query") or "")
    return min(cap, settings.llm_max_tokens) if cap else settings.llm_max_tokens


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
        started = time.monotonic()
        # The second attempt follows a completion the token budget cut: the same draw again would loop again (QA wave 1,
        # step 5: RELATIONS_UPDATE was cut twice in a row at identical settings, ~800 s), so it samples differently --
        # a stronger repetition penalty over a SHORT window (a loop is local; a whole-context penalty makes the model
        # avoid copying the ids it must copy -- QA step 7 saw invented ids come out of a 1.3 whole-context retry) and a
        # little more temperature. Sampling only; the prompt is untouched.
        sampling = (
            {"temperature": temperature, **({"thinking_budget_tokens": b} if (b := _thinking_budget_for(messages)) >= 0 else {})}
            if attempt == 0 else dict(_LOOP_BREAKING_SAMPLING, temperature=max(temperature, 0.5))
        )
        result = _client._post_chat(  # noqa: SLF001 - same package, avoids re-exposing a third public method
            messages=messages,
            n_predict=_n_predict_for(messages),
            **sampling,
        )
        try:
            choice = result["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected llama-server response shape: {result}") from exc
        # Every attempt, the cut-short and the empty ones included, is a fact worth keeping for a later
        # fine-tune corpus (finetune_capture.py): what was asked and what came back, nothing judged here.
        finetune_capture.record(
            messages=messages, response=content, model=result.get("model"), temperature=sampling["temperature"],
            attempt=attempt, finish_reason=choice.get("finish_reason"),
            seconds=time.monotonic() - started, timings=result.get("timings"),
            reasoning_chars=len(choice["message"].get("reasoning_content") or ""),
        )
        # A completion cut by length, or an EMPTY one: with a bounded thinking budget the model can spend it all thinking and
        # end (`finish_reason: "stop"`) on a blank answer (QA wave 3, step 6: MAINMEMORY_FILING, 1029 tokens, content a bare newline).
        # Either way the second attempt does not think (budget 0) and samples a little differently.
        if attempt == 0 and (choice.get("finish_reason") == "length" or not (content or "").strip()):
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
    """Same contract as llm_client.chat_json(), including its retry-once-on-
    unparseable-JSON policy (see _chat_json_with_retry) — this backend's
    own chat() already retries once for a truncated completion; this adds
    the equivalent for a complete-but-malformed one."""
    return _chat_json_with_retry(chat, messages, temperature, effort, model)
