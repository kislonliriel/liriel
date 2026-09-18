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
across the focus and the archive.

Backend (config.py's LLM_BACKEND, default "llamacpp"): all four calls run
against a self-hosted llama-server (scripts/llamacpp/) — every inference
stays under the implementer's own control, no closed external API sees
Liriel's state (MS §17.6 "weight ownership"). Voice notes (Telegram STT/
TTS) are the one exception, still OpenAI via llm_client.py — that's I/O
plumbing, not cognition, and the cost there is negligible. Setting
LLM_BACKEND=anthropic switches all four calls to llm_client.py/litellm/
Claude instead, as a debugging escape hatch — not the default.

When LLM_BACKEND=anthropic, calls 1-3 (structured-JSON extraction, not
the voice the user actually hears) run on config.py's LLM_MODEL_STRUCTURED
(a cheaper model, e.g. a Claude Haiku, when set), while call 4 stays on
LLM_MODEL, since that's the one call where model quality is directly
audible/readable to the user. Both default to the same value, so this is
opt-in (.env), not a behavior change on its own. llamacpp_client.py has no
equivalent split — one local server serves all four.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import ValidationError

import graph_service
from config import settings
from database import Database

# config.LLM_BACKEND picks which of the two ends up bound to chat/chat_json
# here — both modules expose the same (messages, temperature, effort, model)
# signature, so nothing below this needs to know which one it's actually
# talking to. Default is "llamacpp" (self-hosted, scripts/llamacpp/):
# every inference stays under your own control, no closed external API —
# "anthropic" is the escape hatch back to llm_client.py/litellm/Claude.
if settings.llm_backend == "anthropic":
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
    ScenarioData,
    SchemaEntry,
    VectorObjectValence,
)
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

    # --- Graph generation: TrackGraphProcess (service, not an LLM call) ---
    graph_of_traces = graph_service.build_graph_of_traces(db, graph_request.requests)
    if settings.verbose and graph_of_traces:
        boosted = any(r.deep_recall_requested for r in graph_request.requests)
        print(
            f"[debug] GraphOfTraces: {len(graph_of_traces['nodes'])} node(s), "
            f"{len(graph_of_traces['edges'])} edge(s)"
            + (" [deep recall boosted]" if boosted else "")
        )

    # --- Query 2: MOV_MAINMEMORY_UPDATE (MS §12.3) ------------------------
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
        print(f"[debug] MOV_MAINMEMORY_UPDATE: {update_result.model_dump_json(indent=2)}")

    _apply_retrospective(db, update_result)
    _apply_mov_ops(db, mov.mov_id, update_result.prospective.mov_ops)
    _apply_mainmemory_commands(db, update_result.mainmemory_commands)
    _apply_nested_mov_ops(db, update_result.prospective.nested_mov_ops)

    mov = db.load_mov(mov.mov_id)  # reload: reflects everything Query 2 just did
    # nested_mov_ops may have just created/changed one — recompute rather
    # than reuse the pre-Query-2 list.
    nested_movs = _load_nested_movs(db, mov, settings.nested_mov_max_depth)

    # --- Query 3: BEST_PREY_GUESS (MS §12.5) ------------------------------
    # Same graph Query 2 saw: MS §8.2 builds it once per cycle, at
    # ProcessMotivation's request (Query 1) — it isn't re-surveyed per query.
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
    guess = decision_result.best_prey_guess.model_copy(
        update={
            "vov_id": _resolve_id(db, decision_result.best_prey_guess.vov_id),
            "priority": 1, "valence_regime": "Delta", "object_nature": "Objective",
        }
    )
    db.upsert_object(mov.mov_id, guess)
    mov.upsert(guess)
    for i, obj in enumerate(decision_result.accompanying_objectives):
        obj = obj.model_copy(
            update={
                "vov_id": _resolve_id(db, obj.vov_id),
                "priority": i + 2, "valence_regime": "Delta", "object_nature": "Objective",
            }
        )
        db.upsert_object(mov.mov_id, obj)
        mov.upsert(obj)
    _apply_mov_ops(db, mov.mov_id, decision_result.mov_ops)

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


def _apply_retrospective(db: Database, update_result: MovMainMemoryUpdateResult) -> None:
    mov_id = settings.default_mov_id  # Phase 1 only ever runs one MOV
    for entry in update_result.retrospective:
        existing = db.get_object(entry.vov_id)
        if existing is None:
            print(f"[warning] retrospective action on unknown vov_id={entry.vov_id!r}, skipped")
            continue

        if entry.action == "SET_DELTA_REPORT":
            db.upsert_object(mov_id, existing.model_copy(update={"delta_report": entry.delta_report}))
        elif entry.action in ("KEEP_PENDING", "KEEP_PENDING_URGENT"):
            if entry.new_priority is not None:
                db.upsert_object(mov_id, existing.model_copy(update={"priority": entry.new_priority}))
        elif entry.action == "REPRIORITIZE":
            db.upsert_object(mov_id, existing.model_copy(update={"priority": entry.new_priority}))
        elif entry.action in ("ARCHIVE", "ABANDON"):
            if _is_protected(entry.vov_id):
                print(f"[notice] refused to archive protected vov_id={entry.vov_id!r} (core identity)")
                continue
            if entry.action == "ABANDON" and existing.objective is not None:
                obj = existing.objective.model_copy(update={"status": "abandoned"})
                db.upsert_object(mov_id, existing.model_copy(update={"objective": obj}))
            db.archive_object(entry.vov_id)


