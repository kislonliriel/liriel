"""
TrackGraphProcess (docs/MetaScheme_Liriel_Rev0000.md §8.2) — the service
that builds the Graph of Traces (§8) between Query 1 (GRAPH_REQUEST, §12.1)
and Query 2, and again between Query 2 and Query 3 when Query 2 issues its
own SEARCH. Not an LLM call: the graph itself is one thing — Objects and
the relations between them, pulled out of MainMemory around whichever
anchors are relevant this cycle — however those anchors were found.

Two ways an Object becomes an anchor:
  1. The model already has its id (an active Object, or one named in a
     previous cycle) and asks GRAPH_REQUEST to survey its relations.
  2. Nothing currently in focus points to it, and no id is known yet — the
     only way in is by what it's ABOUT. search_memory (MS §12.4 SEARCH,
     finally implemented for real: a keyword/fuzzy match over every
     Object's own text, any nature, not a name-matching special case) finds
     candidate ids by content; those ids then seed the exact same
     traversal as (1). A found Object without its relations is just a
     name — the whole point of a graph, as opposed to a lookup table, is
     that finding Michele should also bring back who Michele is connected
     to, the same way finding her by an already-known id would.

MS §7.1 requires the archive to be "faithful and retrievable" but leaves
its retrieval mechanics ("the form is negotiable, the requirement is not")
to the implementation. Two things follow from that this module owns:

  1. Ranking, in this exact priority order (see _rank_key below):
       1st — relevance to the current context (does this Object's own
             text overlap with what this cycle's message is actually
             about — the same scoring search_memory uses to find an
             anchor in the first place, now applied to its neighbors too);
       2nd — emotional charge (MS §7: MainMemory is "organized by
             affective weight, not by recency");
       3rd — recency (breaks ties charge alone can't).
     Each tier only matters when every tier above it is equal — a highly
     relevant but old, mild memory still outranks an irrelevant but recent,
     highly-charged one.
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

None of this replaces the user themselves as a source of disambiguation:
when even this can't resolve who or what a message refers to, that's
surfaced to the model as legitimate uncertainty (prompts.py's Query 3/4
instructions) rather than papered over — Liriel can ask, the way a person
would, and the answer becomes next cycle's own ScenarioData, which runs
through this exact same process again. The search doesn't have to finish
in one cycle; it can keep going across turns, same as it would for someone
trying to place a name across a conversation.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import settings
from database import Database
from models import GraphRequestItem, MatrixObjectsValence, VectorObjectValence

# ---------------------------------------------------------------------------
# Content matching — shared by relevance ranking (build_graph_of_traces) and
# MainMemorySearchProcess (search_memory) below, since both are really the
# same question asked at different points: does this Object's own text
# overlap with what this cycle's message is about?
# ---------------------------------------------------------------------------

# A fuzzy match this loose isn't safe on an arbitrary short word (too many
# unrelated words sit 0.8+ similar by pure edit distance) — but it is safe,
# and needed, for the actual case this exists for: the same word (usually a
# name) spelled slightly differently. Measured against this project's own
# MainMemory: "michele"/"michelle" = 0.933, "sebastiana"/"sebastiane" = 0.90
# — same person, a spelling slip. "eduardo"/"eduarda" = 0.857 — a
# *different* person (siblings) — sits just below this line on purpose.
_FUZZY_MIN_RATIO = 0.87
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_MIN_WORD_LEN = 4

# Common function words in both languages this project runs in (PT/EN) —
# without this, "que"/"para"/"that"/"with" would spuriously score against
# nearly every row in MainMemory once matching stopped being name-only.
# Not exhaustive; a missed stopword only adds a little noise; it can never
# hide a real match, since real matches ride on the *content* words around it.
_STOPWORDS = {
    "que", "para", "com", "uma", "um", "isso", "essa", "esse", "essas", "esses",
    "muito", "sobre", "ela", "ele", "eles", "elas", "voce", "vc", "eu", "sou",
    "esta", "estou", "foi", "ser", "tem", "tinha", "mas", "por", "nao", "sim",
    "aqui", "ali", "quando", "onde", "porque", "pra", "pro", "dos", "das",
    "seu", "sua", "seus", "suas", "meu", "minha", "meus", "minhas", "nos",
    "the", "and", "that", "with", "have", "this", "was", "for", "you", "your",
    "she", "her", "him", "his", "they", "them", "what", "when", "where",
    "liriel",  # confirmed for real: every message addresses her by name
    # ("Oi Liriel, ...") — treating it as content would score her own
    # identity row as "relevant" on literally every single cycle, which is
    # noise: Liriel is never the new person/topic a message introduces.
}


def _normalize_word(text: str) -> str:
    """Accent- and case-insensitive comparison key ("Fábio" == "Fabio")."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def _content_words(text: Optional[str]) -> set:
    if not text:
        return set()
    return {
        _normalize_word(w) for w in _WORD_RE.findall(text)
        if len(w) >= _MIN_WORD_LEN and _normalize_word(w) not in _STOPWORDS
    }


