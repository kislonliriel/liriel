"""
TestsNotebookApp — a step-by-step, human-in-the-loop test runner for Liriel
(terminal front end; see test_notebook_web.py for the buttons-and-log web UI,
which drives the exact same test_notebook_core.py).

Drives the exact same ProcessMotivation cycle (motivation.run_motivation_cycle)
against the exact same live MOV/MainMemory database MindReader already reads
(config.py's DEFAULT_MOV_ID) — this is not a mock or a sandboxed copy, it's a
real cycle, so what MindReader shows between steps is exactly what a real
interaction would have produced.

Test cases and their steps live in test_notebook_cases.json, not in this file
— add a case, add a step, or edit an existing message's text there; no code
change needed here.

Liriel is never told this is a test: every message goes out with
source="telegram", the same embodiment label a real Telegram text from Fábio
already uses in production (telegram_bot.py) — nothing about the ScenarioData
Liriel sees distinguishes this from an ordinary conversation.

Usage:
    python test_notebook_app.py                # run every case, in order
    python test_notebook_app.py --case "Lya"    # run only case(s) whose name
                                                 # contains this text
    python test_notebook_app.py --reset         # wipe MOV+MainMemory down to
                                                 # just the protected core-
                                                 # identity rows first (asks
                                                 # for confirmation)

Between every step the app pauses and waits for you — this pause is what
gives you time to inspect MindReader before anything else is sent:
    [Enter]   send this step's message as written, then pause before the next
    e         edit this step's text on the fly, then send the edited version
    k         skip this step (don't send it), move on to the next one
    q         stop the whole test run right here

MindReader itself opens automatically as soon as the test starts, and so
does the local llama-server (scripts/llamacpp/start_server.sh) if it isn't
already running — no need to start either one by hand first.
"""
from __future__ import annotations

import argparse
import sys

# Windows' default console codepage (cp1252/cp437) can't encode every
# character a model or the MetaScheme itself may print — reconfigure stdout/
# stderr to UTF-8 unconditionally, matching main.py/telegram_bot.py.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from config import settings
from database import get_database
from motivation import run_motivation_cycle
from test_notebook_core import SOURCE, ensure_llm_backend_ready, load_cases, open_mindreader, reset_mov


def _confirm_reset(db) -> None:
    protected = sorted(settings.protected_vov_ids)
    print(f"[reset] isto vai APAGAR toda linha de mov_objects exceto: {protected}")
    confirm = input("Digite 'reset' para confirmar: ").strip()
    if confirm != "reset":
        print("[reset] cancelado.")
        return
    print(f"[reset] {reset_mov(db)}")


def _prompt_step(case_name: str, label: str, speaker: str, text: str) -> tuple[str, str | None]:
    """Returns (action, text_to_send). action is one of 'send', 'skip', 'quit'."""
    while True:
        print(f"\n=== {case_name} — passo {label} ===")
        print("-" * 60)
        print(f"{speaker}: {text}")
        print("-" * 60)
        choice = input("[Enter] enviar  [e] editar  [k] pular  [q] interromper > ").strip().lower()
        if choice == "":
            return "send", text
        if choice == "e":
            new_text = input("Novo texto: ").strip()
            if not new_text:
                print("[aviso] texto vazio, tente de novo.")
                continue
            return "send", new_text
        if choice == "k":
            return "skip", None
        if choice == "q":
            return "quit", None
        print("[aviso] opção inválida.")


def run(name_filter: str | None) -> None:
    cases = load_cases(name_filter)
    if not cases:
        print(f"[error] nenhum caso encontrado (filtro={name_filter!r}).")
        return

    print(f"[llm] {ensure_llm_backend_ready()}")
    print(f"[mindreader] {open_mindreader()}")

    db = get_database()
    mov = db.load_mov(settings.default_mov_id)

    print("=" * 60)
    print("TestsNotebookApp — passo a passo (Ctrl+C ou 'q' para interromper)")
    print(f"MOV: {settings.default_mov_id}  |  casos: {len(cases)}")
    print("=" * 60)

    try:
        for case in cases:
            for step in case["steps"]:
                current_text = step["text"]
                while True:
                    action, text = _prompt_step(
                        case["name"], step["label"], step.get("speaker", "Fabio"), current_text
                    )
                    if action == "quit":
                        print("\n[interrompido pelo usuário]")
                        return
                    if action == "skip":
                        print(f"[passo {step['label']} pulado]")
                        break

                    current_text = text  # keep an edit around if this attempt fails and gets retried

                    try:
                        response_text, mov, _output_modality = run_motivation_cycle(
                            db, mov, text, source=SOURCE
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"[ProcessMotivation cycle error] {exc}")
                        print("(o passo não avançou — escolha de novo: reenviar, editar, pular ou interromper)")
                        continue

                    print(f"\nLiriel: {response_text}")
                    break
    except KeyboardInterrupt:
        print("\n[interrompido pelo usuário]")
    finally:
        db.close()
        print("\nFim.")


def main() -> None:
    parser = argparse.ArgumentParser(description="TestsNotebookApp — step-by-step test runner for Liriel.")
    parser.add_argument("--case", default=None, help="Só roda casos cujo nome contenha este texto (case-insensitive).")
    parser.add_argument("--reset", action="store_true", help="Reseta a MOV/MainMemory antes de começar (pede confirmação).")
    args = parser.parse_args()

    if args.reset:
        db = get_database()
        try:
            _confirm_reset(db)
        finally:
            db.close()

    run(args.case)


if __name__ == "__main__":
    main()
