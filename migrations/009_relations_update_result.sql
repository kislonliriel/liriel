-- Liriel — RELATIONS_UPDATE (MS §12.6A, Query 5A): one more jsonb column on
-- motivation_cycles for the relations query's own result.
--
-- The typed relations (`write_relations`, `soften_charge`) were part of
-- MOV_UPDATE's output until this revision (migrations/007's
-- mov_update_result still holds them for older cycles). They are now a query
-- of their own, run once MOV_UPDATE has made every Object of the cycle real,
-- so a relation never has to name an Object that has not been minted yet.
--
-- Run after 001-008. Additive and nullable: existing rows are untouched.

alter table motivation_cycles
    add column if not exists relations_update_result jsonb;

comment on column motivation_cycles.relations_update_result is 'MS §12.6A RELATIONS_UPDATE: write_relations / soften_charge';
