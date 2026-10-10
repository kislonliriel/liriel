"""The `claude_session` backend: the cycle's model calls become request files a Claude session answers (LLM_PROFILE=3).
No network, no model. The cycle code is the real one; this checks only the folder protocol and the backend switch."""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SESSION = Path(tempfile.mkdtemp(prefix="verify_claude_session_"))
atexit.register(shutil.rmtree, SESSION, ignore_errors=True)
os.environ["LIRIEL_SESSION_DIR"] = str(SESSION)
os.environ["LLM_PROFILE"] = "3"

GROQ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GROQ))

import claude_session_client as cs  # noqa: E402
import motivation  # noqa: E402
from config import settings  # noqa: E402

CLI = GROQ / "scripts" / "claude_session" / "session_cli.py"


def cli(*args, wait=4):
    out = subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def ask_in_thread(fn, *a, **kw):
    box = {}

    def run():
        try:
            box["value"] = fn(*a, **kw)
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def msgs(query, body="x"):
    return [{"role": "system", "content": "META SCHEME"}, {"role": "user", "content": f"QUERY\nprocess: P\nquery: {query}\n\n{body}"}]


print("[check] LLM_PROFILE=3 binds the cycle to this backend; nothing else is imported for inference")
assert settings.llm_backend == "claude_session"
assert motivation.chat is cs.chat and motivation.chat_json is cs.chat_json

print("[check] a request appears, is printed whole, a malformed JSON answer is refused and the request stays pending, a valid one is sent")
t, box = ask_in_thread(cs.chat_json, msgs("MOV_UPDATE"), temperature=0.3)
time.sleep(1.5)
out = cli("next", "--wait", "5")
assert "=== REQUEST 000001 | query: MOV_UPDATE | temperature 0.3 | answer: JSON object (ans_000001.json)" in out and "META SCHEME" not in out, out
(SESSION / "ans_000001.json").write_text('{"query": "MOV_UPDATE", "mov_ops": [', encoding="utf-8")
out = cli("next", "--wait", "3")
assert "NOT VALID JSON" in out and "=== REQUEST 000001" in out and not (SESSION / "resp_000001.txt").exists(), out
(SESSION / "ans_000001.json").write_text('{"query": "MOV_UPDATE", "mov_ops": []}', encoding="utf-8")
out = cli("next", "--wait", "2")
assert "[answer 000001] sent" in out and "NO PENDING" in out, out
t.join(10)
assert box.get("value") == {"query": "MOV_UPDATE", "mov_ops": []}, box

print("[check] a plain-text request returns the answer text as it was written")
t, box = ask_in_thread(cs.chat, msgs("REPLY"), temperature=0.7)
time.sleep(1.5)
assert "answer: plain text (ans_000002.txt)" in cli("next", "--wait", "5")
(SESSION / "ans_000002.txt").write_text("Hi! All good here... and with you?", encoding="utf-8")
assert "[answer 000002] sent" in cli("next", "--wait", "2")
t.join(10)
assert box.get("value") == "Hi! All good here... and with you?", box

print("[check] within one cycle a long block already shown is pointed to, not repeated; the same request shown twice is whole; a new cycle starts afresh")
big = "ARTIFACT: the MOV\n" + "row of the MOV, long enough to count as a block. " * 40


def one(query, extra, n):
    t, box = ask_in_thread(cs.chat_json, msgs(query, big + "\n\n" + extra))
    time.sleep(1.5)
    out = cli("next", "--wait", "5")
    return t, box, out


t, box, out = one("SAFETY_SCREEN", "first", 3)
assert big.splitlines()[1] in out
(SESSION / "ans_000003.json").write_text("{}", encoding="utf-8")
cli("next", "--wait", "1"); t.join(10)
t, box, out = one("ANCHOR_REVIEW", "second", 4)
assert "identical to the block shown in request 000003" in out and big.splitlines()[1] not in out and "second" in out, out
again = cli("next", "--wait", "3")                      # the same request, shown a second time: whole
assert big.splitlines()[1] in again and "identical to the block" not in again
assert big.splitlines()[1] in cli("next", "--wait", "3", "--full")
(SESSION / "ans_000004.json").write_text("{}", encoding="utf-8")
cli("next", "--wait", "1"); t.join(10)
t, box, out = one("SAFETY_SCREEN", "third", 5)
assert "identical to the block shown in request 000004" in out and big.splitlines()[1] not in out   # a new cycle: the same block is still pointed to
(SESSION / "ans_000005.json").write_text("{}", encoding="utf-8")
cli("next", "--wait", "1"); t.join(10)

