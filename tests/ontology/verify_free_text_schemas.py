"""Rev 0007 AX: the review can write the FREE-TEXT Schemas (BodyFeatures, MentalDisorders, MindVices, PersonalityNaturalAbility, Culture) as text; a Schema change on a row that is
not an agent is not carried out; the prompt asks for the phrases of agent rows only."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import motivation  # noqa: E402
import prompts  # noqa: E402
from config import settings  # noqa: E402
from database import JsonFileDatabase  # noqa: E402
from models import AnchorReviewResult, AnchorTarget, Artifacts, MatrixObjectsValence, ScenarioData, VectorObjectValence  # noqa: E402

db = JsonFileDatabase(path=Path(tempfile.mkdtemp(prefix="verify_ax_")) / "db.json")
top = settings.default_mov_id
db.ensure_mov(top)
up = lambda **kw: db.upsert_object(top, VectorObjectValence(**kw))  # noqa: E731
up(vov_id=settings.liriel_self_vov_id, object_nature="PCI", brief_description="Liriel")
up(vov_id="Sentient_Joao", object_nature="Sentient", brief_description="Tadeu's climbing partner")
up(vov_id="Situation_Accident", object_nature="Situation", brief_description="a motorcycle accident")

text = "lost the left arm at twenty; uses a prosthesis he adapted himself"
reviews = [
    AnchorReviewResult.model_validate({"vov_id": "Sentient_Joao", "mov_id": top, "touches_this_row": "yes", "evidence": [{"words": "lost his left arm", "feelings_changes": []}],
                                       "schemas_changes": [{"schema_name": "BodyFeatures", "v": text, "c": 4, "reason": "stated"},
                                                           {"schema_name": "CharacterCourage", "v": "strong positive", "c": 3, "reason": "r"}]}),
    AnchorReviewResult.model_validate({"vov_id": "Situation_Accident", "mov_id": top, "touches_this_row": "yes", "evidence": [{"words": "acidente de moto", "feelings_changes": []}],
                                       "schemas_changes": [{"schema_name": "BodyFeatures", "v": "should never land on a situation", "c": 4, "reason": "x"}]}),
]

print("[check] a Schema change on a row that is not an agent is dropped; an agent's is kept")
assert motivation._without_schemas_on_non_agents(db, reviews[1]).schemas_changes == []
assert len(motivation._without_schemas_on_non_agents(db, reviews[0]).schemas_changes) == 2

print("[check] the free-text Schema lands as TEXT beside a numeric one on the agent row; nothing lands on the situation")
motivation._apply_anchor_reviews(db, top, reviews)
joao = db.get_object("Sentient_Joao")
assert joao.schemas["BodyFeatures"].v == text and joao.schemas["BodyFeatures"].c == 4, joao.schemas
assert joao.schemas["CharacterCourage"].v == 4.0, joao.schemas
assert not db.get_object("Situation_Accident").schemas

print("[check] the prompt of an agent row carries the free-text rule and the BodyFeatures item of the example; a non-agent's carries no rule")
s = VectorObjectValence(vov_id="PCI_X", object_nature="PCI", valence_regime="State", brief_description="Liriel")
a = VectorObjectValence(vov_id="Sentient_Y", object_nature="Sentient", valence_regime="State", brief_description="y")
t = VectorObjectValence(vov_id="Situation_Z", object_nature="Situation", valence_regime="State", brief_description="z")
art = Artifacts(meta_scheme="MS", mov=MatrixObjectsValence(mov_id="M", objects=[s, a, t]), scenario_data=ScenarioData(text="oi"))
p_agent = prompts.build_anchor_review_prompt(art, AnchorTarget(mov_id="M", vov=a, owner=s, is_nested=False))[1]["content"]
p_thing = prompts.build_anchor_review_prompt(art, AnchorTarget(mov_id="M", vov=t, owner=s, is_nested=False))[1]["content"]
assert "FREE-TEXT SCHEMAS" in p_agent and "never replace what was known with less" in p_agent and '"schema_name": "BodyFeatures"' in p_agent
assert "FREE-TEXT SCHEMAS" not in p_thing

print("[check] Liriel's OWN row: no free-text rule and no BodyFeatures item in her review (QA wrote Beatriz's disorder and wine onto her row)")
p_self = prompts.build_anchor_review_prompt(art, AnchorTarget(mov_id="M", vov=s, owner=s, is_nested=False))[1]["content"]
assert "FREE-TEXT SCHEMAS" not in p_self and "broken nose" not in p_self and '"schema_name": "CharacterEmpathy"' in p_self
assert '"schema_name": "BodyFeatures"' in p_agent  # the others still carry it

print("[check] and whatever a review writes there, a free-text Schema on Liriel's row is not carried out; her numeric ones are")
selfrev = AnchorReviewResult.model_validate({"vov_id": settings.liriel_self_vov_id, "mov_id": top, "evidence": [{"words": "w", "feelings_changes": []}],
                                             "schemas_changes": [{"schema_name": "MentalDisorders", "v": "transtorno de ansiedade", "c": 5, "reason": "r"},
                                                                 {"schema_name": "MindVices", "v": "vinho todo dia", "c": 4, "reason": "r"},
                                                                 {"schema_name": "CharacterEmpathy", "v": "strong positive", "c": 3, "reason": "r"}]})
kept = motivation._without_free_text_schemas_on_self(selfrev)
assert [c.schema_name for c in kept.schemas_changes] == ["CharacterEmpathy"]
assert motivation._without_free_text_schemas_on_self(reviews[0]) is reviews[0]  # another person's row is untouched by this guard
motivation._apply_anchor_reviews(db, top, [selfrev])
me = db.get_object(settings.liriel_self_vov_id)
assert "MentalDisorders" not in me.schemas and "MindVices" not in me.schemas and me.schemas["CharacterEmpathy"].v == 4.0, me.schemas
print("\nALL CHECKS PASSED")
