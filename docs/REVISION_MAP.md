# Revision map

Liriel is described by two families of documents, each with its **own** revision sequence:

* **Reference documents** — *Volume 1 · Liriel: The Architecture of a Persistent Cognitive Instance* (narrative, `.docx`) and the two matrices,
  *MatrixObjectsValence* and *MatrixOrdinance* (`.xlsx`). They are published for human readers on kislon.ai. Master copies:
  `Documents Control\RevNNNN\` (one folder per revision; a document that did not change in a revision is not duplicated there). Copies of the operating documents live in `Documents Control\Operating\`.
* **Operating documents** — the *MetaScheme* (`docs/MetaScheme_Liriel_RevNNNN.md`, loaded verbatim as the model's system prompt) and
  its companion `docs/Ontology_Liriel.md`. They are published only in this repository, and change far more often.

The numbers are independent on purpose: coupling them would break the consecutive sequence of the reference documents. This map ties them.

## Reference documents

| Revision | Volume 1 | MatrixObjectsValence | MatrixOrdinance | Reflected in MetaScheme |
|---|---|---|---|---|
| Rev0000 | Rev0000 | Rev000 | 000 | Rev 0000 – Rev 0008 (all state "derived from Volume 1 Rev 0000") |
| Rev0001 | Rev0001 (rewrite: "About this Revision", status and claims, chapters reorganized) | same as Rev0000 | same as Rev0000 | to be checked against Rev 0008 |
| Rev0002 | Rev0002, from Rev0001 (catches up with MetaScheme Rev 0008: campaign findings, safety screen, Interpellation, batch mode, Claude Code back end) | same as Rev0000 (no post-Rev0000 vocabulary in the example rows; no Rev002 issued) | same as Rev0000 | Rev 0008 |

## Operating documents

| MetaScheme | What it added | Based on |
|---|---|---|
| Rev 0000 | First operating spec of ProcessMotivation | Volume 1 Rev0000, MatrixObjectsValence Rev000 |
| Rev 0001 | Cycle restructured from three dense queries into six narrow ones; δ accounting deferred to ProcessIntrospection | same |
| Rev 0002 | The Feelings/Schemas update decided as explicit KQ10–13 before MOV_UPDATE carries it out | same |
| Rev 0003 | §3.1 sharpened: what an anchored Feeling's Object is | same |
| Rev 0004 | KQ07–KQ09: Ordinances and Schemas read per hunter | same |
| Rev 0005 | Typed relations moved to their own query (RELATIONS_UPDATE) | same |
| Rev 0006 | Ontology of Objects and relations (`Identity`, six bond categories) — see `Ontology_Liriel.md` | same |
| Rev 0007 | What a QA campaign of whole cycles found; SAFETY_SCREEN; `Interpellation` | same |
| Rev 0008 | Batch mode (§12.12, §11.1A) for a model that judges many items in one call | same |

Liriel's factory row (`liriel_seed.py`) is her row `VOV_0000` of MatrixObjectsValence Rev000: BriefDescription, RelevantRemarks, Culture and
Modulating Schemas.

MetaScheme Rev 0000–0008 cite "MatrixObjectsValence (Rev 000/001)": there is no Rev001 of the matrix; the citation is an error, to be
corrected in the next MetaScheme revision (published revisions stay as they are). A matrix revision takes the number of its folder
(`MatrixObjectsValence_Rev002.xlsx` in `Rev0002\`).
