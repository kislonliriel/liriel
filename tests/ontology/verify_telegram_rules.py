"""The rules of the Telegram front end, with no network: the allowed-chat list is mandatory (no list, no bot), a chat that is
not listed gets nothing, every conversation opens with the AI disclosure (once per chat per run, and /start is answered by it),
the messages are English, and --whoami only prints chat ids: it never replies and runs no cycle."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import telegram_bot  # noqa: E402
from config import settings  # noqa: E402


def msg(update_id, chat_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "from": {"first_name": "Tess"}, "text": text}}


def run(argv, allowed, batches):
    """Runs the bot loop over `batches` of updates, then stops it; returns (exit code, messages sent, cycles run)."""
    sent, cycles = [], []
    feed = iter(batches)

    def updates(_offset):
        try:
            return next(feed)
        except StopIteration:
            raise KeyboardInterrupt

    def cycle(db, mov, text, source, sender):
        cycles.append(text)
        return f"reply to {text}", mov, None

    object.__setattr__(settings, "telegram_bot_token", "test-token")
    object.__setattr__(settings, "telegram_allowed_chat_ids", frozenset(allowed))
    db = SimpleNamespace(load_mov=lambda _id: object(), close=lambda: None)
    code = 0
    with patch.object(sys, "argv", ["telegram_bot.py", *argv]), \
         patch.object(telegram_bot, "_get_updates", side_effect=updates), \
         patch.object(telegram_bot, "_send_message", side_effect=lambda chat, text: sent.append((chat, text))), \
         patch.object(telegram_bot, "run_motivation_cycle", side_effect=cycle), \
         patch.object(telegram_bot, "get_database", return_value=db):
        try:
            telegram_bot.run_telegram_bot()
        except SystemExit as exc:
            code = exc.code
        except KeyboardInterrupt:
            pass
    return code, sent, cycles


print("[check] no allowed-chat list: the bot refuses to start, reads no message, answers no one")
code, sent, cycles = run([], [], [[msg(1, 5, "hello")]])
assert code == 1 and sent == [] and cycles == [], (code, sent, cycles)

print("[check] a chat that is not listed gets nothing; a listed one gets the AI disclosure first, once, then the replies")
code, sent, cycles = run([], ["5", "6"], [
    [msg(1, 9, "hello"), msg(2, 5, "/start"), msg(3, 5, "hello"), msg(4, 5, "/start"), msg(5, 6, "hi there")],
])
D = telegram_bot.AI_DISCLOSURE
assert all(chat != 9 for chat, _ in sent), sent
assert sent == [(5, D), (5, "reply to hello"), (5, D), (6, D), (6, "reply to hi there")], sent
assert cycles == ["hello", "hi there"], cycles

print("[check] the disclosure says it is an AI and not a person, in English; every fixed message is English")
assert "an AI" in D and "not a person" in D and "Claude" in D and "emergency" in D
for text in (D, telegram_bot.TRANSCRIPTION_FAILED, telegram_bot.CYCLE_FAILED):
    assert text.isascii() and not any(w in text.lower().split() for w in ("não", "você", "desculpa", "oi")), text

print("[check] --whoami prints chat ids only: no reply, no cycle, and it needs no allowed-chat list")
code, sent, cycles = run(["--whoami"], [], [[msg(1, 5, "hello"), msg(2, 9, "/start")]])
assert code == 0 and sent == [] and cycles == [], (code, sent, cycles)

print("\nALL CHECKS PASSED")
