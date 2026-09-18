"""
LirielMindReader — a standalone, read-only visualization of Liriel's
current MOV (MS §6.2): every active VOV as a node, every recorded
relation (mov_relations, MS §8) as an edge, rendered as an interactive
Obsidian-style force-directed graph in the browser.

Deliberately independent of the Liriel app itself (motivation.py,
telegram_bot.py, main.py) — it only ever reads the same database, never
writes to it, and can run at the same time as a live chat session to
watch the MOV change between messages. Uses the exact same Database
abstraction (database.py) Liriel's own code does, so it points at
whatever .env already configures — no separate connection setup needed.

Run:
    python mindreader/app.py
Then open http://localhost:5050 in a browser. Click "Atualizar" (or
reload the page) any time to see the MOV as it stands right now.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Windows console codepage fix, same as main.py/telegram_bot.py.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

from config import settings  # noqa: E402
from database import get_database  # noqa: E402

app = Flask(__name__, static_folder="static", static_url_path="")

# MS §16 object_nature enum, each given its own color so the same node
# always reads the same way at a glance — "Objective" is handled
# separately (bigger, gold, its own shape) since the user specifically
# asked for those to stand out: MS §10.1 calls the Objective "the most
# important [Object] of all... what the other Objects organize themselves
# around", so visually it should look that way too.
_NATURE_COLORS = {
    "PCI": "#b388ff",
    "Person": "#5aa9e6",
    "Situation": "#ff8c42",
    "Thing": "#8bc34a",
    "Idea": "#4dd0e1",
    "Event": "#ef5350",
    "Memory": "#a1887f",
    "Group": "#26a69a",
    "Animal": "#cddc39",
    "Entity": "#7986cb",
    "Attribute": "#b0bec5",
    "Self-Process": "#ab47bc",
}
_OBJECTIVE_COLOR = "#f5c518"
_DEFAULT_COLOR = "#90a4ae"

# Non-Objective node sizing: proportional to the single most intense
# Feeling axis (MS §3 -- "valência", the signed -5..+5 point on an axis;
# Ordinances use a separate "demand intensity" scale, MS §4.1, not this
# one, so they're excluded). A node with no charged Feelings at all sizes
# at the floor; one sitting at |v|=5 on some axis sizes at the ceiling.
_MIN_NODE_SIZE = 12.0
_MAX_NODE_SIZE = 34.0
# |v| >= this counts as "significativa" for the red/green ring -- MS's
# scale tops out at 5, so 3 is "more than halfway to the most intense a
# Feeling can be recorded", a reasonable line between "some charge" and
# "charge worth flagging visually" without a canon threshold to match.
_SIGNIFICANT_VALENCE = 3.0
_RING_NEGATIVE = "#e05252"
_RING_POSITIVE = "#4caf7d"


def _truncate(text: str | None, n: int = 60) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _dominant_valence(feelings: dict) -> Optional[tuple]:
    """(axis, v) of whichever Feeling on this VOV has the largest |v| --
    None if it has no charged Feelings at all (MS §3.2: most axes are left
    blank, meaning neutral, not zero)."""
    if not feelings:
        return None
    axis, av = max(feelings.items(), key=lambda kv: abs(kv[1].v))
    return axis, av.v


def _node_from_vov(vov) -> dict:
    is_objective = vov.object_nature == "Objective"
    color = _OBJECTIVE_COLOR if is_objective else _NATURE_COLORS.get(vov.object_nature, _DEFAULT_COLOR)

    # "breve explicação textual" visible on the graph itself, not just on
    # hover — a short label under/beside the node — plus the full text
    # and every other field in the tooltip and the click-through detail
    # panel (built client-side from the same payload).
    #
    # vis-network splits shapes into two families: "dot"/"star"/"diamond"/...
    # always draw their label BELOW the shape (on the page background, not
    # on the node's own fill), while "circle"/"box"/"ellipse" draw the
    # whole label INSIDE it, auto-sizing to fit. That's exactly why the
    # dark gold-contrast label color looked broken -- it was never sitting
    # on the gold fill it was tuned for, it was sitting on the dark page
    # background below the star. Rather than fight that with a custom
    # canvas renderer, Objective nodes switch to "circle" below so the
    # priority number renders where it's asked for (centered, on the
    # node's own fill, where dark-on-gold genuinely is the right choice);
    # everyone else keeps the description as a below-node label, now
    # small and always light so it reads on the dark background regardless
    # of which node it belongs to.
    label_desc = _truncate(vov.brief_description, 42)
    if is_objective:
        label = str(vov.priority) if vov.priority is not None else "•"
    else:
        # 🪞 flags a materialized nested MOV (MS §6.8 calls this
        # "specular recursion" -- the mirror is the exact right symbol,
        # not decoration) so it's visible on the graph itself, not only
        # after clicking through to the detail panel's link.
        id_part = f"{vov.vov_id} 🪞" if vov.nested_mov else vov.vov_id
        label = f"{id_part}\n{label_desc}" if label_desc else id_part

    feelings = {k: {"v": v.v, "c": v.c} for k, v in vov.feelings.items()}
    ordinances = {k: {"v": v.v, "c": v.c} for k, v in vov.ordinances.items()}

    if is_objective:
        node_color = {"background": color, "border": "#fff2b8"}
        node_shape = "circle"
        border_width = 3
        size = 22
    else:
        # Size and ring both key off the same dominant axis: the point of
        # this view is to see at a glance who's carrying the most charge
        # right now, not to track every one of the fourteen axes at once.
        dominant = _dominant_valence(vov.feelings)
        v = dominant[1] if dominant else 0.0
        size = _MIN_NODE_SIZE + (min(abs(v), 5.0) / 5.0) * (_MAX_NODE_SIZE - _MIN_NODE_SIZE)
        if dominant and abs(v) >= _SIGNIFICANT_VALENCE:
            node_color = {"background": color, "border": _RING_NEGATIVE if v < 0 else _RING_POSITIVE}
            border_width = 3
        else:
            node_color = color  # a plain string = same border as fill -> no visible ring
            border_width = 1
        node_shape = "dot"

    return {
        "id": vov.vov_id,
        "label": label,
        "group": vov.object_nature,
        "color": node_color,
        "shape": node_shape,
        "borderWidth": border_width,
        "size": size,
        "font": (
            {"color": "#2a2000", "size": 18, "bold": {"color": "#2a2000"}}
            if is_objective
            else {"color": "#aab2bf", "size": 10}
        ),
        # Everything the click-through panel needs, sent once, used by JS.
        "detail": {
            "vov_id": vov.vov_id,
            "object_nature": vov.object_nature,
            "object_type": vov.object_type,
            "valence_regime": vov.valence_regime,
            "priority": vov.priority,
            "brief_description": vov.brief_description,
            "relevant_remarks": vov.relevant_remarks,
            "perceived_age": vov.perceived_age,
            "male_female": vov.male_female,
            "nested_mov": vov.nested_mov,
            "feelings": feelings,
            "ordinances": ordinances,
            "objective": vov.objective.model_dump() if vov.objective else None,
        },
    }


# Edge color/width by how strongly-charged the relation is — MS §8.1: an
# edge in the Graph of Traces "carries affective as well as propositional"
# weight, so a bond with a lot of feeling behind it should visibly stand
# out from a purely factual one.
def _edge_weight(affective: list) -> float:
    return sum(abs(entry.get("v", 0)) for entry in (affective or []))


def _edge_from_relation(rel: dict) -> dict:
    weight = _edge_weight(rel.get("affective"))
    propositional = rel.get("propositional") or ""
    tooltip_lines = [f"<b>{rel['kind']}</b>"]
    if propositional:
        tooltip_lines.append(propositional)
    if rel.get("affective"):
        tooltip_lines.append(
            ", ".join(f"{e['axis']} {e['v']:+g}" for e in rel["affective"])
        )
    if rel.get("confidence"):
        tooltip_lines.append(f"confiança: {rel['confidence']}/5")
    return {
        "id": f"rel-{rel['id']}",
        "from": rel["from_vov_id"],
        "to": rel["to_vov_id"],
        "label": rel["kind"],
        "title": "<br>".join(tooltip_lines),
        "width": 1 + min(weight, 5),
        "color": {"color": "#7c8896", "highlight": "#f5c518"},
        "font": {"color": "#9aa5b1", "size": 10, "strokeWidth": 0},
        "smooth": {"type": "curvedCW", "roundness": 0.15},
    }


def _fallback_edge(from_id: str, to_id: str) -> dict:
    """relevant_relations (MS §6.4) names a bond the VOV itself claims,
    even when no mov_relations row (MS §8, written via WRITE_RELATION)
    has ever recorded what that bond actually is. Shown lighter/dashed
    and generically labeled so it reads as "a link exists" without
    fabricating a description the data doesn't have."""
    return {
        "id": f"fallback-{from_id}-{to_id}",
        "from": from_id,
        "to": to_id,
        "label": "",
        "title": "Vínculo listado em relevant_relations — sem registro correspondente na Graph of Traces (MS §8).",
        "dashes": True,
        "width": 1,
        "color": {"color": "#3d4550", "highlight": "#f5c518"},
        "smooth": {"type": "curvedCW", "roundness": 0.15},
    }


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/mov")
def api_mov():
    # ?mov_id=MOV_0002 lets the frontend follow a nested-MOV link (MS
    # §6.8) into that agent's own focus, instead of always showing the
    # top-level MOV -- same endpoint, same shape, just a different mov_id.
    mov_id = request.args.get("mov_id") or settings.default_mov_id
    db = get_database()
    try:
        mov = db.load_mov(mov_id)
        vov_ids = [o.vov_id for o in mov.active()]
        relations = db.get_relations(vov_ids) if vov_ids else []
    finally:
        db.close()

    nodes = [_node_from_vov(o) for o in mov.active()]
    node_ids = {n["id"] for n in nodes}

    edges = []
    covered_pairs = set()
    for rel in relations:
        if rel["from_vov_id"] not in node_ids or rel["to_vov_id"] not in node_ids:
            continue  # endpoint is archived/outside the focus — not drawn
        edges.append(_edge_from_relation(rel))
        covered_pairs.add(frozenset((rel["from_vov_id"], rel["to_vov_id"])))

    for o in mov.active():
        for other_id in o.relevant_relations:
            if other_id not in node_ids or other_id == o.vov_id:
                continue
            pair = frozenset((o.vov_id, other_id))
            if pair in covered_pairs:
                continue
            covered_pairs.add(pair)
            edges.append(_fallback_edge(o.vov_id, other_id))

    return jsonify({
        "mov_id": mov.mov_id,
        "nodes": nodes,
        "edges": edges,
        "legend": {**{k: v for k, v in _NATURE_COLORS.items()}, "Objective": _OBJECTIVE_COLOR},
    })


if __name__ == "__main__":
    import threading
    import webbrowser

    url = "http://localhost:5050"
    print("=" * 60)
    print("LirielMindReader — visualização somente-leitura da MOV")
    print(f"MOV: {settings.default_mov_id}")
    print(f"Abrindo {url} no seu navegador padrão...")
    print("(se não abrir sozinho, copie esse endereço e cole na barra do navegador)")
    print("=" * 60)
    # Fired on a short delay, on a side thread: app.run() below blocks this
    # thread forever, so the browser has to be launched either before that
    # call or from elsewhere while it runs -- a delay avoids racing the
    # server's own startup (opening before it's actually listening yet).
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=5050, debug=False)
