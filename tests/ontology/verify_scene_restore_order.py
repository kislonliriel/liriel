"""Rev 0007 AW: a person Query 1 names as an element of the scene is brought back from the archive BEFORE the hunters are read and the rows reviewed (not only after Query 4),
so the person who is writing is reviewed in the cycle she returns in; Query 4 may still file her away and the restoration after it brings her back."""
import json
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import motivation  # noqa: E402
from config import settings  # noqa: E402
from database import JsonFileDatabase  # noqa: E402
from models import VectorObjectValence  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="verify_aw_"))
db = JsonFileDatabase(path=tmp / "db.json")
top = settings.default_mov_id
db.ensure_mov(top)
up = lambda **kw: db.upsert_object(top, VectorObjectValence(**kw))  # noqa: E731
up(vov_id=settings.liriel_self_vov_id, object_nature="PCI", brief_description="Liriel")
up(vov_id="Sentient_Present", object_nature="Sentient", brief_description="in focus")
up(vov_id="Sentient_Writer", object_nature="Sentient", brief_description="the person who is writing")
db.archive_object("Sentient_Writer")
up(vov_id="Objective_Old", object_nature="Objective", valence_regime="Delta", priority=2, brief_description="old aim")
db.archive_object("Objective_Old")

calls, review_targets = [], []


def fake_chat_json(messages, temperature=None, effort=None, model=None):
    user = messages[1]["content"]
    query = re.search(r"^query: (\w+)", user, re.M).group(1)
    calls.append(query)
    if query == "SAFETY_SCREEN":
        return {"query": "SAFETY_SCREEN", "at_risk": "none"}
    if query == "SCENE_SUBJECT_CHECK":
        return {"query": query, "is_new_subject": True, "interlocutor": "Sentient_Writer", "elements": [
            {"vov_id": "Sentient_Writer", "object_nature": "Sentient", "is_hunter": True, "new_this_cycle": False, "same_as": "the sender"},
            {"vov_id": "Objective_Old", "object_nature": "Objective", "is_hunter": False, "new_this_cycle": False}]}
    if query == "GRAPH_REQUEST":
        return {"query": query, "requests": [], "search_commands": [], "retrieval_satisfied": True}
    if query == "TACTICAL_SCENE_INTERPRETATION":
        return {"query": query, "board": {"summary": "s"}, "hunters": []}
    if query == "HUNTER_READING":
        return {"query": query, "ordinances_read": [], "schemas_read": []}
    if query == "ANCHOR_REVIEW":
        vov = re.search(r"^target: mov_id=(\S+) vov_id=(\S+)", user, re.M).group(2)
        review_targets.append(vov)
        return {"query": query, "evidence": []}
    if query == "MAINMEMORY_FILING":
        return {"query": query, "archive": [{"vov_id": "Sentient_Writer", "reason": "x"}], "restore": []}  # Query 4 files her away again
    if query == "MOV_UPDATE":
        return {"query": query, "mov_ops": [], "nested_mov_ops": []}
    if query == "RELATIONS_UPDATE":
        return {"query": query, "write_relations": [], "soften_charge": []}
    if query == "IDENTITY_UPDATE":
        return {"query": query, "records": []}
    return {"query": query, "best_prey_guess": {"vov_id": "Objective_Verify", "brief_description": "verify", "relevant_relations": []},
            "accompanying_objectives": [], "handoff_to_processcommandcontrol": {}, "mov_ops": [], "nested_mov_ops": []}


mov = db.load_mov(top)
assert "Sentient_Writer" not in [o.vov_id for o in mov.objects]  # archived when the cycle begins
with patch.object(motivation, "chat_json", side_effect=fake_chat_json), patch.object(motivation, "chat", side_effect=lambda *a, **k: "ok"):
    motivation.run_motivation_cycle(db, mov, "oi, sou eu", source="verify", sender="Writer")

print("[check] the archived person Query 1 named is reviewed (the restoration is in the cycle's draft: the real database changes only at commit)")
assert "Sentient_Writer" in review_targets, review_targets
print("[check] the reviews come before Query 4, and an archived Objective named by Query 1 is not brought back")
assert calls.index("ANCHOR_REVIEW") < calls.index("MAINMEMORY_FILING")
assert db.get_object("Objective_Old").archived
print("[check] Query 4 filed her away and the restoration after it brought her back: she ends in focus")
assert not db.get_object("Sentient_Writer").archived
print("\nALL CHECKS PASSED")
