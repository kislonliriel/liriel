"""
Persistence layer.

Primary backend: PostgreSQL / Supabase (see migrations/001_init.sql and
migrations/002_metascheme_alignment.sql for the schema). Falls back to a
local JSON file when no database is configured, so the chat loop can be
exercised before Supabase is wired up — a development convenience, not a
substitute for real persistence.

The MainMemory (MS §7) is not a separate store: an archived VOV is just a
mov_objects row with archived_at set. load_mov() returns only active
(archived_at IS NULL) rows — the focus (MS §6.2) — while get_object() can
still reach an archived one, which is what RESTORE_VOV needs.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from config import settings
from models import AxisValence, MatrixObjectsValence, SchemaEntry, VectorObjectValence

_VOV_ID_RE = re.compile(r"^VOV_(\d+)")


def _next_vov_id_from(existing_ids) -> str:
    """Next free id in the VOV_00NN convention the MetaScheme/reference
    spreadsheet already use (MS §6.4 examples, MatrixObjectsValence_Rev000.
    xlsx) — highest existing numeric suffix + 1, zero-padded to at least 4
    digits. Ignores any trailing letter suffix (VOV_0002B, VOV_0000B_L3,
    ...): those mark specular-recursion mirror levels of an existing id,
    not a new sequence position, so they don't affect the count. Scans
    every id regardless of mov_id or archived state — vov_id is one global
    primary key, so a retired id must not be handed out again either.

    Kept as a plain scan-then-increment rather than a DB sequence/lock:
    Phase 1 is a single-process terminal chat with no concurrent writers,
    so there's no race to guard against yet."""
    max_n = 0
    for vid in existing_ids:
        m = _VOV_ID_RE.match(vid or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"VOV_{max_n + 1:04d}"


class Database(ABC):
    @abstractmethod
    def load_mov(self, mov_id: str) -> MatrixObjectsValence:
        """Active (non-archived) rows only — the focus."""

    @abstractmethod
    def get_object(self, vov_id: str) -> Optional[VectorObjectValence]:
        """Any row, active or archived."""

    @abstractmethod
    def get_object_mov_id(self, vov_id: str) -> Optional[str]:
        """Which mov_id this row currently lives under (any, active or
        archived) — VectorObjectValence itself carries no mov_id (a vov_id
        is one global identity that happens to sit in exactly one MOV at a
        time), so this is the only way to know where to write it back.
        Needed for materializing nested MOVs (MS §6.8): when a VOV gets a
        real nested_mov pointer, its own updated row has to be upserted
        back into whichever MOV it actually belongs to, not assumed."""

    def get_objects(self, vov_ids: list) -> dict:
        """Any rows, active or archived, keyed by vov_id. Default: one
        get_object() per id — fine at Phase 1 scale; override for a batch
        query if that ever matters."""
        out = {}
        for vov_id in vov_ids:
            vov = self.get_object(vov_id)
            if vov is not None:
                out[vov_id] = vov
        return out

    @abstractmethod
    def upsert_object(self, mov_id: str, vov: VectorObjectValence) -> None:
        ...

    @abstractmethod
    def next_vov_id(self) -> str:
        """A fresh, never-used vov_id in the VOV_00NN convention. The model
        is not trusted to invent a collision-free id for a genuinely new
        object — it has no visibility into ids outside the current MOV
        (e.g. ones reserved by a nested MOV, or by the reference dataset),
        which is exactly how the "VOV_0000" import collided with a chat
        session's own object earlier. Assigning it server-side removes the
        possibility entirely."""

    @abstractmethod
    def archive_object(self, vov_id: str) -> None:
        ...

    @abstractmethod
    def restore_object(self, vov_id: str) -> None:
        ...

    @abstractmethod
    def log_cycle(
        self,
        mov_id: str,
        scenario_text: str,
        update_result: Optional[dict],
        decision_result: Optional[dict],
        response_text: str,
        graph_of_traces: Optional[dict] = None,
    ) -> None:
        ...

    @abstractmethod
    def get_recent_scenario_texts(self, mov_id: str, limit: int = 3) -> List[str]:
        """The raw message text of the last `limit` cycles for this mov,
        most recent first — gives graph_service.search_memory a short
        conversational window instead of just this one message in
        isolation, so a pronoun-based follow-up ("quem é o marido DELA?")
        can still resolve to whoever the previous turn was actually about."""

    @abstractmethod
    def get_recent_decision_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        """Query 3's (BEST_PREY_GUESS, MS §12.5) full output for the last
        `limit` cycles of this mov, most recent first, skipping cycles that
        never reached Query 3 (a crash earlier in the cycle, MS §11.1). The
        Best-Prey Guess VOV persisted in mov_objects carries only its own
        `brief_description` (MS §6.4, ≤25 words) — the handoff to
        ProcessCommandControl (why_now, constraints, success/failure
        criteria, report_back — MS §12.5's own `handoff_to_processcommandcontrol`
        block, MS §11.2's "point of departure") lives only here, once per
        cycle it was actually elected, never on the row itself. MindReader
        uses this to show, for whichever Objective was actually the
        elected prey, the handoff that MS §11's rule of ownership makes
        the ONLY legitimate source of what the reply may say — not a
        second, informal one Liriel calls up from raw conversation text."""

    @abstractmethod
    def write_relation(
        self,
        from_vov_id: str,
        to_vov_id: str,
        kind: str,
        propositional: Optional[str] = None,
        affective: Optional[list] = None,
        confidence: Optional[int] = None,
        since_text: Optional[str] = None,
    ) -> None:
        """MS §12.4 WRITE_RELATION. An edge in the Graph of Traces (MS §8) —
        `kind` plus (from, to) is the edge's identity, so writing the same
        pair+kind again updates that edge in place rather than duplicating
        it (a relationship's propositional/affective content can change
        over time; the *fact* that it's e.g. a "marriage" doesn't repeat)."""

    @abstractmethod
    def soften_charge(self, vov_ids: list) -> None:
        """MS §7.2/§12.4 SOFTEN_CHARGE. Time has passed; the bond stays on
        the record, but its affective weight eases. Halves the magnitude
        of every axis on every edge touching one of these Objects — the
        propositional content and the edge itself are untouched, per §7.2
        ("softening, not dissolution")."""

    @abstractmethod
    def get_relations(self, vov_ids: list, relation_kinds: Optional[list] = None) -> list:
        """Every edge (MS §8.3 shape, as a dict) touching any of these
        vov_ids, regardless of whether either endpoint is archived —
        TrackGraphProcess (graph_service.py) is the one reader who needs
        the archive and the focus at once. One hop; graph_service does its
        own multi-hop traversal by calling this again on newly-reached ids."""

    @abstractmethod
    def get_all_objects(self, mov_id: str) -> List[VectorObjectValence]:
        """Every row (active or archived, any object_nature) in this mov —
        unlike get_relations/get_objects, this has no starting vov_id to key
        off of, because it exists for exactly the case where nothing does
        yet: graph_service.search_memory (MS §12.4 SEARCH) scans this whole
        set to keyword/fuzzy-match a fresh message's content against
        MainMemory, since GRAPH_REQUEST's own traversal can only reach an
        Object the model already has an id or a linked bond for."""

    def ensure_mov(self, mov_id: str, label: Optional[str] = None) -> None:
        """Create the `movs` container row a nested MOV needs before any VOV
        can reference it as `nested_mov` (MS §6.8) — a no-op on the JSON
        fallback, which has no separate movs table."""

    def replace_object(self, mov_id: str, vov: VectorObjectValence) -> None:
        """Like upsert_object, but the feelings map fully replaces whatever
        was there instead of merging axis-by-axis. upsert_object merges on
        purpose (so a PATCH_VOV touching one axis doesn't erase the rest
        during a normal chat cycle) — but that's wrong for a bulk import of
        authoritative reference data, where a vov_id might collide with one
        a chat session already created (e.g. both use the same VOV_000N
        convention) and stale test-session axes would otherwise survive
        alongside the freshly-imported ones. Default: same as upsert_object
        (correct as-is for the JSON backend, which always fully overwrites)."""
        self.upsert_object(mov_id, vov)

    def close(self) -> None:  # pragma: no cover - optional override
        pass


# ---------------------------------------------------------------------------
# PostgreSQL / Supabase backend
# ---------------------------------------------------------------------------

class PostgresDatabase(Database):
    def __init__(self):
        import psycopg2  # imported lazily so the fallback doesn't need it
        import psycopg2.extras

        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras

        if settings.has_discrete_pg_config:
            # Connect via a params dict instead of a DATABASE_URL string —
            # sidesteps URL-encoding a password that contains reserved URI
            # characters (@, :, /, ?, #, ...), a common source of "could not
            # translate host name" errors when the password isn't escaped.
            self._conn = psycopg2.connect(
                host=settings.pg_host,
                port=settings.pg_port,
                dbname=settings.pg_database,
                user=settings.pg_user,
                password=settings.pg_password,
            )
        else:
            self._conn = psycopg2.connect(settings.database_url)
        self._conn.autocommit = True
        self._ensure_mov_row(settings.default_mov_id)

    def _ensure_mov_row(self, mov_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "insert into movs (mov_id) values (%s) on conflict do nothing",
                (mov_id,),
            )

    def ensure_mov(self, mov_id: str, label: Optional[str] = None) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "insert into movs (mov_id, label) values (%s, %s) "
                "on conflict (mov_id) do nothing",
                (mov_id, label),
            )

    def _row_to_vov(self, row: dict, valences: dict) -> VectorObjectValence:
        return VectorObjectValence(
            vov_id=row["vov_id"],
            priority=row["priority"],
            update_datetime=row["update_datetime"],
            object_type=row["object_type"],
            object_nature=row["object_nature"],
            valence_regime=row["valence_regime"],
            nested_mov=row["nested_mov"],
            perceived_age=row["perceived_age"],
            male_female=row["male_female"],
            brief_description=row["brief_description"],
            relevant_relations=row["relevant_relations"] or [],
            delta_report=row["delta_report"],
            relevant_remarks=row["relevant_remarks"],
            feelings=valences,
            ordinances={
                k: AxisValence(**v) for k, v in (row["ordinances"] or {}).items()
            },
            schemas={
                k: SchemaEntry(**v) for k, v in (row["schemas"] or {}).items()
            },
            objective=row["objective"],
            updated_at=row["updated_at"],
            archived=row["archived_at"] is not None,
        )

    def _load_valences(self, cur, vov_ids: list) -> dict:
        if not vov_ids:
            return {}
        cur.execute(
            "select vov_id, axis_key, value, confidence from mov_object_valences "
            "where vov_id = any(%s)",
            (vov_ids,),
        )
        by_vov: dict = {}
        for r in cur.fetchall():
            by_vov.setdefault(r["vov_id"], {})[r["axis_key"]] = AxisValence(
                v=float(r["value"]), c=r["confidence"]
            )
        return by_vov

    def load_mov(self, mov_id: str) -> MatrixObjectsValence:
        with self._conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                "select * from mov_objects where mov_id = %s and archived_at is null",
                (mov_id,),
            )
            rows = cur.fetchall()
            valences = self._load_valences(cur, [r["vov_id"] for r in rows])

        objects = [self._row_to_vov(r, valences.get(r["vov_id"], {})) for r in rows]
        return MatrixObjectsValence(mov_id=mov_id, objects=objects)

    def get_object(self, vov_id: str) -> Optional[VectorObjectValence]:
        with self._conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute("select * from mov_objects where vov_id = %s", (vov_id,))
            row = cur.fetchone()
            if not row:
                return None
            valences = self._load_valences(cur, [vov_id])
        return self._row_to_vov(row, valences.get(vov_id, {}))

    def get_objects(self, vov_ids: list) -> dict:
        if not vov_ids:
            return {}
        with self._conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute("select * from mov_objects where vov_id = any(%s)", (vov_ids,))
            rows = cur.fetchall()
            valences = self._load_valences(cur, [r["vov_id"] for r in rows])
        return {r["vov_id"]: self._row_to_vov(r, valences.get(r["vov_id"], {})) for r in rows}

    def get_all_objects(self, mov_id: str) -> List[VectorObjectValence]:
        with self._conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute("select * from mov_objects where mov_id = %s", (mov_id,))
            rows = cur.fetchall()
            valences = self._load_valences(cur, [r["vov_id"] for r in rows])
        return [self._row_to_vov(r, valences.get(r["vov_id"], {})) for r in rows]

    def get_object_mov_id(self, vov_id: str) -> Optional[str]:
        with self._conn.cursor() as cur:
            cur.execute("select mov_id from mov_objects where vov_id = %s", (vov_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def upsert_object(self, mov_id: str, vov: VectorObjectValence) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                insert into mov_objects (
                    vov_id, mov_id, priority, update_datetime, object_type,
                    object_nature, valence_regime, nested_mov, perceived_age,
                    male_female, brief_description, relevant_relations,
                    delta_report, relevant_remarks, ordinances, schemas,
                    objective, updated_at
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
                )
                on conflict (vov_id) do update set
                    priority = excluded.priority,
                    update_datetime = excluded.update_datetime,
                    object_type = excluded.object_type,
                    object_nature = excluded.object_nature,
                    valence_regime = excluded.valence_regime,
                    nested_mov = excluded.nested_mov,
                    perceived_age = excluded.perceived_age,
                    male_female = excluded.male_female,
                    brief_description = excluded.brief_description,
                    relevant_relations = excluded.relevant_relations,
                    delta_report = excluded.delta_report,
                    relevant_remarks = excluded.relevant_remarks,
                    ordinances = excluded.ordinances,
                    schemas = excluded.schemas,
                    objective = excluded.objective,
                    archived_at = null,
                    updated_at = now()
                """,
                (
                    vov.vov_id,
                    mov_id,
                    vov.priority,
                    vov.update_datetime,
                    vov.object_type,
                    vov.object_nature,
                    vov.valence_regime,
                    vov.nested_mov,
                    vov.perceived_age,
                    vov.male_female,
                    vov.brief_description,
                    json.dumps(vov.relevant_relations),
                    json.dumps(vov.delta_report.model_dump() if vov.delta_report else None),
                    vov.relevant_remarks,
                    json.dumps({k: v.model_dump() for k, v in vov.ordinances.items()}),
                    json.dumps({k: v.model_dump() for k, v in vov.schemas.items()}),
                    json.dumps(vov.objective.model_dump() if vov.objective else None),
                ),
            )
            for axis_key, av in vov.feelings.items():
                cur.execute(
                    """
                    insert into mov_object_valences (vov_id, axis_key, value, confidence, updated_at)
                    values (%s, %s, %s, %s, now())
                    on conflict (vov_id, axis_key) do update set
                        value = excluded.value,
                        confidence = excluded.confidence,
                        updated_at = now()
                    """,
                    (vov.vov_id, axis_key, av.v, av.c),
                )

    def replace_object(self, mov_id: str, vov: VectorObjectValence) -> None:
        with self._conn.cursor() as cur:
            cur.execute("delete from mov_object_valences where vov_id = %s", (vov.vov_id,))
        self.upsert_object(mov_id, vov)

    def archive_object(self, vov_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "update mov_objects set archived_at = now() where vov_id = %s", (vov_id,)
            )

    def restore_object(self, vov_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "update mov_objects set archived_at = null where vov_id = %s", (vov_id,)
            )

    def next_vov_id(self) -> str:
        with self._conn.cursor() as cur:
            cur.execute("select vov_id from mov_objects")
            existing_ids = [r[0] for r in cur.fetchall()]
        return _next_vov_id_from(existing_ids)

    def write_relation(
        self,
        from_vov_id: str,
        to_vov_id: str,
        kind: str,
        propositional: Optional[str] = None,
        affective: Optional[list] = None,
        confidence: Optional[int] = None,
        since_text: Optional[str] = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                insert into mov_relations (
                    from_vov_id, to_vov_id, kind, propositional, affective, confidence, since_text
                ) values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (from_vov_id, to_vov_id, kind) do update set
                    propositional = excluded.propositional,
                    affective = excluded.affective,
                    confidence = excluded.confidence,
                    since_text = excluded.since_text,
                    updated_at = now()
                """,
                (from_vov_id, to_vov_id, kind, propositional, json.dumps(affective or []), confidence, since_text),
            )

    def soften_charge(self, vov_ids: list) -> None:
        if not vov_ids:
            return
        with self._conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                "select id, affective from mov_relations where from_vov_id = any(%s) or to_vov_id = any(%s)",
                (vov_ids, vov_ids),
            )
            rows = cur.fetchall()
            for row in rows:
                softened = [
                    {"axis": entry["axis"], "v": round(entry["v"] * 0.5, 2)}
                    for entry in (row["affective"] or [])
                ]
                cur.execute(
                    "update mov_relations set affective = %s, softened_at = now(), updated_at = now() "
                    "where id = %s",
                    (json.dumps(softened), row["id"]),
                )

    def get_relations(self, vov_ids: list, relation_kinds: Optional[list] = None) -> list:
        if not vov_ids:
            return []
        with self._conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            if relation_kinds:
                cur.execute(
                    "select * from mov_relations where (from_vov_id = any(%s) or to_vov_id = any(%s)) "
                    "and kind = any(%s)",
                    (vov_ids, vov_ids, relation_kinds),
                )
            else:
                cur.execute(
                    "select * from mov_relations where from_vov_id = any(%s) or to_vov_id = any(%s)",
                    (vov_ids, vov_ids),
                )
            return [dict(r) for r in cur.fetchall()]

    def log_cycle(
        self,
        mov_id: str,
        scenario_text: str,
        update_result: Optional[dict],
        decision_result: Optional[dict],
        response_text: str,
        graph_of_traces: Optional[dict] = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                insert into motivation_cycles (
                    mov_id, scenario_data, graph_of_traces, update_result,
                    decision_result, response_text
                ) values (%s, %s, %s, %s, %s, %s)
                """,
                (
                    mov_id,
                    scenario_text,
                    json.dumps(graph_of_traces) if graph_of_traces else None,
                    json.dumps(update_result) if update_result else None,
                    json.dumps(decision_result) if decision_result else None,
                    response_text,
                ),
            )

    def get_recent_scenario_texts(self, mov_id: str, limit: int = 3) -> List[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "select scenario_data from motivation_cycles where mov_id = %s "
                "order by created_at desc limit %s",
                (mov_id, limit),
            )
            return [row[0] for row in cur.fetchall()]

    def get_recent_decision_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "select decision_result from motivation_cycles where mov_id = %s "
                "and decision_result is not null order by created_at desc limit %s",
                (mov_id, limit),
            )
            return [row[0] for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Local JSON fallback (development convenience only)
# ---------------------------------------------------------------------------

class JsonFileDatabase(Database):
    """Stores every VOV (active and archived) in one JSON file, plus an
    append-only .jsonl cycle log. Meant only for trying out the chat loop
    before a real Postgres/Supabase database is configured."""

    def __init__(self, path: Path = Path("local_mov_store.json")):
        self._path = path
        self._log_path = path.with_suffix(".cycles.jsonl")
        self._relations_path = path.with_name(path.stem + "_relations.json")

    def _load_all(self) -> dict:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _save_all(self, data: dict) -> None:
        self._path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def _load_relations(self) -> list:
        if not self._relations_path.exists():
            return []
        return json.loads(self._relations_path.read_text(encoding="utf-8"))

    def _save_relations(self, relations: list) -> None:
        self._relations_path.write_text(json.dumps(relations, indent=2, default=str), encoding="utf-8")

    def load_mov(self, mov_id: str) -> MatrixObjectsValence:
        # "_mov_id" is this store's own bookkeeping key (see upsert_object),
        # not a VectorObjectValence field — Pydantic's default extra="ignore"
        # drops it harmlessly when hydrating. A pre-existing entry without
        # one predates per-mov tracking; here (a filtered listing) that
        # means keeping it only when default_mov_id is the asked-for mov_id.
        data = self._load_all()
        objects = [
            VectorObjectValence.model_validate(v)
            for v in data.values()
            if not v.get("archived") and v.get("_mov_id", settings.default_mov_id) == mov_id
        ]
        return MatrixObjectsValence(mov_id=mov_id, objects=objects)

    def get_object(self, vov_id: str) -> Optional[VectorObjectValence]:
        data = self._load_all()
        raw = data.get(vov_id)
        return VectorObjectValence.model_validate(raw) if raw else None

    def get_object_mov_id(self, vov_id: str) -> Optional[str]:
        data = self._load_all()
        raw = data.get(vov_id)
        if raw is None:
            return None
        return raw.get("_mov_id", settings.default_mov_id)

    def get_all_objects(self, mov_id: str) -> List[VectorObjectValence]:
        data = self._load_all()
        return [
            VectorObjectValence.model_validate(v)
            for v in data.values()
            if v.get("_mov_id", settings.default_mov_id) == mov_id
        ]

    def upsert_object(self, mov_id: str, vov: VectorObjectValence) -> None:
        data = self._load_all()
        vov.archived = False
        raw = json.loads(vov.model_dump_json())
        raw["_mov_id"] = mov_id
        data[vov.vov_id] = raw
        self._save_all(data)

    def archive_object(self, vov_id: str) -> None:
        data = self._load_all()
        if vov_id in data:
            data[vov_id]["archived"] = True
            self._save_all(data)

    def restore_object(self, vov_id: str) -> None:
        data = self._load_all()
        if vov_id in data:
            data[vov_id]["archived"] = False
            self._save_all(data)

    def next_vov_id(self) -> str:
        return _next_vov_id_from(self._load_all().keys())

    def write_relation(
        self,
        from_vov_id: str,
        to_vov_id: str,
        kind: str,
        propositional: Optional[str] = None,
        affective: Optional[list] = None,
        confidence: Optional[int] = None,
        since_text: Optional[str] = None,
    ) -> None:
        relations = self._load_relations()
        entry = {
            "from_vov_id": from_vov_id, "to_vov_id": to_vov_id, "kind": kind,
            "propositional": propositional, "affective": affective or [],
            "confidence": confidence, "since_text": since_text, "softened_at": None,
        }
        for i, r in enumerate(relations):
            if r["from_vov_id"] == from_vov_id and r["to_vov_id"] == to_vov_id and r["kind"] == kind:
                relations[i] = entry
                break
        else:
            relations.append(entry)
        self._save_relations(relations)

    def soften_charge(self, vov_ids: list) -> None:
        relations = self._load_relations()
        touched = set(vov_ids)
        for r in relations:
            if r["from_vov_id"] in touched or r["to_vov_id"] in touched:
                r["affective"] = [{"axis": e["axis"], "v": round(e["v"] * 0.5, 2)} for e in (r["affective"] or [])]
                r["softened_at"] = datetime.now(timezone.utc).isoformat()
        self._save_relations(relations)

    def get_relations(self, vov_ids: list, relation_kinds: Optional[list] = None) -> list:
        touched = set(vov_ids)
        relations = self._load_relations()
        out = [r for r in relations if r["from_vov_id"] in touched or r["to_vov_id"] in touched]
        if relation_kinds:
            out = [r for r in out if r["kind"] in relation_kinds]
        return out

    def log_cycle(
        self,
        mov_id: str,
        scenario_text: str,
        update_result: Optional[dict],
        decision_result: Optional[dict],
        response_text: str,
        graph_of_traces: Optional[dict] = None,
    ) -> None:
        entry = {
            "mov_id": mov_id,
            "scenario_data": scenario_text,
            "graph_of_traces": graph_of_traces,
            "update_result": update_result,
            "decision_result": decision_result,
            "response_text": response_text,
        }
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def get_recent_scenario_texts(self, mov_id: str, limit: int = 3) -> List[str]:
        if not self._log_path.exists():
            return []
        lines = self._log_path.read_text(encoding="utf-8").splitlines()
        texts = []
        for line in reversed(lines):
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("mov_id") == mov_id:
                texts.append(entry.get("scenario_data", ""))
            if len(texts) >= limit:
                break
        return texts

    def get_recent_decision_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        if not self._log_path.exists():
            return []
        lines = self._log_path.read_text(encoding="utf-8").splitlines()
        results = []
        for line in reversed(lines):
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("mov_id") == mov_id and entry.get("decision_result"):
                results.append(entry["decision_result"])
            if len(results) >= limit:
                break
        return results


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_database() -> Database:
    if settings.has_discrete_pg_config or settings.database_url:
        return PostgresDatabase()
    print(
        "[warning] No database configured — using local storage in "
        "'local_mov_store.json' (development only). Configure your .env "
        "and run migrations/001_init.sql + migrations/002_metascheme_alignment.sql "
        "to persist to Supabase."
    )
    return JsonFileDatabase()
