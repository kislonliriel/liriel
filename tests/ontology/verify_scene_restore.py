"""Rev 0007 AM: an archived row Query 1 named as a scene element returns to focus; Objectives, Identity records and unknown ids do not."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import motivation  # noqa: E402
from models import VectorObjectValence  # noqa: E402
from verify_ontology_cycle import MOV, fresh, seed  # noqa: E402

db = fresh("verify_scene_restore.json")
seed(db)
for vid, nat in (("Sentient_Gone", "Sentient"), ("Objective_Old", "Objective"), ("Identity_Rec", "Identity"), ("Sentient_Stays", "Sentient")):
    db.upsert_object(MOV, VectorObjectValence(vov_id=vid, object_nature=nat, valence_regime="State", brief_description=vid))
    db.archive_object(vid)

els = [SimpleNamespace(vov_id="Sentient_Gone"), SimpleNamespace(vov_id="Objective_Old"), SimpleNamespace(vov_id="Identity_Rec"),
       SimpleNamespace(vov_id="No_Such_Row"), SimpleNamespace(vov_id=None), SimpleNamespace(vov_id="Sentient_Stays")]
els.pop()  # Sentient_Stays is not named by Query 1
motivation._restore_scene_elements(db, els)

print("[check] the archived person named as an element is back in focus")
assert not db.get_object("Sentient_Gone").archived
print("[check] an Objective, an Identity record, an unknown id and a person Query 1 did not name stay as they were")
assert db.get_object("Objective_Old").archived and db.get_object("Identity_Rec").archived and db.get_object("Sentient_Stays").archived
print("\nALL CHECKS PASSED")
