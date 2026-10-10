"""Rev 0007 V: the channel's `sender` reaches Query 1 as a fact; without it Query 1 is told to read the author from the text."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import prompts  # noqa: E402
from models import Artifacts, MatrixObjectsValence, ScenarioData  # noqa: E402

mov = MatrixObjectsValence(mov_id="MOV_T", objects=[])


def q1(sender):
    art = Artifacts(meta_scheme="MS", mov=mov, scenario_data=ScenarioData(text="oi", source="telegram", sender=sender))
    return prompts.build_scene_subject_check_prompt(art)[1]["content"]


print("[check] with a sender, Query 1 is told it is a fact of the channel and to name that person")
p = q1("Marta")
assert "sender (a fact of the channel, not a guess): Marta" in p and "If the ScenarioData names a `sender`" in p

print("[check] without one, it is told the channel does not say")
p = q1(None)
assert "the channel does not say who wrote it" in p and "a fact of the channel" not in p

print("[check] Query 1 is handed the archived candidates the automatic search matched (Rev 0007 W)")
art = Artifacts(meta_scheme="MS", mov=mov, scenario_data=ScenarioData(text="oi"),
                archived_candidates=[{"vov_id": "Sentient_Gone", "object_nature": "Sentient", "brief_description": "a colleague"}])
p = prompts.build_scene_subject_check_prompt(art)[1]["content"]
assert "Sentient_Gone" in p and "the ARCHIVED candidates listed" in p and "every archived person, animal and group" in p
assert "[]  # none matched" in q1(None)

print("[check] ScenarioData defaults to no sender (every existing caller keeps its behaviour)")
assert ScenarioData(text="x").sender is None
print("\nALL CHECKS PASSED")
