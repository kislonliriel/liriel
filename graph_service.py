"""
TrackGraphProcess (docs/MetaScheme_Liriel_Rev0000.md §8.2) — the service
that builds the Graph of Traces (§8) between Query 1 (GRAPH_REQUEST, §12.1)
and Query 2. Not an LLM call: ProcessMotivation's Query 1 decides *which*
Objects need their relations surveyed; this module does the actual
traversal over the focus and the archive and hands back nodes+edges in the
§8.3 shape.

MS §7.1 requires the archive to be "faithful and retrievable" but leaves
its retrieval mechanics ("the form is negotiable, the requirement is not")
to the implementation. Two things follow from that this module owns:

  1. Ranking. MS §7 itself says MainMemory is "organized by affective
     weight, not by recency" — so when a request's relations pull in more
     archived Objects than the graph can afford to carry, the ones kept
     are chosen by (1) emotional charge, (2) recency, in that order of
     weight (see _score below).
  2. A size threshold. Nothing in MS §8 caps a graph's size — "do not
     request the whole archive: request the Objects" trusts the *model*
     to scope `focus_objects` sensibly (MS §12.1). This threshold is a
     Phase 1 safety net on top of that trust, not a replacement for it:
     without one, a broad, multi-hop, include_archive=true request could
     pull enough archived Objects into every one of the cycle's remaining
     prompts to meaningfully inflate their size. GRAPH_MAX_NODES (.env)
     sets it; GraphRequestItem.deep_recall_requested (set by the model
     when the user explicitly insists Liriel try hard to remember
     something) raises it to GRAPH_MAX_NODES_BOOSTED for that request only
     — there is no persistent "boosted mode" to leave: the next cycle's
     request simply doesn't set the flag, and the normal threshold applies
     with nothing to revert.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import settings
from database import Database
from models import GraphRequestItem, VectorObjectValence


def _emotional_charge(vov: VectorObjectValence) -> float:
    """MS §7's own vocabulary for MainMemory's organizing principle
    ("affective weight"), operationalized the same way the MetaScheme's
    own SEARCH filter does (`min_abs_valence`, MS §12.4): the sum of the
    absolute value of every Feeling axis the Object carries. A VOV with
    several strongly-charged axes outranks one with a single mild one."""
    return sum(abs(av.v) for av in vov.feelings.values())


def _recency_factor(vov: VectorObjectValence) -> float:
    """1.0 for "just updated", decaying toward 0 as an Object ages — never
    reaching it, so an old-but-highly-charged memory can still outrank a
    recent-but-flat one (matching MS §7: charge first, recency second)."""
    if vov.updated_at is None:
        return 0.0
    updated_at = vov.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - updated_at).total_seconds() / 86400)
    return 1.0 / (1.0 + age_days)


def _score(vov: VectorObjectValence) -> float:
    return _emotional_charge(vov) + settings.graph_recency_weight * _recency_factor(vov)


def _edge_dict(row: dict, source_of: Dict[str, str]) -> dict:
    edge = {
        "from": row["from_vov_id"],
        "to": row["to_vov_id"],
        "kind": row["kind"],
        "directed": row["directed"],
        "propositional": row["propositional"],
        "affective": row["affective"] or [],
        "confidence": row["confidence"],
        "source": "MainMemory" if (
            source_of.get(row["from_vov_id"]) == "MainMemory"
            or source_of.get(row["to_vov_id"]) == "MainMemory"
        ) else "focus",
    }
    if row.get("since_text"):
        edge["since"] = row["since_text"]
    return edge


def build_graph_of_traces(db: Database, requests: List[GraphRequestItem]) -> Optional[dict]:
    """MS §8.3. Runs one BFS-style traversal per request (each may name a
    different set of focus_objects, relation kinds and depth), then merges
    everything into one Graph of Traces. Returns None if there's nothing
    to survey (empty `requests`) — ProcessMotivation simply proceeds
    without a graph that cycle, same as Phase 1 always did before this."""
    if not requests:
        return None

    all_nodes: Dict[str, dict] = {}
    all_edges: Dict[tuple, dict] = {}
    requested_for: List[str] = []

    for req in requests:
        if not req.focus_objects:
            continue
        requested_for.extend(req.focus_objects)
        max_nodes = settings.graph_max_nodes_boosted if req.deep_recall_requested else settings.graph_max_nodes

        frontier = list(dict.fromkeys(req.focus_objects))  # de-dup, keep order
        visited = set(frontier)
        collected_rows: List[dict] = []

        for _ in range(max(1, req.depth)):
            if not frontier:
                break
            rows = db.get_relations(frontier, req.relation_kinds or None)
            collected_rows.extend(rows)
            next_frontier = []
            for row in rows:
                for endpoint in (row["from_vov_id"], row["to_vov_id"]):
                    if endpoint not in visited:
                        visited.add(endpoint)
                        next_frontier.append(endpoint)
            frontier = next_frontier

        if not collected_rows:
            continue

        touched_objects = db.get_objects(sorted(visited))

        # MS §7: rank archived candidates by (emotional charge, recency);
        # keep the focus_objects themselves unconditionally (they're the
        # request's own anchor, already inside MS §7.1's "small and
        # countable" focus) and truncate only the archived overflow.
        anchors = set(req.focus_objects)
        archived_candidates = [
            (vov_id, vov) for vov_id, vov in touched_objects.items()
            if vov.archived and vov_id not in anchors
        ]
        archived_candidates.sort(key=lambda pair: _score(pair[1]), reverse=True)
        kept_archived = {vov_id for vov_id, _ in archived_candidates[:max_nodes]}
        dropped = {vov_id for vov_id, _ in archived_candidates[max_nodes:]}

        kept_ids = set(req.focus_objects) | {
            vov_id for vov_id, vov in touched_objects.items() if not vov.archived
        } | kept_archived

        source_of = {
            vov_id: ("MainMemory" if vov.archived else "focus")
            for vov_id, vov in touched_objects.items()
        }
        for vov_id in req.focus_objects:
            source_of.setdefault(vov_id, "focus")

        for row in collected_rows:
            if row["from_vov_id"] in dropped or row["to_vov_id"] in dropped:
                continue
            if row["from_vov_id"] not in kept_ids or row["to_vov_id"] not in kept_ids:
                continue
            key = (row["from_vov_id"], row["to_vov_id"], row["kind"])
            all_edges[key] = _edge_dict(row, source_of)

        for vov_id in kept_ids:
            if vov_id in all_nodes:
                continue
            vov = touched_objects.get(vov_id)
            all_nodes[vov_id] = {
                "vov_id": vov_id,
                "label": (vov.brief_description[:60] if vov else None),
                "source": source_of.get(vov_id, "focus"),
            }

    if not all_edges and not all_nodes:
        return None

    return {
        "artifact": "GraphOfTraces",
        "requested_for": list(dict.fromkeys(requested_for)),
        "nodes": list(all_nodes.values()),
        "edges": list(all_edges.values()),
    }
