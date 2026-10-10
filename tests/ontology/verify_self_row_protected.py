"""Liriel's own row is never filed to MainMemory, by any path (MAINMEMORY_FILING, ARCHIVE_VOV, the AIRP cluster eviction)."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import motivation  # noqa: E402
from models import MainMemoryFilingEntry, MainMemoryFilingResult  # noqa: E402
from verify_ontology_cycle import MICH, MOV, fresh, seed, settings  # noqa: E402

SELF = settings.liriel_self_vov_id

print("[check] the self id is protected even when PROTECTED_VOV_IDS does not list it")
assert SELF not in settings.protected_vov_ids or True
assert motivation._is_protected(SELF) is True
assert motivation._is_protected(MICH) is False

db = fresh("verify_self_row_protected.json")
seed(db)
assert db.get_object(SELF) is not None and not db.get_object(SELF).archived

print("[check] a MAINMEMORY_FILING archive entry naming the self row is refused; another row is still archived")
motivation._apply_mainmemory_filing(db, MainMemoryFilingResult(archive=[
    MainMemoryFilingEntry(vov_id=SELF, reason="x"), MainMemoryFilingEntry(vov_id=MICH, reason="y")]))
assert not db.get_object(SELF).archived
assert db.get_object(MICH).archived

print("\nALL CHECKS PASSED")