def _word_overlap_score(query_words: set, object_words: set) -> float:
    """Sum, over every query word, of its best match against the object's
    own words — 1.0 for an exact hit, `SequenceMatcher.ratio()` when that's
    at least `_FUZZY_MIN_RATIO` (catches a spelling slip on any content
    word, not just a name), 0 otherwise. An object sharing several distinct
    query words outscores one sharing just one, however exact that one hit."""
    if not query_words or not object_words:
        return 0.0
    total = 0.0
    for qw in query_words:
        if qw in object_words:
            total += 1.0
            continue
        best = max(
            (difflib.SequenceMatcher(None, qw, ow).ratio() for ow in object_words),
            default=0.0,
        )
        if best >= _FUZZY_MIN_RATIO:
            total += best
    return total


def _identity_words(db: Database) -> frozenset:
    """The MOV's own core-identity Objects (settings.protected_vov_ids —
    Liriel, her creator, and the one user this MOV belongs to) are named in
    close to every other Object's own text, for the exact same reason
    "liriel" already sits in _STOPWORDS: a message mentioning them narrows
    nothing down, since virtually everything in MainMemory already does.
    Confirmed for real: "fabio" alone was enough to push a dozen unrelated
    Objects to the same top relevance tier as Eduardo, burying him under
    build_graph_of_traces' node cap by emotional-charge tiebreak alone.
    Derived from the live Objects (not hardcoded) so a differently-named
    deployment needs no code change — only _STOPWORDS' PT/EN function
    words are fixed vocabulary; identity is data."""
    words: set = set()
    for vov_id in settings.protected_vov_ids:
        vov = db.get_object(vov_id)
        if vov is not None:
            words |= _content_words(vov.brief_description)
    return frozenset(words)


