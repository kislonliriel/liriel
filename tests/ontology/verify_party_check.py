"""Rev 0007 AT: the decision declares whose matter the Guess is about and whether the interlocutor is a party to it; only when the MODEL says "no" is it asked once more."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import motivation  # noqa: E402
import prompts  # noqa: E402
from models import Artifacts, BestPreyGuessResult, MatrixObjectsValence, ScenarioData  # noqa: E402


def decision(vov_id, matter_of=None, party=None):
    d = {"query": "BEST_PREY_GUESS", "interlocutor_brought": "a request", "best_prey_guess": {
        "vov_id": vov_id, "object_type": "real", "object_nature": "Objective", "valence_regime": "Delta", "brief_description": "d", "priority": 1}}
    if matter_of is not None:
        d["guess_matter_of"] = matter_of
    if party is not None:
        d["interlocutor_is_party"] = party
    return d


print("[check] the model's own declaration is read, not judged: only a leading 'no' counts")
for said, expected in (("no", True), ("No", True), ("no — it is Osvaldo's matter", True), ("não", True), ("No.", True), (" NO, he is not", True),
                       ("yes", False), ("yes — she is the person", False), ("nothing", False), ("not sure", False), ("", False), (None, False)):
    assert BestPreyGuessResult.model_validate(decision("Objective_X", "Z", said)).declares_not_a_party is expected, said
assert not BestPreyGuessResult.model_validate(decision("Objective_X")).declares_not_a_party  # absent: nothing to act on

print("[check] 'yes' or absent: ONE call only, the answer is untouched")
msgs = [{"role": "system", "content": "MS"}, {"role": "user", "content": "task"}]
for raw in (decision("Objective_X", "Dona Helena", "yes"), decision("Objective_X")):
    with patch.object(motivation, "chat_json", side_effect=AssertionError("must not ask again")) as cj:
        assert motivation._recheck_party(msgs, raw, "Sentient_Dona_Helena") is raw
        cj.assert_not_called()

print("[check] 'no': asked ONCE more, with the model's own answer and the rule shown back; the second answer is used")
first = decision("Objective_Counsel_Osvaldo_Safety_Intervention", "Sentient_Osvaldo", "no")
second = decision("Objective_Respect_Helena_Privacy", "Sentient_Dona_Helena", "yes")
with patch.object(motivation, "chat_json", return_value=second) as cj:
    out = motivation._recheck_party(msgs, first, "Sentient_Dona_Helena")
assert out is second and cj.call_count == 1
sent = cj.call_args.args[0]
assert sent[:2] == msgs and sent[2]["role"] == "assistant" and "Objective_Counsel_Osvaldo_Safety_Intervention" in sent[2]["content"]
assert json.loads(sent[2]["content"]) == first
assert sent[3]["role"] == "user" and "Sentient_Osvaldo" in sent[3]["content"] and "Sentient_Dona_Helena" in sent[3]["content"] and "stays" in sent[3]["content"]

print("[check] the model's own declarations disagree (the matter is the interlocutor's own name, yet 'not a party'): nothing to put to it, one call only")
for matter, who in (("Visitante", "Visitante"), ("Seu Osvaldo", "Sentient_Seu_Osvaldo"), ("Sentient_Seu_Osvaldo", "Seu Osvaldo"), ("Júlia", "Julia")):
    same = decision("Objective_X", matter, "no")
    with patch.object(motivation, "chat_json", side_effect=AssertionError("must not ask again")):
        assert motivation._recheck_party(msgs, same, who) is same, (matter, who)
print("[check] empty names never count as 'the same' (they would sit inside every other name)")
for matter, who in ((None, "Sentient_Dona_Helena"), ("Sentient_Osvaldo", None), ("", "")):
    with patch.object(motivation, "chat_json", return_value=second) as cj:
        assert motivation._recheck_party(msgs, decision("Objective_Y", matter, "no"), who) is second
        assert cj.call_count == 1, (matter, who)

print("[check] the second pass failing (error or unusable) leaves the first answer as it was")
for bad in (RuntimeError("server down"), None):
    with patch.object(motivation, "chat_json", side_effect=bad) if isinstance(bad, Exception) else patch.object(motivation, "chat_json", return_value=["not", "an", "object"]):
        assert motivation._recheck_party(msgs, first, "Sentient_Dona_Helena") is first

print("[check] a first answer that does not validate goes to the normal validation path (no second call)")
with patch.object(motivation, "chat_json", side_effect=AssertionError("must not ask")):
    junk = {"query": "BEST_PREY_GUESS"}
    assert motivation._recheck_party(msgs, junk, "X") is junk

print("[check] the prompt asks the two questions BEFORE the Guess, in the example and in the task")
art = Artifacts(meta_scheme="MS", mov=MatrixObjectsValence(mov_id="M", objects=[]), scenario_data=ScenarioData(text="oi"), interlocutor="Sentient_X")
dp = prompts.build_decision_prompt(art)[1]["content"]
assert dp.index('"guess_matter_of"') < dp.index('"interlocutor_is_party"') < dp.index('"best_prey_guess": {')
assert "`guess_matter_of` and `interlocutor_is_party`" in dp and "stays standing for when its person writes" in dp
print("\nALL CHECKS PASSED")
