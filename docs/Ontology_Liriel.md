# Ontology of Objects and their relations — Liriel

**Technical documentation · accompanies MetaScheme Rev 0006** (`docs/MetaScheme_Liriel_Rev0006.md`, §6.13, §8.8–§8.11, §12.2, §12.7A)

This document has **two parts that do not mix**:

* **Part A — Established concepts.** What was defined by whoever specifies Liriel. Nothing here is an implementation decision.
* **Part B — Implementation decisions.** What had to be chosen to realize Part A in the existing code, each one marked **[D1…]** with its reason; plus what already existed, what changed, how it was verified and what remains open.

The MetaScheme (resident in every call to the model) carries only the operational rules. The long explanation lives here, because this file is never sent to the model.

---

# Part A — Established concepts

## A1. Purpose and scope

The ontology organizes the **MOV** (Matrix Objects Valence) and the **Main Memory** graph, mainly in the service of **ProcessMotivation**. The core of ProcessMotivation is *affective and motivational evaluation*: what has value, which objectives to pursue and what the **Best-Prey Guess** is for turning valences positive. Execution and the operational solving of problems belong to **ProcessCommandControl**.

Every interpretation and every cognitive judgment of this dynamic belongs to the **AI model**. The code provides structures, persistence, queries, limits and validations; **it does not replace the model's judgment with parallel motivational heuristics**. The ontology must make it possible to organize, update and retrieve relevant experiences **without sending the whole Main Memory to the model**.

## A2. Objects and their VOVs

Every object has its **VOV** (Vector Object Valence); the correspondence is preserved, and the VOV is **not** a semantic-search embedding. Identifying objects and drawing their boundaries can remain largely up to the model; no exhaustive classification of everything that exists is built, and other kinds of objects, concrete or abstract, remain allowed.

Five natures form a **mandatory core of representation** (they need not all appear in every cycle):

| Nature | What it is |
|---|---|
| `Objective` | A motivational objective aimed at turning valences positive. The Best-Prey Guess is the objective elected in the cycle, **not** a separate nature. |
| `ScenarioData` | Records what is happening or reaches the process as a scenario. |
| `Sentient` | A being represented as able to feel and to have a perspective of its own — a person, an animal, an imaginary character, a divine entity or another being so understood. The category does not determine its physical existence. |
| `Situation` | A relevant configuration or situation that can receive affective evaluation and motivate objectives. |
| `Identity` | Records a piece of information learned, or a change incorporated into the representation of another object, preserving its origin and the circumstances of the update. |

## A3. Subjects and clusters

A "subject" is **not** an object nature: it is the name of the grouping of `ScenarioData` objects related to one another. Each `ScenarioData` has its VOV; the subject, being a grouping, needs no VOV of its own. Other pertinent objects attach to these groupings through the structure of the graph. Clusters make it possible to tell subjects apart in the focus of attention and to retrieve their contexts later. **An object can take part in several subjects without being duplicated**, and separating the clusters must preserve the pertinent connections between them.

## A4. Categories of bonds (a list not definitively closed)

| Category | Meaning |
|---|---|
| **Subject** | Taking part in the same subject; sustains the forming of clusters. |
| **Emotional charge** | A positive or negative emotional relation, according to the perspective represented. Its description can explain the origin or the transformation of the charge; there is no separate category of "affective origin". |
| **Genealogical** | In the broad sense: kinship and conjugal bond (father, mother, brother, sister, husband, wife). |
| **Space-time** | Places, moments, encounters or happenings in the represented world. |
| **Symbolic** | A cultural, social, institutional, religious or role relation (employer–employee, pastor–congregant). It can carry great motivational importance without expressing feeling directly; it does **not** mean only metaphor. |
| **Identity** | Links an `Identity` object to the principal object whose representation it documents, making it possible to retrieve the records that explain the accumulated knowledge and its changes. |

The categories must serve concrete needs of ProcessMotivation; avoid expanding them just to produce a broader classification.

## A5. Content, multiplicity and direction of bonds

* Each bond identifies the elements involved, its category and a **short textual description** that makes its meaning clear. The text lets the model interpret nuances; the structured fields make it possible to **select information before sending it to the model**.
* Where applicable, there can be a **strength/intensity** to support filtering. A missing intensity does **not** equal zero. There is no universal scale for every category; the existing scales are kept where pertinent.
* The relation of A to B is **not necessarily the same** as that of B to A: direction and perspective are preserved, including when descriptions or intensities differ.
* Two objects can have **several simultaneous bonds**; adding or updating one does not erase the others.

## A6. Persistent identity and update of the principal object

