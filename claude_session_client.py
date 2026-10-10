"""
Third inference backend for motivation.py: the model is an agent at the other end of a folder.

Same call signature as llm_client.chat()/chat_json() and llamacpp_client.chat()/chat_json() (a `messages` list,
`temperature`, `effort`, `model`), but nothing is sent to a model API or a local server: every call is written as a
request file in data/claude_session/ and the cycle WAITS until an answer file for it appears. Whoever answers (a Claude
session, driven by scripts/claude_session/session_cli.py) reads the prompt exactly as the code built it -- MetaScheme as
system prompt, the artifacts, the query -- and writes what the contract asks for. The cycle around it is untouched: the
same prompts, the same validation and repairs, the same draft database committed once at the end, the same Telegram front
end. Only WHO fills in each judgment changes.

    LLM_PROFILE=3        (config.py) selects this backend; scripts/claude_session/session_cli.py serves it.

Protocol (all under SESSION_DIR, git-ignored: the prompts carry the conversations):
    req_000123.json   written by this module: {id, expects: "json"|"text", temperature, effort, model, query, messages}
    alive_000123      touched by this module while it waits (a request whose alive file went stale is orphaned: its
                      cycle died with the process that asked)
    ans_000123.json   written by the answering side (.txt for a plain-text request); the CLI validates it
    resp_000123.txt   what the CLI promotes a valid answer to; this module returns its text

There is no timeout: a cycle takes as long as the answering side takes. `finetune_capture` is not used: its corpus is
the local model's own output.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import List, Optional

from llm_common import LLMError, _chat_json_with_retry

SESSION_DIR = Path(os.environ.get("LIRIEL_SESSION_DIR") or Path(__file__).resolve().parent / "data" / "claude_session")
POLL_SECONDS = 0.5
REPORT_EVERY_SECONDS = 600

_id_lock = threading.Lock()


def _next_id() -> int:
    with _id_lock:
        numbers = [int(m.group(1)) for p in SESSION_DIR.glob("req_*.json") if (m := re.fullmatch(r"req_(\d+)\.json", p.name))]
        return max(numbers, default=0) + 1


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _query_of(messages: List[dict]) -> Optional[str]:
    user = next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")
    found = re.search(r"^query: (\S+)", user, re.M)
    return found.group(1) if found else None


def _ask(messages: List[dict], temperature: float, effort: Optional[str], model: Optional[str], expects: str) -> str:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    number = _next_id()
    stem = f"{number:06d}"
    request = {
        "id": number, "expects": expects, "temperature": temperature, "effort": effort, "model": model,
        "query": _query_of(messages), "messages": messages,
    }
    _write_atomic(SESSION_DIR / f"req_{stem}.json", json.dumps(request, ensure_ascii=False))
    response, alive = SESSION_DIR / f"resp_{stem}.txt", SESSION_DIR / f"alive_{stem}"
    started = last_report = time.monotonic()
    try:
        while True:
            alive.touch()
            if response.exists():
                text = response.read_text(encoding="utf-8")
                if text.strip():
                    return text
                raise LLMError(f"claude_session: the answer to request {stem} is empty")
            now = time.monotonic()
            if now - last_report >= REPORT_EVERY_SECONDS:
                print(f"[claude_session] still waiting for the answer to request {stem} ({request['query']}), {int((now - started) // 60)} min")
                last_report = now
            time.sleep(POLL_SECONDS)
    finally:
        alive.unlink(missing_ok=True)


def chat(
    messages: List[dict],
    temperature: float = 0.5,
    effort: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Same contract as llm_client.chat(): the raw text of the answer."""
    return _ask(messages, temperature, effort, model, "text")


def chat_json(
    messages: List[dict],
    temperature: float = 0.3,
    effort: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Same contract as llm_client.chat_json(), including its retry-once-on-unparseable-JSON policy: an answer the CLI
    could not validate never reaches this point, so a retry here means a second request for the same prompt."""
    return _chat_json_with_retry(
        lambda m, temperature, effort, model: _ask(m, temperature, effort, model, "json"),
        messages, temperature, effort, model,
    )
