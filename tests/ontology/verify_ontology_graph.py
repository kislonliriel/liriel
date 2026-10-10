"""Ontology (MS Rev 0006) — retrieval: selective filters, volume limits, Identity records kept out
of recall/traversal/search and served on request, one Object in several subjects. No network."""
import atexit
import os
import shutil
import tempfile

os.environ.setdefault("LLM_PROFILE", "1")  # no network: every LLM call in these scripts is mocked or not made
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import settings  # noqa: E402
from database import JsonFileDatabase  # noqa: E402
from models import GraphRequestItem, IdentityRecordBlock, IdentityRecordRequest, RelationKey, VectorObjectValence  # noqa: E402
import graph_service  # noqa: E402
import motivation  # noqa: E402

HERE = Path(tempfile.mkdtemp(prefix="liriel_onto_"))
atexit.register(shutil.rmtree, HERE, ignore_errors=True)
MOV = settings.default_mov_id
p = HERE / "verify_onto_graph.json"
for q in [p, p.with_suffix(".cycles.jsonl"), p.with_name(p.stem + "_relations.json")]:
    q.unlink(missing_ok=True)
db = JsonFileDatabase(path=p)
db.ensure_mov(MOV)


def obj(vid, nature="Sentient", desc=None, **kw):
    db.upsert_object(MOV, VectorObjectValence(vov_id=vid, object_nature=nature, brief_description=desc or vid, **kw))


for vid in ("Clara", "Bruno", "Irma", "Lucas", "Pastor", "Casa"):
    obj(vid, "Thing" if vid == "Casa" else "Sentient")
obj("SD_Casal", "ScenarioData", "the couple's matter")
obj("SD_Filho", "ScenarioData", "the son's matter")
obj("Evento_Festa", "Event", "the party")

# bonds: kinds, labels, strengths, charges
db.write_relation("Clara", "Bruno", "Link_Genealogical", propositional="married", label="conjugal", directed=False, strength=5)
db.write_relation("Clara", "Irma", "Link_Genealogical", propositional="sisters", label="siblings", directed=False)  # no strength stated
db.write_relation("Clara", "Lucas", "Link_Genealogical", propositional="her son", label="parenthood", strength=2)
db.write_relation("Clara", "Bruno", "Link_Valence_Load", propositional="adores", directed=True, affective=[{"axis": "LoveAngerEros", "v": 4.0}])
db.write_relation("Clara", "Irma", "Link_Valence_Load", propositional="fond", directed=True, affective=[{"axis": "LoveAngerEros", "v": 1.0}])
db.write_relation("Pastor", "Clara", "Link_Symbolic", propositional="her pastor", label="pastor-congregant", directed=True, strength=3)
db.write_relation("Clara", "Evento_Festa", "Link_Space_Time", propositional="was at the party", label="festa")
# one Object, two subjects
for member, sd in (("Clara", "SD_Casal"), ("Bruno", "SD_Casal"), ("Clara", "SD_Filho"), ("Lucas", "SD_Filho")):
    db.write_relation(member, sd, "Link_Subject_Cluster")
# records of what was learned about Clara and about her marriage
bond = db.get_relations(["Clara", "Bruno"], ["Link_Genealogical"])[0]


def record(vid, target, attr, kind, recorded, info, rel=None):
    from models import RelationRef
    blk = IdentityRecordBlock(target_kind="relation" if rel else "object", target_vov_id=None if rel else target,
                              relation=RelationRef(**rel) if rel else None, field="x", attribute=attr, change_kind=kind,
                              information=info, recorded_at=recorded, reliability=3)
    db.upsert_object(MOV, VectorObjectValence(vov_id=vid, object_nature="Identity", brief_description=f"{attr}: {info}", identity_record=blk))
    db.archive_object(vid)
    if not rel:
        db.write_relation(vid, target, "Link_Identity_Part", directed=True)


record("Id_hair", "Clara", "hair colour", "added", "2026-10-01T10:00:00Z", "Clara has black hair")
record("Id_age", "Clara", "age", "world_change", "2026-10-02T10:00:00Z", "Clara turned 31")
record("Id_job", "Clara", "job", "corrected", "2026-10-03T10:00:00Z", "Clara is a nurse, not a teacher")
record("Id_marriage", None, "length", "added", "2026-10-04T10:00:00Z", "married twelve years",
       rel=dict(relation_id=str(bond["id"]), from_vov_id="Clara", to_vov_id="Bruno", kind="Link_Genealogical", label="conjugal"))


def graph(**kw):
    return graph_service.build_graph_of_traces(db, [GraphRequestItem(focus_objects=["Clara"], depth=1, **kw)], scenario_text="")


def edge_set(g):
    return {(e["kind"], e.get("label", "")) for e in g["edges"]}


