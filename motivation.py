"""
The ProcessMotivation cycle, aligned to the official MetaScheme
(docs/MetaScheme_Liriel_Rev0000.md, "MS" below), MS §11.1.

Four LLM calls per cycle:
  1. GRAPH_REQUEST (MS §12.1) — which Objects' relations are worth surveying.
  2. MOV_MAINMEMORY_UPDATE (MS §12.3) — retrospective + prospective mov_ops.
  3. BEST_PREY_GUESS (MS §12.5) — the Guess, judged, plus a handoff.
  4. Phase-1-only: turn the handoff into Liriel's actual chat reply (no MS
     §12 contract covers this — see prompts.build_reply_prompt).

Between 1 and 2, TrackGraphProcess (graph_service.py) — a service, not an
LLM call — turns Query 1's `requests` into the real GraphOfTraces (MS §8),
traversing mov_relations (the store WRITE_RELATION/SOFTEN_CHARGE write to)
across the focus and the archive. Two more services run alongside it:
graph_service.search_memory, once per cycle against the raw message, and
again right after Query 2 against any SEARCH mainmemory_commands it
emitted — together a real implementation of MS §12.4 SEARCH (keyword/fuzzy
matching over every Object's own text, any nature, not just names), which
this codebase only ever logged as unimplemented before now.

Backend (config.py's LLM_BACKEND, default "litellm" in this fork): all
four calls run through llm_client.py/litellm to Groq by default (see
.env.example) — a larger hosted model, after a smaller self-hosted one
kept missing nuanced MetaScheme distinctions even with explicit,
worked-example-level prompting. Setting LLM_BACKEND=llamacpp switches all
four calls to a self-hosted llama-server (scripts/llamacpp/) instead —
every inference stays under the implementer's own control, no closed
external API sees Liriel's state (MS §17.6 "weight ownership"); this was
the original project's default. Voice notes (Telegram STT/TTS) always go
through OpenAI via llm_client.py regardless of this setting — that's I/O
plumbing, not cognition, and the cost there is negligible.

Under LLM_BACKEND=litellm, calls 1-3 (structured-JSON extraction, not
the voice the user actually hears) run on config.py's LLM_MODEL_STRUCTURED
(a cheaper/faster model, when set), while call 4 stays on LLM_MODEL,
since that's the one call where model quality is directly audible/
readable to the user. Both default to the same value, so this is opt-in
(.env), not a behavior change on its own. llamacpp_client.py has no
equivalent split — one local server serves all four.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, get_args

from pydantic import ValidationError

import graph_service
from config import settings
from database import Database

# config.LLM_BACKEND picks which of the two ends up bound to chat/chat_json
# here — both modules expose the same (messages, temperature, effort, model)
# signature, so nothing below this needs to know which one it's actually
# talking to. "llamacpp" is the self-hosted, local-only path
# (scripts/llamacpp/); "litellm" (this fork's default) routes through
# llm_client.py/litellm to whatever LLM_MODEL's own prefix names — Groq by
# default here, but Anthropic/OpenAI/etc. work the same way.
if settings.llm_backend == "litellm":
    from llm_client import chat, chat_json
else:
    from llamacpp_client import chat, chat_json
from models import (
    Artifacts,
    AxisValence,
    BestPreyGuessResult,
    DeltaReport,
    GraphRequestResult,
    MatrixObjectsValence,
    MovMainMemoryUpdateResult,
    MovOp,
    NestedMovOp,
    ObjectiveBlock,
    RelationKind,
    ScenarioData,
    SchemaEntry,
    VectorObjectValence,
)
from valence_text import convert_entry_v, is_unconverted_numeric, text_to_feeling

_RELATION_KINDS = set(get_args(RelationKind))
from prompts import (
    META_SCHEME,
    build_decision_prompt,
    build_graph_request_prompt,
    build_reply_prompt,
    build_update_prompt,
)


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


def run_motivation_cycle(
    db: Database, mov: MatrixObjectsValence, user_text: str, source: str = "terminal_chat"
) -> tuple[str, MatrixObjectsValence, Optional[str]]:
    """Runs one full ProcessMotivation cycle for a single chat message.

    `source` labels ScenarioData.source (MS §9.3 leaves the embodiment's
    form open) — e.g. "telegram" when telegram_bot.py is the front end
    instead of main.py's terminal loop; purely informational for now.

    Returns (response_text, refreshed_mov, output_modality) — the third
    element is "voice"/"text" only when the user explicitly asked for that
    reply channel (MS §11: ProcessCommandControl's call, via the Query 3
    handoff), else None, meaning the front end should mirror whatever
    channel the message itself arrived on.
    """
    scenario = ScenarioData(text=user_text, source=source)
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

    # --- Query 1: GRAPH_REQUEST (MS §12.1) ---------------------------------
    graph_artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
        graph_of_traces=None, scenario_data=scenario,
    )
    graph_request_raw = chat_json(
        build_graph_request_prompt(graph_artifacts),
        temperature=settings.llm_temperature_update,
        effort=settings.llm_effort_graph,
        model=settings.llm_model_structured,
    )
    graph_request = _validate_or_log(GraphRequestResult, graph_request_raw, "GRAPH_REQUEST")

    if settings.verbose:
        print(f"[debug] GRAPH_REQUEST: {graph_request.model_dump_json(indent=2)}")

    # deep_recall_requested (MS §12.1): the user explicitly insisted Liriel
    # make a real effort to remember something. Until now this only raised
    # GRAPH_MAX_NODES on a traversal from an already-known anchor — it did
    # nothing for the automatic search above, so a weak/wrong contextual
    # hit could still cut a deliberately-insisted-on search short before it
    # ever reached the full archive. Force it here: redo that search with
    # force_blind=True so an explicit "please really try to remember"
    # always gets the real, unrestricted lookup, not just a bigger cap on
    # whatever the cheap pass already (maybe wrongly) settled on.
    if any(r.deep_recall_requested for r in graph_request.requests):
        auto_results = graph_service.search_memory(db, mov, search_context_text, force_blind=True)
        auto_hits = [c["vov_id"] for c in auto_results]
        if settings.verbose:
            print(f"[debug] deep_recall_requested — forced blind search hits: {auto_hits}")
        auto_requests = _requests_with_cluster_recall(
            db, auto_results, reason="auto: deep recall, forced blind search"
        )

    # --- Graph generation: TrackGraphProcess (service, not an LLM call) ---
    # Whatever the model itself asked to survey, PLUS whatever memory
    # search found on its own — one graph, one traversal, ranked by
    # (relevance to this message, emotional charge, recency) in that order.
    graph_of_traces = graph_service.build_graph_of_traces(
        db, list(graph_request.requests) + auto_requests, scenario_text=search_context_text
    )
    if settings.verbose and graph_of_traces:
        boosted = any(r.deep_recall_requested for r in graph_request.requests)
        print(
            f"[debug] GraphOfTraces: {len(graph_of_traces['nodes'])} node(s), "
            f"{len(graph_of_traces['edges'])} edge(s)"
            + (" [deep recall boosted]" if boosted else "")
        )

    # --- Query 2: MOV_MAINMEMORY_UPDATE (MS §12.3) ------------------------
    # MS §7.6 (this session's redesign): Query 2 may ask for another round
    # of its own retrieval against whatever the previous round turned up,
    # bounded by max_retrieval_subqueries — a genuinely new loop where
    # Query 1/graph/Query 2/Query 3 used to run strictly once each. A model
    # that never sets retrieval_satisfied=false runs this exactly once,
    # identical to the old single-pass behavior.
    all_cycle_touched_ids: set = set()
    for retrieval_round in range(settings.max_retrieval_subqueries):
        update_artifacts = Artifacts(
            meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
            graph_of_traces=graph_of_traces, scenario_data=scenario,
        )
        update_raw = chat_json(
            build_update_prompt(update_artifacts),
            temperature=settings.llm_temperature_update,
            effort=settings.llm_effort_update,
            model=settings.llm_model_structured,
        )
        update_result = _validate_or_log(MovMainMemoryUpdateResult, update_raw, "MOV_MAINMEMORY_UPDATE")

        if settings.verbose:
            print(f"[debug] MOV_MAINMEMORY_UPDATE (retrieval round {retrieval_round + 1}"
                  f"/{settings.max_retrieval_subqueries}): {update_result.model_dump_json(indent=2)}")

        _apply_retrospective(db, update_result)
        id_remap = _apply_mov_ops(db, mov.mov_id, update_result.prospective.mov_ops)
        if id_remap:
            _relink_new_objects(db, mov.mov_id, id_remap)
            update_result.mainmemory_commands = _remap_mainmemory_commands(
                update_result.mainmemory_commands, id_remap
            )
            update_result.prospective.nested_mov_ops = [
                op.model_copy(update={"owner_vov_id": id_remap.get(op.owner_vov_id, op.owner_vov_id)})
                if op.owner_vov_id else op
                for op in update_result.prospective.nested_mov_ops
            ]
        _apply_mainmemory_commands(db, update_result.mainmemory_commands)
        _apply_nested_mov_ops(db, update_result.prospective.nested_mov_ops)
        all_cycle_touched_ids |= _cycle_touched_ids(update_result, id_remap)

        # MS §12.4 SEARCH, run for real with this round's own terms — this is
        # what lets Liriel "think about it some more" instead of having to
        # resolve an unrecognized name/topic in one shot: Query 2 names what
        # it's looking for (a SEARCH mainmemory_command), the search runs
        # immediately against the terms it chose (not just the raw message),
        # and the graph is rebuilt to include whatever turns up — same cycle,
        # before Query 3 runs.
        search_hits = [c["vov_id"] for c in graph_service.run_search_commands(db, mov, update_result.mainmemory_commands)]
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
                cmd.get("query") for cmd in update_result.mainmemory_commands
                if isinstance(cmd, dict) and cmd.get("op") == "SEARCH" and cmd.get("query")
            ]
            if search_queries:
                # MS §8.6: a SEARCH that finds nothing is the exact signal that
                # something is genuinely new — it is not a dead end to drop
                # silently. Without this, Query 3 never learns Query 2 even
                # looked, so neither of §8.6's two paths (create it / ask about
                # it) gets taken by default. Confirmed for real: a SEARCH for
                # three new family members Query 2 itself asked for came back
                # empty, Query 3 proceeded as if no search had been attempted,
                # and the reply used their names from the raw ScenarioData
                # anyway — nothing looked wrong in the reply text, but none of
                # them were ever written to MainMemory, so the next cycle that
                # needs one of them starts from zero again.
                graph_of_traces = dict(graph_of_traces) if graph_of_traces else {
                    "artifact": "GraphOfTraces", "requested_for": [], "nodes": [], "edges": [],
                }
                graph_of_traces["unresolved_searches"] = search_queries

        mov = db.load_mov(mov.mov_id)  # reload: reflects everything this round just did
        # nested_mov_ops may have just created/changed one — recompute rather
        # than reuse the pre-round list.
        nested_movs = _load_nested_movs(db, mov, settings.nested_mov_max_depth)

        if update_result.retrieval_satisfied:
            break
        if retrieval_round == settings.max_retrieval_subqueries - 1:
            print(f"[notice] MOV_MAINMEMORY_UPDATE hit MAX_RETRIEVAL_SUBQUERIES="
                  f"{settings.max_retrieval_subqueries} while still wanting more retrieval "
                  f"(retrieval_satisfied=false) — proceeding with what was retrieved this cycle")

    # --- Query 3: BEST_PREY_GUESS (MS §12.5) ------------------------------
    decision_artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
        graph_of_traces=graph_of_traces, scenario_data=scenario,
    )
    decision_raw = chat_json(
        build_decision_prompt(decision_artifacts),
        temperature=settings.llm_temperature_decision,
        effort=settings.llm_effort_decision,
        model=settings.llm_model_structured,
    )
    decision_result = _validate_or_log(BestPreyGuessResult, decision_raw, "BEST_PREY_GUESS")

    if settings.verbose:
        print(f"[debug] BEST_PREY_GUESS: {decision_result.model_dump_json(indent=2)}")

    # MS §14.6: exactly one Objective at priority 1, priorities unique and
    # contiguous — enforce it rather than trust a small local model's count.
    # Accumulated across every retrieval round above (MS §7.6), not just the
    # last — an id a model touched in an earlier round is exactly as
    # legitimate a re-election basis as one touched in the final round.
    cycle_touched_ids = all_cycle_touched_ids
    guess = decision_result.best_prey_guess.model_copy(
        update={
            "vov_id": _resolve_id(db, decision_result.best_prey_guess.vov_id, cycle_touched_ids),
            "priority": 1, "valence_regime": "Delta", "object_nature": "Objective",
        }
    )
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
    _apply_mov_ops(db, mov.mov_id, decision_result.mov_ops)

    mov = db.load_mov(mov.mov_id)
    _renumber_stale_objectives(db, mov, ranked_ids)
    mov = db.load_mov(mov.mov_id)
    _evict_stale_clusters(db, mov)
    mov = db.load_mov(mov.mov_id)

    # --- Phase 1 bridge: compose the actual chat reply --------------------
    # MS §2.6/§4/§5: the reply is Liriel's move, and no move is Feelings
    # alone — it's her own current Ordinances-in-operation, narrowed by her
    # Restrictive Schemas, that make it hers. VOV_0000 carries all three.
    liriel_self = mov.get("VOV_0000")
    reply_artifacts = Artifacts(
        meta_scheme=META_SCHEME, mov=mov, nested_movs=nested_movs,
        graph_of_traces=graph_of_traces, scenario_data=scenario,
    )
    response_text = chat(
        build_reply_prompt(decision_result, user_text, liriel_self, reply_artifacts),
        temperature=settings.llm_temperature_decision,
    ).strip()

    db.log_cycle(
        mov_id=mov.mov_id,
        scenario_text=user_text,
        update_result=update_result.model_dump(),
        decision_result=decision_result.model_dump(),
        response_text=response_text,
        graph_of_traces=graph_of_traces,
    )

    # ProcessCommandControl's call (MS §11), not ProcessMotivation's — None
    # means the user didn't explicitly ask for a specific reply channel, so
    # the front end mirrors whatever channel the message itself arrived on.
    handoff = decision_result.handoff_to_processcommandcontrol
    output_modality = handoff.preferred_output_modality if handoff else None

    return response_text, mov, output_modality


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


def _apply_retrospective(db: Database, update_result: MovMainMemoryUpdateResult) -> None:
    mov_id = settings.default_mov_id  # Phase 1 only ever runs one MOV
    for entry in update_result.retrospective:
        existing = db.get_object(entry.vov_id)
        if existing is None:
            print(f"[warning] retrospective action on unknown vov_id={entry.vov_id!r}, skipped")
            continue
        if existing.archived:
            # MS §11.1 step 3 / §12.3: retrospective reviews an Objective
            # "still open from a previous cycle" — an ARCHIVED row is not
            # open, whatever the model's own retrospective entry claims.
            # Confirmed for real: an archived Objective (an old, resolved
            # "English-speaking delegate" matter) surfaced in GraphOfTraces
            # (MS §8.1 legitimately includes archived candidates for
            # context) and the model reviewed it as KEEP_PENDING anyway —
            # db.upsert_object unconditionally clears archived_at on
            # write, so honoring that review silently reactivated a
            # retired row, and the very next step (BEST_PREY_GUESS)
            # elected that now-active id for a completely unrelated new
            # matter, overwriting the delegate's own content outright.
            # Reviving an archived Objective on purpose still has a real
            # path — RESTORE_VOV (mov_ops) or RETRIEVE (mainmemory_
            # commands) — both explicit and both run before this point in
            # the cycle; a retrospective entry alone is never enough.
            print(f"[warning] retrospective action on archived vov_id={entry.vov_id!r} "
                  f"({entry.action!r}) ignored — it is not an open Objective; use "
                  f"RESTORE_VOV/RETRIEVE first to revive it on purpose")
            continue

        # MS §10.8: the interim report is not optional — whatever this
        # cycle learned (or didn't) about a still-open Objective is written
        # back onto its own row via relevant_remarks, not only argued for
        # in this turn's own JSON and then lost the moment the cycle ends.
        # "No feedback yet" is as legitimate an entry here as "partially
        # attained" — the trail is what makes the eventual delta_report
        # traceable, not just its final number. Applies regardless of
        # which action fires; previously only SET_DELTA_REPORT persisted
        # anything, and even that ignored `reason` in favor of the
        # delta_report's own attribution_note.
        updates: dict = {"relevant_remarks": entry.reason} if entry.reason else {}

        if entry.action == "SET_DELTA_REPORT":
            updates["delta_report"] = entry.delta_report
        elif entry.action in ("KEEP_PENDING", "KEEP_PENDING_URGENT"):
            if entry.new_priority is not None:
                updates["priority"] = entry.new_priority
        elif entry.action == "REPRIORITIZE":
            updates["priority"] = entry.new_priority
        elif entry.action in ("ARCHIVE", "ABANDON"):
            if _is_protected(entry.vov_id):
                print(f"[notice] refused to archive protected vov_id={entry.vov_id!r} (core identity)")
                continue
            if entry.action == "ABANDON" and existing.objective is not None:
                updates["objective"] = existing.objective.model_copy(update={"status": "abandoned"})

        if updates:
            existing = existing.model_copy(update=updates)
            _upsert_and_link(db, mov_id, existing)

        if entry.action in ("ARCHIVE", "ABANDON"):
            db.archive_object(entry.vov_id)


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
            if existing is not None and _nature_conflicts(existing, op.vov.get("object_nature")):
                # Same collision _upsert_nested_row already guards against,
                # just never checked here: an id the model invented for a
                # genuinely new row can coincide with an id some OTHER call
                # in this same cycle also invented for something completely
                # different — confirmed for real, Query 2 UPSERT_VOV'd a new
                # Person (Michele) under a self-guessed id that happened to
                # already be a standing Objective; existing-is-not-None made
                # this look like an intentional patch, so Michele's fields
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
        _ensure_relation_edges(db, vov)


def _remap_mainmemory_commands(commands: list, id_remap: dict) -> list:
    """MS §12.4 commands are raw dicts straight from the model's own JSON
    (not re-validated against vov_ids that exist), so a WRITE_RELATION (or
    RETRIEVE/ARCHIVE/SOFTEN_CHARGE) naming one of this cycle's own
    just-minted objects by its pre-remap guessed id needs the same fix-up
    _apply_mov_ops's id_remap gives everything else, or it fails the same
    way against the real archive (WRITE_RELATION: a foreign-key violation;
    the others: a silent no-op on an id nothing ever used)."""
    if not id_remap:
        return commands
    remapped = []
    for cmd in commands:
        cmd = dict(cmd)
        if cmd.get("from") in id_remap:
            cmd["from"] = id_remap[cmd["from"]]
        if cmd.get("to") in id_remap:
            cmd["to"] = id_remap[cmd["to"]]
        if cmd.get("vov_ids"):
            cmd["vov_ids"] = [id_remap.get(v, v) for v in cmd["vov_ids"]]
        remapped.append(cmd)
    return remapped


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


def _scenario_datas_share_member(db: Database, vov: VectorObjectValence, other_sd_id: str) -> bool:
    """True only when `vov` and `other_sd_id` (both `ScenarioData`) share at
    least one real concerned party — a Sentient, Situation, or the like,
    never another `ScenarioData`. MS §6.10/§14.19: two `ScenarioData` rows
    belong to the same backbone only when the report is genuinely the same
    matter continuing, which in practice always means they are about at
    least one of the same people/situations — two matters that merely
    share the one person who reports everything to Liriel (MS §13.1's
    universal reporter, e.g. Fábio) do NOT thereby become the same matter.
    A `ScenarioData` neighbor is deliberately excluded from both sides of
    this comparison — chaining through one to justify linking to another
    is exactly how one wrong edge merges two unrelated clusters into one
    (see _link_scenario_data_siblings's own docstring for the confirmed
    incident this guards against from the other direction)."""
    vov_member_ids = {rid for rid in vov.relevant_relations if rid != other_sd_id}
    if not vov_member_ids:
        return False
    vov_members = db.get_objects(list(vov_member_ids))
    vov_member_ids = {
        rid for rid in vov_member_ids
        if vov_members.get(rid) and vov_members[rid].object_nature != "ScenarioData"
    }
    if not vov_member_ids:
        return False
    other_neighbor_ids = {
        (row["to_vov_id"] if row["from_vov_id"] == other_sd_id else row["from_vov_id"])
        for row in db.get_relations([other_sd_id], ["Link_Subject_Cluster"])
    }
    other_neighbors = db.get_objects(list(other_neighbor_ids))
    other_member_ids = {
        nid for nid in other_neighbor_ids
        if other_neighbors.get(nid) and other_neighbors[nid].object_nature != "ScenarioData"
    }
    return bool(vov_member_ids & other_member_ids)


def _scenario_data_ids_share_member(db: Database, sd_a_id: str, sd_b_id: str) -> bool:
    """Same check as `_scenario_datas_share_member`, for two already-
    persisted `ScenarioData` ids — used by the WRITE_RELATION mainmemory-
    command path, where (unlike `_ensure_relation_edges`'s in-progress
    `vov`) both sides are already fully written with their own edges by
    the time this runs (`_apply_mov_ops` always completes before
    `_apply_mainmemory_commands` in the same round)."""
    def _members(sd_id: str) -> set:
        neighbor_ids = {
            (row["to_vov_id"] if row["from_vov_id"] == sd_id else row["from_vov_id"])
            for row in db.get_relations([sd_id], ["Link_Subject_Cluster"])
        }
        neighbors = db.get_objects(list(neighbor_ids))
        return {nid for nid in neighbor_ids if neighbors.get(nid) and neighbors[nid].object_nature != "ScenarioData"}

    a_members = _members(sd_a_id)
    if not a_members:
        return False
    return bool(a_members & _members(sd_b_id))


def _ensure_relation_edges(db: Database, vov: VectorObjectValence) -> None:
    """Every relation a VOV claims via `relevant_relations` (MS §6.4)
    becomes a real, walkable `mov_relations` edge (MS §8) — a fallback
    (see _fallback_relation_kind) wherever nothing more specific already
    connects the pair — regardless of whether the model also remembered to
    emit its own WRITE_RELATION mainmemory_command this cycle.
    `relevant_relations` and `mov_relations` are different things (MS §6.4
    vs §8), and prompts.py already asks the model to keep them in sync, but
    a small local model doesn't always comply. Confirmed for real:
    Sebastiana (VOV_0035) correctly listed Eduardo (VOV_0032) in her own
    `relevant_relations` — and had zero rows in `mov_relations` — so once
    she aged out of the active focus, TrackGraphProcess's traversal had no
    edge to walk to reach her from Eduardo (or vice versa) ever again, and
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
        kind = _fallback_relation_kind(vov.object_nature, other.object_nature)
        if (
            kind == "Link_Subject_Cluster"
            and vov.object_nature == "ScenarioData"
            and other.object_nature == "ScenarioData"
            and not _scenario_datas_share_member(db, vov, other_id)
        ):
            # MS §6.10/§14.19: chaining onto an existing backbone requires
            # the report to genuinely be the same matter continuing, not
            # merely a recent ScenarioData in focus. Confirmed for real: a
            # brand-new, unrelated matter's ScenarioData was linked to the
            # most recent one in focus (sharing only the universal reporter,
            # no actual concerned party) — refusing the edge here is what
            # keeps the two clusters from merging into one at the source,
            # rather than only cleaning up the cascade afterward.
            print(f"[notice] refused Link_Subject_Cluster between ScenarioData "
                  f"{vov.vov_id!r} and {other_id!r}: no shared concerned party "
                  f"between them — treating as different matters (MS §6.10/§14.19)")
            continue
        db.write_relation(from_vov_id=vov.vov_id, to_vov_id=other_id, kind=kind)
        connected.add(other_id)


