"""finetune_capture + its hook in llamacpp_client, no network: the llama-server call is mocked."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp(prefix="capture_"))
os.environ["LIRIEL_CAPTURE_DIR"] = str(tmp)
os.environ["LIRIEL_CAPTURE_TAG"] = "qa:demo:01_x"
os.environ.pop("LIRIEL_CAPTURE", None)

import finetune_capture as fc  # noqa: E402
import llamacpp_client as lc  # noqa: E402

SYSTEM = "METASCHEME " * 5000
USER = "ARTIFACT: x\n\nQUERY\nprocess: ProcessMotivation\ncycle_id: cycle_1\nquery: ANCHOR_REVIEW\ntarget: mov_id=M vov_id=V owner_vov_id=O\ntask: >\n  ...\n"
MSGS = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}]


def fake_post(messages, n_predict, **kw):
    return {"model": "gemma-test", "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
            "timings": {"prompt_n": 12, "cache_n": 900, "predicted_n": 7, "other": 1}}


lc._client._post_chat = fake_post

print("[check] one chat() call writes one record with header metadata, tag, timings and the system prompt by hash")
assert lc.chat(MSGS) == '{"ok": true}'
files = list(tmp.glob("calls-*.jsonl"))
assert len(files) == 1
recs = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines()]
assert len(recs) == 1
r = recs[0]
assert r["query"] == "ANCHOR_REVIEW" and r["cycle_id"] == "cycle_1" and r["target"].startswith("mov_id=M")
assert r["contract"] and len(r["contract"]) == 12
assert r["tag"] == "qa:demo:01_x" and r["model"] == "gemma-test" and r["finish_reason"] == "stop" and r["attempt"] == 0
assert r["timings"] == {"prompt_n": 12, "cache_n": 900, "predicted_n": 7}
assert r["messages"] == [MSGS[1]] and r["response"] == '{"ok": true}'
assert (tmp / "systems" / f"{r['system_sha']}.txt").read_text(encoding="utf-8") == SYSTEM

print("[check] the same system prompt is stored once, however many calls use it (also from 8 threads at once)")
ts = [threading.Thread(target=lambda: [lc.chat(MSGS) for _ in range(5)]) for _ in range(8)]
[t.start() for t in ts]
[t.join() for t in ts]
lines = files[0].read_text(encoding="utf-8").splitlines()
assert len(lines) == 41, len(lines)
assert all(json.loads(l)["query"] == "ANCHOR_REVIEW" for l in lines)  # no interleaved / torn lines
assert len(list((tmp / "systems").glob("*.txt"))) == 1

print("[check] a retry after finish_reason=length records BOTH attempts")
calls = {"n": 0}


def cut_then_ok(messages, n_predict, **kw):
    calls["n"] += 1
    if calls["n"] == 1:
        return {"choices": [{"message": {"content": "{trunc"}, "finish_reason": "length"}]}
    return fake_post(messages, n_predict)


lc._client._post_chat = cut_then_ok
before = len(lines)
lc.chat(MSGS)
new = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines()[before:]]
assert [(x["attempt"], x["finish_reason"]) for x in new] == [(0, "length"), (1, "stop")], new

print("[check] after a length cut the second attempt samples differently (stronger repetition penalty, a bit more temperature)")
got = []


def cut_then_ok2(messages, n_predict, **kw):
    got.append(kw)
    if len(got) == 1:
        return {"choices": [{"message": {"content": "{trunc"}, "finish_reason": "length"}]}
    return fake_post(messages, n_predict)


lc._client._post_chat = cut_then_ok2
lc.chat(MSGS, temperature=0.3)
assert got[0] == {"temperature": 0.3, "thinking_budget_tokens": 0}, got   # ANCHOR_REVIEW: no hidden thinking
assert got[1]["repeat_penalty"] == 1.2 and got[1]["repeat_last_n"] == 256 and got[1]["temperature"] == 0.5 and got[1]["thinking_budget_tokens"] == 0, got
lc._client._post_chat = fake_post

print("[check] only MAINMEMORY_FILING is allowed to think (bounded); every other query, and the retry, does not")
cap = []
lc._client._post_chat = lambda messages, n_predict, **kw: (cap.append(kw.get("thinking_budget_tokens")), fake_post(messages, n_predict))[1]
lc.chat([MSGS[0], {"role": "user", "content": USER.replace("ANCHOR_REVIEW", "MAINMEMORY_FILING")}])
lc.chat([MSGS[0], {"role": "user", "content": USER.replace("ANCHOR_REVIEW", "BEST_PREY_GUESS")}])
lc.chat([{"role": "user", "content": "just a reply prompt"}])
assert cap == [2048, 0, 0], cap
lc._client._post_chat = fake_post

print("[check] an EMPTY answer that ended with finish_reason=stop (thinking budget spent) is retried without thinking")
got2 = []


def empty_then_ok(messages, n_predict, **kw):
    got2.append(kw)
    if len(got2) == 1:
        return {"choices": [{"message": {"content": "\n", "reasoning_content": "thinking ..."}, "finish_reason": "stop"}]}
    return fake_post(messages, n_predict)


lc._client._post_chat = empty_then_ok
assert lc.chat(MSGS, temperature=0.3) == '{"ok": true}'
assert got2[1]["thinking_budget_tokens"] == 0, got2
lc._client._post_chat = fake_post

print("[check] a call without a system prompt or header still records; the summary reads the corpus")
fc.record(messages=[{"role": "user", "content": "just a reply prompt"}], response="oi")
s = fc.summary(tmp)
assert "ANCHOR_REVIEW" in s and "reply / no header" in s and "1 distinct system prompt" in s, s

print("[check] the output cap follows the query (a repetition loop costs time in proportion to it); unlisted queries keep the default")
seen = []


def spy(messages, n_predict, **kw):
    seen.append(n_predict)
    return fake_post(messages, n_predict)


lc._client._post_chat = spy
lc.chat(MSGS)  # ANCHOR_REVIEW
lc.chat([MSGS[0], {"role": "user", "content": USER.replace("ANCHOR_REVIEW", "RELATIONS_UPDATE")}])
lc.chat([MSGS[0], {"role": "user", "content": USER.replace("ANCHOR_REVIEW", "BEST_PREY_GUESS")}])
lc.chat([{"role": "user", "content": "just a reply prompt"}])
from config import settings  # noqa: E402
assert seen == [6144, 7168, settings.llm_max_tokens, settings.llm_max_tokens], seen

print("[check] LIRIEL_CAPTURE=0 writes nothing; a write failure never raises")
os.environ["LIRIEL_CAPTURE"] = "0"
n0 = len(files[0].read_text(encoding="utf-8").splitlines())
lc._client._post_chat = fake_post
lc.chat(MSGS)
assert len(files[0].read_text(encoding="utf-8").splitlines()) == n0
os.environ["LIRIEL_CAPTURE"] = "1"
os.environ["LIRIEL_CAPTURE_DIR"] = str(files[0])  # a FILE where a directory is expected
assert lc.chat(MSGS) == '{"ok": true}'

print("\nALL CHECKS PASSED")