An existing object is **reused** when a new reference corresponds to the same identity: another name, another description or another subject do not by themselves justify another principal object. But preserving identity does **not** freeze the VOV: the current representation of a persistent object can and must be modified when Liriel learns something or revises her understanding. The proposal combines:

1. a **principal object**, with a stable identity and an updated VOV;
2. **`Identity`** objects that record the information and changes incorporated;
3. **identity bonds** that link those records to the principal object.

`Identity` objects are distinct records, with their own VOVs; creating them is **not** duplicating the principal object.

## A7. `Identity` objects as records of learning and change

An `Identity` records what was learned or changed and the data needed to understand the update, as available: the principal object; the attribute or aspect affected; the information incorporated and, when there is one, the previous value or understanding; the source and the way it was obtained; the reliability; date and time of the record and, when known and pertinent, the date of the occurrence; a short description of the context or the justification. **Do not invent missing information.** Distinguish *reported* information from an *inference* of the model when relevant.

*Example:* Liriel learns that Clara has black hair. The VOV of the principal object Clara is updated; in parallel an `Identity` is created recording the learning, the source, the reliability and the date/time, linked to Clara by an identity bond. The same mechanism applies to Liriel herself and to other persistent objects, not only people.

If later information corrects earlier information, the current state can change **without erasing** the record of how the earlier understanding was reached. The history must distinguish **added information**, **corrected information** and **an actual change in the represented object**. The `Identity` documents the update; the judgment about how the information changes the current representation belongs to the model.

## A8. Possible application to changes in relations

Relations also change (description, intensity, role, condition…). Consider using the same `Identity` nature to record changes to a relation: the record must identify the **specific relation** (naming only the two objects is not enough, since they can share several bonds). Do not assume an edge can receive another edge; choose a compatible representation and document the decision, **without automatically turning every relation into a new object with a VOV**.

## A9. Preserving records, and specific rules

Updating principal objects does not authorize overwriting historical records indiscriminately. The rules of `ScenarioData` are kept: new occurrences of a subject produce new `ScenarioData` joined to the same cluster, without rewriting the earlier ones. `Identity` records preserve what was learned or considered at each update; a later correction stays recognizable **as later**. Avoid repeated records for the same update; tell new information or change apart from a mere **rereading** with no change.

## A10. Selective retrieval from Main Memory

Combine filters by objects, natures, bonds, descriptions and pertinent parameters. Examples: find Clara through her conjugal bond with Bruno; retrieve objects tied to her by a relevant emotional charge; restrict the search to a subject, event, place or period; retrieve Clara's current state; consult the `Identity` records that explain an attribute or change; investigate the change of a specific relation. Querying the principal object **does not necessarily load its whole history**: the model gets an initial view and asks for details as needed. **Strength of a bond and relevance to the query are different criteria** — a weak link can be decisive; the code offers filters and volume limits, the motivational judgment stays with the model.

## A11. Integration and requested checks

Inspect before changing; assess whether `Link_Identity_Part` already serves as the identity bond, without creating equivalent parallel structures or assuming equivalence from the name; keep the prevention of duplication and the reuse of identifiers, **distinguishing the identity of the principal object from the identity of each learning record**; compatibility and preservation of existing data; update and recording **consistent even in the face of failures**. Check mainly: reuse of the same object in several clusters; coexistence and asymmetry of bonds; update of the principal VOV with the history preserved; selective retrieval of records; the distinction between new information, correction and change in the world; preservation of `ScenarioData`; unambiguous reference to a relation; respect for context limits.

---

# Part B — Implementation

## B1. What already existed, and what was done

