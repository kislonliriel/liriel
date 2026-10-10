"""
The ProcessMotivation cycle, aligned to the official MetaScheme
(docs/MetaScheme_Liriel_Rev0006.md, "MS" below), MS §11.1.

The cycle is many narrow LLM calls instead of a few dense ones — the dense
queries measurably degraded toward generic chatbot reasoning under real load
(misplacing Feelings ownership, skipping required surveys, putting every
relation inside the Objects' own ops) because each one held too much
judgment at once. MS §11.1 itself says the functional set of operations is
fixed, not the number of queries. Retrospective/δ accounting is removed from
this cycle entirely, deferred to ProcessIntrospection (MS §10.8/§11/§17
point 7) — not yet implemented in this codebase.

LLM calls, in order, plus the Phase-1 reply bridge:
  1.  SCENE_SUBJECT_CHECK (MS §12.1) — same matter or new; who/what is on the board, which are hunters.
  2.  GRAPH_REQUEST (MS §12.2) — which Objects' relations are worth surveying; SEARCH.
  3.  TACTICAL_SCENE_INTERPRETATION (MS §12.3) — the scene: relations, space/time/symbol (KQ03-06).
  3A. HUNTER_READING (MS §12.3A) — ONCE PER HUNTER: Ordinances, Schemas, own MOV (KQ07-09).
  3B. ANCHOR_REVIEW (MS §12.3B) — ONCE PER OBJECT: which anchored Feelings/Schemas change (KQ10-13).
  4.  MAINMEMORY_FILING (MS §12.4) — archive/restore; the only query with this authority.
  5.  MOV_UPDATE (MS §12.6) — Objects only: mov_ops/nested_mov_ops.
  5A. RELATIONS_UPDATE (MS §12.6A) — the typed relations, once those Objects exist with real ids.
  6.  BEST_PREY_GUESS (MS §12.7) — the Guess, judged, the §6.11 audit, plus a handoff.
  6A. IDENTITY_UPDATE (MS §12.7A, Rev 0006) — ONCE PER CHANGED TARGET: what was learned or changed in a
      persistent Object or relation, documented as an Identity record. None when nothing that already
      existed changed. Runs last of the content writes, inside the same draft.
  7.  Phase-1-only: turn the handoff into Liriel's actual chat reply (no MS
      §12 contract covers this — see prompts.build_reply_prompt).

Between 2 and 3, TrackGraphProcess (graph_service.py) — a service, not an
LLM call — turns Query 2's `requests` into the real GraphOfTraces (MS §8),
traversing mov_relations (the store RELATIONS_UPDATE's write_relations/
soften_charge write to) across the focus and the archive. Two more
services run alongside it: graph_service.search_memory, once per cycle
against the raw message, and again right after Query 2 against any
`search_commands` it emitted — together a real implementation of MS §12.4
SEARCH (keyword/fuzzy matching over every Object's own text, any nature,
not just names).

Every call's DB-facing writes flow through a DraftDatabase (database.py)
— nothing is durable until the cycle's last query concludes, and then it commits in ONE
transaction (MS §11.1, §14 invariant 32); a later query's
fuller reading can still revise an earlier one's proposal because none of
it was ever final until the last had its say.

Backend (config.py's LLM_BACKEND, default "litellm" in this fork): all
calls run through llm_client.py/litellm to Groq by default (see
.env.example) — a larger hosted model, after a smaller self-hosted one
kept missing nuanced MetaScheme distinctions even with explicit,
worked-example-level prompting. Setting LLM_BACKEND=llamacpp switches all
calls to a self-hosted llama-server (scripts/llamacpp/) instead — every
inference stays under the implementer's own control, no closed external
API sees Liriel's state (MS §17.6 "weight ownership"); this was the
original project's default. Voice notes (Telegram STT/TTS) always go
through OpenAI via llm_client.py regardless of this setting — that's I/O
plumbing, not cognition, and the cost there is negligible.

Under LLM_BACKEND=litellm, the structured-JSON calls (not the voice
the user actually hears) run on config.py's LLM_MODEL_STRUCTURED (a
cheaper/faster model, when set), while the reply bridge stays on
LLM_MODEL, since that's the one call where model quality is directly
audible/readable to the user. Both default to the same value, so this is
opt-in (.env), not a behavior change on its own. llamacpp_client.py has no
equivalent split — one local server serves all of them.
"""
from __future__ import annotations

import contextvars
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional, get_args

from pydantic import ValidationError

import graph_service
from config import settings
from database import Database, DraftDatabase, _norm_label, _plain, _plan_relation_write

# config.LLM_BACKEND picks which of the two ends up bound to chat/chat_json
# here — both modules expose the same (messages, temperature, effort, model)
# signature, so nothing below this needs to know which one it's actually
# talking to. "llamacpp" is the self-hosted, local-only path
# (scripts/llamacpp/); "litellm" (this fork's default) routes through
# llm_client.py/litellm to whatever LLM_MODEL's own prefix names — Groq by
# default here, but Anthropic/OpenAI/etc. work the same way.
if settings.llm_backend == "litellm":
    from llm_client import chat, chat_json
elif settings.llm_backend == "claude_session":
    # a third path: no model API, no local server -- a Claude session answers each request through a folder
    # (claude_session_client.py, served by scripts/claude_session/session_cli.py)
    from claude_session_client import chat, chat_json
else:
    from llamacpp_client import chat, chat_json
from models import (
    INTERPELLATION_ROLES,
    AnchorReviewResult,
    AnchorTarget,
    Artifacts,
    AxisValence,
    BestPreyGuessResult,
    DeltaReport,
    ElementEntry,
    GraphRequestResult,
    HunterReadingResult,
    HunterSceneReading,
    HunterTarget,
    IdentityLedgerChange,
    IdentityRecordBlock,
    IdentityTarget,
    IdentityUpdateResult,
    MainMemoryFilingResult,
    MatrixObjectsValence,
    MovOp,
    MovUpdateResult,
    NestedMovOp,
    ObjectiveBlock,
    RelationKind,
    RelationRef,
    RelationsUpdateResult,
    SafetyScreenResult,
    ScenarioData,
    SceneSubjectCheckResult,
    SchemaEntry,
    TacticalSceneInterpretationResult,
    VectorObjectValence,
)
from models import _clamp_confidence_value
from valence_text import (
    convert_entry_v,
    feeling_to_text,
    is_unconverted_numeric,
    schema_to_text,
    text_to_feeling,
)

_RELATION_KINDS = set(get_args(RelationKind))
from prompts import (
    META_SCHEME,
    _cycle_id,
    build_anchor_review_batch_prompt,
    build_anchor_review_prompt,
    build_decision_prompt,
    build_graph_request_prompt,
    build_hunter_reading_batch_prompt,
    build_hunter_reading_prompt,
    build_identity_update_batch_prompt,
    build_identity_update_prompt,
    build_mainmemory_filing_prompt,
    build_mov_update_prompt,
    build_relations_update_prompt,
    build_reply_prompt,
    build_voice_delivery_prompt,
    build_party_objection,
    build_safety_screen_prompt,
    build_scene_subject_check_prompt,
    build_tactical_scene_interpretation_prompt,
)


def _archived_in_graph(db: Database, graph_of_traces: Optional[dict]) -> list:
    """Rev 0007 T. The Objects that are ARCHIVED among the nodes the retrieval put in this cycle's graph, as {vov_id,
    object_nature, description} -- so Query 4 (the only query that may bring one back, MS §14 invariant 24) is handed them
    plainly instead of having to find them inside the graph. A lookup of facts (the row is archived or it is not); which
    of them the message is about stays Query 4's judgment."""
    return _archived_among(db, [n.get("vov_id") for n in (graph_of_traces or {}).get("nodes", []) or []])


def _run_safety_screen(mov: MatrixObjectsValence, scenario: ScenarioData) -> Optional[SafetyScreenResult]:
    """Rev 0007 AR. SAFETY_SCREEN: a fresh, narrow read of the message ALONE, before anything else (no memory, no household, nothing standing).
    ADVISORY: it adds an artifact to the decision and the reply; if the call or its validation fails, the cycle goes on as it did before it existed."""
    try:
        artifacts = Artifacts(meta_scheme=META_SCHEME, mov=MatrixObjectsValence(mov_id=mov.mov_id, objects=[]), scenario_data=scenario)
        raw = chat_json(
            build_safety_screen_prompt(artifacts),
            temperature=settings.llm_temperature_update,
            effort=settings.llm_effort_update,
            model=settings.llm_model_structured,
        )
        result = SafetyScreenResult.model_validate(raw)
        if result.found:
            print(f"[notice] SAFETY_SCREEN found a risk: {result.at_risk!r} — {result.what!r}")
        return result
    except Exception as exc:  # noqa: BLE001 - advisory
        print(f"[warning] SAFETY_SCREEN failed ({type(exc).__name__}: {exc}) — the cycle goes on without it")
        return None


