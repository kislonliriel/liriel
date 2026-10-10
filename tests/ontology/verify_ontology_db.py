"""Ontology (MS Rev 0006) — persistence layer, JSON backend + DraftDatabase.
No network, no LLM. (The Postgres half runs in verify_ontology_pg.py.)"""
import atexit
import os
import shutil
import tempfile

os.environ.setdefault("LLM_PROFILE", "1")  # no network: every LLM call in these scripts is mocked or not made
import sys
from pathlib import Path

GROQ_DIR = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, GROQ_DIR)

from config import settings  # noqa: E402
from database import DraftDatabase, JsonFileDatabase  # noqa: E402
from models import IdentityRecordBlock, RelationRef, VectorObjectValence  # noqa: E402

HERE = Path(tempfile.mkdtemp(prefix="liriel_onto_"))
atexit.register(shutil.rmtree, HERE, ignore_errors=True)


def fresh(name):
    p = HERE / name
    for q in [p, p.with_suffix(".cycles.jsonl"), p.with_name(p.stem + "_relations.json")]:
        q.unlink(missing_ok=True)
    return JsonFileDatabase(path=p)


def vov(vid, nature="Sentient", desc="x", **kw):
    return VectorObjectValence(vov_id=vid, object_nature=nature, brief_description=desc, **kw)


def seed(db):
    db.ensure_mov(settings.default_mov_id)
    for vid in ("A", "B", "C"):
        db.upsert_object(settings.default_mov_id, vov(vid))


def rels(db, ids=("A", "B"), **kw):
    return [r for r in db.get_relations(list(ids), **kw)]


def check_relation_semantics(db, tag):
    # 1. several bonds, one pair: other categories and the same category told apart by label
    db.write_relation("A", "B", "Link_Genealogical", propositional="married", label="conjugal", strength=5)
    db.write_relation("A", "B", "Link_Symbolic", propositional="A employs B", label="employer–employee")
    db.write_relation("A", "B", "Link_Symbolic", propositional="A is B's pastor", label="Pastor–Congregant")
    db.write_relation("A", "B", "Link_Subject_Cluster")
    assert len(rels(db)) == 4, rels(db)
    sym = [r for r in rels(db) if r["kind"] == "Link_Symbolic"]
    assert {r["label"] for r in sym} == {"employer–employee", "pastor–congregant"}, sym

    # 2. updating one bond touches no other, and an omitted field is not blanked
    db.write_relation("A", "B", "Link_Symbolic", label="employer–employee", confidence=3)
    emp = next(r for r in rels(db) if r["label"] == "employer–employee")
    assert emp["propositional"] == "A employs B" and emp["confidence"] == 3, emp
    pas = next(r for r in rels(db) if r["label"] == "pastor–congregant")
    assert pas["propositional"] == "A is B's pastor" and pas["confidence"] is None, pas
    assert len(rels(db)) == 4

    # 3. the relation of A with B is not the relation of B with A
    db.write_relation("A", "B", "Link_Valence_Load", propositional="A adores B",
                      affective=[{"axis": "LoveAngerEros", "v": 4.0}], directed=True)
    db.write_relation("B", "A", "Link_Valence_Load", propositional="B tolerates A",
                      affective=[{"axis": "LoveAngerEros", "v": 1.0}], directed=True)
    val = [r for r in rels(db) if r["kind"] == "Link_Valence_Load"]
    assert len(val) == 2 and all(r["directed"] for r in val), val
    ab = next(r for r in val if r["from_vov_id"] == "A")
    ba = next(r for r in val if r["from_vov_id"] == "B")
    assert ab["propositional"] == "A adores B" and ba["propositional"] == "B tolerates A"
    assert ab["affective"][0]["v"] == 4.0 and ba["affective"][0]["v"] == 1.0
    db.write_relation("B", "A", "Link_Valence_Load", propositional="B has warmed to A",
                      affective=[{"axis": "LoveAngerEros", "v": 2.0}])  # directed omitted -> keeps True
    ab2 = next(r for r in db.get_relations(["A", "B"]) if r["kind"] == "Link_Valence_Load" and r["from_vov_id"] == "A")
    assert ab2["propositional"] == "A adores B" and ab2["affective"][0]["v"] == 4.0, "updating B->A touched A->B"
    ba2 = next(r for r in db.get_relations(["A", "B"]) if r["kind"] == "Link_Valence_Load" and r["from_vov_id"] == "B")
    assert ba2["directed"] and ba2["affective"][0]["v"] == 2.0 and ba2["id"] == ba["id"]

    # 4. affective=[] clears the charge, None keeps it
    db.write_relation("B", "A", "Link_Valence_Load", affective=[])
    assert next(r for r in db.get_relations(["A", "B"]) if r["kind"] == "Link_Valence_Load" and r["from_vov_id"] == "B")["affective"] == []

    # 5. an undirected bond is ONE row, whichever way round it is written
    db.write_relation("C", "A", "Link_Space_Time", propositional="met at the party", label="festa")
    db.write_relation("A", "C", "Link_Space_Time", propositional="met at the party (Ana's)", label="festa")
    st = [r for r in db.get_relations(["A", "C"]) if r["kind"] == "Link_Space_Time"]
    assert len(st) == 1 and st[0]["directed"] is False and st[0]["propositional"].endswith("(Ana's)"), st
    assert (st[0]["from_vov_id"], st[0]["to_vov_id"]) == ("A", "C")  # fixed order
    # ... and a directed one is not folded into it
    db.write_relation("C", "A", "Link_Space_Time", propositional="C hosted A", label="festa", directed=True)
    assert len([r for r in db.get_relations(["A", "C"]) if r["kind"] == "Link_Space_Time"]) == 2

    # 6. defaults by kind
    assert next(r for r in rels(db) if r["kind"] == "Link_Subject_Cluster")["directed"] is False
    db.write_relation("B", "C", "Link_Identity_Part")
    assert db.get_relations(["B", "C"], ["Link_Identity_Part"])[0]["directed"] is True
    print(f"[check] {tag}: parallel bonds, asymmetry, partial updates, undirected-once, defaults OK")


