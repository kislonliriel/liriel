"""
The answering side of the `claude_session` backend (claude_session_client.py): read what a cycle asks, write the answer.
Run it as `python liriel.py ...` (the launcher at the repository root).

    python liriel.py setup [--install]
        Checks Python and the two packages the embodiment needs (pydantic, python-dotenv), plants Liriel's first row in a local store
        (data/store/, no database to configure) and says what to do next. Idempotent: run it again any time.
    python liriel.py say "message" [--sender NAME]
        One message of the person to Liriel: starts a cycle in the background and prints its first request (or, if the cycle needs
        nothing from you, her reply). Answer each request with `answer` until the REPLY block appears.
    python liriel.py answer < answer.json        (or:  python liriel.py answer <<'JSON' ... JSON)
        Gives the answer to the request on the screen -- a JSON object, or plain text where the request says so -- checks it, and prints
        the next request, or the REPLY block when the cycle is done. A JSON that does not parse keeps the same request on screen.
    python liriel.py reply
        Prints the reply of the last finished cycle again.
    python liriel.py next [--wait SECONDS] [--full]
        Promotes every answer file written since the last call (ans_N.json / ans_N.txt, validated: a JSON request must hold
        a JSON object, or the problem is printed and the request stays pending), then waits for the oldest request still
        pending and prints it. Blocks up to --wait seconds (default 540); prints "NO PENDING" when nothing came.
        A block of text longer than 500 characters that an earlier request already showed (in this cycle or an earlier one, up to
        120 requests ago) is replaced by a one-line pointer to it: the instructions of a query, a row that did not change. The
        prompt the code built is untouched; this only spares the reader repeating itself. The answer shape ("Respond with ONLY...")
        is never folded. --full prints the request whole, and a request is always printed whole the second time it is shown.
    python liriel.py show N      Request N whole (nothing folded), without changing what counts as shown.
    python liriel.py fresh       Forget what was shown (after a new session or a compacted context): the next requests are whole.
    python scripts/claude_session/session_cli.py system [SHA]
        Prints the system prompt (the MetaScheme) once.
    python scripts/claude_session/session_cli.py watch
        Prints one line when a cycle starts and nobody is serving it (for a Monitor); quiet while `next` is being used.
    python scripts/claude_session/session_cli.py status
        What is pending, what is being waited on.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SESSION_DIR = Path(os.environ.get("LIRIEL_SESSION_DIR") or ROOT / "data" / "claude_session")
SESSION_DIR.mkdir(parents=True, exist_ok=True)
STATE = SESSION_DIR / "cli_state.json"
HEARTBEAT = SESSION_DIR / "serving.txt"
ALIVE_FRESH_SECONDS = 6
MIN_DEDUPE_CHARS = 500
FOLD_WINDOW = 120               # a block identical to one shown up to this many requests ago (about nine cycles, across cycles) is pointed to; older is shown whole
SHAPE_PREFIXES = ("Respond with ONLY",)   # the answer shape of a query is never folded: it is the contract of the answer
PRINT_LIMIT_CHARS = 26000       # the shell shows ~30k characters of a command's output; longer requests go to part files
PART_CHARS = 24000
LINE_WRAP = 1800                # the viewer of a part file truncates very long lines; wrapping loses nothing


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"seen": {}, "printed": []}


def _save_state(state: dict) -> None:
    tmp = STATE.with_name(STATE.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE)


def _stem(n: int) -> str:
    return f"{n:06d}"


def _requests() -> list:
    out = []
    for p in sorted(SESSION_DIR.glob("req_*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _alive(n: int) -> bool:
    f = SESSION_DIR / f"alive_{_stem(n)}"
    return f.exists() and time.time() - f.stat().st_mtime < ALIVE_FRESH_SECONDS


def _pending() -> list:
    return [r for r in _requests() if not (SESSION_DIR / f"resp_{_stem(r['id'])}.txt").exists() and _alive(r["id"])]


# The cycle validates each answer against the model of its query (models.py) and loses the WHOLE cycle if one does not fit (it is only reported to the
# person as a technical problem). The same check, made here before the answer is sent, keeps the request pending with the pydantic message instead:
# a malformed answer costs one resend, not a cycle. A batch answer is only warned about: the cycle asks an item that does not validate alone.
_ITEM_MODELS = {"HUNTER_READING_BATCH": "HunterReadingResult", "ANCHOR_REVIEW_BATCH": "AnchorReviewResult", "IDENTITY_UPDATE_BATCH": "IdentityUpdateResult"}
_QUERY_MODELS = {
    "SAFETY_SCREEN": "SafetyScreenResult", "SCENE_SUBJECT_CHECK": "SceneSubjectCheckResult", "GRAPH_REQUEST": "GraphRequestResult",
    "TACTICAL_SCENE_INTERPRETATION": "TacticalSceneInterpretationResult", "MAINMEMORY_FILING": "MainMemoryFilingResult",
    "MOV_UPDATE": "MovUpdateResult", "RELATIONS_UPDATE": "RelationsUpdateResult", "BEST_PREY_GUESS": "BestPreyGuessResult",
    "ANCHOR_REVIEW": "AnchorReviewResult", "HUNTER_READING": "HunterReadingResult", "IDENTITY_UPDATE": "IdentityUpdateResult",
}


def _prevalidate(query: str, text: str) -> tuple:
    """(blocking problem or None, warnings) for a JSON answer to `query`. Reads the contract only (types, enumerations, required fields)."""
    import models  # noqa: PLC0415
    from llm_common import _extract_json  # noqa: PLC0415
    from pydantic import ValidationError  # noqa: PLC0415

    def problem(model_name: str, data) -> str:
        try:
            getattr(models, model_name).model_validate(data)
            return ""
        except ValidationError as exc:
            lines = [f"  {'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:6]]
            return f"{model_name} does not accept this answer ({exc.error_count()} error(s)):\n" + "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return ""

    data = _extract_json(text)
    if query in _QUERY_MODELS:
        msg = problem(_QUERY_MODELS[query], data)
        return (msg or None), []
    if query in _ITEM_MODELS and isinstance(data, dict):
        warns = []
        for i, entry in enumerate(data.get("results") or [], 1):
            if isinstance(entry, dict):
                filled = {"query": query[:-6], "cycle_id": data.get("cycle_id"), **entry}
                msg = problem(_ITEM_MODELS[query], filled)
                if msg:
                    warns.append(f"entry {i} ({entry.get('vov_id') or entry.get('vov_id_or_label') or entry.get('target_id')}): {msg}")
        return None, warns
    return None, []


def _promote_answers() -> list:
    """ans_N.json/.txt -> resp_N.txt for every pending request; returns the messages to show."""
    notes = []
    from llm_common import LLMError, _extract_json  # noqa: PLC0415

    for r in _requests():
        n, stem = r["id"], _stem(r["id"])
        resp = SESSION_DIR / f"resp_{stem}.txt"
        if resp.exists():
            continue
        for ext in ("json", "txt"):
            ans = SESSION_DIR / f"ans_{stem}.{ext}"
            if not ans.exists():
                continue
            text = ans.read_text(encoding="utf-8")
            if not text.strip():
                notes.append(f"[answer {stem}] {ans.name} is empty -- write the answer again")
                continue
            if r["expects"] == "json":
                try:
                    _extract_json(text)
                except LLMError as exc:
                    notes.append(f"[answer {stem}] NOT VALID JSON, request {stem} stays pending: {exc}")
                    continue
                if not os.environ.get("LIRIEL_NO_PREVALIDATE"):
                    try:
                        blocking, warns = _prevalidate(r.get("query") or "", text)
                    except Exception:  # noqa: BLE001 - a failing check must never stop a good answer
                        blocking, warns = None, []
                    for w in warns:
                        notes.append(f"[answer {stem}] WARNING, the cycle will ask this item again alone: {w}")
                    if blocking:
                        ans.rename(ans.with_name(ans.name + ".rejected"))
                        notes.append(f"[answer {stem}] REJECTED, request {stem} stays pending -- {blocking}\nFix it and send it again.")
                        continue
            tmp = resp.with_name(resp.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, resp)
            ans.rename(ans.with_name(ans.name + ".sent"))
            notes.append(f"[answer {stem}] sent ({len(text)} chars)")
            break
    return notes


def _system_of(r: dict) -> tuple:
    system = next((m["content"] for m in r["messages"] if m.get("role") == "system"), "")
    sha = hashlib.sha1(system.encode("utf-8")).hexdigest()[:10]
    path = SESSION_DIR / f"system_{sha}.md"
    if not path.exists():
        path.write_text(system, encoding="utf-8")
    return sha, len(system)


def _user_of(r: dict) -> str:
    return "\n\n".join(m["content"] for m in r["messages"] if m.get("role") != "system")


def _block_key(b: str) -> str:
    """Identity of a block, blind to the cycle id (the one thing that differs between two cycles' copies of the same instruction or row)."""
    import re
    return hashlib.sha1(re.sub(r"cycle_\d{8}_\d{4,6}", "cycle_X", b).encode("utf-8")).hexdigest()[:12]


def _render(r: dict, state: dict, full: bool, remember: bool = True) -> str:
    """The request as the code built it, except that a long block this reader was already shown (up to FOLD_WINDOW requests ago, in this
    cycle or an earlier one) is replaced by a one-line pointer: the same text, spared a second reading. Never folded: the answer shape
    (SHAPE_PREFIXES), a request shown a second time, or anything under `full`. `remember=False` (the `show` command) leaves the memory untouched."""
    n = r["id"]
    sha, system_chars = _system_of(r)
    user = _user_of(r)
    seen = state.setdefault("seen", {})
    reprint = n in state["printed"]
    blocks, shown = user.split("\n\n"), []
    for b in blocks:
        key = _block_key(b)
        before = seen.get(key)
        foldable = (not full and not reprint and len(b) >= MIN_DEDUPE_CHARS and not b.startswith(SHAPE_PREFIXES)
                    and before is not None and before.get("n") is not None and 0 < n - before["n"] <= FOLD_WINDOW)
        if foldable:
            head = b.strip().splitlines()[0][:90] if b.strip() else ""
            cyc = re.search(r"cycle_\d{8}_\d{4,6}", b)
            tail = f" -- THIS cycle_id: {cyc.group(0)}" if cyc else ""
            shown.append(f"[... {len(b)} characters, identical to the block shown in request {before['req']}: \"{head}\"{tail} ...]")
        else:
            shown.append(b)
            if remember and len(b) >= MIN_DEDUPE_CHARS:
                seen[key] = {"req": _stem(n), "n": n}
    if remember and n not in state["printed"]:
        state["printed"].append(n)
        state["printed"] = state["printed"][-400:]
    if len(seen) > 600:                                   # keep the memory bounded: the oldest go first
        for k in sorted(seen, key=lambda k: seen[k].get("n", 0))[:len(seen) - 600]:
            del seen[k]
    kind = "JSON object (ans_%s.json)" % _stem(n) if r["expects"] == "json" else "plain text (ans_%s.txt)" % _stem(n)
    body = "\n\n".join(shown)
    return (
        f"=== REQUEST {_stem(n)} | query: {r.get('query')} | temperature {r['temperature']} | answer: {kind}\n"
        f"system prompt: {sha} ({system_chars} chars; `system` prints it) | write the answer into {SESSION_DIR}\n"
        f"--- USER ---\n{body}\n=== end of request {_stem(n)} ==="
    )


def _emit(text: str, n: int) -> None:
    if len(text) <= PRINT_LIMIT_CHARS:
        print(text)
        return
    wrapped = "\n".join(textwrap.fill(line, LINE_WRAP, replace_whitespace=False, drop_whitespace=False, break_long_words=True) if len(line) > LINE_WRAP else line
                        for line in text.splitlines())
    parts = [wrapped[i:i + PART_CHARS] for i in range(0, len(wrapped), PART_CHARS)]
    paths = []
    for i, part in enumerate(parts, 1):
        p = SESSION_DIR / f"view_{_stem(n)}_{i}of{len(parts)}.md"
        p.write_text(part, encoding="utf-8")
        paths.append(str(p))
    first = text.splitlines()[0]
    print(first)
    print(f"LONG REQUEST ({len(text)} chars): read these {len(parts)} part files in order, then answer:")
    for p in paths:
        print("  " + p)
    for line in _summary(text):
        print(line)


def _summary(text: str) -> list:
    """What a long request is about, without reading it: who sent the message and what it says, the items of a batch, the target of a
    per-item query, and the Objects in the MOV (id and nature) -- so most of the reading can be spared. Read the parts for the rest."""
    import re
    out = ["--- at a glance (the parts hold the whole request) ---"]
    for pat in (r"^source: .+$", r"^sender .+$", r"^report: .{0,400}", r"^target: .+$"):
        m = re.search(pat, text, re.M)
        if m:
            out.append(m.group(0)[:420])
    items = re.findall(r"^ITEM \d+ of \d+ — .+$", text, re.M)
    out += items
    mov = re.search(r"^ARTIFACT: (?:Updated )?MOV \(.+?\)\n(\{.*?)\n\nARTIFACT", text, re.S | re.M)
    if mov:
        rows = re.findall(r'"vov_id": "([^"]+)",\s*"priority": (\w+).*?"object_nature": "([^"]+)"', mov.group(1), re.S)
        out.append("MOV rows: " + "; ".join(f"{v} [{n}{', P' + pr if pr != 'null' else ''}]" for v, pr, n in rows))
    return out


CYCLE = SESSION_DIR / "cycle.json"        # the cycle in flight: {text, sender, source, started, pid}
REPLY = SESSION_DIR / "reply.json"        # the last finished cycle: {reply, modality} or {error}
CYCLE_LOG = SESSION_DIR / "cycle.log"     # what the cycle process printed


def _store_env() -> None:
    """The embodiment's defaults, set BEFORE config is imported: a local store (no database to configure; LIRIEL_STORE=database keeps a
    configured one) and the Claude-session backend. Anything already in the environment wins."""
    if os.environ.get("LIRIEL_STORE", "").strip().lower() != "database":
        os.environ.setdefault("LIRIEL_LOCAL_STORE", str(ROOT / "data" / "store" / "local_mov_store.json"))
    os.environ.setdefault("LLM_PROFILE", "3")
    os.environ.setdefault("LIRIEL_SESSION_DIR", str(SESSION_DIR))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":  # never os.kill(pid, 0) on Windows: it terminates the process
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cycle_running() -> bool:
    cycle = _read_json(CYCLE)
    return bool(cycle) and _pid_alive(cycle.get("pid", 0))


def _print_reply(done: dict) -> None:
    if done.get("error"):
        print("=== CYCLE FAILED ===")
        print(done["error"])
        print("=== Nothing of this cycle was committed (MS §11.1). Tell the person plainly that it failed and what the error says; they can send the message again. ===")
        return
    print("=== REPLY — the cycle is finished and committed. Answer the person now, AS LIRIEL, with exactly this text: it is what she says ===")
    print(done.get("reply", ""))
    channel = "she was asked to speak it" if done.get("modality") == "voice" else "written"
    print(f"=== end of the reply ({channel}) ===")


def _wait_and_print(wait: int, full: bool = False) -> int:
    """Promotes any answer written, then waits for what comes next and prints it: the oldest request still pending, or the reply of
    the finished cycle, or the failure of a cycle that died. Prints NO PENDING after `wait` seconds."""
    deadline = time.monotonic() + wait
    state = _load_state()
    notes: list = []
    while True:
        HEARTBEAT.write_text(str(time.time()), encoding="utf-8")
        notes += _promote_answers()
        pend = sorted(_pending(), key=lambda r: r["id"])
        done = _read_json(REPLY)
        cycle = _read_json(CYCLE)
        if pend or done is not None or (cycle and not _pid_alive(cycle.get("pid", 0))) or time.monotonic() >= deadline:
            for note in notes:
                print(note)
        if pend:
            text = _render(pend[0], state, full)
            _save_state(state)
            _emit(text, pend[0]["id"])
            return 0
        if done is not None:
            _print_reply(done)
            return 0
        if cycle and not _pid_alive(cycle.get("pid", 0)):
            tail = CYCLE_LOG.read_text(encoding="utf-8", errors="replace")[-1500:] if CYCLE_LOG.exists() else ""
            _print_reply({"error": "the cycle process ended without a reply.\n" + tail})
            return 1
        if time.monotonic() >= deadline:
            print(f"NO PENDING (waited {wait}s)" + ("; a cycle is running, ask again" if _cycle_running() else "; no cycle is running"))
            return 0
        time.sleep(0.5)


def cmd_next(args) -> int:
    return _wait_and_print(args.wait, args.full)


def cmd_show(args) -> int:
    """Request N whole, as the code built it (nothing folded), without touching what is remembered as shown."""
    path = SESSION_DIR / f"req_{_stem(args.n)}.json"
    r = _read_json(path)
    if r is None:
        print(f"No request {_stem(args.n)} on disk.")
        return 2
    _emit(_render(r, _load_state(), True, remember=False), args.n)
    return 0


def cmd_fresh(_args) -> int:
    """Forget what was shown: the next request is printed whole. Run it when the reader's page was emptied (a new session, a compacted context)."""
    _save_state({"seen": {}, "printed": []})
    print("Forgotten: the next requests are shown whole.")
    return 0


def cmd_say(args) -> int:
    _store_env()
    if _cycle_running():
        print("A cycle is already running: finish it first (`python liriel.py next` shows what it waits for).")
        return 2
    text = sys.stdin.read() if args.text == "-" else args.text
    if not text.strip():
        print("Nothing to say: give the message as the argument (or - to read it from stdin).")
        return 2
    REPLY.unlink(missing_ok=True)
    CYCLE.write_text(json.dumps({"text": text, "sender": args.sender, "source": args.source, "started": time.time(), "pid": 0}, ensure_ascii=False), encoding="utf-8")
    log = CYCLE_LOG.open("w", encoding="utf-8")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "cwd": str(ROOT), "env": {**os.environ, "PYTHONIOENCODING": "utf-8"}}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_run-cycle"], **kwargs)
    meta = _read_json(CYCLE) or {}
    meta["pid"] = proc.pid
    CYCLE.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return _wait_and_print(args.wait)