# 1. kinds, labels, strength, charge, subject ----------------------------------------------------------------------------------------------
g = graph()
assert {"Link_Genealogical", "Link_Valence_Load", "Link_Symbolic", "Link_Space_Time", "Link_Subject_Cluster"} <= {e["kind"] for e in g["edges"]}
assert all(e.get("id") for e in g["edges"]) and any(e.get("label") == "pastor-congregant" and e["directed"] for e in g["edges"])
assert all(n["vov_id"] not in ("Id_hair", "Id_age", "Id_job", "Id_marriage") for n in g["nodes"]), "a record entered the graph"
print("[check] the graph walks all six kinds; edges carry id/label/direction; no Identity record becomes a node")

assert edge_set(graph(relation_kinds=["Link_Genealogical"])) == {("Link_Genealogical", "conjugal"), ("Link_Genealogical", "siblings"), ("Link_Genealogical", "parenthood")}
assert edge_set(graph(relation_kinds=["Link_Genealogical"], labels=["Conjugal"])) == {("Link_Genealogical", "conjugal")}
ms = edge_set(graph(relation_kinds=["Link_Genealogical"], min_strength=3))
assert ms == {("Link_Genealogical", "conjugal"), ("Link_Genealogical", "siblings")}, ms  # strength 2 out; NO strength stated stays (not a zero)
mc = edge_set(graph(relation_kinds=["Link_Valence_Load", "Link_Symbolic"], min_charge="strong"))
assert mc == {("Link_Valence_Load", ""), ("Link_Symbolic", "pastor-congregant")}, mc          # the mild charge is out; a non-emotional bond is untouched
g_sub = graph(within_subject="SD_Filho")
assert {n["vov_id"] for n in g_sub["nodes"]} == {"Clara", "Lucas", "SD_Filho"}, g_sub["nodes"]
assert not any(n["vov_id"] in ("Irma", "Pastor", "Bruno", "SD_Casal") for n in g_sub["nodes"]), "within_subject leaked another matter"
print("[check] filters narrow only: labels, min_strength (absent strength kept), min_charge (emotional only), within_subject")

# 2. volume limit on the bonds ---------------------------------------------------------------------------------------------------------------
object.__setattr__(settings, "graph_max_edges", 3)
gl = graph()
assert len(gl["edges"]) == 3 and gl["edges_omitted"] == len(g["edges"]) - 3, (len(gl["edges"]), gl.get("edges_omitted"))
object.__setattr__(settings, "graph_max_edges", 60)
print("[check] a graph never carries more bonds than GRAPH_MAX_EDGES and says how many it left out")

# 3. the node carries a summary, never the history ----------------------------------------------------------------------------------------------------
node = next(n for n in g["nodes"] if n["vov_id"] == "Clara")["identity_records"]
assert node["count"] == 3 and node["corrections"] == 1 and set(node["attributes"]) == {"hair colour", "age", "job"} and node["last_recorded_at"] == "2026-10-03T10:00:00Z", node
conj = next(e for e in g["edges"] if e.get("label") == "conjugal")
assert conj["identity_records"]["count"] == 1, conj
assert not next((n for n in g["nodes"] if n["vov_id"] == "Bruno"), {}).get("identity_records"), "a node without records got a summary"
print("[check] node/bond summaries: how many records, about what, how recent — and only where records exist")

# 4. identity recall and search leave the records out ---------------------------------------------------------------------------------------------------
db.write_relation("Irma", "Clara", "Link_Identity_Part", propositional="a Sub-Object of the same identity", directed=True)
req = graph_service.build_identity_recall_request(db, "Clara")
resolved = set(req[0].focus_objects)
assert resolved == {"Clara", "Irma"}, resolved
assert db.get_object("Id_hair").archived and db.get_object("Id_job").archived, "identity recall restored a record into the focus"
hits = graph_service.search_memory(db, db.load_mov(MOV), "black hair nurse teacher turned", force_blind=True)
assert not [h for h in hits if h["object_nature"] == "Identity"], hits
print("[check] identity recall returns the Master + Sub-Objects only (records stay archived); search never surfaces a record")

# 5. records on request: bounded, filtered, newest first ---------------------------------------------------------------------------------------------------
def ask(**kw):
    return graph_service.retrieve_identity_records(db, [IdentityRecordRequest(**kw)])[0]


