"""
Shared logic between TestsNotebookApp's two front ends — the terminal runner
(test_notebook_app.py) and the web UI (test_notebook_web.py). Both drive the
exact same run_motivation_cycle against the exact same live MOV MindReader
reads; this module only holds what's common to both: loading
test_notebook_cases.json, the protected-ids reset, and opening MindReader.

Liriel is never told this is a test: SOURCE below is the same embodiment
label a real Telegram text message already uses in production
(telegram_bot.py) — nothing about the ScenarioData she sees marks a message
sent through either front end as different from an ordinary conversation.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Callable

import requests

from config import settings
from database import PostgresDatabase

CASES_PATH = Path(__file__).parent / "test_notebook_cases.json"
MINDREADER_APP = Path(__file__).parent / "mindreader" / "app.py"
MINDREADER_URL = "http://localhost:5050"
START_SERVER_SCRIPT = Path(__file__).parent / "scripts" / "llamacpp" / "start_server.sh"
SOURCE = "telegram"


def load_cases(name_filter: str | None = None) -> list[dict]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    if name_filter:
        needle = name_filter.strip().lower()
        cases = [c for c in cases if needle in c["name"].lower()]
    return cases


def mindreader_is_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", 5050)) == 0


def open_mindreader() -> str:
    """Opens MindReader, starting its server first if it isn't up yet.
    Returns a one-line status message for the caller to surface to the user."""
    if mindreader_is_running():
        webbrowser.open(MINDREADER_URL)
        return f"MindReader já estava rodando em {MINDREADER_URL} — aba aberta."
    # New process group on Windows so a later Ctrl+C aimed at the caller's
    # own input loop doesn't also tear down MindReader's server.
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    subprocess.Popen(
        [sys.executable, str(MINDREADER_APP)],
        cwd=str(MINDREADER_APP.parent.parent),
        creationflags=creationflags,
    )
    # mindreader/app.py opens its own browser tab once its server is listening.
    return f"Iniciando MindReader em {MINDREADER_URL}..."


def _find_git_bash() -> str | None:
    """start_server.sh needs a real Git-for-Windows bash — plain
    shutil.which("bash") is unreliable here because Windows itself ships a
    bash.exe stub under System32 that launches WSL instead (confirmed for
    real: "WSL (9 - Relay) ERROR: execvpe(/bin/bash) failed" on a machine
    with no WSL distro set up). Known Git install locations are checked
    first; a PATH match is used only as a fallback, and only if it isn't
    that System32 stub."""
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if Path(candidate).exists():
            return candidate
    found = shutil.which("bash") or shutil.which("bash.exe")
    if found and "system32" not in found.lower():
        return found
    return None


def llamacpp_is_healthy() -> bool:
    try:
        return requests.get(f"{settings.llamacpp_base_url}/health", timeout=1.5).ok
    except requests.RequestException:
        return False


def ensure_llm_backend_ready(
    on_progress: Callable[[str], None] = print,
    timeout_s: float = 180.0,
    poll_interval: float = 2.0,
) -> str:
    """When config.py's LLM_BACKEND is "llamacpp" (the default) and nothing
    answers at settings.llamacpp_base_url yet, starts
    scripts/llamacpp/start_server.sh in the background — it already waits
    for its own /health and warms the MetaScheme prefix cache itself, so
    this only needs to poll /health and return once that's true (or once
    timeout_s elapses, in which case it just warns and lets the caller
    proceed — the first real send will surface the same connection error
    as before if the server genuinely isn't up yet).

    A non-llamacpp backend (e.g. "litellm", routing to Groq/Anthropic/etc.) is a no-op — there's no local
    server to start. `on_progress` is called with each status line, so
    both front ends can surface this the way they already surface
    everything else (print for the CLI, the log panel for the web UI)."""
    if settings.llm_backend != "llamacpp":
        return f"Backend LLM: {settings.llm_backend} (nenhum servidor local para iniciar)."

    if llamacpp_is_healthy():
        return f"llama-server já está pronto em {settings.llamacpp_base_url}."

    bash = _find_git_bash()
    if not bash or not START_SERVER_SCRIPT.exists():
        return (f"[aviso] llama-server não responde em {settings.llamacpp_base_url} e não foi "
                f"possível iniciá-lo automaticamente (bash ou {START_SERVER_SCRIPT.name} não "
                f"encontrado) — rode `bash {START_SERVER_SCRIPT}` manualmente num terminal.")

    on_progress(f"Iniciando llama-server via {START_SERVER_SCRIPT.name}...")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    subprocess.Popen(
        [bash, str(START_SERVER_SCRIPT)],
        cwd=str(START_SERVER_SCRIPT.parent),
        creationflags=creationflags,
    )

    on_progress(f"Aguardando llama-server ficar pronto em {settings.llamacpp_base_url} "
                f"(pode levar até alguns minutos para carregar o modelo na GPU)...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if llamacpp_is_healthy():
            return "llama-server pronto (cache do MetaScheme sendo aquecido em segundo plano)."
        time.sleep(poll_interval)
    return (f"[aviso] llama-server ainda não respondeu após {int(timeout_s)}s — pode estar "
            f"carregando o modelo ainda; se o primeiro envio falhar, aguarde e tente de novo.")


def reset_mov(db) -> str:
    """Deletes every mov_objects row except settings.protected_vov_ids.
    Returns a one-line status message. Callers are responsible for getting
    the user's confirmation before calling this — it's a hard, irreversible
    delete, not an archive."""
    if not isinstance(db, PostgresDatabase):
        return "Reset só funciona com um banco Postgres — este não é um."
    protected = sorted(settings.protected_vov_ids)
    if not protected:
        return "settings.protected_vov_ids está vazio — reset recusado (apagaria tudo)."
    with db._conn.cursor() as cur:
        cur.execute("DELETE FROM mov_objects WHERE vov_id NOT IN %s", (tuple(protected),))
        deleted = cur.rowcount
    return f"Reset concluído — {deleted} linha(s) removida(s). Mantidos: {', '.join(protected)}."
