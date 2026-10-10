-- Liriel — ANCHOR_REVIEW (MS §12.3A, KQ10-13): one more jsonb column on
-- motivation_cycles for the per-Object review results.
--
-- Query 3A runs ONCE PER OBJECT in the MOV and in each nested MOV (a MOV can
-- hold dozens of Objects; one query cannot give each the detail the judgment
-- needs), so a cycle produces a list of results. They are stored as one
-- object, {"cycle_id": ..., "reviews": [ ... ]}, in the same row as the other
-- queries' own columns (migrations/007), so each Object's answer to
-- KQ10/KQ11/KQ12/KQ13 can be read back individually.
--
-- Run after 001-007. Additive and nullable: existing rows are untouched.

alter table motivation_cycles
    add column if not exists anchor_review_results jsonb;

comment on column motivation_cycles.anchor_review_results is 'MS §12.3A ANCHOR_REVIEW (KQ10-13), once per Object: {cycle_id, reviews[]}';