print("[check] the answer shape is never folded; `show` prints a request whole; `fresh` makes the next one whole; an old block is whole again")
NL = chr(10)
shape = "Respond with ONLY a JSON object shaped exactly like this example:" + NL + ('{ "field_of_the_shape": "x" }' + NL) * 40
body_with_shape = big + NL + NL + shape


def ask_shape():
    t, box = ask_in_thread(cs.chat_json, msgs("SAFETY_SCREEN", body_with_shape))
    time.sleep(1.5)
    return t


t = ask_shape()
out = cli("next", "--wait", "5")
assert "identical to the block" in out and "field_of_the_shape" in out          # the big block folded, the shape whole
(SESSION / "ans_000006.json").write_text("{}", encoding="utf-8")
cli("next", "--wait", "1"); t.join(10)
t = ask_shape()
out = cli("show", "7")
assert big.splitlines()[1] in out and "identical to the block" not in out
assert "identical to the block" in cli("next", "--wait", "5")                  # `show` did not change what counts as shown
(SESSION / "ans_000007.json").write_text("{}", encoding="utf-8")
cli("next", "--wait", "1"); t.join(10)
cli("fresh")
t = ask_shape()
out = cli("next", "--wait", "5")
assert big.splitlines()[1] in out and "identical to the block" not in out
(SESSION / "ans_000008.json").write_text("{}", encoding="utf-8")
cli("next", "--wait", "1"); t.join(10)


print("[check] an answer the query's model would refuse is rejected here and the request stays pending; a good one goes through")
t, box = ask_in_thread(cs.chat_json, msgs("MAINMEMORY_FILING", "ARTIFACT: x"))
time.sleep(1.5)
assert "REQUEST" in cli("next", "--wait", "5")
bad = json.dumps({"query": "MAINMEMORY_FILING", "cycle_id": "c", "archive": [], "restore": ["Sentient_X"], "notes": ""})
(SESSION / "ans_000009.json").write_text(bad, encoding="utf-8")
out = cli("next", "--wait", "2")
assert "REJECTED" in out and "restore.0" in out and not (SESSION / "resp_000009.txt").exists(), out
good = json.dumps({"query": "MAINMEMORY_FILING", "cycle_id": "c", "archive": [], "restore": [{"vov_id": "Sentient_X", "reason": "named"}], "notes": ""})
(SESSION / "ans_000009.json").write_text(good, encoding="utf-8")
assert "[answer 000009] sent" in cli("next", "--wait", "2")
t.join(10)

print("[check] a request longer than the shell shows goes to part files, none of it lost")
huge = "line of the prompt number %d with some words in it\n" * 1
body = "\n".join(f"line of the prompt number {i} with some words in it" for i in range(1500))
t, box = ask_in_thread(cs.chat_json, msgs("MOV_UPDATE", body))
time.sleep(1.5)
out = cli("next", "--wait", "5")
parts = [line.strip() for line in out.splitlines() if line.strip().endswith(".md")]
assert "LONG REQUEST" in out and len(parts) >= 2, out
joined = "".join(Path(p).read_text(encoding="utf-8") for p in parts)
assert "line of the prompt number 0 " in joined and "line of the prompt number 1499 " in joined
(SESSION / "ans_000010.json").write_text("{}", encoding="utf-8")
cli("next", "--wait", "1"); t.join(10)

print("[check] a request whose cycle is gone (no alive signal) is not served; the system prompt can be read once")
(SESSION / "req_000011.json").write_text(json.dumps({"id": 11, "expects": "json", "temperature": 0.3, "effort": None, "model": None,
                                                     "query": "ORPHAN", "messages": msgs("ORPHAN")}), encoding="utf-8")
assert "NO PENDING" in cli("next", "--wait", "2")
assert "META SCHEME" in cli("system")
print("\nALL CHECKS PASSED")
