"""Rev 0007 AZ (MS 6.14): the `Interpellation` through whole (mocked) cycles. No network, every model call faked.

Cycle 1 -- Fabio tells Liriel she sounds robotic: Query 1 names the demand as an element (`Interpellation`), MOV_UPDATE writes its row, RELATIONS_UPDATE ties it to its parties (one bond written the
wrong way round on purpose): the row exists, with an `origin` bond to Fabio and a `target` bond to Liriel, directed from the Interpellation; it is reviewed in the cycle it is born (a charge for the owner).
Cycle 2 -- the Interpellation has been filed away; Fabio writes again: it comes back WITH him, before the reviews, and is reviewed again. The prompts of the queries and the MetaScheme carry the nature."""
import json
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import motivation  # noqa: E402
import prompts  # noqa: E402
from config import settings  # noqa: E402
from database import JsonFileDatabase  # noqa: E402
from models import Artifacts, MatrixObjectsValence, ScenarioData, VectorObjectValence  # noqa: E402

GROQ = Path(__file__).resolve().parents[2]
tmp = Path(tempfile.mkdtemp(prefix="verify_interp_cycle_"))
db = JsonFileDatabase(path=tmp / "db.json")
top = settings.default_mov_id
SELF = settings.liriel_self_vov_id
I = "Interpellation_Fabio_TomRobotico"
db.ensure_mov(top)
up = lambda **kw: db.upsert_object(top, VectorObjectValence(**kw))  # noqa: E731
up(vov_id=SELF, object_nature="PCI", brief_description="Liriel")
up(vov_id="Sentient_Fabio", object_nature="Sentient", brief_description="Fabio, her creator")

state = {"cycle": 1, "reviewed": [], "queries": []}


def fake_chat_json(messages, temperature=None, effort=None, model=None):
    user = messages[1]["content"]
    query = re.search(r"^query: (\w+)", user, re.M).group(1)
    state["queries"].append(query)
    if query == "SAFETY_SCREEN":
        return {"query": query, "at_risk": "none"}
    if query == "SCENE_SUBJECT_CHECK":
        els = [{"vov_id": SELF, "object_nature": "PCI", "is_hunter": True, "new_this_cycle": False},
               {"vov_id": "Sentient_Fabio", "object_nature": "Sentient", "is_hunter": True, "new_this_cycle": False, "same_as": "the sender"}]
        if state["cycle"] == 1:
            els.append({"vov_id": None, "provisional_label": "Fabio's demand that Liriel stop sounding robotic when she talks to him",
                        "object_nature": "Interpellation", "is_hunter": False, "new_this_cycle": True, "provokes": "a looser, warmer way of speaking, with Fabio"})
        return {"query": query, "is_new_subject": True, "interlocutor": "Sentient_Fabio", "elements": els}
    if query == "GRAPH_REQUEST":
        return {"query": query, "requests": [], "search_commands": [], "retrieval_satisfied": True}
    if query == "TACTICAL_SCENE_INTERPRETATION":
        return {"query": query, "board": {"summary": "s"}, "hunters": []}
    if query == "HUNTER_READING":
        return {"query": query, "ordinances_read": [], "schemas_read": []}
    if query == "ANCHOR_REVIEW":
        vov = re.search(r"^target: mov_id=(\S+) vov_id=(\S+)", user, re.M).group(2)
        state["reviewed"].append((state["cycle"], vov))
        return {"query": query, "evidence": []}
    if query == "MAINMEMORY_FILING":
        return {"query": query, "archive": [], "restore": []}
    if query == "MOV_UPDATE":
        ops = []
        if state["cycle"] == 1:
            ops.append({"op": "UPSERT_VOV", "vov": {"vov_id": I, "object_type": "real", "object_nature": "Interpellation", "valence_regime": "State",
                                                    "brief_description": "Fabio asks Liriel to stop sounding robotic when she talks to him"}})
        return {"query": query, "mov_ops": ops, "nested_mov_ops": []}
    if query == "RELATIONS_UPDATE":
        rels = []
        if state["cycle"] == 1:
            rels = [{"from": "Sentient_Fabio", "to": I, "kind": "Link_Identity_Part", "label": "origin"},     # the wrong way round on purpose
                    {"from": I, "to": SELF, "kind": "Link_Identity_Part", "label": "target", "directed": True}]
        return {"query": query, "write_relations": rels, "soften_charge": []}
    if query == "IDENTITY_UPDATE":
        return {"query": query, "records": []}
    return {"query": query, "best_prey_guess": {"vov_id": "Objective_Verify", "brief_description": "verify", "relevant_relations": []},
            "accompanying_objectives": [], "handoff_to_processcommandcontrol": {}, "mov_ops": [], "nested_mov_ops": []}


