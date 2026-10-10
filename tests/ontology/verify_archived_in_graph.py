"""Rev 0007 T: `_archived_in_graph` lists, from the retrieval's graph nodes, the Objects that are ARCHIVED in the store."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import motivation  # noqa: E402
from models import VectorObjectValence  # noqa: E402
from verify_ontology_cycle import MICH, MOV, fresh, seed  # noqa: E402

db = fresh("verify_archived_in_graph.json")
seed(db)
db.upsert_object(MOV, VectorObjectValence(vov_id="Sentient_Gone", object_nature="Sentient", valence_regime="State",
                                          brief_description="a colleague, no longer in focus"))
db.upsert_object(MOV, VectorObjectValence(vov_id="Identity_Gone_Record", object_nature="Identity", valence_regime="State",
                                          brief_description="a record"))
db.archive_object("Sentient_Gone")
db.archive_object("Identity_Gone_Record")
graph = {"nodes": [{"vov_id": "Sentient_Gone", "source": "MainMemory"}, {"vov_id": MICH, "source": "MOV"},
                   {"vov_id": "Identity_Gone_Record", "source": "MainMemory"}, {"vov_id": "Sentient_Gone"}, {"vov_id": "no_such_row"}, {}]}

print("[check] only archived, non-Identity, existing rows; each once; active rows and unknown ids are left out")
got = motivation._archived_in_graph(db, graph)
assert got == [{"vov_id": "Sentient_Gone", "object_nature": "Sentient", "brief_description": "a colleague, no longer in focus"}], got

print("[check] no graph, or a graph without nodes, gives an empty list")
assert motivation._archived_in_graph(db, None) == [] and motivation._archived_in_graph(db, {}) == []
print("\nALL CHECKS PASSED")
