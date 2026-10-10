"""Ontology (MS Rev 0006) — the Postgres half.

mode "dry":  runs migrations/010 and every persistence check INSIDE one transaction on the
             live database and ROLLS IT BACK — DDL is transactional in Postgres, so
             nothing persists (checked at the end).
mode "live": run after migration 010 has been applied. Exercises the real transaction()
             (atomic commit, rollback on failure) with throw-away rows, then deletes them.
"""
import atexit
import os
import shutil
import tempfile

os.environ.setdefault("LLM_PROFILE", "1")  # no network: every LLM call in these scripts is mocked or not made
import sys
from pathlib import Path

GROQ_DIR = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, GROQ_DIR)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings  # noqa: E402
from database import DraftDatabase, PostgresDatabase  # noqa: E402
from verify_ontology_db import check_identity_records, check_relation_semantics, seed, vov  # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else "dry"
db = PostgresDatabase()
MIGRATION = Path(GROQ_DIR, "migrations", "010_ontology.sql").read_text(encoding="utf-8")
MIGRATION_011 = Path(GROQ_DIR, "migrations", "011_interpellation.sql").read_text(encoding="utf-8")  # Rev 0007 AZ (MS 6.14)
TEST_IDS = ("A", "B", "C", "Identity_1", "Identity_2", "Identity_3", "Identity_4", "Identity_5", "Interp_1")


def residue():
    with db._conn.cursor() as cur:
        cur.execute("select count(*) from mov_objects where vov_id = any(%s)", (list(TEST_IDS),))
        return cur.fetchone()[0]


def columns(table):
    with db._conn.cursor() as cur:
        cur.execute("select column_name from information_schema.columns where table_name = %s", (table,))
        return {r[0] for r in cur.fetchall()}


assert residue() == 0, "test ids already present in the live database — refusing to run"

if mode == "dry":
    # Works on a database that has not been migrated yet (the usual case for this mode) AND on one that has:
    # either way the migration must be idempotent and the rollback must restore exactly this state.
    state_before = (columns("mov_relations"), columns("mov_objects"), columns("motivation_cycles"))
    db._conn.autocommit = False
    db._in_tx = True  # one outer transaction; nothing below may reconnect or commit
    with db._conn.cursor() as cur:
        cur.execute(MIGRATION)
        cur.execute(MIGRATION)  # idempotent: running it twice must be harmless
    print("[check] migration 010 runs (twice) inside the transaction")
    assert {"label", "strength"} <= columns("mov_relations") and "identity_record" in columns("mov_objects")
    with db._conn.cursor() as cur:
        cur.execute(MIGRATION_011)
        cur.execute(MIGRATION_011)  # idempotent
    print("[check] migration 011 runs (twice) inside the transaction")
    seed(db)
    check_relation_semantics(db, "PostgresDatabase")
    check_identity_records(db, "PostgresDatabase")
    # Rev 0007 AZ (MS 6.14): an Interpellation is a row of its own nature, tied to its parties by the Identity bond, label = the role
    db.upsert_object(settings.default_mov_id, vov("Interp_1", "Interpellation", "A asks B to listen"))
    db.write_relation("Interp_1", "A", "Link_Identity_Part", label="origin", directed=True)
    db.write_relation("Interp_1", "B", "Link_Identity_Part", label="target", directed=True)
    assert db.get_object("Interp_1").object_nature == "Interpellation"
    assert {(r["to_vov_id"], r["label"]) for r in db.get_relations(["Interp_1"], ["Link_Identity_Part"])} == {("A", "origin"), ("B", "target")}
    print("[check] PostgresDatabase: an Interpellation row and its two role bonds persist and read back")
    # a nature outside the list is still refused by the widened constraint; Identity is not
    with db._conn.cursor() as cur:
        cur.execute("savepoint s")
        try:
            cur.execute("insert into mov_objects (vov_id, mov_id, object_nature, brief_description) values ('Bogus_1', %s, 'Bogus', 'x')",
                        (settings.default_mov_id,))
        except Exception as exc:  # noqa: BLE001
            cur.execute("rollback to savepoint s")
            print(f"[check] object_nature CHECK still refuses an unknown nature ({type(exc).__name__}) and accepts Identity")
        else:
            raise AssertionError("an unknown nature was accepted")
    # the old key is gone: two bonds of one category differing only by label coexist (checked above), and the same bond twice is one
    db._conn.rollback()
    db._in_tx = False
    db._conn.autocommit = True
    assert (columns("mov_relations"), columns("mov_objects"), columns("motivation_cycles")) == state_before, \
        "rollback did not restore the schema as it was"
    assert residue() == 0
    print("[check] rolled back: no column, no row, no constraint change persisted\n\nALL CHECKS PASSED (dry)")
