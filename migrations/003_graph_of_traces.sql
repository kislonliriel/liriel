-- Liriel — the Graph of Traces (docs/MetaScheme_Liriel_Rev0000.md §8) and
-- the two MainMemory commands it depends on: WRITE_RELATION and
-- SOFTEN_CHARGE (§7.2, §12.4). Until now these were only logged
-- ([notice] MainMemory command not executed) — this table is what makes
-- them real, and what TrackGraphProcess (graph_service.py) actually
-- traverses.
--
-- Run after 001_init.sql and 002_metascheme_alignment.sql.

-- One edge between two Objects, "affective as well as propositional" (§8.1).
-- Undirected relations (marriage, kinship, friendship, ...) are stored once
-- with directed=false; the traversal in graph_service.py reads such a row
-- from either endpoint. Multiple distinct relations between the same pair
-- (e.g. "marriage" and later "grievance") are separate rows — kind is part
-- of the identity of an edge, not a property to overwrite.
create table if not exists mov_relations (
    id             uuid primary key default gen_random_uuid(),
    from_vov_id    text not null references mov_objects(vov_id) on delete cascade,
    to_vov_id      text not null references mov_objects(vov_id) on delete cascade,
    kind           text not null,
    directed       boolean not null default false,
    propositional  text,
    -- List of {"axis": "...", "v": <float>} — the same {axis,v} shape used
    -- elsewhere for a quick per-axis affective reading on an edge (§8.3).
    -- SOFTEN_CHARGE (§7.2) scales these down in place; the edge and its
    -- propositional content survive ("the bond itself remains").
    affective      jsonb not null default '[]',
    confidence     smallint check (confidence between 1 and 5),
    since_text     text,   -- free-text "since" from §8.3 (e.g. "2020") — not a real date
    softened_at    timestamptz,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (from_vov_id, to_vov_id, kind)
);

create index idx_mov_relations_from on mov_relations(from_vov_id);
create index idx_mov_relations_to on mov_relations(to_vov_id);
