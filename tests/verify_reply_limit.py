"""REPLY_MAX_WORDS — the limit on the words of Liriel's text reply. No network, no real model.

  python tests/verify_reply_limit.py
"""
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LLM_PROFILE", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "ontology"))

from config import settings  # noqa: E402
from database import JsonFileDatabase  # noqa: E402
from models import BestPreyGuessResult  # noqa: E402
import motivation  # noqa: E402
import prompts  # noqa: E402
import verify_ontology_cycle as vc  # noqa: E402  (its seed + mocked queries: one whole cycle without a model)

limit = motivation._limit_reply_words
words = lambda t: len(t.split())  # noqa: E731

# 1. the cut ----------------------------------------------------------------------------------------------------
three = "I understood what you said about Bianca today. Let us talk calmly about this right now. Afterwards I will come back to talk with Marcos."
assert limit(three, 0) == three and limit(three, words(three)) == three and limit(three, 500) == three
cut = limit(three, 14)  # 8 + 8 + 9 words: 14 fits the first sentence and a bit of the second
assert cut == "I understood what you said about Bianca today.", cut
assert words(limit(three, 12)) <= 12 and limit(three, 12).endswith(".")

one_long = " ".join(["word"] * 40)  # no sentence end anywhere: hard cut at the limit, marked
hard = limit(one_long, 10)
assert words(hard) == 10 and hard.endswith("…") and "word…" in hard, hard

paragraphs = "A short first paragraph here.\n\nA second paragraph that will go past the agreed limit."
assert limit(paragraphs, 7) == "A short first paragraph here.", repr(limit(paragraphs, 7))
assert limit('She said "I will call him now." Then one more long sentence that does not fit.', 8) == 'She said "I will call him now."'
assert limit(three, -3) == three  # a negative limit is no limit
print("[check] the cut: no-op at 0/under/at the limit; back to the last complete sentence; hard cut + ellipsis only when no sentence fits; "
      "paragraphs and closing quotes respected")

# 2. the prompt ask ---------------------------------------------------------------------------------------------------
decision = BestPreyGuessResult.model_validate({
    "query": "BEST_PREY_GUESS", "accompanying_objectives": [], "handoff_to_processcommandcontrol": {},
    "best_prey_guess": {"vov_id": "Objective_X", "brief_description": "x", "relevant_relations": []},
})
on = prompts.build_reply_prompt(decision, "oi", None, None, max_words=150)[1]["content"]
off = prompts.build_reply_prompt(decision, "oi", None, None)[1]["content"]
assert "at most 150 words" in on and "LENGTH LIMIT" in on and "LENGTH LIMIT" not in off
assert on.rstrip().endswith("Write your reply now, as Liriel, to the user.") and "{length_rule}" not in on
print("[check] the reply prompt asks for the limit when set, and carries no trace of it when not")

# 3. the flag ------------------------------------------------------------------------------------------------------------
def flag(value):
    env = {k: v for k, v in os.environ.items() if k != "REPLY_MAX_WORDS"}
    if value is not None:
        env["REPLY_MAX_WORDS"] = value
    env["LLM_PROFILE"] = "1"
    out = subprocess.run([sys.executable, "-c", "from config import settings; print(settings.reply_max_words)"],
                         cwd=ROOT, env=env, capture_output=True, text=True)
    return out.stdout.strip().splitlines()[-1] if out.returncode == 0 else out.stderr.strip().splitlines()[-1]


assert flag("150") == "150" and flag("0") == "0" and flag("-5") == "0"
# .env is read by config.py itself, so "no REPLY_MAX_WORDS in the environment" may still pick up the user's .env:
# the shipped default is what .env.example says
assert "REPLY_MAX_WORDS=0" in (ROOT / ".env.example").read_text(encoding="utf-8")
print("[check] REPLY_MAX_WORDS: 150 -> 150, 0 -> 0 (off), negative -> 0; shipped off in .env.example")

# 4. a whole cycle, reply too long ----------------------------------------------------------------------------------------
LONG = ("I understood what you told me about Clara. " * 4 + "I want to help you with this. " * 4 +
        "Let us think together about the next step. " * 4).strip()
HERE = Path(tempfile.mkdtemp(prefix="liriel_reply_"))
atexit.register(shutil.rmtree, HERE, ignore_errors=True)


def cycle(max_words):
    object.__setattr__(settings, "reply_max_words", max_words)
    db = JsonFileDatabase(path=HERE / f"store_{max_words}.json")
    vc.seed(db)
    asked = []

    def fake_chat(messages, temperature=None):
        asked.append(messages[1]["content"])
        return "  " + LONG + "  "

    calls, prompts_by_query = [], {}
    with patch.object(motivation, "chat_json", side_effect=vc.responses(prompts_by_query, calls)), \
         patch.object(motivation, "chat", side_effect=fake_chat):
        reply, _, _ = motivation.run_motivation_cycle(db, db.load_mov(vc.MOV), "Did Clara dye her hair?", source="verify")
    import json
    logged = json.loads(db._log_path.read_text(encoding="utf-8").splitlines()[-1])["response_text"]
    return reply, logged, asked[0]


try:
    reply, logged, asked = cycle(30)
    assert words(reply) <= 30 and reply.endswith((".", "!", "?")), reply
    assert logged == reply, "the log holds a different text than the one sent"
    assert "at most 30 words" in asked
    reply0, logged0, asked0 = cycle(0)
    assert reply0 == LONG and logged0 == LONG and "LENGTH LIMIT" not in asked0
finally:
    object.__setattr__(settings, "reply_max_words", 0)
print(f"[check] one whole cycle: a {words(LONG)}-word reply comes out at {words(reply)} words (a complete sentence), "
      "that same text is what is logged; with 0 it is untouched")

print("\nALL CHECKS PASSED")