def check_identity_records(db, tag):
    mov = settings.default_mov_id

    def rec(vid, target, attr, kind, recorded, occurred=None, rel=None):
        blk = IdentityRecordBlock(
            target_kind="relation" if rel else "object", target_vov_id=None if rel else target, relation=rel,
            field="brief_description", attribute=attr, change_kind=kind, information=f"{attr} {kind}",
            recorded_at=recorded, occurred_at=occurred, reliability=3,
        )
        v = vov(vid, "Identity", f"{attr}: {kind}", identity_record=blk)
        db.upsert_object(mov, v)
        db.archive_object(vid)

    rec("Identity_1", "A", "cor do cabelo", "added", "2026-10-01T10:00:00Z")
    rec("Identity_2", "A", "cor do cabelo", "corrected", "2026-10-03T10:00:00Z")
    rec("Identity_3", "A", "profession", "world_change", "2026-10-02T10:00:00Z", occurred="2024-03")
    rec("Identity_4", "B", "idade", "added", "2026-10-02T11:00:00Z")
    relid = db.get_relations(["A", "B"], ["Link_Genealogical"])[0]["id"]
    rec("Identity_5", None, "bond strength", "world_change", "2026-10-04T10:00:00Z",
        rel=RelationRef(relation_id=relid, from_vov_id="A", to_vov_id="B", kind="Link_Genealogical", label="conjugal"))

    got = db.get_identity_records(target_vov_ids=["A"])
    assert [r.vov_id for r in got] == ["Identity_2", "Identity_3", "Identity_1"], [r.vov_id for r in got]  # newest first
    assert [r.vov_id for r in db.get_identity_records(target_vov_ids=["A"], limit=1)] == ["Identity_2"]
    assert [r.vov_id for r in db.get_identity_records(target_vov_ids=["A"], attribute="cabelo")] == ["Identity_2", "Identity_1"]
    assert [r.vov_id for r in db.get_identity_records(target_vov_ids=["A"], change_kinds=["corrected"])] == ["Identity_2"]
    assert [r.vov_id for r in db.get_identity_records(target_vov_ids=["A"], since="2026-10-02")] == ["Identity_2"]
    assert {r.vov_id for r in db.get_identity_records(target_vov_ids=["A"], until="2024-12-31")} == {"Identity_3"}
    assert {r.vov_id for r in db.get_identity_records(target_vov_ids=["A"], since="2024", until="2024")} == {"Identity_3"}
    assert [r.vov_id for r in db.get_identity_records(relation_ids=[relid])] == ["Identity_5"]
    assert db.get_identity_records(target_vov_ids=["C"]) == []
    # a principal Object's own retrieval never drags its history in
    assert all(o.object_nature != "Identity" for o in db.load_mov(mov).objects)
    idx = db.identity_index(target_vov_ids=["A", "B", "C"], relation_ids=[relid])
    assert idx["objects"]["A"]["count"] == 3 and idx["objects"]["A"]["corrections"] == 1, idx
    assert idx["objects"]["A"]["last_recorded_at"] == "2026-10-03T10:00:00Z" and "C" not in idx["objects"]
    assert idx["relations"][relid]["count"] == 1
    print(f"[check] {tag}: Identity records — filters, newest-first, limit, period (prefix), relation ref, index, never in the focus OK")


