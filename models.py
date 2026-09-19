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

from pydantic import BaseModel, Field, field_validator, model_validator

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
ObjectNature = str  # MS §6.4, §16: PCI | Person | Objective | Situation | Thing | Idea | Event | Memory | Group | Animal | Entity | Attribute | Self-Process
ValenceRegime = str  # MS §6.4, §16: State | Delta

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


# ---------------------------------------------------------------------------
# Query 1 — GRAPH_REQUEST (MS §12.1)
# ---------------------------------------------------------------------------

class HunterDraft(BaseModel):
    vov_id: str
    engaged: bool = True
    ordinances_read: List[Dict] = Field(default_factory=list)
    supposed_prey: Optional[str] = None
    note: Optional[str] = None


class CurrentTacticalSceneDraft(BaseModel):
    board: Optional[str] = None
    hunters: List[HunterDraft] = Field(default_factory=list)
    relations: str = "pending"  # MS §12.1: literally "pending" until the graph comes back


class GraphRequestItem(BaseModel):
    focus_objects: List[str] = Field(default_factory=list)
    relation_kinds: List[str] = Field(default_factory=list)
    include_archive: bool = True
    depth: int = 1
    reason: Optional[str] = None
    # Phase 1 addition — MS §0.6 permits a QUERY to narrow this document,
    # and an optional field a model can simply not set is not a narrowing
    # that could contradict anything: a §12.1-only model still produces a
    # valid request. Set when the user explicitly insists Liriel make an
    # effort to remember something specific; graph_service.py raises the
    # MainMemory node threshold for this request only when set. There's no
    # state to revert afterward — the next cycle just doesn't set it, and
    # the normal threshold applies on its own.
    deep_recall_requested: bool = False


class GraphRequestResult(BaseModel):
    """MS §12.1. Query 1 of the ProcessMotivation cycle — run before the
    Graph of Traces exists (MS §8.2), so it works from the MOV and
    ScenarioData alone to decide whose relations are worth surveying."""

    query: str = "GRAPH_REQUEST"
    cycle_id: Optional[str] = None
    current_tactical_scene_draft: Optional[CurrentTacticalSceneDraft] = None
    requests: List[GraphRequestItem] = Field(default_factory=list)
    pending_from_previous_cycle: List[str] = Field(default_factory=list)
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Query 2 — MOV_MAINMEMORY_UPDATE (MS §12.3)
# ---------------------------------------------------------------------------

RetrospectiveAction = Literal[
    "SET_DELTA_REPORT", "KEEP_PENDING", "KEEP_PENDING_URGENT",
    "ARCHIVE", "REPRIORITIZE", "ABANDON",
]


class RetrospectiveEntry(BaseModel):
    """MS §12.3. `reason` is not an optional audit note: MS §10.8 makes it
    the Objective's own interim report, written back onto its row
    (motivation.py's _apply_retrospective persists it to relevant_remarks)
    every cycle a still-open Objective gets reviewed — "no feedback yet"
    included, whatever the action. Optional here only so a model that
    omits it doesn't fail the whole MOV_MAINMEMORY_UPDATE payload; the
    prompt itself asks for it unconditionally."""

    vov_id: str
    outcome_known: bool = False
    action: RetrospectiveAction
    delta_report: Optional[DeltaReport] = None
    new_priority: Optional[int] = None
    reason: Optional[str] = None

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, v):
        """RetrospectiveAction and MovOpName (§12.3's own two vocabularies,
        both keyed by vov_id in the very same payload) sit close enough
        that a model reasoning about "this row is done" reaches for the
        mov_ops spelling instead — seen for real from the local model:
        "ARCHIVE_VOV" where only bare "ARCHIVE" is valid here, the mirror
        image of MovOp._normalize_op's already-known "REPRIORITIZE" mixup
        (that one goes the other way: RetrospectiveAction's own word
        appearing where MovOpName's "SET_PRIORITY" was expected). Both
        directions get normalized rather than just the one already caught,
        since it's the same confusion regardless of which side it lands on."""
        if isinstance(v, str):
            mapped = {"ARCHIVE_VOV": "ARCHIVE", "SET_PRIORITY": "REPRIORITIZE"}.get(v.strip().upper())
            if mapped:
                return mapped
        return v


MovOpName = Literal["UPSERT_VOV", "PATCH_VOV", "SET_PRIORITY", "ARCHIVE_VOV", "RESTORE_VOV", "SPLIT_VOV"]


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
    vov_id: Optional[str] = None                        # PATCH_VOV / SET_PRIORITY / ARCHIVE_VOV / RESTORE_VOV / SPLIT_VOV
    patch: Optional[Dict] = None                         # PATCH_VOV (partial field merge)
    priority: Optional[int] = None                       # SET_PRIORITY
    into: Optional[List[Dict]] = None                     # SPLIT_VOV
    reason: Optional[str] = None

    @field_validator("op", mode="before")
    @classmethod
    def _normalize_op(cls, v):
        """RetrospectiveAction (this same query's `retrospective` list, MS
        §12.3) has its own "REPRIORITIZE" for an Objective's outcome
        review. A model reasoning about an ordinary mov_op that also
        happens to change a VOV's priority sometimes reaches for that
        same word instead of MovOpName's own "SET_PRIORITY" — seen for
        real from the 12B Gemma model, carrying the exact vov_id/priority
        shape SET_PRIORITY already expects. Remapping the name is enough;
        motivation.py's existing SET_PRIORITY handler needs no change."""
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


