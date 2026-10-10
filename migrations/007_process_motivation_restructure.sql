-- Liriel — ProcessMotivation restructure: one jsonb column per query of
-- the new six-query cycle (MS §11.1 Rev 0001), replacing the old
-- `update_result` column. `decision_result` keeps its name — it's still
-- BEST_PREY_GUESS's own output, just the last of six queries now instead
-- of the third of three (migrations/002's "three query results" comment
-- above `motivation_cycles` is stale as of this revision).
--
-- Run after 001-006.
--
-- DO NOT RUN against the live Supabase DB without explicit user go-ahead
-- (same practice as every other migration this project has run so far).

alter table motivation_cycles
    add column if not exists scene_subject_check_result jsonb,
    add column if not exists graph_request_result jsonb,
    add column if not exists tactical_scene_result jsonb,
    add column if not exists mainmemory_filing_result jsonb;

-- update_result was GRAPH_REQUEST+MOV_MAINMEMORY_UPDATE's combined output
-- pre-redesign; GRAPH_REQUEST's own share of that now lives in
-- graph_request_result, and what remains of MOV_MAINMEMORY_UPDATE
-- narrows to mov_update_result (MS §12.6). Nothing reads update_result
-- going forward (database.py's log_cycle no longer writes it), so this
-- drops it rather than leaving it to silently stop being populated.
alter table motivation_cycles rename column update_result to mov_update_result;

comment on column motivation_cycles.scene_subject_check_result is 'MS §12.1 SCENE_SUBJECT_CHECK';
comment on column motivation_cycles.graph_request_result is 'MS §12.2 GRAPH_REQUEST';
comment on column motivation_cycles.tactical_scene_result is 'MS §12.3 TACTICAL_SCENE_INTERPRETATION';
comment on column motivation_cycles.mainmemory_filing_result is 'MS §12.4 MAINMEMORY_FILING';
comment on column motivation_cycles.mov_update_result is 'MS §12.6 MOV_UPDATE';
comment on column motivation_cycles.decision_result is 'MS §12.7 BEST_PREY_GUESS';
