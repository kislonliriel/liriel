"""MindReader: the number inside a ScenarioData circle is its place WITHIN ITS SUBJECT and restarts at 1 when a new subject begins.
No Postgres, no network. Whether a row opens a subject or continues one is the model's own declaration (SCENE_SUBJECT_CHECK's
`is_new_subject` / `continuing_scenario_data_id`), found by time; the rows' own Link_Subject_Cluster bonds are the fallback."""
import atexit
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("LLM_PROFILE", "1")  # no network: every LLM call in these scripts is mocked or not made
import sys
from pathlib import Path

GROQ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GROQ))

from config import settings  # noqa: E402
from database import JsonFileDatabase  # noqa: E402
from mindreader import app as mr  # noqa: E402
from models import VectorObjectValence  # noqa: E402

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


class Stub:
    """Just the four reads `_scenario_data_sequence` makes. `rows`: (vov_id, created_minute, updated_minute);
    `checks`: (logged_minute, is_new_subject, continuing_id); `bonds`: (from, to)."""

    def __init__(self, rows, checks, bonds=(), with_creation=True):
        self.rows, self.checks, self.bonds, self.with_creation = rows, checks, bonds, with_creation

    def get_all_objects(self, mov_id):
        return [VectorObjectValence(vov_id=v, object_nature="ScenarioData", brief_description=v, updated_at=at(u)) for v, _c, u in self.rows]

    def get_creation_times(self, mov_id, nature):
        return {v: at(c) for v, c, _u in self.rows} if self.with_creation else {}

    def get_scene_subject_check_timeline(self, mov_id):
        return [{"at": at(m), "is_new_subject": new, "continuing_scenario_data_id": cont} for m, new, cont in self.checks]

    def get_relations(self, ids, kinds=None):
        return [{"from_vov_id": a, "to_vov_id": b, "kind": "Link_Subject_Cluster"} for a, b in self.bonds]


def seq(stub):
    return {k: v for k, v in mr._scenario_data_sequence(stub, "M").items()}


print("[check] a new subject restarts at 1; a continuation counts on within its subject, even when other subjects came in between")
# each row is committed ~1 minute before its cycle is logged
rows = [("A1", 1, 1), ("A2", 11, 11), ("B1", 21, 21), ("A3", 31, 31), ("B2", 41, 41), ("C1", 51, 51)]
checks = [(2, True, None), (12, False, "A1"), (22, True, None), (32, False, "A2"), (42, False, "B1"), (52, True, None)]
s = seq(Stub(rows, checks))
assert [s[k][0] for k in ("A1", "A2", "B1", "A3", "B2", "C1")] == [1, 2, 1, 3, 2, 1], s
assert s["A3"][1] == "A1" and s["B2"][1] == "B1" and s["C1"][1] == "C1", s
print("[check] a continuation that names a row of the MIDDLE of a chain still joins the chain (A3 named A2 above: subject A1)")

print("[check] several rows written by one new-subject cycle share that subject and count on")
s = seq(Stub([("X1", 1, 1), ("X2", 1, 1), ("Y1", 11, 11)], [(2, True, None), (12, True, None)]))
assert s["X1"] == (1, "X1") and s["X2"] == (2, "X1") and s["Y1"] == (1, "Y1"), s

print("[check] updated_at is not the creation time: a row filed away (and restored) later keeps its place")
rows = [("A1", 1, 500), ("B1", 11, 11), ("A2", 21, 21)]          # A1 was archived much later, so its updated_at jumped
checks = [(2, True, None), (12, True, None), (22, False, "A1")]
s = seq(Stub(rows, checks))
assert s["A1"] == (1, "A1") and s["B1"] == (1, "B1") and s["A2"] == (2, "A1"), s
s = seq(Stub(rows, checks, with_creation=False))                  # a backend that keeps no creation time falls back to updated_at
assert set(s) == {"A1", "B1", "A2"}

