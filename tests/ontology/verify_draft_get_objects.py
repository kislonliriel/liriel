"""DraftDatabase.get_objects: the same answer as one get_object() per id, but the untouched ids reach the real database in ONE batch call."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import DraftDatabase  # noqa: E402
from models import VectorObjectValence  # noqa: E402
from verify_ontology_cycle import MICH, MOV, fresh, seed, settings  # noqa: E402

SELF = settings.liriel_self_vov_id
real = fresh("verify_draft_get_objects.json")
seed(real)
draft = DraftDatabase(real)
ids = [SELF, MICH, "Nobody_Here", SELF]  # a missing id and a duplicate on purpose

print("[check] untouched ids: same rows as the one-by-one loop, in the caller's order, missing ids left out, duplicates collapsed")
loop = {i: draft.get_object(i) for i in dict.fromkeys(ids) if draft.get_object(i) is not None}
got = draft.get_objects(ids)
assert list(got) == list(loop) == [SELF, MICH], list(got)
assert all(got[i].model_dump() == loop[i].model_dump() for i in got)

print("[check] the real database's get_objects is asked ONCE, for the untouched ids only (this JSON backend's own batch is the default loop; Postgres runs one query)")
with patch.object(real, "get_objects", wraps=real.get_objects) as batch:
    draft.get_objects(ids)
    assert batch.call_count == 1 and set(batch.call_args.args[0]) == {SELF, MICH, "Nobody_Here"}

print("[check] a row this draft touched comes from the draft, not from the real database")
patched = real.get_object(MICH).model_copy(update={"brief_description": "CHANGED IN THE DRAFT"})
draft.replace_object(MOV, patched)
got = draft.get_objects([SELF, MICH])
assert got[MICH].brief_description == "CHANGED IN THE DRAFT" and real.get_object(MICH).brief_description != "CHANGED IN THE DRAFT"
with patch.object(real, "get_objects", wraps=real.get_objects) as batch:
    draft.get_objects([SELF, MICH])
    assert set(batch.call_args.args[0]) == {SELF}  # MICH is the draft's own

print("[check] a draft archive is visible too (archived rows are returned, as get_object does), and empty input costs nothing")
draft.archive_object(MICH)
assert draft.get_objects([MICH])[MICH].archived and not real.get_object(MICH).archived
assert draft.get_objects([]) == {}
print("\nALL CHECKS PASSED")