class Prospective(BaseModel):
    mov_ops: List[MovOp] = Field(default_factory=list)
    nested_mov_ops: List[NestedMovOp] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _sort_ops_by_vocabulary(cls, data):
        """MS §12.3's `prospective` block puts two distinct op vocabularies
        side by side in the same JSON object: mov_ops (MovOpName) and
        nested_mov_ops (NestedMovOpName) — plus a third, RetrospectiveAction,
        elsewhere in the same query. This project has already hardcoded two
        one-off fixes for the model reaching for the wrong WORD within the
        right array (MovOp._normalize_op: "REPRIORITIZE" where
        "SET_PRIORITY" was meant; RetrospectiveEntry._normalize_action:
        "ARCHIVE_VOV" where "ARCHIVE" was meant). This is the same family
        of confusion but one level up: confirmed for real, a whole
        CREATE_NESTED_MOV item — not just a mislabeled word — placed
        inside `mov_ops` instead of `nested_mov_ops`, which MovOp's own
        strict Literal correctly rejects (it doesn't even recognize
        "CREATE_NESTED_MOV" as one of its six names) but fails the WHOLE
        MOV_MAINMEMORY_UPDATE payload over one item that plainly belongs
        one array over.

        Rather than hardcode a remap for this one newly-observed pair (the
        two vocabularies have nine names between them, so there are many
        more possible pairings than anyone has actually seen yet), sort
        every item in EITHER list by which vocabulary its own `op` value
        actually names, before either list is validated against its own
        strict Literal. An op name is unambiguous about which kind of
        operation it is regardless of which array the model put it in —
        this closes every current and future instance of this same
        confusion in one general pass, not just the one seen so far."""
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


class MovMainMemoryUpdateResult(BaseModel):
    """MS §12.3. Query 2 of the ProcessMotivation cycle."""

    query: str = "MOV_MAINMEMORY_UPDATE"
    cycle_id: Optional[str] = None
    retrospective: List[RetrospectiveEntry] = Field(default_factory=list)
    prospective: Prospective = Field(default_factory=Prospective)
    # MainMemory commands (MS §12.4). Only RETRIEVE/ARCHIVE are executed
    # (as archived_at flips); SEARCH/WRITE_RELATION/SOFTEN_CHARGE need the
    # Graph of Traces or a real index Phase 1 doesn't have — logged, not run.
    mainmemory_commands: List[Dict] = Field(default_factory=list)
    focus_size_after: Optional[int] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Query 3 — BEST_PREY_GUESS (MS §12.5)
# ---------------------------------------------------------------------------

class Hunter(BaseModel):
    vov_id: str
    ordinances_read: List[Dict] = Field(default_factory=list)
    supposed_prey: Optional[str] = None
    # MS §16 enumerates target/obstacle/collaborator/rival/ally/bystander,
    # but doesn't cover a hunter's own relation to their own row (e.g.
    # Liriel's VOV_0000 rating itself) — kept as free text (see the note by
    # the other loosened enums above) rather than crashing on "self" & co.
    relation_to_liriel: Optional[str] = None


class CurrentTacticalScene(BaseModel):
    board: Optional[str] = None
    hunters: List[Hunter] = Field(default_factory=list)
    relations_summary: Optional[str] = None


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
    """MS §12.5. Query 3 — the central act of the cycle."""

    query: str = "BEST_PREY_GUESS"
    cycle_id: Optional[str] = None
    current_tactical_scene: Optional[CurrentTacticalScene] = None
    best_prey_guess: VectorObjectValence
    accompanying_objectives: List[VectorObjectValence] = Field(default_factory=list)
    handoff_to_processcommandcontrol: Optional[HandoffToProcessCommandControl] = None
    mov_ops: List[MovOp] = Field(default_factory=list)
    notes: Optional[str] = None

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
            for obj in data.get("accompanying_objectives") or []:
                if isinstance(obj, dict):
                    obj.setdefault("object_nature", "Objective")
        return data


# ---------------------------------------------------------------------------
# Phase-1-only: turning the handoff into an actual chat reply.
# Not part of the MetaScheme's §12 contracts (no formal contract exists for
# "ProcessCommandControl composes a reply" — MS §9.3 leaves embodiment open).
# ---------------------------------------------------------------------------

class ChatReplyResult(BaseModel):
    response_text: str
