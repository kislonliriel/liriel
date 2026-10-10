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

import contextlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from config import settings
from models import AxisValence, MatrixObjectsValence, SchemaEntry, VectorObjectValence, default_directed

_NICKNAME_PART_RE = re.compile(r"[^A-Za-z0-9]+")


def _sanitize_nickname(nickname: Optional[str]) -> str:
    """A model-composed nickname (MS §6.4: `<object_nature>_<ShortSlug>_
    <Qualifier>`) is free text as far as the model is concerned — normalize
    it into a safe id: alnum runs joined by single underscores, no leading/
    trailing underscore. Doesn't enforce the three-part shape itself — a
    two-part or malformed attempt still becomes a usable, if less
    descriptive, id rather than being rejected outright."""
    if not nickname:
        return ""
    # Rev 0007 AU2: accents are taken off the letter first ("João" -> "Joao"), not cut out of the id ("Jo_o"): QA minted `Sentient_J_lia_Neteta`
    # and `Sentient_Jo_o`, ids no later answer ever spelled the same way again.
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", nickname.strip()) if not unicodedata.combining(ch))
    parts = [p for p in _NICKNAME_PART_RE.split(plain) if p]
    return "_".join(parts)


def _mint_vov_id_from(existing_ids, nickname: Optional[str], object_nature: Optional[str] = None) -> str:
    """New-object id minting (this session's redesign, MS §6.4): the model
    composes a three-part nickname instead of guessing a numeric VOV_nnnn —
    exactly like the numeric convention it replaces, the model is never
    trusted to guarantee collision-freedom on its own: this returns the
    nickname verbatim when it's free, or with a numeric disambiguating
    suffix appended on the rare actual collision. A blank or unusable
    nickname falls back to a generic `<nature>_Unlabeled` base rather than
    failing the mint outright — this codebase's usual tolerance for a
    small model's format drift. Legacy `VOV_nnnn` ids already on record
    (from before this convention) are untouched by this — they keep
    working as ordinary ids and are simply part of `existing_ids` like any
    other, so a nickname can never collide with one silently."""
    existing = set(existing_ids)
    base = _sanitize_nickname(nickname)
    if not base:
        base = f"{_sanitize_nickname(object_nature) or 'Object'}_Unlabeled"
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


# ---------------------------------------------------------------------------
# Relations (MS §8, Rev 0006) — one bond is (from, to, kind, label); two Objects may
# hold any number of them at once and writing one never touches another.
# ---------------------------------------------------------------------------

def _norm_label(label: Optional[str]) -> str:
    """A bond's `label` is the short word that tells two bonds of one category
    apart ("conjugal" / "siblings", "employer–employee" / "pastor–congregant"). Compared
    whitespace- and case-insensitively, so the same bond written twice is one bond."""
    return " ".join((label or "").split()).casefold()[:60]


def _edge_key(row: dict) -> tuple:
    return (row["from_vov_id"], row["to_vov_id"], row["kind"], _norm_label(row.get("label")))


def _plan_relation_write(rows: list, from_vov_id: str, to_vov_id: str, kind: str, label: str, directed: Optional[bool]):
    """Where a write of this bond lands, given the bonds already touching its ends.
    Returns `(existing_row | None, from, to, directed)`.

    A directed bond is its own row, so A->B and B->A coexist with their own wording
    and strength (the relation of A with B is not the relation of B with A). An
    undirected bond is stored ONCE, whichever way round it is written: an existing
    undirected row of the same kind and label is found in either orientation, and a
    new one is stored with its ends in a fixed order. `directed=None` keeps what an
    existing bond already says, and otherwise takes the kind's default."""
    label = _norm_label(label)

    def same_bond(r):
        return r["kind"] == kind and _norm_label(r.get("label")) == label

    exact = next((r for r in rows if same_bond(r) and r["from_vov_id"] == from_vov_id and r["to_vov_id"] == to_vov_id), None)
    if exact is not None:
        final = bool(exact.get("directed")) if directed is None else bool(directed)
        return exact, exact["from_vov_id"], exact["to_vov_id"], final
    flipped = next(
        (r for r in rows if same_bond(r) and not r.get("directed")
         and r["from_vov_id"] == to_vov_id and r["to_vov_id"] == from_vov_id),
        None,
    )
    if flipped is not None and directed is not True:
        # The same undirected bond, written from its other end. (A writer that WANTS a
        # separate directed bond beside it says so with directed=True.)
        return flipped, flipped["from_vov_id"], flipped["to_vov_id"], False
    final = default_directed(kind) if directed is None else bool(directed)
    if not final:
        a, b = sorted((from_vov_id, to_vov_id))
        if any(same_bond(r) and r["from_vov_id"] == a and r["to_vov_id"] == b for r in rows):
            a, b = from_vov_id, to_vov_id  # the fixed order is taken by a directed bond: keep this one's own
        return None, a, b, False
    return None, from_vov_id, to_vov_id, True


# ---------------------------------------------------------------------------
# Identity records (MS §6.13, Rev 0006) — selective retrieval
# ---------------------------------------------------------------------------

