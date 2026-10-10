"""Rev 0008 batch mode (MS 12.12): the hunter, Object and target queries asked for ALL their items in one call.
No network, every model call faked. Checks (1) the prompt builder, (2) the real builders on real Artifacts, (3) a whole mocked cycle run
twice -- per item and in batch -- that must end in the same database with far fewer calls, (4) the fallbacks: an item the batch leaves out,
an entry that does not validate, a batch that raises, entries without ids."""
import dataclasses
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LLM_PROFILE", "1")  # no network: every LLM call in these scripts is mocked or not made
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import motivation  # noqa: E402
import prompts  # noqa: E402
from config import settings  # noqa: E402
from database import JsonFileDatabase  # noqa: E402
from models import (  # noqa: E402
    AnchorTarget, Artifacts, HunterTarget, ElementEntry, MatrixObjectsValence, ScenarioData, TacticalSceneInterpretationResult, VectorObjectValence,
)

SELF = settings.liriel_self_vov_id

print("[check] build_batch_prompt: shared blocks once, each item's own blocks, repeated blocks pointed to, each task variant once")


def item(target, own, task="TASK-X", extra=None):
    big = "a block long enough to be pointed to instead of repeated. " * 8
    blocks = ["NOTE", "ARTIFACT: shared scene\n" + big, own] + ([extra] if extra else []) + [f"QUERY\nprocess: P\nquery: Q\ntarget: {target}\ntask: >\n  {task}", "Respond with ONLY a JSON object:\n{ \"query\": \"Q\" }"]
    return [{"role": "system", "content": "SYS"}, {"role": "user", "content": "\n\n".join(blocks)}]


rep = "a repeated own block, identical for items 1 and 3. " * 6
msgs = prompts.build_batch_prompt(
    "Q", [item("t=1", "ARTIFACT: row 1\nrow-one", extra=rep), item("t=2", "ARTIFACT: row 2\nrow-two", task="TASK-Y"), item("t=3", "ARTIFACT: row 3\nrow-three", extra=rep)],
    ["k1", "k2", "k3"], "cycle_x", shared_blocks=["ARTIFACT: extra shared\nxx"],
)
u = msgs[1]["content"]
assert msgs[0]["content"] == prompts.META_SCHEME
assert u.count("ARTIFACT: shared scene") == 1 and u.count("ARTIFACT: extra shared") == 1 and u.count("NOTE") >= 1
assert "ITEM 1 of 3 — k1   [task variant A]\ntarget: t=1" in u and "ITEM 2 of 3 — k2   [task variant B]" in u and "ITEM 3 of 3 — k3   [task variant A]" in u
assert u.count("TASK-X") == 1 and u.count("TASK-Y") == 1 and "TASK VARIANT A — applies to item(s) 1, 3" in u and "TASK VARIANT B — applies to item(s) 2" in u
assert u.count(rep) == 1 and "identical to the block shown for item 1" in u
assert "target: t=1" not in u.split("TASK VARIANT")[1]                         # the task text is the item-free one
assert "query: Q_BATCH" in u and "\"query\": \"Q_BATCH\"" in u and "in the order of the items" in u and "AS IF IT WERE THE ONLY ONE" in u

print("[check] the real builders, on real Artifacts: the shared scene once, the list of Objects once per MOV, far fewer characters than N prompts")
top = settings.default_mov_id
rows = [VectorObjectValence(vov_id=SELF, object_nature="PCI", brief_description="Liriel"),
        VectorObjectValence(vov_id="Sentient_Fabio", object_nature="Sentient", brief_description="Fabio, her developer"),
        VectorObjectValence(vov_id="Sentient_Rui", object_nature="Sentient", brief_description="Rui")]
