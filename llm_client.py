"""
LLM abstraction layer.

Uses litellm so that switching providers is a matter of changing LLM_MODEL
(and, if needed, an API key) in .env — no code changes required. Examples:

    LLM_MODEL=groq/llama-3.3-70b-versatile   # Groq (needs GROQ_API_KEY) — this fork's default
    LLM_MODEL=ollama/gemma2                  # local Ollama
    LLM_MODEL=gpt-4o-mini                    # OpenAI (needs OPENAI_API_KEY)
    LLM_MODEL=claude-sonnet-5                # Anthropic (needs ANTHROPIC_API_KEY)
"""
from __future__ import annotations

import json
import re
import time
from typing import List, Optional

import litellm

from config import settings
from llm_common import (  # noqa: F401 - re-exported: the rest of the code imports these from here
    LLMError,
    _CODE_FENCE_RE,
    _chat_json_with_retry,
    _extract_json,
    _repair_json_glitches,
)

# Some providers reject sampling params we pass unconditionally. Notably,
# current Claude models (e.g. claude-sonnet-5) run with extended thinking on
# by default, and the Anthropic API then requires temperature=1 — it rejects
# any other explicit value instead of ignoring it. Since Phase 1's whole
# point is "swap LLM_MODEL in .env, no code changes", we don't want a
# provider-specific branch here: let litellm silently drop a param a given
# provider doesn't support (falling back to that provider's default) rather
# than erroring out.
litellm.drop_params = True


def _is_anthropic_model(model: str) -> bool:
    return model.startswith("claude-") or model.startswith("anthropic/")


def _with_cache_control(messages: List[dict]) -> List[dict]:
    """Anthropic caches an exact-prefix match up to an explicit
    `cache_control` breakpoint (GA, no beta header) — there's no
    provider-agnostic way to ask for this, so unlike everything else in
    this file it only applies to Claude models (OpenAI caches long
    prompts automatically, with no equivalent parameter to set; Ollama has
    no server-side cache at all). Placed on the system message: that's the
    ~18.7k-token MetaScheme, byte-identical across all 3 calls of a cycle
    and across every cycle after it, by far the largest stable prefix we
    have. Without this, every one of the 3 sequential calls per chat
    message reprocesses the whole MetaScheme from zero, which is a real
    chunk of the multi-minute latency per reply."""
    out = []
    for msg in messages:
        if msg.get("role") == "system" and isinstance(msg.get("content"), str):
            out.append({
                "role": "system",
                "content": [{
                    "type": "text",
                    "text": msg["content"],
                    "cache_control": {"type": "ephemeral"},
                }],
            })
        else:
            out.append(msg)
    return out


_RETRY_AFTER_RE = re.compile(r"try again in ([0-9.]+)\s*(ms|s)(?![a-z])", re.IGNORECASE)


def _rate_limit_wait_seconds(exc: Exception, attempt: int) -> float:
    """How long to wait before retrying a rate-limited call: the delay the
    provider itself names ("Please try again in 11.9s", Groq's wording) plus
    a second of margin, else exponential backoff capped at a minute."""
    m = _RETRY_AFTER_RE.search(str(exc))
    if m:
        seconds = float(m.group(1)) / (1000.0 if m.group(2).lower() == "ms" else 1.0)
        return seconds + 1.0
    return min(60.0, 5.0 * (2 ** attempt))


def _completion(kwargs: dict):
    """litellm.completion that waits out a provider rate limit instead of
    failing the whole cycle on it. A cycle makes dozens of calls (one per
    Object in the MOV, MS §12.3A, plus the rest), each carrying the whole
    MetaScheme, so a tokens-per-minute limit is routine rather than
    exceptional. Gives up — raising the provider's own error — after
    `settings.llm_rate_limit_retries` waits."""
    retries = settings.llm_rate_limit_retries
    for attempt in range(retries + 1):
        try:
            return litellm.completion(**kwargs)
        except litellm.RateLimitError as exc:
            if attempt == retries:
                raise
            wait = _rate_limit_wait_seconds(exc, attempt)
            print(f"[notice] provider rate limit ({kwargs['model']}) — waiting {wait:.0f}s, "
                  f"retry {attempt + 1}/{retries}")
            time.sleep(wait)


