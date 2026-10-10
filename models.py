"""
Pydantic data models for the Liriel Phase 1 MVP, aligned to the official
MetaScheme (docs/MetaScheme_Liriel_Rev0000.md — cited below as "MS §n").

Phase 1/2 still narrows what the MetaScheme itself allows narrowing (MS
§0.6: "any QUERY block may narrow this document but may not contradict
§1"):

- GRAPH_REQUEST (MS §12.1) and the Graph of Traces (MS §8) are implemented
  — graph_service.py is TrackGraphProcess, run as a service (not an LLM
  call) between Query 1 and Query 2.
- The MainMemory command vocabulary (MS §12.4): RETRIEVE/ARCHIVE/
  WRITE_RELATION/SOFTEN_CHARGE are executed (see database.py, motivation.py
  _apply_mainmemory_commands). SEARCH (free-text/filtered lookup without a
  known id) still needs a real index — logged, not acted on.
- Nested MOVs (MS §6.8, specular recursion) are materialized: a
  CREATE_NESTED_MOV/PATCH_NESTED_VOV/ARCHIVE_NESTED_MOV op (NestedMovOp
  below) creates and updates a real mov_id, stored the same way the
  default MOV is — see motivation.py's _apply_nested_mov_ops.

Everything else here mirrors the MetaScheme's field-by-field specification
(MS §6.4, §10.6, §10.7, §12.2-§12.5) as closely as a typed model allows.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional, Union, get_args

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from valence_text import convert_entry_v, is_unconverted_numeric

# Small and large models alike sometimes write a placeholder ("NA", "N/A",
# "None", "-") into a field that's genuinely absent instead of emitting JSON
# null — seen from Gemma on `priority` and from Claude Sonnet 5 on
# `nested_mov` (the latter crashed the DB insert: MS §6.4 says nested_mov is
# a mov_id or absent, and "NA" isn't a real mov_id, so it tripped the
# foreign-key constraint on mov_objects). Module-level, not a class
# attribute, because Pydantic v2 turns an underscore-prefixed class
# attribute into a per-instance PrivateAttr rather than a shared constant.
_PLACEHOLDER_NONE_TOKENS = {"na", "n/a", "none", "null", "-", ""}


def _clamp_confidence_value(v):
    """AxisValence/SchemaEntry's `c` (MS §6.4 confidence, 1-5) is validated
    strictly wherever it sits inside a top-level query result (e.g.
    BestPreyGuessResult.best_prey_guess) — unlike a mov_op's patch, there is
    no lenient merge path to fall back on there, so one out-of-range value
    anywhere in an otherwise-valid ~2000-token JSON object used to crash the
    *entire* cycle. Seen for real: Claude Sonnet 5 emitted `\"c\": 0` (reading
    it as \"no confidence\" rather than the MS §6.4 floor of 1). Clamp into
    range instead of raising; leave None/absent as-is (a real \"unstated\")."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    if v < 1:
        return 1
    if v > 5:
        return 5
    return int(v)

# ---------------------------------------------------------------------------
# The fourteen Feeling axes (MS §3)
# ---------------------------------------------------------------------------

class FeelingAxisDef(BaseModel):
    key: str
    label: str
    positive_pole: str
    negative_pole: str
    description: str


FEELING_AXES: List[FeelingAxisDef] = [
    FeelingAxisDef(
        key="HopeFear",
        label="Hope / Fear",
        positive_pole="Hope",
        negative_pole="Fear",
        description="Hope at the good that seems likely, fear at the threat drawing near.",
    ),
    FeelingAxisDef(
        key="BodySensationsPleasurePain",
        label="Pleasure / Pain",
        positive_pole="Pleasure",
        negative_pole="Pain",
        description="Physical pleasure and pain in their most direct, non-negotiable form.",
    ),
    FeelingAxisDef(
        key="BodySensationsOther",
        label="Other body sensations",
        positive_pole="Well-being",
        negative_pole="Malaise",
        description=(
            "Every other report of the body: hunger, thirst, cold, heat, "
            "itch, tingling, shivers, ticklishness, fatigue."
        ),
    ),
    FeelingAxisDef(
        key="PrideEmbarrassmentShame",
        label="Pride / Embarrassment-Shame",
        positive_pole="Pride",
        negative_pole="Embarrassment/Shame",
        description="Knowing oneself well regarded vs. knowing/suspecting oneself the object of contempt.",
    ),
    FeelingAxisDef(
        key="AttractionDisgust",
        label="Attraction / Disgust",
        positive_pole="Attraction",
        negative_pole="Disgust",
        description="Drawn to bodies, things, ideas, up to fascination, vs. repelled, down to revulsion.",
    ),
    FeelingAxisDef(
        key="ExcitementBoredom",
        label="Excitement / Boredom",
        positive_pole="Excitement",
        negative_pole="Boredom",
        description="Stirred, taken up by the object, vs. the emptiness of what fails to hold.",
    ),
    FeelingAxisDef(
        key="LoveAngerEros",
        label="Love-Anger-Eros",
        positive_pole="Love/Eros",
        negative_pole="Anger",
        description="Warmth directed at someone, tenderness, desire, vs. fury — a single bundle.",
    ),
    FeelingAxisDef(
        key="MirthGloom",
        label="Mirth / Gloom",
        positive_pole="Mirth",
        negative_pole="Gloom",
        description="The comic, the amusing as felt from within, vs. the somber, the gloomy.",
    ),
    FeelingAxisDef(
        key="CutenessCreepiness",
        label="Cuteness / Creepiness",
        positive_pole="Cuteness",
        negative_pole="Creepiness",
        description="The tender 'aww' that draws in and stirs protection, vs. the eerie that pushes away.",
    ),
    FeelingAxisDef(
        key="PositiveNegativeAmazement",
        label="Amazement (+/-)",
        positive_pole="Awe",
        negative_pole="Horror",
        description="Wonderstruck awe before the sublime, vs. horror-struck awe before the terrible.",
    ),
    FeelingAxisDef(
        key="CuriosityIndifference",
        label="Curiosity / Indifference",
        positive_pole="Curiosity",
        negative_pole="Indifference",
        description="Intrigued, something asks to be known, vs. nothing intrigues.",
    ),
    FeelingAxisDef(
        key="HappinessSadnessDRH",
        label="Happiness/Sadness (Desire-Related)",
        positive_pole="Happiness (DRH)",
        negative_pole="Sadness (DRH)",
        description=(
            "Desire Related Happiness: the immediate joy of what one wanted and "
            "got, or is about to get, vs. disappointment, the thwarted plan. Quick, dated."
        ),
    ),
    FeelingAxisDef(
        key="LoveHateSublime",
        label="Love / Hate (Sublime)",
        positive_pole="Love (sublime)",
        negative_pole="Hate",
        description=(
            "The deep, lasting bond with what transcends the everyday — love that "
            "fills and lifts, vs. hate that poisons and corrodes. Runs through self-esteem."
        ),
    ),
    FeelingAxisDef(
        key="HappinessSadnessCES",
        label="Happiness/Sadness (Existential)",
        positive_pole="Happiness (CES)",
        negative_pole="Sadness (CES)",
        description=(
            "Core Existential Satisfaction: the grave, diffuse weight a thing carries "
            "on the plane of existence. Slow, not dissolved by a better day."
        ),
    ),
]

AXIS_KEYS: List[str] = [axis.key for axis in FEELING_AXES]


# ---------------------------------------------------------------------------
# Ordinances (MS §4) and Modulating Schemas (MS §5) — reference lists.
# Recorded on a VOV's `ordinances` / `schemas` maps (MS §6.4, §16).
# ---------------------------------------------------------------------------

INSTINCT_KEYS: List[str] = [
    "InstinctSurvival",
    "InstinctGregariousness",
    "InstinctMotherhood",
    "InstinctProcreation",
    "InstinctCompanionship",
    "InstinctExploration",
    "InstinctGamePlay",
    "InstinctFatherhood",
]

ARCHETYPE_KEYS: List[str] = [
    "ArchetypeSingularity",
    "ArchetypeShadow",
    "ArchetypeDignity",
    "ArchetypeTrickster",
    "ArchetypeHero",
    "ArchetypeIntegrity",
    "ArchetypeAnimaAnimus",
    "ArchetypeEntertainment",
    "ArchetypeCreation",
]

# MS §16: Instincts + Archetypes + the third family (PersistentLongings).
ORDINANCE_KEYS: List[str] = INSTINCT_KEYS + ARCHETYPE_KEYS + ["PersistentLongings"]

SCHEMA_KEYS: List[str] = [
    "Culture",
    "SympathyAntipathyforStructReality",
    "CharacterHumbleness",
    "CharacterCourage",
    "CharacterEmpathy",
    "CharacterHonesty",
    "CharacterIntentionality",
    "CharacterForgiveness",
    "CharacterTemperance",
    "CharacterGoodEvil",
    "MoralBalance",
    "PersonalityAmbition",
    "PersonalityAuthenticity",
    "PersonalityAffectivity",
    "PersonalityAgreeableness",
    "PersonalityNeuroticism",
    "PersonalityOpenness",
    "PersonalityExtraversion",
    "PersonalityConscientiousness",
    "PersonalityLeadership",
    "PersonalitySociability",
    "PersonalityNaturalAbility",
    "IntelligenceLevel",
    "MindVices",
    "MentalDisorders",
    "BodyFeatures",
]


# ---------------------------------------------------------------------------
# Core value types (MS §6.4-§6.5)
# ---------------------------------------------------------------------------

class AxisValence(BaseModel):
    """A (v, c) pair on one Feeling or Ordinance axis — MS §6.4 uses the
    literal keys `v` (value) and `c` (confidence), e.g. {"v": 1, "c": 2}
    (MS §12.2); keep the same keys here so the model's output round-trips
    without translation.

    Feelings: v in [-5, +5] (MS §3). Ordinances: v in [0, +5], never
    negative (MS §4.1, §14.2) — enforced by callers, not here, since the
    two share this same shape.
    """

    v: float
    c: Optional[int] = Field(None, ge=1, le=5)

    @field_validator("c", mode="before")
    @classmethod
    def _clamp_confidence(cls, v):
        return _clamp_confidence_value(v)


class SchemaEntry(BaseModel):
    """One Modulating Schema reading, same {v, c} shape as AxisValence (MS
    §12.2 example: `"Culture": {"v": "Western", "c": 4}`). Most are numeric
    (MS §5.2); a few (Culture, PersonalityNaturalAbility, MindVices,
    MentalDisorders, BodyFeatures) are free text (MS §5.3, §5.5, §5.7)."""

    v: Union[float, str]
    c: Optional[int] = Field(None, ge=1, le=5)

    @field_validator("c", mode="before")
    @classmethod
    def _clamp_confidence(cls, v):
        return _clamp_confidence_value(v)


# These categorical fields are documented as closed vocabularies in MS §16,
# and the prompts (prompts.py) tell the model exactly what they are. But
# none of them drive branching in this codebase — they're read/stored, not
# switched on — so they're typed as plain `str` rather than a strict
# Literal: a small local model drifting one value off spec (e.g. writing
# "self" for a relation_to_liriel MS §16 doesn't even enumerate for
# Liriel's own row) shouldn't crash the whole cycle over a descriptive
# field. MovOpName and RetrospectiveAction below DO drive branching in
# motivation.py and stay strict Literals for exactly that reason.
ObjectType = str  # MS §6.4, §16: real | imagined | hypothetical
_CLOSED_OBJECT_NATURES = ("PCI", "Person", "Sentient", "Objective", "Situation", "Thing", "Idea", "Event", "Memory", "Group", "Animal",
                          "Entity", "Attribute", "Self-Process", "ScenarioData", "Identity",
                          "Interpellation")  # mirrors mov_objects_object_nature_check (migration 011)
ObjectNature = str  # MS §6.4, §16: PCI | Sentient | Objective | Situation | Thing | Idea | Event | Memory | Group | Animal | Entity | Attribute | Self-Process | ScenarioData | Identity | Interpellation

# Rev 0007 AZ (MS §6.14): an `Interpellation` sits BETWEEN its parties -- one `Link_Identity_Part` edge from the Interpellation to each party
# (satellite -> principal, like every Identity-category edge), the `label` of the edge saying the party's role in it.
INTERPELLATION_ROLES = ("origin", "target")
ValenceRegime = str  # MS §6.4, §16: State | Delta

# Closed relation-kind vocabulary — MS §8.3. Rev 0006 (the ontology of Objects and
# relations): `kind` is the CATEGORY of a bond, six of them. The first three are the
# original closed set; Genealogical, Space_Time and Symbolic are new, and
# `Link_Identity_Part` is the "identity" category of the ontology (a satellite
# record or part -> the principal Object whose representation it documents).
#
#   Link_Subject_Cluster  Subject           same matter; sustains the clusters (MS §6.10)
#   Link_Valence_Load     Emotional charge  an emotional charge, from one side's perspective
#   Link_Genealogical     Genealogical      kinship and conjugal bond, in the broad sense
#   Link_Space_Time       Space-time        places, moments, encounters, happenings
#   Link_Symbolic         Symbolic          cultural / social / institutional / religious / role bonds
#   Link_Identity_Part    Identity          satellite (Identity record, Sub-Object) -> principal Object
#
# Strict on purpose, like MovOpName/RetrospectiveAction below: `kind` drives real
# branching (a cluster recall walks only Link_Subject_Cluster edges; an identity
# recall walks only Link_Identity_Part ones), so an unrecognized value silently
# matching nothing would be a worse failure mode than refusing it — but see
# GraphRequestItem's own before-validator, which drops stray values from
# `relation_kinds` rather than letting one bad entry fail the whole GRAPH_REQUEST
# the way a bare strict Literal would (this codebase's established tolerance for a
# small model's drift). The list is not closed forever (ontology §4): a category is
# added only for a concrete need of ProcessMotivation, never to be exhaustive.
RelationKind = Literal[
    "Link_Subject_Cluster",
    "Link_Valence_Load",
    "Link_Genealogical",
    "Link_Space_Time",
    "Link_Symbolic",
    "Link_Identity_Part",
]
_RELATION_KINDS = set(get_args(RelationKind))

RELATION_CATEGORY_LABEL = {
    "Link_Subject_Cluster": "Subject",
    "Link_Valence_Load": "Emotional charge",
    "Link_Genealogical": "Genealogical",
    "Link_Space_Time": "Space-time",
    "Link_Symbolic": "Symbolic",
    "Link_Identity_Part": "Identity",
}

# Whether a bond is directed when its writer does not say. A bond is read from one
# side's perspective (A's charge for B, A is B's father, A is B's employer, a record
# documenting B) except two: membership in one matter, and a shared place/moment, are
# symmetric. A writer always overrides this with an explicit `directed`.
_RELATION_DEFAULT_DIRECTED = {
    "Link_Subject_Cluster": False,
    "Link_Valence_Load": True,
    "Link_Genealogical": True,
    "Link_Space_Time": False,
    "Link_Symbolic": True,
    "Link_Identity_Part": True,
}


def default_directed(kind: str) -> bool:
    return _RELATION_DEFAULT_DIRECTED.get(kind, True)

Genus = str  # MS §16: Prey | ThreatResponse
Species = str  # MS §16: Conquest | Healing
ThreatNature = str  # MS §16: Fight | Flight
ThreatHorizon = str  # MS §16: Immediate | Imminent | Contingency
GainForm = str  # MS §16: Increment | Recovery | AvoidedNegativation
ObjectiveStatus = str  # MS §16: open | pending_urgent | resolved | abandoned
Outcome = str  # MS §16: attained | partial | failed | interrupted
Attribution = str  # MS §16: world_opacity | judgment_error | information_gap | deception | mixed


class ObjectiveBlock(BaseModel):
    """MS §10.6. Required on every VOV with object_nature == "Objective"."""

    genus: Genus
    species: Optional[Species] = None
    threat_nature: Optional[ThreatNature] = None
    threat_horizon: Optional[ThreatHorizon] = None
    gain_form: GainForm
    channel_ordinances: List[str] = Field(default_factory=list)
    beneficiary_scope: List[str] = Field(default_factory=list)
    granularity: str = "high_level"  # MS §2.3, §16: high_level | stage
    status: ObjectiveStatus = "open"
    cycles_open: int = 0
    information_seeking: bool = False


class DeltaReport(BaseModel):
    """MS §10.7. Written only once an Objective's outcome is known."""

    resolved_at: Optional[str] = None
    outcome: Outcome
    per_axis: Dict[str, Dict[str, float]] = Field(default_factory=dict)
    attribution: Attribution
    attribution_note: Optional[str] = None


# ---------------------------------------------------------------------------
# Identity records (MS §6.13, Rev 0006) — an Object of nature "Identity" documents
# something learned or changed about a principal Object or about one relation.
# ---------------------------------------------------------------------------

# What a record says about the change (ontology §7): new information that was not
# there before / an earlier understanding put right / the represented thing itself
# changed in the world. Kept distinct on purpose — a correction must stay
# recognizable as later than what it corrects, and a world change is not a mistake.
IDENTITY_CHANGE_KINDS = ("added", "corrected", "world_change")
# How the information reached Liriel — a reported fact is not the model's inference.
IDENTITY_OBTAINED_VIA = ("reported", "observed", "inferred")


class RelationRef(BaseModel):
    """One specific relation, unambiguously (ontology §8): the edge's own id plus a
    snapshot of its natural key, so a record stays readable — and findable — even
    when only the key is known. Two Objects may share several bonds, so the pair
    alone never identifies one."""

    relation_id: Optional[str] = None
    from_vov_id: str
    to_vov_id: str
    kind: str
    label: str = ""


class IdentityRecordBlock(BaseModel):
    """The payload of an `Identity` Object (MS §6.13). `field`, `value_before` and
    `value_after` are the architecture's own bookkeeping — copied from the state it
    overwrote, never from the model; `information`, `change_kind`, source, reliability
    and context are the model's judgment about it. Immutable once written: a later
    correction is a NEW record that `supersedes` this one, never an edit."""

    target_kind: str = "object"                 # "object" | "relation"
    target_vov_id: Optional[str] = None          # target_kind == "object": the principal Object
    relation: Optional[RelationRef] = None       # target_kind == "relation": which edge
    field: str                                   # which field of the target changed
    attribute: Optional[str] = None              # the aspect affected, in the model's words
    change_kind: str = "added"                   # IDENTITY_CHANGE_KINDS
    information: str                             # what was learned/changed, short
    value_before: Optional[str] = None           # prior value, when there was one
    value_after: Optional[str] = None            # the value now
    source: Optional[str] = None                 # who/what it came from
    obtained_via: Optional[str] = None           # IDENTITY_OBTAINED_VIA
    reliability: Optional[int] = Field(None, ge=1, le=5)
    recorded_at: str                             # when Liriel recorded it (architecture clock)
    occurred_at: Optional[str] = None            # when it happened, if known and pertinent
    context: Optional[str] = None                # short justification
    supersedes: Optional[str] = None             # vov_id of the earlier record this corrects
    cycle_id: Optional[str] = None

    @field_validator("reliability", mode="before")
    @classmethod
    def _clamp_reliability(cls, v):
        return _clamp_confidence_value(v)


class VectorObjectValence(BaseModel):
    """One Object in the MOV — one VOV (MS §6.3-§6.4)."""

    vov_id: str
    priority: Optional[int] = None  # "NA" on non-Objective rows -> None
    update_datetime: Optional[str] = None
    object_type: ObjectType = "real"
    object_nature: ObjectNature
    valence_regime: ValenceRegime = "State"
    nested_mov: Optional[str] = None
    perceived_age: Optional[str] = None
    male_female: Optional[str] = None
    brief_description: str
    relevant_relations: List[str] = Field(default_factory=list)
    delta_report: Optional[DeltaReport] = None
    relevant_remarks: Optional[str] = None
    feelings: Dict[str, AxisValence] = Field(default_factory=dict)
    ordinances: Dict[str, AxisValence] = Field(default_factory=dict)
    schemas: Dict[str, SchemaEntry] = Field(default_factory=dict)
    objective: Optional[ObjectiveBlock] = None
    # Rev 0006 (MS §6.13): present only on rows of object_nature "Identity".
    identity_record: Optional[IdentityRecordBlock] = None
    updated_at: Optional[datetime] = None
    archived: bool = False  # true once ARCHIVE_VOV/MainMemory ARCHIVE fires

    @field_validator(
        "priority", "nested_mov", "perceived_age", "male_female",
        "relevant_remarks", mode="before",
    )
    @classmethod
    def _placeholder_means_none(cls, v):
        if isinstance(v, str) and v.strip().lower() in _PLACEHOLDER_NONE_TOKENS:
            return None
        return v

    @model_validator(mode="before")
    @classmethod
    def _normalize_object_nature(cls, data):
        """`object_nature` is the one closed vocabulary that the database enforces with a CHECK constraint and that, until now, nothing
        normalized (object_type and valence_regime already are, just below). QA wave 5, step 1: for a character in a tabletop game the
        12B wrote `object_nature: "Imagined"` (the word of `object_type`, the field next to it) and the cycle died at commit with
        `mov_objects_object_nature_check`, losing everything it had done. So: a nature that differs from the list only by case is
        put right; a type word (`imagined`, `hypothetical`) written as the nature becomes the row's `object_type` (when that was
        left as `real`) and the nature falls back to the neutral `Thing`; any other value outside the list also falls back to `Thing`,
        with a warning — the same 'unrecognized value takes the field's default' policy as object_type and valence_regime."""
        if not isinstance(data, dict) or not isinstance(data.get("object_nature"), str):
            return data
        raw = data["object_nature"].strip()
        canonical = {n.lower(): n for n in _CLOSED_OBJECT_NATURES}
        if raw.lower() in canonical:
            data = dict(data); data["object_nature"] = canonical[raw.lower()]
            return data
        data = dict(data)
        if raw.lower() in {"imagined", "hypothetical"} and str(data.get("object_type") or "real").strip().lower() == "real":
            data["object_type"] = raw.lower()
        print(f"[warning] object_nature {raw!r} is not in the closed list (MS §6.4) on {data.get('vov_id')!r} — using 'Thing'")
        data["object_nature"] = "Thing"
        return data

    @field_validator("object_type", mode="before")
    @classmethod
    def _normalize_object_type(cls, v):
        """`object_type` (MS §6.4: real/imagined/hypothetical, the Object's
        ontological standing) sits right next to `object_nature` (MS §6.4:
        PCI/Person/Objective/.../Self-Process) in the same JSON row, and a
        model sometimes writes one field's vocabulary into the other —
        confirmed for real, `"object_type": "Objective"` (object_nature's
        own word, not this field's). Not a strict Literal here on purpose
        (MovOpName/RetrospectiveAction drive branching and stay strict for
        that reason; this one doesn't) — but mov_objects' own check
        constraint enforces the three-value enum regardless, so a value
        this field doesn't recognize crashed anyway, just later (at the
        database write) and far more crypticly than at validation. Falls
        back to the field's own default ("real") for anything
        unrecognized, the same as leaving it unset already would."""
        if isinstance(v, str):
            lowered = v.strip().lower()
            return lowered if lowered in {"real", "imagined", "hypothetical"} else "real"
        return v

    @field_validator("valence_regime", mode="before")
    @classmethod
    def _normalize_valence_regime(cls, v):
        """Same confusable-adjacent-vocabulary risk as object_type above,
        against mov_objects' own `valence_regime in ('State', 'Delta')`
        check constraint — falls back to the field's own default
        ("State", the overwhelmingly common regime; "Delta" only applies
        to Objective rows, MS §6.7) for anything unrecognized."""
        if isinstance(v, str):
            return v if v in ("State", "Delta") else "State"
        return v

    @field_validator("perceived_age", "male_female", mode="before")
    @classmethod
    def _stringify_bare_number(cls, v):
        """perceived_age is Optional[str] (MS §6.4 allows "unknown", "N/A",
        an age band, ...) but a model sometimes writes a bare number instead
        of a string — seen from Claude Sonnet 5 (`"perceived_age": 55` for
        Patricia), same failure shape hit importing the reference xlsx
        (`26.0`). Pydantic v2 doesn't auto-coerce int/float to str, so this
        silently failed the whole VOV with no existing row to patch onto
        (UPSERT_VOV skipped, object never created) rather than raising
        somewhere visible. Stringify here, dropping a spurious ".0"."""
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        if isinstance(v, (int, float)):
            return str(v)
        return v

    @field_validator("relevant_relations", mode="before")
    @classmethod
    def _none_means_empty(cls, v):
        return v or []

    @model_validator(mode="before")
    @classmethod
    def _convert_textual_valences(cls, data):
        """Feelings/Ordinances/Schemas `v` reaches inference as text — e.g.
        "strong Fear", "moderate demand", "moderate positive" — never a bare
        signed number (this session's redesign, MS §3/§4/§5.2; see
        valence_text.py for the correspondence table and the rationale).
        Converts each entry's `v` back to the float the rest of this
        codebase already expects, before AxisValence.v's float type ever
        sees it. Runs here (whole-VOV level) so it covers both a genuinely
        new UPSERT_VOV object (motivation.py's `_coerce_vov` calls
        `VectorObjectValence.model_validate` directly) and a VOV nested
        straight inside another query's own contract (e.g.
        BestPreyGuessResult.best_prey_guess) — anywhere this class is
        validated at all. The one path this does NOT cover is
        `_coerce_patch`'s single-axis merge (AxisValence/SchemaEntry
        validated one entry at a time, never as a whole VOV) — that path
        calls the same `valence_text.convert_entry_v` directly.
        A `v` that's already numeric passes through untouched. A `v` that
        stays a string after conversion (a Feeling/Ordinance pole this
        module didn't recognize — confirmed for real, "moderate Love" for
        LoveAngerEros crashed the whole BestPreyGuessResult on
        AxisValence.v's strict float type before this guard existed) is
        dropped, not passed through to crash: same tolerance as an axis
        with no stated charge (MS §6.6), not a reason to fail the query."""
        if not isinstance(data, dict):
            return data
        for nested_field in ("feelings", "ordinances", "schemas"):
            entries = data.get(nested_field)
            if isinstance(entries, dict):
                converted = {}
                for k, v in entries.items():
                    new_entry = convert_entry_v(nested_field, k, v)
                    if is_unconverted_numeric(nested_field, new_entry):
                        print(f"[warning] unparseable {nested_field} value for axis "
                              f"{k!r}: {new_entry.get('v')!r} (MS §3.5 textual form "
                              f"not recognized) — dropping this axis")
                        continue
                    converted[k] = new_entry
                data = {**data, nested_field: converted}
        return data

    @field_validator("updated_at", mode="before")
    @classmethod
    def _tolerate_malformed_updated_at(cls, v):
        """`updated_at` is database.py bookkeeping (MS never asks for it —
        the field it does define is `update_datetime`, a plain string,
        MS §6.4), excluded from what prompts.py shows the model precisely
        so it has nothing to imitate. Confirmed necessary anyway: the 12B
        Gemma model emitted its own value here once regardless
        ('2026-09-17T010802.000000Z' — missing separators, not valid
        ISO-8601), which crashed the whole cycle with no existing row to
        fall back on. A stray value here is never load-bearing — it's
        overwritten by the database layer on the next read/write — so
        drop anything that doesn't parse instead of failing the cycle
        over a field the model was never supposed to set."""
        if v is None or isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None


class MatrixObjectsValence(BaseModel):
    """The MOV: Liriel's locus of attention (MS §6.2). Archived rows are
    kept (not deleted) so the same table doubles as the MainMemory (MS §7)
    — see database.py — but are filtered out of what reaches the prompt."""

    mov_id: str
    objects: List[VectorObjectValence] = Field(default_factory=list)

    def active(self) -> List[VectorObjectValence]:
        return [o for o in self.objects if not o.archived]

    def get(self, vov_id: str) -> Optional[VectorObjectValence]:
        return next((o for o in self.objects if o.vov_id == vov_id), None)

    def upsert(self, obj: VectorObjectValence) -> None:
        for i, existing in enumerate(self.objects):
            if existing.vov_id == obj.vov_id:
                self.objects[i] = obj
                return
        self.objects.append(obj)


# ---------------------------------------------------------------------------
# ProcessMotivation cycle artifacts (MS §0.3, §9)
# ---------------------------------------------------------------------------

class ScenarioData(BaseModel):
    """What ProcessCommandControl would report (MS §9, §12.6). In Phase 1
    this is just the raw text the user typed in the terminal chat — there
    is no separate ProcessCommandControl call producing a structured report."""

    text: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "terminal_chat"
    # Rev 0007 V: who the CHANNEL says wrote it (a name, label or vov_id the embodiment vouches for — e.g. the Telegram
    # account's person). None = the channel does not say; Query 1 then reads the author from the text.
    sender: Optional[str] = None


class Artifacts(BaseModel):
    """MS §0.3 block [3]: MOV + nested MOVs (MS §6.8 — one level of specular
    recursion per entry; motivation.py gathers them from any VOV.nested_mov
    pointer active in `mov`), GraphOfTraces (MS §8.3 shape, built by
    graph_service.build_graph_of_traces — None on Query 1/GRAPH_REQUEST
    itself, per MS §8.2: "the one Artifact not available when the cycle
    begins"), and ScenarioData.

    GraphOfTraces itself now carries MS §12.4 SEARCH's results too — an
    anchor graph_service.search_memory finds by keyword/fuzzy content
    match (any Object nature, not just a name) is fed into the same
    build_graph_of_traces traversal as one the model requested by id, so
    it arrives here with its relations already pulled in, not as a
    separate flat list. There is deliberately no second "candidates"
    artifact alongside this one."""

    meta_scheme: str
    mov: MatrixObjectsValence
    nested_movs: List[MatrixObjectsValence] = Field(default_factory=list)
    graph_of_traces: Optional[dict] = None
    scenario_data: ScenarioData
    # This revision's redesign (MS §9.4/§11/§12.1-§12.3): Query 1's own
    # Savanna reading, threaded into Query 2/3 so neither re-derives it
    # independently; the full Current Tactical Scene, built once by Query 3
    # and threaded into Queries 4-6 the same way graph_of_traces already is.
    scene_elements: List["ElementEntry"] = Field(default_factory=list)
    current_tactical_scene: Optional["TacticalSceneInterpretationResult"] = None
    # Rev 0003: the per-Object ANCHOR_REVIEW results (Query 3B), threaded into
    # MOV_UPDATE so a `missing_cause` any of them flagged (a cause Object not on
    # record yet) reaches the one query that can create it.
    anchor_reviews: List["AnchorReviewResult"] = Field(default_factory=list)
    # Rev 0007 AR: the fresh safety read of THIS message alone (None when it was not made or failed).
    safety_screen: Optional["SafetyScreenResult"] = None
    # Rev 0007: Query 1's answer to "who is writing" (see SceneSubjectCheckResult.interlocutor).
    interlocutor: Optional[str] = None
    # Rev 0007 T: the ARCHIVED Objects the retrieval brought into this cycle's graph (id + description), listed for Query 4.
    archived_in_graph: List[dict] = Field(default_factory=list)
    # Rev 0007 W: the ARCHIVED hits of the automatic MainMemory search (id, nature, description), listed for Query 1.
    archived_candidates: List[dict] = Field(default_factory=list)
    # Rev 0005: the ids MOV_UPDATE created or changed this cycle (real ids, after
    # minting), so RELATIONS_UPDATE knows which Objects' bonds are new business.
    written_vov_ids: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Query 1 — SCENE_SUBJECT_CHECK (MS §12.1, this revision's redesign)
# ---------------------------------------------------------------------------

class ElementEntry(BaseModel):
    """MS §12.1. One Object belonging to this cycle's Savanna — the full
    set when `is_new_subject` is true, only what's new/changed otherwise.
    Exactly one of `vov_id`/`provisional_label` is meaningful: `vov_id`
    when the Object already has a row (anywhere — active or archived),
    `provisional_label` free text when it doesn't exist as any VOV yet.
    Minting the real nickname id happens only at UPSERT_VOV time (MS
    §12.6), once TACTICAL_SCENE_INTERPRETATION has had its own say on what
    the Object actually is — asking for a properly composed nickname this
    early, before that judgment has even run, would be premature structure."""

    vov_id: Optional[str] = None
    provisional_label: Optional[str] = None
    object_nature: Optional[str] = None
    is_hunter: bool = False
    new_this_cycle: bool = True
    # Rev 0007 AH: for an element that is a CAUSE (a Situation, Event or Thing, not a hunter): what it provokes, and in whom.
    provokes: Optional[str] = None
    # Rev 0007 AL: for an element that carries an EXISTING vov_id: the words (of the report and of the row on record) that show it is that very Object.
    same_as: Optional[str] = None


class SceneSubjectCheckResult(BaseModel):
    """MS §12.1. Query 1 of the ProcessMotivation cycle — run before the
    Graph of Traces exists, same timing MS §8.2 already required of the
    old GRAPH_REQUEST. Answers MS §9.4 front 1/2's first question: is this
    the same matter continuing, or a new one, and who/what is actually on
    the board."""

    query: str = "SCENE_SUBJECT_CHECK"
    cycle_id: Optional[str] = None
    is_new_subject: bool = True
    # Set when continuing a matter whose own ScenarioData backbone is
    # already visible in the MOV above; left null when the matter
    # continues something currently archived (TACTICAL_SCENE_INTERPRETATION/
    # GRAPH_REQUEST may still discover it later the same cycle) or when
    # is_new_subject is true.
    continuing_scenario_data_id: Optional[str] = None
    elements: List[ElementEntry] = Field(default_factory=list)
    # Rev 0007: who wrote this message — the person this cycle's reply is addressed to — as the
    # `vov_id` or `provisional_label` of one of `elements`. The architecture never guesses it from
    # the text; the reply's discretion (what may be said to WHOM) depends on it.
    interlocutor: Optional[str] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Query 2 — GRAPH_REQUEST (MS §12.2, narrowed this revision: no longer
# independently reconstructs current_tactical_scene_draft/hunters, since
# Query 1 (SCENE_SUBJECT_CHECK) already did; SEARCH moves here from the old
# Query 2, since "what needs retrieving from MainMemory" is this query's
# own question now, MS §9.4's forward note)
# ---------------------------------------------------------------------------

class GraphRequestItem(BaseModel):
    focus_objects: List[str] = Field(default_factory=list)
    relation_kinds: List[RelationKind] = Field(default_factory=list)
    include_archive: bool = True
    depth: int = 1
    reason: Optional[str] = None
    # Rev 0006 (ontology §10): selective retrieval. All optional — an unset filter
    # is "no filter", and the relevance of a bond is judged by the model, never by
    # these (a weak bond can be decisive; the code only offers filters and limits).
    #   labels        only bonds whose `label` is one of these ("conjugal", ...)
    #   min_strength  only bonds at least this strong; a bond with NO stated
    #                 strength is kept — an absent strength is not a zero
    #   min_charge    only emotional bonds at least this charged (a magnitude word:
    #                 slight/mild/moderate/strong/extreme); other categories are kept
    #   within_subject  only Objects belonging to this matter (a ScenarioData id)
    labels: List[str] = Field(default_factory=list)
    min_strength: Optional[int] = None
    min_charge: Optional[str] = None
    within_subject: Optional[str] = None

    @field_validator("labels", mode="before")
    @classmethod
    def _labels_as_list(cls, v):
        """One label written as a bare string is one label; anything else that is not a
        list of strings means no label filter (a filter only ever narrows, so dropping a
        malformed one degrades toward "no filter", never toward a failed request)."""
        if isinstance(v, str):
            return [v] if v.strip() else []
        if isinstance(v, list):
            return [x for x in v if isinstance(x, str) and x.strip()]
        return []

    @field_validator("min_strength", mode="before")
    @classmethod
    def _strength_filter(cls, v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if 1 <= n <= 5 else None

    @field_validator("min_charge", "within_subject", mode="before")
    @classmethod
    def _text_filter(cls, v):
        return v.strip() if isinstance(v, str) and v.strip() else None

    @field_validator("relation_kinds", mode="before")
    @classmethod
    def _drop_unrecognized_kinds(cls, v):
        """`relation_kinds` is a closed six-value vocabulary (RelationKind
        above), unlike the open one it replaces — but a strict Literal
        alone would fail the WHOLE GraphRequestResult over one stray value
        a model reaches for out of habit (the old open vocabulary's
        "marriage"/"kinship"/etc., or a typo). An empty `relation_kinds`
        already means "no filter" (graph_service.build_graph_of_traces:
        `req.relation_kinds or None`), so dropping an unrecognized entry
        is always safe — at worst it degrades toward that same no-filter
        behavior instead of aborting the request."""
        if not isinstance(v, list):
            return v
        return [k for k in v if k in _RELATION_KINDS]
    # Phase 1 addition — MS §0.6 permits a QUERY to narrow this document,
    # and an optional field a model can simply not set is not a narrowing
    # that could contradict anything: a §12.1-only model still produces a
    # valid request. Set when the user explicitly insists Liriel make an
    # effort to remember something specific; graph_service.py raises the
    # MainMemory node threshold for this request only when set. There's no
    # state to revert afterward — the next cycle just doesn't set it, and
    # the normal threshold applies on its own.
    deep_recall_requested: bool = False


class RelationKey(BaseModel):
    """How the model names ONE relation in a request (ontology §8): the natural key,
    never an id it would have to copy. The architecture resolves it to the edge."""

    from_vov_id: str = Field(alias="from")
    to_vov_id: str = Field(alias="to")
    kind: str
    label: str = ""

    model_config = {"populate_by_name": True}


class IdentityRecordRequest(BaseModel):
    """MS §12.2 (Rev 0006). A request for the Identity records that explain one
    Object's current state, one attribute of it, or one specific relation — wanted
    only when the current state alone is not enough. A bounded, selective view:
    `limit` is capped by the architecture (settings.identity_records_max)."""

    target: Optional[str] = None                 # a principal Object's vov_id
    relation: Optional[RelationKey] = None       # or one specific relation
    attribute: Optional[str] = None
    change_kinds: List[str] = Field(default_factory=list)
    since: Optional[str] = None                  # ISO date/time, inclusive
    until: Optional[str] = None
    limit: Optional[int] = None
    reason: Optional[str] = None

    @field_validator("change_kinds", mode="before")
    @classmethod
    def _known_change_kinds(cls, v):
        if not isinstance(v, list):
            return v
        return [k for k in v if k in IDENTITY_CHANGE_KINDS]


class GraphRequestResult(BaseModel):
    """MS §12.2. Query 2 of the ProcessMotivation cycle — run before the
    Graph of Traces exists (MS §8.2), so it works from the MOV, ScenarioData
    and Query 1's own `elements` to decide whose relations are worth
    surveying. `search_commands` is for exactly the elements Query 1 could
    only give a `provisional_label`, or a known element whose id genuinely
    isn't resolvable — moved here from the old Query 2 (this revision's
    redesign), since MainMemory retrieval is this query's own question now."""

    query: str = "GRAPH_REQUEST"
    cycle_id: Optional[str] = None
    requests: List[GraphRequestItem] = Field(default_factory=list)
    # Rev 0006: Identity records wanted alongside the graph (MS §12.2).
    identity_requests: List[IdentityRecordRequest] = Field(default_factory=list)

    @field_validator("identity_requests", mode="before")
    @classmethod
    def _drop_malformed_identity_requests(cls, v):
        """One malformed record request must not fail the whole GRAPH_REQUEST: it is
        dropped (with a notice), the rest stand."""
        if not isinstance(v, list):
            return []
        kept = []
        for item in v:
            try:
                IdentityRecordRequest.model_validate(item)
            except ValidationError:
                print(f"[notice] GRAPH_REQUEST: dropped a malformed identity_requests entry: {item!r}")
                continue
            kept.append(item)
        return kept

    search_commands: List[Dict] = Field(default_factory=list)
    pending_from_previous_cycle: List[str] = Field(default_factory=list)
    # This session's redesign (MS §7.6): mirrors MovUpdateResult's own
    # field below — set false when another round of this same query,
    # against whatever this round's own search_commands just retrieved,
    # would still change the request. Defaults true (single pass).
    retrieval_satisfied: bool = True
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Query 3 — TACTICAL_SCENE_INTERPRETATION (MS §12.3, new this revision)
# ---------------------------------------------------------------------------

class BoardReading(BaseModel):
    """MS §9.4 front 1, MS §12.3 — space/time/symbolic answered as their
    own fields, not folded into free prose, so each is individually
    auditable (this session's redesign, directly requested: being able to
    see exactly how the model answered each question, to catch generic
    "parrot" reasoning that skips the architecture rather than follows it)."""

    summary: Optional[str] = None
    space: Optional[str] = None
    time: Optional[str] = None
    symbolic: Optional[str] = None


class HunterSceneReading(BaseModel):
    """MS §9.4 front 2 / §4.8 — ONE hunter's reading, produced by its own
    HUNTER_READING call (MS §12.3A, below): a Savanna can hold many hunters,
    and one query cannot give each the detail the reading needs.
    `ordinances_read` is KQ07, `schemas_read` is KQ08, `needs_own_mov` is
    KQ09 — whether this hunter's inner life is modeled richly enough to
    warrant a `nested_mov` (MS §6.8) it does not yet have; MOV_UPDATE (MS
    §12.6) materializes it via `nested_mov_ops`, this reading never writes
    to the MOV itself. The architecture assembles every hunter's reading
    into the Current Tactical Scene's `hunters` list."""

    vov_id_or_label: str
    relation_to_liriel: Optional[str] = None
    ordinances_read: List[Dict] = Field(default_factory=list)
    schemas_read: List[Dict] = Field(default_factory=list)
    supposed_prey: Optional[str] = None
    needs_own_mov: bool = False
    feels_about: List[str] = Field(default_factory=list)  # Rev 0007: see HunterReadingResult.feels_about
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Query 3A — HUNTER_READING (MS §12.3A, KQ07-KQ09): once per hunter
# ---------------------------------------------------------------------------

class HunterReadingResult(BaseModel):
    """MS §12.3A. Query 3A of the ProcessMotivation cycle — asked ONCE PER
    HUNTER (every element SCENE_SUBJECT_CHECK flagged `is_hunter`, plus
    Liriel always, MS §9.4): KQ07 (the Ordinances motivating THIS hunter),
    KQ08 (its Modulating Schemas) and KQ09 (whether it needs a `nested_mov`
    of its own). `vov_id_or_label` is an audit echo ONLY — motivation.py
    builds the `HunterSceneReading` with the label of the hunter it
    actually asked about, never trusting what the model wrote back."""

    query: str = "HUNTER_READING"
    cycle_id: Optional[str] = None
    vov_id_or_label: Optional[str] = None
    relation_to_liriel: Optional[str] = None
    ordinances_read: List[Dict] = Field(default_factory=list)
    schemas_read: List[Dict] = Field(default_factory=list)
    supposed_prey: Optional[str] = None
    needs_own_mov: bool = False
    # Rev 0007: the Objects (ids or labels of the scene's elements, or rows already on record) this
    # hunter has a charge ABOUT — the cause of something it feels. Each is mirrored into its nested MOV.
    feels_about: List[str] = Field(default_factory=list)
    notes: Optional[str] = None


class HunterTarget(BaseModel):
    """The ONE hunter a single HUNTER_READING call is about: `label` is its
    `vov_id` when it has a row, else the `provisional_label` SCENE_SUBJECT_
    CHECK gave it; `element` is that query's own entry for it; `vov` its row
    when one exists (focus or MainMemory); `is_liriel` marks Liriel herself,
    always read (MS §9.4 front 2)."""

    label: str
    element: "ElementEntry"
    vov: Optional[VectorObjectValence] = None
    is_liriel: bool = False
    liriel: Optional[VectorObjectValence] = None  # her own row, so `relation_to_liriel` can be read against it


class TacticalSceneInterpretationResult(BaseModel):
    """MS §12.3. Query 3 of the ProcessMotivation cycle, run only once the
    Graph of Traces exists. Produces the full Current Tactical Scene (MS
    §9.4) as a shared Artifact carried into every later query of this same
    cycle (motivation.py threads this through `Artifacts.
    current_tactical_scene`) — no later query re-derives its own partial
    version of it. This query answers the scene-level questions only —
    KQ03 (`relations_summary`: what are the relations; the actual
    WRITE_RELATION commands carrying it out are RELATIONS_UPDATE's, MS
    §12.6A) and KQ04-06 (`board`). KQ07-09 (what
    motivates each hunter) are asked once per hunter by HUNTER_READING
    (MS §12.3A, `HunterReadingResult` above), and KQ10-13 (which Feelings/Schemas anchored on an Object need
    updating) are NOT answered here: a MOV can hold dozens of Objects, and
    one query cannot give each the detail the judgment needs, so they are
    asked once per Object by ANCHOR_REVIEW (MS §12.3B, `AnchorReviewResult`
    below) right after this query."""

    query: str = "TACTICAL_SCENE_INTERPRETATION"
    cycle_id: Optional[str] = None
    board: Optional[BoardReading] = None
    # The judgment lives here (KQ03: what are the relations) — the actual
    # WRITE_RELATION commands carrying it out are RELATIONS_UPDATE's (Query 5A,
    # MS §12.6A), written once MOV_UPDATE has made every Object real.
    relations_summary: Optional[str] = None
    # NOT answered by this query: assembled by the architecture from the
    # per-hunter HUNTER_READING calls (MS §12.3A) that run right after it, so
    # that every later query reads the complete Current Tactical Scene.
    hunters: List[HunterSceneReading] = Field(default_factory=list)
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Query 3B — ANCHOR_REVIEW (MS §12.3B, KQ10-13): once per Object
# ---------------------------------------------------------------------------

class FeelingChange(BaseModel):
    """One Feelings-axis change on the single Object row an ANCHOR_REVIEW is
    about. `v` is a word (MS §3.5); `"neutral"` releases a charge that was
    recorded on the wrong Object (MS §3.1: the Object must be the cause) or
    no longer holds."""

    axis: str
    v: str
    c: Optional[int] = None
    reason: Optional[str] = None


class SchemaChange(BaseModel):
    """One Modulating-Schema change on the single (agent) Object row an
    ANCHOR_REVIEW is about — Schemas describe a PLAYER (MS §5), so this list
    stays empty for any row that is not an agent."""

    schema_name: str
    v: str
    c: Optional[int] = None
    reason: Optional[str] = None


_NO_EVIDENCE_WORDS = {"", "nothing", "none", "nada", "nenhum", "nenhuma", "n/a", "-"}  # what an evidence entry says when it has none


class ReviewEvidence(BaseModel):
    """Rev 0007 M. One piece of evidence an ANCHOR_REVIEW rests its changes on: the words of the report (or what in
    the scene) that bear on the row, and the Feelings changes THOSE words support."""

    words: str = ""
    who_feels_it: Optional[str] = None  # Rev 0007 U: whose feeling/state the words describe — the owner's, or another person's
    feelings_changes: List[FeelingChange] = Field(default_factory=list)


class AnchorReviewResult(BaseModel):
    """MS §12.3B. Query 3B of the ProcessMotivation cycle — asked ONCE PER
    OBJECT in Liriel's MOV and once per Object in every already-
    materialized nested MOV (KQ10-KQ13, and identically KQ10B-KQ13B when the
    matter is a continuing one: those variants are not scoped to what is
    new). `feelings_changes` answers KQ10 (Liriel's MOV) / KQ11 (a nested
    MOV): which valences the Feelings anchored on THIS Object should be
    updated to, `[]` meaning "reviewed, nothing changes". `schemas_changes`
    answers KQ12 / KQ13 the same way for Modulating Schemas.
    `missing_cause` is set when the true cause of a valence the owner
    experiences is an Object not on record at all — MOV_UPDATE (MS §12.6)
    creates it. `mov_id`/`vov_id`/`owner_vov_id`/`cycle_id` are audit
    echoes ONLY: motivation.py overwrites them with the target it actually
    asked about, never trusting what the model wrote back."""

    query: str = "ANCHOR_REVIEW"
    cycle_id: Optional[str] = None
    mov_id: Optional[str] = None
    vov_id: Optional[str] = None
    owner_vov_id: Optional[str] = None
    # Rev 0007 AV: answered FIRST -- does THIS report name, describe or bring about THIS Object? "no" = the row is untouched by it.
    touches_this_row: Optional[str] = None
    report_says: Optional[str] = None  # Rev 0007 J/M: the evidence words joined, or "nothing" — derived from `evidence`
    evidence: Optional[List[ReviewEvidence]] = None  # Rev 0007 M: the changes live inside the evidence that supports them
    feelings_changes: List[FeelingChange] = Field(default_factory=list)
    schemas_changes: List[SchemaChange] = Field(default_factory=list)
    missing_cause: Optional[str] = None
    born: bool = False  # Rev 0007: audit echo — this review was of a row created in the same cycle
    notes: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _changes_live_inside_their_evidence(cls, data):
        """Rev 0007 M. When the answer carries `evidence`, the Feelings changes are exactly those written inside its
        entries (a change with no entry to stand in does not exist); a stray top-level `feelings_changes` beside it is
        not read. Pure reshaping of the model's own answer. An answer WITHOUT `evidence` (the flat shape older
        logs and tests use) passes through unchanged.

        Rev 0007 AV: a model that answered `touches_this_row` with a "no" has declared that the report does not touch this row: no Feeling
        or Schema change is carried out (a missing cause it names is another Object's business and stays)."""
        if isinstance(data, dict):
            first = str(data.get("touches_this_row") or "").lower().replace("—", " ").replace("-", " ").replace(",", " ").replace(".", " ").split()
            if first and first[0] in ("no", "não", "nao", "false"):
                out = dict(data)
                out.update(evidence=[], feelings_changes=[], schemas_changes=[], report_says="nothing")
                return out
        if not isinstance(data, dict) or not isinstance(data.get("evidence"), list):
            return data
        # An entry whose `words` is empty or the protocol's own "no evidence" token (the model sometimes writes "nothing" there and still lists
        # changes — QA wave 2, step 1: six releases of Liriel's fear of an unrelated pregnancy, reason "the report is about something else")
        # declares that it has no evidence; its changes are not carried out. This reads the model's own declaration, not the message.
        entries = [
            {"words": str(e.get("words") or "").strip(),
             "who_feels_it": str(e["who_feels_it"]).strip() if e.get("who_feels_it") else None,
             "feelings_changes": e["feelings_changes"] if isinstance(e.get("feelings_changes"), list) else []}
            for e in data["evidence"] if isinstance(e, dict)
        ]
        entries = [e for e in entries if e["words"].lower().strip(" .") not in _NO_EVIDENCE_WORDS]
        flat = [c for e in entries for c in e["feelings_changes"]]
        words = [e["words"] for e in entries if e["words"]]
        out = dict(data)
        out["evidence"] = entries
        out["feelings_changes"] = flat
        out["report_says"] = " | ".join(words) if words else "nothing"
        return out


class SafetyScreenResult(BaseModel):
    """Rev 0007 AR. SAFETY_SCREEN: a fresh read of the message ALONE (no memory, no household) — does it suggest that someone's life or safety is at risk?
    `at_risk` is "none" or one of self | other | health_decision | danger_to_others (free text tolerated); `what` quotes the writer's own words."""

    query: str = "SAFETY_SCREEN"
    cycle_id: Optional[str] = None
    says: Optional[str] = None  # Rev 0007 AS: what the writer says and asks in THIS message, in their own matter (nothing from memory)
    harm_described: Optional[str] = None  # Rev 0007 AY: the harm to a person's body or life the message describes, read back before `at_risk`
    at_risk: str = "none"
    what: Optional[str] = None
    notes: Optional[str] = None

    @property
    def found(self) -> bool:
        return (self.at_risk or "none").strip().lower() not in ("", "none", "no", "nothing", "nada", "nenhum", "false")


class AnchorTarget(BaseModel):
    """The ONE Object row a single ANCHOR_REVIEW call is about (MS §12.3B):
    `vov` is the row under review, `mov_id` the MOV it sits in, `owner` whose
    MOV that is - Liriel's own row for her top-level MOV (KQ10/KQ12), the
    hunter row whose `nested_mov` points at `mov_id` for a nested one
    (KQ11/KQ13, `is_nested=True`)."""

    mov_id: str
    vov: VectorObjectValence
    owner: Optional[VectorObjectValence] = None
    is_nested: bool = False
    # Rev 0007: True when the row was CREATED this very cycle (no earlier review could see it): the
    # review then makes the first judgment of what, if anything, its owner experiences because of it.
    born: bool = False


# ---------------------------------------------------------------------------
# Query 4 — MAINMEMORY_FILING (MS §12.4, new this revision)
# ---------------------------------------------------------------------------

class MainMemoryFilingEntry(BaseModel):
    vov_id: str
    reason: Optional[str] = None


class MainMemoryFilingResult(BaseModel):
    """MS §12.4. The one query with standing authority to move an Object's
    MOV<->MainMemory membership this revision (MS §14 invariant 24) — both
    directions (`archive`/`restore`) are the same kind of decision (does
    this Object belong in active focus right now), just opposite ways."""

    query: str = "MAINMEMORY_FILING"
    cycle_id: Optional[str] = None
    archive: List[MainMemoryFilingEntry] = Field(default_factory=list)
    restore: List[MainMemoryFilingEntry] = Field(default_factory=list)
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Query 5 — MOV_UPDATE (MS §12.6; this revision narrows the old
# MOV_MAINMEMORY_UPDATE: retrospective/delta is removed entirely — deferred
# to ProcessIntrospection, MS §10.8/§11/§17 point 7 — and ARCHIVE_VOV/
# RESTORE_VOV move to MAINMEMORY_FILING above)
# ---------------------------------------------------------------------------

MovOpName = Literal["UPSERT_VOV", "PATCH_VOV", "SET_PRIORITY", "SPLIT_VOV"]


class MovOp(BaseModel):
    op: MovOpName
    # `vov`/`into` are raw dicts, not VectorObjectValence, on purpose: a
    # model sometimes sends UPSERT_VOV/SPLIT_VOV with only a few changed
    # fields (e.g. just vov_id) instead of switching to PATCH_VOV — seen
    # from Claude Sonnet 5. Validating strictly here would fail the *whole*
    # BestPreyGuessResult/MovMainMemoryUpdateResult over one malformed op.
    # motivation.py's _coerce_vov merges these onto the existing row (like
    # a patch) when they're incomplete, and only fully validates them when
    # there's no existing row to merge onto (a genuinely new object).
    vov: Optional[Dict] = None                          # UPSERT_VOV
    vov_id: Optional[str] = None                        # PATCH_VOV / SET_PRIORITY / SPLIT_VOV
    patch: Optional[Dict] = None                         # PATCH_VOV (partial field merge)
    priority: Optional[int] = None                       # SET_PRIORITY
    into: Optional[List[Dict]] = None                     # SPLIT_VOV
    reason: Optional[str] = None

    @field_validator("op", mode="before")
    @classmethod
    def _normalize_op(cls, v):
        """A model reasoning about an ordinary mov_op that changes a VOV's
        priority sometimes reaches for "REPRIORITIZE" instead of MovOpName's
        own "SET_PRIORITY" — seen for real from the 12B Gemma model,
        carrying the exact vov_id/priority shape SET_PRIORITY already
        expects. Remapping the name is enough; motivation.py's existing
        SET_PRIORITY handler needs no change. Also catches ARCHIVE_VOV/
        RESTORE_VOV, retired from this vocabulary this revision (MOV_UPDATE
        no longer archives/restores, MS §12.4/§14 invariant 24) — a model
        still reaching for the old name here fails loudly rather than
        silently, by being rejected as unrecognized downstream, same as any
        other unsupported op; this bare rename-on-sight only covers the
        one case Gemma was actually observed to confuse."""
        if isinstance(v, str) and v.strip().upper() == "REPRIORITIZE":
            return "SET_PRIORITY"
        return v


NestedMovOpName = Literal["CREATE_NESTED_MOV", "PATCH_NESTED_VOV", "ARCHIVE_NESTED_MOV"]


class NestedMovOp(BaseModel):
    """MS §12.3 nested_mov_ops, MS §6.8 (specular recursion as a data
    structure). `rows` is a list of raw dicts, not VectorObjectValence, for
    the same reason MovOp.vov/into are (motivation.py's _coerce_vov merges
    a partial row onto an existing one when there is one, and only fully
    validates a genuinely new row)."""

    op: NestedMovOpName
    mov_id: str                                          # the nested MOV's own id, e.g. "MOV_0002"
    owner_vov_id: Optional[str] = None                    # CREATE_NESTED_MOV: whose focus this models
    depth: Optional[int] = None                           # CREATE_NESTED_MOV: mirror-nesting depth (MS §6.8 rule d)
    rows: Optional[List[Dict]] = None                     # CREATE_NESTED_MOV: initial/additional rows (§12.2 each)
    vov_id: Optional[str] = None                          # PATCH_NESTED_VOV: which row inside mov_id
    patch: Optional[Dict] = None                          # PATCH_NESTED_VOV
    reason: Optional[str] = None

    @field_validator("op", mode="before")
    @classmethod
    def _normalize_op(cls, v):
        """Adding one more row to an ALREADY-materialized nested MOV is
        still CREATE_NESTED_MOV — motivation.py's handler (db.ensure_mov +
        one _upsert_nested_row per entry in `rows`) is idempotent against
        an existing mov_id, so a fresh row list is exactly what it wants.
        Seen for real: Claude Haiku 4.5 named this case "CREATE_NESTED_VOV"
        instead (reasonably, since it's really the VOV that's new, not the
        MOV) — a strict Literal has no room for that, and this field sits
        outside the patch/vov leniency _coerce_patch/_coerce_vov give
        top-level mov_ops, so one such op used to fail the whole
        MOV_MAINMEMORY_UPDATE/BEST_PREY_GUESS result outright."""
        if isinstance(v, str) and v.strip().upper() == "CREATE_NESTED_VOV":
            return "CREATE_NESTED_MOV"
        return v

    @model_validator(mode="before")
    @classmethod
    def _singular_row_into_rows(cls, data):
        """Same "CREATE_NESTED_VOV" moment (see _normalize_op) also carried
        its one new row under a singular `vov` key instead of `rows` (a
        one-item list) — unlike `op`, unknown keys are just dropped by
        pydantic's default `extra` policy, not rejected, so this one would
        have passed validation clean and then silently created nothing at
        all (motivation.py's CREATE_NESTED_MOV handler only ever reads
        `rows`). Fold a stray `vov`/`row` singular into `rows` before
        validation, same idea as MovOp reusing UPSERT_VOV's `vov` key."""
        if not isinstance(data, dict) or data.get("rows"):
            return data
        for singular_key in ("vov", "row"):
            single = data.get(singular_key)
            if isinstance(single, dict):
                data = {**data, "rows": [single]}
                break
        return data


_MOV_OP_NAMES = set(get_args(MovOpName))
_NESTED_MOV_OP_NAMES = set(get_args(NestedMovOpName))


def _sort_ops_by_vocabulary(data: dict) -> dict:
    """Shared by MovUpdateResult and BestPreyGuessResult below — both put
    two distinct op vocabularies side by side at the top level: `mov_ops`
    (MovOpName) and `nested_mov_ops` (NestedMovOpName). This project has
    already hardcoded one-off fixes for the model reaching for the wrong
    WORD within the right array (MovOp._normalize_op: "REPRIORITIZE" where
    "SET_PRIORITY" was meant). This is the same family of confusion but one
    level up: confirmed for real, a whole CREATE_NESTED_MOV item — not just
    a mislabeled word — placed inside `mov_ops` instead of `nested_mov_ops`,
    which MovOp's own strict Literal correctly rejects (it doesn't even
    recognize "CREATE_NESTED_MOV" as one of its names) but fails the WHOLE
    result over one item that plainly belongs one array over.

    Rather than hardcode a remap for this one newly-observed pair, sort
    every item in EITHER list by which vocabulary its own `op` value
    actually names, before either list is validated against its own strict
    Literal. An op name is unambiguous about which kind of operation it is
    regardless of which array the model put it in — this closes every
    current and future instance of this same confusion in one general pass.
    Anything neither vocabulary names (a relation write, say) is left in
    `mov_ops`, where MovOp's strict Literal rejects it loudly — relations
    are RELATIONS_UPDATE's own query (MS §12.6A), not this array's."""
    if not isinstance(data, dict):
        return data
    mov_ops, nested_ops = data.get("mov_ops"), data.get("nested_mov_ops")
    if not isinstance(mov_ops, list) and not isinstance(nested_ops, list):
        return data
    sorted_mov, sorted_nested = [], []
    for item in (mov_ops or []):
        op = item.get("op") if isinstance(item, dict) else None
        (sorted_nested if op in _NESTED_MOV_OP_NAMES else sorted_mov).append(item)
    for item in (nested_ops or []):
        op = item.get("op") if isinstance(item, dict) else None
        (sorted_mov if op in _MOV_OP_NAMES else sorted_nested).append(item)
    return {**data, "mov_ops": sorted_mov, "nested_mov_ops": sorted_nested}


class SoftenChargeEntry(BaseModel):
    vov_ids: List[str] = Field(default_factory=list)
    reason: Optional[str] = None


class MovUpdateResult(BaseModel):
    """MS §12.6. Query 5 of the ProcessMotivation cycle — Objects only: what
    enters the MOV, what is patched or split, which nested MOVs are
    created. This revision's redesign narrows the old MOV_MAINMEMORY_UPDATE:
    `retrospective` is removed entirely (MS §10.8/§11/§17 point 7 — deferred
    to ProcessIntrospection, not yet built), ARCHIVE_VOV/RESTORE_VOV move out
    of `mov_ops` to MAINMEMORY_FILING (MS §12.4), and the typed relations
    (`write_relations`, `soften_charge`) move to RELATIONS_UPDATE (Query 5A,
    MS §12.6A) — a query of their own, run once these Objects exist with
    their real ids, so a relation never has to name an Object that has not
    been minted yet. `mov_ops`/`nested_mov_ops` sit at the top level
    directly, not wrapped in a `prospective` block — that wrapper's only
    reason to exist was contrasting with `retrospective`, which no longer
    exists here."""

    query: str = "MOV_UPDATE"
    cycle_id: Optional[str] = None
    mov_ops: List[MovOp] = Field(default_factory=list)
    nested_mov_ops: List[NestedMovOp] = Field(default_factory=list)
    focus_size_after: Optional[int] = None
    notes: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sort_ops(cls, data):
        return _sort_ops_by_vocabulary(data)


# ---------------------------------------------------------------------------
# Query 5A — RELATIONS_UPDATE (MS §12.6A)
# ---------------------------------------------------------------------------

class RelationsUpdateResult(BaseModel):
    """MS §12.6A. Query 5A of the ProcessMotivation cycle — the typed
    relations (`mov_relations`, MS §8) between Objects, written once
    MOV_UPDATE has made every Object of this cycle real: every id the model
    sees in the MOV is an id that exists, so there is no forward reference
    to resolve. `write_relations` entries are raw dicts (`from`, `to`,
    `kind`, `propositional`, `affective`, `confidence`) — `from` is a Python
    keyword, and motivation.py's _apply_write_relations validates each one
    against the database (both ends must exist, MS §6.10/§8.3's closed
    kinds) before writing. `soften_charge` is the relation-side counterpart
    of time passing (MS §7.2): the bond is kept, its charge eased."""

    query: str = "RELATIONS_UPDATE"
    cycle_id: Optional[str] = None
    write_relations: List[Dict] = Field(default_factory=list)
    soften_charge: List[SoftenChargeEntry] = Field(default_factory=list)
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Query 6A — IDENTITY_UPDATE (MS §12.7A, Rev 0006)
# ---------------------------------------------------------------------------

class IdentityLedgerChange(BaseModel):
    """One thing that differs between the committed state and this cycle's draft —
    bookkeeping the architecture computes (a before/after diff), never a judgment.
    `ref` is minted per target ("c1", "c2", ...); the model points at it, so it never
    has to copy a value or an id."""

    ref: str
    field: str
    before: Optional[str] = None
    after: Optional[str] = None


class IdentityTarget(BaseModel):
    """What IDENTITY_UPDATE is asked about, ONE target per call: a principal Object
    that already existed before this cycle and changed during it, or one relation
    that did. `current` is the Object's row (kind "object") or the relation as a
    dict (kind "relation"); `prior_records` are the records already on file for it,
    newest first and bounded, so the model can tell a re-reading from a new
    information and can mark a correction."""

    kind: str                                   # "object" | "relation"
    target_id: str                              # vov_id, or the relation's id
    label: str                                  # shown to the model
    nature: Optional[str] = None
    current_object: Optional[VectorObjectValence] = None
    current_relation: Optional[Dict] = None
    changes: List[IdentityLedgerChange] = Field(default_factory=list)
    prior_records: List[Dict] = Field(default_factory=list)


# The unambiguous near-misses of the three change kinds (a spelling of the same word,
# not a judgment): anything else unrecognized falls back to "added".
_CHANGE_KIND_SYNONYMS = {
    "correction": "corrected", "correct": "corrected",
    "add": "added", "addition": "added", "new": "added", "new_information": "added",
    "world_changed": "world_change", "changed": "world_change", "change": "world_change",
}


class IdentityRecordProposal(BaseModel):
    """One record the model proposes for one ledger change (MS §12.7A)."""

    change_ref: str
    change_kind: str = "added"
    attribute: Optional[str] = None
    information: str
    source: Optional[str] = None
    obtained_via: Optional[str] = None
    reliability: Optional[int] = None
    occurred_at: Optional[str] = None
    context: Optional[str] = None
    supersedes: Optional[str] = None

    @field_validator("change_kind", mode="before")
    @classmethod
    def _normalize_change_kind(cls, v):
        """Closed vocabulary (it drives filtering), but a stray word falls back to the
        neutral "added" rather than failing the whole query."""
        if isinstance(v, str):
            lowered = v.strip().lower().replace("-", "_").replace(" ", "_")
            lowered = _CHANGE_KIND_SYNONYMS.get(lowered, lowered)
            return lowered if lowered in IDENTITY_CHANGE_KINDS else "added"
        return "added"

    @field_validator("obtained_via", mode="before")
    @classmethod
    def _normalize_obtained_via(cls, v):
        if isinstance(v, str):
            lowered = v.strip().lower()
            return lowered if lowered in IDENTITY_OBTAINED_VIA else None
        return None

    @field_validator("reliability", mode="before")
    @classmethod
    def _clamp_reliability(cls, v):
        return _clamp_confidence_value(v)


class IdentityUpdateResult(BaseModel):
    """MS §12.7A. Query 6A — which of the changes the architecture found in ONE
    target are worth a record, and what each record says. `records: []` is the
    right answer for a mere re-reading with nothing new: the judgment of whether
    something was learned is the model's, the record's bookkeeping is the
    architecture's."""

    query: str = "IDENTITY_UPDATE"
    cycle_id: Optional[str] = None
    target_id: Optional[str] = None             # echoed; ignored — the target actually asked is used
    # Rev 0007 AO: answered FIRST — what Liriel knows now about this target that she did not know before this report ("nothing new" when only a
    # re-wording, a number moved by her appraisal, or the same fact again). When it says nothing new the architecture records nothing.
    learned: Optional[str] = None
    records: List[IdentityRecordProposal] = Field(default_factory=list)
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _nothing_learned_means_no_records(self):
        said = (self.learned or "").lower().strip(" .")
        if said and self.records and (said in _NO_EVIDENCE_WORDS or said.startswith(("nothing new", "nothing learned", "no new", "nada de novo", "nada novo"))):
            self.records = []   # only a DECLARED "nothing new"; an answer without `learned` (older logs, mocks) keeps its records
        return self

    @field_validator("records", mode="before")
    @classmethod
    def _drop_malformed_records(cls, v):
        """A proposal without a ref or without its information is no record; dropping it
        (with a notice) leaves the others intact instead of failing the query — the
        change it pointed at simply stays unrecorded, visible in the cycle log."""
        if not isinstance(v, list):
            return []
        kept = []
        for item in v:
            try:
                IdentityRecordProposal.model_validate(item)
            except ValidationError:
                print(f"[notice] IDENTITY_UPDATE: dropped a malformed record proposal: {item!r}")
                continue
            kept.append(item)
        return kept


# ---------------------------------------------------------------------------
# Query 6 — BEST_PREY_GUESS (MS §12.7)
# ---------------------------------------------------------------------------
# Hunter/CurrentTacticalScene used to live here, reconstructed independently
# by this query. This revision replaces both with HunterSceneReading/
# TacticalSceneInterpretationResult above — the Current Tactical Scene is
# now a shared Artifact built once (Query 3) and threaded through every
# later query, not rebuilt piecemeal by whichever query happens to need it.


class HandoffToProcessCommandControl(BaseModel):
    """MS §12.5. What ProcessMotivation hands ProcessCommandControl. In
    Phase 1, ProcessCommandControl is not a separate process/call — see
    motivation.py's third step, which turns this handoff directly into
    Liriel's chat reply (an embodiment detail the MetaScheme leaves open,
    MS §9.3)."""

    objective_summary: Optional[str] = None
    why_now: Optional[str] = None
    expected_gains_summary: Optional[str] = None
    information_needed: List[str] = Field(default_factory=list)
    # Rev 0007 X: what the MOV / graph / records hold about the people or matters the interlocutor asks about or the reply needs
    # (one short statement each), plus "not recorded: ..." for what Liriel does not know. The reply says exactly that.
    recorded_facts: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    success_criteria: List[str] = Field(default_factory=list)
    failure_criteria: List[str] = Field(default_factory=list)
    report_back: List[str] = Field(default_factory=list)
    # Phase-1 embodiment detail, not an MS field: which channel to conduct
    # the reply through (MS §11's "conducting the action in the world" —
    # ProcessCommandControl's call, not ProcessMotivation's). Set only when
    # the user's message explicitly asked for a specific channel (e.g. "me
    # manda isso em áudio" while typing, or "responde por escrito" while
    # speaking); null means no explicit ask — the front end mirrors
    # whatever channel the user's own message came in on.
    preferred_output_modality: Optional[Literal["text", "voice"]] = None


class BestPreyGuessResult(BaseModel):
    """MS §12.7. Query 6 — the central act of the cycle, and the last
    query: nothing any query decided this cycle is durable until this one
    concludes (MS §11.1). No longer carries its own `current_tactical_scene`
    (dropped this revision — it's a shared Artifact from Query 3 now,
    `Artifacts.current_tactical_scene`, not independently reconstructed here)."""

    query: str = "BEST_PREY_GUESS"
    cycle_id: Optional[str] = None
    # Rev 0007 N: what the INTERLOCUTOR brought in this message, in their own matter -- answered FIRST, before the Guess.
    interlocutor_brought: Optional[str] = None
    # Rev 0007 S: what Liriel's own Character/Personality Schemas forbid or require of a move in THIS matter -- answered
    # BEFORE the Guess, which is then elected inside it.
    character_says: Optional[str] = None
    # Rev 0007 AJ: what the interlocutor asked Liriel to promise, keep or do — and what she can honestly give instead. Answered BEFORE the Guess.
    asked_of_liriel: Optional[str] = None
    # Rev 0007 AQ: of what Liriel holds about the matter the interlocutor asks about, what THIS interlocutor may be told (or "nothing of it").
    may_be_told: Optional[str] = None
    # Rev 0007 AT: whose matter the Guess is about (a name; not whom the reply goes to), and whether the interlocutor is that person or acts on that
    # matter with Liriel in THIS message ("yes"/"no") -- answered BEFORE the Guess.
    guess_matter_of: Optional[str] = None
    interlocutor_is_party: Optional[str] = None
    best_prey_guess: VectorObjectValence
    # Rev 0007: the exact vov_id of a STANDING, active Objective this Guess merely continues (the
    # model's own judgment that it is the same pursuit). The architecture then updates that row in
    # place instead of minting a duplicate beside it, as it did every cycle before.
    continues_objective: Optional[str] = None
    accompanying_objectives: List[VectorObjectValence] = Field(default_factory=list)
    handoff_to_processcommandcontrol: Optional[HandoffToProcessCommandControl] = None
    mov_ops: List[MovOp] = Field(default_factory=list)
    # This session's redesign: this query is asked to audit every `feelings`
    # entry across the MOV for §6.11 ownership (Liriel's own charge vs. a
    # copy of what the Object itself feels) — MOV_UPDATE already had
    # nested_mov_ops for exactly this move (MS §6.8's mirror), but this
    # query had no way to carry out a correction it found, only to flag or
    # ignore it. Same shape, same mechanism, same place a correction belongs.
    nested_mov_ops: List[NestedMovOp] = Field(default_factory=list)
    notes: Optional[str] = None

    @property
    def declares_not_a_party(self) -> bool:
        """Rev 0007 AT. Reads the model's OWN declaration (it judges nothing): the first word of `interlocutor_is_party` is a "no"."""
        words = (self.interlocutor_is_party or "").lower().replace("—", " ").replace("-", " ").replace(",", " ").replace(".", " ").split()
        return bool(words) and words[0] in ("no", "não", "nao", "false")

    @model_validator(mode="before")
    @classmethod
    def _sort_ops(cls, data):
        return _sort_ops_by_vocabulary(data)

    @model_validator(mode="before")
    @classmethod
    def _default_objective_nature(cls, data):
        """`object_nature` is required on VectorObjectValence (every VOV
        must declare its nature, MS §6.4) — correctly so everywhere else,
        but a real crash on the 12B Gemma model: it omitted the field on
        two `accompanying_objectives` entries, which aborted the whole
        cycle's validation before motivation.py ever got a chance to run.
        That's wasted concern, not a real gap: motivation.py *already*
        force-sets object_nature="Objective" on best_prey_guess and every
        accompanying_objectives entry unconditionally, right after this
        validates (MS §14.6's "exactly one Objective at priority 1" is
        enforced the same way, not trusted from the model) — whatever the
        model does or doesn't put here for these two fields specifically
        is discarded either way. Filling the gap before validation, only
        when it's actually missing, just stops that known-irrelevant
        omission from crashing the cycle before its own override runs."""
        if isinstance(data, dict):
            bpg = data.get("best_prey_guess")
            if isinstance(bpg, dict):
                bpg.setdefault("object_nature", "Objective")
            kept = []
            for obj in data.get("accompanying_objectives") or []:
                if isinstance(obj, dict):
                    obj.setdefault("object_nature", "Objective")
                    # An accompanying Objective is an optional extra and a row with no description says nothing; the 12B left
                    # `brief_description` out of one (QA wave 4, step 9) and the whole cycle fell on a validation error.
                    # Same tolerance as one malformed axis in _coerce_patch: drop the extra, keep the cycle.
                    if not str(obj.get("brief_description") or "").strip():
                        print(f"[warning] dropped an accompanying Objective with no brief_description: {obj.get('vov_id')!r}")
                        continue
                kept.append(obj)
            if "accompanying_objectives" in data:
                data["accompanying_objectives"] = kept
        return data


# ---------------------------------------------------------------------------
# Phase-1-only: turning the handoff into an actual chat reply.
# Not part of the MetaScheme's §12 contracts (no formal contract exists for
# "ProcessCommandControl composes a reply" — MS §9.3 leaves embodiment open).
# ---------------------------------------------------------------------------

class ChatReplyResult(BaseModel):
    response_text: str