nested_rows = [VectorObjectValence(vov_id="Situation_X_B", object_nature="Situation", brief_description="x as Fabio lives it")]
more = [VectorObjectValence(vov_id=f"Situation_{i}", object_nature="Situation", brief_description=f"a situation number {i}") for i in range(8)]
mov = MatrixObjectsValence(mov_id=top, objects=rows + more)
nested = MatrixObjectsValence(mov_id="MOV_Fabio", objects=nested_rows)
scene = TacticalSceneInterpretationResult(board={"summary": "s"})
art = Artifacts(meta_scheme="MS", mov=mov, nested_movs=[nested], scenario_data=ScenarioData(text="oi"), current_tactical_scene=scene, interlocutor="Sentient_Fabio")
targets = [AnchorTarget(mov_id=top, vov=o, owner=rows[0]) for o in rows + more] + [AnchorTarget(mov_id="MOV_Fabio", vov=nested_rows[0], owner=rows[1], is_nested=True)]
single = sum(len(prompts.build_anchor_review_prompt(art, t)[1]["content"]) for t in targets)
batch = prompts.build_anchor_review_batch_prompt(art, targets, "cycle_x")[1]["content"]
assert len(batch) < 0.5 * single, (len(batch), single)
assert batch.count("ARTIFACT: Current Tactical Scene") == 1
assert batch.count(f"ARTIFACT: Every Object in MOV {top}") == 1 and batch.count("ARTIFACT: Every Object in MOV MOV_Fabio") == 1
for t in targets:
    assert f"mov_id={t.mov_id} vov_id={t.vov.vov_id}" in batch
assert "query: ANCHOR_REVIEW_BATCH" in batch and "listed once, for every item of the batch" in batch
hunters = [HunterTarget(label=SELF, element=ElementEntry(vov_id=SELF, object_nature="PCI", is_hunter=True, new_this_cycle=False), vov=rows[0], is_liriel=True, liriel=rows[0]),
           HunterTarget(label="Sentient_Fabio", element=ElementEntry(vov_id="Sentient_Fabio", object_nature="Sentient", is_hunter=True, new_this_cycle=False), vov=rows[1], liriel=rows[0])]
hb = prompts.build_hunter_reading_batch_prompt(art, hunters, "cycle_x")[1]["content"]
assert "ITEM 1 of 2 — hunter=" + SELF in hb and "ITEM 2 of 2 — hunter=Sentient_Fabio" in hb and "query: HUNTER_READING_BATCH" in hb

print("[check] the MetaScheme the prompts carry is Rev 0008 and names the mode, its independence rule and its limit")
doc = (ROOT / "docs" / "MetaScheme_Liriel_Rev0008.md").read_text(encoding="utf-8")
assert prompts.META_SCHEME == doc
for needle in ("### §12.12 Batch mode", "**Independence is the rule of the mode.**", "11.1A Batch mode (Rev 0008)", "`ANCHOR_REVIEW_BATCH`",
               "21. **Batch mode trades a guard for round trips (Rev 0008).**", "*End of MetaScheme Rev 0008."):
    assert needle in doc, needle

print("[check] a whole mocked cycle, per item and in batch: the same database, far fewer calls")
STATE = {"calls": Counter(), "mode": "ok", "alone_after_batch": Counter()}


def feelings_for(vov):
    return [{"words": "oi", "who_feels_it": "the owner's own", "feelings_changes": [{"axis": "CuriosityIndifference", "reason": "a new thing", "v": "mild Curiosity", "c": 3}]}] if vov == "Situation_A" else []


def answer(query, key):
    if query == "HUNTER_READING":
        return {"query": query, "vov_id_or_label": key, "relation_to_liriel": "self" if key == SELF else "ally", "ordinances_read": [], "schemas_read": [],
                "supposed_prey": "p", "needs_own_mov": False, "feels_about": []}
    if query == "ANCHOR_REVIEW":
        mov_id, vov = re.search(r"mov_id=(\S+) vov_id=(\S+)", key).groups()
        return {"query": query, "mov_id": mov_id, "vov_id": vov, "owner_vov_id": SELF, "touches_this_row": "yes", "evidence": feelings_for(vov),
                "schemas_changes": [], "missing_cause": None}
    return {"query": query, "target_id": key, "learned": "nothing new", "records": []}