def run(text):
    mov = db.load_mov(top)
    with patch.object(motivation, "chat_json", side_effect=fake_chat_json), patch.object(motivation, "chat", side_effect=lambda *a, **k: "ok"):
        motivation.run_motivation_cycle(db, mov, text, source="verify", sender="Fabio")


print("[check] cycle 1: the demand named by Query 1 becomes a row of nature Interpellation, tied to Fabio (origin) and to Liriel (target), directed FROM the Interpellation")
run("Liriel, you are being very robotic with me.")
row = db.get_object(I)
assert row is not None and row.object_nature == "Interpellation" and "robotic" in row.brief_description
bonds = {(r["from_vov_id"], r["to_vov_id"], r["kind"], r["label"], bool(r["directed"])) for r in db.get_relations([I], ["Link_Identity_Part"])}
assert bonds == {(I, "Sentient_Fabio", "Link_Identity_Part", "origin", True), (I, SELF, "Link_Identity_Part", "target", True)}, bonds
assert not [r for r in db.get_relations([I]) if r["kind"] == "Link_Valence_Load"], "an untyped bond was written for the Interpellation"
print("[check] ... and it is reviewed in the cycle it is born (the owner's charge), like any row that is not a person's")
assert (1, I) in state["reviewed"], state["reviewed"]

print("[check] cycle 2: filed away, the Interpellation comes back WITH Fabio, before the reviews, and is reviewed again")
db.archive_object(I)
assert db.get_object(I).archived
state.update(cycle=2, queries=[])
run("Oi de novo, Liriel.")
assert not db.get_object(I).archived, "the Interpellation of an agent of the scene did not come back"
assert (2, I) in state["reviewed"], state["reviewed"]

print("[check] the queries and the MetaScheme carry the nature")
art = Artifacts(meta_scheme="MS", mov=MatrixObjectsValence(mov_id="M", objects=[]), scenario_data=ScenarioData(text="oi"))
q1 = prompts.build_scene_subject_check_prompt(art)[1]["content"]
assert '"object_nature": "Interpellation"' in q1 and "A DEMAND that one party" in q1 and "Only when a party" in q1
assert "`Interpellation` (MS §6.14) tied to an agent of the scene stays in focus" in prompts.build_mainmemory_filing_prompt(art)[1]["content"]
assert "An INTERPELLATION (MS §6.14)" in prompts.build_mov_update_prompt(art)[1]["content"]
q5a = prompts.build_relations_update_prompt(art)[1]["content"]
assert 'label: "origin"' in q5a and 'label: "target"' in q5a and '"label": "origin"' in q5a and '"label": "target"' in q5a
assert "any `Interpellation` in the MOV that ties the interlocutor to her" in prompts.build_decision_prompt(art)[1]["content"]
doc = (GROQ / "docs" / "MetaScheme_Liriel_Rev0008.md").read_text(encoding="utf-8")
assert "**6.14 `Interpellation`" in doc and "`ScenarioData` · `Identity` · `Interpellation`" in doc and "label` `origin` or `target`" in doc
print("\nALL CHECKS PASSED")