def _recheck_party(messages: list, raw: dict, interlocutor: Optional[str]) -> dict:
    """Rev 0007 AT. The decision's own answer said the Guess is about a matter the interlocutor is not a party to (`interlocutor_is_party: no`): the architecture's
    rule is that such an Objective stays standing and is not this reply's Guess. Ask ONCE more, showing the model its own answer and the rule against it. Reads a
    declaration the model made; judges no content. A second answer that is unusable leaves the first as it was."""
    try:
        first = BestPreyGuessResult.model_validate(raw)
    except Exception:  # noqa: BLE001 - the normal validation path reports it
        return raw
    if not first.declares_not_a_party:
        return raw
    # The model named the interlocutor herself as the owner of the matter and then said she is not a party to it: its own two declarations disagree
    # (QA, limites step 6: matter "Visitante", interlocutor "Visitante", party "no"). There is nothing to put to it; the first answer stands. A lexical
    # comparison of the two names (case, accents, punctuation folded; one inside the other, so "Seu Osvaldo" meets "Sentient_Seu_Osvaldo").
    matter_key, who_key = _id_key(first.guess_matter_of or ""), _id_key(interlocutor or "")
    if matter_key and who_key and (matter_key in who_key or who_key in matter_key):
        print(f"[notice] BEST_PREY_GUESS: matter of {first.guess_matter_of!r} and interlocutor {interlocutor!r} are the same name, yet 'not a party' — the answer stands")
        return raw
    print(f"[notice] BEST_PREY_GUESS: the Guess {first.best_prey_guess.vov_id!r} is about {first.guess_matter_of!r}'s matter and the interlocutor "
          f"{interlocutor!r} is not a party — asking once more")
    try:
        again = chat_json(
            list(messages) + [
                {"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)},
                {"role": "user", "content": build_party_objection(first.best_prey_guess.vov_id, first.guess_matter_of, interlocutor)},
            ],
            temperature=settings.llm_temperature_decision,
            effort=settings.llm_effort_decision,
            model=settings.llm_model_structured,
        )
        BestPreyGuessResult.model_validate(again)
        return again
    except Exception as exc:  # noqa: BLE001 - the first answer stands
        print(f"[warning] BEST_PREY_GUESS second pass failed ({type(exc).__name__}: {exc}) — the first answer stands")
        return raw


def _id_key(vov_id: str) -> str:
    """Case, accents and punctuation folded away: `Sentient_Júlia_Neteta` and `Sentient_Julia_Neteta` are the same string of an id."""
    return re.sub(r"[^0-9a-z]", "", _plain(vov_id))


def _canonical_scene_ids(db: Database, check: SceneSubjectCheckResult) -> SceneSubjectCheckResult:
    """Rev 0007 AU. An id Query 1 wrote that no row has, but that equals exactly ONE existing id once case, accents and punctuation are folded
    away, is that id mistyped (QA: `Sentient_Júlia_Neteta` for the row `Sentient_Julia_Neteta` -> a second Julia was minted). Lexical identity of
    strings, not a judgment about who a person is; an exact id is kept as it is, and two candidates leave the id untouched."""
    known = set(db.all_vov_ids())
    by_key: Dict[str, list] = {}
    for existing in known:
        by_key.setdefault(_id_key(existing), []).append(existing)

    def fix(vov_id: Optional[str]) -> Optional[str]:
        if not vov_id or vov_id in known:
            return vov_id
        hits = by_key.get(_id_key(vov_id), [])
        if len(hits) == 1:
            print(f"[notice] Query 1 wrote {vov_id!r}, which no row has; read as the existing {hits[0]!r} (same id once accents, case and punctuation are folded)")
            return hits[0]
        return vov_id

    for element in check.elements:
        element.vov_id = fix(element.vov_id)
    check.interlocutor = fix(check.interlocutor)
    check.continuing_scenario_data_id = fix(check.continuing_scenario_data_id)
    return check


def _author_unknown(interlocutor: Optional[str]) -> bool:
    """Query 1's own answer for 'nothing in the message tells who is writing' (MS §12.1)."""
    return (interlocutor or "").strip().lower().startswith("unknown")


def _need_to_know(mov: MatrixObjectsValence, nested_movs: list, graph_of_traces: Optional[dict], interlocutor: Optional[str]):
    """Rev 0007 AK. What the decision and the reply are SHOWN of the household. When the author is known, all of it. When Query 1 said
    'unknown', only Liriel's own row — no other person, no nested MOV, no graph: what a stranger's reply is not shown it cannot repeat."""
    if not _author_unknown(interlocutor):
        return mov, nested_movs, graph_of_traces
    own = [o for o in mov.objects if o.vov_id == settings.liriel_self_vov_id]
    return MatrixObjectsValence(mov_id=mov.mov_id, objects=own), [], None


def _archived_candidates(db: Database, mov: MatrixObjectsValence, auto_hits: list) -> list:
    """Rev 0007 W2. What Query 1 is handed as 'on record but not in focus': EVERY archived agent of this MOV (people, animals, groups
    -- few, and what a conversation keeps returning to) plus the archived hits of the automatic search (any nature). The automatic
    search alone keeps six candidates ranked by matched words and, where one name fills dozens of rows, never reaches the others."""
    agents = [o.vov_id for o in db.get_all_objects(mov.mov_id) if o.archived and o.object_nature in _AGENT_OBJECT_NATURES]
    return _archived_among(db, agents + list(auto_hits))


def _archived_among(db: Database, vov_ids: list) -> list:
    """Of these ids, the Objects that are ARCHIVED (and not Identity records), each once, as {vov_id, object_nature,
    brief_description}. A lookup of facts."""
    out, seen = [], set()
    for vid in vov_ids:
        if not vid or vid in seen:
            continue
        seen.add(vid)
        row = db.get_object(vid)
        if row is not None and row.archived and row.object_nature != "Identity":
            out.append({"vov_id": row.vov_id, "object_nature": row.object_nature, "brief_description": row.brief_description})
    return out


def _validate_or_log(model_cls, raw: dict, label: str):
    """`model_cls.model_validate(raw)`, but on failure prints the raw JSON
    first before re-raising. A validation error here (e.g. one out-of-range
    field somewhere in an otherwise-valid ~2000-token object) previously
    lost the model's actual output entirely — it's never written to
    motivation_cycles (that only happens after every step of the cycle
    succeeds), so a crash mid-cycle left no way to tell what the model
    actually said, including its own `notes` explaining its reasoning.
    telegram_bot.py/main.py already catch and report the exception; this
    only adds the diagnostic that was missing."""
    try:
        return model_cls.model_validate(raw)
    except ValidationError as exc:
        print(f"[error] {label} failed validation — raw model output:\n{raw}")
        raise exc


def _validate_batch_entry(model_cls, entry: Optional[dict], label: str):
    """One entry of a batch answer (Rev 0008 §12.12) read as the item's own result: None when the batch carried no entry for it, or
    when the entry does not validate -- the caller then asks that item alone, as without batch mode."""
    if entry is None:
        return None
    try:
        return model_cls.model_validate(entry)
    except ValidationError as exc:
        print(f"[notice] {label}: its entry in the batch does not validate ({exc.error_count()} error(s)); asking it alone")
        return None


# While MOV_UPDATE's ops are applied, an untyped `relevant_relations` fallback edge
# (_ensure_relation_edges) must NOT be written yet: RELATIONS_UPDATE runs right after and
# writes the TYPED bonds, and a fallback written first would sit beside the typed one as
# an empty parallel bond. The ids wait here and are linked once RELATIONS_UPDATE is done
# — then only pairs it left with no bond at all get the fallback.
_DEFERRED_LINKS: contextvars.ContextVar = contextvars.ContextVar("_DEFERRED_LINKS", default=None)


def _link_or_defer(db: Database, vov: VectorObjectValence) -> None:
    pending = _DEFERRED_LINKS.get()
    if pending is not None:
        pending.add(vov.vov_id)
    else:
        _ensure_relation_edges(db, vov)


def _flush_relation_fallbacks(db: Database, pending: set) -> None:
    for vov_id in sorted(pending):
        vov = db.get_object(vov_id)
        if vov is not None:
            _ensure_relation_edges(db, vov)
    pending.clear()


def run_motivation_cycle(
    db: Database, mov: MatrixObjectsValence, user_text: str, source: str = "terminal_chat",
    sender: Optional[str] = None,
) -> tuple[str, MatrixObjectsValence, Optional[str]]:
    """Runs one full ProcessMotivation cycle for a single chat message.

    `source` labels ScenarioData.source (MS §9.3 leaves the embodiment's
    form open) — e.g. "telegram" when telegram_bot.py is the front end
    instead of main.py's terminal loop; purely informational for now.
    `sender` (Rev 0007 V) is who the CHANNEL says wrote the message — a name,
    label or vov_id the embodiment vouches for; None means it does not say and
    Query 1 reads the author from the text, as before.

    Returns (response_text, refreshed_mov, output_modality) — the third
    element is "voice"/"text" only when the user explicitly asked for that
    reply channel (MS §11: ProcessCommandControl's call, via the Query 3
    handoff), else None, meaning the front end should mirror whatever
    channel the message itself arrived on.

    MS §11.1 sequencing (this session's redesign, database.DraftDatabase):
    everything below runs against a private draft, not `db` itself — every
    write Query 1's own recall (RESTORE_VOV), Query 2 (retrospective,
    mov_ops, mainmemory_commands, across every retrieval round) and Query 3
    (its own mov_ops) make this cycle lands there, and only there. Query 3
    still reads all of it as if it were already real (the draft answers
    every read the same way the real database would) and can PATCH_VOV
    over anything Query 2 proposed before any of it is durable — that is
    the entire point: this cycle's judgment does not finish until Query 3
    concludes, so nothing this cycle writes should be final before then
    either. `db.commit()` below, once Query 3's own writes are in, is the
    single moment any of it reaches the real database.
    """
    real_db = db
    db = DraftDatabase(real_db)
    try:
        return _run_motivation_cycle_drafted(db, mov, user_text, source, sender)
    finally:
        db.cleanup()


def _run_motivation_cycle_drafted(
    db: DraftDatabase, mov: MatrixObjectsValence, user_text: str, source: str, sender: Optional[str] = None
) -> tuple[str, MatrixObjectsValence, Optional[str]]:
    """The actual cycle body: six queries (this revision's redesign, MS
    §11.1) run in sequence, every write going through the DraftDatabase
    `run_motivation_cycle` built; no write below reaches the real database
    until db.commit(), after Query 6 concludes."""
    scenario = ScenarioData(text=user_text, source=source, sender=sender)
    safety_screen = _run_safety_screen(mov, scenario)
    nested_movs = _load_nested_movs(db, mov, settings.nested_mov_max_depth)

    # A short window of recent conversation, not just this one message in
    # isolation -- resolves a pronoun-based follow-up ("quem é o marido
    # DELA?") that carries no name of its own but clearly continues what
    # the last turn was about. Confirmed for real: without this, a
    # follow-up like that scored zero relevance against every Object in
    # MainMemory, including the very Person the conversation was already
    # about (whose own row had, by then, aged out of the cheap contextual
    # pool too). Used for automatic search and relevance ranking only —
    # log_cycle below still stores just this message, and Query 2's own
    # deliberate SEARCH command (run_search_commands) keeps using only the
    # terms the model itself chose.
    recent_texts = db.get_recent_scenario_texts(mov.mov_id, limit=3)
    search_context_text = "\n".join(list(reversed(recent_texts)) + [user_text]) if recent_texts else user_text

    # MS §12.4 SEARCH, run automatically every cycle against that context:
    # keyword/fuzzy-matches every Object in MainMemory (any nature, active
    # or archived) so something nothing currently in focus points to (a
    # fresh speaker, a topic raised again after weeks, a name typed with a
    # different spelling) can still be found. This only finds *ids* — the
    # graph built below is what pulls in their relations too,
    # automatically, not gated on the model remembering to ask for them
    # (confirmed for real: it doesn't always ask).
    auto_results = graph_service.search_memory(db, mov, search_context_text)
    auto_hits = [c["vov_id"] for c in auto_results]
    if settings.verbose and auto_hits:
        print(f"[debug] memory search hits: {auto_hits}")
    auto_requests = _requests_with_cluster_recall(db, auto_results, reason="auto: matched by memory search")

    # --- Query 1: SCENE_SUBJECT_CHECK (MS §12.1) ---------------------------
    # Run before the Graph of Traces exists, same timing MS §8.2 already
    # required of the old GRAPH_REQUEST. Decides whether this is the same
    # matter continuing or a new one, and names the Savanna's elements —
    # threaded into every later query of this cycle as `scene_elements`,
    # so none of them re-derives who/what is in play independently.
    scene_artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
        graph_of_traces=None, scenario_data=scenario,
        archived_candidates=_archived_candidates(db, mov, auto_hits),
    )
    scene_subject_raw = chat_json(
        build_scene_subject_check_prompt(scene_artifacts),
        temperature=settings.llm_temperature_update,
        effort=settings.llm_effort_graph,
        model=settings.llm_model_structured,
    )
    scene_subject_check = _validate_or_log(SceneSubjectCheckResult, scene_subject_raw, "SCENE_SUBJECT_CHECK")
    scene_subject_check = _canonical_scene_ids(db, scene_subject_check)
    scene_elements = scene_subject_check.elements
    interlocutor = scene_subject_check.interlocutor

    if settings.verbose:
        print(f"[debug] SCENE_SUBJECT_CHECK: {scene_subject_check.model_dump_json(indent=2)}")

    # --- Query 2: GRAPH_REQUEST (MS §12.2) ---------------------------------
    # MS §7.6: may ask for another round of its own retrieval against
    # whatever the previous round turned up, bounded by
    # max_retrieval_subqueries. Unlike Rev 0000's single dense Query 2,
    # this query writes nothing to the MOV itself — only graph_of_traces
    # changes between rounds, so there is no reload of `mov`/`nested_movs`
    # inside this loop.
    graph_request: Optional[GraphRequestResult] = None
    graph_of_traces: Optional[dict] = None
    for retrieval_round in range(settings.max_retrieval_subqueries):
        graph_artifacts = Artifacts(
            meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
            graph_of_traces=graph_of_traces,
            scenario_data=scenario, scene_elements=scene_elements,
        )
        graph_request_raw = chat_json(
            build_graph_request_prompt(graph_artifacts),
            temperature=settings.llm_temperature_update,
            effort=settings.llm_effort_graph,
            model=settings.llm_model_structured,
        )
        graph_request = _validate_or_log(GraphRequestResult, graph_request_raw, "GRAPH_REQUEST")

        if settings.verbose:
            print(f"[debug] GRAPH_REQUEST (retrieval round {retrieval_round + 1}"
                  f"/{settings.max_retrieval_subqueries}): {graph_request.model_dump_json(indent=2)}")

        # deep_recall_requested (MS §12.1 origin, now on GraphRequestItem):
        # the user explicitly insisted Liriel make a real effort to
        # remember something. Redo the automatic search with
        # force_blind=True so an explicit "please really try to remember"
        # always gets the real, unrestricted lookup, not just a bigger cap
        # on whatever the cheap pass already (maybe wrongly) settled on.
        if any(r.deep_recall_requested for r in graph_request.requests):
            auto_results = graph_service.search_memory(db, mov, search_context_text, force_blind=True)
            auto_hits = [c["vov_id"] for c in auto_results]
            if settings.verbose:
                print(f"[debug] deep_recall_requested — forced blind search hits: {auto_hits}")
            auto_requests = _requests_with_cluster_recall(
                db, auto_results, reason="auto: deep recall, forced blind search"
            )

        # --- Graph generation: TrackGraphProcess (service, not an LLM call) ---
        graph_of_traces = graph_service.build_graph_of_traces(
            db, list(graph_request.requests) + auto_requests, scenario_text=search_context_text
        )

        # MS §12.4 SEARCH, run for real with this round's own terms — this is
        # what lets Liriel "think about it some more" instead of having to
        # resolve an unrecognized name/topic in one shot.
        search_hits = [c["vov_id"] for c in graph_service.run_search_commands(db, mov, graph_request.search_commands)]
        if search_hits:
            if settings.verbose:
                print(f"[debug] Query 2 SEARCH hits: {search_hits}")
            graph_of_traces = graph_service.build_graph_of_traces(
                db,
                list(graph_request.requests) + auto_requests
                + graph_service.requests_from_ids(search_hits, reason="Query 2's own SEARCH"),
                scenario_text=search_context_text,
            )
        else:
            search_queries = [
                cmd.get("query") for cmd in graph_request.search_commands
                if isinstance(cmd, dict) and cmd.get("query")
            ]
            if search_queries:
                # MS §8.6: a SEARCH that finds nothing is the exact signal that
                # something is genuinely new — it is not a dead end to drop
                # silently.
                graph_of_traces = dict(graph_of_traces) if graph_of_traces else {
                    "artifact": "GraphOfTraces", "requested_for": [], "nodes": [], "edges": [],
                }
                graph_of_traces["unresolved_searches"] = search_queries

        # Rev 0006 (MS §12.2): the Identity records the model asked for, answered
        # selectively and bounded — never the whole history of an Object.
        if graph_request.identity_requests:
            graph_of_traces = dict(graph_of_traces) if graph_of_traces else {
                "artifact": "GraphOfTraces", "requested_for": [], "nodes": [], "edges": [],
            }
            graph_of_traces["identity_records"] = graph_service.retrieve_identity_records(
                db, graph_request.identity_requests
            )

        if settings.verbose and graph_of_traces:
            print(f"[debug] GraphOfTraces: {len(graph_of_traces.get('nodes', []))} node(s), "
                  f"{len(graph_of_traces.get('edges', []))} edge(s)")

        if graph_request.retrieval_satisfied:
            break
        if retrieval_round == settings.max_retrieval_subqueries - 1:
            print(f"[notice] GRAPH_REQUEST hit MAX_RETRIEVAL_SUBQUERIES="
                  f"{settings.max_retrieval_subqueries} while still wanting more retrieval "
                  f"(retrieval_satisfied=false) — proceeding with what was retrieved this cycle")

    # --- Query 3: TACTICAL_SCENE_INTERPRETATION (MS §12.3) -----------------
    # Run only now that the Graph of Traces exists. Produces the full
    # Current Tactical Scene (MS §9.4), carried forward as a shared
    # Artifact into Queries 4-6 — none of them re-derives it. Read-only:
    # no mov_ops, no mainmemory writes of any kind.
    tactical_scene_artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
        graph_of_traces=graph_of_traces, scenario_data=scenario, scene_elements=scene_elements,
    )
    tactical_scene_raw = chat_json(
        build_tactical_scene_interpretation_prompt(tactical_scene_artifacts),
        temperature=settings.llm_temperature_update,
        effort=settings.llm_effort_update,
        model=settings.llm_model_structured,
    )
    current_tactical_scene = _validate_or_log(
        TacticalSceneInterpretationResult, tactical_scene_raw, "TACTICAL_SCENE_INTERPRETATION"
    )

    if settings.verbose:
        print(f"[debug] TACTICAL_SCENE_INTERPRETATION: {current_tactical_scene.model_dump_json(indent=2)}")

    # Rev 0007 AW: the people and things Query 1 named as the scene's elements come back from the archive NOW, before the hunters are read and the
    # rows reviewed -- not after Query 4. The person who is writing was, in 8 of 9 QA steps, archived at this point, so her own row (feelings,
    # Schemas) was not looked at until the next time she wrote. Query 4 may still file a row away; the same restoration after it brings it back.
    _restore_scene_elements(db, scene_elements)
    mov = db.load_mov(mov.mov_id)
    nested_movs = _load_nested_movs(db, mov, settings.nested_mov_max_depth)

    # --- Query 3A: HUNTER_READING (MS §12.3A) — KQ07-09, ONCE PER HUNTER -----
    # A Savanna can hold many hunters; one query cannot read each in the detail
    # MS §4.8 asks for, so every hunter — each element Query 1 flagged, Liriel
    # always — gets its own call, with the scene Query 3 just read (board and
    # relations, KQ03-06) as shared context. The readings are assembled into
    # the Current Tactical Scene's `hunters`, so every later query reads the
    # complete scene.
    cycle_id = _cycle_id(scenario)
    hunter_artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
        graph_of_traces=graph_of_traces, scenario_data=scenario,
        scene_elements=scene_elements, current_tactical_scene=current_tactical_scene,
    )
    hunter_readings = _run_hunter_readings(hunter_artifacts, _hunter_targets(db, mov, scene_elements), cycle_id)
    current_tactical_scene = current_tactical_scene.model_copy(update={"hunters": hunter_readings})

    # --- Query 3B: ANCHOR_REVIEW (MS §12.3B) — KQ10-13, ONCE PER OBJECT ------
    # A MOV can hold dozens of Objects; one query cannot give each the detail
    # this judgment needs, so every reviewable Object — in Liriel's MOV and in
    # every already-materialized nested MOV — gets its own call. They are
    # independent (each reads only the shared Current Tactical Scene and its
    # own row), applied mechanically as soon as all have answered: ETAPA 1's
    # last act, so MAINMEMORY_FILING and MOV_UPDATE both see the updated
    # valences (and nothing is archived yet, so no review can name a row that
    # filing is about to move).
    review_artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
        graph_of_traces=graph_of_traces, scenario_data=scenario,
        scene_elements=scene_elements, current_tactical_scene=current_tactical_scene,
        interlocutor=interlocutor,
    )
    anchor_reviews = _run_anchor_reviews(review_artifacts, _anchor_targets(mov, nested_movs), cycle_id)
    _apply_anchor_reviews(db, mov.mov_id, anchor_reviews)
    mov = db.load_mov(mov.mov_id)  # reflects every KQ10-13 update just applied
    nested_movs = _load_nested_movs(db, mov, settings.nested_mov_max_depth)

    # --- Query 4: MAINMEMORY_FILING (MS §12.4) -----------------------------
    # The one query with standing authority to move an Object's
    # MOV<->MainMemory membership this revision (MS §14 invariant 24).
    filing_artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
        graph_of_traces=graph_of_traces, scenario_data=scenario,
        current_tactical_scene=current_tactical_scene, scene_elements=scene_elements,
        archived_in_graph=_archived_in_graph(db, graph_of_traces), interlocutor=interlocutor,
    )
    filing_raw = chat_json(
        build_mainmemory_filing_prompt(filing_artifacts),
        temperature=settings.llm_temperature_update,
        effort=settings.llm_effort_update,
        model=settings.llm_model_structured,
    )
    filing_result = _validate_or_log(MainMemoryFilingResult, filing_raw, "MAINMEMORY_FILING")

    if settings.verbose:
        print(f"[debug] MAINMEMORY_FILING: {filing_result.model_dump_json(indent=2)}")

    _apply_mainmemory_filing(db, filing_result)
    _restore_scene_elements(db, scene_elements)
    mov = db.load_mov(mov.mov_id)  # reflects whatever this query just archived/restored
    nested_movs = _load_nested_movs(db, mov, settings.nested_mov_max_depth)

    # --- Query 5: MOV_UPDATE (MS §12.6) ------------------------------------
    mov_update_artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
        graph_of_traces=graph_of_traces, scenario_data=scenario,
        current_tactical_scene=current_tactical_scene, anchor_reviews=anchor_reviews,
        scene_elements=scene_elements,
    )
    mov_update_raw = chat_json(
        build_mov_update_prompt(mov_update_artifacts),
        temperature=settings.llm_temperature_update,
        effort=settings.llm_effort_update,
        model=settings.llm_model_structured,
    )
    mov_update_result = _validate_or_log(MovUpdateResult, mov_update_raw, "MOV_UPDATE")

    if settings.verbose:
        print(f"[debug] MOV_UPDATE: {mov_update_result.model_dump_json(indent=2)}")

    pending_links: set = set()
    defer_token = _DEFERRED_LINKS.set(pending_links)
    try:
        id_remap = _apply_mov_ops(db, mov.mov_id, mov_update_result.mov_ops)
        if id_remap:
            _relink_new_objects(db, mov.mov_id, id_remap)
            mov_update_result.nested_mov_ops = [
                op.model_copy(update={"owner_vov_id": id_remap.get(op.owner_vov_id, op.owner_vov_id)})
                if op.owner_vov_id else op
                for op in mov_update_result.nested_mov_ops
            ]
        _apply_nested_mov_ops(db, mov_update_result.nested_mov_ops)
    finally:
        _DEFERRED_LINKS.reset(defer_token)
    all_cycle_touched_ids = _mov_ops_touched_ids(mov_update_result.mov_ops, id_remap)

    mov = db.load_mov(mov.mov_id)  # reflects everything MOV_UPDATE just did
    # nested_mov_ops may have just created/changed one — recompute rather
    # than reuse the pre-query list.
    nested_movs = _load_nested_movs(db, mov, settings.nested_mov_max_depth)

    # --- Query 5A: RELATIONS_UPDATE (MS §12.6A) ----------------------------
    # The typed relations, written only now that MOV_UPDATE has made every
    # Object of this cycle real: the MOV the model reads below holds real ids
    # only, so no relation ever names an Object that has not been minted yet
    # (the forward-reference problem MOV_UPDATE used to carry).
    relations_artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
        graph_of_traces=graph_of_traces, scenario_data=scenario,
        current_tactical_scene=current_tactical_scene,
        written_vov_ids=sorted(all_cycle_touched_ids),
    )
    relations_raw = chat_json(
        build_relations_update_prompt(relations_artifacts),
        temperature=settings.llm_temperature_update,
        effort=settings.llm_effort_update,
        model=settings.llm_model_structured,
    )
    relations_update_result = _validate_or_log(RelationsUpdateResult, relations_raw, "RELATIONS_UPDATE")

    if settings.verbose:
        print(f"[debug] RELATIONS_UPDATE: {relations_update_result.model_dump_json(indent=2)}")

    _apply_write_relations(db, relations_update_result.write_relations)
    _apply_soften_charge(db, relations_update_result.soften_charge)
    _flush_relation_fallbacks(db, pending_links)  # only now: pairs RELATIONS_UPDATE left unconnected

    # --- Query 5B: ANCHOR_REVIEW of the rows BORN this cycle (MS §12.3B, Rev 0007) -----
    # Query 3B could only review rows that existed before the cycle. A row MOV_UPDATE just
    # created therefore had its Feelings set in the same dense call that created it — and QA
    # found that call anchoring the charge on whoever it happened to (the person in crisis,
    # not the crisis), copying a third party's own state onto that party's row, and never
    # minting the Situation that is the cause. The same one-judgment-per-Object review now runs
    # once the scene exists as Objects and bonds; MOV_UPDATE writes no Feelings at all.
    born_targets = _born_anchor_targets(db, mov, nested_movs)
    if born_targets:
        born_ids = [t.vov.vov_id for t in born_targets]
        born_graph = graph_service.build_graph_of_traces(
            db, graph_service.requests_from_ids(born_ids, reason="rows born this cycle", depth=1),
            scenario_text=search_context_text,
        )
        born_artifacts = Artifacts(
            meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
            graph_of_traces=born_graph or graph_of_traces, scenario_data=scenario,
            scene_elements=scene_elements, current_tactical_scene=current_tactical_scene,
            interlocutor=interlocutor,
        )
        born_reviews = _run_anchor_reviews(born_artifacts, born_targets, cycle_id)
        _apply_anchor_reviews(db, mov.mov_id, born_reviews)
        anchor_reviews = list(anchor_reviews) + born_reviews
        mov = db.load_mov(mov.mov_id)  # reflects the Feelings just decided
        nested_movs = _load_nested_movs(db, mov, settings.nested_mov_max_depth)

    # --- Query 6: BEST_PREY_GUESS (MS §12.7) -------------------------------
    shown_mov, shown_nested, shown_graph = _need_to_know(mov, nested_movs, graph_of_traces, interlocutor)
    decision_artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=shown_mov, nested_movs=shown_nested,
        graph_of_traces=shown_graph, scenario_data=scenario,
        current_tactical_scene=current_tactical_scene, interlocutor=interlocutor, safety_screen=safety_screen,
    )
    decision_messages = build_decision_prompt(decision_artifacts)
    decision_raw = chat_json(
        decision_messages,
        temperature=settings.llm_temperature_decision,
        effort=settings.llm_effort_decision,
        model=settings.llm_model_structured,
    )
    decision_raw = _recheck_party(decision_messages, decision_raw, interlocutor)
    decision_result = _validate_or_log(BestPreyGuessResult, decision_raw, "BEST_PREY_GUESS")

    if settings.verbose:
        print(f"[debug] BEST_PREY_GUESS: {decision_result.model_dump_json(indent=2)}")

    # MS §14 invariant 6: exactly one Objective at priority 1, priorities
    # unique and contiguous — enforce it rather than trust a small local
    # model's count. `cycle_touched_ids` is MOV_UPDATE's own touched set —
    # the only legitimate basis for re-electing an id that already belongs
    # to a standing Objective (see _resolve_id's own docstring).
    cycle_touched_ids = all_cycle_touched_ids
    proposed_guess_id = decision_result.best_prey_guess.vov_id
    standing = None
    if decision_result.continues_objective:
        # Rev 0007: the model judged this Guess the SAME pursuit as a standing Objective and named
        # it; the architecture only carries that out (update in place, one more cycle open) —
        # whether two pursuits are the same is never decided here. Without it, every cycle that
        # re-elected a standing aim minted a duplicate row and pushed the original down the ranks.
        standing = db.get_object(decision_result.continues_objective)
        if standing is not None and standing.object_nature == "Objective" and not standing.archived:
            proposed_guess_id = standing.vov_id
            cycle_touched_ids = set(cycle_touched_ids) | {standing.vov_id}
        else:
            print(f"[notice] BEST_PREY_GUESS continues_objective={decision_result.continues_objective!r} is not "
                  f"a standing, active Objective — treated as a new one")
            standing = None
    guess = decision_result.best_prey_guess.model_copy(
        update={
            "vov_id": _resolve_id(db, proposed_guess_id, cycle_touched_ids),
            "priority": 1, "valence_regime": "Delta", "object_nature": "Objective",
        }
    )
    if standing is not None and standing.objective is not None and guess.objective is not None:
        guess = guess.model_copy(update={"objective": guess.objective.model_copy(
            update={"cycles_open": (standing.objective.cycles_open or 0) + 1})})
    _upsert_and_link(db, mov.mov_id, guess)
    mov.upsert(guess)
    ranked_ids = [guess.vov_id]
    for i, obj in enumerate(decision_result.accompanying_objectives):
        obj = obj.model_copy(
            update={
                "vov_id": _resolve_id(db, obj.vov_id, cycle_touched_ids),
                "priority": i + 2, "valence_regime": "Delta", "object_nature": "Objective",
            }
        )
        _upsert_and_link(db, mov.mov_id, obj)
        mov.upsert(obj)
        ranked_ids.append(obj.vov_id)

    decision_id_remap = _apply_mov_ops(db, mov.mov_id, decision_result.mov_ops)
    if decision_id_remap:
        _relink_new_objects(db, mov.mov_id, decision_id_remap)
        decision_result.nested_mov_ops = [
            op.model_copy(update={"owner_vov_id": decision_id_remap.get(op.owner_vov_id, op.owner_vov_id)})
            if op.owner_vov_id else op
            for op in decision_result.nested_mov_ops
        ]
    # MS §6.11 audit (this session's redesign): Query 6 may have found a
    # feelings entry on the shared MOV that's actually the Object's own
    # state, not Liriel's — the same wiring MOV_UPDATE's own nested_mov_ops
    # already uses to carry out that exact move (MS §6.8's mirror).
    _apply_nested_mov_ops(db, decision_result.nested_mov_ops)

    mov = db.load_mov(mov.mov_id)
    _renumber_stale_objectives(db, mov, ranked_ids)
    mov = db.load_mov(mov.mov_id)
    _evict_stale_clusters(db, mov)
    mov = db.load_mov(mov.mov_id)

    # --- Query 6A: IDENTITY_UPDATE (MS §12.7A, Rev 0006) --------------------
    # Last of the content writes, so it sees EVERYTHING this cycle changed in an Object
    # that already existed — MOV_UPDATE's patches, the anchored-valence updates, the
    # decision's audit patches, the relations — and records what was learned. It runs
    # inside the same draft: the records reach the real database in the very
    # transaction that carries the changes they document.
    identity_update = _run_identity_updates(db, mov.mov_id, scenario, cycle_id)

    # MS §11.1: every judgment this cycle makes (all six queries, and the
    # mechanical renumber/evict passes just above) has now concluded —
    # this is the one point any of it becomes durable. Nothing below
    # writes to the MOV at all (Phase 1's reply-composition bridge is
    # read-only, MS §0.4's own scoping), so committing here rather than
    # after log_cycle changes nothing about what gets persisted, only
    # that the database reflects this cycle's outcome before the
    # (slower) reply LLM call runs.
    db.commit()

    # --- Phase 1 bridge: compose the actual chat reply --------------------
    # MS §2.6/§4/§5: the reply is Liriel's move, and no move is Feelings
    # alone — it's her own current Ordinances-in-operation, narrowed by her
    # Restrictive Schemas, that make it hers. Her own row carries all three
    # — looked up by settings.liriel_self_vov_id (a nickname, MS §6.4), not
    # a hardcoded "VOV_0000": that literal lookup previously returned None
    # on every cycle since the id migration, silently starving this call of
    # her own state.
    liriel_self = mov.get(settings.liriel_self_vov_id)
    reply_artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=shown_mov, nested_movs=shown_nested,
        graph_of_traces=shown_graph, scenario_data=scenario,
        current_tactical_scene=current_tactical_scene, interlocutor=interlocutor, safety_screen=safety_screen,
    )
    # ProcessCommandControl's call (MS §11), not ProcessMotivation's — None
    # means the user didn't explicitly ask for a specific reply channel, so
    # the front end mirrors whatever channel the message itself arrived on
    # (telegram_bot._deliver_reply: the same two conditions decide `spoken`).
    handoff = decision_result.handoff_to_processcommandcontrol
    output_modality = handoff.preferred_output_modality if handoff else None
    spoken = output_modality == "voice" or (output_modality is None and source == "telegram_voice")
    response_text = chat(
        build_reply_prompt(decision_result, user_text, liriel_self, reply_artifacts, max_words=settings.reply_max_words, spoken=spoken),
        temperature=settings.llm_temperature_decision,
    ).strip()
    response_text = _limit_reply_words(response_text, settings.reply_max_words)

    db.log_cycle(
        mov_id=mov.mov_id,
        scenario_text=user_text,
        scene_subject_check_result=scene_subject_check.model_dump(),
        graph_request_result=graph_request.model_dump(),
        tactical_scene_result=current_tactical_scene.model_dump(),
        anchor_review_results={"cycle_id": cycle_id, "reviews": [r.model_dump() for r in anchor_reviews]},
        mainmemory_filing_result=filing_result.model_dump(),
        mov_update_result=mov_update_result.model_dump(),
        relations_update_result=relations_update_result.model_dump(),
        decision_result=decision_result.model_dump(),
        response_text=response_text,
        graph_of_traces=graph_of_traces,
        identity_update_result=identity_update,
    )

    return response_text, mov, output_modality


