"""Ontology (MS Rev 0006) — one whole ProcessMotivation cycle, JsonFileDatabase/DraftDatabase,
every LLM call mocked. No network.

Seeds a persistent Sentient (Clara) with a conjugal bond to Bruno, a second bond of another
kind, and one Identity record already on file; then a cycle that teaches Liriel three things
about Clara and changes the conjugal bond.
"""
import atexit
import os
import shutil
import tempfile

os.environ.setdefault("LLM_PROFILE", "1")  # no network: every LLM call in these scripts is mocked or not made
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import settings  # noqa: E402
from database import JsonFileDatabase  # noqa: E402
from models import IdentityRecordBlock, SchemaEntry, VectorObjectValence  # noqa: E402
import motivation  # noqa: E402

HERE = Path(tempfile.mkdtemp(prefix="liriel_onto_"))
atexit.register(shutil.rmtree, HERE, ignore_errors=True)
MOV = settings.default_mov_id
MICH, OTAV = "Sentient_Clara", "Sentient_Bruno"
SEEDED_RECORD = "Identity_Sentient_Clara_relevant_remarks"


def section(text, header):
    """The text under an `ARTIFACT: <header>...` line, up to the next ARTIFACT/QUERY block."""
    m = re.search(rf"^ARTIFACT: {re.escape(header)}[^\n]*\n(.*?)(?=^ARTIFACT:|^QUERY)", text, re.S | re.M)
    assert m, f"no ARTIFACT {header!r} in prompt"
    return m.group(1).strip()


def fresh(name):
    p = HERE / name
    for q in [p, p.with_suffix(".cycles.jsonl"), p.with_name(p.stem + "_relations.json")]:
        q.unlink(missing_ok=True)
    return JsonFileDatabase(path=p)


def seed(db):
    db.ensure_mov(MOV)
    up = lambda **kw: db.upsert_object(MOV, VectorObjectValence(**kw))  # noqa: E731
    up(vov_id=settings.liriel_self_vov_id, object_nature="PCI", brief_description="Liriel")
    up(vov_id=MICH, object_nature="Sentient", brief_description="Clara, wife of Bruno", perceived_age="30",
       relevant_remarks="likes tea", schemas={"CharacterEmpathy": SchemaEntry(v=2.0, c=3)}, relevant_relations=[OTAV])
    up(vov_id=OTAV, object_nature="Sentient", brief_description="Bruno, Clara's husband", relevant_relations=[MICH])
    db.write_relation(MICH, OTAV, "Link_Genealogical", propositional="married", label="conjugal", directed=False)
    db.write_relation(MICH, OTAV, "Link_Valence_Load", propositional="Clara adores Bruno", directed=True,
                      affective=[{"axis": "LoveAngerEros", "v": 4.0}])
    blk = IdentityRecordBlock(
        target_vov_id=MICH, field="relevant_remarks", attribute="tastes", change_kind="added", information="likes tea",
        value_after="likes tea", source="Fábio", obtained_via="reported", reliability=3, recorded_at="2026-09-01T10:00:00Z",
    )
    db.upsert_object(MOV, VectorObjectValence(
        vov_id=SEEDED_RECORD, object_nature="Identity", brief_description="tastes: likes tea", identity_record=blk))
    db.archive_object(SEEDED_RECORD)
    db.write_relation(SEEDED_RECORD, MICH, "Link_Identity_Part", directed=True)