| Concept (Part A) | Existed already? | Where / how | What changed |
|---|---|---|---|
| Natures `Objective`, `ScenarioData`, `Sentient`, `Situation` | **Yes** | `models.ObjectNature` (free text), `CHECK` on `mov_objects` (migrations 005/006), MS §6.4/§16 | Nothing. |
| **`Identity`** nature | **No** | — | **New**: nature + `identity_record` block (jsonb) + widened `CHECK` (migr. 010). |
| Subject = grouping of `ScenarioData`; clusters | **Yes** | MS §6.10, `Link_Subject_Cluster`, `motivation._evict_stale_clusters` (does not archive a member still needed by a cluster that stays) | **Fixed** a latent bug (D13): the "backbone" walk crossed a shared member and merged two subjects. |
| Immutability of `ScenarioData` | **Yes** | `motivation._apply_mov_ops` (PATCH refused; UPSERT over an existing one creates a new id) | Same rule extended to `Identity` (D16). |
| Object reuse / no duplication | **Yes** | `mint_vov_id`, `_resolve_id`, collision by nature/archived, folding of repetitions in the same batch, `SPLIT_VOV` (confused identity), SEARCH | Kept in full. |
| **Subject** bond | **Yes** | `Link_Subject_Cluster` | Name kept. |
| **Emotional charge** bond | **Partial** | `Link_Valence_Load`, but it was the *catch-all* for any other bond (migr. 006 put everything there) | Now means emotional charge only; gains 3 sibling categories. Old data was **not** reclassified (D17). |
| **Genealogical, Space-time, Symbolic** bonds | **No** | — | **New**: `Link_Genealogical`, `Link_Space_Time`, `Link_Symbolic` (D7). |
| **Identity** bond | **Partial** | `Link_Identity_Part` = Sub-Object → `Object_Master` (MS §6.9/§7.5) | **Reused** (D5): same satellite → principal direction; what tells a record from a Sub-Object is the satellite's nature. No parallel structure. |
| Short textual description + structured fields | **Yes** | `propositional`, `affective`, `confidence`, `since_text` | Nothing. |
| Optional **strength/intensity** | **No** (`confidence` is certainty of the information, not strength) | — | **New** column `strength` 1–5, null = not stated (D11). |
| **Direction** of the bond | **Apparent, not real** | the `directed` column existed but **no `write_relation` wrote it** (always `false`) | Now actually written; A→B and B→A are separate bonds (D10). |
| **Several bonds between the same pair** | **Partial** | `unique(from,to,kind)`: only one per category; rewriting blanked the omitted fields | Key becomes `(from,to,kind,label)`; partial update (D8, D9). |
| Retrieval by bond/depth/node limit | **Yes** | `graph_service.build_graph_of_traces`, `relation_kinds`, `GRAPH_MAX_NODES`, ranking relevance→charge→recency | Gains filters (`labels`, `min_strength`, `min_charge`, `within_subject`) and a cap on bonds (D12). |
| Querying the principal without the history | **N/A** (there was no history) | — | New: records stay out of recall, traversal and search; only on explicit, bounded request (D12). |
| Consistency in the face of failures | **No** | `DraftDatabase.commit()` replayed write by write, `autocommit=True` | **Atomic commit** in one transaction (D15). |
| Graph view (Obsidian-style) | **Yes** | MindReader | Colors/legend of the 6 categories, parallel bonds, arrows, records panel (D19). |

## B2. Implementation decisions