# The longest direction passed to the speech model (characters): a length cap on what the model wrote, nothing more.
_VOICE_DELIVERY_MAX_CHARS = 600


def compose_voice_delivery(user_text: str, reply_text: str, liriel_self: Optional[VectorObjectValence]) -> Optional[str]:
    """Phase 1 embodiment (ProcessCommandControl's stand-in, MS §11), called by the front end only when a reply is going
    to be SPOKEN by a voice that can be directed (llm_client.tts_is_steerable): Liriel writes, from her own Feelings and
    the words of the reply, how the voice should sound saying them (prompts.build_voice_delivery_prompt). The direction is
    the model's own; the code only tidies it (surrounding quotes/fences, whitespace, a length cap). It never judges the
    emotion and never touches the reply. None when the call fails or comes back empty -- the reply is then spoken without
    a direction, as it always was."""
    try:
        raw = chat(
            build_voice_delivery_prompt(reply_text, user_text, liriel_self),
            temperature=settings.llm_temperature_decision,
        )
    except Exception as exc:  # noqa: BLE001 - a failed direction must never cost the reply its voice
        print(f"[notice] voice direction unavailable ({type(exc).__name__}): {exc}")
        return None
    text = " ".join((raw or "").strip().strip("`\"'“”").split())
    if len(text) > _VOICE_DELIVERY_MAX_CHARS:
        cut = text[:_VOICE_DELIVERY_MAX_CHARS]
        text = cut[: cut.rfind(". ") + 1] if ". " in cut else cut.rstrip()
    return text or None


def _limit_reply_words(text: str, max_words: int) -> str:
    """REPLY_MAX_WORDS (config.py). 0 = no limit. The reply prompt already asks for the limit;
    this only enforces it on whatever still comes back longer, by cutting at the last COMPLETE
    sentence in the second half of what fits (else at the limit itself, with an ellipsis), so a
    reply never ends mid-sentence. It is a length cap on the final text — it judges nothing about
    its content, and nothing downstream is built from it."""
    if max_words <= 0:
        return text
    ends = [m.end() for m in re.finditer(r"\S+", text)]
    if len(ends) <= max_words:
        return text
    kept = text[: ends[max_words - 1]]
    sentence_ends = list(re.finditer(r"[.!?…][\"'”’)\]]*(?=\s|$)", kept))
    if sentence_ends and sentence_ends[-1].end() >= len(kept) * 0.5:
        cut = kept[: sentence_ends[-1].end()]
    else:
        cut = kept.rstrip(" ,;:—-") + "…"
    print(f"[notice] reply was {len(ends)} words, over REPLY_MAX_WORDS={max_words} — "
          f"cut to {len(cut.split())} at a sentence boundary")
    return cut.rstrip()


def _requests_with_cluster_recall(db: Database, results: list, reason: str) -> list:
    """AIRP (MS §7.4/§7.5): wraps graph_service.requests_from_ids with the
    two whole-family recall guarantees. Every hit still gets the ordinary
    one-hop survey requests_from_ids already gives any anchor — this only
    ADDS to that when the single strongest hit is a `ScenarioData` backbone
    Object (MS §6.10: the topic it belongs to is resurfacing, so its whole
    cluster gets pulled back per `settings.trace_depth`) or a `Sentient`
    Object (MS §6.9/§7.5: the whole identity — `Object_Master` plus every
    `Link_Identity_Part` Sub-Object — comes back in one motion, no depth
    parameter). `results` is search_memory's own return shape (each
    carries `object_nature` already, no extra lookup needed) — always its
    most-relevant-first ordering, so `results[0]` is the one candidate
    this treats as "the principal"."""
    hits = [c["vov_id"] for c in results]
    requests = graph_service.requests_from_ids(hits, reason=reason)
    if not results:
        return requests
    principal_id = results[0]["vov_id"]
    principal_nature = results[0]["object_nature"]
    if principal_nature == "ScenarioData":
        if settings.trace_depth <= 0:
            # TraceDepth 0: the principal alone is guaranteed, no relations
            # walked — there's nothing for a GraphRequestItem to build a
            # node from (see build_cluster_recall_request's own docstring),
            # so it's restored directly instead.
            db.restore_object(principal_id)
        else:
            requests = requests + graph_service.build_cluster_recall_request(
                db, principal_id, settings.trace_depth
            )
        if settings.verbose:
            print(f"[debug] AIRP cluster recall from principal={principal_id!r} "
                  f"at TraceDepth={settings.trace_depth}")
    elif principal_nature == "Sentient":
        requests = requests + graph_service.build_identity_recall_request(db, principal_id)
        if settings.verbose:
            print(f"[debug] AIRP identity recall from principal={principal_id!r}")
    return requests


def _normalize_desc(text: Optional[str]) -> str:
    """Cheap, exact-after-normalization key for spotting a duplicate
    freshly-minted row within the SAME mov_ops batch (see _apply_mov_ops) —
    deliberately not the fuzzy matching graph_service.py uses for
    cross-cycle recall (that's a judgment call the model makes by reading
    GraphOfTraces; this is a much narrower, purely mechanical safety net
    against the model repeating the identical description verbatim,
    several times, within its own single response)."""
    return " ".join((text or "").strip().lower().split())


