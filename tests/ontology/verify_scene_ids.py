"""Rev 0007 AU: an id Query 1 writes that no row has, but that is exactly ONE existing id once case/accents/punctuation are folded, is read as that id."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import motivation  # noqa: E402
from models import ElementEntry, SceneSubjectCheckResult, VectorObjectValence  # noqa: E402
from verify_ontology_cycle import MOV, fresh, seed  # noqa: E402

db = fresh("verify_scene_ids.json")
seed(db)
for vid in ("Sentient_Julia_Neteta", "Sentient_Cassio", "Sentient_Cassio_2", "Sentient_Ana_Maria", "Sentient_AnaMaria"):
    db.upsert_object(MOV, VectorObjectValence(vov_id=vid, object_nature="Sentient", valence_regime="State", brief_description=vid))

def check(**kw):
    return motivation._canonical_scene_ids(db, SceneSubjectCheckResult.model_validate(kw))

print("[check] the accent the model put back is folded away: Sentient_Júlia_Neteta -> Sentient_Julia_Neteta (elements, interlocutor and continuing id)")
r = check(interlocutor="Sentient_Júlia_Neteta", continuing_scenario_data_id="sentient_JULIA_neteta",
          elements=[{"vov_id": "Sentient_Júlia_Neteta", "object_nature": "Sentient", "is_hunter": True, "new_this_cycle": False}])
assert r.interlocutor == "Sentient_Julia_Neteta" and r.continuing_scenario_data_id == "Sentient_Julia_Neteta"
assert r.elements[0].vov_id == "Sentient_Julia_Neteta"

print("[check] an exact id is never touched; neither are a new element (no id), a provisional label, or an unknown id with no counterpart")
r = check(interlocutor="Sentient_Julia_Neteta", elements=[
    {"vov_id": "Sentient_Julia_Neteta"}, {"vov_id": None, "provisional_label": "tabletop RPG night", "object_nature": "Event"}, {"vov_id": "Sentient_Nobody_Known"}])
assert r.interlocutor == "Sentient_Julia_Neteta"
assert [e.vov_id for e in r.elements] == ["Sentient_Julia_Neteta", None, "Sentient_Nobody_Known"]
assert check(interlocutor="Seu Osvaldo").interlocutor == "Seu Osvaldo"

print("[check] a suffix makes a different id (Sentient_Cassio vs Sentient_Cassio_2 stay what they are); two candidates for one folded key leave the id untouched")
assert check(interlocutor="Sentient_Cassio").interlocutor == "Sentient_Cassio"
assert check(interlocutor="Sentient_Cássio_2").interlocutor == "Sentient_Cassio_2"
assert check(interlocutor="Sentient_Ana-Maria").interlocutor == "Sentient_Ana-Maria"  # folds to the same key as BOTH Ana_Maria and AnaMaria

print("[check] no Query 1 answer at all (nothing named) is a no-op")
r = check()
assert r.interlocutor is None and r.elements == []

print("[check] AU2: a minted id takes the accent off the letter (Joao), it does not cut a hole in the id (Jo_o); an existing id still collides into a suffix")
from database import _mint_vov_id_from, _sanitize_nickname  # noqa: E402
assert _sanitize_nickname("Sentient_João_Parceiro") == "Sentient_Joao_Parceiro"
assert _sanitize_nickname("Objective_Ação_Rápida") == "Objective_Acao_Rapida" and _sanitize_nickname("  Idea_x y  ") == "Idea_x_y"
assert _mint_vov_id_from([], "Sentient_Júlia") == "Sentient_Julia"
assert _mint_vov_id_from(["Sentient_Julia"], "Sentient_Júlia") == "Sentient_Julia_2"
print("\nALL CHECKS PASSED")
