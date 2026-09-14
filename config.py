"""
Central configuration, loaded from environment variables (see .env.example).

Keeping every tunable in one place is what lets Phase 1 swap LLM providers
(local Ollama vs. an external API) and DB targets without touching the rest
of the code — only the .env file changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- LLM ---
    llm_model: str
    # Model for the cycle's 3 structured-JSON calls (GRAPH_REQUEST,
    # MOV_MAINMEMORY_UPDATE, BEST_PREY_GUESS) — separate from llm_model,
    # which stays reserved for the 4th call (reply composition, where
    # Liriel's actual voice/personality quality is what the user hears).
    # Defaults to llm_model itself (no behavior change) unless set.
    llm_model_structured: str
    llm_api_base: Optional[str]
    llm_temperature_update: float
    llm_temperature_decision: float
    llm_effort_update: str
    llm_effort_decision: str
    llm_effort_graph: str
    llm_request_timeout: float
    llm_num_ctx: int
    # No provider default is trustworthy enough to leave unset: BEST_PREY_GUESS
    # is the largest of the cycle's JSON outputs (the full objective block +
    # accompanying_objectives + handoff), and a model whose reasoning tokens
    # share the same budget as its visible output (seen on claude-haiku-4-5,
    # unlike claude-sonnet-5) can silently truncate mid-JSON well short of
    # what looks like a generous limit. Comfortable headroom above the
    # largest completion measured in this project's own testing (~5.1k
    # tokens, already truncated at that point).
    llm_max_tokens: int

    # --- Graph of Traces (MS §8) ---
    graph_max_nodes: int
    graph_max_nodes_boosted: int
    graph_recency_weight: float

    # --- Nested MOVs (MS §6.8) ---
    nested_mov_max_depth: int

    # --- Telegram front end (telegram_bot.py) ---
    telegram_bot_token: Optional[str]
    telegram_allowed_chat_ids: frozenset[str]

    # --- Voice (telegram_bot.py voice notes) ---
    # OpenAI-only for now, called through litellm like everything else in
    # llm_client.py — needs its own OPENAI_API_KEY in .env even when
    # LLM_MODEL is Claude, since Anthropic has no STT/TTS endpoint.
    stt_model: str
    tts_model: str
    tts_voice: str
    # Natural-language tone steering — only gpt-4o-mini-tts honors it
    # (ignored elsewhere via litellm.drop_params). Empty disables it.
    tts_instructions: Optional[str]

    # --- Core identity (Phase 1 policy, not an MS §12 field) ---
    protected_vov_ids: frozenset[str]

    # --- Database ---
    # Prefer the discrete PG* fields when a password contains reserved URI
    # characters (@, :, /, ?, #, ...) — building the connection as a dict
    # avoids the URL-encoding pitfalls of a single DATABASE_URL string.
    database_url: Optional[str]
    pg_host: Optional[str]
    pg_port: int
    pg_database: str
    pg_user: Optional[str]
    pg_password: Optional[str]

    # --- Liriel ---
    default_mov_id: str
    verbose: bool

    @property
    def has_discrete_pg_config(self) -> bool:
        return bool(self.pg_host and self.pg_user and self.pg_password)


def load_settings() -> Settings:
    _llm_model = os.getenv("LLM_MODEL", "ollama/gemma4:26b-a4b-it-qat")
    return Settings(
        llm_model=_llm_model,
        # Defaults to llm_model itself — set LLM_MODEL_STRUCTURED explicitly
        # to route the 3 structured-JSON calls to a cheaper model (e.g. a
        # Claude Haiku) while LLM_MODEL keeps doing the reply-composition
        # call, where Liriel's voice/personality quality is what shows.
        llm_model_structured=os.getenv("LLM_MODEL_STRUCTURED") or _llm_model,
        llm_api_base=os.getenv("LLM_API_BASE") or None,
        llm_temperature_update=float(os.getenv("LLM_TEMPERATURE_UPDATE", "0.3")),
        llm_temperature_decision=float(os.getenv("LLM_TEMPERATURE_DECISION", "0.7")),
        # Anthropic-only (ignored elsewhere via litellm.drop_params): current
        # Claude models run extended thinking on by default, which measured
        # ~87s and ~8.7k mostly-invisible reasoning tokens per call against
        # this MetaScheme-sized prompt. "low" measured 8-18s on the same
        # prompts with no loss of schema validity or reasoning quality in
        # testing — both JSON-extraction queries, so default to it there.
        # Reply composition (Phase 1's third call) is plain text, already
        # fast (~3s) and more sensitive to warmth than these two, so it's
        # left at the provider default rather than tuned here.
        llm_effort_update=os.getenv("LLM_EFFORT_UPDATE", "low"),
        llm_effort_decision=os.getenv("LLM_EFFORT_DECISION", "low"),
        # Query 1 (GRAPH_REQUEST, MS §12.1) is the same kind of structured-
        # JSON extraction task as Query 2 — deciding which ids to survey,
        # not the central judgment — so it defaults to the same low effort.
        llm_effort_graph=os.getenv("LLM_EFFORT_GRAPH", "low"),
        # 120s (fine for the short prompt this replaced) isn't enough once
        # a call routinely carries the ~18.7k-token MetaScheme plus a MOV
        # and produces a couple thousand completion tokens — that alone
        # measured well over two minutes on a local 26B model.
        llm_request_timeout=float(os.getenv("LLM_REQUEST_TIMEOUT", "600")),
        # The official MetaScheme alone is ~18.7k tokens (docs/MetaScheme_
        # Liriel_Rev0000.md, MS §0.6's own estimate), resident on every call
        # — plus the MOV and the response JSON schema. 8192 (fine for the
        # Phase-1-only summary this replaced) is nowhere near enough now.
        llm_num_ctx=int(os.getenv("LLM_NUM_CTX", "40960")),
        llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
        database_url=os.getenv("DATABASE_URL") or None,
        pg_host=os.getenv("PGHOST") or None,
        pg_port=int(os.getenv("PGPORT", "5432")),
        pg_database=os.getenv("PGDATABASE", "postgres"),
        pg_user=os.getenv("PGUSER") or None,
        pg_password=os.getenv("PGPASSWORD") or None,
        default_mov_id=os.getenv("DEFAULT_MOV_ID", "MOV_DEFAULT"),
        verbose=_get_bool("LIRIEL_VERBOSE", False),
        # graph_service.py: the max archived Objects one GRAPH_REQUEST item
        # admits into the Graph of Traces, ranked by (emotional charge,
        # recency) — MS §8/§7 leave the size to the implementation. The
        # _boosted variant applies only when the model sets
        # deep_recall_requested on that request (user explicitly insisted
        # Liriel try hard to remember something) — nothing persists after,
        # the next cycle's request just doesn't set the flag.
        graph_max_nodes=int(os.getenv("GRAPH_MAX_NODES", "12")),
        graph_max_nodes_boosted=int(os.getenv("GRAPH_MAX_NODES_BOOSTED", "40")),
        # Weight of the recency factor (in [0,1], see graph_service._recency_
        # factor) relative to raw emotional charge (typically a few units
        # per axis) when ranking archived candidates. Charge dominates by
        # design (MS §7: "organized by affective weight, not by recency");
        # this just lets recency break ties and nudge close calls.
        graph_recency_weight=float(os.getenv("GRAPH_RECENCY_WEIGHT", "2.0")),
        # MS §6.8 rule (d): "human adults sustain four or five orders before
        # performance breaks down... do not nest deeper than the QUERY
        # authorizes." This is that authorization for Phase 1/2 — how many
        # levels of nested MOV motivation.py will gather into the prompt.
        # Creating a deeper one is still possible; it just won't be shown
        # back to the model, so there's no incentive to keep populating it.
        nested_mov_max_depth=int(os.getenv("NESTED_MOV_MAX_DEPTH", "2")),
        # From @BotFather (/newbot). Only telegram_bot.py reads this — the
        # terminal chat (main.py) doesn't need it.
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        # Locks the bot to specific Telegram chats so a stranger who finds
        # its username can't drive Liriel's MOV. Comma-separated for more
        # than one person (e.g. "111111111,222222222"). Leave blank for
        # first run — telegram_bot.py prints the chat_id of any message it
        # receives while this is unset, so you can copy it in and restart
        # once you know the id(s).
        telegram_allowed_chat_ids=frozenset(
            v.strip() for v in os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").split(",") if v.strip()
        ),
        # Whisper (speech-to-text) for incoming Telegram voice notes.
        stt_model=os.getenv("STT_MODEL", "whisper-1"),
        # OpenAI TTS (text-to-speech) for the spoken reply. response_format
        # is fixed to "opus" in llm_client.py — that's Ogg/Opus, the exact
        # container Telegram's own voice notes use, so no ffmpeg conversion
        # is needed on the way back out.
        tts_model=os.getenv("TTS_MODEL", "tts-1"),
        # One of alloy/echo/fable/onyx/nova/shimmer (tts-1) — nova reads as
        # warm/clear in both English and Portuguese in OpenAI's own samples.
        tts_voice=os.getenv("TTS_VOICE", "nova"),
        tts_instructions=os.getenv("TTS_INSTRUCTIONS") or None,
        # Core identity: who Liriel is (VOV_0000) and who made her (her
        # creator VOV_0001, her developer VOV_0002) — unlike every other
        # Object, MS §7's focus/archive cycle must never apply to these.
        # Losing them from the MOV isn't "forgetting a case detail", it's
        # losing who she is. Everything else (the ongoing story, live-chat
        # Objects) archives and is retrieved via the Graph of Traces as
        # normal. Comma-separated; blank disables the guard entirely.
        protected_vov_ids=frozenset(
            v.strip() for v in os.getenv(
                "PROTECTED_VOV_IDS", "VOV_0000,VOV_0001,VOV_0002"
            ).split(",") if v.strip()
        ),
    )


settings = load_settings()