def _link_scenario_data_siblings(db: Database, vov: VectorObjectValence) -> None:
    """MS §6.10: every `ScenarioData` Object of the same cluster should be
    directly reachable from any other via `Link_Subject_Cluster`, not left
    to rely on TraceDepth's BFS threading through their shared members
    alone. Confirmed for real: a chain of 5 `ScenarioData` siblings for the
    same matter — each one correctly sharing `Link_Subject_Cluster` edges
    to the exact same four Sentients — had only ONE direct backbone-to-
    backbone edge among the five, even though which cluster each belonged
    to was never actually in doubt anywhere in the data.

    This derives the missing edges mechanically from a fact the model
    ALREADY established — which non-`ScenarioData` Objects this row shares
    a `Link_Subject_Cluster` edge with — rather than guessing which
    cluster something belongs to: it only ever connects two `ScenarioData`
    rows that each, independently, already claim the same member. That
    keeps it on the safe side of this codebase's own standing line (MS
    §11.1: "the intelligence dwells in the queries") — nothing here
    decides membership, it only completes the structural consequence of a
    membership decision the model already made.

    The "member" set MUST exclude other `ScenarioData` rows, not just
    happen to usually be Sentients/Situations — confirmed for real, the
    first version of this function used every `Link_Subject_Cluster`
    neighbor undiscriminated, including sibling ScenarioData themselves.
    One model-authored mistake (an unrelated new matter's ScenarioData
    wrongly linked to an existing one, e.g. because both were recent) was
    enough to chain through THAT ScenarioData's own neighbors and merge
    two genuinely separate clusters into one sprawling mass in a single
    pass — this function amplifying a single bad edge into dozens, which
    is the opposite of what it exists for. Only a real concerned party
    (Sentient, Situation, ...) shared between two `ScenarioData` rows is
    evidence they belong together; another `ScenarioData` being reachable
    proves nothing on its own, since that reachability is exactly the fact
    in question."""
    if vov.object_nature != "ScenarioData":
        return
    neighbor_ids = {
        (row["to_vov_id"] if row["from_vov_id"] == vov.vov_id else row["from_vov_id"])
        for row in db.get_relations([vov.vov_id], ["Link_Subject_Cluster"])
    }
    if not neighbor_ids:
        return
    neighbors = db.get_objects(list(neighbor_ids))
    members = {
        nid for nid in neighbor_ids
        if neighbors.get(nid) and neighbors[nid].object_nature != "ScenarioData"
    }
    if not members:
        return
    sibling_ids: set = set()
    for member_id in members:
        for row in db.get_relations([member_id], ["Link_Subject_Cluster"]):
            other = row["to_vov_id"] if row["from_vov_id"] == member_id else row["from_vov_id"]
            if other != vov.vov_id:
                sibling_ids.add(other)
    if not sibling_ids:
        return
    already_connected = {
        (row["to_vov_id"] if row["from_vov_id"] == vov.vov_id else row["from_vov_id"])
        for row in db.get_relations([vov.vov_id])
    }
    related = db.get_objects(list(sibling_ids))
    for sid in sibling_ids:
        sibling = related.get(sid)
        if sibling is None or sibling.object_nature != "ScenarioData" or sid in already_connected:
            continue
        db.write_relation(from_vov_id=vov.vov_id, to_vov_id=sid, kind="Link_Subject_Cluster")
        print(f"[notice] auto-linked ScenarioData backbone siblings {vov.vov_id!r} <-> {sid!r} "
              f"(shared cluster member) — MS §6.10 backbone connectivity")
        already_connected.add(sid)


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
    its own `relevant_relations` that the actual graph can't walk to."""
    vov = _enforce_objective_cluster_only_relations(db, vov)
    db.upsert_object(mov_id, vov)
    _ensure_relation_edges(db, vov)
    _link_scenario_data_siblings(db, vov)


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


