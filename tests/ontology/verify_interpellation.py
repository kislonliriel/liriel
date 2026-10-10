"""Rev 0007 AZ (MS 6.14): the `Interpellation` nature -- the structural half. No network.

An Interpellation sits BETWEEN its parties (origin / target, each possibly a set), tied to each by the Identity bond (Interpellation -> party) whose label says the role. The model decides that
one exists; the code only (1) accepts the nature, (2) lets a WRITE_RELATION that touches one through only as a role bond or the matter bond, turning a bond written the wrong way round, (3) never
writes an untyped bond for it from `relevant_relations`, (4) brings the archived Interpellations of an agent of the scene back with it, (5) recalls from an Interpellation without picking a master."""
import atexit
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("LLM_PROFILE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import graph_service  # noqa: E402
import motivation  # noqa: E402
from config import settings  # noqa: E402
from database import JsonFileDatabase  # noqa: E402
from models import INTERPELLATION_ROLES, VectorObjectValence, _CLOSED_OBJECT_NATURES  # noqa: E402

GROQ = Path(__file__).resolve().parents[2]
HERE = Path(tempfile.mkdtemp(prefix="liriel_interp_"))
atexit.register(shutil.rmtree, HERE, ignore_errors=True)
MOV = settings.default_mov_id
SELF = settings.liriel_self_vov_id
db = JsonFileDatabase(path=HERE / "interp.json")
db.ensure_mov(MOV)


def obj(vid, nature="Sentient", desc=None, **kw):
    db.upsert_object(MOV, VectorObjectValence(vov_id=vid, object_nature=nature, brief_description=desc or vid, **kw))


print("[check] the nature is a member of the closed list, mirrored by migration 011, and a row of it keeps its nature")
assert "Interpellation" in _CLOSED_OBJECT_NATURES and INTERPELLATION_ROLES == ("origin", "target")
sql = (GROQ / "migrations" / "011_interpellation.sql").read_text(encoding="utf-8")
listed = re.findall(r"'([A-Za-z-]+)'", sql[sql.index("check (object_nature in"):])
assert set(listed) == set(_CLOSED_OBJECT_NATURES), (set(listed) ^ set(_CLOSED_OBJECT_NATURES))
assert VectorObjectValence(vov_id="Interpellation_X", object_nature="Interpellation", brief_description="x").object_nature == "Interpellation"

obj(SELF, "PCI", "Liriel")
for vid in ("Sentient_Fabio", "Sentient_Marta", "Sentient_Rui"):
    obj(vid)
obj("Group_Familia", "Group", "the family")
obj("Interpellation_Fabio_TomRobotico", "Interpellation", "Fabio asks Liriel to stop sounding robotic when she talks to him")
obj("ScenarioData_Fabio_Robotico", "ScenarioData", "Fabio said she sounds robotic")
obj("Objective_Ouvir", "Objective", "listen", valence_regime="Delta", priority=2)
obj("Identity_Rec", "Identity", "a record")
obj("Interpellation_Outra", "Interpellation", "another one")
I = "Interpellation_Fabio_TomRobotico"


def write(*cmds):
    motivation._apply_write_relations(db, [dict(c) for c in cmds])


def bonds(vid):
    return [(r["from_vov_id"], r["to_vov_id"], r["kind"], r.get("label"), r.get("directed")) for r in db.get_relations([vid])]


print("[check] a role bond is carried out: Interpellation -> party, Identity bond, label origin/target, directed; a SET of parties is one bond each")
write({"from": I, "to": "Sentient_Fabio", "kind": "Link_Identity_Part", "label": "origin"},
      {"from": I, "to": SELF, "kind": "Link_Identity_Part", "label": "target"},
      {"from": I, "to": "Group_Familia", "kind": "Link_Identity_Part", "label": "Origin"})   # a group as origin, role word in another case
got = {(f, t, k, l) for f, t, k, l, d in bonds(I)}
assert (I, "Sentient_Fabio", "Link_Identity_Part", "origin") in got and (I, SELF, "Link_Identity_Part", "target") in got
assert (I, "Group_Familia", "Link_Identity_Part", "origin") in got, got
assert all(d for f, t, k, l, d in bonds(I) if k == "Link_Identity_Part"), "an Identity bond is always directed"

print("[check] written the wrong way round (party -> Interpellation), the bond is turned the right way")
write({"from": "Sentient_Marta", "to": I, "kind": "Link_Identity_Part", "label": "target"})
assert (I, "Sentient_Marta", "Link_Identity_Part", "target", True) in bonds(I) and ("Sentient_Marta", I, "Link_Identity_Part", "target", True) not in bonds(I)

print("[check] refused: no role / an unknown role / another kind of bond / a party that is a record, a matter, an Objective or another Interpellation")
before = len(bonds(I))
write({"from": I, "to": "Sentient_Rui", "kind": "Link_Identity_Part"},                       # no role
      {"from": I, "to": "Sentient_Rui", "kind": "Link_Identity_Part", "label": "source"},     # unknown role
      {"from": I, "to": "Sentient_Rui", "kind": "Link_Valence_Load", "propositional": "x"},   # another kind
      {"from": "Sentient_Rui", "to": I, "kind": "Link_Symbolic", "label": "boss"},
      {"from": I, "to": "Identity_Rec", "kind": "Link_Identity_Part", "label": "target"},
      {"from": I, "to": "Objective_Ouvir", "kind": "Link_Identity_Part", "label": "target"},
      {"from": I, "to": "Interpellation_Outra", "kind": "Link_Identity_Part", "label": "target"},
      {"from": I, "to": "Sentient_Rui", "kind": "Link_Subject_Cluster"})                      # a matter bond to a non-ScenarioData
assert len(bonds(I)) == before, bonds(I)

print("[check] the matter bond to its ScenarioData is allowed")
write({"from": "ScenarioData_Fabio_Robotico", "to": I, "kind": "Link_Subject_Cluster"})
assert any(k == "Link_Subject_Cluster" for f, t, k, l, d in bonds(I)) and len(bonds(I)) == before + 1

print("[check] relevant_relations never becomes an untyped bond with an Interpellation (the role cannot be guessed); the matter still does")
person = VectorObjectValence(vov_id="Sentient_Rui", object_nature="Sentient", brief_description="Rui", relevant_relations=[I, "Sentient_Fabio"])
motivation._ensure_relation_edges(db, person)
assert not [b for b in bonds("Sentient_Rui") if I in (b[0], b[1])], bonds("Sentient_Rui")
interp = VectorObjectValence(vov_id="Interpellation_Outra", object_nature="Interpellation", brief_description="x",
                             relevant_relations=["Sentient_Rui", "ScenarioData_Fabio_Robotico"])
motivation._ensure_relation_edges(db, interp)
assert not [b for b in bonds("Interpellation_Outra") if "Sentient_Rui" in (b[0], b[1])]
assert any(k == "Link_Subject_Cluster" for f, t, k, l, d in bonds("Interpellation_Outra"))

print("[check] pulling an agent of the scene pulls its archived Interpellations; Liriel's own row does not (every Interpellation aimed at her would return every cycle)")
db.archive_object(I)
assert db.get_object(I).archived
motivation._restore_scene_elements(db, [SimpleNamespace(vov_id=SELF)])
assert db.get_object(I).archived, "Liriel's own row restored an Interpellation"
motivation._restore_scene_elements(db, [SimpleNamespace(vov_id="Sentient_Rui")])
assert db.get_object(I).archived, "an agent the Interpellation is NOT tied to restored it"
motivation._restore_scene_elements(db, [SimpleNamespace(vov_id="Sentient_Fabio")])
assert not db.get_object(I).archived, "the origin of the Interpellation did not bring it back"
db.archive_object(I)
motivation._restore_scene_elements(db, [SimpleNamespace(vov_id="Sentient_Marta")])  # a target other than Liriel
assert not db.get_object(I).archived, "a target did not bring it back"
db.archive_object(I)
motivation._restore_scene_elements(db, [SimpleNamespace(vov_id=None), SimpleNamespace(vov_id="No_Such_Row")])  # nothing named: nothing happens
assert db.get_object(I).archived

print("[check] identity recall of a Sentient brings the Interpellations tied to it (satellites), and the recall of an Interpellation brings every party, with no 'master'")
db.restore_object(I)
fab = graph_service.build_identity_recall_request(db, "Sentient_Fabio")
assert I in {f for req in fab for f in req.focus_objects}, "the Interpellation did not come with its origin"
db.archive_object("Sentient_Fabio")
db.archive_object(SELF)
back = graph_service.build_identity_recall_request(db, I)
focus = {f for req in back for f in req.focus_objects}
assert {I, "Sentient_Fabio", SELF, "Group_Familia", "Sentient_Marta"} <= focus, focus
assert not db.get_object("Sentient_Fabio").archived and not db.get_object(SELF).archived, "the parties were not restored with the Interpellation"
assert "Sentient_Rui" not in focus and "Identity_Rec" not in focus
print("\nALL CHECKS PASSED")
