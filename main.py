"""
Liriel — Phase 1 MVP: interactive terminal chat.

Usage:
    python main.py            # start the chat loop
    python main.py --check    # test the LLM and database connections and exit
"""
from __future__ import annotations

import sys

# Windows' default console codepage (cp1252/cp437) can't encode every
# character a model or the MetaScheme itself may print (e.g. "→", seen for
# real crashing a verbose debug print of a JSON reply) — reconfigure stdout/
# stderr to UTF-8 unconditionally, replacing anything a given terminal font
# still can't render rather than crashing the whole cycle over one glyph.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from config import settings
from database import get_database
from llm_client import chat
from motivation import run_motivation_cycle

EXIT_WORDS = {"exit", "quit", "bye", "sair"}


def selftest() -> bool:
    ok = True

    print(f"-> Testing LLM (model={settings.llm_model}, api_base={settings.llm_api_base})...")
    try:
        reply = chat(
            [{"role": "user", "content": "Reply with exactly one word: OK"}],
            temperature=0.0,
        )
        print(f"   OK — model response: {reply.strip()!r}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"   FAILED: {exc}")

    print("-> Testing database...")
    try:
        db = get_database()
        mov = db.load_mov(settings.default_mov_id)
        db.close()
        print(f"   OK — MOV '{mov.mov_id}' loaded with {len(mov.objects)} object(s).")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"   FAILED: {exc}")

    return ok


def chat_loop() -> None:
    db = get_database()
    mov = db.load_mov(settings.default_mov_id)

    print("=" * 60)
    print("Liriel — Phase 1 MVP (type 'exit' to quit)")
    print(f"Model: {settings.llm_model}")
    print("=" * 60)

    try:
        while True:
            try:
                user_text = input("\nYou: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_text:
                continue
            if user_text.lower() in EXIT_WORDS:
                break

            try:
                response_text, mov, _output_modality = run_motivation_cycle(db, mov, user_text)
            except Exception as exc:  # noqa: BLE001
                print(f"[ProcessMotivation cycle error] {exc}")
                continue

            print(f"\nLiriel: {response_text}")
    finally:
        db.close()
        print("\nGoodbye.")


def main() -> None:
    if "--check" in sys.argv:
        sys.exit(0 if selftest() else 1)
    chat_loop()


if __name__ == "__main__":
    main()