def check_draft_and_commit():
    real = fresh("verify_onto_real.json")
    seed(real)
    real.write_relation("A", "B", "Link_Genealogical", propositional="married", label="conjugal", directed=False, strength=5)
    real.write_relation("A", "B", "Link_Valence_Load", propositional="A adores B", directed=True,
                        affective=[{"axis": "LoveAngerEros", "v": 4.0}])
    real_rel = {r["kind"]: r for r in real.get_relations(["A", "B"])}

    draft = DraftDatabase(real)
    # an undirected real bond written from the other end is the SAME bond, and the draft reports the real id
    draft.write_relation("B", "A", "Link_Genealogical", label="Conjugal", propositional="married, 12 years")
    seen = [r for r in draft.get_relations(["A", "B"]) if r["kind"] == "Link_Genealogical"]
    assert len(seen) == 1 and seen[0]["id"] == real_rel["Link_Genealogical"]["id"], seen
    assert seen[0]["strength"] == 5 and seen[0]["propositional"] == "married, 12 years"
    # a draft edit of the directed bond leaves the reverse and the others alone
    draft.write_relation("A", "B", "Link_Valence_Load", affective=[{"axis": "LoveAngerEros", "v": 5.0}])
    draft.write_relation("B", "A", "Link_Valence_Load", propositional="B tolerates A", directed=True)
    assert real.get_relations(["A", "B"], ["Link_Valence_Load"])[0]["affective"][0]["v"] == 4.0, "draft leaked before commit"
    # what changed this cycle, from the committed state
    assert draft.get_committed_relations(["A", "B"], ["Link_Genealogical"])[0]["propositional"] == "married"
    assert draft.drafted_relations(), "no drafted relations"
    draft.commit()
    after = real.get_relations(["A", "B"])
    gen = [r for r in after if r["kind"] == "Link_Genealogical"]
    assert len(gen) == 1 and gen[0]["id"] == real_rel["Link_Genealogical"]["id"] and gen[0]["propositional"] == "married, 12 years"
    assert gen[0]["strength"] == 5 and gen[0]["directed"] is False
    val = {r["from_vov_id"]: r for r in after if r["kind"] == "Link_Valence_Load"}
    assert set(val) == {"A", "B"} and val["A"]["affective"][0]["v"] == 5.0 and val["A"]["propositional"] == "A adores B"
    assert val["B"]["propositional"] == "B tolerates A" and val["B"]["directed"] is True
    assert len(after) == 3, after
    draft.cleanup()
    print("[check] DraftDatabase: undirected-once across real+draft, real id reported, no leak before commit, faithful replay OK")

    # atomic commit: a failure half-way leaves the real database exactly as it was
    real2 = fresh("verify_onto_real2.json")
    seed(real2)
    real2.write_relation("A", "B", "Link_Symbolic", propositional="boss", label="chefe")
    before_objs, before_rels = real2._path.read_bytes(), real2._relations_path.read_bytes()
    d2 = DraftDatabase(real2)
    d2.upsert_object(settings.default_mov_id, vov("A", desc="A, now with black hair"))
    d2.write_relation("A", "B", "Link_Symbolic", label="chefe", propositional="former boss")
    d2.write_relation("A", "C", "Link_Genealogical", label="siblings")
    calls = {"n": 0}
    orig = real2.write_relation

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure mid-commit")
        return orig(*a, **kw)

    real2.write_relation = flaky
    try:
        d2.commit()
    except RuntimeError:
        pass
    else:
        raise AssertionError("commit should have failed")
    assert real2._path.read_bytes() == before_objs and real2._relations_path.read_bytes() == before_rels, \
        "a failed commit left a partial write behind"
    real2.write_relation = orig
    d2.commit()  # and the very same draft commits cleanly once the fault is gone
    assert real2.get_object("A").brief_description == "A, now with black hair"
    assert len(real2.get_relations(["A"])) == 2
    d2.cleanup()
    print("[check] atomic commit: a mid-commit failure rolls the whole thing back; the retry then lands completely OK")


def main():
    db = fresh("verify_onto_a.json")
    seed(db)
    check_relation_semantics(db, "JsonFileDatabase")
    check_identity_records(db, "JsonFileDatabase")
    check_draft_and_commit()
    # the same Identity checks through a draft over a real store
    real = fresh("verify_onto_real3.json")
    seed(real)
    real.write_relation("A", "B", "Link_Genealogical", label="conjugal")
    d = DraftDatabase(real)
    check_identity_records(d, "DraftDatabase")
    d.cleanup()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
