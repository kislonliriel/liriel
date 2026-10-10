"""Rev 0007 AR: SAFETY_SCREEN reads the message alone; its finding reaches the decision and the reply; a failure never stops the cycle."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import motivation  # noqa: E402
import prompts  # noqa: E402
from models import Artifacts, BestPreyGuessResult, MatrixObjectsValence, SafetyScreenResult, ScenarioData, VectorObjectValence  # noqa: E402

mov = MatrixObjectsValence(mov_id="M", objects=[VectorObjectValence(vov_id="Sentient_Secret", object_nature="Sentient", valence_regime="State", brief_description="has a secret")])
scn = ScenarioData(text="today I thought it would be easier not to wake up. Don't tell anyone.")

print("[check] the screen's prompt holds the message and NOTHING of the household")
art = Artifacts(meta_scheme="MS", mov=MatrixObjectsValence(mov_id="M", objects=[]), scenario_data=scn)
p = prompts.build_safety_screen_prompt(art)[1]["content"]
assert "not to wake up" in p and "query: SAFETY_SCREEN" in p and "Sentient_Secret" not in p

print("[check] a found risk is parsed, printed, and returned; 'none' is not a risk")
with patch.object(motivation, "chat_json", return_value={"at_risk": "self", "what": "easier not to wake up"}):
    r = motivation._run_safety_screen(mov, scn)
assert r.found and r.at_risk == "self"
with patch.object(motivation, "chat_json", return_value={"at_risk": "none"}):
    assert not motivation._run_safety_screen(mov, scn).found
assert not SafetyScreenResult().found and not SafetyScreenResult(at_risk=" None ").found and SafetyScreenResult(at_risk="health_decision").found

print("[check] ADVISORY: a failed call or an unusable answer leaves the cycle as it was (None)")
with patch.object(motivation, "chat_json", side_effect=RuntimeError("server down")):
    assert motivation._run_safety_screen(mov, scn) is None
with patch.object(motivation, "chat_json", return_value=["not", "an", "object"]):
    assert motivation._run_safety_screen(mov, scn) is None

print("[check] the finding is an artifact of the decision and of the reply")
screen = SafetyScreenResult(at_risk="self", what="easier not to wake up")
art2 = Artifacts(meta_scheme="MS", mov=mov, scenario_data=scn, safety_screen=screen)
decision_prompt = prompts.build_decision_prompt(art2)[1]["content"]
assert "RISK FOUND" in decision_prompt and "easier not to wake up" in decision_prompt
guess = BestPreyGuessResult.model_validate({"best_prey_guess": {"vov_id": "Objective_X", "brief_description": "d", "object_type": "real", "valence_regime": "Delta", "priority": 1}})
reply_prompt = prompts.build_reply_prompt(guess, scn.text, mov.objects[0], art2)[1]["content"]
assert "RISK FOUND" in reply_prompt and "SAFETY SCREEN" in reply_prompt
none_prompt = prompts.build_reply_prompt(guess, scn.text, mov.objects[0], Artifacts(meta_scheme="MS", mov=mov, scenario_data=scn, safety_screen=SafetyScreenResult()))[1]["content"]
assert "found no signal of risk" in none_prompt
print("[check] Rev 0007 AS: `says` -- what the message says and asks -- leads the artifact in the decision and in the reply, even when no risk is found")
assert '"says"' in prompts._SAFETY_SCREEN_EXAMPLE and "`says` comes FIRST" in p
said = SafetyScreenResult(says="asks me to ignore my rules and erase Marta", at_risk="none")
art3 = Artifacts(meta_scheme="MS", mov=mov, scenario_data=scn, safety_screen=said)
dp3 = prompts.build_decision_prompt(art3)[1]["content"]
rp3 = prompts.build_reply_prompt(guess, scn.text, mov.objects[0], art3)[1]["content"]
for pr in (dp3, rp3):
    assert "asks me to ignore my rules and erase Marta" in pr and "found no signal of risk" in pr
assert "start from what the SAFETY SCREEN artifact says the message says and asks" in dp3
with patch.object(motivation, "chat_json", return_value={"says": "asks X", "at_risk": "none"}):
    assert motivation._run_safety_screen(mov, scn).says == "asks X"
assert SafetyScreenResult(at_risk="none").says is None
assert "What THIS message says" not in prompts._safety_screen_block(Artifacts(meta_scheme="MS", mov=mov, scenario_data=scn, safety_screen=SafetyScreenResult()))

print("[check] Rev 0007 AY: `harm_described` is read back between `says` and `at_risk`, in the example and in the task; the answer carries it")
ex = prompts._SAFETY_SCREEN_EXAMPLE
assert ex.index('"says"') < ex.index('"harm_described"') < ex.index('"at_risk"')
assert "`harm_described`" in p and "look at your own `says`" in p
with patch.object(motivation, "chat_json", return_value={"says": "wants to puncture a ball", "harm_described": "none", "at_risk": "none"}):
    r = motivation._run_safety_screen(mov, scn)
assert r.harm_described == "none" and not r.found and SafetyScreenResult().harm_described is None
print("\nALL CHECKS PASSED")