**[D1] The records are born from a new query — `IDENTITY_UPDATE` (MS §12.7A, "Query 6A").** It runs **after** the decision and the mechanical passes, right before the commit, to see **everything** the cycle changed (`MOV_UPDATE` patches, `ANCHOR_REVIEW` updates, the decision's own patches, relations). One call **per changed target** (the same "one judgment at a time" shape as the per-hunter and per-Object calls); **no** call if nothing that already existed changed. *Alternative discarded:* asking for the records inside `MOV_UPDATE` — it is the densest query of the cycle and has already failed from too much responsibility on the local 12B.

**[D2] The ledger belongs to the code; the judgment to the model.** What differs between the **stored** state and the cycle's **draft** is bookkeeping arithmetic (like `_evict_stale_clusters`): field, `before`, `after`, copied from what the cycle is about to overwrite — never typed by the model. The model decides whether it is **learning** or a mere rereading, the kind of change, the source, the reliability. *Ledger fields of an Object:* `brief_description`, `relevant_remarks`, `perceived_age`, `male_female`, `object_type` and each Modulating Schema; *of a relation:* text, charge, strength, confidence, `since`, direction. *Out:* Feelings/priority (they already have `ANCHOR_REVIEW`), Ordinances (readings of the moment), Objective gains, `ScenarioData`, `Objective`, nested-MOV rows (mirrors), Objects born in the cycle (being born is not changing), structural bonds, and charge that merely **faded with time** (`SOFTEN_CHARGE`, already stamped in `softened_at`).

**[D3] The code never invents a record.** If the model answers `records: []` for a real change, the previous value stays only in the cycle log (open O2). An exact repetition of what is already filed (same target, field and value) is dropped; an unknown `change_ref`, a second record for the same change and a `supersedes` that does not point to a record of the same target are refused or ignored with a warning. A `reliability` outside 1–5 is clamped; a `change_kind`/`obtained_via` outside the vocabulary falls back to a neutral value (`added`/null) without failing the query. Format drift of a small model on the new contracts (a `label` written as loose text, a non-numeric `min_strength`, a malformed records request or record proposal) is normalized or dropped with a warning — the cycle never falls over it, and the unrecorded change stays visible in the log.

**[D4] A record is an `Identity` Object** (`vov_id` `Identity_<target>_<field>`, numeric suffix from the minter) with an `identity_record` block: `target_kind`, target (`vov_id` **or** relation), `field`, `attribute`, `change_kind` ∈ {`added`, `corrected`, `world_change`}, `information`, `value_before`, `value_after`, `source`, `obtained_via` ∈ {`reported`, `observed`, `inferred`}, `reliability`, `recorded_at` (the architecture's clock), `occurred_at`, `context`, `supersedes`, `cycle_id`. It is **born archived** (it is MainMemory, never focus) and is **immutable**: a correction = a new record that `supersedes` the previous one (which is why a correction stays recognizable as later).

**[D5] Identity bond = `Link_Identity_Part`, reused.** Assessed and accepted: it already was "satellite → principal", always directed (`from` = satellite, `to` = principal). A record links to the principal that way. The name was **not** taken as a guarantee of equivalence: a Sub-Object (§6.9) is a thing of the world split off a confused row; a record documents learning about **one** object. Identity recall (§7.5, "the Master + all Sub-Objects") was adjusted to **exclude records**, or else remembering a person would bring back everything ever learned about them (and restore it to the focus). The identity of the **principal object** (stable id, reused) and that of **each record** (own id, immutable) are therefore distinct.

**[D6] Change to a relation: same nature, reference by the relation's `id`.** The record carries the `relation_id` (a uuid stable across updates) **plus** a copy of the natural key `(from,to,kind,label)`, to stay readable and findable. No edge is linked to an edge, **no** object-with-VOV is created for the relation, and a relation record does **not** receive an edge (it is found by id). The model never copies a uuid: it asks for a relation by the natural key, and the architecture resolves it. A relation *born* in the cycle has no "change"; faded charge and structural bonds (`Link_Subject_Cluster`, `Link_Identity_Part`) produce no record.

**[D7] Six categories** (`kind`, closed): `Link_Subject_Cluster` (Subject), `Link_Valence_Load` (Emotional charge), `Link_Genealogical`, `Link_Space_Time`, `Link_Symbolic`, `Link_Identity_Part`. The vocabulary is strict as before; `GraphRequestItem` still drops an unknown value instead of failing the request. Default direction when `directed` is omitted: **undirected** for `Link_Subject_Cluster` and `Link_Space_Time`, **directed** for the other four; the model is instructed to state it (open O7).

**[D8] Identity of a bond = `(from, to, kind, label)`.** `label` (≤60 characters, compared ignoring case/whitespace) is the short word that tells two bonds of the same category between the same pair apart ("employer–employee" × "pastor–congregant"). `""` when the category would not apply twice. Migration 010 swaps the unique constraint; every existing row gets `label ''` and **keeps** its identity.

**[D9] Partial update.** Writing the same bond again changes **only the fields given**; omitted ≠ erased (`affective: []` is the explicit way to clear the charge). Before, `ON CONFLICT … DO UPDATE` overwrote everything with whatever came (including `null`).

**[D10] Direction and perspective.** `directed` is now written. A directed bond is **one row per direction** (A→B and B→A coexist, each with its own text/strength). An undirected bond is written **only once**, in whichever direction it is written (the existing row is found in both directions; a new one uses a fixed order). The `DraftDatabase` decides "same bond?" on what the cycle **sees** (real + draft) and reports the **real id** of the edge, not the draft's.

**[D11] Strength.** `strength` 1–5, optional, **category-relative**; for emotional charge the existing scale still applies (`affective`, in words). `min_strength` **keeps** a bond with no strength stated; `min_charge` applies only to `Link_Valence_Load` (an unstated charge is not a "relevant charge").

**[D12] Selective retrieval and limits.** (a) The graph gains `labels`, `min_strength`, `min_charge`, `within_subject`. (b) `GRAPH_MAX_EDGES` (60; 150 on a `deep_recall` request) cuts bonds — first those touching less relevant objects, then the weakest — and the graph reports `edges_omitted`. (c) Each node and each bond carries only a **summary** `identity_records` `{count, corrections, attributes, last_recorded_at}`. (d) `identity_requests` in `GRAPH_REQUEST` asks for the **records** of an object or of **one** relation, filtered by `attribute` (matched ignoring accents/case, both ways), `change_kinds`, `since`/`until` and `limit`, **always** bounded by `IDENTITY_RECORDS_MAX` (20; default 5), newest to oldest, with `total_matching`/`truncated`. (e) Records stay out of traversal, identity recall, neighborhood and search (`search_memory`). *Period* compares ISO text **by prefix** ("2024" overlaps "2024-03-01") and applies to the date of the occurrence when known, otherwise to the date of the record; it is **not** a generic graph filter (open O5).

**[D13] Backbone fix (found during verification).** `_cluster_backbone_ids` walked `Link_Subject_Cluster` freely; since an object can be in several subjects, it crossed the shared member and **merged two subjects** into one. Now it only steps from `ScenarioData` to `ScenarioData`. This is required by "the same object in several clusters".

**[D14] Placeholder bond after the typed ones.** `_ensure_relation_edges` (creates a content-less bond for what the object lists in `relevant_relations`) ran when each row was written — before `RELATIONS_UPDATE`. With several categories this would leave an empty bond parallel to the typed one (a couple with an "emotional charge" carrying no charge). Now it is **deferred** until after `RELATIONS_UPDATE`: it only fills a pair with no bond at all. And the placeholder bond **says of itself that it is untyped** (text: *listed in relevant_relations — the kind of bond and its meaning are not stated yet*), because in the real test (B6) 9 of 13 "emotional charge" bonds were empty placeholders, indistinguishable from a real emotion; typing it later is a first statement, not a change (it produces no ledger entry).

**[D15] Atomic commit.** `Database.transaction()`: on Postgres, one transaction (`autocommit` off during the commit; rollback on any failure); on the development JSON store, a snapshot of the files restored on failure. `DraftDatabase.commit()` runs entirely inside it, and `_with_reconnect` does **not** retry inside a transaction (reconnecting would replay the rest outside it). Update + record, and relation + record, are kept together or not at all.

**[D16] Protections.** No query can create, edit, split or relate an `Identity` row (UPSERT/PATCH/SPLIT/`write_relation` refused with a warning); `ANCHOR_REVIEW` and `search_memory` ignore them.

**[D17] Compatibility and old data.** Nothing is migrated "in content": old bonds keep `kind`, `directed=false` and `label ''`; a `Link_Valence_Load` that was really kinship **stays** until rewritten (reclassifying is a judgment of content, not of the architecture). An unknown `kind` written by the model still falls back to `Link_Valence_Load` with a warning (previous behavior).

**[D18] `softened_at` now survives the commit** (the replay did not carry it).

**[D19] Visualization.** MindReader: its own color for `Identity`; a legend of the 6 categories; parallel bonds between the same pair with different curvatures; an arrow when directed; a "category · label" caption; width by strength/charge; clicking a bond shows its fields; an object's panel shows the **summary** of records and a button that loads them (`/api/identity`, bounded, the model's text always escaped).

**[D20] Cost.** MetaScheme Rev 0006 is ~17.5 thousand characters (≈ +4.4 thousand tokens) larger than Rev 0005 in **every** call — the resident text was reduced to the operational part (this documentation carries the rest). `IDENTITY_UPDATE` adds one call per changed target (zero when nothing changed).

## B3. Data schema and cycle

* `mov_objects.identity_record jsonb`, `CHECK` with `'Identity'`, partial indexes by target and by relation.
* `mov_relations.label text not null default ''`, `strength smallint 1..5`, `unique(from,to,kind,label)`.
* `motivation_cycles.identity_update_result jsonb` — `{cycle_id, results: [{target, kind, changes, proposals, notes}], written: [...], skipped: [{reason}]}`.
* Cycle: … `MOV_UPDATE` (5) → `RELATIONS_UPDATE` (5A; typed bonds with `label`/`directed`/`strength`; then the placeholder bond) → `BEST_PREY_GUESS` (6) → mechanical passes → **`IDENTITY_UPDATE` (6A)** → `commit` (single transaction) → reply.
* Configuration: `GRAPH_MAX_EDGES`, `GRAPH_MAX_EDGES_BOOSTED`, `IDENTITY_RECORDS_DEFAULT_LIMIT`, `IDENTITY_RECORDS_MAX`.
* Files: `models.py`, `database.py`, `graph_service.py`, `motivation.py`, `prompts.py`, `config.py`, `.env.example`, `migrations/010_ontology.sql`, `mindreader/`, `docs/MetaScheme_Liriel_Rev0006.md`, `tests/ontology/`.

## B4. Verification

All synthetic (no real model, no network); they run from `tests/ontology/` (see the `README.md` there).

| Requested check | Script | What it proves |
|---|---|---|
| Coexistence and asymmetry of bonds | `verify_ontology_db.py`, `verify_ontology_pg.py` | Three categories + two of the same kind (labels) between one pair; updating one touches neither the others nor erases an omitted field; A→B ≠ B→A; undirected written once, in both writing directions; per-category defaults. **Also on real Postgres** (migration + checks inside a rolled-back transaction). |
| Consistency in the face of failures | same; `verify_ontology_cycle.py` | A failure in the middle of the commit undoes object **and** relation (JSON and **real Postgres**, with throw-away rows); the same draft then commits in full. |
| Update of the principal with the history preserved; distinction between new information, correction and change in the world | `verify_ontology_cycle.py` | A whole cycle (mocked queries): ledger computed on stored × draft (includes the decision's own patch; excludes the newborn); `before/after` come from the ledger, not from the model; three distinct kinds; a correction `supersedes` the earlier record; the old record intact; attempts to forge/edit/link a record refused; repetition and unknown ref dropped. |
| Unambiguous reference to a relation | `verify_ontology_cycle.py`, `verify_ontology_graph.py` | A relation record cites the bond's **id** + key; no edge-on-edge; a bond found by the natural key (either end if undirected); two bonds of the same pair are not confused. |
| Selective retrieval of records; respect for limits | `verify_ontology_graph.py`, `verify_ontology_mindreader.py` | Newest first; filters by kind/attribute/period; `limit` and `IDENTITY_RECORDS_MAX`; `GRAPH_MAX_EDGES` + `edges_omitted`; summary per node/bond; records out of traversal, recall and search (and a `RESTORE` of a record is refused); `/api/identity` bounded and escaped; tolerance of crooked formats on the new contracts. |
| Reuse of the same object in several clusters | `verify_ontology_graph.py` | One row, two subjects; the backbone does **not** merge them (fix D13); archiving one subject keeps the object another still needs and archives the exclusive ones. |
| Preservation of `ScenarioData` | `verify_ontology_cycle.py` + older suites | PATCH refused and UPSERT over an existing one yields a new id (rule kept); `verify_cluster_fixes`, `verify_eviction_fix`, `verify_ownership_fix`, `verify_draft_db` kept passing. |
| Compatibility | older suites, adapted | `verify_six_query_cycle`, `verify_anchor_review_cycle`, `verify_hunter_reading_cycle` pass (adjusted only for the new call); `verify_directed_fix` changed its expectation on purpose (`directed` is now written). Four older scripts (`test_airp`, `verify_meteor_cycle`, `verify_retrieval_loop`, `verify_kq10_13_cycle`) **were already failing before** (confirmed on a copy of the earlier tree) — they predate this change. |

**Migration 010 on the real database:** first run and rolled back inside a transaction (idempotent: runs twice), then applied; the existing data ended up with `label ''`, `directed false`, with no loss.

## B5. Choices that remain open

* **O1 — What "learning" is.** The ledger covers representation (description, remarks, age, sex, type, Schemas), not Liriel's reactions (Feelings), nor Ordinances. Where the line falls is a design decision, open to revision.
* **O2 — Change without a record.** If the model returns `records: []` for a real change, the previous value survives only in the cycle log. Forcing a record would mean inventing source/reliability and would flood the history with rewrites.
* **O3 — Records and `SPLIT_VOV`.** When a confused object is split (§6.9), the records stay with the archived original (they document how that understanding was reached) and the pieces start with no records. Reattaching each record to the piece it belongs to is not decided.
* **O4 — Nested-MOV rows.** Changes in mirrors produce no record (they model another's inner state, not a known thing about the object). One may want to record them.
* **O5 — Period and place.** `since`/`until` apply to records; "place/event/moment" are reached through the structure (`Link_Space_Time` to `Event`/`Thing`), not through a generic time filter on the graph.
* **O6 — Placeholder bond.** The bond created for an untyped `relevant_relations` entry is still `Link_Valence_Load` (or `Link_Subject_Cluster` when one end is `ScenarioData`), now marked as untyped in its own text. It is still a stopgap that asserts a category nobody asserted; the right way is for the model to type it (in the real test the 12B typed the bonds it wrote, but left several untyped). Open alternative: a category of its own, "untyped".
* **O7 — Default direction** when the model omits `directed` (see D7).
* **O8 — Several writes of the same field in the same cycle** collapse into a single change (before × after the cycle), not one per step.
* **O9 — Cost.** +≈4.4 thousand tokens per call (MS) and one call per changed target; §12 can be pruned per call (MS §0.6) or parallelized (`FANOUT_CONCURRENCY`).
* **O10 — Quality of the local model.** Only the synthetic tests above are deterministic; the behavior of Gemma 4 12B with the new prompts (typing bonds, stating direction, telling rereading from learning) is empirical — see B6.

## B6. Real test on Gemma 4 12B (local)

Three cycles of the Bianca case (steps 1–3), with the **real** model, on a draft store (JSON), `LLM_PROFILE=1`, context 98,304. Nothing was written to the database.

**Mechanics.** The three cycles finished with no validation error: 583 s, 797 s, 1027 s (the time grows with the number of hunters and of Objects reviewed). `IDENTITY_UPDATE` ran only on step 2 (two calls: Fábio and one bond); on steps 1 and 3 no ledger field of an already existing Object changed, so no call was made.

**What the 12B did well.**
* **Categories and labels:** it wrote 6 `Link_Genealogical` bonds with a `label` (`brother`, `spouse`, `father`, `mother`, `uncle`, `wife`) and used `Link_Valence_Load` for emotional charge — it did not confuse kinship with emotion.
* **Direction:** it stated `directed` on **every** bond that is not a Subject bond (on Subject bonds, 10 of 29 stayed at the default); it marked the symmetric ones (siblings, spouses) **mutual** and the asymmetric ones (father/mother/uncle → Bianca) **directed**.
* **Asymmetry with its own text and strength:** Bianca → Thiago (`strength 4`, "intense romantic pursuit") and Thiago → Bianca (`strength 2`, "mutual adolescent interest") as **two separate bonds**.
* **Rereading × learning:** asked about the change in a bond's text ("trusted confidant" → "confidant in family crisis"), it answered `records: []` and explained: *a rewording that incorporates the current context; neither learning nor a correction of the trust*. That is exactly the distinction asked for.
* The record it wrote has `before/after` copied by the code, a plausible source (the Telegram message), `reported`, reliability 4, was born archived and linked to Fábio by `Link_Identity_Part`.

**What the 12B did questionably.**
* **The only record** (Fábio, `brief_description`, `world_change`: "went from reporting Vinícius's school crisis to Bianca's family crisis") is faithful to what changed, but **is not learning about Fábio**: his description carries the subject of the moment, and the model treated the change of subject as a change in the world. It is an earlier modeling problem (description used as a summary of the scene) that the ledger only exposed. It stands as evidence for O1 (what counts as "learning").
* **Placeholder bonds:** 9 of the 13 emotional-charge bonds were **empty** placeholders (created from `relevant_relations` that `RELATIONS_UPDATE` did not type) — fixed (D14: now they declare themselves untyped and produce no ledger entry).
* **Subject** bonds dominate (33 of 53), as expected of the backbone; `strength` appeared on only 4 bonds (it is optional).
* The ids still carry the model's naming mistakes (`Sentient_Bianca_Neta_Fabio`: Bianca is a niece, not a granddaughter), unrelated to this change.

**Not exercised in this real test:** `identity_requests` (the model asked for no records — there was only one), a correction with `supersedes` (needs a second cycle correcting the earlier one) and a relation target with a record (the 12B preferred `[]`). Those paths are covered only by the synthetic tests.


## B7. Revision Rev 0007 — what a QA campaign of whole cycles found

A QA campaign of whole cycles (its scenarios, runner and findings are kept outside this public repository), with the local Gemma 4 12B on an isolated MOV (`MOV_QA`). `MOV_DEFAULT` was not touched. Normative document: `docs/MetaScheme_Liriel_Rev0007.md`.

**Decisions (each gives the model a better question; none puts a judgment of content in the code).**

* **D21 — Feelings of new rows, once the scene exists (Query 5B).** `MOV_UPDATE` no longer writes `feelings` on any row. Each row created in the cycle (Liriel's MOV and the nested MOVs) is reviewed, one per call, by the same `ANCHOR_REVIEW` (the *born* variant), after `RELATIONS_UPDATE`. Finding: the cycle with the dense call anchored the fear on the affected person (Helena, Marta, Pedrinho), copied the state of third parties and never created the `Situation` that is the cause.
* **D22 — The cause exists before the charge.** Query 1 names the causes (Situation/Event/Thing) as elements of the scene; `MOV_UPDATE` creates them. A cause is what *provokes* the feeling, never the feeling nor its owner ("Marta's fear" is not an Object). Each hunter says, in its `HUNTER_READING`, what it feels something about (`feels_about`); `MOV_UPDATE` mirrors exactly that in its nested MOV.
* **D23 — Interlocutor and discretion.** Query 1 names who wrote; the decision and the reply receive the rule "what someone told is theirs" (no repeating confidences to another person, still less to a child). Finding: answering Pedrinho (7 years old), Liriel mentioned his parents' conflict.
* **D24 — A continued Objective.** Re-electing a standing Objective created `Objective_X_2` beside `Objective_X` in every cycle. Now the id of a standing, active Objective written by the model is the model's own identification (and `continues_objective` covers the case of a different id); the code only applies the update in place. An archived Objective is never revived this way.
* **D25 — The VOV follows reality; the `Identity` record keeps everything pertinent to that aspect.** Liriel's own row included. An attempt to (a) veto changes to the description of Liriel's row and (b) narrow the bond ledger was **reverted**: both took a judgment away from the model. Instead, the model is guided: the row states what is KNOWN (it does not summarize the moment nor read inner state); a number that changed only because Liriel's evaluation changed, with no new information, is a rereading (`records: []`), and the confidence of a learning goes in `reliability`/`obtained_via`.
* **D26 — `"neutral"` on an axis the row never had does not write a 0** (blank ≠ zero, MS §6.6). A structural rule: it only looks at whether the axis exists.
* **D27 — In a nested MOV, the OWNER's value changes with evidence about the owner**; news from another person about the Object does not change what the owner feels.

**Choices that remain open.** O12: the interlocutor is the model's reading; in deployment the embodiment should supply it as a fact. O13: the 12B still over-links the members of a cluster (all with all) and sometimes gets kinship wrong (daughter-in-law/son-in-law). O14: the quality of the review of nested rows depends on `MOV_UPDATE` having created the right mirrors. O15: the cost grows with the rows created in the cycle (one call per born row).

## B8. The `Interpellation` nature — a demand that sits between two Objects (Rev 0007, part AZ)

Requested by the author after testing Liriel over Telegram: a fundamental nature was missing for what one Object **demands** of another. The name was chosen among `Interpellation`, `Provocation`, `Summons`, `Imperative` and `Imprint`; `Interpellation` was kept: addressing someone **demanding an answer** and, in Althusser, constituting the identity of whoever is called — the three things the concept gathers (it comes from an origin, it demands an attitude of the target, it can shape the target's identity). It is neutral in value: the charge, positive or negative, is on the axes, not in the name.

**The concept.** An `Interpellation` is the *persistent* demand that one Object — or a set of them, the **origin** — addresses to another Object, or set, the **target**, asking it for an **attitude**: at least while the target deals with the origin, and sometimes beyond, because it can change who the target is. Example: Fabio says Liriel is being "robotic". What he said is an `Event`/`Situation`; what **stays** — "behave less robotically with me" — is the Interpellation. It is not an `Objective` (Liriel's objective), nor an `Identity` (a record of what was learned), nor a bond (it has content and charge of its own).

**Structure (author's decision: reuse the Identity bond).** The Interpellation sits **in the middle**: one `Link_Identity_Part` bond from the Interpellation **to each party**, directed (satellite → principal, like every bond of the Identity category), with `label` `origin` or `target`. Origin and target can be sets: one edge per party. It belongs to the subject it came from like any Object of the cluster, through `Link_Subject_Cluster` to the ScenarioData. It lives in the MOV of whoever sees it (MS §6.11): "Fabio demands of Liriel" in her MOV; "Rui demands of Marta", as Marta lives it, in Marta's nested MOV. The charge on the row (for example `PositiveNegativeAmazement`) is the MOV owner's and is what makes it weigh in attention, in what stays in focus and in what is memorized.

**Retrieval.** Identity recall (MS §7.5) already brings the principal with all its satellites; since the Interpellation is a satellite of each party, it comes back with the Sentient (and goes to the focus). When Query 1 names an agent of the scene, the archived Interpellations tied to it come back with it — **except through Liriel's own row**, or every demand made of her would come back in every cycle. Recalled from itself, the Interpellation brings all its parties and picks no "master".

**What belongs to the model and what to the code.** The model decides that an Interpellation exists, what it asks and who the parties are (Query 1 names it as an element, Query 5 writes the row, Query 5A the bonds, Query 6 reads what it demands of the reply). The code only keeps the structure: a `WRITE_RELATION` touching an Interpellation is honored only as a role bond or a subject bond; a bond written the wrong way round is flipped (the ends are known by their nature); `relevant_relations` never becomes a role-less bond with an Interpellation; Liriel's row is reviewed like the others (and free-text schemas never on it).

**What was NOT built.** Replicating an Interpellation to other Objects and changing the target's identity beyond the obvious belong to ProcessIntrospection and ProcessDormancy, which do not exist in the code yet; in ProcessMotivation the Interpellation sits among the Objects of the MOV.

**Database.** Migration `011_interpellation.sql` (widens the `CHECK` on `object_nature`; `kind` has no CHECK and `label` exists since 010). Applied to the live database and verified in a rolled-back transaction (`tests/ontology/verify_ontology_pg.py`).

**Verification.** `tests/ontology/verify_interpellation.py` (nature, roles, guarding of the bonds, direction flip, recall with the agent, recall from the Interpellation), `verify_interpellation_cycle.py` (two mocked cycles: creation with origin and target; it comes back from the archive with Fabio and is reviewed again; prompts and MetaScheme) and the extensions of `verify_ontology_mindreader.py` (its own color, "origin/target" roles on the edges, side panel).

**Open (needs real cycles).** How often the 12B fires an Interpellation, and on what; whether it confuses it with an ordinary request or fails to create it (the prompt carries one example in Query 1 and another in 5A, and the 12B copies what it sees); and whether Query 6 really comes to shape behavior from it (`character_says`).
