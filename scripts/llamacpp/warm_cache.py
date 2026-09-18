"""
Warms a running llama-server's in-memory prefix cache with the
MetaScheme, so the first real Liriel call doesn't pay to process all
~18k tokens of it.

This replaces the original "build_cache.sh" step: llama-server has no
on-disk prompt-cache file the way llama-cli does (confirmed against the
actual binary and against llama.cpp's own docs -- see start_server.sh's
header comment), so there's nothing to *build* ahead of time. Warming
means sending one request that contains only the MetaScheme block with
n_predict=0, right after the server reports healthy, which is exactly
what start_server.sh already does automatically. Run this by hand only
if you started the server yourself and skipped that step, or want to
re-warm after the cache was evicted by an unrelated request.

Usage:
    python warm_cache.py [path/to/MetaScheme.md] [--port 8080]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from client import LlamaCppClient

# Same fix as main.py/telegram_bot.py -- see client.py's __main__ block.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "metascheme_path",
        nargs="?",
        default=str(Path(__file__).resolve().parent.parent.parent / "docs" / "MetaScheme_Liriel_Rev0000.md"),
        help="Path to the MetaScheme markdown file (default: docs/MetaScheme_Liriel_Rev0000.md)",
    )
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    path = Path(args.metascheme_path)
    if not path.exists():
        raise SystemExit(f"MetaScheme file not found: {path}")

    text = path.read_text(encoding="utf-8")
    print(f"Warming cache with {path} ({len(text)} chars)...")

    client = LlamaCppClient(base_url=f"http://localhost:{args.port}")
    result = client.warm_up(text)

    prompt_tokens = result.get("usage", {}).get("prompt_tokens")
    print(f"prompt_tokens={prompt_tokens}")
    if prompt_tokens:
        print("Warm-up complete. The next call sharing this prefix should reuse it.")


if __name__ == "__main__":
    main()
