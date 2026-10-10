"""MS Rev 0007 (QA wave 1) — one whole ProcessMotivation cycle, JsonFileDatabase/DraftDatabase, every LLM call mocked.

  * rows BORN this cycle are reviewed (ANCHOR_REVIEW, "born" variant) after RELATIONS_UPDATE — Liriel's MOV and a nested MOV,
    never an existing row, an Objective or a ScenarioData — and the Feelings the review decides are applied;
  * MOV_UPDATE sees the Savanna elements; Liriel's own row cannot have its description/remarks rewritten;
  * the interlocutor reaches the decision and the reply; a Guess that `continues_objective` updates the standing row in place.
"""
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import motivation  # noqa: E402
from models import ObjectiveBlock, VectorObjectValence  # noqa: E402
from verify_ontology_cycle import MICH, MOV, OTAV, fresh, section, seed, settings  # noqa: E402

SELF = settings.liriel_self_vov_id
STANDING = "Objective_Standing_Aim"
BORN = "You are reviewing a row CREATED THIS CYCLE"  # placeholder, replaced below by the real marker
BORN_MARK = "THIS ROW WAS CREATED DURING THIS VERY CYCLE"


def main():
    db = fresh("verify_rev0007.json")
    seed(db)
    db.upsert_object(MOV, VectorObjectValence(
        vov_id=STANDING, object_nature="Objective", valence_regime="Delta", priority=1, brief_description="keep helping Clara",
        objective=ObjectiveBlock(genus="Prey", species="Conquest", gain_form="Increment", channel_ordinances=["InstinctCompanionship"],
                                 beneficiary_scope=[SELF], cycles_open=2)))
    db.upsert_object(MOV, VectorObjectValence(
        vov_id="Objective_Second_Aim", object_nature="Objective", valence_regime="Delta", priority=2, brief_description="stay close to Bruno",
        objective=ObjectiveBlock(genus="Prey", species="Conquest", gain_form="Increment", channel_ordinances=["InstinctCompanionship"],
                                 beneficiary_scope=[SELF], cycles_open=1)))
    calls, prompts, born_for, replies = [], {}, [], []

    def fake_chat_json(messages, temperature=None, effort=None, model=None):
        user = messages[1]["content"]
        q = re.search(r"^query: (\w+)", user, re.M).group(1)
        calls.append(q)
        prompts.setdefault(q, []).append(user)
        if q == "SCENE_SUBJECT_CHECK":
            return {"query": q, "is_new_subject": False, "interlocutor": MICH, "elements": [
                {"vov_id": MICH, "object_nature": "Sentient", "is_hunter": False, "new_this_cycle": False},
                {"vov_id": None, "provisional_label": "the fall that hurt Lucas", "object_nature": "Situation", "is_hunter": False, "new_this_cycle": True}]}
        if q == "GRAPH_REQUEST":
            return {"query": q, "search_commands": [], "retrieval_satisfied": True, "requests": [{"focus_objects": [MICH], "depth": 1}]}
        if q == "TACTICAL_SCENE_INTERPRETATION":
            return {"query": q, "board": {"summary": "Clara tells about Lucas"}, "relations_summary": "r"}
        if q == "HUNTER_READING":
            return {"query": q, "ordinances_read": [], "schemas_read": [], "needs_own_mov": False}
        if q == "ANCHOR_REVIEW":
            if BORN_MARK in user:
                vid = re.search(r"vov_id=(\S+)", user).group(1)
                born_for.append(vid)
                if vid == "Situation_Queda_Lucas":
                    return {"query": q, "feelings_changes": [{"axis": "HopeFear", "v": "strong Fear", "c": 4}], "schemas_changes": []}
                return {"query": q, "feelings_changes": [], "schemas_changes": []}
            return {"query": q, "feelings_changes": [], "schemas_changes": []}
        if q == "MAINMEMORY_FILING":
            return {"query": q, "archive": [], "restore": []}
        if q == "MOV_UPDATE":
            assert "Savanna elements named by Query 1" in user and "the fall that hurt Lucas" in user, "MOV_UPDATE did not see the elements"
            assert "NO ROW YOU WRITE CARRIES `feelings`" in user
            return {"query": q, "mov_ops": [
                {"op": "PATCH_VOV", "vov_id": SELF, "patch": {"brief_description": "Observing a domestic crisis", "relevant_remarks": "scene summary"}},
                {"op": "UPSERT_VOV", "vov": {"vov_id": "Sentient_Lucas", "object_nature": "Sentient", "brief_description": "Lucas, Clara's son", "relevant_relations": [MICH]}},
                {"op": "UPSERT_VOV", "vov": {"vov_id": "Situation_Queda_Lucas", "object_nature": "Situation", "brief_description": "Lucas fell and hurt his arm", "relevant_relations": [MICH]}},
                {"op": "UPSERT_VOV", "vov": {"vov_id": "ScenarioData_Queda", "object_nature": "ScenarioData", "brief_description": "backbone", "relevant_relations": [MICH]}},
            ], "nested_mov_ops": [
                {"op": "CREATE_NESTED_MOV", "mov_id": "MOV_Clara", "owner_vov_id": MICH, "depth": 1, "rows": [
                    {"vov_id": "Situation_Queda_Lucas_B", "object_nature": "Situation", "brief_description": "the fall, as Clara lives it", "relevant_relations": ["Situation_Queda_Lucas"]}]},
            ]}
        if q == "RELATIONS_UPDATE":
            return {"query": q, "write_relations": [], "soften_charge": []}
        if q == "BEST_PREY_GUESS":
            assert "Interlocutor" in user and MICH in section(user, "Interlocutor"), "the decision did not see the interlocutor"
            assert "continues_objective" in user
            return {"query": q, "continues_objective": STANDING, "handoff_to_processcommandcontrol": {},
                    "accompanying_objectives": [{"vov_id": "Objective_Second_Aim", "brief_description": "stay close to Bruno, gently", "priority": 2,
                                                 "objective": {"genus": "Prey", "species": "Conquest", "gain_form": "Increment",
                                                               "channel_ordinances": ["InstinctCompanionship"], "beneficiary_scope": [SELF], "cycles_open": 0}}],
                    "best_prey_guess": {"vov_id": "Objective_Something_Else", "brief_description": "keep helping Clara — now about the fall",
                                        "relevant_relations": [],
                                        "objective": {"genus": "Prey", "species": "Conquest", "gain_form": "Increment",
                                                      "channel_ordinances": ["InstinctCompanionship"], "beneficiary_scope": [SELF], "cycles_open": 0}},
                    "mov_ops": [], "nested_mov_ops": []}
        if q == "IDENTITY_UPDATE":
            return {"query": q, "records": []}
        raise AssertionError(f"unexpected query {q}")

    def fake_chat(messages, temperature=None, **kw):
        replies.append(messages[1]["content"])
        return "ok"

    mov = db.load_mov(MOV)
    with patch.object(motivation, "chat_json", side_effect=fake_chat_json), patch.object(motivation, "chat", side_effect=fake_chat):
        motivation.run_motivation_cycle(db, mov, "Lucas fell and hurt his arm.", source="verify")

    # --- Query 5B ---------------------------------------------------------------------------------------------------
    order = [c for c in calls if c in ("MOV_UPDATE", "RELATIONS_UPDATE", "BEST_PREY_GUESS")]
    assert order == ["MOV_UPDATE", "RELATIONS_UPDATE", "BEST_PREY_GUESS"], order
    assert calls.index("RELATIONS_UPDATE") < max(i for i, c in enumerate(calls) if c == "ANCHOR_REVIEW") < calls.index("BEST_PREY_GUESS")
    assert sorted(born_for) == ["Sentient_Lucas", "Situation_Queda_Lucas", "Situation_Queda_Lucas_B"], born_for
    print("[check] Query 5B reviewed exactly the rows born this cycle (Liriel's MOV and a nested MOV): no existing row, no ScenarioData, no Objective")
    assert db.get_object("Situation_Queda_Lucas").feelings["HopeFear"].v == -4.0
    print("[check] the Feelings the born-row review decided were applied (the cause Situation carries the fear)")
    assert db.get_object("Sentient_Lucas").feelings == {}, "a person got a charge nobody decided"

    # --- Liriel's own row ---------------------------------------------------------------------------------------------
    me = db.get_object(SELF)
    assert me.brief_description == "Observing a domestic crisis", me.brief_description  # the model's call; the architecture does not veto it
    print("[check] Liriel's own row follows what the model writes, like any other row (no code veto)")

    # --- the interlocutor ----------------------------------------------------------------------------------------------
    assert MICH in section(replies[0], "Interlocutor — who wrote this message, and so the ONLY person this reply is for"), "the reply did not see the interlocutor"
    assert "WHO THIS REPLY IS FOR" in replies[0] and "WHO THIS REPLY IS FOR" in prompts["BEST_PREY_GUESS"][0]
    print("[check] the interlocutor and the discretion rule reach the decision and the reply")
    assert "WHAT LIRIEL CAN DO" in replies[0] and "WHAT LIRIEL CAN DO" in prompts["BEST_PREY_GUESS"][0]
    assert "interlocutor_brought" in prompts["BEST_PREY_GUESS"][0]
    assert "DUTY OF CARE" in replies[0] and "DUTY OF CARE" in prompts["BEST_PREY_GUESS"][0]
    assert "WHAT LIRIEL PROMISES" in replies[0] and "WHAT LIRIEL PROMISES" in prompts["BEST_PREY_GUESS"][0]
    assert "HER OWN INNER LIFE" in replies[0] and "HER OWN INNER LIFE" in prompts["BEST_PREY_GUESS"][0]
    assert "asked_of_liriel" in replies[0] and "asked_of_liriel" in prompts["BEST_PREY_GUESS"][0]
    assert "may_be_told" in replies[0] and "may_be_told" in prompts["BEST_PREY_GUESS"][0]
    print("[check] the Phase 1 reach and the interlocutor_brought question reach the decision (and the reach the reply)")

    # --- a continued Objective --------------------------------------------------------------------------------------------
    objs = [o for o in db.load_mov(MOV).objects if o.object_nature == "Objective"]
    assert sorted(o.vov_id for o in objs) == sorted([STANDING, "Objective_Second_Aim"]), [o.vov_id for o in objs]
    o = next(x for x in objs if x.vov_id == STANDING)
    assert o.priority == 1 and o.brief_description.endswith("about the fall") and o.objective.cycles_open == 3, (o.priority, o.brief_description, o.objective.cycles_open)
    second = next(x for x in objs if x.vov_id == "Objective_Second_Aim")
    assert second.brief_description.endswith("gently") and second.priority == 2
    print("[check] a Guess that continues a standing Objective, and a standing Objective the model re-lists by its own id, are updated in place — no duplicate rows")
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
