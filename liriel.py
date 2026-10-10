#!/usr/bin/env python
"""Liriel, embodied by a Claude session. The whole command line is here:

    python liriel.py setup               once, on a new machine (checks Python and two packages, plants Liriel's first row in a local store)
    python liriel.py say "message"       one message to Liriel; prints the first thing the cycle asks of you
    python liriel.py answer <<'JSON' ... JSON    your answer; prints the next request, and at the end her reply
    python liriel.py reply | next | status | system | watch

See CLAUDE.md for what a Claude Code session does with it, and README.md ("Embody Liriel with Claude Code")."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts" / "claude_session"))
import session_cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(session_cli.main())