def _cycle_touched_ids(update_result: MovMainMemoryUpdateResult, id_remap: dict) -> set:
    """Every vov_id ProcessMotivation consciously decided something about
    THIS cycle: reviewed in the retrospective (whatever the action —
    KEEP_PENDING counts exactly as much as ARCHIVE, both are a real
    decision), or targeted/minted by one of this cycle's own prospective
    mov_ops. `_resolve_id` uses this as the only legitimate basis for
    Query 3 re-electing an id that already belongs to a standing
    Objective — an id existing at all is not enough on its own, or Query 3
    could silently re-use *any* old Objective's row for a brand-new,
    unrelated guess just because the model happened to invent the same
    number. Confirmed for real: an "Objective" about Michele sat at
    VOV_0005 for six straight cycles, correctly re-elected each time as
    the same continuing hunt — Query 2's own retrospective reviewed it
    every one of those cycles — and then, on a seventh cycle whose
    retrospective never mentioned VOV_0005 at all, Query 3 guessed that
    same id for an entirely different, unrelated Objective ("identify the
    specific daughter..."), and _resolve_id let it through because an
    Objective already sat there. The Michele objective wasn't archived,
    wasn't given a delta_report, wasn't reprioritized down — it was simply
    gone, overwritten, with no record of what became of it. An Objective
    can be *edited* — refined, re-elected, carried forward — but not
    silently erased by a completely different one that happens to share
    its number; MS §10.5's δ can only ever be reckoned for an Objective
    whose end was actually recorded through the retrospective, never one
    that just vanished under new content."""
    touched = {entry.vov_id for entry in update_result.retrospective}
    for op in update_result.prospective.mov_ops:
        if op.op == "UPSERT_VOV" and op.vov is not None:
            model_id = op.vov.get("vov_id")
            if model_id:
                touched.add(id_remap.get(model_id, model_id))
        elif op.vov_id:
            touched.add(op.vov_id)
    return touched


