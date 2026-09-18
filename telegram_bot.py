"""
Telegram front end for Liriel — an alternative to main.py's terminal chat
loop, driving the exact same ProcessMotivation cycle (motivation.py)
against the exact same MOV (Postgres/Supabase). Long-polls the Telegram
Bot HTTP API directly with `requests` rather than pulling in an async
framework (python-telegram-bot et al.) — consistent with the rest of
Phase 1's synchronous style, and all this needs is getUpdates/sendMessage
(+ getFile/sendVoice for voice notes).

MS §9.3: ScenarioData's form depends on embodiment, not a format — this is
simply a second embodiment (Telegram chat) alongside the terminal one,
both funneling into the same run_motivation_cycle. A voice note is the
same embodiment with an extra step on each end: llm_client.transcribe_audio
turns it into the text ScenarioData already expects, and — only when the
incoming message was itself voice — llm_client.synthesize_speech turns
Liriel's reply back into one before it goes out. A typed message still
gets a typed reply.

Usage:
    python telegram_bot.py

Setup: create a bot via @BotFather in Telegram (/newbot), then put the
token it gives you in .env as TELEGRAM_BOT_TOKEN — see README.md. Voice
notes additionally need OPENAI_API_KEY in .env (Whisper STT + OpenAI TTS,
via llm_client.py) even when LLM_MODEL is a Claude model.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import requests

# Windows' default console codepage (cp1252/cp437) can't encode every
# character a model or the MetaScheme itself may print (e.g. "→", seen for
# real crashing a verbose debug print of a JSON reply) — reconfigure stdout/
# stderr to UTF-8 unconditionally, replacing anything a given terminal font
# still can't render rather than crashing the whole cycle over one glyph.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from config import settings
from database import get_database
from llm_client import LLMError, synthesize_speech, transcribe_audio
from motivation import run_motivation_cycle

_POLL_TIMEOUT = 30  # seconds — Telegram long-polls getUpdates for this long
COMMANDS = {"/start", "/help"}
GREETING = "Oi! Sou a Liriel. Pode falar comigo normalmente, por texto ou áudio — sem precisar de comandos."


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


def _file_url(file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"


def _get_updates(offset: int | None) -> list:
    params = {"timeout": _POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(_api("getUpdates"), params=params, timeout=_POLL_TIMEOUT + 10)
    resp.raise_for_status()
    return resp.json()["result"]


def _send_message(chat_id, text: str) -> None:
    # Telegram's hard cap is 4096 chars per message; split rather than truncate.
    for i in range(0, len(text), 4000):
        resp = requests.post(
            _api("sendMessage"),
            json={"chat_id": chat_id, "text": text[i : i + 4000]},
            timeout=30,
        )
        if not resp.ok:
            print(f"[warning] sendMessage failed ({resp.status_code}): {resp.text}")


def _send_voice(chat_id, audio_path: str) -> None:
    with open(audio_path, "rb") as f:
        resp = requests.post(
            _api("sendVoice"),
            data={"chat_id": chat_id},
            files={"voice": ("reply.ogg", f, "audio/ogg")},
            timeout=60,
        )
    if not resp.ok:
        # Raise rather than just log: _deliver_reply's caller falls back to
        # a typed message on failure — a rejected upload shouldn't silently
        # leave the user with no reply at all.
        raise requests.RequestException(f"sendVoice failed ({resp.status_code}): {resp.text}")


def _download_telegram_file(file_id: str, dest_path: Path) -> None:
    meta = requests.get(_api("getFile"), params={"file_id": file_id}, timeout=30)
    meta.raise_for_status()
    remote_path = meta.json()["result"]["file_path"]
    resp = requests.get(_file_url(remote_path), timeout=60)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)


def _deliver_reply(chat_id, response_text: str, output_modality: str | None, input_was_voice: bool) -> None:
    """Chooses the reply channel and sends it. `output_modality` is
    ProcessCommandControl's own call (MS §11, via Query 3's handoff) when
    the user explicitly asked for a specific channel — it overrides the
    default of mirroring whichever channel the message itself arrived on."""
    want_voice = output_modality == "voice" or (output_modality is None and input_was_voice)
    if output_modality is not None:
        print(f"[telegram] explicit output_modality requested: {output_modality!r}")

    if not want_voice:
        _send_message(chat_id, response_text)
        return

    with tempfile.TemporaryDirectory(prefix="liriel_voice_out_") as tmp:
        reply_path = Path(tmp) / "reply.ogg"
        try:
            synthesize_speech(response_text, str(reply_path))
            # A network hiccup partway through downloading the audio from
            # OpenAI doesn't always raise — litellm's HttpxBinaryResponseContent
            # can write a truncated file with no error at all. Seen for real:
            # a ~1000-char reply that should run ~60s of audio (tts-1/opus
            # measures ~550 bytes/char in this project's own testing) arrived
            # in Telegram as a fraction-of-a-second clip. A generous floor
            # (150 bytes/char, ~27% of the measured average) catches a
            # cut-short download without false-flagging normal variance from
            # punctuation, pacing, or silence.
            min_expected_bytes = max(3000, len(response_text) * 150)
            actual_bytes = reply_path.stat().st_size
            if actual_bytes < min_expected_bytes:
                print(f"[voice synthesis error] suspiciously small audio file "
                      f"({actual_bytes} bytes for {len(response_text)} chars, "
                      f"expected >= {min_expected_bytes}) — likely a truncated "
                      f"download; sending text instead")
                _send_message(chat_id, response_text)
                return
            _send_voice(chat_id, str(reply_path))
        except (LLMError, OSError, requests.RequestException) as exc:
            print(f"[voice synthesis error] {exc}")
            # Don't lose the reply just because speaking it failed.
            _send_message(chat_id, response_text)


def _handle_voice_message(db, mov, chat_id, voice: dict):
    """Downloads the incoming voice note, transcribes it (Whisper), and
    runs the normal cycle on the resulting text — same as a typed message
    from here on, including which channel the reply goes out on."""
    with tempfile.TemporaryDirectory(prefix="liriel_voice_in_") as tmp:
        incoming_path = Path(tmp) / "incoming.ogg"
        try:
            _download_telegram_file(voice["file_id"], incoming_path)
            user_text = transcribe_audio(str(incoming_path), language="pt")
        except (requests.RequestException, LLMError) as exc:
            print(f"[voice transcription error] {exc}")
            _send_message(chat_id, "Não consegui entender o áudio agora. Pode tentar de novo, ou escrever?")
            return mov
        print(f"[telegram] chat_id={chat_id} (voice): {user_text!r}")

    try:
        response_text, mov, output_modality = run_motivation_cycle(db, mov, user_text, source="telegram_voice")
    except Exception as exc:  # noqa: BLE001
        print(f"[ProcessMotivation cycle error] {exc}")
        _send_message(chat_id, "Desculpa, tive um problema técnico agora. Tenta de novo em instantes?")
        return mov

    _deliver_reply(chat_id, response_text, output_modality, input_was_voice=True)
    return mov


def run_telegram_bot() -> None:
    if not settings.telegram_bot_token:
        print("[error] TELEGRAM_BOT_TOKEN is not set in .env — see README.md's Telegram section.")
        return

    if not settings.telegram_allowed_chat_ids:
        print(
            "[warning] TELEGRAM_ALLOWED_CHAT_ID is not set — this bot will currently "
            "reply to ANYONE who messages it. The chat_id of every incoming message "
            "is printed below; copy the ones you want into .env (comma-separated for "
            "more than one) and restart to lock it down."
        )

    db = get_database()
    mov = db.load_mov(settings.default_mov_id)
    offset = None

    if settings.llm_backend == "anthropic":
        cognition_line = f"Cognition: Anthropic/litellm — {settings.llm_model}"
    else:
        cognition_line = f"Cognition: local llama-server — {settings.llamacpp_base_url} (make sure scripts/llamacpp/start_server.sh is running)"

    print("=" * 60)
    print("Liriel — Telegram front end (Ctrl+C to stop)")
    print(cognition_line)
    print(f"Voice (STT/TTS only, still OpenAI): {settings.stt_model} / {settings.tts_model} ({settings.tts_voice})")
    print("=" * 60)

    try:
        while True:
            try:
                updates = _get_updates(offset)
            except requests.RequestException as exc:
                print(f"[warning] getUpdates failed, retrying in 5s: {exc}")
                time.sleep(5)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue  # ignore edits, channel posts, etc. for now

                chat_id = message["chat"]["id"]

                if settings.telegram_allowed_chat_ids and str(chat_id) not in settings.telegram_allowed_chat_ids:
                    print(f"[notice] ignored message from unauthorized chat_id={chat_id}")
                    continue

                voice = message.get("voice")
                if voice is not None:
                    mov = _handle_voice_message(db, mov, chat_id, voice)
                    continue

                if "text" not in message:
                    continue  # ignore photos, stickers, etc. for now

                user_text = message["text"].strip()
                print(f"[telegram] chat_id={chat_id}: {user_text!r}")

                if not user_text:
                    continue
                if user_text.lower() in COMMANDS:
                    _send_message(chat_id, GREETING)
                    continue

                try:
                    response_text, mov, output_modality = run_motivation_cycle(db, mov, user_text, source="telegram")
                except Exception as exc:  # noqa: BLE001
                    print(f"[ProcessMotivation cycle error] {exc}")
                    _send_message(chat_id, "Desculpa, tive um problema técnico agora. Tenta de novo em instantes?")
                    continue

                _deliver_reply(chat_id, response_text, output_modality, input_was_voice=False)
    finally:
        db.close()
        print("\nStopped.")


if __name__ == "__main__":
    run_telegram_bot()