print("[check] without a usable declaration the row follows its own Link_Subject_Cluster bond to an earlier ScenarioData, else opens a subject")
rows = [("A1", 1, 1), ("A2", 11, 11), ("A3", 21, 21), ("A4", 31, 31)]
checks = [(2, True, None), (12, False, None), (22, False, "A3"), (32, False, "Z9")]   # A2: continues but names nothing; A3 names itself; A4 names a row that is not there
s = seq(Stub(rows, checks, bonds=[("A2", "A1"), ("A4", "A2")]))
assert s["A2"] == (2, "A1"), s                      # followed its bond to A1
assert s["A3"] == (1, "A3"), s                      # named itself: no earlier row, no bond -> its own subject
assert s["A4"] == (3, "A1"), s                      # unresolved name, bond to A2 -> A2's subject
print("[check] no checks at all (a backend that keeps no times): the bonds alone chain the rows; unbonded rows each open a subject")
s = seq(Stub([("P1", 1, 1), ("P2", 11, 11), ("Q1", 21, 21)], [], bonds=[("P1", "P2")]))
assert s["P1"] == (1, "P1") and s["P2"] == (2, "P1") and s["Q1"] == (1, "Q1"), s
assert seq(Stub([], [])) == {}

print("[check] the database layer: the JSON store stamps each logged cycle, gives it back oldest first, and keeps no creation times")
tmp = Path(tempfile.mkdtemp(prefix="verify_seq_"))
atexit.register(shutil.rmtree, tmp, ignore_errors=True)
db = JsonFileDatabase(path=tmp / "db.json")
MOV = settings.default_mov_id
db.ensure_mov(MOV)
for i, (new, cont) in enumerate([(True, None), (False, "ScenarioData_One"), (True, None)]):
    db.upsert_object(MOV, VectorObjectValence(vov_id=f"ScenarioData_{['One', 'Two', 'Three'][i]}", object_nature="ScenarioData", brief_description="x"))
    time.sleep(0.02)   # the cycle is logged after its rows are committed, never at the same instant
    db.log_cycle(MOV, "t", {"is_new_subject": new, "continuing_scenario_data_id": cont}, None, None, None, None, None, None, None, "r")
    time.sleep(0.02)
db.log_cycle("OTHER_MOV", "t", {"is_new_subject": True}, None, None, None, None, None, None, None, "r")
tl = db.get_scene_subject_check_timeline(MOV)
assert [c["is_new_subject"] for c in tl] == [True, False, True] and all(isinstance(c["at"], datetime) for c in tl)
assert [c["at"] for c in tl] == sorted(c["at"] for c in tl)
assert db.get_creation_times(MOV, "ScenarioData") == {}
s = mr._scenario_data_sequence(db, MOV)
assert [s[k][0] for k in ("ScenarioData_One", "ScenarioData_Two", "ScenarioData_Three")] == [1, 2, 1], s

print("[check] the node carries the number and the subject; the hover title names the subject; nothing else changed")
vov = VectorObjectValence(vov_id="ScenarioData_Two", object_nature="ScenarioData", brief_description="second of the subject")
node = mr._node_from_vov(vov, None, None, 2, None, "ScenarioData_One")
assert node["label"] == "2" and node["detail"]["scenario_seq"] == 2 and node["detail"]["scenario_subject"] == "ScenarioData_One"
assert node["title"].startswith("#2 — ScenarioData_Two\nassunto: ScenarioData_One\n"), node["title"]
opener = mr._node_from_vov(VectorObjectValence(vov_id="ScenarioData_One", object_nature="ScenarioData", brief_description="x"), None, None, 1, None, "ScenarioData_One")
assert opener["label"] == "1" and "assunto:" not in opener["title"]
html = (GROQ / "mindreader" / "static" / "index.html").read_text(encoding="utf-8")
assert "restarts at 1 with each new subject" in html and "d.scenario_subject" in html
print("\nALL CHECKS PASSED")
