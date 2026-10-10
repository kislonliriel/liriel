"""
Textual <-> numeric conversion for every valence value (`v`) the
MetaScheme records on Feelings, Ordinances and Schemas — MS §3/§4/§5.2,
this session's redesign. The model reasons about a charge in words
("strong Fear", "moderate demand"); the number is a storage detail the
architecture keeps for itself and never offers to inference.

Confidence (`c`, 1-5) is out of scope here — it is an epistemic rating,
not a felt charge, and stays a plain number everywhere.

The magnitude table is deliberately bijective (`MAGNITUDE_WORDS` /
`WORD_TO_MAGNITUDE` are exact inverses) so a value can always be
round-tripped: number -> word for whatever reaches the model, word ->
number for whatever a model-authored patch needs to become in the DB.
"""
from __future__ import annotations

import re
from typing import Optional, Union

MAGNITUDE_WORDS = {
    0: "neutral",
    1: "slight",
    2: "mild",
    3: "moderate",
    4: "strong",
    5: "extreme",
}
WORD_TO_MAGNITUDE = {word: n for n, word in MAGNITUDE_WORDS.items()}


def magnitude_word(abs_v: float) -> str:
    """|v| (0-5, any float the model/DB might hold) -> its band word.
    Clamped into range rather than raising — a stray 5.5 or -0.2 (already
    tolerated elsewhere in this codebase, e.g. AxisValence.c's own clamp)
    shouldn't crash a serialization step over a half-point of magnitude."""
    n = round(abs_v)
    if n < 0:
        n = 0
    if n > 5:
        n = 5
    return MAGNITUDE_WORDS[n]


def word_to_magnitude(word: str) -> Optional[int]:
    return WORD_TO_MAGNITUDE.get(word.strip().lower())


def _axis_by_key(axis_key: str):
    from models import FEELING_AXES  # local import: avoid a cycle at module load

    return next((a for a in FEELING_AXES if a.key == axis_key), None)


def _pole_aliases(pole: str) -> set:
    """A named pole is sometimes a compound ("Love/Eros") or carries a
    parenthetical qualifier ("Happiness (DRH)", "Love (sublime)") — a
    smaller model reliably shortens these to their first/plain word
    ("Love", "Happiness") rather than reproducing the punctuation exactly.
    Confirmed for real: "moderate Love" for LoveAngerEros's "Love/Eros"
    crashed BEST_PREY_GUESS outright (float_parsing on the still-string
    `v`) the first time a real message exercised this axis. Every
    reasonable shortening is accepted as the same pole, not just the
    literal MS §3 text."""
    stripped = re.sub(r"\([^)]*\)", "", pole).strip()
    aliases = {p.strip() for p in stripped.split("/") if p.strip()}
    aliases.add(stripped)
    aliases.add(pole.strip())
    return {a.lower() for a in aliases if a}


# ---------------------------------------------------------------------------
# Feelings (-5..+5, bipolar, named poles — MS §3)
# ---------------------------------------------------------------------------

def feeling_to_text(axis_key: str, v: float) -> str:
    if v == 0:
        return "neutral"
    axis = _axis_by_key(axis_key)
    pole = (axis.positive_pole if v > 0 else axis.negative_pole) if axis else (
        "positive" if v > 0 else "negative"
    )
    return f"{magnitude_word(abs(v))} {pole}"