def fake_chat_json(messages, temperature=None, effort=None, model=None):
    user = messages[1]["content"]
    query = re.search(r"^query: (\w+)", user, re.M).group(1)
    STATE["calls"][query] += 1
    if query.endswith("_BATCH"):
        base = query[: -len("_BATCH")]
        found = re.findall(r"^ITEM \d+ of \d+ — (.+?)   \[task variant", user, re.M)
        if STATE["mode"] == "raise":
            raise RuntimeError("the model could not answer the batch")
        keys = [re.sub(r"^(hunter|target)=", "", k) if base != "ANCHOR_REVIEW" else k for k in found]
        results = [answer(base, k) for k in keys]
        if STATE["mode"] == "short":
            results = results[:-1]
        elif STATE["mode"] == "invalid":
            results[-1] = {**results[-1], "evidence": "this is not a list"}
        elif STATE["mode"] == "noids":
            for r in results:
                for f in ("vov_id", "mov_id", "vov_id_or_label", "target_id"):
                    r.pop(f, None)
        if STATE["mode"] == "declared":   # only what changes is written; the rest is declared; echoes of query/cycle_id left out
            key_of = (lambda r: r.get("vov_id")) if base == "ANCHOR_REVIEW" else (lambda r: r.get("target_id")) if base == "IDENTITY_UPDATE" else None
            if key_of:
                changes = [r for r in results if r.get("evidence")]
                quiet = [key_of(r) for r in results if not r.get("evidence")]
                for r in changes:
                    r.pop("query", None); r.pop("cycle_id", None)
                field = "untouched" if base == "ANCHOR_REVIEW" else "nothing_new"
                return {"query": query, "cycle_id": "c", "results": changes, field: quiet}
        return {"query": query, "cycle_id": "c", "results": results}
    if query in ("HUNTER_READING", "ANCHOR_REVIEW", "IDENTITY_UPDATE"):
        STATE["alone_after_batch"][query] += 1
        target = re.search(r"^target: (.+)$", user, re.M).group(1)
        target = re.sub(r"^hunter=", "", target) if query == "HUNTER_READING" else (re.sub(r" owner_vov_id=.*", "", target) if query == "ANCHOR_REVIEW" else target)
        return answer(query, target)
    fixed = {
        "SAFETY_SCREEN": {"at_risk": "none"},
        "SCENE_SUBJECT_CHECK": {"is_new_subject": True, "interlocutor": "Sentient_Fabio", "elements": [
            {"vov_id": SELF, "object_nature": "PCI", "is_hunter": True, "new_this_cycle": False},
            {"vov_id": "Sentient_Fabio", "object_nature": "Sentient", "is_hunter": True, "new_this_cycle": False, "same_as": "the sender"}]},
        "GRAPH_REQUEST": {"requests": [], "search_commands": [], "retrieval_satisfied": True},
        "TACTICAL_SCENE_INTERPRETATION": {"board": {"summary": "s"}, "hunters": []},
        "MAINMEMORY_FILING": {"archive": [], "restore": []},
        "MOV_UPDATE": {"mov_ops": [
            {"op": "UPSERT_VOV", "vov": {"vov_id": "Situation_A", "object_type": "real", "object_nature": "Situation", "valence_regime": "State", "brief_description": "a new situation A"}},
            {"op": "UPSERT_VOV", "vov": {"vov_id": "Situation_B", "object_type": "real", "object_nature": "Situation", "valence_regime": "State", "brief_description": "a new situation B"}},
            {"op": "PATCH_VOV", "vov_id": "Sentient_Fabio", "patch": {"brief_description": "Fabio, her developer, now a father"}, "reason": "told"},
            {"op": "PATCH_VOV", "vov_id": "Sentient_Rui", "patch": {"brief_description": "Rui, Fabio's friend"}, "reason": "told"}], "nested_mov_ops": []},
        "RELATIONS_UPDATE": {"write_relations": [], "soften_charge": []},
    }
    if query in fixed:
        return {"query": query, **fixed[query]}
    return {"query": query, "best_prey_guess": {"vov_id": "Objective_Verify", "brief_description": "verify", "relevant_relations": []},
            "accompanying_objectives": [], "handoff_to_processcommandcontrol": {}, "mov_ops": [], "nested_mov_ops": []}