def chat(
    messages: List[dict],
    temperature: float = 0.5,
    effort: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Send a chat completion request and return the raw text content.

    `model` overrides `settings.llm_model` for this call only — motivation.py
    uses this to run the cycle's three structured-JSON calls on a cheaper
    model (config.py's LLM_MODEL_STRUCTURED) while the reply-composition
    call, the one Liriel's actual voice/personality quality depends on,
    stays on LLM_MODEL. Defaults to LLM_MODEL when omitted, so every other
    caller is unaffected.

    `effort` maps to litellm's `reasoning_effort`, which it translates to
    Anthropic's `output_config.effort` for Claude models (confirmed via
    `litellm.get_supported_openai_params`) — passed through unconditionally
    and left to `litellm.drop_params` to discard on providers that don't
    support it, the same way every other provider-specific param here is
    handled (num_ctx, cache_control, the temperature-vs-thinking conflict).
    """
    model = model or settings.llm_model
    if _is_anthropic_model(model):
        messages = _with_cache_control(messages)

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "timeout": settings.llm_request_timeout,
        # No provider default is trustworthy enough to leave unset — a
        # model whose reasoning tokens share the same budget as its visible
        # completion (seen on claude-haiku-4-5, config.py's
        # LLM_MODEL_STRUCTURED) can silently truncate mid-JSON well short
        # of what looks like a generous limit.
        "max_tokens": settings.llm_max_tokens,
    }
    if effort:
        kwargs["reasoning_effort"] = effort
    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base
    if model.startswith("ollama/"):
        # Ollama defaults to a 4096-token context window, which our prompts
        # (MetaScheme + MOV + embedded JSON schema) can exceed, silently
        # truncating the response to empty. Raise it explicitly.
        kwargs["num_ctx"] = settings.llm_num_ctx

    t0 = time.monotonic()
    try:
        response = _completion(kwargs)
    except Exception as exc:  # noqa: BLE001 - surface any provider error clearly
        # Some Claude models enable extended thinking under reasoning_effort
        # and then require temperature=1 outright (a value conflict, not an
        # unsupported param — litellm.drop_params doesn't cover this).
        # claude-sonnet-5 doesn't hit this (litellm routes its effort
        # through a different mechanism); claude-haiku-4-5 does. Rather than
        # hardcode which models need it, retry once at temperature=1 only
        # when the provider's own error says exactly that — every other
        # call (including this one, on any other failure) keeps the
        # caller's original temperature, which matters for the structured-
        # JSON calls' consistency.
        if effort and "temperature` may only be set to 1 when thinking is enabled" in str(exc):
            kwargs["temperature"] = 1
            try:
                response = _completion(kwargs)
            except Exception as retry_exc:  # noqa: BLE001
                raise LLMError(
                    f"LLM call failed (model={model}, "
                    f"api_base={settings.llm_api_base}): {retry_exc}"
                ) from retry_exc
        else:
            raise LLMError(
                f"LLM call failed (model={model}, "
                f"api_base={settings.llm_api_base}): {exc}"
            ) from exc
    elapsed = time.monotonic() - t0

    if settings.verbose:
        usage = getattr(response, "usage", None)
        cache_read = getattr(usage, "cache_read_input_tokens", None) if usage else None
        cache_write = getattr(usage, "cache_creation_input_tokens", None) if usage else None
        print(
            f"[debug] LLM call ({model}): {elapsed:.1f}s, input={getattr(usage, 'prompt_tokens', '?')}, "
            f"output={getattr(usage, 'completion_tokens', '?')}, "
            f"cache_read={cache_read}, cache_write={cache_write}"
        )

    content = response["choices"][0]["message"]["content"]
    if content is None:
        # Not itself informative ("why" is what actually matters — a
        # finish_reason of "length" means max_tokens was consumed entirely
        # by invisible reasoning before any visible text was written, e.g.
        # too little headroom left after LLM_MAX_TOKENS for a heavier
        # BEST_PREY_GUESS/MOV_MAINMEMORY_UPDATE JSON; anything else here
        # points to a genuine provider-side stop). Surface both rather than
        # this being a dead end the next time it happens.
        finish_reason = response["choices"][0].get("finish_reason")
        raise LLMError(
            f"LLM returned an empty response (model={model}, finish_reason={finish_reason!r})."
        )
    return content


def chat_json(
    messages: List[dict],
    temperature: float = 0.3,
    effort: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Chat completion where the model is expected to return one JSON object."""
    return _chat_json_with_retry(chat, messages, temperature, effort, model)


def transcribe_audio(file_path: str, language: Optional[str] = None) -> str:
    """Speech-to-text (config.py's STT_MODEL, OpenAI Whisper by default),
    via the same litellm entry point telegram_bot.py uses for a voice note.
    `language` is an ISO-639-1 hint (e.g. "pt") — optional, Whisper
    auto-detects when omitted."""
    try:
        with open(file_path, "rb") as f:
            response = litellm.transcription(
                model=settings.stt_model,
                file=f,
                language=language,
                timeout=settings.llm_request_timeout,
            )
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"Transcription failed (model={settings.stt_model}): {exc}") from exc

    text = (response.text or "").strip()
    if not text:
        raise LLMError("Transcription returned empty text.")
    return text


def tts_is_steerable() -> bool:
    """Whether the configured TTS model takes `instructions` at all. OpenAI documents it for gpt-4o-mini-tts (and its
    successors, all named gpt-*); tts-1 / tts-1-hd are not steerable, and their output cannot carry a per-reply emotion
    however it is asked for. A fact about the model, not a judgment about any content."""
    return settings.tts_model.lower().startswith("gpt-")


def synthesize_speech(text: str, out_path: str, delivery: Optional[str] = None) -> None:
    """Text-to-speech (config.py's TTS_MODEL/TTS_VOICE, OpenAI TTS by
    default), written to out_path as Ogg/Opus — the same container
    Telegram's own voice notes use, so telegram_bot.py can send it back via
    sendVoice with no format conversion.

    `instructions` (config.py's TTS_INSTRUCTIONS) steers tone/delivery in
    natural language — only gpt-4o-mini-tts honors it; tts-1/tts-1-hd
    ignore it via litellm.drop_params, same as every other provider-
    specific param in this file. The classic 6 voice presets (alloy/echo/
    fable/onyx/nova/shimmer) measured barely distinguishable from each
    other in Portuguese on tts-1 in this project's own testing — telling
    the model how to sound, rather than just which preset to use, is what
    actually moves it.

    `delivery` is the per-reply direction Liriel writes herself (motivation.compose_voice_delivery) — how THIS reply is
    said, from her own Feelings; it is added after the fixed TTS_INSTRUCTIONS (who the voice is), so the persona stays
    and the emotion changes with the reply.
    """
    instructions = " ".join(p for p in (settings.tts_instructions, delivery) if p)
    kwargs: dict = {
        "model": settings.tts_model,
        "voice": settings.tts_voice,
        "input": text,
        "response_format": "opus",
        "timeout": settings.llm_request_timeout,
    }
    if instructions:
        kwargs["instructions"] = instructions
    try:
        response = litellm.speech(**kwargs)
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"Speech synthesis failed (model={settings.tts_model}): {exc}") from exc

    response.write_to_file(out_path)