def text_to_feeling(axis_key: str, text: str) -> Optional[float]:
    """None on anything unparseable — callers warn and drop, same tolerance
    already used throughout models.py/motivation.py for model format drift."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if t.lower() == "neutral":
        return 0.0
    axis = _axis_by_key(axis_key)
    if axis is None:
        return None
    parts = t.split(" ", 1)
    if len(parts) != 2:
        return None
    word, pole_text = parts[0], parts[1].strip()
    magnitude = word_to_magnitude(word)
    if magnitude is None:
        return None
    pole_text_lower = pole_text.lower()
    if pole_text_lower in _pole_aliases(axis.positive_pole):
        return float(magnitude)
    if pole_text_lower in _pole_aliases(axis.negative_pole):
        return float(-magnitude)
    return None


# ---------------------------------------------------------------------------
# Ordinances (0..+5, unipolar, never negative — MS §4.1)
# ---------------------------------------------------------------------------

def ordinance_to_text(v: float) -> str:
    if v == 0:
        return "no demand"
    return f"{magnitude_word(abs(v))} demand"


def text_to_ordinance(text: str) -> Optional[float]:
    if not isinstance(text, str):
        return None
    t = text.strip().lower()
    if t == "no demand":
        return 0.0
    parts = t.split(" ")
    if len(parts) == 2 and parts[1] == "demand":
        magnitude = word_to_magnitude(parts[0])
        if magnitude is not None:
            return float(magnitude)
    return None


# ---------------------------------------------------------------------------
# Schemas (-5..+5 or free text — MS §5.2-§5.7)
# ---------------------------------------------------------------------------
# Which numeric schemas are "really" bipolar is an open point (MS §17.1,
# left for Rev 0001) — this deliberately does not resolve it. A numeric
# value's sign is rendered generically (magnitude + "positive"/"negative"),
# never against a per-schema named opposite pole, so nothing here commits
# to an answer §17.1 explicitly defers.

def schema_to_text(v: Union[float, str]) -> Union[str, str]:
    if isinstance(v, str):
        return v  # free-text schema (Culture, MindVices, ...) — unchanged
    if v == 0:
        return "neutral"
    sign = "positive" if v > 0 else "negative"
    return f"{magnitude_word(abs(v))} {sign}"


def text_to_schema(text: Union[str, float]) -> Union[float, str]:
    """A numeric-pattern string ("moderate positive") becomes its float;
    anything else (including genuine free text like "Western") passes
    through unchanged — that's exactly the right fallback, since a
    free-text schema's value was never meant to parse as magnitude+sign."""
    if not isinstance(text, str):
        return text
    t = text.strip().lower()
    if t == "neutral":
        return 0.0
    parts = t.split(" ")
    if len(parts) == 2 and parts[1] in ("positive", "negative"):
        magnitude = word_to_magnitude(parts[0])
        if magnitude is not None:
            return float(magnitude) if parts[1] == "positive" else float(-magnitude)
    return text


def convert_entry_v(nested_field: str, axis_key: str, raw_entry):
    """raw_entry is one {"v": ..., "c": ...}-shaped dict from a feelings/
    ordinances/schemas map. Converts a textual `v` back to its float (or,
    for a genuine free-text schema, leaves it as text) — a numeric `v`
    passes through unchanged, so this is safe to call unconditionally
    regardless of whether this particular entry actually used the textual
    form. Used both when validating a whole new VOV (models.py's
    VectorObjectValence validator) and when merging a single axis onto an
    existing one (motivation.py's _coerce_patch, which validates one entry
    at a time via AxisValence/SchemaEntry directly and never goes through
    the whole-VOV validator)."""
    if not isinstance(raw_entry, dict) or "v" not in raw_entry:
        return raw_entry
    v = raw_entry["v"]
    if not isinstance(v, str):
        return raw_entry
    if nested_field == "feelings":
        converted = text_to_feeling(axis_key, v)
    elif nested_field == "ordinances":
        converted = text_to_ordinance(v)
    else:
        converted = text_to_schema(v)
    if converted is None:
        return raw_entry
    return {**raw_entry, "v": converted}


def is_unconverted_numeric(nested_field: str, entry) -> bool:
    """True when `entry`'s `v` is STILL a string after `convert_entry_v` —
    i.e. genuinely unparseable text on a field that must end up a float
    (Feelings/Ordinances, always; unlike Schemas, where a `v` that never
    matched the numeric "<magnitude> positive/negative" pattern is
    correctly just left as text — that's a real free-text Schema, not a
    failure). Callers use this to drop the whole axis entry rather than
    let a stray string crash AxisValence.v's strict float type — the same
    "blank cell" tolerance MS §6.6 already gives an axis with no stated
    charge, applied to a charge that was stated but not in a form this
    module recognized."""
    return (
        nested_field in ("feelings", "ordinances")
        and isinstance(entry, dict)
        and isinstance(entry.get("v"), str)
    )