def cmd_answer(args) -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    text = sys.stdin.read().strip()
    pend = sorted(_pending(), key=lambda r: r["id"])
    if not pend:
        print("No request is waiting for an answer.")
        return _wait_and_print(args.wait)
    if not text:
        print("The answer is empty: nothing was sent. Give it on stdin.")
        return _wait_and_print(args.wait, full=True)
    request = pend[0]
    ext = "json" if request["expects"] == "json" else "txt"
    (SESSION_DIR / f"ans_{_stem(request['id'])}.{ext}").write_text(text + "\n", encoding="utf-8")
    return _wait_and_print(args.wait)


def cmd_reply(_args) -> int:
    done = _read_json(REPLY)
    if done is None:
        print("No finished cycle yet." + (" One is running." if _cycle_running() else ""))
        return 1
    _print_reply(done)
    return 0


def cmd_run_cycle(_args) -> int:
    """The cycle itself, in a background process of its own (started by `say`): the real ProcessMotivation cycle, whose model calls are
    the requests the person at the other end of `answer` fills in."""
    _store_env()
    meta = _read_json(CYCLE) or {}
    db = None
    try:
        from config import settings
        from database import get_database
        import liriel_seed
        import motivation

        db = get_database()
        liriel_seed.seed_self_row(db)
        mov = db.load_mov(settings.default_mov_id)
        reply, _mov, modality = motivation.run_motivation_cycle(
            db, mov, meta.get("text", ""), source=meta.get("source") or "terminal_chat", sender=meta.get("sender"),
        )
        out = {"reply": reply, "modality": modality, "finished": time.time()}
    except BaseException as exc:  # noqa: BLE001 - whatever stopped the cycle is the reply of this cycle
        import traceback
        out = {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-1500:], "finished": time.time()}
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    tmp = REPLY.with_name(REPLY.name + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, REPLY)
    CYCLE.unlink(missing_ok=True)
    return 0 if "reply" in out else 1