else:
    assert "label" in columns("mov_relations"), "migration 010 not applied"
    TEST_MOV = "MOV_ONTOLOGY_TEST"  # a throw-away MOV: nothing below touches the default one
    try:
        db.ensure_mov(TEST_MOV)
        for vid in ("A", "B", "C"):
            db.upsert_object(TEST_MOV, vov(vid))
        db.write_relation("A", "B", "Link_Symbolic", propositional="boss", label="chefe")
        before = db.get_relations(["A", "B"])
        d = DraftDatabase(db)
        d.upsert_object(TEST_MOV, vov("A", desc="A, now with black hair"))
        d.write_relation("A", "B", "Link_Symbolic", label="chefe", propositional="former boss")
        d.write_relation("A", "C", "Link_Genealogical", label="siblings")
        calls = {"n": 0}
        orig = db.write_relation

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated failure mid-commit")
            return orig(*a, **kw)

        db.write_relation = flaky
        try:
            d.commit()
        except RuntimeError:
            pass
        else:
            raise AssertionError("commit should have failed")
        db.write_relation = orig
        assert db.get_object("A").brief_description == "x", "the object update survived a failed commit"
        assert db.get_relations(["A", "B"]) == before and not db.get_relations(["A", "C"], ["Link_Genealogical"]), \
            "a relation write survived a failed commit"
        assert db._in_tx is False and db._conn.autocommit is True
        print("[check] PostgresDatabase.transaction(): a failure half-way through commit rolls back the object AND relation writes")
        d.commit()
        assert db.get_object("A").brief_description == "A, now with black hair"
        assert db.get_relations(["A", "B"])[0]["propositional"] == "former boss"
        assert len(db.get_relations(["A"])) == 2
        d.cleanup()
        print("[check] the same draft then commits completely once the fault is gone")

        # A cycle waits minutes on the model, and the server drops a connection left idle that long
        # (seen live). (1) A DEAD connection when the commit starts must be replaced, not fail it:
        d2 = DraftDatabase(db)
        d2.upsert_object(TEST_MOV, vov("B", desc="B, after the connection died"))
        db._conn.close()  # exactly what an idle drop leaves behind
        d2.commit()
        assert db.get_object("B").brief_description == "B, after the connection died"
        d2.cleanup()
        print("[check] a connection that died while the cycle waited is replaced before the commit's transaction — the commit succeeds")

        # (2) A connection lost IN the commit is rolled back whole by the server: retried once, from the start.
        import psycopg2

        d3 = DraftDatabase(db)
        d3.upsert_object(TEST_MOV, vov("C", desc="C, committed on the second attempt"))
        d3.write_relation("B", "C", "Link_Symbolic", label="vizinhos", propositional="neighbours")
        orig_replace, tries = db.replace_object, {"n": 0}

        def drop_once(*a, **kw):
            tries["n"] += 1
            if tries["n"] == 1:
                db._conn.close()
                raise psycopg2.OperationalError("simulated: server closed the connection unexpectedly")
            return orig_replace(*a, **kw)

        db.replace_object = drop_once
        d3.commit()
        db.replace_object = orig_replace
        assert tries["n"] == 2 and db.get_object("C").brief_description == "C, committed on the second attempt"
        assert [r["propositional"] for r in db.get_relations(["B", "C"], ["Link_Symbolic"])
                if {r["from_vov_id"], r["to_vov_id"]} == {"B", "C"}] == ["neighbours"]
        assert db._in_tx is False and db._conn.autocommit is True
        d3.cleanup()
        print("[check] a connection lost INSIDE the commit: rolled back whole, reconnected, committed again once — nothing lost, nothing doubled"
              "\n\nALL CHECKS PASSED (live)")
    finally:
        with db._conn.cursor() as cur:
            cur.execute("delete from mov_objects where vov_id = any(%s)", (list(TEST_IDS),))  # relations cascade
            cur.execute("delete from movs where mov_id = %s", (TEST_MOV,))
        assert residue() == 0
db.close()
