-- Liriel — Phase 1 MVP schema
-- Run this once against your Supabase / PostgreSQL database
-- (Supabase SQL editor, or `psql "$DATABASE_URL" -f migrations/001_init.sql`).
--
-- Scope note: this schema covers only the fourteen Feeling axes (Chapter 6
-- of the architecture document). Ordinances (Instincts/Archetypes/
-- PersistentLongings) and Modulating Schemas (Culture/Character/
-- Personality/...) are left for a later phase.

create extension if not exists pgcrypto;

-- One row per persistent MOV: Liriel's own focus, or a nested MOV modelling
-- another player's focus (e.g. MOV_0002 in the reference spreadsheet, which
-- models Fabio's focus as Liriel imagines it).
create table if not exists movs (
    mov_id     text primary key,
    label      text,
    created_at timestamptz not null default now()
);

-- Reference table: the fourteen Feeling axes ("The Feelings: The Names of
-- the Force", Chapter 6). Informational / for prompting & UI — the
-- authoritative definitions live in models.py.
create table if not exists feeling_axes (
    axis_key      text primary key,
    label         text not null,
    positive_pole text not null,
    negative_pole text not null,
    description   text not null,
    display_order smallint not null
);

-- Objects (VectorObjectValence / "VOV") held in a MOV's focus.
create table if not exists mov_objects (
    vov_id             text primary key,
    mov_id             text not null references movs(mov_id) on delete cascade,
    priority           integer,
    object_type        text not null default 'real'
                        check (object_type in ('real', 'fictional')),
    object_nature      text not null
                        check (object_nature in
                               ('PCI', 'Person', 'Situation', 'Objective',
                                'Group', 'Place', 'Thing')),
    valence_regime     text not null default 'State'
                        check (valence_regime in ('State', 'Delta')),
    nested_mov         text references movs(mov_id),
    perceived_age      text,
    sex                text,
    brief_description  text not null,
    relevant_relations text,
    delta_report       jsonb,
    relevant_remarks   text,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

create index if not exists idx_mov_objects_mov_id on mov_objects(mov_id);

-- The MatrixObjectsValence proper: one row per (Object, Feeling axis), with
-- the valence (-5..+5) and confidence (1..5) notes described in Chapter 6.
create table if not exists mov_object_valences (
    vov_id      text not null references mov_objects(vov_id) on delete cascade,
    axis_key    text not null references feeling_axes(axis_key),
    value       numeric(3, 1) not null check (value between -5 and 5),
    confidence  smallint check (confidence between 1 and 5),
    updated_at  timestamptz not null default now(),
    primary key (vov_id, axis_key)
);

-- Log of every ProcessMotivation cycle — useful for debugging/tracing in the
-- MVP. graph_of_traces stays NULL in Phase 1: it is the plug point for
-- Phase 2 (GraphOfTraces / TrackGraphProcess) mentioned in Chapter 14.
create table if not exists motivation_cycles (
    id              uuid primary key default gen_random_uuid(),
    mov_id          text not null references movs(mov_id) on delete cascade,
    scenario_data   text not null,
    graph_of_traces jsonb,
    update_notes    text,
    best_prey_guess jsonb,
    response_text   text,
    created_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Seed data
-- ---------------------------------------------------------------------

insert into feeling_axes
    (axis_key, label, positive_pole, negative_pole, description, display_order)
values
    ('HopeFear', 'Hope / Fear', 'Hope', 'Fear',
     'The sense of hope in the face of a good that seems likely, and fear in the face of a threat drawing near.', 1),
    ('BodySensationsPleasurePain', 'Pleasure / Pain', 'Pleasure', 'Pain',
     'Physical pleasure and pain in their most direct, non-negotiable form.', 2),
    ('BodySensationsOther', 'Other body sensations', 'Well-being', 'Malaise',
     'Every other sensation the body reports about itself: hunger, thirst, cold, heat, itching, fatigue, and the like.', 3),
    ('PrideEmbarrassmentShame', 'Pride / Embarrassment-Shame', 'Pride', 'Embarrassment/Shame',
     'One''s standing in one''s own eyes as seen through the group''s.', 4),
    ('AttractionDisgust', 'Attraction / Disgust', 'Attraction', 'Disgust',
     'Being drawn to bodies, things and ideas, versus being repelled by them.', 5),
    ('ExcitementBoredom', 'Excitement / Boredom', 'Excitement', 'Boredom',
     'The eagerness an object kindles, versus the emptiness of when something fails to hold.', 6),
    ('LoveAngerEros', 'Love-Anger-Eros', 'Love/Eros', 'Anger',
     'The warmth directed at someone, from tenderness to fury, shot through with desire.', 7),
    ('MirthGloom', 'Mirth / Gloom', 'Mirth', 'Gloom',
     'Finding something funny, versus the somber, the gloomy.', 8),
    ('CutenessCreepiness', 'Cuteness / Creepiness', 'Cuteness', 'Creepiness',
     'The tender "aww" that draws one in, versus the eerie sense that pushes one away.', 9),
    ('PositiveNegativeAmazement', 'Amazement (+/-)', 'Awe', 'Horror',
     'Astonishment in its two signs: wonderstruck awe versus horror-struck awe.', 10),
    ('CuriosityIndifference', 'Curiosity / Indifference', 'Curiosity', 'Indifference',
     'Being intrigued, that something asks to be known, versus nothing intriguing.', 11),
    ('HappinessSadnessDRH', 'Happiness/Sadness (Desire-Related)', 'Happiness (DRH)', 'Sadness (DRH)',
     'Desire Related Happiness: the quick, dated joy or sadness tied to a particular desire being satisfied or not.', 12),
    ('HappinessSadnessCES', 'Happiness/Sadness (Existential)', 'Happiness (CES)', 'Sadness (CES)',
     'Core Existential Satisfaction: the slow, grave joy or sadness tied to what something means on the plane of existence.', 13),
    ('LoveHateSublime', 'Love / Hate (Sublime)', 'Love (sublime)', 'Hate',
     'Love and hate in the sublime register: the deep, lasting bond with what transcends the everyday.', 14)
on conflict (axis_key) do nothing;

-- Default MOV used by the Phase 1 terminal chat loop.
insert into movs (mov_id, label)
values ('MOV_DEFAULT', 'Default single-user chat MOV')
on conflict (mov_id) do nothing;