def _plain(text: Optional[str]) -> str:
    """Accent- and case-insensitive comparison key."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold().strip()


def _period_ok(value: Optional[str], bound: str, lower: bool) -> bool:
    """ISO-8601 text compared by prefix, so "2024" overlaps "2024-03-01" instead of
    falling just short of it: a date known only to the month or the year still lands
    in a period that contains it."""
    if not value:
        return False
    n = min(len(value), len(bound))
    return value[:n] >= bound[:n] if lower else value[:n] <= bound[:n]


def _filter_identity_records(records: list, attribute=None, since=None, until=None, limit=None) -> list:
    """Newest first. The period applies to when the thing happened when that is
    known, otherwise to when it was recorded; the attribute matches the aspect in
    either direction ("cabelo" finds "cor do cabelo")."""
    need = _plain(attribute) if attribute else None
    out = []
    for rec in records:
        blk = rec.identity_record
        if blk is None:
            continue
        if need:
            hay = {_plain(blk.attribute), _plain(blk.field)} - {""}
            if not any(need in h or h in need for h in hay):
                continue
        when = blk.occurred_at or blk.recorded_at
        if since and not _period_ok(when, since, True):
            continue
        if until and not _period_ok(when, until, False):
            continue
        out.append(rec)
    out.sort(key=lambda r: (r.identity_record.recorded_at, r.vov_id), reverse=True)
    return out[:limit] if limit else out


def _index_add(entry: dict, blk) -> None:
    entry["count"] += 1
    if blk.change_kind == "corrected":
        entry["corrections"] += 1
    label = blk.attribute or blk.field
    if label and label not in entry["attributes"] and len(entry["attributes"]) < 6:
        entry["attributes"].append(label)
    if entry["last_recorded_at"] is None:
        entry["last_recorded_at"] = blk.recorded_at


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
    def mint_vov_id(self, nickname: Optional[str], object_nature: Optional[str] = None) -> str:
        """A fresh, never-used vov_id built from the model's own composed
        nickname (MS §6.4: `<object_nature>_<ShortSlug>_<Qualifier>`). The
        model is not trusted to guarantee collision-freedom itself — it has
        no visibility into ids outside the current MOV (e.g. ones reserved
        by a nested MOV, or by the reference dataset), which is exactly how
        the "VOV_0000" import once collided with a chat session's own
        object. Assigning the final id server-side removes the possibility
        entirely: the nickname's wording is the model's, uniqueness is
        the architecture's. `object_nature` is used only for the fallback
        label when `nickname` is blank/unusable."""

    @abstractmethod
    def all_vov_ids(self) -> List[str]:
        """Every vov_id on record, any mov (including nested ones), active
        or archived — vov_id is one global namespace (MS §6.4), so minting
        a fresh one has to check against all of it, not just one mov's own
        rows. Broken out as its own method (both concrete backends used to
        do this inline, inside mint_vov_id itself) so DraftDatabase below
        can check a real backend's existing ids directly, without routing
        through its minting logic."""

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
        scene_subject_check_result: Optional[dict],
        graph_request_result: Optional[dict],
        tactical_scene_result: Optional[dict],
        anchor_review_results: Optional[dict],
        mainmemory_filing_result: Optional[dict],
        mov_update_result: Optional[dict],
        relations_update_result: Optional[dict],
        decision_result: Optional[dict],
        response_text: str,
        graph_of_traces: Optional[dict] = None,
        identity_update_result: Optional[dict] = None,
    ) -> None:
        """MS §11.1 Rev 0001: one jsonb column per query of the six-query
        cycle, replacing the old two-column (`update_result`/
        `decision_result`) shape — the user's own stated audit goal is
        reading each query's raw answer back directly, which a merged
        column can't support. `decision_result` keeps its pre-existing name
        (BEST_PREY_GUESS's own output) so get_recent_decision_results'
        existing readers (mindreader/app.py's _latest_bpg_info) keep
        working unchanged. `anchor_review_results` (Rev 0003) is one
        object, `{"cycle_id": ..., "reviews": [AnchorReviewResult, ...]}` —
        Query 3B runs once PER OBJECT (MS §12.3B), so a single cycle
        produces a list of results, not one."""
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
        """BEST_PREY_GUESS's (MS §12.7) full output for the last `limit`
        cycles of this mov, most recent first, skipping cycles that never
        reached that query (a crash earlier in the cycle, MS §11.1). Each
        entry is `{**decision_result, "response_text": ...}` — the actual
        chat reply that cycle sent (Phase 1's ProcessCommandControl
        stand-in, MS §11), alongside the decision that supposedly drove it.
        Kept together so a human (or a future audit process) can check the
        reply against the handoff it claims to carry out, rather than
        trusting that it did — this is the whole point of MS §11.2's rule of
        ownership being checkable, not just asserted. The
        Best-Prey Guess VOV persisted in mov_objects carries only its own
        `brief_description` (MS §6.4, ≤25 words) — the handoff to
        ProcessCommandControl (why_now, constraints, success/failure
        criteria, report_back — MS §12.7's own `handoff_to_processcommandcontrol`
        block, MS §11.2's "point of departure") lives only here, once per
        cycle it was actually elected, never on the row itself. MindReader
        uses this to show, for whichever Objective was actually the
        elected prey, the handoff that MS §11's rule of ownership makes
        the ONLY legitimate source of what the reply may say — not a
        second, informal one Liriel calls up from raw conversation text."""

    @abstractmethod
    def get_recent_tactical_scene_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        """TACTICAL_SCENE_INTERPRETATION's (MS §12.3) full output for the
        last `limit` cycles of this mov, most recent first — the shared
        Current Tactical Scene artifact MindReader's cluster/scenario view
        reads, now that it's its own query's output rather than folded into
        decision_result (its pre-redesign home when BEST_PREY_GUESS still
        rebuilt its own copy of it each cycle)."""

    @abstractmethod
    def get_recent_scene_subject_check_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        """SCENE_SUBJECT_CHECK's (MS §12.1) full output for the last `limit`
        cycles of this mov, most recent first — MindReader's ScenarioData
        panel joins this against get_recent_tactical_scene_results by
        shared `cycle_id` to show every one of MS §9.4's Key Questions
        (which elements/hunters, KQ01-02, alongside KQ03-08) for whichever
        cycle's decision actually related to that cluster, not just the
        board/hunters half TACTICAL_SCENE_INTERPRETATION alone answers."""

    def get_scene_subject_check_timeline(self, mov_id: str) -> List[dict]:
        """Every logged SCENE_SUBJECT_CHECK of this mov, OLDEST first, each
        `{"at": <when the cycle was logged>, **check}` -- MindReader reads
        `is_new_subject` / `continuing_scenario_data_id` off it to number a
        ScenarioData within its subject. A cycle is logged just after its
        rows are committed, so a row belongs to the first entry logged at or
        after its own `updated_at`. Not abstract: a backend that keeps no
        times answers [] and the numbering falls back to the rows' bonds."""
        return []

    def get_creation_times(self, mov_id: str, object_nature: str) -> dict:
        """{vov_id: when the row was first written} for the rows of one
        nature. `updated_at` is not that: archiving and restoring a row stamp
        it again. Not abstract: a backend that does not keep it answers {}
        and the caller falls back to `updated_at`."""
        return {}

    @abstractmethod
    def get_recent_anchor_review_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        """Every cycle's ANCHOR_REVIEW results (MS §12.3B, KQ10-13), most
        recent first, each `{"cycle_id": ..., "reviews": [...]}` — one
        entry per cycle that reviewed at least one Object. MindReader joins
        it to the other queries of the same cycle by `cycle_id`."""

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
        label: str = "",
        directed: Optional[bool] = None,
        strength: Optional[int] = None,
        softened_at: Optional[str] = None,
    ) -> None:
        """MS §12.4 WRITE_RELATION. An edge in the Graph of Traces (MS §8). Its
        identity is (from, to, kind, label) — Rev 0006: two Objects may hold any
        number of bonds at once (a genealogical one beside affective, symbolic and
        subject ones, even two symbolic ones told apart by `label`), and writing one
        never touches another. Writing the same bond again updates it in place, and
        only the fields actually given change: an omitted field is left as it was
        (`None` is "not stated", never "blank"; `affective=[]` does clear the charge).
        `directed` says whether the bond reads differently from each side; omitted, an
        existing bond keeps what it says and a new one takes its kind's default. An
        undirected bond is stored once, whichever way round it is written. `strength`
        is optional, 1-5, and its absence is not a zero."""

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

    @contextlib.contextmanager
    def transaction(self):
        """Everything written inside this block becomes durable together or not at
        all (Rev 0006: an Object's update and the Identity record that documents it
        must never be seen apart, not even by a crash half-way through a commit).
        Default: no guarantee — the backends override it."""
        yield

    def get_committed_object(self, vov_id: str) -> Optional[VectorObjectValence]:
        """The row as it stood BEFORE the current cycle's draft began. Plain
        backends have no draft, so it is simply the row; DraftDatabase answers from
        the real database, which is what makes "what did this cycle change" a diff."""
        return self.get_object(vov_id)

    def get_committed_relations(self, vov_ids: list, relation_kinds: Optional[list] = None) -> list:
        """Same as get_committed_object, for relations."""
        return self.get_relations(vov_ids, relation_kinds)

    @abstractmethod
    def get_identity_records(
        self,
        target_vov_ids: Optional[list] = None,
        relation_ids: Optional[list] = None,
        change_kinds: Optional[list] = None,
        attribute: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[VectorObjectValence]:
        """MS §6.13. The Identity records (rows of nature "Identity") that match,
        newest first and bounded by `limit`. `target_vov_ids` are principal Objects,
        `relation_ids` are specific relations. Reading a principal Object never loads
        its history — records come only from here, on request."""

    def identity_index(self, target_vov_ids: Optional[list] = None, relation_ids: Optional[list] = None) -> dict:
        """A compact first look at what is on file, so the model can ask for detail
        only where it is wanted: `{"objects": {vov_id: {count, corrections,
        attributes, last_recorded_at}}, "relations": {relation_id: {...}}}`. Only
        targets that have at least one record appear."""
        out: dict = {"objects": {}, "relations": {}}
        if target_vov_ids:
            for rec in self.get_identity_records(target_vov_ids=list(target_vov_ids)):
                blk = rec.identity_record
                entry = out["objects"].setdefault(
                    blk.target_vov_id, {"count": 0, "corrections": 0, "attributes": [], "last_recorded_at": None}
                )
                _index_add(entry, blk)
        if relation_ids:
            for rec in self.get_identity_records(relation_ids=list(relation_ids)):
                blk = rec.identity_record
                rid = blk.relation.relation_id if blk.relation else None
                if rid is None:
                    continue
                entry = out["relations"].setdefault(
                    rid, {"count": 0, "corrections": 0, "attributes": [], "last_recorded_at": None}
                )
                _index_add(entry, blk)
        return out

    def close(self) -> None:  # pragma: no cover - optional override
        pass


# ---------------------------------------------------------------------------
# PostgreSQL / Supabase backend
# ---------------------------------------------------------------------------

def _with_reconnect(method):
    """Retries a PostgresDatabase method exactly once, transparently
    reconnecting first, on a dead connection.

    PostgresDatabase opens ONE connection in __init__ and holds it for the
    life of the process — fine for a short-lived script, but the Telegram
    bot (telegram_bot.py) keeps that same object alive for its entire run,
    often idle for long stretches between messages. Confirmed for real: a
    real incoming message ("Milton bateu o carro... a Manuely me ligou
    desesperada!") failed the whole cycle with "server closed the
    connection unexpectedly" — Supabase had dropped the idle connection,
    and psycopg2 does not self-heal a dead connection on its own; every
    subsequent call would have failed the same way until the process was
    restarted by hand. This closes that gap generally, for every method
    below, rather than patching the one call site that happened to be hit
    first."""
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (self._psycopg2.OperationalError, self._psycopg2.InterfaceError) as exc:
            if getattr(self, "_in_tx", False):
                # A connection that dies inside a transaction took the transaction with it:
                # retrying on a fresh autocommit connection would write the rest of it
                # piecemeal. Fail instead, and let the whole block be redone.
                raise
            print(f"[notice] Postgres connection appears dead ({exc}) — "
                  f"reconnecting and retrying {method.__name__} once")
            self._reconnect()
            return method(self, *args, **kwargs)
    return wrapper


class PostgresDatabase(Database):
    def __init__(self):
        import psycopg2  # imported lazily so the fallback doesn't need it
        import psycopg2.extras

        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras
        self._conn = self._connect()
        self._conn.autocommit = True
        self._in_tx = False
        self._ensure_mov_row(settings.default_mov_id)

    def _connect(self):
        if settings.has_discrete_pg_config:
            # Connect via a params dict instead of a DATABASE_URL string —
            # sidesteps URL-encoding a password that contains reserved URI
            # characters (@, :, /, ?, #, ...), a common source of "could not
            # translate host name" errors when the password isn't escaped.
            return self._psycopg2.connect(
                host=settings.pg_host,
                port=settings.pg_port,
                dbname=settings.pg_database,
                user=settings.pg_user,
                password=settings.pg_password,
            )
        return self._psycopg2.connect(settings.database_url)

    def _reconnect(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass  # already broken/closed — nothing more to release
        self._conn = self._connect()
        self._conn.autocommit = True

    def _ensure_mov_row(self, mov_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "insert into movs (mov_id) values (%s) on conflict do nothing",
                (mov_id,),
            )

    @_with_reconnect
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
            identity_record=row.get("identity_record") or None,
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

    @_with_reconnect
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

    @_with_reconnect
    def get_object(self, vov_id: str) -> Optional[VectorObjectValence]:
        with self._conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute("select * from mov_objects where vov_id = %s", (vov_id,))
            row = cur.fetchone()
            if not row:
                return None
            valences = self._load_valences(cur, [vov_id])
        return self._row_to_vov(row, valences.get(vov_id, {}))

    @_with_reconnect
    def get_objects(self, vov_ids: list) -> dict:
        if not vov_ids:
            return {}
        with self._conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute("select * from mov_objects where vov_id = any(%s)", (vov_ids,))
            rows = cur.fetchall()
            valences = self._load_valences(cur, [r["vov_id"] for r in rows])
        return {r["vov_id"]: self._row_to_vov(r, valences.get(r["vov_id"], {})) for r in rows}

    @_with_reconnect
    def get_all_objects(self, mov_id: str) -> List[VectorObjectValence]:
        with self._conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute("select * from mov_objects where mov_id = %s", (mov_id,))
            rows = cur.fetchall()
            valences = self._load_valences(cur, [r["vov_id"] for r in rows])
        return [self._row_to_vov(r, valences.get(r["vov_id"], {})) for r in rows]

    @_with_reconnect
    def get_object_mov_id(self, vov_id: str) -> Optional[str]:
        with self._conn.cursor() as cur:
            cur.execute("select mov_id from mov_objects where vov_id = %s", (vov_id,))
            row = cur.fetchone()
            return row[0] if row else None

    @_with_reconnect
    def upsert_object(self, mov_id: str, vov: VectorObjectValence) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                insert into mov_objects (
                    vov_id, mov_id, priority, update_datetime, object_type,
                    object_nature, valence_regime, nested_mov, perceived_age,
                    male_female, brief_description, relevant_relations,
                    delta_report, relevant_remarks, ordinances, schemas,
                    objective, identity_record, updated_at
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
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
                    identity_record = excluded.identity_record,
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
                    json.dumps(vov.identity_record.model_dump() if vov.identity_record else None),
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

    @_with_reconnect
    def replace_object(self, mov_id: str, vov: VectorObjectValence) -> None:
        with self._conn.cursor() as cur:
            cur.execute("delete from mov_object_valences where vov_id = %s", (vov.vov_id,))
        self.upsert_object(mov_id, vov)

    @_with_reconnect
    def archive_object(self, vov_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "update mov_objects set archived_at = now() where vov_id = %s", (vov_id,)
            )

    @_with_reconnect
    def restore_object(self, vov_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "update mov_objects set archived_at = null where vov_id = %s", (vov_id,)
            )

    @_with_reconnect
    def all_vov_ids(self) -> List[str]:
        with self._conn.cursor() as cur:
            cur.execute("select vov_id from mov_objects")
            return [r[0] for r in cur.fetchall()]

    @_with_reconnect
    def mint_vov_id(self, nickname: Optional[str], object_nature: Optional[str] = None) -> str:
        return _mint_vov_id_from(self.all_vov_ids(), nickname, object_nature)

    @_with_reconnect
    def write_relation(
        self,
        from_vov_id: str,
        to_vov_id: str,
        kind: str,
        propositional: Optional[str] = None,
        affective: Optional[list] = None,
        confidence: Optional[int] = None,
        since_text: Optional[str] = None,
        label: str = "",
        directed: Optional[bool] = None,
        strength: Optional[int] = None,
        softened_at: Optional[str] = None,
    ) -> None:
        label = _norm_label(label)
        existing, a, b, is_directed = _plan_relation_write(
            self.get_relations([from_vov_id, to_vov_id]), from_vov_id, to_vov_id, kind, label, directed
        )
        with self._conn.cursor() as cur:
            if existing is not None:
                cur.execute(
                    """
                    update mov_relations set
                        propositional = coalesce(%s, propositional),
                        affective = case when %s then %s::jsonb else affective end,
                        confidence = coalesce(%s, confidence),
                        since_text = coalesce(%s, since_text),
                        strength = coalesce(%s, strength),
                        directed = %s,
                        softened_at = coalesce(%s::timestamptz, softened_at),
                        updated_at = now()
                    where id = %s
                    """,
                    (propositional, affective is not None, json.dumps(affective or []), confidence,
                     since_text, strength, is_directed, softened_at, existing["id"]),
                )
            else:
                cur.execute(
                    """
                    insert into mov_relations (
                        from_vov_id, to_vov_id, kind, label, directed, propositional,
                        affective, confidence, since_text, strength, softened_at
                    ) values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::timestamptz)
                    on conflict (from_vov_id, to_vov_id, kind, label) do update set
                        directed = excluded.directed,
                        propositional = coalesce(excluded.propositional, mov_relations.propositional),
                        affective = excluded.affective,
                        confidence = coalesce(excluded.confidence, mov_relations.confidence),
                        since_text = coalesce(excluded.since_text, mov_relations.since_text),
                        strength = coalesce(excluded.strength, mov_relations.strength),
                        updated_at = now()
                    """,
                    (a, b, kind, label, is_directed, propositional, json.dumps(affective or []),
                     confidence, since_text, strength, softened_at),
                )

    @_with_reconnect
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

    @_with_reconnect
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

    @_with_reconnect
    def get_identity_records(
        self,
        target_vov_ids: Optional[list] = None,
        relation_ids: Optional[list] = None,
        change_kinds: Optional[list] = None,
        attribute: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[VectorObjectValence]:
        where, params = ["object_nature = 'Identity'"], []
        if target_vov_ids is not None:
            where.append("identity_record->>'target_vov_id' = any(%s)")
            params.append(list(target_vov_ids))
        if relation_ids is not None:
            where.append("identity_record->'relation'->>'relation_id' = any(%s)")
            params.append(list(relation_ids))
        if change_kinds:
            where.append("identity_record->>'change_kind' = any(%s)")
            params.append(list(change_kinds))
        with self._conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute("select * from mov_objects where " + " and ".join(where), params)
            rows = cur.fetchall()
        records = [self._row_to_vov(r, {}) for r in rows]
        return _filter_identity_records(records, attribute, since, until, limit)

    @_with_reconnect
    def _ensure_alive(self) -> None:
        """A cycle spends minutes waiting on the model, and the server may drop a connection left
        idle that long (Supabase does). `_with_reconnect` replaces a dead connection but, on purpose,
        never retries INSIDE a transaction (the rest would land outside it) — so liveness is checked
        here, once, right before the commit's transaction begins."""
        with self._conn.cursor() as cur:
            cur.execute("select 1")

    @contextlib.contextmanager
    def transaction(self):
        if self._in_tx:  # already inside one: join it
            yield
            return
        self._ensure_alive()
        self._in_tx = True
        self._conn.autocommit = False
        try:
            yield
            self._conn.commit()
        except BaseException:
            try:
                self._conn.rollback()
            except Exception:  # the connection itself is gone; the server already discarded the transaction
                pass
            raise
        finally:
            self._in_tx = False
            try:
                self._conn.autocommit = True
            except Exception:
                pass  # a dead connection is replaced, autocommit on, by the next call's reconnect

    @_with_reconnect
    def log_cycle(
        self,
        mov_id: str,
        scenario_text: str,
        scene_subject_check_result: Optional[dict],
        graph_request_result: Optional[dict],
        tactical_scene_result: Optional[dict],
        anchor_review_results: Optional[dict],
        mainmemory_filing_result: Optional[dict],
        mov_update_result: Optional[dict],
        relations_update_result: Optional[dict],
        decision_result: Optional[dict],
        response_text: str,
        graph_of_traces: Optional[dict] = None,
        identity_update_result: Optional[dict] = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                insert into motivation_cycles (
                    mov_id, scenario_data, graph_of_traces,
                    scene_subject_check_result, graph_request_result,
                    tactical_scene_result, anchor_review_results, mainmemory_filing_result,
                    mov_update_result, relations_update_result, decision_result, response_text,
                    identity_update_result
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    mov_id,
                    scenario_text,
                    json.dumps(graph_of_traces) if graph_of_traces else None,
                    json.dumps(scene_subject_check_result) if scene_subject_check_result else None,
                    json.dumps(graph_request_result) if graph_request_result else None,
                    json.dumps(tactical_scene_result) if tactical_scene_result else None,
                    json.dumps(anchor_review_results) if anchor_review_results else None,
                    json.dumps(mainmemory_filing_result) if mainmemory_filing_result else None,
                    json.dumps(mov_update_result) if mov_update_result else None,
                    json.dumps(relations_update_result) if relations_update_result else None,
                    json.dumps(decision_result) if decision_result else None,
                    response_text,
                    json.dumps(identity_update_result) if identity_update_result else None,
                ),
            )

    @_with_reconnect
    def get_recent_scenario_texts(self, mov_id: str, limit: int = 3) -> List[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "select scenario_data from motivation_cycles where mov_id = %s "
                "order by created_at desc limit %s",
                (mov_id, limit),
            )
            return [row[0] for row in cur.fetchall()]

    @_with_reconnect
    def get_recent_decision_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "select decision_result, response_text from motivation_cycles where mov_id = %s "
                "and decision_result is not null order by created_at desc limit %s",
                (mov_id, limit),
            )
            return [{**row[0], "response_text": row[1]} for row in cur.fetchall()]

    @_with_reconnect
    def get_recent_tactical_scene_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "select tactical_scene_result from motivation_cycles where mov_id = %s "
                "and tactical_scene_result is not null order by created_at desc limit %s",
                (mov_id, limit),
            )
            return [row[0] for row in cur.fetchall()]

    @_with_reconnect
    def get_recent_scene_subject_check_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "select scene_subject_check_result from motivation_cycles where mov_id = %s "
                "and scene_subject_check_result is not null order by created_at desc limit %s",
                (mov_id, limit),
            )
            return [row[0] for row in cur.fetchall()]

    @_with_reconnect
    def get_scene_subject_check_timeline(self, mov_id: str) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "select created_at, scene_subject_check_result from motivation_cycles where mov_id = %s "
                "and scene_subject_check_result is not null order by created_at",
                (mov_id,),
            )
            return [{**row[1], "at": row[0]} for row in cur.fetchall()]

    @_with_reconnect
    def get_creation_times(self, mov_id: str, object_nature: str) -> dict:
        with self._conn.cursor() as cur:
            cur.execute(
                "select vov_id, created_at from mov_objects where mov_id = %s and object_nature = %s",
                (mov_id, object_nature),
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    @_with_reconnect
    def get_recent_anchor_review_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "select anchor_review_results from motivation_cycles where mov_id = %s "
                "and anchor_review_results is not null order by created_at desc limit %s",
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
        # PostgresDatabase's own upsert always stamps updated_at = now() in
        # its SQL, regardless of whatever the Python object carries in that
        # field -- this backend has to do the same itself, or a freshly
        # minted VOV (whose model-supplied JSON never sets updated_at, only
        # the free-text update_datetime) keeps updated_at=None. Confirmed
        # for real, via DraftDatabase (this session's redesign): a brand
        # new ScenarioData cluster, drafted here mid-cycle before its real
        # Postgres commit, read as updated_at=None -- _evict_stale_clusters'
        # own recency sort (motivation.py) treats a None timestamp as the
        # oldest possible, so the cluster this cycle JUST created was the
        # one AIRP's MemoryStrength cap evicted first, every time, the
        # opposite of "oldest survives longest." graph_service.py's own
        # _recency_factor has the identical blind spot for the same reason.
        vov.updated_at = datetime.now(timezone.utc)
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

    def all_vov_ids(self) -> List[str]:
        return list(self._load_all().keys())

    def mint_vov_id(self, nickname: Optional[str], object_nature: Optional[str] = None) -> str:
        return _mint_vov_id_from(self.all_vov_ids(), nickname, object_nature)

    def _store_relation(
        self, from_vov_id: str, to_vov_id: str, kind: str, label: str, directed: bool,
        propositional=None, affective=None, confidence=None, since_text=None,
        strength=None, softened_at=None,
    ) -> None:
        """Stores one bond EXACTLY as oriented (the caller has already resolved which
        row it is, see _plan_relation_write); only the fields given change an existing
        one, and its id survives the update — like Postgres's own update-by-id."""
        relations = self._load_relations()
        label = _norm_label(label)
        for r in relations:
            if r["from_vov_id"] == from_vov_id and r["to_vov_id"] == to_vov_id and r["kind"] == kind \
                    and _norm_label(r.get("label")) == label:
                r["directed"] = directed
                if propositional is not None:
                    r["propositional"] = propositional
                if affective is not None:
                    r["affective"] = affective
                if confidence is not None:
                    r["confidence"] = confidence
                if since_text is not None:
                    r["since_text"] = since_text
                if strength is not None:
                    r["strength"] = strength
                if softened_at is not None:
                    r["softened_at"] = softened_at
                break
        else:
            relations.append({
                "id": str(uuid.uuid4()),
                "from_vov_id": from_vov_id, "to_vov_id": to_vov_id, "kind": kind, "label": label,
                "directed": directed, "propositional": propositional, "affective": affective or [],
                "confidence": confidence, "since_text": since_text, "strength": strength,
                "softened_at": softened_at,
            })
        self._save_relations(relations)

    def write_relation(
        self,
        from_vov_id: str,
        to_vov_id: str,
        kind: str,
        propositional: Optional[str] = None,
        affective: Optional[list] = None,
        confidence: Optional[int] = None,
        since_text: Optional[str] = None,
        label: str = "",
        directed: Optional[bool] = None,
        strength: Optional[int] = None,
        softened_at: Optional[str] = None,
    ) -> None:
        touching = self.get_relations([from_vov_id, to_vov_id])
        _, a, b, is_directed = _plan_relation_write(touching, from_vov_id, to_vov_id, kind, label, directed)
        self._store_relation(
            a, b, kind, label, is_directed, propositional=propositional, affective=affective,
            confidence=confidence, since_text=since_text, strength=strength, softened_at=softened_at,
        )

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

    def get_identity_records(
        self,
        target_vov_ids: Optional[list] = None,
        relation_ids: Optional[list] = None,
        change_kinds: Optional[list] = None,
        attribute: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[VectorObjectValence]:
        records = []
        for raw in self._load_all().values():
            if raw.get("object_nature") != "Identity" or not raw.get("identity_record"):
                continue
            rec = VectorObjectValence.model_validate(raw)
            blk = rec.identity_record
            if target_vov_ids is not None and blk.target_vov_id not in target_vov_ids:
                continue
            if relation_ids is not None and not (blk.relation and blk.relation.relation_id in relation_ids):
                continue
            if change_kinds and blk.change_kind not in change_kinds:
                continue
            records.append(rec)
        return _filter_identity_records(records, attribute, since, until, limit)

    @contextlib.contextmanager
    def transaction(self):
        files = (self._path, self._relations_path)
        snapshot = {p: (p.read_bytes() if p.exists() else None) for p in files}
        try:
            yield
        except BaseException:
            for p, data in snapshot.items():
                if data is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(data)
            raise

    def log_cycle(
        self,
        mov_id: str,
        scenario_text: str,
        scene_subject_check_result: Optional[dict],
        graph_request_result: Optional[dict],
        tactical_scene_result: Optional[dict],
        anchor_review_results: Optional[dict],
        mainmemory_filing_result: Optional[dict],
        mov_update_result: Optional[dict],
        relations_update_result: Optional[dict],
        decision_result: Optional[dict],
        response_text: str,
        graph_of_traces: Optional[dict] = None,
        identity_update_result: Optional[dict] = None,
    ) -> None:
        entry = {
            "mov_id": mov_id,
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "scenario_data": scenario_text,
            "graph_of_traces": graph_of_traces,
            "scene_subject_check_result": scene_subject_check_result,
            "graph_request_result": graph_request_result,
            "tactical_scene_result": tactical_scene_result,
            "anchor_review_results": anchor_review_results,
            "mainmemory_filing_result": mainmemory_filing_result,
            "mov_update_result": mov_update_result,
            "relations_update_result": relations_update_result,
            "decision_result": decision_result,
            "response_text": response_text,
            "identity_update_result": identity_update_result,
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
                results.append({**entry["decision_result"], "response_text": entry.get("response_text")})
            if len(results) >= limit:
                break
        return results

    def get_recent_tactical_scene_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        if not self._log_path.exists():
            return []
        lines = self._log_path.read_text(encoding="utf-8").splitlines()
        results = []
        for line in reversed(lines):
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("mov_id") == mov_id and entry.get("tactical_scene_result"):
                results.append(entry["tactical_scene_result"])
            if len(results) >= limit:
                break
        return results

    def get_recent_scene_subject_check_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        if not self._log_path.exists():
            return []
        lines = self._log_path.read_text(encoding="utf-8").splitlines()
        results = []
        for line in reversed(lines):
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("mov_id") == mov_id and entry.get("scene_subject_check_result"):
                results.append(entry["scene_subject_check_result"])
            if len(results) >= limit:
                break
        return results

    def get_scene_subject_check_timeline(self, mov_id: str) -> List[dict]:
        if not self._log_path.exists():
            return []
        out = []
        for line in self._log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("mov_id") != mov_id or not entry.get("scene_subject_check_result") or not entry.get("logged_at"):
                continue  # a cycle logged before the times were kept cannot be placed
            out.append({**entry["scene_subject_check_result"], "at": datetime.fromisoformat(entry["logged_at"])})
        return out

    def get_recent_anchor_review_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        if not self._log_path.exists():
            return []
        lines = self._log_path.read_text(encoding="utf-8").splitlines()
        results = []
        for line in reversed(lines):
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("mov_id") == mov_id and entry.get("anchor_review_results"):
                results.append(entry["anchor_review_results"])
            if len(results) >= limit:
                break
        return results


# ---------------------------------------------------------------------------
# Deferred-write draft layer (this session's redesign, MS §11.1 sequencing)
# ---------------------------------------------------------------------------

class DraftDatabase(Database):
    """A copy-on-write draft over a real `Database`, so a whole
    ProcessMotivation cycle's worth of writes — Query 2's retrospective/
    mov_ops/mainmemory_commands, across every retrieval round, and Query
    3's own mov_ops on top of them — land in a private scratch store
    instead of the real one, and become real only once, in `commit()`,
    after every one of this cycle's own queries has fully concluded.

    Why: confirmed for real, a report Liriel ultimately judged not
    credible (a fabricated meteor-impact warning) still left an extreme,
    maximum-confidence HopeFear on record, because Query 2 committed that
    Feeling straight to the database before Query 3's fuller reading of
    the same scene — the reading that actually produced the skepticism —
    had even run. The fix the user asked for is not a new instruction
    telling some query how to judge (MS §11.1: "the intelligence dwells in
    the queries" is already true, judgment is never this module's to
    second-guess) — it is that nothing this cycle writes should be
    durable before every judgment this cycle makes has actually happened.
    A later query in the SAME cycle needs to see an earlier one's proposed
    writes as if they were already real (that is the entire point of
    letting it revise them) without them actually being real yet — hence
    a draft, not a delay.

    Every read here falls through to the real database until THIS draft
    has itself written that row/edge; every write lands only in the
    draft's own private `JsonFileDatabase` (chosen because it already
    implements this same `Database` interface end to end — no method
    below needed new logic beyond "check the draft first, else ask the
    real one," so every existing write path (_apply_mov_ops,
    _apply_retrospective, _apply_mainmemory_commands, _ensure_relation_edges,
    graph_service's own restore_object calls, ...) works against a
    DraftDatabase completely unchanged). `commit()` is the one place this
    object ever touches the real database, and it is purely mechanical —
    a replay of decisions already made, not a decision of its own — same
    standing this session already draws around `_evict_stale_clusters` and
    `_renumber_stale_objectives`: bookkeeping, never judgment.
    """

    def __init__(self, real: Database):
        self._real = real
        self._scratch_dir = Path(tempfile.mkdtemp(prefix="liriel_draft_"))
        self._scratch = JsonFileDatabase(self._scratch_dir / "draft.json")
        self._touched_ids: set = set()
        self._ensured_movs: Dict[str, Optional[str]] = {}

    def load_mov(self, mov_id: str) -> MatrixObjectsValence:
        # The real database's own load_mov (active rows only) is the cheap
        # starting point — the scratch draft only ever holds what THIS
        # cycle touched, never the whole archive, so there's no need to
        # pull the real database's archived rows too just to merge them
        # (unlike get_all_objects below, which genuinely needs both).
        merged = {vov.vov_id: vov for vov in self._real.load_mov(mov_id).objects}
        for vov in self._scratch.get_all_objects(mov_id):
            if vov.archived:
                merged.pop(vov.vov_id, None)  # drafted archive of a real-active row
            else:
                merged[vov.vov_id] = vov  # new, restored, or patched this cycle
        return MatrixObjectsValence(mov_id=mov_id, objects=list(merged.values()))

    def get_object(self, vov_id: str) -> Optional[VectorObjectValence]:
        if vov_id in self._touched_ids:
            return self._scratch.get_object(vov_id)
        return self._real.get_object(vov_id)

    def get_objects(self, vov_ids: list) -> dict:
        """Same answer as the base class's one-get_object()-per-id loop (a row this draft touched comes from the scratch store, any other from the
        real database), but the untouched ids go to the real database in ONE batch query. Without this the Graph of Traces (hundreds of nodes a
        cycle) cost two round trips per node: QA measured ~400 s of a cycle's wall clock (1,209 get_object calls, 2,468 queries at ~0.17 s each)."""
        ids = list(dict.fromkeys(vov_ids))
        untouched = [i for i in ids if i not in self._touched_ids]
        found = dict(self._real.get_objects(untouched)) if untouched else {}
        for i in ids:
            if i in self._touched_ids:
                vov = self._scratch.get_object(i)
                if vov is not None:
                    found[i] = vov
        return {i: found[i] for i in ids if i in found}

    def get_object_mov_id(self, vov_id: str) -> Optional[str]:
        if vov_id in self._touched_ids:
            return self._scratch.get_object_mov_id(vov_id)
        return self._real.get_object_mov_id(vov_id)

    def get_all_objects(self, mov_id: str) -> List[VectorObjectValence]:
        # Unlike load_mov, SEARCH (graph_service.search_memory's blind
        # fallback, and its own contextual/recency pool) genuinely needs
        # the full archive, active and archived alike — that's the one
        # case a real get_all_objects call can't be skipped.
        merged = {vov.vov_id: vov for vov in self._real.get_all_objects(mov_id)}
        merged.update({vov.vov_id: vov for vov in self._scratch.get_all_objects(mov_id)})
        return list(merged.values())

    def upsert_object(self, mov_id: str, vov: VectorObjectValence) -> None:
        self._scratch.upsert_object(mov_id, vov)
        self._touched_ids.add(vov.vov_id)

    def replace_object(self, mov_id: str, vov: VectorObjectValence) -> None:
        self._scratch.replace_object(mov_id, vov)
        self._touched_ids.add(vov.vov_id)

    def mint_vov_id(self, nickname: Optional[str], object_nature: Optional[str] = None) -> str:
        return _mint_vov_id_from(self.all_vov_ids(), nickname, object_nature)

    def all_vov_ids(self) -> List[str]:
        return list(set(self._real.all_vov_ids()) | set(self._scratch.all_vov_ids()))

    def _copy_forward(self, vov_id: str) -> Optional[VectorObjectValence]:
        """archive_object/restore_object need the row to actually exist in
        the scratch store before flipping its archived bit there (the JSON
        backend's own archive_object/restore_object are no-ops on an id it
        doesn't hold) — pull it from the real database once, the first
        time this draft ever touches it."""
        vov = self._real.get_object(vov_id)
        if vov is None:
            return None
        mov_id = self._real.get_object_mov_id(vov_id) or settings.default_mov_id
        self._scratch.upsert_object(mov_id, vov)
        return vov

    def archive_object(self, vov_id: str) -> None:
        if vov_id not in self._touched_ids and self._copy_forward(vov_id) is None:
            return
        self._scratch.archive_object(vov_id)
        self._touched_ids.add(vov_id)

    def restore_object(self, vov_id: str) -> None:
        if vov_id not in self._touched_ids and self._copy_forward(vov_id) is None:
            return
        self._scratch.restore_object(vov_id)
        self._touched_ids.add(vov_id)

    def get_relations(self, vov_ids: list, relation_kinds: Optional[list] = None) -> list:
        # Keyed by (from, to, kind, label) — a draft edge shadows the real one under the
        # exact same identity write_relation itself uses to decide "update in place" vs.
        # "a new edge" (MS §12.4). A drafted copy of an edge that already exists for real
        # reports the REAL edge's id: that id is what an Identity record cites, and the
        # draft's own bookkeeping id would mean nothing once this cycle is committed.
        merged: Dict[tuple, dict] = {}
        real_ids: Dict[tuple, object] = {}
        for row in self._real.get_relations(vov_ids, relation_kinds):
            key = _edge_key(row)
            merged[key] = row
            real_ids[key] = row.get("id")
        for row in self._scratch.get_relations(vov_ids, relation_kinds):
            key = _edge_key(row)
            merged[key] = {**row, "id": real_ids[key]} if key in real_ids else row
        return list(merged.values())

    def write_relation(
        self,
        from_vov_id: str,
        to_vov_id: str,
        kind: str,
        propositional: Optional[str] = None,
        affective: Optional[list] = None,
        confidence: Optional[int] = None,
        since_text: Optional[str] = None,
        label: str = "",
        directed: Optional[bool] = None,
        strength: Optional[int] = None,
        softened_at: Optional[str] = None,
    ) -> None:
        # Which bond this is is decided against what the cycle can SEE (the real edges
        # plus the draft's), and the draft then holds the COMPLETE bond — current fields
        # carried forward under the given ones — so commit() replays exactly the state
        # later queries have been reading.
        label = _norm_label(label)
        existing, a, b, is_directed = _plan_relation_write(
            self.get_relations([from_vov_id, to_vov_id]), from_vov_id, to_vov_id, kind, label, directed
        )
        base = existing or {}

        def pick(new, field):
            return new if new is not None else base.get(field)

        self._scratch._store_relation(
            a, b, kind, label, is_directed,
            propositional=pick(propositional, "propositional"), affective=pick(affective, "affective"),
            confidence=pick(confidence, "confidence"), since_text=pick(since_text, "since_text"),
            strength=pick(strength, "strength"), softened_at=pick(softened_at, "softened_at"),
        )

    def soften_charge(self, vov_ids: list) -> None:
        # Unlike write_relation (where get_relations' own merge already
        # gives a fresh draft edge priority over a stale real one),
        # soften_charge needs every edge touching vov_ids to actually be
        # IN the scratch store before it runs — the JSON backend's own
        # soften_charge only ever iterates its own relations file — so any
        # real edge not yet shadowed is copied forward here first.
        if not vov_ids:
            return
        scratch_keys = {_edge_key(r) for r in self._scratch.get_relations(vov_ids)}
        for row in self._real.get_relations(vov_ids):
            if _edge_key(row) in scratch_keys:
                continue
            self._scratch._store_relation(
                row["from_vov_id"], row["to_vov_id"], row["kind"], row.get("label") or "",
                bool(row.get("directed")), propositional=row.get("propositional"),
                affective=row.get("affective"), confidence=row.get("confidence"),
                since_text=row.get("since_text"), strength=row.get("strength"),
                softened_at=row.get("softened_at"),
            )
        self._scratch.soften_charge(vov_ids)

    def get_committed_object(self, vov_id: str) -> Optional[VectorObjectValence]:
        return self._real.get_object(vov_id)

    def get_committed_relations(self, vov_ids: list, relation_kinds: Optional[list] = None) -> list:
        return self._real.get_relations(vov_ids, relation_kinds)

    def drafted_vov_ids(self) -> set:
        """Every Object this cycle wrote, archived or restored so far."""
        return set(self._touched_ids)

    def drafted_relations(self) -> list:
        """Every bond this cycle wrote or softened so far, as the draft holds it."""
        return list(self._scratch._load_relations())

    def get_identity_records(
        self,
        target_vov_ids: Optional[list] = None,
        relation_ids: Optional[list] = None,
        change_kinds: Optional[list] = None,
        attribute: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[VectorObjectValence]:
        merged = {r.vov_id: r for r in self._real.get_identity_records(
            target_vov_ids=target_vov_ids, relation_ids=relation_ids, change_kinds=change_kinds)}
        merged.update({r.vov_id: r for r in self._scratch.get_identity_records(
            target_vov_ids=target_vov_ids, relation_ids=relation_ids, change_kinds=change_kinds)})
        return _filter_identity_records(list(merged.values()), attribute, since, until, limit)

    def ensure_mov(self, mov_id: str, label: Optional[str] = None) -> None:
        self._scratch.ensure_mov(mov_id, label)  # no-op on the JSON backend
        self._ensured_movs[mov_id] = label

    # Cycle history is read-only context from PAST (already-committed)
    # cycles, and logging THIS cycle is a one-time audit write, not a
    # content judgment anything downstream could still revise — neither
    # belongs in the draft; both go straight to the real database.
    def log_cycle(
        self,
        mov_id: str,
        scenario_text: str,
        scene_subject_check_result: Optional[dict],
        graph_request_result: Optional[dict],
        tactical_scene_result: Optional[dict],
        anchor_review_results: Optional[dict],
        mainmemory_filing_result: Optional[dict],
        mov_update_result: Optional[dict],
        relations_update_result: Optional[dict],
        decision_result: Optional[dict],
        response_text: str,
        graph_of_traces: Optional[dict] = None,
        identity_update_result: Optional[dict] = None,
    ) -> None:
        self._real.log_cycle(
            mov_id, scenario_text, scene_subject_check_result, graph_request_result,
            tactical_scene_result, anchor_review_results, mainmemory_filing_result, mov_update_result,
            relations_update_result, decision_result, response_text, graph_of_traces,
            identity_update_result,
        )

    def get_recent_scenario_texts(self, mov_id: str, limit: int = 3) -> List[str]:
        return self._real.get_recent_scenario_texts(mov_id, limit)

    def get_recent_decision_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        return self._real.get_recent_decision_results(mov_id, limit)

    def get_recent_tactical_scene_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        return self._real.get_recent_tactical_scene_results(mov_id, limit)

    def get_recent_scene_subject_check_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        return self._real.get_recent_scene_subject_check_results(mov_id, limit)

    def get_scene_subject_check_timeline(self, mov_id: str) -> List[dict]:
        return self._real.get_scene_subject_check_timeline(mov_id)

    def get_creation_times(self, mov_id: str, object_nature: str) -> dict:
        return self._real.get_creation_times(mov_id, object_nature)

    def get_recent_anchor_review_results(self, mov_id: str, limit: int = 200) -> List[dict]:
        return self._real.get_recent_anchor_review_results(mov_id, limit)

    def commit(self) -> None:
        """Makes the cycle durable (see _commit_once). If the connection is lost WHILE committing, the
        server has rolled the whole transaction back — nothing half-written — so it is safe to
        reconnect and commit again, once, instead of throwing away a cycle of minutes of model calls."""
        try:
            self._commit_once()
        except Exception as exc:  # noqa: BLE001
            lost = type(exc).__module__.startswith("psycopg2") and type(exc).__name__ in ("OperationalError", "InterfaceError")
            if not (lost and hasattr(self._real, "_reconnect")):
                raise
            print(f"[notice] connection lost while committing ({exc}) — the transaction was rolled back "
                  f"whole; reconnecting and committing again once")
            self._real._reconnect()
            self._commit_once()

    def _commit_once(self) -> None:
        """The only place this draft ever touches the real database —
        called once, after every one of this cycle's own queries has fully
        concluded. Purely mechanical: every judgment already happened
        while this draft was being built; this only makes it durable.

        Uses replace_object, not upsert_object, for each touched row: the
        draft's own copy is already a complete, correct final VOV (built
        through the same _coerce_patch/_coerce_vov merging every write
        path already goes through) — a plain upsert would leave a feelings
        axis this cycle actually cleared still sitting in the real row
        untouched, the exact bug replace_object was introduced to fix
        elsewhere in this pipeline (a stale Feeling surviving a
        model_copy(update={"feelings": {}})). Both backends' upsert_object
        also unconditionally clears archived_at — mirroring that would
        silently un-archive anything this draft actually decided to file
        away, so archive_object is called explicitly, right after, for
        every row whose final drafted state is archived.
        """
        # All of it in ONE transaction (Rev 0006): an Object's update and the Identity record
        # that documents it, a relation and the record of its change, reach the real
        # database together or not at all — a failure half-way rolls everything back.
        with self._real.transaction():
            for mov_id, label in self._ensured_movs.items():
                self._real.ensure_mov(mov_id, label)
            for vov_id in self._touched_ids:
                vov = self._scratch.get_object(vov_id)
                if vov is None:
                    continue
                # Captured before replace_object: JsonFileDatabase.upsert_object
                # (which replace_object falls back to on both backends' write
                # path) mutates its own `vov` argument's `.archived` to False
                # in place before writing it — checking vov.archived AFTER that
                # call would always see the post-mutation value, silently
                # dropping every archive this draft actually decided.
                was_archived = vov.archived
                mov_id = self._scratch.get_object_mov_id(vov_id) or settings.default_mov_id
                self._real.replace_object(mov_id, vov)
                if was_archived:
                    self._real.archive_object(vov_id)
            for row in self._scratch._load_relations():
                self._real.write_relation(
                    from_vov_id=row["from_vov_id"], to_vov_id=row["to_vov_id"], kind=row["kind"],
                    propositional=row.get("propositional"), affective=row.get("affective"),
                    confidence=row.get("confidence"), since_text=row.get("since_text"),
                    label=row.get("label") or "", directed=row.get("directed"),
                    strength=row.get("strength"), softened_at=row.get("softened_at"),
                )

    def cleanup(self) -> None:
        """Discards this draft's scratch files. Safe to call whether or not
        commit() ever ran — an abandoned draft (a cycle that crashed before
        concluding) simply vanishes along with it, which is correct: none
        of it ever reached the real database either."""
        shutil.rmtree(self._scratch_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_database() -> Database:
    # LIRIEL_LOCAL_STORE: an explicit local store (the Claude-session embodiment, liriel.py, and the tests set it). It wins over any
    # configured database: asking for a local file is asking not to touch the other. Unset, a configured database is used and, without
    # one, the old development fallback in the working directory (with its warning).
    configured = os.getenv("LIRIEL_LOCAL_STORE", "").strip()
    if not configured and (settings.has_discrete_pg_config or settings.database_url):
        return PostgresDatabase()
    if not configured:
        print(
            "[warning] No database configured — using local storage in "
            "'local_mov_store.json' (development only). Configure your .env "
            "and run migrations/001_init.sql + migrations/002_metascheme_alignment.sql "
            "to persist to Supabase."
        )
        return JsonFileDatabase()
    path = Path(configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    return JsonFileDatabase(path=path)