def _relevance(vov: VectorObjectValence, query_words: set) -> float:
    if not query_words:
        return 0.0
    object_words = _content_words(vov.brief_description) | _content_words(vov.relevant_remarks)
    return _word_overlap_score(query_words, object_words)


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
    recent-but-flat one."""
    if vov.updated_at is None:
        return 0.0
    updated_at = vov.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - updated_at).total_seconds() / 86400)
    return 1.0 / (1.0 + age_days)


# Being directly connected to a highly relevant Object is itself relevant
# — the whole point of a graph over a flat lookup (module docstring, point
# 2, and the user's own definition: an object AND its relevant relations
# to other objects, not just an isolated found object). A neighbor only
# inherits a fraction of what it borrows, so a direct content match still
# always outranks one that's only relevant by association. Confirmed for
# real: Eduardo's own bio never repeats "Sebastiana" or "marido" back — he
# is only ever named FROM her side of the relation — so pure content
# relevance left him tied with random noise at 0.0 and he lost the cutoff
# to unrelated but emotionally loud Objects.
_NEIGHBOR_RELEVANCE_DECAY = 0.5


def _propagate_relevance(base_relevance: Dict[str, float], collected_rows: List[dict]) -> Dict[str, float]:
    """One hop only, by design: MS §8's own graph is a small, immediate
    neighborhood ("the object and its relevant relations"), not a
    transitive web — an Object two relations away from a match earns its
    place on its own relevance, charge or recency, not by riding a long
    chain of borrowed scores."""
    adjacency: Dict[str, set] = {}
    for row in collected_rows:
        a, b = row["from_vov_id"], row["to_vov_id"]
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    graph_relevance = dict(base_relevance)
    for vov_id, neighbors in adjacency.items():
        best_neighbor = max((base_relevance.get(n, 0.0) for n in neighbors), default=0.0)
        graph_relevance[vov_id] = max(
            graph_relevance.get(vov_id, 0.0), best_neighbor * _NEIGHBOR_RELEVANCE_DECAY
        )
    return graph_relevance


def _rank_key(vov: VectorObjectValence, relevance: float) -> tuple:
    """Strict lexicographic priority — relevance, THEN charge, THEN
    recency — rather than a blended weighted sum: a sum could let a huge
    charge outrank higher relevance, which is exactly backwards from what
    was asked for ("1º relevância... 2º carga emocional... 3º recência").
    Each tier is a tiebreaker for the one before it, nothing more."""
    return (round(relevance, 2), round(_emotional_charge(vov), 2), round(_recency_factor(vov), 4))


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


def build_graph_of_traces(
    db: Database, requests: List[GraphRequestItem], scenario_text: str = ""
) -> Optional[dict]:
    """MS §8.3. Runs one BFS-style traversal per request (each may name a
    different set of focus_objects, relation kinds and depth), then merges
    everything into one Graph of Traces. `scenario_text` (this cycle's raw
    message) drives the relevance tier of the ranking below — pass it every
    time, whether `requests` came from the model's own GRAPH_REQUEST or
    were synthesized from search_memory hits (see requests_from_ids).
    Returns None if there's nothing to survey (empty `requests`) —
    ProcessMotivation simply proceeds without a graph that cycle."""
    if not requests:
        return None

    query_words = _content_words(scenario_text) - _identity_words(db)
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
        base_relevance = {vid: _relevance(vov, query_words) for vid, vov in touched_objects.items()}
        graph_relevance = _propagate_relevance(base_relevance, collected_rows)

        # Rank archived candidates by (relevance, charge, recency) — see
        # _rank_key; keep the focus_objects themselves unconditionally
        # (they're the request's own anchor, already inside MS §7.1's
        # "small and countable" focus) and truncate only the archived
        # overflow.
        anchors = set(req.focus_objects)
        archived_candidates = [
            (vov_id, vov) for vov_id, vov in touched_objects.items()
            if vov.archived and vov_id not in anchors
        ]
        archived_candidates.sort(key=lambda pair: _rank_key(pair[1], graph_relevance[pair[0]]), reverse=True)
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
            node = {
                "vov_id": vov_id,
                "label": (vov.brief_description[:60] if vov else None),
                "source": source_of.get(vov_id, "focus"),
            }
            if vov is not None:
                relevance = graph_relevance.get(vov_id, 0.0)
                if relevance > 0:
                    node["relevance"] = round(relevance, 2)
            all_nodes[vov_id] = node

    if not all_edges and not all_nodes:
        return None

    return {
        "artifact": "GraphOfTraces",
        "requested_for": list(dict.fromkeys(requested_for)),
        "nodes": list(all_nodes.values()),
        "edges": list(all_edges.values()),
    }


def requests_from_ids(vov_ids: List[str], reason: str, depth: int = 3) -> List[GraphRequestItem]:
    """Wraps a bare list of ids (from search_memory/run_search_commands)
    into GraphRequestItem(s), so an anchor FOUND by content match feeds the
    exact same traversal as one the model already knew the id for —
    finding an Object should also bring back who/what it's connected to,
    not just its own row in isolation. `include_archive=True` and `depth`
    3 by default (one more than prompts.py's own "2 or more" guidance to
    the model): an anchor surfaced this way was, by definition, found
    through an indirect route (search, not a direct id) — confirmed for
    real, a depth of 2 reached "Sebastiana" from a search hit two hops
    away but fell one hop short of also reaching "Eduardo," her own
    husband, named in the very next edge. The automatic path compensates
    for that extra indirection with one more hop of reach."""
    if not vov_ids:
        return []
    return [GraphRequestItem(
        focus_objects=list(dict.fromkeys(vov_ids)),
        relation_kinds=[],
        include_archive=True,
        depth=depth,
        reason=reason,
    )]


# ---------------------------------------------------------------------------
# MainMemorySearchProcess — MS §12.4 SEARCH, actually implemented (this
# codebase only ever logged it before: "still needs a real index — Phase
# 2"). Finds candidate anchor ids by keyword/fuzzy content match, for
# build_graph_of_traces to then pull relations around (see
# requests_from_ids above) — this function only ever answers "which ids",
# never "and what are they connected to": that's the graph's job, not this
# one's, so the two compose instead of duplicating each other.
#
# search_memory itself is two stages, not one flat scan — MainMemory search
# is nearly always about something already connected to the current focus
# (an active Object, or one recently touched), not a cold lookup over the
# entire archive:
#   Stage 1, "contextual": score only what's already active in the MOV,
#     whatever is directly linked to an active Object (one hop, whether
#     that neighbor is archived or not), and a small net of whatever was
#     most recently touched in MainMemory regardless of a link. This is
#     cheap (a handful of Objects, not the whole archive) and matches how
#     a person actually searches their own memory: starting from what's
#     already on their mind.
#   Stage 2, "blind": the full archive scan, run ONLY when Stage 1 turns up
#     nothing — genuinely unrelated to anything currently active or recent,
#     the case a truly new topic (or someone Liriel hasn't heard from in a
#     long time) actually is.
#
# Two ways this gets invoked:
#   1. Automatically, once per cycle, against the raw message — called
#      from motivation.py before Query 1, seeding that cycle's graph.
#   2. On demand, when Query 2 (MOV_MAINMEMORY_UPDATE) itself doesn't
#      recognize something and emits a SEARCH mainmemory_command with its
#      own query terms (MS §12.4) — run_search_commands runs that search
#      and motivation.py rebuilds the graph from its hits before Query 3
#      (BEST_PREY_GUESS) runs, in the same cycle. This is Liriel's own
#      "let me think about that some more": she doesn't have to resolve
#      identity in one shot, and if even that doesn't turn anything up,
#      the uncertainty itself becomes something she can ask the user about
#      — whose answer becomes next cycle's own message, run through this
#      same process again.
# ---------------------------------------------------------------------------

# Size of the "recently touched" net in Stage 1 — independent of any
# explicit link to something active, just whatever MainMemory last wrote
# to. Small on purpose: this is a cheap first pass, not a replacement for
# Stage 2's full scan when it comes up empty.
_CONTEXTUAL_RECENT_LIMIT = 15


def _contextual_pool(db: Database, mov: MatrixObjectsValence) -> List[VectorObjectValence]:
    """Stage 1's search space: every Object already active in `mov`, every
    Object directly linked to one of them (one hop via mov_relations,
    archived or not — MS §8's own edges), and a small recency net on top.
    Deliberately not a full graph traversal (that's build_graph_of_traces'
    job, run afterward on whatever this finds) — just enough to answer
    "is this already connected to what's on Liriel's mind" cheaply."""
    active = mov.active()
    pool: Dict[str, VectorObjectValence] = {o.vov_id: o for o in active}
    active_ids = list(pool.keys())
    if active_ids:
        rows = db.get_relations(active_ids)
        neighbor_ids = {
            vid for row in rows for vid in (row["from_vov_id"], row["to_vov_id"])
        } - set(active_ids)
        if neighbor_ids:
            pool.update(db.get_objects(list(neighbor_ids)))

    all_objects = db.get_all_objects(mov.mov_id)
    recent = sorted(
        (o for o in all_objects if o.vov_id not in pool),
        key=lambda o: o.updated_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:_CONTEXTUAL_RECENT_LIMIT]
    for o in recent:
        pool[o.vov_id] = o
    return list(pool.values())


def _score_objects(objects: List[VectorObjectValence], query_words: set) -> List[tuple]:
    scored = []
    for vov in objects:
        if vov.vov_id in settings.protected_vov_ids:
            continue  # Liriel and her creator/developer are never "found" this way
        score = _relevance(vov, query_words)
        if score > 0:
            scored.append((score, vov))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def search_memory(
    db: Database,
    mov: MatrixObjectsValence,
    query_text: str,
    max_candidates: int = 6,
    force_blind: bool = False,
) -> List[dict]:
    """Scores Objects by how much their own text (brief_description +
    relevant_remarks) overlaps with `query_text`'s content words — exact or
    a close spelling variant. Not a semantic index (it can't tell that
    "chefe" and "superior hierárquico" mean the same thing) — it's a plain
    keyword/fuzzy match, which is exactly what MS §12.4 itself asks for
    ("free-text/filtered lookup").

    Tries the contextual pool first (see _contextual_pool) — what's already
    active, its immediate neighbors, and a recency net — and only falls
    back to scanning the *entire* archive (any nature, active or archived)
    if that comes up empty: a "blind search" is the exception, for
    something genuinely outside the current context, not the default path
    for every message.

    `force_blind=True` skips straight to the full-archive scan regardless
    of what the contextual pool finds — for the two cases where "the
    context already found *something*" isn't good enough on its own:
    (1) the user explicitly asked Liriel to make a real effort to remember
    (MS §12.1's `deep_recall_requested` — run_motivation_cycle re-runs this
    search with force_blind=True when that flag is set, since a weak
    contextual hit shouldn't cut a deliberate, insisted-on search short),
    and (2) run_search_commands below, where Query 2 itself is explicitly
    saying the passive path already failed to resolve something — running
    only the cheap contextual pass again would just repeat that failure.

    Returns up to `max_candidates` matches, highest score first, each as
    {"vov_id", "brief_description", "object_nature", "archived", "score"}
    — feed the vov_ids into requests_from_ids to pull their graph in too.
    """
    query_words = _content_words(query_text) - _identity_words(db)
    if not query_words:
        return []

    scored = [] if force_blind else _score_objects(_contextual_pool(db, mov), query_words)
    if not scored:
        scored = _score_objects(db.get_all_objects(mov.mov_id), query_words)

    return [
        {
            "vov_id": vov.vov_id,
            "brief_description": vov.brief_description,
            "object_nature": vov.object_nature,
            "archived": vov.archived,
            "score": round(score, 2),
        }
        for score, vov in scored[:max_candidates]
    ]


def run_search_commands(db: Database, mov: MatrixObjectsValence, mainmemory_commands: list) -> List[dict]:
    """Executes every `{"op": "SEARCH", "query": "..."}` command Query 2
    (MOV_MAINMEMORY_UPDATE) emitted this cycle, with the model's own search
    terms rather than only the raw message. Always `force_blind=True`: by
    the time the model deliberately reaches for SEARCH, GraphOfTraces and
    the automatic contextual pass have already had their chance — running
    the same cheap pool again would just repeat whatever already failed to
    resolve it. Merges results across multiple SEARCH commands in one
    cycle, keeping each vov_id's best score."""
    best: Dict[str, dict] = {}
    for cmd in mainmemory_commands:
        if cmd.get("op") != "SEARCH" or not cmd.get("query"):
            continue
        for result in search_memory(db, mov, cmd["query"], force_blind=True):
            existing = best.get(result["vov_id"])
            if existing is None or result["score"] > existing["score"]:
                best[result["vov_id"]] = result
    return sorted(best.values(), key=lambda r: r["score"], reverse=True)