def responses(prompts_by_query, calls):
    base = {
        "SCENE_SUBJECT_CHECK": {"query": "SCENE_SUBJECT_CHECK", "is_new_subject": False,
                                "elements": [{"vov_id": MICH, "object_nature": "Sentient", "is_hunter": False, "new_this_cycle": False}]},
        "GRAPH_REQUEST": {"query": "GRAPH_REQUEST", "search_commands": [], "retrieval_satisfied": True,
                          "requests": [{"focus_objects": [MICH], "relation_kinds": ["Link_Genealogical", "Link_Valence_Load"], "depth": 1}],
                          "identity_requests": [{"target": MICH, "limit": 3}]},
        "TACTICAL_SCENE_INTERPRETATION": {"query": "TACTICAL_SCENE_INTERPRETATION", "board": {"summary": "Fábio tells about Clara"}, "relations_summary": "r"},
        "HUNTER_READING": {"query": "HUNTER_READING", "ordinances_read": [], "schemas_read": []},
        "ANCHOR_REVIEW": {"query": "ANCHOR_REVIEW", "feelings_changes": [], "schemas_changes": []},
        "MAINMEMORY_FILING": {"query": "MAINMEMORY_FILING", "archive": [], "restore": [{"vov_id": SEEDED_RECORD, "reason": "the model tries to bring a record into the focus"}]},
        "MOV_UPDATE": {"query": "MOV_UPDATE", "nested_mov_ops": [], "mov_ops": [
            {"op": "PATCH_VOV", "vov_id": MICH, "patch": {"brief_description": "Clara, Bruno's wife, has black hair", "perceived_age": "31"}},
            {"op": "UPSERT_VOV", "vov": {"vov_id": "Sentient_Lucas", "object_nature": "Sentient", "brief_description": "Lucas, Clara's son", "relevant_relations": [MICH]}},
            {"op": "UPSERT_VOV", "vov": {"vov_id": "Thing_Casa", "object_nature": "Thing", "brief_description": "The couple's house", "relevant_relations": [OTAV]}},
            {"op": "UPSERT_VOV", "vov": {"vov_id": "Identity_Forged", "object_nature": "Identity", "brief_description": "a record written by the model"}},
            {"op": "PATCH_VOV", "vov_id": SEEDED_RECORD, "patch": {"brief_description": "tampered"}},
        ]},
        "RELATIONS_UPDATE": {"query": "RELATIONS_UPDATE", "soften_charge": [], "write_relations": [
            {"from": OTAV, "to": MICH, "kind": "Link_Genealogical", "label": "Conjugal", "propositional": "married for 12 years", "strength": 5},
            {"from": MICH, "to": OTAV, "kind": "Link_Symbolic", "label": "patroa-empregado", "directed": True, "propositional": "Clara is Bruno's boss at the shop"},
            {"from": "Sentient_Lucas", "to": MICH, "kind": "Link_Genealogical", "label": "parenthood", "directed": True, "propositional": "Lucas is Clara's son"},
            {"from": MICH, "to": "Sentient_Lucas", "kind": "Link_Valence_Load", "directed": True, "affective": [{"axis": "LoveAngerEros", "v": "strong Love/Eros"}]},
            {"from": SEEDED_RECORD, "to": OTAV, "kind": "Link_Symbolic", "propositional": "a model relating a record"},
        ]},
        "BEST_PREY_GUESS": {"query": "BEST_PREY_GUESS", "accompanying_objectives": [], "handoff_to_processcommandcontrol": {},
                            "best_prey_guess": {"vov_id": "Objective_Verify", "brief_description": "verify", "relevant_relations": []},
                            "mov_ops": [{"op": "PATCH_VOV", "vov_id": MICH, "patch": {"relevant_remarks": "likes tea, hates coffee"}}],
                            "nested_mov_ops": []},
    }

    def identity(user):
        label = re.search(r"^target: (.+)$", user, re.M).group(1)
        ledger = {c["field"]: c for c in json.loads(section(user, "what changed in it"))}
        if label == MICH:
            return {"query": "IDENTITY_UPDATE", "target_id": "WRONG_ECHO", "records": [
                {"change_ref": ledger["brief_description"]["ref"], "change_kind": "added", "attribute": "hair colour",
                 "information": "Clara has black hair", "source": "Fábio, in this message", "obtained_via": "reported", "reliability": 4},
                {"change_ref": ledger["perceived_age"]["ref"], "change_kind": "world change", "attribute": "age",
                 "information": "Clara turned 31", "obtained_via": "inferred", "reliability": 9, "occurred_at": "2026-10"},
                {"change_ref": ledger["relevant_remarks"]["ref"], "change_kind": "Correction", "attribute": "tastes",
                 "information": "she hates coffee, not only likes tea", "supersedes": SEEDED_RECORD, "reliability": 3},
                {"change_ref": "c99", "information": "a change that is not in the ledger"},
                {"change_ref": ledger["brief_description"]["ref"], "information": "a second record for the same change"},
            ]}
        if "Link_Genealogical" in label:
            ledger = {c["field"]: c for c in json.loads(section(user, "what changed in it"))}
            return {"query": "IDENTITY_UPDATE", "records": [
                {"change_ref": ledger["propositional"]["ref"], "change_kind": "added", "attribute": "length of marriage",
                 "information": "they have been married for twelve years", "reliability": 4}]}
        return {"query": "IDENTITY_UPDATE", "records": []}

    def fake_chat_json(messages, temperature=None, effort=None, model=None):
        user = messages[1]["content"]
        query = re.search(r"^query: (\w+)", user, re.M).group(1)
        calls.append(query)
        prompts_by_query.setdefault(query, []).append(user)
        return identity(user) if query == "IDENTITY_UPDATE" else base[query]

    return fake_chat_json