def _apply_mov_ops(db: Database, mov_id: str, ops: list) -> dict:
    """Applies MS §12.3 mov_ops; returns `id_remap` — {model-guessed vov_id:
    real persisted vov_id} for every row this call minted (UPSERT_VOV/
    SPLIT_VOV never trust the model's own id for a genuinely new row, see
    below). run_motivation_cycle uses this to fix up anything ELSE in this
    same Query 2 response that referenced one of those guessed ids before
    the real one was known.

    Confirmed for real: Query 2 introduced three new Person rows in one
    cycle (a sibling, his wife, their son) under its own guessed ids, then
    emitted a WRITE_RELATION between two of them naming those same guessed
    ids — one didn't match the id actually assigned, and the relation
    write crashed on a foreign-key violation naming a vov_id that was
    never inserted anywhere. Not minting the model's own id was already
    correct (it can't know what's actually next, and trusting it risks a
    collision) — the gap was never propagating the swap to the rest of
    this cycle's own output, which is very much still expected to be
    internally consistent.

    Also guards against a second, related failure also confirmed for
    real: the same repetition tendency behind the reasoning-loop and
    truncated-JSON crashes (llamacpp_client.py) showing up INSIDE an
    otherwise well-formed response — four back-to-back UPSERT_VOV entries
    for "Gabriela, daughter of Michel and Thaíse.", word-for-word
    identical, each minting its own new row. GraphOfTraces can only steer
    the model away from a duplicate that already existed BEFORE this
    call; it has no way to stop the model from repeating itself within
    the one response it's currently writing. See `minted_this_batch`
    below: an UPSERT_VOV about to mint a genuinely new row is folded into
    an earlier one from the SAME batch instead, when the two are the same
    object_nature and the same brief_description after whitespace/case
    normalization — an exact-repeat check, not the fuzzy cross-cycle
    matching GraphOfTraces already gives the model to reason over."""
    minted_this_batch: Dict[tuple, str] = {}
    id_remap: dict = {}
    for op in ops:
        op: MovOp
        if op.op == "UPSERT_VOV" and op.vov is not None:
            model_id = op.vov.get("vov_id")
            existing = db.get_object(model_id) if model_id else None
            if op.vov.get("object_nature") == "Identity" or (existing is not None and existing.object_nature == "Identity"):
                # MS §6.13: an Identity record documents a change the ARCHITECTURE found
                # (IDENTITY_UPDATE writes it, with the before/after it captured and the
                # link to its Object, in one stroke) — one made here would have neither,
                # and an existing one is never edited.
                print(f"[notice] refused UPSERT_VOV of an Identity row ({model_id!r}) — records are "
                      f"written only by IDENTITY_UPDATE (MS §6.13) — skipped")
                continue
            if existing is not None and _nature_conflicts(existing, op.vov.get("object_nature")):
                # Same collision _upsert_nested_row already guards against,
                # just never checked here: an id the model invented for a
                # genuinely new row can coincide with an id some OTHER call
                # in this same cycle also invented for something completely
                # different — confirmed for real, Query 2 UPSERT_VOV'd a new
                # Person (Clara) under a self-guessed id that happened to
                # already be a standing Objective; existing-is-not-None made
                # this look like an intentional patch, so Clara's fields
                # merged onto that Objective's row instead of becoming her
                # own. Treat a nature mismatch as proof it's not the same
                # row, same as the nested-row path already does.
                print(f"[warning] UPSERT_VOV vov_id={model_id!r} collides with an existing "
                      f"{existing.object_nature!r} row but this one is "
                      f"{op.vov.get('object_nature')!r} — treating as a different entity, "
                      f"minting a new id instead of merging")
                existing = None
            elif existing is not None and existing.archived:
                # An archived row is not "the same object" either, matching
                # nature or not: MS's own MainMemory model (§7) has exactly
                # one sanctioned way back into focus — RESTORE_VOV/RETRIEVE,
                # explicit and separate from an ordinary content write.
                # UPSERT_VOV silently landing on an archived id would
                # revive AND overwrite it in one step, with nothing to show
                # it was ever a different thing before — confirmed for
                # real via the closely-related _apply_retrospective bug
                # this same guard mirrors (an archived "delegate" Objective
                # got reactivated by a stray retrospective review and then
                # overwritten by an unrelated new Best-Prey-Guess). Minting
                # fresh here keeps that door closed from this side too.
                print(f"[warning] UPSERT_VOV vov_id={model_id!r} names an ARCHIVED "
                      f"{existing.object_nature!r} row — treating as a different entity, "
                      f"minting a new id instead of reviving and overwriting it")
                existing = None
            elif existing is not None and existing.object_nature == "ScenarioData":
                # MS §6.10: a ScenarioData row is never edited in place, only
                # added to — reusing an existing ScenarioData's own id in a
                # later UPSERT_VOV would merge onto it via the same
                # _coerce_patch path PATCH_VOV uses (see the dedicated
                # PATCH_VOV guard above), silently rewriting the trace this
                # backbone exists to keep. Treating it as "not the same row"
                # here, same as the archived/nature-conflict guards above,
                # forces a fresh mint instead — exactly what "always a new
                # row" requires, without losing this cycle's content.
                print(f"[notice] UPSERT_VOV vov_id={model_id!r} names an existing ScenarioData "
                      f"row — ScenarioData is never edited (MS §6.10), minting a new id instead")
                existing = None
            dedup_key = (op.vov.get("object_nature"), _normalize_desc(op.vov.get("brief_description")))
            if existing is None and dedup_key[1]:
                dup_id = minted_this_batch.get(dedup_key)
                if dup_id is not None:
                    # The exact-repeat case _apply_mov_ops's own docstring
                    # describes: a genuinely new row about to be minted
                    # matches, object_nature and brief_description both,
                    # one already minted earlier in this SAME batch — fold
                    # into that one instead of creating a sibling duplicate.
                    print(f"[warning] UPSERT_VOV vov_id={model_id!r} repeats an object this "
                          f"same batch already created ({dup_id!r}, {dedup_key[1]!r}) — "
                          f"folding into it instead of minting a duplicate")
                    existing = db.get_object(dup_id)
                    if model_id:
                        id_remap[model_id] = dup_id
            vov = _coerce_vov(existing, op.vov)
            if vov is None:
                print(f"[warning] UPSERT_VOV missing required fields and no existing "
                      f"object to patch onto, skipped: {op.vov}")
                continue
            if existing is None:  # genuinely new — don't trust the model's own id verbatim
                real_id = db.mint_vov_id(model_id, op.vov.get("object_nature"))
                if model_id and model_id != real_id:
                    id_remap[model_id] = real_id
                vov = vov.model_copy(update={"vov_id": real_id})
                if dedup_key[1]:
                    minted_this_batch[dedup_key] = real_id
            elif vov.vov_id != existing.vov_id:
                # _coerce_vov merges op.vov (which still carries the
                # model's OWN guessed vov_id, e.g. from the dedup case
                # above where `existing` was fetched by a DIFFERENT id
                # than op.vov["vov_id"]) onto `existing` via a plain dict
                # update — nothing stopped that merge from quietly
                # "renaming" the real row to the model's guess. Confirmed
                # for real: this silently defeated the dedup fold above,
                # inserting a fresh row under the guessed id instead of
                # updating the one it was supposed to fold into. Once a
                # merge target is chosen, its real id is the only id that
                # write may ever land under.
                vov = vov.model_copy(update={"vov_id": existing.vov_id})
            _upsert_and_link(db, mov_id, vov)
        elif op.op == "PATCH_VOV" and op.vov_id and op.patch:
            existing = db.get_object(op.vov_id)
            if existing is None:
                print(f"[warning] PATCH_VOV on unknown vov_id={op.vov_id!r}, skipped")
                continue
            if existing.object_nature == "ScenarioData":
                # MS §6.10: a ScenarioData row, once written, is a fixed
                # trace of what was known at that point in the matter's
                # history — editing it in place erases exactly the record
                # the backbone exists to keep. Unlike the cluster-matching
                # judgment (which matter a new report continues) this is
                # purely mechanical to check, so — same reasoning as
                # _is_protected refusing an ARCHIVE_VOV on core identity —
                # it's refused here rather than only asked for in the
                # prompt.
                print(f"[notice] refused PATCH_VOV on ScenarioData row {op.vov_id!r} "
                      f"(MS §6.10: never edited, only added to) — skipped: {op.patch}")
                continue
            if existing.object_nature == "Identity":
                # Same standing as ScenarioData: the record of what was known at that
                # point. A later correction is a NEW record that supersedes this one.
                print(f"[notice] refused PATCH_VOV on Identity record {op.vov_id!r} "
                      f"(MS §6.13: never edited, a correction is a new record) — skipped: {op.patch}")
                continue
            merged = existing.model_copy(update=_coerce_patch(existing, op.patch))
            _upsert_and_link(db, mov_id, merged)
        elif op.op == "SET_PRIORITY" and op.vov_id:
            existing = db.get_object(op.vov_id)
            if existing is None:
                continue
            _upsert_and_link(db, mov_id, existing.model_copy(update={"priority": op.priority}))
        elif op.op == "ARCHIVE_VOV" and op.vov_id:
            if _is_protected(op.vov_id):
                print(f"[notice] refused to archive protected vov_id={op.vov_id!r} (core identity)")
                continue
            db.archive_object(op.vov_id)
        elif op.op == "RESTORE_VOV" and op.vov_id:
            db.restore_object(op.vov_id)
        elif op.op == "SPLIT_VOV" and op.vov_id and op.into:
            base = db.get_object(op.vov_id)
            if base is not None and base.object_nature == "Identity":
                print(f"[notice] refused SPLIT_VOV of Identity record {op.vov_id!r} (MS §6.13) — skipped")
                continue
            db.archive_object(op.vov_id)
            for raw_piece in op.into:
                piece = _coerce_vov(base, raw_piece)
                if piece is None:
                    print(f"[warning] SPLIT_VOV piece missing required fields and no "
                          f"base object to inherit from, skipped: {raw_piece}")
                    continue
                # Every piece is a genuinely new object — the original id
                # (op.vov_id) was just archived, so it's never reused here.
                piece_model_id = raw_piece.get("vov_id") if isinstance(raw_piece, dict) else None
                piece_nature = raw_piece.get("object_nature") if isinstance(raw_piece, dict) else None
                real_id = db.mint_vov_id(piece_model_id, piece_nature)
                if piece_model_id and piece_model_id != real_id:
                    id_remap[piece_model_id] = real_id
                piece = piece.model_copy(update={"vov_id": real_id})
                _upsert_and_link(db, mov_id, piece)
        else:
            print(f"[warning] unrecognized or incomplete mov_op: {op.model_dump()}")

    return id_remap


def _relink_new_objects(db: Database, mov_id: str, id_remap: dict) -> None:
    """Second pass, only over rows minted this same batch (see
    _apply_mov_ops): a relevant_relations entry naming a SIBLING new
    object by its model-guessed id couldn't be linked when
    _ensure_relation_edges first ran for it — that sibling didn't exist in
    the DB yet at that point, so the existence check silently skipped it,
    same as it would for any genuinely-missing id. Now that every sibling
    minted this cycle has its real id, remap any stale guesses still
    sitting in relevant_relations and retry the link."""
    for real_id in id_remap.values():
        vov = db.get_object(real_id)
        if vov is None or not vov.relevant_relations:
            continue
        remapped_relations = [id_remap.get(r, r) for r in vov.relevant_relations]
        if remapped_relations != vov.relevant_relations:
            vov = vov.model_copy(update={"relevant_relations": remapped_relations})
            db.upsert_object(mov_id, vov)
        _link_or_defer(db, vov)


def _load_nested_movs(db: Database, mov: MatrixObjectsValence, max_depth: int) -> list:
    """MS §0.3 block [3]: "MOV (+ nested MOVs)". Gathers every nested MOV
    reachable from an active VOV.nested_mov pointer (MS §6.4/§6.8), breadth-
    first, up to `max_depth` mirror levels — a nested MOV's own rows can
    carry a further nested_mov (the next mirror level), which is why this
    recurses instead of a single flat pass. MS §6.8 rule (d): "do not nest
    deeper than the QUERY authorizes" — max_depth is that authorization."""
    nested: List[MatrixObjectsValence] = []
    seen = {mov.mov_id}
    frontier = [mov]
    for _ in range(max(0, max_depth)):
        next_frontier = []
        for level_mov in frontier:
            for obj in level_mov.active():
                nid = obj.nested_mov
                if not nid or nid in seen:
                    continue
                seen.add(nid)
                child = db.load_mov(nid)
                nested.append(child)
                next_frontier.append(child)
        if not next_frontier:
            break
        frontier = next_frontier
    return nested


def _apply_nested_mov_ops(db: Database, ops: list) -> None:
    """MS §12.3 nested_mov_ops / MS §6.8 (specular recursion as a data
    structure) — the persistence underneath it already exists (the
    reference-dataset import populates several real mov_ids the same way),
    so materializing these ops is mostly about wiring the same primitives
    (ensure_mov/upsert_object/archive_object) through this vocabulary."""
    for op in ops:
        op: NestedMovOp
        if op.op == "CREATE_NESTED_MOV":
            label = f"Nested MOV (owner={op.owner_vov_id}, depth={op.depth})" if op.owner_vov_id else None
            db.ensure_mov(op.mov_id, label)
            if op.owner_vov_id:
                owner = db.get_object(op.owner_vov_id)
                if owner is None:
                    print(f"[warning] CREATE_NESTED_MOV names unknown owner_vov_id="
                          f"{op.owner_vov_id!r}; created {op.mov_id!r} without a back-reference")
                elif owner.nested_mov != op.mov_id:
                    # MS §6.4: nested_mov lives on the owner's own row, in
                    # whichever MOV that row actually belongs to (usually
                    # the default one, but an owner can itself be a row
                    # inside another nested MOV — a deeper mirror level).
                    owner_mov_id = db.get_object_mov_id(op.owner_vov_id) or settings.default_mov_id
                    _upsert_and_link(db, owner_mov_id, owner.model_copy(update={"nested_mov": op.mov_id}))
            for raw_row in (op.rows or []):
                _upsert_nested_row(db, op.mov_id, raw_row)
        elif op.op == "PATCH_NESTED_VOV" and op.vov_id and op.patch:
            existing = db.get_object(op.vov_id)
            if existing is None:
                print(f"[warning] PATCH_NESTED_VOV on unknown vov_id={op.vov_id!r} in {op.mov_id!r}, skipped")
                continue
            if existing.object_nature == "Identity":
                print(f"[notice] refused PATCH_NESTED_VOV on Identity record {op.vov_id!r} (MS §6.13) — skipped")
                continue
            if _agent_fields_present(op.patch) and existing.object_nature not in _AGENT_OBJECT_NATURES:
                # Ordinances/Schemas only describe agents (MS §4/§5) — a
                # patch carrying them against a non-agent row isn't a
                # legitimate update to that row at all, it's the model
                # aiming at an id that turned out to already mean something
                # else (seen for real: Fábio/Adriana's read of Mike landing
                # on VOV_0009B, a pre-existing "repair the bond" Objective,
                # because both happened to share that id). Dropping only
                # the incompatible sub-fields (as _coerce_patch still does
                # for any other caller) isn't enough here: the rest of the
                # same patch — brief_description included — would still
                # silently overwrite the unrelated row's identity. Redirect
                # the whole patch to a fresh row instead of touching it.
                new_id = db.mint_vov_id(op.vov_id, op.patch.get("object_nature"))
                print(f"[warning] PATCH_NESTED_VOV vov_id={op.vov_id!r} carries agent-only "
                      f"fields but the existing row is object_nature={existing.object_nature!r} "
                      f"— treating as a different entity, creating {new_id!r} instead: {op.patch}")
                seed = {**op.patch, "vov_id": new_id, "object_nature": op.patch.get("object_nature", "Sentient")}
                _upsert_nested_row(db, op.mov_id, seed)
                continue
            merged = existing.model_copy(update=_coerce_patch(existing, op.patch))
            _upsert_and_link(db, op.mov_id, merged)
        elif op.op == "ARCHIVE_NESTED_MOV":
            # No separate "archived" flag on a MOV itself — MainMemory
            # already works per-row (archived_at); archiving every row
            # currently active in it achieves the same thing.
            for obj in db.load_mov(op.mov_id).objects:
                db.archive_object(obj.vov_id)
        else:
            print(f"[warning] unrecognized or incomplete nested_mov_op: {op.model_dump()}")


def _upsert_nested_row(db: Database, mov_id: str, raw_row: dict) -> None:
    """A CREATE_NESTED_MOV row is either a genuinely new mirror-suffixed VOV
    (MS §6.8 rule a: "suffix the mirror level... keep IDs unique across the
    nesting") or a partial update to one already there. Unlike a brand-new
    top-level Object (_apply_mov_ops' UPSERT_VOV), a new id here is NOT
    reassigned by the server by default — the suffix itself is the
    meaningful part (VOV_0009 -> VOV_0009B).

    But a mirror suffix isn't guaranteed collision-free — e.g. a nested MOV
    seeded once from the reference dataset can already hold an unrelated
    "VOV_0009B" (a different Object entirely) before the model ever mirrors
    the real VOV_0009 under it. Two signals catch that: the existing id
    belongs to a different mov_id outright, or — same mov_id — its
    object_nature doesn't match what's being written (a Person can't
    quietly become an Objective). Either one means "not actually the same
    row"; reassign a fresh id and insert as new rather than merge into it.
    """
    vov_id = raw_row.get("vov_id")
    existing = db.get_object(vov_id) if vov_id else None
    if raw_row.get("object_nature") == "Identity" or (existing is not None and existing.object_nature == "Identity"):
        print(f"[notice] refused nested row {vov_id!r}: Identity records are written only by IDENTITY_UPDATE (MS §6.13)")
        return
    collision = existing is not None and (
        db.get_object_mov_id(vov_id) not in (mov_id, None)
        or (raw_row.get("object_nature") and existing.object_nature != raw_row["object_nature"])
    )
    if collision:
        new_id = db.mint_vov_id(vov_id, raw_row.get("object_nature"))
        print(f"[warning] nested row id {vov_id!r} collides with an unrelated existing "
              f"object, reassigning id -> {new_id!r}: {raw_row}")
        existing = None
        raw_row = {**raw_row, "vov_id": new_id}
    vov = _coerce_vov(existing, raw_row)
    if vov is None:
        print(f"[warning] nested row missing required fields and no existing "
              f"object to patch onto, skipped: {raw_row}")
        return
    _upsert_and_link(db, mov_id, vov)


