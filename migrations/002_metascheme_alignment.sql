-- Liriel — align schema to the official MetaScheme (docs/MetaScheme_Liriel_Rev0000.md)
-- Run after 001_init.sql.
--
-- Drops and recreates mov_objects / mov_object_valences / motivation_cycles
-- to match the MetaScheme's VOV shape (MS §6.4, §12.2): object_type and
-- object_nature enums change, `sex` becomes `male_female`, and Ordinances
-- (MS §4), Modulating Schemas (MS §5) and the Objective block (MS §10.6)
-- are now recorded per row. Pre-release churn — see the WIP notice in
-- phase1_mvp/README.md; if you have real data in these tables, export it
-- before running this.

drop table if exists mov_object_valences;
drop table if exists motivation_cycles;
drop table if exists mov_objects;

-- Objects (VectorObjectValence / "VOV") held in a MOV's focus — and, once
-- archived_at is set, in its MainMemory (MS §7; both live in this one
-- table, distinguished by that column, per Phase 1's simplification).
create table mov_objects (
    vov_id             text primary key,
    mov_id             text not null references movs(mov_id) on delete cascade,
    priority           integer,
    update_datetime    text,
    object_type        text not null default 'real'
                        check (object_type in ('real', 'imagined', 'hypothetical')),
    object_nature      text not null
                        check (object_nature in
                               ('PCI', 'Person', 'Objective', 'Situation', 'Thing',
                                'Idea', 'Event', 'Memory', 'Group', 'Animal',
                                'Entity', 'Attribute', 'Self-Process')),
    valence_regime     text not null default 'State'
                        check (valence_regime in ('State', 'Delta')),
    nested_mov         text references movs(mov_id),
    perceived_age      text,
    male_female        text,
    brief_description  text not null,
    relevant_relations jsonb not null default '[]',
    delta_report       jsonb,
    relevant_remarks   text,
    ordinances         jsonb not null default '{}',
    schemas            jsonb not null default '{}',
    objective          jsonb,
    archived_at        timestamptz,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

create index idx_mov_objects_mov_id on mov_objects(mov_id);
create index idx_mov_objects_focus on mov_objects(mov_id, archived_at);

-- Feelings proper: one row per (Object, Feeling axis) — unchanged shape
-- from 001_init.sql, just recreated because mov_objects was dropped.
create table mov_object_valences (
    vov_id      text not null references mov_objects(vov_id) on delete cascade,
    axis_key    text not null references feeling_axes(axis_key),
    value       numeric(3, 1) not null check (value between -5 and 5),
    confidence  smallint check (confidence between 1 and 5),
    updated_at  timestamptz not null default now(),
    primary key (vov_id, axis_key)
);

-- Log of every ProcessMotivation cycle. Now three query results
-- (MOV_MAINMEMORY_UPDATE, BEST_PREY_GUESS, and Phase 1's reply-composition
-- step) instead of two.
create table motivation_cycles (
    id              uuid primary key default gen_random_uuid(),
    mov_id          text not null references movs(mov_id) on delete cascade,
    scenario_data   text not null,
    graph_of_traces jsonb,
    update_result   jsonb,
    decision_result jsonb,
    response_text   text,
    created_at      timestamptz not null default now()
);