def run(store_name, flaky_replace=False):
    db = fresh(store_name)
    seed(db)
    before_files = (db._path.read_bytes(), db._relations_path.read_bytes())
    calls, prompts = [], {}
    if flaky_replace:
        orig, n = db.replace_object, {"n": 0}

        def boom(*a, **kw):
            n["n"] += 1
            if n["n"] == 3:
                raise RuntimeError("simulated failure inside the commit")
            return orig(*a, **kw)

        db.replace_object = boom
    mov = db.load_mov(MOV)
    with patch.object(motivation, "chat_json", side_effect=responses(prompts, calls)), \
         patch.object(motivation, "chat", side_effect=lambda *a, **k: "ok"):
        try:
            motivation.run_motivation_cycle(db, mov, "A Clara pintou o cabelo? Ela tem cabelo preto.", source="verify")
            failed = None
        except RuntimeError as exc:
            failed = exc
    return db, calls, prompts, before_files, failed


def main():
    db, calls, prompts, _, failed = run("verify_onto_cycle.json")
    assert failed is None, failed

    # --- the cycle's shape ---------------------------------------------------------------------------
    assert calls[-3:] == ["BEST_PREY_GUESS", "IDENTITY_UPDATE", "IDENTITY_UPDATE"], calls
    targets = [re.search(r"^target: (.+)$", u, re.M).group(1) for u in prompts["IDENTITY_UPDATE"]]
    assert targets[0] == MICH and "Link_Genealogical" in targets[1], targets
    assert len(targets) == 2, f"asked about something that did not change/exist before: {targets}"
    print(f"[check] Query 6A asked about exactly the two changed targets, after the decision: {targets}")

    # --- the ledger and what the model saw -----------------------------------------------------------------
    obj_prompt = prompts["IDENTITY_UPDATE"][0]
    ledger = {c["field"]: c for c in json.loads(section(obj_prompt, "what changed in it"))}
    assert set(ledger) == {"brief_description", "relevant_remarks", "perceived_age"}, ledger  # incl. the DECISION's patch
    assert ledger["brief_description"]["before"] == "Clara, wife of Bruno" and ledger["perceived_age"]["after"] == "31"
    assert SEEDED_RECORD in obj_prompt, "the records already on file were not shown"
    assert "Sentient_Lucas" not in " ".join(targets), "an Object born this cycle was treated as a change"
    print("[check] ledger: committed-vs-draft diff incl. the decision's own patch; prior records shown; births excluded")

    # --- the records ----------------------------------------------------------------------------------------------
    recs = db.get_identity_records(target_vov_ids=[MICH])
    new = [r for r in recs if r.vov_id != SEEDED_RECORD]
    assert len(recs) == 4 and len(new) == 3, [r.vov_id for r in recs]
    by_field = {r.identity_record.field: r for r in new}
    hair = by_field["brief_description"].identity_record
    assert hair.value_before == "Clara, wife of Bruno" and hair.value_after == "Clara, Bruno's wife, has black hair", hair
    assert hair.change_kind == "added" and hair.obtained_via == "reported" and hair.source == "Fábio, in this message"
    age = by_field["perceived_age"].identity_record
    assert age.change_kind == "world_change" and age.reliability == 5 and age.occurred_at == "2026-10", age  # 9 clamped to 5
    fix = by_field["relevant_remarks"].identity_record
    assert fix.change_kind == "corrected" and fix.supersedes == SEEDED_RECORD, fix
    assert fix.value_before == "likes tea" and fix.value_after == "likes tea, hates coffee"
    for r in new:
        assert db.get_object(r.vov_id).archived and r.identity_record.recorded_at.endswith("Z") and r.identity_record.cycle_id
        assert r.identity_record.target_vov_id == MICH and r.object_nature == "Identity"
        links = db.get_relations([r.vov_id], ["Link_Identity_Part"])
        assert len(links) == 1 and (links[0]["from_vov_id"], links[0]["to_vov_id"], links[0]["directed"]) == (r.vov_id, MICH, True)
    seeded = db.get_object(SEEDED_RECORD)
    assert seeded.identity_record.value_after == "likes tea" and seeded.identity_record.information == "likes tea", "a record was edited"
    assert "tampered" not in seeded.brief_description
    assert db.get_object("Identity_Forged") is None, "the model created an Identity row"
    assert all(o.object_nature != "Identity" for o in db.load_mov(MOV).objects), "a record entered the focus"
    assert db.get_object(SEEDED_RECORD).archived, "a restore brought a record into the focus"
    print("[check] records: before/after from the ledger, 3 kinds kept apart, correction supersedes, reliability clamped, "
          "born archived, bound record->Object, seeded record untouched, forged/edited/restored records refused")

    # --- the relation record --------------------------------------------------------------------------------------------
    conj = [r for r in db.get_relations([MICH, OTAV]) if r["kind"] == "Link_Genealogical"
            and {r["from_vov_id"], r["to_vov_id"]} == {MICH, OTAV}]
    assert len(conj) == 1 and conj[0]["strength"] == 5 and conj[0]["propositional"] == "married for 12 years" and conj[0]["directed"] is False, conj
    rrec = db.get_identity_records(relation_ids=[str(conj[0]["id"])])
    assert len(rrec) == 1, rrec
    blk = rrec[0].identity_record
    assert blk.target_kind == "relation" and blk.target_vov_id is None
    assert (blk.relation.relation_id, blk.relation.from_vov_id, blk.relation.to_vov_id, blk.relation.kind, blk.relation.label) == \
        (str(conj[0]["id"]), *sorted((MICH, OTAV)), "Link_Genealogical", "conjugal"), blk.relation  # an undirected bond is kept in id order
    assert not db.get_relations([rrec[0].vov_id]), "a relation record got an edge"
    print("[check] relation record cites that bond's own id + key snapshot; no edge-on-edge; the bond updated once, not duplicated")

    # --- coexistence / asymmetry ---------------------------------------------------------------------------------------------
    pair = {(r["kind"], r["label"]): r for r in db.get_relations([MICH, OTAV]) if "Identity" not in r["kind"]
            and {r["from_vov_id"], r["to_vov_id"]} == {MICH, OTAV}}
    assert set(pair) == {("Link_Genealogical", "conjugal"), ("Link_Valence_Load", ""), ("Link_Symbolic", "patroa-empregado")}, pair
    assert pair[("Link_Valence_Load", "")]["propositional"] == "Clara adores Bruno", "an update to another bond altered it"
    assert pair[("Link_Symbolic", "patroa-empregado")]["directed"] is True
    print("[check] three bonds of three kinds coexist between one pair; writing one left the others as they were")

    # --- the untyped fallback runs AFTER the typed relations ---------------------------------------------------------------------
    lucas = db.get_relations(["Sentient_Lucas"])
    kinds = {(r["kind"], r["from_vov_id"]) for r in lucas}
    assert ("Link_Genealogical", "Sentient_Lucas") in kinds and ("Link_Valence_Load", "Sentient_Lucas") not in kinds, kinds
    assert len([r for r in lucas if r["kind"] == "Link_Valence_Load"]) == 1  # the model's own, Clara -> Lucas
    casa = db.get_relations(["Thing_Casa"])
    assert len(casa) == 1 and casa[0]["kind"] == "Link_Valence_Load", casa  # nothing typed it: the fallback fills the gap
    assert casa[0]["propositional"] == motivation._UNTYPED_BOND_TEXT and not casa[0]["affective"], "the fallback bond does not say it is untyped"
    # typing an untyped bond for the first time is not a change of a known bond: no ledger entry, no Query 6A call for it
    assert motivation._relation_ledger({"propositional": motivation._UNTYPED_BOND_TEXT}, {"propositional": "Bruno owns the house"}) == []
    assert motivation._relation_ledger({"propositional": "married"}, {"propositional": "married for 12 years"})[0][0] == "propositional"
    assert not [r for r in db.get_relations(["Sentient_Lucas"]) if r["to_vov_id"] == SEEDED_RECORD or r["from_vov_id"] == SEEDED_RECORD]
    assert not [r for r in db.get_relations([OTAV]) if SEEDED_RECORD in (r["from_vov_id"], r["to_vov_id"]) and r["kind"] == "Link_Symbolic"], \
        "a model-written bond naming a record was accepted"
    print("[check] fallback bond only where nothing typed the pair (Thing_Casa); none beside the typed Lucas bond; bond naming a record refused")

    # --- retrieval: the record is asked for, the history is not dragged in ------------------------------------------------------------
    tactical = prompts["TACTICAL_SCENE_INTERPRETATION"][0]
    graph = json.loads(section(tactical, "GraphOfTraces"))
    assert all(n["vov_id"] != SEEDED_RECORD for n in graph["nodes"]), "a record entered the traversal"
    node = next(n for n in graph["nodes"] if n["vov_id"] == MICH)
    assert node["identity_records"]["count"] == 1 and node["identity_records"]["attributes"] == ["tastes"], node
    assert graph["identity_records"][0]["records"][0]["vov_id"] == SEEDED_RECORD and graph["identity_records"][0]["total_matching"] == 1
    assert any(e.get("label") == "conjugal" and e.get("id") for e in graph["edges"]), graph["edges"]
    print("[check] the node carries only a summary of its records; the requested record arrives in identity_records; edges expose id/label")

    # --- the log ----------------------------------------------------------------------------------------------------------------------------------
    entry = json.loads(db._log_path.read_text(encoding="utf-8").splitlines()[-1])
    logged = entry["identity_update_result"]
    assert len(logged["written"]) == 4 and len(logged["skipped"]) == 2, logged
    assert {x["reason"] for x in logged["skipped"]} == {"names a change that is not in the ledger", "a second record for the same change"}
    print("[check] cycle log: 4 records written, 2 proposals skipped with their reasons")

    # --- atomicity ---------------------------------------------------------------------------------------------------------------------------------------
    db2, _, _, before, failed = run("verify_onto_cycle_flaky.json", flaky_replace=True)
    assert failed is not None
    assert (db2._path.read_bytes(), db2._relations_path.read_bytes()) == before, "a failed commit left a partial write"
    assert not db2.get_identity_records(target_vov_ids=[MICH], limit=None) or len(db2.get_identity_records(target_vov_ids=[MICH])) == 1
    print("[check] a failure inside the commit leaves the database exactly as before (no object update without its record, nor the reverse)")

    for name in ("verify_onto_cycle", "verify_onto_cycle_flaky"):
        p = HERE / f"{name}.json"
        for q in [p, p.with_suffix(".cycles.jsonl"), p.with_name(p.stem + "_relations.json")]:
            q.unlink(missing_ok=True)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