def _apply_mov_ops(db: Database, mov_id: str, ops: list) -> None:
    for op in ops:
        op: MovOp
        if op.op == "UPSERT_VOV" and op.vov is not None:
            existing = db.get_object(op.vov.get("vov_id")) if op.vov.get("vov_id") else None
            vov = _coerce_vov(existing, op.vov)
            if vov is None:
                print(f"[warning] UPSERT_VOV missing required fields and no existing "
                      f"object to patch onto, skipped: {op.vov}")
                continue
            if existing is None:  # genuinely new — don't trust the model's own id
                vov = vov.model_copy(update={"vov_id": db.next_vov_id()})
            db.upsert_object(mov_id, vov)
        elif op.op == "PATCH_VOV" and op.vov_id and op.patch:
            existing = db.get_object(op.vov_id)
            if existing is None:
                print(f"[warning] PATCH_VOV on unknown vov_id={op.vov_id!r}, skipped")
                continue
            merged = existing.model_copy(update=_coerce_patch(existing, op.patch))
            db.upsert_object(mov_id, merged)
        elif op.op == "SET_PRIORITY" and op.vov_id:
            existing = db.get_object(op.vov_id)
            if existing is None:
                continue
            db.upsert_object(mov_id, existing.model_copy(update={"priority": op.priority}))
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
                piece = piece.model_copy(update={"vov_id": db.next_vov_id()})
                db.upsert_object(mov_id, piece)
        else:
            print(f"[warning] unrecognized or incomplete mov_op: {op.model_dump()}")


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
                    db.upsert_object(owner_mov_id, owner.model_copy(update={"nested_mov": op.mov_id}))
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
                new_id = db.next_vov_id()
                print(f"[warning] PATCH_NESTED_VOV vov_id={op.vov_id!r} carries agent-only "
                      f"fields but the existing row is object_nature={existing.object_nature!r} "
                      f"— treating as a different entity, creating {new_id!r} instead: {op.patch}")
                seed = {**op.patch, "vov_id": new_id, "object_nature": op.patch.get("object_nature", "Person")}
                _upsert_nested_row(db, op.mov_id, seed)
                continue
            merged = existing.model_copy(update=_coerce_patch(existing, op.patch))
            db.upsert_object(op.mov_id, merged)
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
        new_id = db.next_vov_id()
        print(f"[warning] nested row id {vov_id!r} collides with an unrelated existing "
              f"object, reassigning id -> {new_id!r}: {raw_row}")
        existing = None
        raw_row = {**raw_row, "vov_id": new_id}
    vov = _coerce_vov(existing, raw_row)
    if vov is None:
        print(f"[warning] nested row missing required fields and no existing "
              f"object to patch onto, skipped: {raw_row}")
        return
    db.upsert_object(mov_id, vov)


def _resolve_id(db: Database, proposed_id: str) -> str:
    """The model's best_prey_guess/accompanying_objectives usually re-elect
    an Objective Query 2 already created in this same cycle (by echoing its
    id back from the MOV it was handed) — that's a legitimate reuse, so
    keep it. If the id doesn't match anything in the database, though,
    trusting it verbatim risks a collision (or a silent typo the model
    made copying an id) exactly like the ones _apply_mov_ops guards
    against; assign a fresh one instead."""
    if db.get_object(proposed_id) is not None:
        return proposed_id
    return db.next_vov_id()


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
_AGENT_OBJECT_NATURES = {"PCI", "Person", "Animal", "Group", "Entity"}


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


def _apply_mainmemory_commands(db: Database, commands: list) -> None:
    """MS §12.4. RETRIEVE/ARCHIVE/WRITE_RELATION/SOFTEN_CHARGE are executed;
    SEARCH still needs a real semantic/full-text index over the archive —
    Phase 2 — and is only logged here (the model isn't left to depend on it:
    GRAPH_REQUEST + TrackGraphProcess is the working path back into
    MainMemory as of this phase)."""
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
            db.write_relation(
                from_vov_id=cmd["from"],
                to_vov_id=cmd["to"],
                kind=cmd["kind"],
                propositional=cmd.get("propositional"),
                affective=cmd.get("affective"),
                confidence=cmd.get("confidence"),
            )
        elif op == "SOFTEN_CHARGE":
            db.soften_charge(cmd.get("vov_ids", []))
        else:
            print(f"[notice] MainMemory command not executed (Phase 2): {cmd}")
