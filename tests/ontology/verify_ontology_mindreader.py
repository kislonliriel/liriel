"""Ontology (MS Rev 0006) — MindReader's API against a seeded scratch store (no Postgres, no network)."""
import atexit
import os
import shutil
import tempfile

os.environ.setdefault("LLM_PROFILE", "1")  # no network: every LLM call in these scripts is mocked or not made
import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

GROQ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GROQ))

from config import settings  # noqa: E402
from database import JsonFileDatabase  # noqa: E402
from models import IdentityRecordBlock, RelationRef, VectorObjectValence  # noqa: E402
from mindreader import app as mr  # noqa: E402

HERE = Path(tempfile.mkdtemp(prefix="liriel_onto_"))
atexit.register(shutil.rmtree, HERE, ignore_errors=True)
MOV = settings.default_mov_id
p = HERE / "verify_onto_mr.json"
for q in [p, p.with_suffix(".cycles.jsonl"), p.with_name(p.stem + "_relations.json")]:
    q.unlink(missing_ok=True)
db = JsonFileDatabase(path=p)
db.ensure_mov(MOV)
for vid in ("Clara", "Bruno"):
    db.upsert_object(MOV, VectorObjectValence(vov_id=vid, object_nature="Sentient", brief_description=vid))
db.write_relation("Clara", "Bruno", "Link_Genealogical", propositional="married", label="conjugal", directed=False, strength=5)
db.write_relation("Clara", "Bruno", "Link_Valence_Load", propositional="adores", directed=True, affective=[{"axis": "LoveAngerEros", "v": 4.0}])
db.write_relation("Clara", "Bruno", "Link_Symbolic", propositional="boss", label="patroa-empregado", directed=True)
bond = next(r for r in db.get_relations(["Clara"]) if r["kind"] == "Link_Genealogical")


def rec(vid, **kw):
    db.upsert_object(MOV, VectorObjectValence(vov_id=vid, object_nature="Identity", brief_description=vid, identity_record=IdentityRecordBlock(**kw)))
    db.archive_object(vid)


for i in range(14):  # more than the viewer's default page
    rec(f"Id_m{i:02d}", target_vov_id="Clara", field="brief_description", attribute="hair", change_kind="added",
        information=f"<b>fact {i}</b>", recorded_at=f"2026-10-{i + 1:02d}T10:00:00Z", reliability=3)
rec("Id_bond", target_kind="relation", relation=RelationRef(relation_id=str(bond["id"]), from_vov_id="Clara", to_vov_id="Bruno",
    kind="Link_Genealogical", label="conjugal"), field="propositional", change_kind="world_change", information="married ten years",
    recorded_at="2026-10-20T10:00:00Z")

