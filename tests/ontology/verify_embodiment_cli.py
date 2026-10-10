"""liriel.py end to end, as a Claude Code session would use it: `setup`, `say`, then `answer` request after request until the REPLY block.
Real processes (the cycle runs detached in the background), the real cycle code and prompts, a local store in a temporary folder; the only
thing faked is the person answering, which is a scripted responder reading each printed request and answering by its query. No network,
no database, no model."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TMP = Path(tempfile.mkdtemp(prefix="verify_embodiment_"))
ENV = {**os.environ, "LIRIEL_SESSION_DIR": str(TMP / "session"), "LIRIEL_LOCAL_STORE": str(TMP / "store" / "local_mov_store.json"),
       "LLM_PROFILE": "3", "PYTHONIOENCODING": "utf-8"}
ENV.pop("LIRIEL_STORE", None)


def cleanup():
    for line in subprocess.run([sys.executable, "-c", "import sys"], capture_output=True).stderr.decode().splitlines():
        pass
    shutil.rmtree(TMP, ignore_errors=True)


def liriel(*args, stdin=None):
    out = subprocess.run([sys.executable, str(ROOT / "liriel.py"), *args], input=stdin, capture_output=True, text=True, encoding="utf-8", env=ENV, cwd=str(ROOT), timeout=300)
    return out.stdout + out.stderr


def full_text(shown: str) -> str:
    """What the request says, whole: the part files when the request was long."""
    parts = re.findall(r"^\s+(\S+view_\d+_\d+of\d+\.md)\s*$", shown, re.M)
    return shown if not parts else "\n".join(Path(p).read_text(encoding="utf-8") for p in parts)


SELF = "PCI_Liriel_Self"
CYC = "cycle_x"


def respond(shown: str):
    """(answer text) for the request on screen, by its query."""
    head = re.search(r"^=== REQUEST \d+ \| query: (\S+)", shown, re.M)
    query = head.group(1)
    text = full_text(shown)
    if query == "None":
        return "Hi! So good to hear from you... All good here. And you, how are you?"
    if query.endswith("_BATCH"):
        base = query[: -len("_BATCH")]
        keys = re.findall(r"^ITEM \d+ of \d+ — (.+?)   \[task variant", text, re.M)
        entries = []
        for k in keys:
            if base == "HUNTER_READING":
                label = k.replace("hunter=", "")
                entries.append({"query": base, "vov_id_or_label": label, "relation_to_liriel": "self" if label == SELF else "ally", "ordinances_read": [],
                                "schemas_read": [], "supposed_prey": "p", "needs_own_mov": False, "feels_about": []})
            elif base == "ANCHOR_REVIEW":
                mov_id, vov = re.search(r"mov_id=(\S+) vov_id=(\S+)", k).groups()
                entries.append({"query": base, "mov_id": mov_id, "vov_id": vov, "owner_vov_id": SELF, "touches_this_row": "no", "evidence": [], "schemas_changes": [], "missing_cause": None})
            else:
                entries.append({"query": base, "target_id": k.replace("target=", ""), "learned": "nothing new", "records": []})
        return json.dumps({"query": query, "cycle_id": CYC, "results": entries})
    simple = {
        "SAFETY_SCREEN": {"says": "a greeting", "harm_described": "none", "at_risk": "none", "what": "", "notes": ""},
        "SCENE_SUBJECT_CHECK": {"is_new_subject": True, "interlocutor": "Tess", "elements": [
            {"vov_id": SELF, "object_nature": "PCI", "is_hunter": True, "new_this_cycle": False},
            {"vov_id": None, "provisional_label": "Tess", "object_nature": "Sentient", "is_hunter": True, "new_this_cycle": True}]},
        "GRAPH_REQUEST": {"requests": [], "search_commands": [], "retrieval_satisfied": True},
        "TACTICAL_SCENE_INTERPRETATION": {"board": {"summary": "a greeting", "space": "none", "time": "none", "symbolic": "none"}, "relations_summary": "none"},
        "MAINMEMORY_FILING": {"archive": [], "restore": []},
        "MOV_UPDATE": {"mov_ops": [
            {"op": "UPSERT_VOV", "vov": {"vov_id": "Sentient_Tess", "object_type": "real", "object_nature": "Sentient", "valence_regime": "State",
                                          "brief_description": "Tess, who greets Liriel", "relevant_relations": ["ScenarioData_Tess_Greeting"]}},
            {"op": "UPSERT_VOV", "vov": {"vov_id": "ScenarioData_Tess_Greeting", "object_type": "real", "object_nature": "ScenarioData", "valence_regime": "State",
                                          "brief_description": "backbone: Tess greets Liriel", "relevant_relations": ["Sentient_Tess", SELF],
                                          "relevant_remarks": "Tess greeted Liriel and asked how she is."}}], "nested_mov_ops": []},
        "RELATIONS_UPDATE": {"write_relations": [], "soften_charge": []},
        "ANCHOR_REVIEW": {"mov_id": "MOV_DEFAULT", "vov_id": "x", "owner_vov_id": SELF, "touches_this_row": "no", "evidence": [], "schemas_changes": [], "missing_cause": None},
        "HUNTER_READING": {"vov_id_or_label": "x", "relation_to_liriel": "ally", "ordinances_read": [], "schemas_read": [], "supposed_prey": "p", "needs_own_mov": False, "feels_about": []},
        "BEST_PREY_GUESS": {"interlocutor_brought": "a greeting", "character_says": "warm and honest", "may_be_told": "nothing asked", "asked_of_liriel": "nothing asked",
                            "guess_matter_of": "Tess", "interlocutor_is_party": "yes",
                            "best_prey_guess": {"vov_id": "Objective_Tess_Welcome", "priority": 1, "object_type": "real", "object_nature": "Objective", "valence_regime": "Delta",
                                                "brief_description": "welcome Tess", "relevant_relations": ["ScenarioData_Tess_Greeting"],
                                                "objective": {"genus": "Prey", "species": "Conquest", "gain_form": "Increment", "channel_ordinances": ["InstinctCompanionship"],
                                                              "beneficiary_scope": [SELF], "granularity": "stage", "status": "open", "cycles_open": 0, "information_seeking": True}},
                            "accompanying_objectives": [], "handoff_to_processcommandcontrol": {"objective_summary": "welcome", "why_now": "greeted"},
                            "mov_ops": [], "nested_mov_ops": []},
    }
    if query == "ANCHOR_REVIEW":
        target = re.search(r"^target: mov_id=(\S+) vov_id=(\S+) ", text, re.M)
        simple[query] = {**simple[query], "mov_id": target.group(1), "vov_id": target.group(2)}
    if query == "HUNTER_READING":
        simple[query] = {**simple[query], "vov_id_or_label": re.search(r"^target: hunter=(.+)$", text, re.M).group(1)}
    return json.dumps({"query": query, "cycle_id": CYC, **simple[query]})


try:
    print("[check] setup on a machine with nothing: the first row is planted, no database, no key, no server")
    out = liriel("setup")
    assert "Liriel's first row planted." in out and "backend claude_session" in out and "Ready." in out, out
    assert "was already there" in liriel("setup"), "setup is idempotent"
    assert (TMP / "store" / "local_mov_store.json").exists()

    print("[check] say: a cycle starts in the background and the first request is on screen; a second say is refused while it runs")
    shown = liriel("say", "Hi, Liriel! How are you?", "--sender", "Tess")
    assert "=== REQUEST 000001 | query: SAFETY_SCREEN" in shown and "Hi, Liriel! How are you?" in shown, shown
    assert "already running" in liriel("say", "outra")
    assert "pending: ['000001:SAFETY_SCREEN']" in liriel("status")

    print("[check] answer, request after request, until the REPLY block; a JSON that does not parse keeps the same request on screen")
    queries = []
    bad_sent = False
    for _ in range(40):
        if "=== REPLY" in shown or "=== CYCLE FAILED" in shown:
            break
        head = re.search(r"^=== REQUEST (\d+) \| query: (\S+)", shown, re.M)
        assert head, shown[:400]
        queries.append(head.group(2))
        if head.group(2) == "SCENE_SUBJECT_CHECK" and not bad_sent:
            bad_sent = True
            again = liriel("answer", stdin='{"query": "SCENE_SUBJECT_CHECK", "elements": [')
            assert "NOT VALID JSON" in again and f"=== REQUEST {head.group(1)} | query: SCENE_SUBJECT_CHECK" in again, again[:300]
            shown = again
        shown = liriel("answer", stdin=respond(shown))
    assert "=== REPLY" in shown, (queries, shown[:600])
    print("        requests answered:", len(queries), "->", queries)
    assert "Hi! So good to hear from you... All good here." in shown and "AS LIRIEL" in shown and "(written)" in shown
    assert "HUNTER_READING_BATCH" in queries, "batch mode is on for this embodiment"

    print("[check] the reply can be read again; the cycle is over (no cycle file) and what it wrote is in the local store")
    assert "So good to hear from you" in liriel("reply")
    assert not (TMP / "session" / "cycle.json").exists()
    store = json.loads((TMP / "store" / "local_mov_store.json").read_text(encoding="utf-8"))
    ids = {v for mov in store.values() for v in (mov if isinstance(mov, dict) else {})} if isinstance(store, dict) else set()
    assert any("Tess" in str(k) for k in store) or "Sentient_Tess" in json.dumps(store), list(store)[:5]

    print("[check] a second message starts a new cycle on the same memory")
    shown = liriel("say", "Thank you!", "--sender", "Tess")
    assert "=== REQUEST" in shown and "SAFETY_SCREEN" in shown
    for p in (TMP / "session").glob("alive_*"):
        pass
    print("\nALL CHECKS PASSED")
finally:
    # stop the second cycle's process (it waits for an answer nobody will give), then remove the temporary folders
    c = json.loads((TMP / "session" / "cycle.json").read_text(encoding="utf-8")) if (TMP / "session" / "cycle.json").exists() else {}
    if c.get("pid"):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(c["pid"]), "/F"], capture_output=True)
        else:
            try:
                os.kill(c["pid"], 15)
            except OSError:
                pass
    shutil.rmtree(TMP, ignore_errors=True)