def cmd_setup(args) -> int:
    """Everything a new machine needs before the first message, and nothing it does not: no database, no key, no server."""
    problems = []
    if sys.version_info < (3, 10):
        problems.append(f"Python 3.10 or newer is needed (this is {sys.version.split()[0]}).")
    missing = []
    for module, package in (("pydantic", "pydantic>=2.7.0"), ("dotenv", "python-dotenv>=1.0.1")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        if args.install:
            subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
        else:
            problems.append(f"Missing packages: run  {sys.executable} -m pip install {' '.join(missing)}   (or: python liriel.py setup --install)")
    if problems:
        for p in problems:
            print("[setup] " + p)
        return 1
    _store_env()
    if os.environ.get("LIRIEL_LOCAL_STORE"):  # the folder of the store actually used, not always data/store
        Path(os.environ["LIRIEL_LOCAL_STORE"]).parent.mkdir(parents=True, exist_ok=True)
    from config import settings
    from database import get_database
    import liriel_seed
    import motivation  # noqa: F401 - imports everything a cycle needs: a broken install fails here, not in the middle of a message

    db = get_database()
    try:
        planted = liriel_seed.seed_self_row(db)
    finally:
        db.close()
    print(f"[setup] Python {sys.version.split()[0]}, packages present, backend {settings.llm_backend}, store {os.environ.get('LIRIEL_LOCAL_STORE', '(configured database)')}")
    print("[setup] Liriel's first row " + ("planted." if planted else "was already there."))
    print('[setup] Ready. First message:  python liriel.py say "Oi, Liriel!" --sender "<the person\'s name>"')
    return 0


def cmd_system(args) -> int:
    path = SESSION_DIR / f"system_{args.sha}.md" if args.sha else max(SESSION_DIR.glob("system_*.md"), key=lambda p: p.stat().st_mtime, default=None)
    if path is None or not path.exists():
        print("no system prompt seen yet")
        return 1
    text = path.read_text(encoding="utf-8")
    if len(text) <= PRINT_LIMIT_CHARS:
        print(f"# {path.name}")
        print(text)
        return 0
    wrapped = "\n".join(textwrap.fill(line, LINE_WRAP, replace_whitespace=False, drop_whitespace=False, break_long_words=True) if len(line) > LINE_WRAP else line
                        for line in text.splitlines())
    parts = [wrapped[i:i + PART_CHARS] for i in range(0, len(wrapped), PART_CHARS)]
    print(f"# {path.name}: {len(text)} chars, read these {len(parts)} part files in order")
    for i, part in enumerate(parts, 1):
        p = SESSION_DIR / f"{path.stem}_{i}of{len(parts)}.md"
        p.write_text(part, encoding="utf-8")
        print("  " + str(p))
    return 0


def cmd_watch(_args) -> int:
    announced: dict = {}
    print("watching for cycles that need an answer", flush=True)
    while True:
        try:
            beat = float(HEARTBEAT.read_text(encoding="utf-8")) if HEARTBEAT.exists() else 0.0
        except ValueError:
            beat = 0.0
        pend = sorted(_pending(), key=lambda r: r["id"])
        if pend and time.time() - beat > 90:
            r = pend[0]
            if time.time() - announced.get(r["id"], 0) > 300:
                announced[r["id"]] = time.time()
                print(f"NEW CYCLE WAITING: request {_stem(r['id'])} ({r.get('query')}) -- run `session_cli.py next` and answer until no request is pending", flush=True)
        time.sleep(3)


def cmd_status(_args) -> int:
    pend = _pending()
    print(f"pending: {[ _stem(r['id']) + ':' + str(r.get('query')) for r in pend ]}")
    allr = _requests()
    print(f"requests so far: {len(allr)}; last: {_stem(allr[-1]['id']) if allr else '-'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("next")
    p.add_argument("--wait", type=int, default=540)
    p.add_argument("--full", action="store_true")
    p.set_defaults(fn=cmd_next)
    p = sub.add_parser("say")
    p.add_argument("text", help="the person's message (or - to read it from stdin)")
    p.add_argument("--sender", default=os.environ.get("LIRIEL_SENDER") or None, help="who is writing, when the channel knows (a fact, not a guess)")
    p.add_argument("--source", default="terminal_chat")
    p.add_argument("--wait", type=int, default=240)
    p.set_defaults(fn=cmd_say)
    p = sub.add_parser("answer")
    p.add_argument("--wait", type=int, default=540)
    p.set_defaults(fn=cmd_answer)
    p = sub.add_parser("show")
    p.add_argument("n", type=int)
    p.set_defaults(fn=cmd_show)
    sub.add_parser("fresh").set_defaults(fn=cmd_fresh)
    sub.add_parser("reply").set_defaults(fn=cmd_reply)
    sub.add_parser("_run-cycle").set_defaults(fn=cmd_run_cycle)
    p = sub.add_parser("setup")
    p.add_argument("--install", action="store_true", help="pip install what is missing")
    p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("system")
    p.add_argument("sha", nargs="?")
    p.set_defaults(fn=cmd_system)
    sub.add_parser("watch").set_defaults(fn=cmd_watch)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