client = mr.app.test_client()
with patch.object(mr, "get_database", return_value=db):
    data = client.get("/api/mov").get_json()
    assert set(data["kinds"]) == {"Link_Subject_Cluster", "Link_Valence_Load", "Link_Genealogical", "Link_Space_Time", "Link_Symbolic", "Link_Identity_Part"}
    assert data["legend"]["Identity"], "the Identity nature has no colour"
    assert all(n["detail"]["object_nature"] != "Identity" for n in data["nodes"]), "an archived record was drawn"
    pair = [e for e in data["edges"] if {e["from"], e["to"]} == {"Clara", "Bruno"}]
    assert len(pair) == 3 and len({e["smooth"]["roundness"] for e in pair}) == 3, "parallel bonds overlap"
    by_kind = {e["detail"]["kind"]: e for e in pair}
    assert by_kind["Link_Valence_Load"]["arrows"] == {"to": {"enabled": True}} and "arrows" not in by_kind["Link_Genealogical"]
    assert by_kind["Link_Genealogical"]["label"] == "Genealogical · conjugal" and by_kind["Link_Genealogical"]["detail"]["strength"] == 5
    assert len({e["color"]["color"] for e in pair}) == 3, "the kinds share a colour"
    assert by_kind["Link_Genealogical"]["detail"]["identity_index"]["count"] == 1 and by_kind["Link_Valence_Load"]["detail"]["identity_index"] is None
    mich = next(n for n in data["nodes"] if n["id"] == "Clara")["detail"]["identity_index"]
    assert mich["count"] == 14 and mich["attributes"] == ["hair"], mich
    assert next(n for n in data["nodes"] if n["id"] == "Bruno")["detail"]["identity_index"] is None
    print("[check] /api/mov: six kinds with colours, parallel bonds fanned out, direction/strength/label on the edge, record summaries")

    # the full-vector window shows the row's HEADER (MS §6.4): every field of the row that is not the feelings/ordinances/schemas vector
    db.upsert_object(MOV, VectorObjectValence(vov_id="Header_Row", object_nature="Sentient", brief_description="a row with a full header", priority=None,
                                              update_datetime="2026_10_09_0900", relevant_relations=["Clara"], perceived_age="40", male_female="M",
                                              relevant_remarks="a remark for the header"))
    hdr = next(n for n in client.get("/api/mov").get_json()["nodes"] if n["id"] == "Header_Row")["detail"]
    for k in ("priority", "update_datetime", "object_type", "object_nature", "valence_regime", "nested_mov", "perceived_age", "male_female",
              "brief_description", "relevant_relations", "delta_report", "relevant_remarks", "archived"):
        assert k in hdr, f"header field {k} is not sent to the page"
    assert hdr["update_datetime"] == "2026_10_09_0900" and hdr["relevant_relations"] == ["Clara"] and hdr["perceived_age"] == "40"
    assert hdr["male_female"] == "M" and hdr["relevant_remarks"] == "a remark for the header" and hdr["delta_report"] is None and hdr["archived"] is False
    print("[check] /api/mov: every field of the row's header reaches the page (update_datetime, relevant_relations, delta_report, archived ...)")

    # Rev 0007 AZ (MS 6.14): an Interpellation has its own colour and its two Identity bonds read as origin / target
    db.upsert_object(MOV, VectorObjectValence(vov_id="Interpellation_X", object_nature="Interpellation", brief_description="Clara asks Bruno to listen"))
    db.write_relation("Interpellation_X", "Clara", "Link_Identity_Part", label="origin", directed=True)
    db.write_relation("Interpellation_X", "Bruno", "Link_Identity_Part", label="target", directed=True)
    ip = client.get("/api/mov").get_json()
    assert ip["legend"]["Interpellation"], "the Interpellation nature has no colour"
    node = next(n for n in ip["nodes"] if n["id"] == "Interpellation_X")
    assert node["detail"]["object_nature"] == "Interpellation"
    roles = {e["to"]: e["label"] for e in ip["edges"] if e["from"] == "Interpellation_X" and e["detail"]["kind"] == "Link_Identity_Part"}
    assert roles == {"Clara": "Identity · origin", "Bruno": "Identity · target"}, roles
    assert {e["to"]: e["detail"]["label"] for e in ip["edges"] if e["from"] == "Interpellation_X"} == {"Clara": "origin", "Bruno": "target"}
    print("[check] /api/mov: an Interpellation has its own colour and its Identity bonds read as origin / target")

    r = client.get("/api/identity?vov_id=Clara").get_json()
    assert r["total"] == 14 and len(r["records"]) == 10 and r["truncated"] is True
    assert r["records"][0]["vov_id"] == "Id_m13" and r["records"][-1]["vov_id"] == "Id_m04", "not newest-first / not bounded"
    object.__setattr__(settings, "identity_records_max", 3)
    assert len(client.get("/api/identity?vov_id=Clara&limit=50").get_json()["records"]) == 3, "the cap was not enforced"
    object.__setattr__(settings, "identity_records_max", 20)
    rb = client.get(f"/api/identity?relation_id={bond['id']}").get_json()
    assert rb["total"] == 1 and rb["records"][0]["relation"]["label"] == "conjugal" and rb["records"][0]["change_kind"] == "world_change"
    assert client.get("/api/identity").status_code == 400
    print("[check] /api/identity: newest first, bounded by limit and by IDENTITY_RECORDS_MAX, one bond by its own id, 400 without a target")

# the page script must at least parse (it is shipped as one inline <script>)
html = (GROQ / "mindreader" / "static" / "index.html").read_text(encoding="utf-8")
script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
# ... and the full-vector window carries the header block, in the order of the specification, above the three columns
header_keys = re.findall(r'\["([a-z_]+)", d\.', script[script.index("function fmtHeader"):script.index("function openVectorModal")])
assert header_keys == ["priority", "update_datetime", "vov_id", "object_type", "object_nature", "valence_regime", "nested_mov", "perceived_age", "male_female",
                       "brief_description", "relevant_relations", "delta_report", "relevant_remarks", "archived"], header_keys
modal = script[script.index("function openVectorModal"):script.index("function closeVectorModal")]
assert modal.index('id="vector-header"') < modal.index('id="vector-columns"') and "fmtHeader(d)" in modal
print("[check] index.html: the full-vector window shows the row header (MS §6.4), in order, above the vector")
assert 'Interpellation: "' in html and "interpHtml" in script and 'e.detail.label === role' in script and 'parties("origin")' in script and 'parties("target")' in script
print("[check] index.html: the side panel of an Interpellation names its origin and its target (read from the Identity bonds)")
tmp = HERE / "mr_script_check.js"
tmp.write_text(script, encoding="utf-8")
try:
    out = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
    if out.returncode == 0:
        print("[check] index.html's inline script parses (node --check)")
    elif "not recognized" in out.stderr or "not found" in out.stderr:
        print("[skip] node not available — script syntax not machine-checked")
    else:
        raise AssertionError(out.stderr)
except FileNotFoundError:
    print("[skip] node not available — script syntax not machine-checked")
tmp.unlink(missing_ok=True)

for q in [p, p.with_suffix(".cycles.jsonl"), p.with_name(p.stem + "_relations.json")]:
    q.unlink(missing_ok=True)
print("\nALL CHECKS PASSED")