r = ask(target="Clara")
assert [x["vov_id"] for x in r["records"]] == ["Id_job", "Id_age", "Id_hair"] and r["total_matching"] == 3 and "truncated" not in r, r
assert [x["vov_id"] for x in ask(target="Clara", limit=1)["records"]] == ["Id_job"] and ask(target="Clara", limit=1)["truncated"] is True
assert [x["vov_id"] for x in ask(target="Clara", change_kinds=["corrected"])["records"]] == ["Id_job"]
assert [x["vov_id"] for x in ask(target="Clara", attribute="hair")["records"]] == ["Id_hair"]
assert [x["vov_id"] for x in ask(target="Clara", since="2026-10-02", until="2026-10-02")["records"]] == ["Id_age"]
object.__setattr__(settings, "identity_records_max", 2)
capped = ask(target="Clara", limit=50)
assert len(capped["records"]) == 2 and capped["total_matching"] == 3 and capped["truncated"] is True, capped
object.__setattr__(settings, "identity_records_max", 20)
# one specific bond, named by its natural key — from either end for an undirected one, only as written for a directed one
k = dict(kind="Link_Genealogical", label="conjugal")
assert [x["vov_id"] for x in ask(relation={"from": "Clara", "to": "Bruno", **k})["records"]] == ["Id_marriage"]
assert [x["vov_id"] for x in ask(relation={"from": "Bruno", "to": "Clara", **k})["records"]] == ["Id_marriage"]
assert "unresolved" in ask(relation={"from": "Clara", "to": "Bruno", "kind": "Link_Genealogical", "label": "siblings"})
assert "unresolved" in ask(reason="no target")
print("[check] records on request: newest first, filtered by kind/attribute/period, capped by IDENTITY_RECORDS_MAX, one bond found by its key from either end")

# 6. the same Object in several subjects ---------------------------------------------------------------------------------------------------------------------
clusters = {r["to_vov_id"] for r in db.get_relations(["Clara"], ["Link_Subject_Cluster"])}
assert clusters == {"SD_Casal", "SD_Filho"} and len([v for v in db.all_vov_ids() if v == "Clara"]) == 1
assert graph_service._cluster_backbone_ids(db, "SD_Casal") == {"SD_Casal"}, "a shared member fused two subjects into one backbone"
assert graph_service._cluster_backbone_ids(db, "SD_Filho") == {"SD_Filho"}
db.write_relation("SD_Casal", "SD_Filho", "Link_Subject_Cluster", propositional="the same matter after all")  # a real backbone edge joins them
assert graph_service._cluster_backbone_ids(db, "SD_Casal") == {"SD_Casal", "SD_Filho"}
db.write_relation("SD_Casal", "SD_Filho", "Link_Subject_Cluster", directed=False)
# (and it is not left behind: the eviction check below needs the two subjects apart)
rels = db._load_relations(); db._save_relations([r for r in rels if not (r["kind"] == "Link_Subject_Cluster" and {r["from_vov_id"], r["to_vov_id"]} == {"SD_Casal", "SD_Filho"})])
object.__setattr__(settings, "memory_strength", 1)
# make SD_Casal the older subject, so ITS cluster is the one evicted
import time
db.upsert_object(MOV, db.get_object("SD_Filho"))
time.sleep(0.01)
motivation._evict_stale_clusters(db, db.load_mov(MOV))
assert db.get_object("SD_Casal").archived and not db.get_object("SD_Filho").archived
assert not db.get_object("Clara").archived, "an Object shared with a surviving subject was filed with the evicted one"
assert db.get_object("Bruno").archived and not db.get_object("Lucas").archived
print("[check] one Object, two subjects: one row; evicting one subject keeps the Object a surviving subject still needs, files the exclusive ones")

# 7. a small model's slips of shape never fail the whole query ------------------------------------------------------------------------------
from models import GraphRequestResult, IdentityUpdateResult  # noqa: E402

r = GraphRequestResult.model_validate({
    "requests": [{"focus_objects": ["A"], "labels": "conjugal", "min_strength": "3", "min_charge": 2, "within_subject": None}],
    "identity_requests": [{"target": "A", "limit": "many"}, {"target": "B", "relation": {"from_vov_id": "B", "to_vov_id": "C", "kind": "Link_Symbolic"}}, "junk"],
})
assert r.requests[0].labels == ["conjugal"] and r.requests[0].min_strength == 3 and r.requests[0].min_charge is None
assert [i.target for i in r.identity_requests] == ["B"] and r.identity_requests[0].relation.from_vov_id == "B"
u = IdentityUpdateResult.model_validate({"records": [{"change_ref": "c1", "information": None}, {"information": "no ref"}, {"change_ref": "c2", "information": "ok"}]})
assert [x.change_ref for x in u.records] == ["c2"]
print("[check] tolerance: a bare-string label, a non-numeric strength, a malformed request or record proposal — dropped or defaulted, the query stands")

for q in [p, p.with_suffix(".cycles.jsonl"), p.with_name(p.stem + "_relations.json")]:
    q.unlink(missing_ok=True)
print("\nALL CHECKS PASSED")
