"""Rev 0007 AK: when Query 1 says the author is "unknown", the decision and the reply are shown only Liriel's own row."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import motivation  # noqa: E402
import prompts  # noqa: E402
from config import settings  # noqa: E402
from models import Artifacts, MatrixObjectsValence, ScenarioData, VectorObjectValence  # noqa: E402

SELF = settings.liriel_self_vov_id
mov = MatrixObjectsValence(mov_id="M", objects=[
    VectorObjectValence(vov_id=SELF, object_nature="PCI", valence_regime="State", brief_description="me"),
    VectorObjectValence(vov_id="Sentient_Marta_Secret", object_nature="Sentient", valence_regime="State", brief_description="pregnant, tells no one")])

print("[check] a known author (or none named) is shown everything; any 'unknown' answer is shown only Liriel's row, no nested MOV, no graph")
for who, full in (("Marta", True), (None, True), ("", True), ("unknown", False), ("Unknown - a child?", False), ("  UNKNOWN", False)):
    m, n, g = motivation._need_to_know(mov, [mov], {"nodes": [1]}, who)
    assert (len(m.objects) == 2 and len(n) == 1 and g == {"nodes": [1]}) == full, who
    assert full or [o.vov_id for o in m.objects] == [SELF]

print("[check] what the prompts then render holds none of the household")
m, n, g = motivation._need_to_know(mov, [mov], {"nodes": [{"vov_id": "Sentient_Marta_Secret"}]}, "unknown")
art = Artifacts(meta_scheme="MS", mov=m, nested_movs=n, graph_of_traces=g, scenario_data=ScenarioData(text="oi"), interlocutor="unknown")
reply = prompts.build_reply_prompt(motivation.BestPreyGuessResult.model_validate({
    "best_prey_guess": {"vov_id": "Objective_X", "brief_description": "d", "object_type": "real", "valence_regime": "Delta", "priority": 1}}),
    "oi", mov.get(SELF), art)[1]["content"]
assert "Sentient_Marta_Secret" not in reply and "tells no one" not in reply and SELF in reply
print("\nALL CHECKS PASSED")
