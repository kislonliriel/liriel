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

For your OWN chat only: the bot refuses to start without
TELEGRAM_ALLOWED_CHAT_ID, and it never answers a chat that is not listed.
Anthropic's official Claude Code "channels" feature
(https://code.claude.com/docs/en/channels) is the alternative way to reach
your own Claude Code session from a chat app.

Usage:
    python telegram_bot.py --whoami   # prints the chat_id of each message it receives; never replies, runs no cycle
    python telegram_bot.py            # the bot itself, for the chats in TELEGRAM_ALLOWED_CHAT_ID

Setup: create a bot via @BotFather in Telegram (/newbot), put the token it
gives you in .env as TELEGRAM_BOT_TOKEN, run --whoami and message the bot
to learn your chat_id, then put it in .env as TELEGRAM_ALLOWED_CHAT_ID —
see README.md. Voice notes additionally need OPENAI_API_KEY in .env
(Whisper STT + OpenAI TTS, via llm_client.py) even when LLM_MODEL is a
Claude model.

Every conversation starts with an AI disclosure (AI_DISCLOSURE): on /start,
and on the first message from each chat after the bot starts.
"""
from __future__ import annotations

import sys
import tempfile
import time
import traceback
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
from llm_client import LLMError, synthesize_speech, transcribe_audio, tts_is_steerable
from motivation import compose_voice_delivery, run_motivation_cycle

_POLL_TIMEOUT = 30  # seconds — Telegram long-polls getUpdates for this long
COMMANDS = {"/start", "/help"}
# Said at the start of every conversation (Anthropic's usage policy: tell people they are talking to an AI).
AI_DISCLOSURE = (
    "Hi! I'm Liriel, an AI: a Persistent Cognitive Instance whose judgment is an AI model (Claude), not a person. "
    "This is a developer test environment, not a crisis or medical service; if you are in danger, contact your local "
    "emergency services. You can write to me or send a voice note, no commands needed."
)
TRANSCRIPTION_FAILED = "I couldn't understand the audio just now. Could you try again, or write it instead?"
CYCLE_FAILED = "Sorry, I had a technical problem just now. Could you try again in a moment?"


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


def _voice_delivery(user_text: str | None, response_text: str, mov) -> str | None:
    """The emotional direction of the spoken reply, written by Liriel herself (motivation.compose_voice_delivery) from her
    own Feelings and the words she is about to say. Only when the TTS model can be steered at all (tts-1 cannot, so the
    call is not spent on it) and TTS_DELIVERY is not 0. None = the reply is spoken as it always was."""
    if mov is None or not settings.tts_delivery or not tts_is_steerable():
        return None
    delivery = compose_voice_delivery(user_text or "", response_text, mov.get(settings.liriel_self_vov_id))
    if delivery:
        print(f"[telegram] voice direction: {delivery}")
    return delivery


def _deliver_reply(
    chat_id, response_text: str, output_modality: str | None, input_was_voice: bool,
    user_text: str | None = None, mov=None,
) -> None:
    """Chooses the reply channel and sends it. `output_modality` is
    ProcessCommandControl's own call (MS §11, via Query 3's handoff) when
    the user explicitly asked for a specific channel — it overrides the
    default of mirroring whichever channel the message itself arrived on.
    `user_text` and `mov` (the MOV the cycle just refreshed) only feed the
    voice direction of a spoken reply."""
    want_voice = output_modality == "voice" or (output_modality is None and input_was_voice)
    if output_modality is not None:
        print(f"[telegram] explicit output_modality requested: {output_modality!r}")

    if not want_voice:
        _send_message(chat_id, response_text)
        return

    with tempfile.TemporaryDirectory(prefix="liriel_voice_out_") as tmp:
        reply_path = Path(tmp) / "reply.ogg"
        try:
            synthesize_speech(response_text, str(reply_path), delivery=_voice_delivery(user_text, response_text, mov))
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


def _sender_name(message: dict) -> str | None:
    """Rev 0007 V: who the CHANNEL says wrote the message — the Telegram account's own name, a fact of the channel and not a guess.
    Without it ProcessMotivation's Query 1 reads the author from the text alone, and a plain "hi, how are you?" comes back "unknown":
    an unknown author is shown none of the household (Rev 0007 AK), so Liriel could not even remember who she talks to."""
    sender = message.get("from") or {}
    return sender.get("first_name") or sender.get("username") or None


def _handle_voice_message(db, mov, chat_id, voice: dict, sender: str | None = None):
    """Downloads the incoming voice note, transcribes it (Whisper), and
    runs the normal cycle on the resulting text — same as a typed message
    from here on, including which channel the reply goes out on."""
    with tempfile.TemporaryDirectory(prefix="liriel_voice_in_") as tmp:
        incoming_path = Path(tmp) / "incoming.ogg"
        try:
            _download_telegram_file(voice["file_id"], incoming_path)
            user_text = transcribe_audio(str(incoming_path))  # no language hint: Liriel answers in the person's language
        except (requests.RequestException, LLMError) as exc:
            print(f"[voice transcription error] {exc}")
            _send_message(chat_id, TRANSCRIPTION_FAILED)
            return mov
        print(f"[telegram] chat_id={chat_id} (voice): {user_text!r}")

    try:
        response_text, mov, output_modality = run_motivation_cycle(db, mov, user_text, source="telegram_voice", sender=sender)
    except Exception as exc:  # noqa: BLE001
        print(f"[ProcessMotivation cycle error] {exc}")
        traceback.print_exc()
        _send_message(chat_id, CYCLE_FAILED)
        return mov

    _deliver_reply(chat_id, response_text, output_modality, input_was_voice=True, user_text=user_text, mov=mov)
    return mov


def run_whoami() -> None:
    """Prints the chat_id of every message the bot receives, so its owner can fill TELEGRAM_ALLOWED_CHAT_ID. Never replies,
    never runs a cycle, writes nothing: the id is shown on this console only."""
    print("Waiting for messages: send anything to your bot from your own Telegram account (Ctrl+C to stop).")
    print("Nothing is answered in this mode.")
    offset = None
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
                if message:
                    print(f"chat_id={message['chat']['id']}  ->  put it in .env as TELEGRAM_ALLOWED_CHAT_ID")
    except KeyboardInterrupt:
        print("\nStopped.")


def run_telegram_bot() -> None:
    if not settings.telegram_bot_token:
        print("[error] TELEGRAM_BOT_TOKEN is not set in .env — see README.md's Telegram section.")
        return

    if "--whoami" in sys.argv[1:]:
        run_whoami()
        return

    if not settings.telegram_allowed_chat_ids:
        print(
            "[error] TELEGRAM_ALLOWED_CHAT_ID is not set in .env: the bot only talks to the chats listed there, and refuses "
            "to start without the list. Run `python telegram_bot.py --whoami`, message your bot from your own Telegram "
            "account, and put the chat_id it prints in .env (comma-separated for more than one)."
        )
        sys.exit(1)

    disclosed: set[str] = set()  # chats told this run that Liriel is an AI; kept in memory only, never written anywhere

    db = get_database()
    mov = db.load_mov(settings.default_mov_id)
    offset = None

    if settings.llm_backend == "litellm":
        cognition_line = f"Cognition: litellm — {settings.llm_model}"
    elif settings.llm_backend == "claude_session":
        cognition_line = "Cognition: a Claude session answering through data/claude_session/ (scripts/claude_session/session_cli.py) — no model API, no local server"
    else:
        cognition_line = f"Cognition: local llama-server — {settings.llamacpp_base_url} (make sure a llama-server is running there)"

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

                if str(chat_id) not in settings.telegram_allowed_chat_ids:
                    print(f"[notice] ignored message from unauthorized chat_id={chat_id}")
                    continue

                text = (message.get("text") or "").strip()
                if str(chat_id) not in disclosed:
                    disclosed.add(str(chat_id))
                    _send_message(chat_id, AI_DISCLOSURE)
                    if text.lower() in COMMANDS:
                        continue  # /start or /help: the disclosure is the whole answer

                voice = message.get("voice")
                if voice is not None:
                    mov = _handle_voice_message(db, mov, chat_id, voice, sender=_sender_name(message))
                    continue

                if "text" not in message:
                    continue  # ignore photos, stickers, etc. for now

                user_text = message["text"].strip()
                print(f"[telegram] chat_id={chat_id}: {user_text!r}")

                if not user_text:
                    continue
                if user_text.lower() in COMMANDS:
                    _send_message(chat_id, AI_DISCLOSURE)
                    continue

                try:
                    response_text, mov, output_modality = run_motivation_cycle(db, mov, user_text, source="telegram", sender=_sender_name(message))
                except Exception as exc:  # noqa: BLE001
                    print(f"[ProcessMotivation cycle error] {exc}")
                    traceback.print_exc()
                    _send_message(chat_id, CYCLE_FAILED)
                    continue

                _deliver_reply(chat_id, response_text, output_modality, input_was_voice=False, user_text=user_text, mov=mov)
    finally:
        db.close()
        print("\nStopped.")


if __name__ == "__main__":
    run_telegram_bot()
