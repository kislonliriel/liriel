"""Run every synthetic check (no network, no real model) and report PASS/FAIL.

  python tests/run_all.py

Runs tests/ontology/verify_*.py, tests/verify_reply_limit.py and tests/finetune/verify_capture.py
from the repository root, skipping verify_ontology_pg.py (it needs a Postgres database).
Exits non-zero if any check fails.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
SKIP = {"verify_ontology_pg.py"}
EXTRA = [TESTS / "verify_reply_limit.py", TESTS / "finetune" / "verify_capture.py"]


def main() -> int:
    checks = sorted((TESTS / "ontology").glob("verify_*.py")) + EXTRA
    checks = [c for c in checks if c.name not in SKIP]
    failed = []
    for check in checks:
        name = check.relative_to(ROOT).as_posix()
        if not check.is_file():
            print("FAIL  %s (file not found)" % name)
            failed.append(name)
            continue
        proc = subprocess.run([sys.executable, str(check)], cwd=str(ROOT),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, errors="replace")
        if proc.returncode == 0:
            print("PASS  %s" % name)
            continue
        print("FAIL  %s (exit %d)" % (name, proc.returncode))
        failed.append(name)
        for stream in (proc.stdout, proc.stderr):
            if stream.strip():
                print("\n".join("    " + line for line in stream.rstrip().splitlines()[-20:]))
    print("\n%d passed, %d failed, %d total" % (len(checks) - len(failed), len(failed), len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
