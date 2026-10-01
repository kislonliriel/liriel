"""
TestsNotebookApp — web UI. A more visual front end for the same step-by-step,
human-in-the-loop test runner as test_notebook_app.py (the terminal version)
— both drive the exact same pipeline (test_notebook_core.py ->
motivation.run_motivation_cycle) against the exact same live MOV, and both
keep Liriel unaware this is a test (source="telegram", same as the CLI).

Pick a case, watch each step's text before sending it (editable right in the
page), and see Liriel's reply — or any error — appear in a running log, all
through buttons instead of typed terminal commands.

Run:
    python test_notebook_web.py
Then http://localhost:5057 opens automatically, and so does MindReader
(same auto-open behavior as the CLI version) — watch it update between
steps. The local llama-server (scripts/llamacpp/start_server.sh) is also
started automatically in the background if it isn't already running —
progress shows up both in this terminal and in the page's own log panel.
"""
from __future__ import annotations

import sys
import threading
import webbrowser

# Windows console codepage fix, same as main.py/telegram_bot.py.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from flask import Flask, jsonify, request, send_from_directory

from config import settings
from database import get_database
from motivation import run_motivation_cycle
from test_notebook_core import SOURCE, ensure_llm_backend_ready, load_cases, open_mindreader, reset_mov

app = Flask(__name__, static_folder="test_notebook_web_static", static_url_path="")

# A single local user drives this at a time — plain module-level state
# guarded by one lock, same spirit as the CLI version's local variables.
_state = {
    "db": None,
    "mov": None,
    "steps": [],   # flat list of {case_name, label, speaker, text}
    "index": 0,
    "running": False,
    "log": [],     # [{type, text}], oldest first
}
_lock = threading.Lock()


def _log(kind: str, text: str) -> None:
    _state["log"].append({"type": kind, "text": text})


def _public_state() -> dict:
    idx = _state["index"]
    steps = _state["steps"]
    current = steps[idx] if _state["running"] and idx < len(steps) else None
    return {
        "running": _state["running"],
        "finished": _state["running"] and idx >= len(steps) and len(steps) > 0,
        "total": len(steps),
        "index": idx,
        "current": current,
        "mov_id": settings.default_mov_id,
        "log": _state["log"][-300:],
    }


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/cases")
def api_cases():
    return jsonify(load_cases())


@app.get("/api/state")
def api_state():
    return jsonify(_public_state())


@app.post("/api/start")
def api_start():
    with _lock:
        body = request.get_json(silent=True) or {}
        name_filter = (body.get("case") or "").strip() or None
        cases = load_cases(name_filter)
        if not cases:
            return jsonify({"error": f"nenhum caso encontrado para {name_filter!r}"}), 400

        steps = [
            {
                "case_name": case["name"],
                "label": step["label"],
                "speaker": step.get("speaker", "Fabio"),
                "text": step["text"],
            }
            for case in cases
            for step in case["steps"]
        ]

        if _state["db"] is None:
            _state["db"] = get_database()
        # Always (re)load fresh — not just on first connect: a Resetar MOV
        # click before Iniciar already set _state["db"], so gating this load
        # on "db is None" left _state["mov"] stuck at None and crashed the
        # first /api/send with "'NoneType' object has no attribute 'mov_id'".
        _state["mov"] = _state["db"].load_mov(settings.default_mov_id)
        _log("info", f"Conectado à MOV '{settings.default_mov_id}'.")

        _state["steps"] = steps
        _state["index"] = 0
        _state["running"] = True
        _log("info", f"Iniciado — {len(steps)} passo(s) em {len(cases)} caso(s).")
        return jsonify(_public_state())


@app.post("/api/send")
def api_send():
    with _lock:
        if not _state["running"] or _state["index"] >= len(_state["steps"]):
            return jsonify({"error": "nenhum teste em andamento"}), 400
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        if not text:
            return jsonify({"error": "texto vazio"}), 400

        step = _state["steps"][_state["index"]]
        _log("sent", f"[{step['case_name']} · passo {step['label']}] {step['speaker']}: {text}")
        try:
            reply, mov, _modality = run_motivation_cycle(_state["db"], _state["mov"], text, source=SOURCE)
        except Exception as exc:  # noqa: BLE001
            print(f"[ProcessMotivation cycle error] {exc}")
            _log("error", f"Erro no ciclo: {exc}")
            return jsonify({"ok": False, "error": str(exc), "state": _public_state()})

        _state["mov"] = mov
        _log("reply", reply)
        _state["index"] += 1
        return jsonify({"ok": True, "reply": reply, "state": _public_state()})


@app.post("/api/skip")
def api_skip():
    with _lock:
        if not _state["running"] or _state["index"] >= len(_state["steps"]):
            return jsonify({"error": "nenhum teste em andamento"}), 400
        step = _state["steps"][_state["index"]]
        _log("skip", f"[{step['case_name']} · passo {step['label']}] pulado.")
        _state["index"] += 1
        return jsonify(_public_state())


@app.post("/api/stop")
def api_stop():
    with _lock:
        _state["running"] = False
        _state["steps"] = []
        _state["index"] = 0
        _log("info", "Teste interrompido.")
        return jsonify(_public_state())


@app.post("/api/reset")
def api_reset():
    with _lock:
        body = request.get_json(silent=True) or {}
        if not body.get("confirm"):
            return jsonify({"error": "confirmação obrigatória"}), 400
        if _state["db"] is None:
            _state["db"] = get_database()
        msg = reset_mov(_state["db"])
        if _state["mov"] is not None:
            _state["mov"] = _state["db"].load_mov(settings.default_mov_id)
        _log("info", msg)
        return jsonify({"message": msg, "state": _public_state()})


@app.post("/api/open_mindreader")
def api_open_mindreader():
    msg = open_mindreader()
    _log("info", msg)
    return jsonify({"message": msg})


def _startup_progress(msg: str) -> None:
    print(f"[llm] {msg}")
    _log("info", msg)


if __name__ == "__main__":
    url = "http://localhost:5057"
    print("=" * 60)
    print("TestsNotebookApp — interface web")
    print(f"MOV: {settings.default_mov_id}")
    print(f"Abrindo {url} ...")
    print("=" * 60)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    threading.Timer(1.2, open_mindreader).start()
    # Runs in the background so Flask still binds its port immediately —
    # loading a local model onto the GPU can take a couple of minutes, and
    # there's no reason the page itself should wait on that.
    threading.Thread(
        target=lambda: _log("info", ensure_llm_backend_ready(on_progress=_startup_progress)),
        daemon=True,
    ).start()
    app.run(host="127.0.0.1", port=5057, debug=False)