def _renumber_stale_objectives(db: Database, mov: MatrixObjectsValence, ranked_ids: List[str]) -> None:
    """MS §14.6: "exactly one Objective at priority 1, priorities unique
    and contiguous." The block above already enforces this on the
    Objectives Query 3 actually ranked this cycle (best_prey_guess +
    accompanying_objectives, priorities 1, 2, 3, ...) — but any OTHER
    Objective row already active in the MOV from an earlier cycle, and
    not archived/reprioritized by this cycle's retrospective or mov_ops,
    keeps whatever priority it already had. Query 2's retrospective step
    is a per-cycle model judgment call on which open Objectives to
    revisit, not a guarantee every one gets reviewed every cycle — seen
    for real: an old "Quésia" Objective sat at priority 1 for several
    cycles after the conversation moved on, alongside whatever each new
    cycle's own guess also claimed priority 1 for, until 4 different
    Objectives were simultaneously "priority 1" in the same MOV. Left
    alone that's not just a MindReader display quirk: it's the invariant
    this comment already claimed to enforce, silently broken the moment
    an old Objective outlives the cycle that created it.

    Renumbers every active Objective NOT in `ranked_ids` (this cycle's own
    ranking) to continue right after it, ordered by whatever priority it
    already had (ties broken by vov_id for determinism) — restoring
    unique, contiguous priorities across the *whole* active Objective set
    rather than just this cycle's own picks.
    """
    others = sorted(
        (o for o in mov.active() if o.object_nature == "Objective" and o.vov_id not in ranked_ids),
        key=lambda o: (o.priority if o.priority is not None else 10**9, o.vov_id),
    )
    next_priority = len(ranked_ids) + 1
    for obj in others:
        if obj.priority != next_priority:
            _upsert_and_link(db, mov.mov_id, obj.model_copy(update={"priority": next_priority}))
        next_priority += 1