def _resolve_id(db: Database, proposed_id: str, cycle_touched_ids: set) -> str:
    """The model's best_prey_guess/accompanying_objectives usually re-elect
    an Objective Query 2 already created or reviewed in this same cycle
    (by echoing its id back from the MOV it was handed) — that's a
    legitimate reuse, so keep it, but only when BOTH: the row already
    there actually is an Objective (this id is always about to become
    one, run_motivation_cycle force-sets object_nature="Objective" right
    after this returns, so reusing an id that currently names something
    else is never re-election, it's a collision — confirmed for real
    against a Person, see _nature_conflicts), AND this exact cycle
    actually did something with that id (see _cycle_touched_ids) — an id
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
        and proposed_id in cycle_touched_ids
    ):
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
    """Core identity (VOV_0000 + her creator/developer, config.py's
    protected_vov_ids) is exempt from MS §7's focus/archive cycle — losing
    one of these from the MOV isn't forgetting a case detail, it's losing
    who Liriel is. Everything else archives and retrieves via the Graph of
    Traces as normal."""
    return bool(vov_id) and vov_id in settings.protected_vov_ids


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
                merged[key] = (
                    raw_entry if isinstance(raw_entry, entry_type)
                    else entry_type.model_validate(raw_entry)
                )
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


def _apply_mainmemory_commands(db: Database, commands: list) -> None:
    """MS §12.4. RETRIEVE/ARCHIVE/WRITE_RELATION/SOFTEN_CHARGE are executed
    here directly. SEARCH is also fully executed now (graph_service.
    search_memory/run_search_commands — MS §12.4's free-text lookup,
    finally a real implementation, not name-specific) — but *not* here:
    it returns data Query 3 needs to see, not just a side effect to apply,
    so run_motivation_cycle calls graph_service.run_search_commands
    directly right after this function, once, over the whole commands list,
    rather than per-command like the others below."""
    for cmd in commands:
        op = cmd.get("op")
        if op == "RETRIEVE":
            for vov_id in cmd.get("vov_ids", []):
                db.restore_object(vov_id)
        elif op == "ARCHIVE":
            for vov_id in cmd.get("vov_ids", []):
                if _is_protected(vov_id):
                    print(f"[notice] refused to archive protected vov_id={vov_id!r} (core identity)")
                    continue
                db.archive_object(vov_id)
        elif op == "WRITE_RELATION" and cmd.get("from") and cmd.get("to") and cmd.get("kind"):
            from_id, to_id = cmd["from"], cmd["to"]
            from_obj, to_obj = db.get_object(from_id), db.get_object(to_id)
            from_nature = from_obj.object_nature if from_obj else None
            to_nature = to_obj.object_nature if to_obj else None
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
                # the same cycle's mainmemory_commands, which landed
                # unchecked. Refusing it here closes the other half of the
                # same gap.
                print(f"[notice] refused WRITE_RELATION {from_id!r} ({from_nature}) -> "
                      f"{to_id!r} ({to_nature}): an Objective may only link to its "
                      f"ScenarioData origin (MS §6.10) — skipped")
                continue
            if (
                from_nature == "ScenarioData" and to_nature == "ScenarioData"
                and not _scenario_data_ids_share_member(db, from_id, to_id)
            ):
                # Same guard _ensure_relation_edges applies to a
                # relevant_relations-derived edge, for the other path a
                # model can use to write one directly (MS §6.10/§14.19).
                print(f"[notice] refused WRITE_RELATION {from_id!r} <-> {to_id!r}: "
                      f"no shared concerned party between these two ScenarioData — "
                      f"treating as different matters (MS §6.10/§14.19) — skipped")
                continue
            kind = cmd["kind"]
            if kind not in _RELATION_KINDS:
                print(f"[notice] WRITE_RELATION kind={kind!r} isn't one of the closed three "
                      f"(MS §8.3) — writing as Link_Valence_Load instead")
                kind = "Link_Valence_Load"
            db.write_relation(
                from_vov_id=from_id,
                to_vov_id=to_id,
                kind=kind,
                propositional=cmd.get("propositional"),
                affective=_affective_to_numeric(cmd.get("affective")),
                confidence=cmd.get("confidence"),
            )
        elif op == "SOFTEN_CHARGE":
            db.soften_charge(cmd.get("vov_ids", []))
        elif op == "SEARCH":
            pass  # handled by graph_service.run_search_commands, not here
        else:
            print(f"[notice] unrecognized MainMemory command: {cmd}")