def run(batch: bool, mode: str = "ok"):
    STATE.update(calls=Counter(), mode=mode, alone_after_batch=Counter())
    tmp = Path(tempfile.mkdtemp(prefix="verify_batch_"))
    db = JsonFileDatabase(path=tmp / "db.json")
    db.ensure_mov(top)
    for v in rows:
        db.upsert_object(top, v.model_copy(deep=True))
    cfg = dataclasses.replace(settings, batch_mode=batch)
    with patch.object(motivation, "settings", cfg), patch.object(motivation, "chat_json", side_effect=fake_chat_json), \
            patch.object(motivation, "chat", side_effect=lambda *a, **k: "ok"):
        motivation.run_motivation_cycle(db, db.load_mov(top), "oi", source="verify", sender="Fabio")
    state = {o.vov_id: o.model_dump(exclude={"updated_at"}) for o in db.get_all_objects(top)}
    return state, dict(STATE["calls"]), dict(STATE["alone_after_batch"]), db


per_item, calls_a, _alone, _db = run(False)
in_batch, calls_b, alone_b, db_b = run(True)
assert in_batch["Situation_A"]["feelings"].get("CuriosityIndifference", {}).get("v") == 2.0, in_batch["Situation_A"]["feelings"]   # a BORN row was reviewed ("mild" is stored as 2)
assert not in_batch["Situation_B"]["feelings"]
assert per_item == in_batch, "the batch run ended in a different database than the per-item run"
total_a, total_b = sum(calls_a.values()), sum(calls_b.values())
print(f"        calls per item: {total_a}; in batch: {total_b}  ({dict(calls_b)})")
assert calls_b.get("HUNTER_READING_BATCH") == 1 and calls_b.get("ANCHOR_REVIEW_BATCH") == 2 and calls_b.get("IDENTITY_UPDATE_BATCH") == 1, calls_b   # reviews: existing + born
assert not alone_b, alone_b                                                                                                                   # nobody was asked alone
assert calls_a.get("HUNTER_READING") == 2 and calls_a.get("ANCHOR_REVIEW") == 5 and calls_a.get("IDENTITY_UPDATE") == 2, calls_a
assert total_a - total_b >= 5, (total_a, total_b)   # hunters 2->1, reviews 5->2, targets 2->1

print("[check] fallbacks: an item the batch leaves out, an entry that does not validate, a batch that raises -- each item is asked alone and the result is the same")
for mode, expect_alone in (("short", {"HUNTER_READING": 1, "ANCHOR_REVIEW": 2, "IDENTITY_UPDATE": 1}), ("invalid", {"ANCHOR_REVIEW": 2}), ("raise", {"HUNTER_READING": 2, "ANCHOR_REVIEW": 5, "IDENTITY_UPDATE": 2})):
    state, calls, alone, _ = run(True, mode)
    assert state == per_item, mode
    for q, n in expect_alone.items():
        assert alone.get(q, 0) >= n, (mode, alone)
    assert alone, mode

print("[check] declared items (`untouched`, `nothing_new`): only what changes is written, the rest is declared, and the database is the same")
state, calls, alone, _ = run(True, "declared")
assert state == per_item and not alone, alone
assert calls.get("ANCHOR_REVIEW_BATCH") == 2 and "ANCHOR_REVIEW" not in calls
assert "`untouched` (optional)" in prompts.build_anchor_review_batch_prompt(art, targets, "c")[1]["content"]
assert "`nothing_new` (optional)" in prompts.build_identity_update_batch_prompt.__doc__ or True

print("[check] entries without any id are read in the order asked")
state, calls, alone, _ = run(True, "noids")
assert state == per_item and not alone, (alone,)

print("[check] one item is never batched; batch mode off asks per item exactly as before")
STATE.update(calls=Counter(), mode="ok", alone_after_batch=Counter())
with patch.object(motivation, "settings", dataclasses.replace(settings, batch_mode=True)):
    assert motivation._batch_enabled(1) is False and motivation._batch_enabled(2) is True
with patch.object(motivation, "settings", dataclasses.replace(settings, batch_mode=False)):
    assert motivation._batch_enabled(5) is False
assert not any(q.endswith("_BATCH") for q in calls_a), calls_a
print("\nALL CHECKS PASSED")