def _evict_stale_clusters(db: Database, mov: MatrixObjectsValence) -> None:
    """MS §7.4 (AIRP). `MemoryStrength` caps how many clusters (MS §6.10)
    may hold the focus at once. Unlike _renumber_stale_objectives above,
    nothing here is model-decided: this is the mechanical guarantee that
    makes TraceDepth's own generosity affordable over time — a config
    parameter enforced in code, the same way `_is_protected` enforces core
    identity's exemption everywhere else in this module, not a per-cycle
    judgment call the model is trusted to remember to make on its own.

    A cluster = one connected component of active `ScenarioData` Objects
    joined by `Link_Subject_Cluster` edges to one another (MS §6.10, §8.3)
    — a topic can have more than one backbone row, and they still count as
    ONE cluster toward MemoryStrength, not several. Once more clusters are
    active than `settings.memory_strength` allows, the oldest (by recency —
    the only tiebreak available here; there's no "current message" to score
    relevance or the usual MS §8.4 order against a standing-focus check)
    is filed to MainMemory: its backbone, plus whichever `Link_Subject_Cluster`
    member Objects belong to it EXCLUSIVELY. A member also reachable from a
    surviving cluster's backbone is left alone — it's still needed to
    understand a matter that isn't leaving, exactly what MS §7.4 requires
    ("never filed on this account alone"). `_is_protected` core identity
    is never touched, same as every other archive path in this module.
    """
    active_backbones = {o.vov_id: o for o in mov.active() if o.object_nature == "ScenarioData"}
    if len(active_backbones) <= 1:
        return  # can't have more than one cluster with at most one backbone row

    edges = db.get_relations(list(active_backbones.keys()), ["Link_Subject_Cluster"])
    adjacency: Dict[str, set] = {vid: set() for vid in active_backbones}
    for row in edges:
        a, b = row["from_vov_id"], row["to_vov_id"]
        if a in adjacency and b in adjacency:
            adjacency[a].add(b)
            adjacency[b].add(a)

    seen: set = set()
    clusters: List[List[str]] = []
    for vid in active_backbones:
        if vid in seen:
            continue
        component, frontier = [], [vid]
        seen.add(vid)
        while frontier:
            cur = frontier.pop()
            component.append(cur)
            for nxt in adjacency[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        clusters.append(component)

    if len(clusters) <= settings.memory_strength:
        return

    def _cluster_recency(cluster_ids: List[str]) -> datetime:
        updates = [active_backbones[vid].updated_at for vid in cluster_ids if active_backbones[vid].updated_at]
        return max(updates) if updates else datetime.min.replace(tzinfo=timezone.utc)

    clusters.sort(key=_cluster_recency)
    excess = len(clusters) - settings.memory_strength
    stale_clusters, surviving_clusters = clusters[:excess], clusters[excess:]
    surviving_backbone_ids = {vid for cluster in surviving_clusters for vid in cluster}

    for cluster_ids in stale_clusters:
        to_archive = set(cluster_ids)
        member_ids: set = set()
        for vid in cluster_ids:
            for row in db.get_relations([vid], ["Link_Subject_Cluster"]):
                member_ids.add(row["to_vov_id"] if row["from_vov_id"] == vid else row["from_vov_id"])
        member_ids -= set(cluster_ids)  # a fellow backbone row of this same cluster, not a member
        for member_id in member_ids:
            linked_backbones = {
                (row["from_vov_id"] if row["to_vov_id"] == member_id else row["to_vov_id"])
                for row in db.get_relations([member_id], ["Link_Subject_Cluster"])
            }
            if linked_backbones & surviving_backbone_ids:
                continue  # still needed by a cluster that isn't leaving
            to_archive.add(member_id)
        for vov_id in to_archive:
            if _is_protected(vov_id):
                continue
            db.archive_object(vov_id)
        if settings.verbose:
            print(f"[debug] AIRP MemoryStrength={settings.memory_strength}: "
                  f"archived stale cluster {sorted(to_archive)!r}")


def _fallback_relation_kind(a_nature: Optional[str], b_nature: Optional[str]) -> str:
    """Which of the closed three (MS §8.3) an edge is, when nothing more
    specific (a model's own WRITE_RELATION) says so. `Link_Subject_Cluster`
    is recoverable from the endpoints' own `object_nature` alone — MS §8.3
    already documents this ("the distinction... is recoverable from each
    endpoint's own object_nature, not from a separate kind value") — so an
    edge touching a ScenarioData row is that kind unconditionally, not the
    generic fallback. `Link_Identity_Part` (Sub-Object -> Object_Master,
    MS §6.9) has no equivalent signal in the VOV shape itself — nothing
    marks a row as "this one's Master" — so it still depends on the model
    emitting its own WRITE_RELATION; everything else defaults to
    `Link_Valence_Load`, the generic "some other bond" catch-all.

    Confirmed for real: once the closed vocabulary shipped, the model
    stopped emitting WRITE_RELATION for cluster edges at all (it used to,
    under the old open `cluster_backbone`/`cluster_member` kind, but never
    once under the new names) — every such edge was falling through to
    this function's old unconditional `Link_Valence_Load` default, so not
    one `Link_Subject_Cluster` edge existed anywhere in a live cluster.
    Deriving it here closes that gap without depending on the model
    remembering a step it had already stopped taking."""
    if "ScenarioData" in (a_nature, b_nature):
        return "Link_Subject_Cluster"
    return "Link_Valence_Load"


# What a fallback bond (below) says about itself. It is the architecture's own statement, not a
# judgment about the Objects: the bond exists because an Object listed the other in
# `relevant_relations`, and nobody has said what kind of bond it is. Written into the bond's text so
# the model (reading the Graph of Traces) and the viewer see it for what it is, and so a bond that is
# later typed for real replaces it in place without being mistaken for a change of a known bond.
_UNTYPED_BOND_TEXT = "listed in relevant_relations — the kind of bond and its meaning are not stated yet"


def _ensure_relation_edges(db: Database, vov: VectorObjectValence) -> None:
    """Every relation a VOV claims via `relevant_relations` (MS §6.4)
    becomes a real, walkable `mov_relations` edge (MS §8) — a fallback
    (see _fallback_relation_kind) wherever nothing more specific already
    connects the pair — regardless of whether the model also remembered to
    emit its own WRITE_RELATION mainmemory_command this cycle.
    `relevant_relations` and `mov_relations` are different things (MS §6.4
    vs §8), and prompts.py already asks the model to keep them in sync, but
    a small local model doesn't always comply. Confirmed for real:
    Teresa (VOV_0035) correctly listed Paulo (VOV_0032) in her own
    `relevant_relations` — and had zero rows in `mov_relations` — so once
    she aged out of the active focus, TrackGraphProcess's traversal had no
    edge to walk to reach her from Paulo (or vice versa) ever again, and
    a later "who is her husband?" found every Objective *about* contacting
    her but never the one row that actually says who he is. This closes
    that gap unconditionally, at the point every VOV gets written, instead
    of trusting the model to keep asking for it."""
    if not vov.relevant_relations:
        return
    existing_edges = db.get_relations([vov.vov_id])
    connected = {
        (row["from_vov_id"] if row["to_vov_id"] == vov.vov_id else row["to_vov_id"])
        for row in existing_edges
    }
    for other_id in vov.relevant_relations:
        if other_id == vov.vov_id or other_id in connected:
            continue
        other = db.get_object(other_id)
        if other is None:
            continue  # relevant_relations can point at a stale/typo'd id -- don't invent an edge to nothing
        if "Interpellation" in (vov.object_nature, other.object_nature) and "ScenarioData" not in (vov.object_nature, other.object_nature):
            continue  # Rev 0007 AZ: an Interpellation's parties are tied by role bonds (origin/target) alone; the role cannot be guessed from a list of ids
        kind = _fallback_relation_kind(vov.object_nature, other.object_nature)
        # No mechanical veto here on purpose: whether two ScenarioData rows
        # are "the same matter" is a content judgment (MS §6.10/§11.1),
        # never code's to second-guess. If the model wrote this into
        # relevant_relations, it already made that call — an earlier
        # "shared member" heuristic here once refused/rewrote the model's
        # own correct judgment and, worse, was later found to auto-merge
        # unrelated matters on its own (see _apply_mov_ops's history).
        db.write_relation(from_vov_id=vov.vov_id, to_vov_id=other_id, kind=kind, propositional=_UNTYPED_BOND_TEXT)
        connected.add(other_id)


def _enforce_objective_cluster_only_relations(db: Database, vov: VectorObjectValence) -> VectorObjectValence:
    """MS §6.10: "An Objective belonging to a cluster... relate it to the
    ScenarioData Object(s) that gave rise to it and stop there" — not also
    to the Sentients/Situations the matter touches, since walking that one
    edge onward already reaches the rest of the cluster for free. Prompted
    for explicitly (prompts.py, both Query 2 and Query 3) and demonstrated
    in both worked examples, but a small local model complies
    inconsistently — confirmed for real, across otherwise-identical retries
    of the same scenario: one run got it exactly right (an Objective
    related to its ScenarioData alone), the next added the bonded Sentient
    directly anyway, and a third dropped the ScenarioData link entirely
    and related only to a Sentient. Prompting alone isn't holding this
    invariant reliably enough — same conclusion this codebase already
    reached for "exactly one Objective at priority 1" (MS §14.6, enforced
    below in run_motivation_cycle, not trusted from the model's own count).

    Only prunes when the Objective already names at least one ScenarioData
    in its own `relevant_relations` — that's what marks it as belonging to
    a cluster at all (MS §6.10's own condition); a standalone Objective
    with no cluster origin is untouched, since the "stop there" rule never
    applied to it in the first place. Silent about what it can't fix:
    a cycle that drops the ScenarioData link entirely leaves nothing here
    to recognize the Objective as cluster-bound, so its stray relations
    aren't touched either — this closes the "too many edges" failure this
    session actually observed, not the "too few" one, which is a prompting
    problem it doesn't have a code-level answer for."""
    if vov.object_nature != "Objective" or not vov.relevant_relations:
        return vov
    related = db.get_objects(vov.relevant_relations)
    scenario_ids = [rid for rid in vov.relevant_relations if related.get(rid) and related[rid].object_nature == "ScenarioData"]
    if not scenario_ids or set(vov.relevant_relations) == set(scenario_ids):
        return vov  # not cluster-bound, or already compliant -- nothing to prune
    print(f"[notice] Objective {vov.vov_id!r} related to more than its ScenarioData "
          f"origin (MS §6.10) — pruning to {scenario_ids!r}, was {vov.relevant_relations!r}")
    return vov.model_copy(update={"relevant_relations": scenario_ids})


def _upsert_and_link(db: Database, mov_id: str, vov: VectorObjectValence) -> None:
    """db.upsert_object, plus _ensure_relation_edges right after — every
    write path in this module should go through this instead of calling
    db.upsert_object directly, so no VOV can end up claiming a relation in
    its own `relevant_relations` that the actual graph can't walk to.
    Deliberately does NOT auto-complete any further edges beyond what the
    model itself claimed: an earlier `_link_scenario_data_siblings` pass
    tried to fill in "missing" ScenarioData-to-ScenarioData backbone edges
    from a shared-member heuristic, and that heuristic auto-merged two
    unrelated matters that only happened to share a reporter. Whether two
    ScenarioData belong to the same cluster is a content judgment (MS
    §6.10/§11.1) — the model must write every edge it means to exist
    itself; no backend process infers or completes one on its behalf."""
    vov = _enforce_objective_cluster_only_relations(db, vov)
    db.upsert_object(mov_id, vov)
    _link_or_defer(db, vov)


def _nature_conflicts(existing: VectorObjectValence, expected_nature: Optional[str]) -> bool:
    """An id collision is only a legitimate patch/re-election when the row
    already there is plausibly the same kind of thing being written — the
    model has no visibility into ids another call in this same cycle
    already claimed for something else, so a shared id can be pure
    coincidence between two independently-invented guesses. Shared by
    _apply_mov_ops and _resolve_id; _upsert_nested_row has its own
    version of this same check (plus a cross-mov_id signal that doesn't
    apply to top-level ids)."""
    return bool(expected_nature) and existing.object_nature != expected_nature


def _mov_ops_touched_ids(mov_ops: list, id_remap: dict) -> set:
    """Every vov_id ProcessMotivation consciously decided something about
    THIS cycle: targeted/minted by one of MOV_UPDATE's own mov_ops.
    `_resolve_id` uses this as the only legitimate basis for BEST_PREY_GUESS
    re-electing an id that already belongs to a standing Objective — an id
    existing at all is not enough on its own, or BEST_PREY_GUESS could
    silently re-use *any* old Objective's row for a brand-new, unrelated
    guess just because the model happened to invent the same number.
    Confirmed for real (pre-redesign, when this set also included
    retrospective-reviewed ids): an "Objective" about Clara sat at
    VOV_0005 for six straight cycles, correctly re-elected each time as
    the same continuing hunt — the retrospective reviewed it every one of
    those cycles — and then, on a seventh cycle whose retrospective never
    mentioned VOV_0005 at all, a later query guessed that same id for an
    entirely different, unrelated Objective ("identify the specific
    daughter..."), and _resolve_id let it through because an Objective
    already sat there. The Clara objective wasn't archived, wasn't
    reprioritized down — it was simply gone, overwritten, with no record
    of what became of it. An Objective can be *edited* — refined,
    re-elected, carried forward — but not silently erased by a completely
    different one that happens to share its number. Retrospective review
    is no longer a basis for this set at all (MS §10.8/§11/§17 point 7 —
    that duty moved to ProcessIntrospection, not yet built), leaving
    MOV_UPDATE's own mov_ops as the only source of a legitimately
    "touched" id this cycle."""
    touched: set = set()
    for op in mov_ops:
        if op.op == "UPSERT_VOV" and op.vov is not None:
            model_id = op.vov.get("vov_id")
            if model_id:
                touched.add(id_remap.get(model_id, model_id))
        elif op.vov_id:
            touched.add(op.vov_id)
    return touched


def _resolve_id(db: Database, proposed_id: str, cycle_touched_ids: set) -> str:
    """The model's best_prey_guess/accompanying_objectives usually re-elect
    an Objective MOV_UPDATE already created or touched in this same cycle
    (by echoing its id back from the MOV it was handed) — that's a
    legitimate reuse, so keep it, but only when BOTH: the row already
    there actually is an Objective (this id is always about to become
    one, run_motivation_cycle force-sets object_nature="Objective" right
    after this returns, so reusing an id that currently names something
    else is never re-election, it's a collision — confirmed for real
    against a Person, see _nature_conflicts), AND this exact cycle
    actually did something with that id (see _mov_ops_touched_ids) — an id
    merely existing, untouched by anything THIS cycle decided, is no more
    evidence of intentional re-election than a Person's id would be;
    confirmed for real, a six-cycle-standing Objective got silently
    overwritten by an unrelated new one that happened to guess its number
    on a cycle that never reviewed it at all. If either check fails,
    trusting the id verbatim risks a collision (or a silent typo the
    model made copying an id); assign a fresh one instead.

    A THIRD condition, added after the same failure resurfaced through a
    different door: `existing` must not be archived. An archived Objective
    can legitimately end up in `cycle_touched_ids` (e.g. the retrospective
    reviewing it) without that meaning re-election was ever intended — MS
    itself distinguishes "still open" from filed away, and _apply_
    retrospective now refuses to act on an archived id for exactly this
    reason. This check stays anyway, in case an archived id reaches here
    by some other route than the one already closed: reusing it would
    revive AND overwrite a retired row in the same stroke, exactly the
    "erased by a completely different one" MS itself forbids."""
    existing = db.get_object(proposed_id)
    if (
        existing is not None
        and not _nature_conflicts(existing, "Objective")
        and not existing.archived
    ):
        # Rev 0007 (QA wave 1): the id of a STANDING, ACTIVE Objective, written by the model in its
        # own answer, is the model identifying it — the same pursuit, re-ranked or continued.
        # Refusing it (the old "must be in the cycle's touched set" check) minted `Objective_X_2`
        # beside `Objective_X` on every cycle that kept an aim, and pushed the original down the
        # ranks. The rule that stays: an ARCHIVED row is never revived or overwritten this way.
        return proposed_id
    return db.mint_vov_id(proposed_id, "Objective")


def _coerce_vov(existing: Optional[VectorObjectValence], raw: dict) -> Optional[VectorObjectValence]:
    """Build a full VectorObjectValence from a model-supplied dict that may be
    a complete new object or a partial one meant to patch/inherit from an
    existing row — some models emit UPSERT_VOV/SPLIT_VOV with only a few
    changed fields instead of PATCH_VOV. Returns None if there's neither a
    complete object nor an existing one to fill in the gaps."""
    if existing is not None:
        return existing.model_copy(update=_coerce_patch(existing, raw))
    try:
        return VectorObjectValence.model_validate(raw)
    except ValidationError:
        return None


_NESTED_MAP_FIELDS = {"feelings": AxisValence, "ordinances": AxisValence, "schemas": SchemaEntry}
_NESTED_OBJECT_FIELDS = {"delta_report": DeltaReport, "objective": ObjectiveBlock}
# MS §4/§5: Ordinances and Restrictive Schemas describe "players in the
# hunt" — an agent's own drives and the traits that narrow them. Feelings
# are universal (MS §3.2: "every Object holds a position on all fourteen"),
# but a Situation/Objective/Thing/Event/... has no Character or Instincts
# of its own to record. Without this, a patch aimed at the wrong row (an
# id collision, or the model simply misreading which row it meant) can
# silently graft a person's personality onto an unrelated Objective — seen
# for real: Fábio's read of Mike landed on VOV_0009B, the pre-existing
# "repair the bond with Adriana" Objective in his nested MOV, because both
# happened to share that id.
_AGENT_OBJECT_NATURES = {"PCI", "Sentient", "Person", "Animal", "Group", "Entity"}


def _agent_fields_present(patch: dict) -> bool:
    return bool(patch.get("ordinances")) or bool(patch.get("schemas"))


def _is_protected(vov_id: Optional[str]) -> bool:
    """Core identity (Liriel's own row + her creator/developer, config.py's
    protected_vov_ids) is exempt from MS §7's focus/archive cycle — losing
    one of these from the MOV isn't forgetting a case detail, it's losing
    who Liriel is. Everything else archives and retrieves via the Graph of
    Traces as normal.

    Liriel's own row (`settings.liriel_self_vov_id`) is protected whatever PROTECTED_VOV_IDS says: it is the first row of the MOV and
    the OWNER every review and every reply reads (QA wave 4, step 7: the row `PCI_Liriel_QA` had been linked as a member of a matter's
    cluster, the AIRP cluster eviction (MS §7.4) filed that cluster's exclusive members — her row among them — and from then on the
    reviews saw no owner ("null") and the reply prompt found no row of hers, for six steps and a wave, silently). PROTECTED_VOV_IDS
    defaults to VOV_0000..VOV_0002, which are not the self id of a fresh install (`PCI_Liriel_Self`) either."""
    return bool(vov_id) and (vov_id in settings.protected_vov_ids or vov_id == settings.liriel_self_vov_id)


def _coerce_patch(existing: VectorObjectValence, patch: dict) -> dict:
    """A PATCH_VOV patch (or an UPSERT_VOV/SPLIT_VOV treated as one by
    _coerce_vov) carries plain dicts straight out of the model's JSON, but
    `existing.model_copy(update=...)` — used to apply the merge — does no
    validation. Two things need to happen here before that copy, or the
    merged VOV ends up with raw dicts sitting where a typed model belongs
    (e.g. `vov.feelings['Joy']` a dict instead of an AxisValence), which
    then breaks the very next attribute access in database.py (`av.v`):

    1. Nested maps (feelings/ordinances/schemas) merge one level deep
       instead of replacing the whole map, so a patch to one axis doesn't
       wipe the rest — and each entry is coerced to its model type.
    2. Singular nested objects (delta_report/objective) merge onto whatever
       is already there instead of being validated as a full replacement —
       a model may patch just one of an Objective's fields (e.g.
       `{"cycles_open": 1}`, seen from Claude Sonnet 5 tracking a cycle
       count), and ObjectiveBlock requires several others (genus,
       gain_form, ...) that a bare field-level patch was never going to
       carry. If there's nothing to merge onto and the patch alone isn't a
       complete object, the field is dropped from the patch (left
       unchanged on `existing`) rather than crashing the whole cycle.
    """
    out = dict(patch)
    target_nature = out.get("object_nature", existing.object_nature)
    for agent_only_field in ("ordinances", "schemas"):
        if agent_only_field in out and target_nature not in _AGENT_OBJECT_NATURES:
            print(f"[warning] dropped {agent_only_field!r} patch on {existing.vov_id!r} "
                  f"(object_nature={target_nature!r} isn't an agent — MS §4/§5 apply only "
                  f"to players in the hunt): {out[agent_only_field]}")
            out.pop(agent_only_field)
    for nested_field, entry_type in _NESTED_MAP_FIELDS.items():
        if nested_field in out and isinstance(out.get(nested_field), dict):
            merged = dict(getattr(existing, nested_field))
            for key, raw_entry in out[nested_field].items():
                if raw_entry is None:
                    # The merge below only ever adds/updates an entry —
                    # nothing short of this let a PATCH_VOV actually REMOVE
                    # a stale axis (MS §3.2: a blank axis is itself
                    # information, not the same thing as some other value).
                    # Confirmed for real: the only way to clear one used to
                    # be a direct db.replace_object call from outside the
                    # model's own mov_ops entirely. An explicit JSON `null`
                    # for a specific key is the model's own request to drop
                    # it — same "the model decides what, code just carries
                    # it out" boundary as every other op here.
                    merged.pop(key, None)
                    continue
                if isinstance(raw_entry, (str, int, float)) and not isinstance(raw_entry, bool):
                    # A bare scalar ("neutral", "mild Fear", 2) is a `v` written
                    # without its {v, c} wrapper: the same value one level less
                    # nested, so it takes the same conversion below. Observed on
                    # the local Gemma 4 12B releasing a charge ("neutral") in
                    # BEST_PREY_GUESS's PATCH_VOV, which crashed the whole cycle.
                    raw_entry = {"v": raw_entry}
                if isinstance(raw_entry, dict):
                    # MS §3.5's textual redesign: `v` arrives as a word
                    # ("strong Fear") — convert back to the float this
                    # merge (and everything downstream) expects. Only
                    # needed here: a whole-new-VOV validation instead goes
                    # through models.py's own VectorObjectValence
                    # validator, which does the same conversion at that level.
                    raw_entry = convert_entry_v(nested_field, key, raw_entry)
                    if is_unconverted_numeric(nested_field, raw_entry):
                        # Unlike models.py's whole-VOV validator, nothing
                        # upstream of _coerce_patch catches a ValidationError
                        # here (_coerce_vov's try/except only guards the
                        # OTHER branch, a genuinely new object) — leaving
                        # this axis in would crash the entire cycle
                        # uncaught, not just fail one query's validation.
                        print(f"[warning] unparseable {nested_field} value for axis "
                              f"{key!r} on {existing.vov_id!r}: {raw_entry.get('v')!r} "
                              f"(MS §3.5 textual form not recognized) — dropping this axis")
                        continue
                try:
                    merged[key] = (
                        raw_entry if isinstance(raw_entry, entry_type)
                        else entry_type.model_validate(raw_entry)
                    )
                except ValidationError:
                    # Whatever shape this was, one malformed axis must not take
                    # the whole cycle down with it — same blank-cell tolerance
                    # as an unparseable value above.
                    print(f"[warning] malformed {nested_field} entry for axis {key!r} "
                          f"on {existing.vov_id!r}: {raw_entry!r} — dropping this axis")
                    continue
            out[nested_field] = merged
    for field, model_type in _NESTED_OBJECT_FIELDS.items():
        if field not in patch or patch[field] is None:
            continue
        raw_val = patch[field]
        if isinstance(raw_val, model_type):
            continue
        current = getattr(existing, field, None)
        merged_dict = {**(current.model_dump() if current is not None else {}), **raw_val}
        try:
            out[field] = model_type.model_validate(merged_dict)
        except ValidationError:
            print(f"[warning] {field} patch on {existing.vov_id!r} missing required "
                  f"fields and no existing {field} to merge onto, left unchanged: {raw_val}")
            out.pop(field, None)
    return out


def _affective_to_numeric(affective: Optional[list]) -> Optional[list]:
    """A WRITE_RELATION command's `affective` entries (MS §12.4) arrive with
    a textual `v` now (MS §3.5's redesign — an edge's affective weight is
    felt the same way a VOV's own Feeling is, always feeling-axis-keyed).
    `database.py`'s soften_charge later does real arithmetic on the stored
    value (`v * 0.5`), so the DB row must hold the float, not the word —
    this is the one place that conversion happens for an edge, the mirror
    of `convert_entry_v` used for a VOV's own feelings/ordinances/schemas.
    A `v` already numeric passes through unchanged; an unparseable text
    `v` is dropped from the list entirely — leaving it as text would let
    it reach the jsonb column fine (no crash here), only to blow up later
    inside soften_charge's real arithmetic (`v * 0.5` on a string)."""
    if not affective:
        return affective
    out = []
    for entry in affective:
        if not isinstance(entry, dict) or "axis" not in entry:
            continue
        v = entry.get("v")
        if isinstance(v, str):
            converted = text_to_feeling(entry["axis"], v)
            if converted is None:
                print(f"[warning] unparseable affective value for axis "
                      f"{entry['axis']!r}: {v!r} — dropping this affective entry")
                continue
            v = converted
        out.append({**entry, "v": v})
    return out


def _restore_scene_elements(db: Database, scene_elements: list) -> None:
    """Rev 0007 AM. An element Query 1 gave an EXISTING vov_id is in the scene by Query 1's own judgment (backed by its `same_as`); the cycle cannot
    write about it from the archive — MOV_UPDATE would refuse to overwrite an archived row and mint a second one for the same person. So after
    Query 4, an archived row named as a scene element returns to focus. Never an Objective (it is Liriel's own aim, MS §12.4) nor an Identity
    record (MainMemory, MS §6.13); every other decision about what leaves and returns stays Query 4's."""
    for el in scene_elements or []:
        vid = getattr(el, "vov_id", None)
        if not vid:
            continue
        row = db.get_object(vid)
        if row is None or not row.archived or row.object_nature in ("Objective", "Identity"):
            continue
        print(f"[notice] restored {vid!r}: Query 1 named it as an element of this scene")
        db.restore_object(vid)
    _restore_interpellations_of(db, scene_elements)


def _restore_interpellations_of(db: Database, scene_elements: list) -> None:
    """Rev 0007 AZ (MS §6.14). Pulling an agent pulls the Interpellations tied to it, to model her behaviour in function of it: for every agent Query 1 named as an
    element (Liriel's own row aside -- every Interpellation aimed at her would otherwise return every cycle), an ARCHIVED Interpellation tied to it by an Identity bond returns
    to focus. Structural: it reads the bonds and the natures, nothing of the content."""
    agent_ids = []
    for el in scene_elements or []:
        vid = getattr(el, "vov_id", None)
        if not vid or vid == settings.liriel_self_vov_id:
            continue
        row = db.get_object(vid)
        if row is not None and row.object_nature in _AGENT_OBJECT_NATURES:
            agent_ids.append(vid)
    if not agent_ids:
        return
    satellites = {r["from_vov_id"] for r in db.get_relations(agent_ids, ["Link_Identity_Part"]) if r["to_vov_id"] in agent_ids}
    for sid, row in db.get_objects(sorted(satellites)).items():
        if row.object_nature == "Interpellation" and row.archived:
            print(f"[notice] restored {sid!r}: an Interpellation tied to an agent of this scene")
            db.restore_object(sid)


def _apply_mainmemory_filing(db: Database, filing_result: "MainMemoryFilingResult") -> None:
    """MS §12.4. MAINMEMORY_FILING is, this revision, the only query with
    standing authority to move an Object's MOV<->MainMemory membership
    (MS §14 invariant 24) — both directions (`archive`/`restore`) are the
    same kind of decision (does this Object belong in active focus right
    now), just opposite ways, so one query owns both rather than splitting
    across ARCHIVE_VOV/RESTORE_VOV mov_ops as earlier revisions did."""
    for entry in filing_result.restore:
        row = db.get_object(entry.vov_id)
        if row is not None and row.object_nature == "Identity":
            # MS §6.13/§14 invariant 31: a record is MainMemory, reached by an explicit,
            # bounded request — never carried into the focus (the Graph of Traces lists record
            # ids so they can be cited, not so they can be restored).
            print(f"[notice] refused RESTORE of Identity record {entry.vov_id!r} — records stay in MainMemory (MS §6.13)")
            continue
        db.restore_object(entry.vov_id)
    for entry in filing_result.archive:
        if _is_protected(entry.vov_id):
            print(f"[notice] refused to archive protected vov_id={entry.vov_id!r} (core identity)")
            continue
        db.archive_object(entry.vov_id)


def _apply_write_relations(db: Database, write_relations: list) -> None:
    """MS §12.6A. WRITE_RELATION commands RELATIONS_UPDATE emits — run after
    MOV_UPDATE, so every id the model could name is already a real one; an
    id it invented anyway is caught here (both ends must exist)."""
    for cmd in write_relations:
        if not (cmd.get("from") and cmd.get("to") and cmd.get("kind")):
            continue
        from_id, to_id = cmd["from"], cmd["to"]
        from_obj, to_obj = db.get_object(from_id), db.get_object(to_id)
        if from_obj is None or to_obj is None:
            # Same reasoning as _ensure_relation_edges's own "relevant_
            # relations can point at a stale/typo'd id -- don't invent
            # an edge to nothing": this command's from/to isn't a
            # content judgment to second-guess, it's a mechanical
            # existence check. Confirmed for real: a WRITE_RELATION
            # named a vov_id ("Sentient_Camila_EsposaFabio") the model
            # never actually minted this cycle or any prior one --
            # writing it straight through crashed on a foreign-key
            # violation, losing the ENTIRE cycle's output (MS §11.1
            # sequencing means nothing this cycle decided is durable
            # until every query has concluded and committed together).
            missing = from_id if from_obj is None else to_id
            print(f"[warning] WRITE_RELATION names {missing!r}, which doesn't exist "
                  f"(not created this cycle, no prior row either) — skipped: {cmd}")
            continue
        from_nature, to_nature = from_obj.object_nature, to_obj.object_nature
        if "Identity" in (from_nature, to_nature):
            print(f"[notice] refused WRITE_RELATION {from_id!r} ({from_nature}) -> "
                  f"{to_id!r} ({to_nature}): an Identity record is linked to its Object by "
                  f"IDENTITY_UPDATE alone (MS §6.13) — skipped")
            continue
        if "Interpellation" in (from_nature, to_nature):
            # Rev 0007 AZ (MS §6.14): an Interpellation sits between its parties; it is tied to each by the Identity bond (satellite -> principal)
            # whose label says `origin` or `target`, and to the matter it came from by the cluster bond -- nothing else. Which Object is the
            # Interpellation is known by nature, so a bond written the wrong way round is turned the right way (a structural fix, no judgment).
            bond_kind = cmd["kind"]
            if "Interpellation" in (from_nature, to_nature) and from_nature == to_nature:
                print(f"[notice] refused WRITE_RELATION {from_id!r} -> {to_id!r}: an Interpellation is not tied to another Interpellation (MS §6.14) — skipped")
                continue
            if bond_kind == "Link_Subject_Cluster":
                if "ScenarioData" not in (from_nature, to_nature):
                    print(f"[notice] refused WRITE_RELATION {from_id!r} -> {to_id!r}: an Interpellation joins a matter only through its ScenarioData (MS §6.14) — skipped")
                    continue
            elif bond_kind == "Link_Identity_Part":
                role = str(cmd.get("label") or "").strip().lower()
                if role not in INTERPELLATION_ROLES:
                    print(f"[notice] refused WRITE_RELATION {from_id!r} -> {to_id!r}: an Interpellation's Identity bond carries label 'origin' or 'target' "
                          f"(got {cmd.get('label')!r}) (MS §6.14) — skipped")
                    continue
                party_nature = to_nature if from_nature == "Interpellation" else from_nature
                if party_nature in ("Identity", "ScenarioData", "Objective"):
                    print(f"[notice] refused WRITE_RELATION {from_id!r} ({from_nature}) -> {to_id!r} ({to_nature}): a {party_nature} is not a party to an Interpellation (MS §6.14) — skipped")
                    continue
                if to_nature == "Interpellation":
                    from_id, to_id = to_id, from_id  # satellite -> principal, always
                cmd = dict(cmd, label=role)
            else:
                print(f"[notice] refused WRITE_RELATION {from_id!r} -> {to_id!r} kind={bond_kind!r}: an Interpellation is tied only by the Identity bond "
                      f"(origin/target) and the cluster bond (MS §6.14) — skipped")
                continue
        if "Objective" in (from_nature, to_nature) and "ScenarioData" not in (from_nature, to_nature):
            # MS §6.10, same invariant _enforce_objective_cluster_only_
            # relations holds for relevant_relations -- but a model's
            # own explicit WRITE_RELATION command is a SEPARATE path
            # that bypasses that helper entirely (it writes straight to
            # mov_relations, keyed off this command's own from/to, not
            # off any VOV's relevant_relations). Confirmed for real:
            # relevant_relations came out correctly pruned to the
            # ScenarioData origin alone, and the model STILL emitted a
            # direct WRITE_RELATION from the Objective to a Sentient in
            # the same cycle's write_relations, which landed unchecked.
            # Refusing it here closes the other half of the same gap.
            print(f"[notice] refused WRITE_RELATION {from_id!r} ({from_nature}) -> "
                  f"{to_id!r} ({to_nature}): an Objective may only link to its "
                  f"ScenarioData origin (MS §6.10) — skipped")
            continue
        # No "shared member" veto here on purpose, same reasoning as
        # _ensure_relation_edges: whether two ScenarioData are the same
        # matter is the model's own call to make when it writes this
        # command — not code's to second-guess by a heuristic.
        kind = cmd["kind"]
        if kind not in _RELATION_KINDS:
            print(f"[notice] WRITE_RELATION kind={kind!r} isn't one of the closed six "
                  f"(MS §8.3) — writing as Link_Valence_Load instead")
            kind = "Link_Valence_Load"
        # Rev 0006: a bond is (from, to, kind, label); only what the command states is
        # written, so updating one bond never blanks a field or touches another.
        directed = _as_bool(cmd.get("directed"))
        if kind == "Link_Identity_Part":
            directed = True  # satellite -> principal is a direction, always (MS §6.9)
        db.write_relation(
            from_vov_id=from_id,
            to_vov_id=to_id,
            kind=kind,
            propositional=cmd.get("propositional"),
            affective=_affective_to_numeric(cmd.get("affective")) if "affective" in cmd else None,
            confidence=_clamp_confidence_value(cmd.get("confidence")),
            label=str(cmd.get("label") or "").strip()[:60],
            directed=directed,
            strength=_as_strength(cmd.get("strength")),
        )


def _as_bool(v) -> Optional[bool]:
    """A `directed` flag as the model may write it; anything unrecognized is "not stated"
    (the bond keeps what it says, or takes its kind's default)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        lowered = v.strip().lower()
        if lowered in ("true", "yes", "directed"):
            return True
        if lowered in ("false", "no", "mutual", "symmetric", "undirected"):
            return False
    return None


def _as_strength(v) -> Optional[int]:
    """Optional 1-5 intensity of a bond. Absent stays absent — never a zero."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(max(1, min(5, v)))


def _apply_soften_charge(db: Database, entries: list) -> None:
    """MS §12.6A. SOFTEN_CHARGE entries RELATIONS_UPDATE emits."""
    for entry in entries:
        if entry.vov_ids:
            db.soften_charge(entry.vov_ids)


# ---------------------------------------------------------------------------
# Query 6A — IDENTITY_UPDATE (MS §12.7A, Rev 0006)
# ---------------------------------------------------------------------------
#
# The ontology's Identity records (MS §6.13) document what Liriel LEARNED or CHANGED about
# a principal Object or one relation. Two halves, kept apart on purpose:
#   * the architecture's — a ledger of what differs between the committed state and this
#     cycle's draft (before/after, copied from the state it is about to overwrite), and the
#     bookkeeping of a record (id, link, time, immutability, no exact repeats);
#   * the model's — whether a difference is something LEARNED at all (a re-wording is not),
#     which kind of change it is, where it came from, how far to trust it.
# Nothing here decides what was learned. A difference the model does not turn into a record
# simply stays unrecorded (and visible in the cycle log) — the code never invents one.

# The fields of an Object that are the REPRESENTATION of it (what Liriel knows about it): its
# description, remarks, age, sex, standing, and its Modulating Schemas. Not Liriel's own
# reactions to it (Feelings, priority), nor the readings of a moment (Ordinances), nor a
# Objective's gains — those have their own queries.
_IDENTITY_OBJECT_FIELDS = ("brief_description", "relevant_remarks", "perceived_age", "male_female", "object_type")
# A ScenarioData is never edited (MS §6.10), an Objective is a pursuit rather than a thing
# learned about, and a record is not itself a subject of records.
_IDENTITY_EXCLUDED_NATURES = {"ScenarioData", "Objective", "Identity"}
# Structural bonds carry no learning: the matter a thing belongs to, and the record -> Object link.
_IDENTITY_EXCLUDED_RELATION_KINDS = {"Link_Subject_Cluster", "Link_Identity_Part"}


def _plain_text(v) -> str:
    return " ".join(str(v).split()) if v is not None else ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _affective_text(affective) -> str:
    return ", ".join(
        f"{e['axis']}: {feeling_to_text(e['axis'], e['v'])}"
        for e in sorted((affective or []), key=lambda e: e["axis"])
    )


def _object_ledger(before: VectorObjectValence, after: VectorObjectValence) -> List[tuple]:
    changes = []
    for field in _IDENTITY_OBJECT_FIELDS:
        b, a = _plain_text(getattr(before, field)), _plain_text(getattr(after, field))
        if b != a:
            changes.append((field, b or None, a or None))
    for key in sorted(set(before.schemas) | set(after.schemas)):
        be, ae = before.schemas.get(key), after.schemas.get(key)
        bt = schema_to_text(be.v) if be is not None else None
        at = schema_to_text(ae.v) if ae is not None else None
        if bt != at:  # the value; a confidence that moved alone is not a new reading
            changes.append((f"schemas.{key}", bt, f"{at} (c={ae.c})" if ae is not None and ae.c else at))
    return changes


def _relation_ledger(before: dict, after: dict) -> List[tuple]:
    """Every field of a bond that differs between the state on record and the draft: what it says,
    since when, which way it reads, and Liriel's charge, strength and confidence in it. Which of
    these differences is a LEARNING — and with what reliability — is the model's judgment alone
    (IDENTITY_UPDATE); the architecture only lists them. (A narrower ledger, written to stop QA's
    pile of empty records, was reverted: deciding that a field can never be a learning is a
    judgment, and it is not the code's.)"""
    changes = []
    for field in ("propositional", "since_text"):
        b, a = _plain_text(before.get(field)), _plain_text(after.get(field))
        if field == "propositional" and b == _UNTYPED_BOND_TEXT:
            continue  # a bond that was untyped being typed for the first time: stating it is not a change of it
        if b != a:
            changes.append((field, b or None, a or None))
    for field in ("confidence", "strength"):
        b, a = before.get(field), after.get(field)
        if b != a:
            changes.append((field, None if b is None else str(b), None if a is None else str(a)))
    if bool(before.get("directed")) != bool(after.get("directed")):
        changes.append(("directed", "directed" if before.get("directed") else "mutual",
                        "directed" if after.get("directed") else "mutual"))
    b, a = _affective_text(before.get("affective")), _affective_text(after.get("affective"))
    # A charge that eased with time (SOFTEN_CHARGE, MS §7.2) is the passing of time, already
    # stamped on the bond (`softened_at`), not something learned about it.
    if b != a and after.get("softened_at") == before.get("softened_at"):
        changes.append(("affective", b or None, a or None))
    return changes


def _identity_targets(db: DraftDatabase, top_mov_id: str) -> List[IdentityTarget]:
    """What changed THIS cycle in a persistent Object or relation: committed state vs draft.
    A thing born this cycle is not a change; neither is a row of a nested MOV (a mirror of
    someone's inner state, not a thing known about)."""
    targets: List[IdentityTarget] = []

    def numbered(changes: List[tuple]) -> List[IdentityLedgerChange]:
        return [IdentityLedgerChange(ref=f"c{i}", field=f, before=b, after=a) for i, (f, b, a) in enumerate(changes, 1)]

    for vov_id in sorted(db.drafted_vov_ids()):
        before, after = db.get_committed_object(vov_id), db.get_object(vov_id)
        if before is None or after is None:
            continue
        if before.object_nature in _IDENTITY_EXCLUDED_NATURES or after.object_nature in _IDENTITY_EXCLUDED_NATURES:
            continue
        if db.get_object_mov_id(vov_id) != top_mov_id:
            continue
        changes = _object_ledger(before, after)
        if changes:
            targets.append(IdentityTarget(
                kind="object", target_id=vov_id, label=vov_id, nature=after.object_nature,
                current_object=after, changes=numbered(changes),
            ))

    seen = set()
    for row in db.drafted_relations():
        if row["kind"] in _IDENTITY_EXCLUDED_RELATION_KINDS:
            continue
        ends = [row["from_vov_id"], row["to_vov_id"]]
        old, *_ = _plan_relation_write(db.get_committed_relations(ends, [row["kind"]]), *ends, row["kind"], row.get("label") or "", bool(row.get("directed")))
        new, *_ = _plan_relation_write(db.get_relations(ends, [row["kind"]]), *ends, row["kind"], row.get("label") or "", bool(row.get("directed")))
        if old is None or new is None or str(old["id"]) in seen:
            continue  # a bond born this cycle is not a change of one
        seen.add(str(old["id"]))
        changes = _relation_ledger(old, new)
        if not changes:
            continue
        objs = db.get_objects(ends)
        current = {
            **{k: new.get(k) for k in ("from_vov_id", "to_vov_id", "kind", "label", "directed", "propositional",
                                       "confidence", "strength", "since_text")},
            "affective": _affective_text(new.get("affective")),
            "from_description": objs[ends[0]].brief_description if ends[0] in objs else None,
            "to_description": objs[ends[1]].brief_description if ends[1] in objs else None,
        }
        arrow = "->" if new.get("directed") else "<->"
        targets.append(IdentityTarget(
            kind="relation", target_id=str(old["id"]),
            label=f"{old['from_vov_id']} {arrow} {old['to_vov_id']} [{old['kind']}{':' + old['label'] if old.get('label') else ''}]",
            current_relation=current, changes=numbered(changes),
        ))
    return targets


def _prior_identity_records(db: Database, target: IdentityTarget, limit: Optional[int]) -> list:
    if target.kind == "object":
        return db.get_identity_records(target_vov_ids=[target.target_id], limit=limit)
    return db.get_identity_records(relation_ids=[target.target_id], limit=limit)


def _run_identity_updates(db: DraftDatabase, top_mov_id: str, scenario: ScenarioData, cycle_id: str) -> Optional[dict]:
    """Query 6A. One call per changed target (the same one-judgment-at-a-time shape as the
    per-hunter and per-Object queries); none at all when nothing that already existed
    changed. Returns the audit entry for the cycle log (None when there was nothing to ask)."""
    targets = _identity_targets(db, top_mov_id)
    if not targets:
        return None
    for t in targets:
        t.prior_records = [
            graph_service._record_summary(r) for r in _prior_identity_records(db, t, settings.identity_records_default_limit)
        ]
    artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=MatrixObjectsValence(mov_id=top_mov_id), scenario_data=scenario,
    )

    raws = _batch_raws(
        "IDENTITY_UPDATE", lambda: build_identity_update_batch_prompt(artifacts, targets, cycle_id), targets,
        lambda t: [t.target_id], lambda e: [str(e.get("target_id") or "")],
        declared=("nothing_new", {"learned": "nothing new", "records": []}),
    )

    def ask(pair) -> IdentityUpdateResult:
        target, batch_raw = pair
        result = _validate_batch_entry(IdentityUpdateResult, batch_raw, f"IDENTITY_UPDATE {target.label}")
        if result is not None:
            return result
        raw = chat_json(
            build_identity_update_prompt(artifacts, target, cycle_id),
            temperature=settings.llm_temperature_update,
            effort=settings.llm_effort_update,
            model=settings.llm_model_structured,
        )
        return _validate_or_log(IdentityUpdateResult, raw, f"IDENTITY_UPDATE {target.label}")

    results = _fan_out("IDENTITY_UPDATE", "changed target(s)", ask, list(zip(targets, raws)))
    written: List[dict] = []
    skipped: List[dict] = []
    audit = []
    for target, result in zip(targets, results):
        w, sk = _write_identity_records(db, top_mov_id, target, result, cycle_id)
        written += w
        skipped += sk
        audit.append({
            "target": target.target_id, "kind": target.kind, "label": target.label,
            "changes": [c.model_dump() for c in target.changes],
            "proposals": [p.model_dump() for p in result.records], "notes": result.notes,
        })
    if settings.verbose:
        print(f"[debug] IDENTITY_UPDATE: {len(targets)} target(s), {len(written)} record(s) written, {len(skipped)} skipped")
    return {"cycle_id": cycle_id, "results": audit, "written": written, "skipped": skipped}


