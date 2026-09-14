"""
LLM abstraction layer.

Uses litellm so that switching providers is a matter of changing LLM_MODEL
(and, if needed, an API key) in .env — no code changes required. Examples:

    LLM_MODEL=ollama/gemma2          # local Ollama (default for Phase 1)
    LLM_MODEL=gpt-4o-mini            # OpenAI (needs OPENAI_API_KEY)
    LLM_MODEL=claude-sonnet-5        # Anthropic (needs ANTHROPIC_API_KEY)
"""
from __future__ import annotations

import json
import re
import time
from typing import List, Optional

import litellm

from config import settings

# Some providers reject sampling params we pass unconditionally. Notably,
# current Claude models (e.g. claude-sonnet-5) run with extended thinking on
# by default, and the Anthropic API then requires temperature=1 — it rejects
# any other explicit value instead of ignoring it. Since Phase 1's whole
# point is "swap LLM_MODEL in .env, no code changes", we don't want a
# provider-specific branch here: let litellm silently drop a param a given
# provider doesn't support (falling back to that provider's default) rather
# than erroring out.
litellm.drop_params = True


class LLMError(RuntimeError):
    """Raised when the LLM call fails or returns something we can't parse."""


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
        response = litellm.completion(**kwargs)
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
                response = litellm.completion(**kwargs)
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


def chat_json(
    messages: List[dict],
    temperature: float = 0.3,
    effort: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Chat completion where the model is expected to return one JSON object."""
    raw = chat(messages, temperature=temperature, effort=effort, model=model)
    return _extract_json(raw)


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


def synthesize_speech(text: str, out_path: str) -> None:
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
    """
    kwargs: dict = {
        "model": settings.tts_model,
        "voice": settings.tts_voice,
        "input": text,
        "response_format": "opus",
        "timeout": settings.llm_request_timeout,
    }
    if settings.tts_instructions:
        kwargs["instructions"] = settings.tts_instructions
    try:
        response = litellm.speech(**kwargs)
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"Speech synthesis failed (model={settings.tts_model}): {exc}") from exc

    response.write_to_file(out_path)
