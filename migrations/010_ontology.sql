-- Liriel — ontology of Objects and relations (MetaScheme Rev 0006).
--
-- Part 1: the `Identity` Object nature and its payload.
--   An Identity Object documents something learned or changed about a principal
--   Object (or about one relation): the row's own columns are an ordinary VOV, and
--   `identity_record` holds the record (what, before/after, source, reliability,
--   when). Additive: widens a CHECK, adds a nullable column and two partial indexes.
--
-- Part 2: relations.
--   * `label`    — the short word that tells two bonds of the same category between
--                  the same pair apart. It becomes part of the bond's identity.
--   * `strength` — optional 1-5 intensity, NULL = not stated (not a zero).
--   * The old unique(from_vov_id, to_vov_id, kind) is replaced by
--     unique(from_vov_id, to_vov_id, kind, label). Every existing row gets label ''
--     so it keeps its identity: nothing is merged, deleted or renamed. `kind` has no
--     CHECK constraint (migrations/003), so the three new categories need no change.
--
-- Part 3: the cycle log gets one more column for IDENTITY_UPDATE's results.
--
-- Run after 001-009. Safe to run twice.

-- Part 1 ----------------------------------------------------------------------------
-- Only when the list does not already take `Identity`: a database that has since run 011 (which widens
-- the same list further, to `Interpellation`) and holds such rows must not have its constraint narrowed
-- back by running this file again.
do $$
begin
    if not exists (select 1 from pg_constraint
                   where conrelid = 'mov_objects'::regclass
                     and conname = 'mov_objects_object_nature_check'
                     and pg_get_constraintdef(oid) like '%Identity%') then
        alter table mov_objects drop constraint if exists mov_objects_object_nature_check;

        alter table mov_objects add constraint mov_objects_object_nature_check
            check (object_nature in
                   ('PCI', 'Person', 'Sentient', 'Objective', 'Situation', 'Thing',
                    'Idea', 'Event', 'Memory', 'Group', 'Animal',
                    'Entity', 'Attribute', 'Self-Process', 'ScenarioData', 'Identity'));
    end if;
end
$$;

alter table mov_objects add column if not exists identity_record jsonb;

comment on column mov_objects.identity_record is
    'MS §6.13: payload of an Identity Object (target, field, change_kind, information, before/after, source, reliability, recorded_at, ...). NULL on every other nature.';

create index if not exists idx_mov_objects_identity_target
    on mov_objects ((identity_record->>'target_vov_id'))
    where object_nature = 'Identity';

create index if not exists idx_mov_objects_identity_relation
    on mov_objects ((identity_record->'relation'->>'relation_id'))
    where object_nature = 'Identity';

-- Part 2 ----------------------------------------------------------------------------
alter table mov_relations add column if not exists label text not null default '';

alter table mov_relations add column if not exists strength smallint
    check (strength between 1 and 5);

comment on column mov_relations.label is
    'MS §8: short word telling two bonds of one category between the same pair apart; part of the bond''s identity. Stored case-folded.';
comment on column mov_relations.strength is
    'MS §8: optional 1-5 intensity of the bond; NULL means not stated, never zero.';

alter table mov_relations drop constraint if exists mov_relations_from_vov_id_to_vov_id_kind_key;

do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conrelid = 'mov_relations'::regclass and conname = 'mov_relations_bond_key'
    ) then
        alter table mov_relations
            add constraint mov_relations_bond_key unique (from_vov_id, to_vov_id, kind, label);
    end if;
end $$;

-- Part 3 ----------------------------------------------------------------------------
alter table motivation_cycles add column if not exists identity_update_result jsonb;

comment on column motivation_cycles.identity_update_result is
    'MS §12.7A IDENTITY_UPDATE: {cycle_id, results: [...], written: [...]}';