def _write_identity_records(
    db: Database, top_mov_id: str, target: IdentityTarget, result: IdentityUpdateResult, cycle_id: str
) -> tuple:
    """Turns the model's proposals for ONE target into Identity Objects: archived at birth
    (they are MainMemory, never the focus), immutable, and — for an Object — linked to it by
    `Link_Identity_Part` (record -> principal). The ref the model names is looked up in the
    ledger the architecture built, so before/after are the real overwritten values, whatever
    the model wrote; an id it echoes is never trusted."""
    written: List[dict] = []
    skipped: List[dict] = []
    on_file = _prior_identity_records(db, target, None)
    on_file_ids = {r.vov_id for r in on_file}
    used_refs: set = set()

    def skip(reason: str, ref: str) -> None:
        print(f"[notice] IDENTITY_UPDATE {target.label}: {reason} ({ref}) — no record written")
        skipped.append({"target": target.target_id, "ref": ref, "reason": reason})

    for p in result.records:
        change = next((c for c in target.changes if c.ref == p.change_ref), None)
        if change is None:
            skip("names a change that is not in the ledger", p.change_ref)
            continue
        if change.ref in used_refs:
            skip("a second record for the same change", p.change_ref)
            continue
        information = _plain_text(p.information)[:400]
        if not information:
            skip("empty information", p.change_ref)
            continue
        if any(
            r.identity_record.field == change.field
            and _plain_text(r.identity_record.value_after) == _plain_text(change.after)
            for r in on_file
        ):
            skip("the same information is already on file for this field (a re-reading, not a new learning)", p.change_ref)
            continue
        supersedes = p.supersedes if p.supersedes in on_file_ids else None
        if p.supersedes and supersedes is None:
            print(f"[notice] IDENTITY_UPDATE {target.label}: `supersedes` {p.supersedes!r} is not a record on "
                  f"file for this target — ignored")
        used_refs.add(change.ref)
        recorded_at = _now_iso()
        if target.kind == "object":
            block_target = dict(target_kind="object", target_vov_id=target.target_id)
            mov_id = db.get_object_mov_id(target.target_id) or top_mov_id
            nickname = f"Identity_{target.target_id}_{change.field}"
        else:
            cur = target.current_relation
            block_target = dict(target_kind="relation", relation=RelationRef(
                relation_id=target.target_id, from_vov_id=cur["from_vov_id"], to_vov_id=cur["to_vov_id"],
                kind=cur["kind"], label=cur.get("label") or "",
            ))
            mov_id = db.get_object_mov_id(cur["from_vov_id"]) or top_mov_id
            nickname = f"Identity_{cur['from_vov_id']}_{cur['kind']}_{change.field}"
        attribute = _plain_text(p.attribute) or change.field
        block = IdentityRecordBlock(
            **block_target, field=change.field, attribute=attribute, change_kind=p.change_kind,
            information=information, value_before=change.before, value_after=change.after,
            source=_plain_text(p.source) or None, obtained_via=p.obtained_via, reliability=p.reliability,
            recorded_at=recorded_at, occurred_at=_plain_text(p.occurred_at) or None,
            context=_plain_text(p.context)[:200] or None, supersedes=supersedes, cycle_id=cycle_id,
        )
        record = VectorObjectValence(
            vov_id=db.mint_vov_id(nickname, "Identity"), object_type="real", object_nature="Identity",
            valence_regime="State", brief_description=f"{attribute}: {information}"[:300],
            update_datetime=recorded_at, identity_record=block,
        )
        db.upsert_object(mov_id, record)
        db.archive_object(record.vov_id)
        if target.kind == "object":
            db.write_relation(
                from_vov_id=record.vov_id, to_vov_id=target.target_id, kind="Link_Identity_Part",
                propositional=attribute[:100], confidence=p.reliability, directed=True,
            )
        written.append({"vov_id": record.vov_id, "target": target.target_id, "field": change.field,
                        "change_kind": block.change_kind})
        on_file.append(record)
        on_file_ids.add(record.vov_id)
    return written, skipped


