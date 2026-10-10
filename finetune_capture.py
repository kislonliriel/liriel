"""
Capture of every local LLM call as raw material for a future LoRA/QLoRA fine-tune.

Why now: the MetaScheme contract is still being revised, so no training happens yet -- but every call
the local model answers (prompt in, completion out) is the only place the *exact* prompt text exists
(motivation_cycles keeps results, not prompts). Collecting it from today means that when the contract
freezes there is a corpus to curate: filter by the QA verdicts, replace the weak completions with a
stronger teacher's, and train. NOTHING here judges quality; a record is a fact (what was asked, what
came back), never a label of good or bad.

Layout (all under LIRIEL_CAPTURE_DIR, default <repo>/data/finetune/, git-ignored -- the prompts carry
the conversations):
    calls-YYYYMMDD.jsonl      one JSON object per llama-server attempt, appended
    systems/<sha16>.txt       each distinct system prompt (the MetaScheme, ~150k chars) stored ONCE;
                              a call refers to it by `system_sha`
A record: ts, tag, contract (hash of prompts.py + models.py: which revision wrote the prompt), query, cycle_id, target, backend, model, temperature, attempt, finish_reason, seconds,
timings {prompt_n, cache_n, predicted_n}, reasoning_chars (the model's hidden thinking, not stored), system_sha, messages (the non-system turns), response.
`tag` is provenance set by the caller through LIRIEL_CAPTURE_TAG (the QA runner sets "qa:<scenario>:<step>").
`query`, `cycle_id` and `target` are read off the prompt's own header lines, as plain metadata.

Controls: LIRIEL_CAPTURE=0 turns it off; LIRIEL_CAPTURE_DIR moves it. It never raises: a failure to write
is printed once and the call goes on.

    python finetune_capture.py            # corpus summary: records per query, systems, size
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parent
_LOCK = threading.Lock()
_WARNED = False


def enabled() -> bool:
    return os.environ.get("LIRIEL_CAPTURE", "1").strip() != "0"


def capture_dir() -> Path:
    return Path(os.environ.get("LIRIEL_CAPTURE_DIR") or (_ROOT / "data" / "finetune"))


def _contract_version() -> str:
    """Which version of the contract wrote the prompts: a short hash of prompts.py + models.py. A pair (prompt, completion)
    is only a lesson in the rules of the revision that produced it, so curation can keep the final revision's records and
    drop the rest. Computed once; '' when the files cannot be read."""
    try:
        h = hashlib.sha256()
        for name in ("prompts.py", "models.py"):
            h.update((_ROOT / name).read_bytes())
        return h.hexdigest()[:12]
    except OSError:
        return ""


_CONTRACT = _contract_version()

_QUERY_RE = re.compile(r"^query:\s*(\S+)", re.M)
_CYCLE_RE = re.compile(r"^cycle_id:\s*(\S+)", re.M)
_TARGET_RE = re.compile(r"^target:\s*(.+)$", re.M)


def _header(messages: List[dict]) -> dict:
    """query / cycle_id / target as they appear in the last user turn -- metadata only."""
    user = next((m.get("content") for m in reversed(messages) if m.get("role") == "user"), "") or ""
    if not isinstance(user, str):
        user = ""
    out = {}
    for key, rx in (("query", _QUERY_RE), ("cycle_id", _CYCLE_RE), ("target", _TARGET_RE)):
        m = rx.search(user)
        out[key] = m.group(1).strip() if m else None
    return out


def header(messages: List[dict]) -> dict:
    """query / cycle_id / target read off the prompt's own header lines (public: llamacpp_client sizes its output cap by query)."""
    return _header(messages)


def _store_system(directory: Path, text: str) -> str:
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    path = directory / "systems" / f"{sha}.txt"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return sha


def record(
    *,
    messages: List[dict],
    response: Optional[str],
    backend: str = "llamacpp",
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    attempt: int = 0,
    finish_reason: Optional[str] = None,
    seconds: Optional[float] = None,
    timings: Optional[dict] = None,
    reasoning_chars: Optional[int] = None,
) -> None:
    """Appends one attempt to today's file. Never raises."""
    global _WARNED
    if not enabled():
        return
    try:
        directory = capture_dir()
        directory.mkdir(parents=True, exist_ok=True)
        system = next((m.get("content") for m in messages if m.get("role") == "system"), None)
        rest = [m for m in messages if m.get("role") != "system"]
        t = timings or {}
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tag": os.environ.get("LIRIEL_CAPTURE_TAG") or None,
            "contract": _CONTRACT or None,
            **_header(messages),
            "backend": backend,
            "model": model,
            "temperature": temperature,
            "attempt": attempt,
            "finish_reason": finish_reason,
            "seconds": round(seconds, 2) if seconds is not None else None,
            "timings": {k: t.get(k) for k in ("prompt_n", "cache_n", "predicted_n")} if t else None,
            "reasoning_chars": reasoning_chars,
            "system_sha": None,
            "messages": rest,
            "response": response,
        }
        with _LOCK:
            if isinstance(system, str) and system:
                rec["system_sha"] = _store_system(directory, system)
            line = json.dumps(rec, ensure_ascii=False)
            with open(directory / f"calls-{time.strftime('%Y%m%d')}.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 - capture must never take a cycle down
        if not _WARNED:
            _WARNED = True
            print(f"[warning] finetune capture failed ({exc!r}); the call goes on without it", file=sys.stderr)


def summary(directory: Optional[Path] = None) -> str:
    d = directory or capture_dir()
    files = sorted(d.glob("calls-*.jsonl"))
    if not files:
        return f"no records under {d}"
    per_query: Counter = Counter()
    per_tag: Counter = Counter()
    length_cut = total = 0
    chars = 0
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            total += 1
            per_query[r.get("query") or "(reply / no header)"] += 1
            per_tag[(r.get("tag") or "(untagged)").split(":")[0]] += 1
            length_cut += r.get("finish_reason") == "length"
            chars += len(r.get("response") or "") + sum(len(str(m.get("content"))) for m in r.get("messages", []))
    systems = list((d / "systems").glob("*.txt"))
    lines = [f"{total} attempts in {len(files)} file(s) under {d}; ~{chars / 1e6:.1f} M chars of non-system text; "
             f"{len(systems)} distinct system prompt(s); {length_cut} cut by length"]
    lines += [f"  {n:5d}  {q}" for q, n in per_query.most_common()]
    lines += [f"  tags: " + ", ".join(f"{t}={n}" for t, n in per_tag.most_common())]
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())
