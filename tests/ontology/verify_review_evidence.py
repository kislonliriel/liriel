"""Rev 0007 M: AnchorReviewResult flattens `evidence` into `feelings_changes`; the flat shape still passes through."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from models import AnchorReviewResult  # noqa: E402

print("[check] changes written inside evidence become the review's feelings_changes, and the words are kept as report_says")
r = AnchorReviewResult.model_validate({
    "evidence": [
        {"words": "she already had some juice", "feelings_changes": [{"axis": "HopeFear", "v": "neutral", "c": 3, "reason": "crisis over"}]},
        {"words": "she is better today", "feelings_changes": [{"axis": "MirthGloom", "v": "mild Mirth", "c": 3, "reason": "relief"}]},
    ],
})
assert [(c.axis, c.v) for c in r.feelings_changes] == [("HopeFear", "neutral"), ("MirthGloom", "mild Mirth")]
assert r.report_says == "she already had some juice | she is better today" and len(r.evidence) == 2

print("[check] who_feels_it is kept with its entry")
r = AnchorReviewResult.model_validate({"evidence": [{"words": "cold, hunger", "who_feels_it": "Tadeu", "feelings_changes": []},
                                                   {"words": "x", "feelings_changes": []}]})
assert [e.who_feels_it for e in r.evidence] == ["Tadeu", None]

print("[check] `evidence: []` leaves no change, even if a stray top-level feelings_changes is also written")
r = AnchorReviewResult.model_validate({
    "evidence": [],
    "feelings_changes": [{"axis": "HopeFear", "v": "neutral", "c": 3, "reason": "the report focuses on something else"}],
})
assert r.feelings_changes == [] and r.report_says == "nothing"

print("[check] an evidence entry without changes, or malformed entries, are tolerated")
r = AnchorReviewResult.model_validate({"evidence": [{"words": "x"}, "junk", {"feelings_changes": "junk"}, None]})
assert r.feelings_changes == [] and r.report_says == "x"

print("[check] the flat shape (no `evidence`) is untouched — older logs and the mocked cycles")
r = AnchorReviewResult.model_validate({"report_says": "nothing", "feelings_changes": [{"axis": "HopeFear", "v": "neutral", "c": 3}]})
assert len(r.feelings_changes) == 1 and r.evidence is None and r.report_says == "nothing"
r = AnchorReviewResult.model_validate({"feelings_changes": [], "schemas_changes": [{"schema_name": "CharacterEmpathy", "v": "strong positive", "c": 3}]})
assert len(r.schemas_changes) == 1

print("[check] BestPreyGuessResult drops an accompanying Objective with no description instead of failing the cycle")
from models import BestPreyGuessResult  # noqa: E402
r = BestPreyGuessResult.model_validate({
    "best_prey_guess": {"vov_id": "Objective_X", "brief_description": "d", "object_type": "real", "valence_regime": "Delta", "priority": 1},
    "accompanying_objectives": [{"vov_id": "Objective_A"}, {"vov_id": "Objective_B", "brief_description": "ok"}]})
assert [o.vov_id for o in r.accompanying_objectives] == ["Objective_B"]

print("[check] object_nature outside the closed list never reaches the database CHECK: case is put right, a type word becomes object_type, the rest is Thing")
from models import VectorObjectValence  # noqa: E402
def _mk(**kw):
    base = dict(vov_id="X_1", valence_regime="State", brief_description="d"); base.update(kw)
    return VectorObjectValence.model_validate(base)
a = _mk(object_nature="Imagined"); assert (a.object_nature, a.object_type) == ("Thing", "imagined")
assert _mk(object_nature="sentient").object_nature == "Sentient" and _mk(object_nature="Self-Process").object_nature == "Self-Process"
assert _mk(object_nature="Quest").object_nature == "Thing"

print("[check] an evidence entry whose words are the no-evidence token carries no change (the model declaring it has none)")
r = AnchorReviewResult.model_validate({"evidence": [{"words": "nothing", "feelings_changes": [{"axis": "HopeFear", "v": "neutral", "c": 3}]},
                                                   {"words": "Nothing.", "feelings_changes": [{"axis": "MirthGloom", "v": "neutral", "c": 3}]},
                                                   {"words": "she already had some juice", "feelings_changes": [{"axis": "HopeFear", "v": "neutral", "c": 3}]}]})
assert [c.axis for c in r.feelings_changes] == ["HopeFear"] and r.report_says == "she already had some juice" and len(r.evidence) == 1

print("[check] IDENTITY_UPDATE: a declared 'nothing new' drops the records; an answer without `learned` (or with an empty one) keeps them")
from models import IdentityUpdateResult  # noqa: E402
_rec = {"change_ref": "c1", "information": "x"}
assert IdentityUpdateResult.model_validate({"learned": "Nothing new.", "records": [_rec]}).records == []
assert IdentityUpdateResult.model_validate({"learned": "nothing", "records": [_rec]}).records == []
assert len(IdentityUpdateResult.model_validate({"learned": "She is in the 2nd year of high school.", "records": [_rec]}).records) == 1
assert len(IdentityUpdateResult.model_validate({"records": [_rec]}).records) == 1
assert len(IdentityUpdateResult.model_validate({"learned": "", "records": [_rec]}).records) == 1

print("[check] Rev 0007 AV: `touches_this_row` -- a declared 'no' carries no Feeling or Schema change; 'yes' or absent changes nothing about how the answer is read")
_fc = {"axis": "HopeFear", "v": "mild Fear", "c": 3, "reason": "r"}
_sc = {"schema_name": "CharacterEmpathy", "v": "strong positive", "c": 3, "reason": "r"}
for said in ("no", "No", "no — only the rest of the scene", "não", "NO."):
    r = AnchorReviewResult.model_validate({"touches_this_row": said, "evidence": [{"words": "some words", "feelings_changes": [_fc]}], "schemas_changes": [_sc], "missing_cause": "a cause"})
    assert r.feelings_changes == [] and r.schemas_changes == [] and r.report_says == "nothing" and r.evidence == [] and r.missing_cause == "a cause", said
for said in ("yes", "yes — it names her", "nothing", "notably", "", None):
    r = AnchorReviewResult.model_validate({"touches_this_row": said, "evidence": [{"words": "some words", "feelings_changes": [_fc]}], "schemas_changes": [_sc]})
    assert len(r.feelings_changes) == 1 and len(r.schemas_changes) == 1, said
assert len(AnchorReviewResult.model_validate({"feelings_changes": [_fc]}).feelings_changes) == 1  # the flat shape older logs use

print("[check] Rev 0007 AV: the prompt asks it of every row but the owner's own (her row is her own state, judged as before)")
import prompts  # noqa: E402
from models import AnchorTarget, Artifacts, MatrixObjectsValence, ScenarioData, VectorObjectValence  # noqa: E402
_self = VectorObjectValence(vov_id="PCI_X", object_nature="PCI", valence_regime="State", brief_description="Liriel")
_other = VectorObjectValence(vov_id="Situation_Y", object_nature="Situation", valence_regime="State", brief_description="y")
_art = Artifacts(meta_scheme="MS", mov=MatrixObjectsValence(mov_id="M", objects=[_self, _other]), scenario_data=ScenarioData(text="oi"))
_p_other = prompts.build_anchor_review_prompt(_art, AnchorTarget(mov_id="M", vov=_other, owner=_self, is_nested=False))[1]["content"]
_p_self = prompts.build_anchor_review_prompt(_art, AnchorTarget(mov_id="M", vov=_self, owner=_self, is_nested=False))[1]["content"]
assert '"touches_this_row"' in _p_other and "answer `touches_this_row`" in _p_other and "a row the report does not touch keeps what it has" in _p_other
assert "touches_this_row" not in _p_self
assert _p_other.index('"touches_this_row"') < _p_other.index('"evidence"')
print("\nALL CHECKS PASSED")