def _reviewable_rows(mov: MatrixObjectsValence) -> List[VectorObjectValence]:
    """The rows of ONE MOV that ANCHOR_REVIEW (MS §12.3B) is asked about:
    active, `valence_regime == "State"` (a `Delta` row is an Objective,
    whose `feelings` are its EXPECTED GAINS — the ruler for δ, MS §10 —
    not a charge anchored on a cause) and not a `ScenarioData` (never
    edited in place once written, MS §6.10 — a PATCH on one is refused).
    A structural filter on declared types, not a judgment about content:
    every other Object is reviewed, and what changes is the model's call."""
    return [
        o for o in mov.active()
        if o.valence_regime == "State" and o.object_nature not in ("ScenarioData", "Identity")
    ]


def _anchor_targets(mov: MatrixObjectsValence, nested_movs: list) -> List[AnchorTarget]:
    """Every (MOV, Object) pair ANCHOR_REVIEW runs for: each reviewable row
    of Liriel's own MOV (KQ10/KQ12, owner Liriel) and of every
    already-materialized nested MOV (KQ11/KQ13, owner = the hunter whose
    `nested_mov` points at it)."""
    liriel = mov.get(settings.liriel_self_vov_id)
    targets = [AnchorTarget(mov_id=mov.mov_id, vov=o, owner=liriel) for o in _reviewable_rows(mov)]
    pools = [mov] + list(nested_movs)
    for nested in nested_movs:
        owner = next((o for pool in pools for o in pool.objects if o.nested_mov == nested.mov_id), None)
        targets += [
            AnchorTarget(mov_id=nested.mov_id, vov=o, owner=owner, is_nested=True)
            for o in _reviewable_rows(nested)
        ]
    return targets


def _born_anchor_targets(db: DraftDatabase, mov: MatrixObjectsValence, nested_movs: list) -> List[AnchorTarget]:
    """The (MOV, Object) pairs of rows created THIS cycle — Liriel's MOV and every nested MOV —
    that the post-write ANCHOR_REVIEW (Query 5B) is asked about. A structural filter (drafted
    this cycle, no committed row behind it, the same reviewable kinds as Query 3B)."""
    born = {v for v in db.drafted_vov_ids() if db.get_committed_object(v) is None}
    return [t.model_copy(update={"born": True}) for t in _anchor_targets(mov, nested_movs) if t.vov.vov_id in born]


def _fan_out(label: str, noun: str, fn, items: list) -> list:
    """Runs one independent per-item call for every item (one per hunter, one
    per Object) and returns the results in item order. The calls share no
    state — each reads the shared scene artifacts and its own row — so
    `settings.fanout_concurrency` may run several at once; with 1 they run
    one after another. Any failure aborts the whole cycle, same as every
    other query (nothing is durable until BEST_PREY_GUESS concludes)."""
    workers = max(1, min(settings.fanout_concurrency, len(items) or 1))
    print(f"[info] {label}: {len(items)} {noun}, {workers} at a time")
    if workers == 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def _batch_enabled(n_items: int) -> bool:
    """Batch mode (MetaScheme Rev 0008, §12.12): worth it only when there is more than one item to ask about."""
    return settings.batch_mode and n_items >= 2


def _batch_raws(query: str, build, targets: list, target_keys, result_keys, declared: Optional[tuple] = None) -> list:
    """Rev 0008 §12.12. Asks ALL the items of one per-item query (every hunter, every Object under review, every changed target) in ONE
    call and returns, aligned with `targets`, the model's raw entry for each (None where the answer has none -- the caller then asks
    that item alone, exactly as without batch mode, so a batch that comes back short or wrong never costs the cycle an item).
    Entries are matched to items by the ids they echo (`target_keys(target)` / `result_keys(entry)` return the acceptable ids);
    only when none matches at all, and there are as many entries as items, are they read in the order asked. Judges nothing: it
    pairs what the model wrote with the item it was written for. `declared` = (field, template): ids the model lists under `field` instead of
    writing their entries are expanded to `template` (the plain "nothing to say" answer of that query, MS §12.12) -- an entry written in
    `results` always wins over a listing."""
    if not _batch_enabled(len(targets)):
        return [None] * len(targets)
    print(f"[info] {query}: {len(targets)} item(s) asked in ONE batch call")
    try:
        raw = chat_json(
            build(), temperature=settings.llm_temperature_update, effort=settings.llm_effort_update, model=settings.llm_model_structured,
        )
    except Exception as exc:  # noqa: BLE001 - a batch the model could not answer must not cost the cycle: each item is asked alone
        print(f"[notice] {query} batch failed ({type(exc).__name__}: {exc}); asking each item alone")
        return [None] * len(targets)
    results = raw.get("results") if isinstance(raw, dict) else None
    if not isinstance(results, list):
        print(f"[notice] {query} batch carried no `results` list; asking each item alone")
        return [None] * len(targets)
    by_key: dict = {}
    for entry in results:
        if isinstance(entry, dict):
            for k in result_keys(entry):
                by_key.setdefault(k, entry)
    if declared:
        listed = raw.get(declared[0])
        for ident in (listed if isinstance(listed, list) else []):
            if isinstance(ident, (str, int)):
                by_key.setdefault(str(ident), dict(declared[1]))
    aligned = [next((by_key[k] for k in target_keys(t) if k in by_key), None) for t in targets]
    if all(a is None for a in aligned) and len(results) == len(targets) and all(isinstance(r, dict) for r in results):
        aligned = list(results)
    seen: set = set()
    for i, entry in enumerate(aligned):  # one entry cannot answer two items
        if entry is not None:
            if id(entry) in seen:
                aligned[i] = None
            seen.add(id(entry))
    missing = sum(1 for a in aligned if a is None)
    if missing:
        print(f"[notice] {query} batch: {missing} of {len(targets)} item(s) not answered; asking those alone")
    return aligned


def _hunter_targets(db: Database, mov: MatrixObjectsValence, scene_elements: list) -> List[HunterTarget]:
    """Every hunter HUNTER_READING (MS §12.3A) is asked about: Liriel always
    (MS §9.4 front 2 — never absent from her own hunters list) and each
    element SCENE_SUBJECT_CHECK flagged `is_hunter` (KQ02 — the model's own
    answer; in a continuing matter that is what is new or changed). An
    element with a `vov_id` carries its row when one exists, in focus or in
    MainMemory; a brand-new one has only its `provisional_label`."""
    liriel_id = settings.liriel_self_vov_id
    liriel = mov.get(liriel_id)
    targets = [HunterTarget(
        label=liriel_id,
        element=ElementEntry(vov_id=liriel_id, object_nature="PCI", is_hunter=True, new_this_cycle=False),
        vov=liriel, is_liriel=True, liriel=liriel,
    )]
    seen = {liriel_id}
    for el in scene_elements:
        if not el.is_hunter:
            continue
        label = el.vov_id or el.provisional_label
        if not label or label in seen:
            continue
        seen.add(label)
        row = (mov.get(el.vov_id) or db.get_object(el.vov_id)) if el.vov_id else None
        targets.append(HunterTarget(label=label, element=el, vov=row, liriel=liriel))
    return targets


def _run_hunter_readings(
    artifacts: Artifacts, targets: List[HunterTarget], cycle_id: str
) -> List[HunterSceneReading]:
    """One HUNTER_READING call per hunter (MS §12.3A, KQ07-KQ09). The label
    the model echoes is ignored: each reading is filed under the hunter
    actually asked about."""
    raws = _batch_raws(
        "HUNTER_READING", lambda: build_hunter_reading_batch_prompt(artifacts, targets, cycle_id), targets,
        lambda t: [t.label], lambda e: [str(e.get("vov_id_or_label") or "")],
    )

    def read(pair) -> HunterSceneReading:
        target, batch_raw = pair
        r = _validate_batch_entry(HunterReadingResult, batch_raw, f"HUNTER_READING {target.label}")
        if r is None:
            raw = chat_json(
                build_hunter_reading_prompt(artifacts, target),
                temperature=settings.llm_temperature_update,
                effort=settings.llm_effort_update,
                model=settings.llm_model_structured,
            )
            r = _validate_or_log(HunterReadingResult, raw, f"HUNTER_READING {target.label}")
        return HunterSceneReading(
            vov_id_or_label=target.label, relation_to_liriel=r.relation_to_liriel,
            ordinances_read=r.ordinances_read, schemas_read=r.schemas_read,
            supposed_prey=r.supposed_prey, needs_own_mov=r.needs_own_mov, feels_about=r.feels_about, notes=r.notes,
        )

    readings = _fan_out("HUNTER_READING", "hunter(s) to read", read, list(zip(targets, raws)))
    if settings.verbose:
        for r in readings:
            print(f"[debug] HUNTER_READING {r.vov_id_or_label}: {r.model_dump_json(indent=2)}")
    return readings


def _run_anchor_reviews(
    artifacts: Artifacts, targets: List[AnchorTarget], cycle_id: str
) -> List[AnchorReviewResult]:
    """One ANCHOR_REVIEW call per target (MS §12.3B). The ids the model
    echoes are overwritten with the target actually asked about — never
    trusted."""
    once = {vid for vid in (t.vov.vov_id for t in targets) if sum(1 for t in targets if t.vov.vov_id == vid) == 1}
    raws = _batch_raws(
        "ANCHOR_REVIEW", lambda: build_anchor_review_batch_prompt(artifacts, targets, cycle_id), targets,
        lambda t: [f"{t.mov_id}::{t.vov.vov_id}"] + ([t.vov.vov_id] if t.vov.vov_id in once else []),
        lambda e: [f"{e.get('mov_id')}::{e.get('vov_id')}", str(e.get("vov_id") or "")],
        declared=("untouched", {"touches_this_row": "no", "evidence": [], "schemas_changes": [], "missing_cause": None}),
    )

    def review(pair) -> AnchorReviewResult:
        target, batch_raw = pair
        result = _validate_batch_entry(AnchorReviewResult, batch_raw, f"ANCHOR_REVIEW {target.vov.vov_id}")
        if result is None:
            raw = chat_json(
                build_anchor_review_prompt(artifacts, target),
                temperature=settings.llm_temperature_update,
                effort=settings.llm_effort_update,
                model=settings.llm_model_structured,
            )
            result = _validate_or_log(AnchorReviewResult, raw, f"ANCHOR_REVIEW {target.vov.vov_id}")
        return result.model_copy(update={
            "cycle_id": cycle_id, "mov_id": target.mov_id, "vov_id": target.vov.vov_id,
            "owner_vov_id": target.owner.vov_id if target.owner else None, "born": target.born,
        })

    results = _fan_out("ANCHOR_REVIEW", "Object(s) to review", review, list(zip(targets, raws)))
    if settings.verbose:
        for r in results:
            print(f"[debug] ANCHOR_REVIEW {r.vov_id}: {r.model_dump_json(indent=2)}")
    return results


def _ops_from_anchor_reviews(
    reviews: List[AnchorReviewResult], top_mov_id: str
) -> tuple[list[MovOp], list[NestedMovOp]]:
    """KQ10-13 answers translated into the PATCH_VOV/PATCH_NESTED_VOV shape
    MOV_UPDATE's own ops already use, so _apply_mov_ops/_apply_nested_mov_ops
    (and _coerce_patch's one-axis-at-a-time merge) carry them out unchanged.
    Pure transform, no DB access: mechanical execution of an already-fully-
    specified decision, not a fresh judgment — every row/axis/value came
    from the model's own answer for that one Object."""
    mov_ops: List[MovOp] = []
    nested_ops: List[NestedMovOp] = []
    for r in reviews:
        patch: dict = {}
        if r.feelings_changes:
            patch["feelings"] = {c.axis: {"v": c.v, "c": c.c} for c in r.feelings_changes}
        if r.schemas_changes:
            patch["schemas"] = {c.schema_name: {"v": c.v, "c": c.c} for c in r.schemas_changes}
        if not patch:
            continue
        if r.mov_id == top_mov_id:
            mov_ops.append(MovOp(op="PATCH_VOV", vov_id=r.vov_id, patch=patch, reason="MS §12.3B KQ10/12"))
        else:
            nested_ops.append(NestedMovOp(
                op="PATCH_NESTED_VOV", mov_id=r.mov_id, vov_id=r.vov_id, patch=patch, reason="MS §12.3B KQ11/13",
            ))
    return mov_ops, nested_ops


def _without_noop_releases(db: Database, review: AnchorReviewResult) -> AnchorReviewResult:
    """A `"neutral"` on an axis the row never carried releases nothing: written as-is it would
    leave an explicit 0 where the axis was blank (MS §6.6: blank is not zero) — QA wave 1 found
    every reviewed row dotted with them. Structural: it looks only at whether the axis is set."""
    if not review.feelings_changes:
        return review
    row = db.get_object(review.vov_id)
    if row is None:
        return review
    kept = [c for c in review.feelings_changes if not (c.v.strip().lower() == "neutral" and c.axis not in row.feelings)]
    return review if len(kept) == len(review.feelings_changes) else review.model_copy(update={"feelings_changes": kept})


def _without_schemas_on_non_agents(db: Database, review: AnchorReviewResult) -> AnchorReviewResult:
    """Rev 0007 AX. Modulating Schemas describe a PLAYER (MS §5): an agent row (person, animal, group, entity, Liriel). A Schema change a review writes
    on any other row (a situation, a thing, an idea) is not carried out. Structural: it looks only at the row's own `object_nature`."""
    if not review.schemas_changes:
        return review
    row = db.get_object(review.vov_id)
    if row is None or row.object_nature in _AGENT_OBJECT_NATURES:
        return review
    return review.model_copy(update={"schemas_changes": []})


_FREE_TEXT_SCHEMA_KEYS = frozenset({"Culture", "PersonalityNaturalAbility", "MindVices", "MentalDisorders", "BodyFeatures"})  # MS §5.3, §5.5, §5.7: text, not scales


def _without_free_text_schemas_on_self(review: AnchorReviewResult) -> AnchorReviewResult:
    """Rev 0007 AX. The free-text Schemas (a body feature, a diagnosis, a vice, a gift, a culture) of LIRIEL's own row are not written from what a
    person reports: QA wrote Beatriz's anxiety disorder and her daily wine onto Liriel's row. Structural: it looks only at the row's id and the Schema's name."""
    if review.vov_id != settings.liriel_self_vov_id or not review.schemas_changes:
        return review
    kept = [c for c in review.schemas_changes if c.schema_name not in _FREE_TEXT_SCHEMA_KEYS]
    return review if len(kept) == len(review.schemas_changes) else review.model_copy(update={"schemas_changes": kept})


def _apply_anchor_reviews(db: Database, top_mov_id: str, reviews: List[AnchorReviewResult]) -> None:
    """Carries out every KQ10-13 answer (see _ops_from_anchor_reviews)."""
    reviews = [_without_noop_releases(db, _without_free_text_schemas_on_self(_without_schemas_on_non_agents(db, r))) for r in reviews]
    mov_ops, nested_ops = _ops_from_anchor_reviews(reviews, top_mov_id)
    if mov_ops:
        _apply_mov_ops(db, top_mov_id, mov_ops)
    if nested_ops:
        _apply_nested_mov_ops(db, nested_ops)
