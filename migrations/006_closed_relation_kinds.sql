-- Liriel — architecture redesign: widen mov_objects.object_nature for
-- "Sentient" (replaces "Person" going forward, MS §6.4), and remap every
-- existing mov_relations.kind value onto the new closed three-value
-- vocabulary (Link_Valence_Load / Link_Identity_Part / Link_Subject_Cluster
-- — MS §8.3, this session's redesign).
--
-- Run after 001-005.
--
-- Part 1 (schema, additive/safe): widen the object_nature CHECK constraint.
-- "Person" is KEPT in the list, not replaced — existing rows are not force-
-- relabeled by this migration (see Part 3, optional).

alter table mov_objects drop constraint mov_objects_object_nature_check;

alter table mov_objects add constraint mov_objects_object_nature_check
    check (object_nature in
           ('PCI', 'Person', 'Sentient', 'Objective', 'Situation', 'Thing',
            'Idea', 'Event', 'Memory', 'Group', 'Animal',
            'Entity', 'Attribute', 'Self-Process', 'ScenarioData'));

-- Part 2 (DATA WRITE on existing rows — confirm before running against a
-- real database with real relations in it). mov_relations.kind has no
-- CHECK constraint (free text, migrations/003), so old rows keep working
-- for traversal today — but a request that explicitly filters by the new
-- closed kinds (e.g. an identity-recall or cluster-recall
-- relation_kinds=["Link_Subject_Cluster"]) will not match them unless
-- they're remapped here. cluster_backbone/cluster_member (AIRP's own prior
-- vocabulary) both become Link_Subject_Cluster; every other pre-existing
-- kind (the open vocabulary this replaces entirely — "marriage",
-- "kinship", "associated", etc.) becomes Link_Valence_Load, the generic
-- catch-all for "some other bond carrying its own weight."
--
-- Inspect first:
--   select kind, count(*) from mov_relations group by kind order by 2 desc;
--
-- mov_relations has a unique(from_vov_id, to_vov_id, kind) constraint
-- (migrations/003) — collapsing several old kinds into one new kind can
-- make two previously-distinct rows between the same pair (e.g. a
-- "cluster_backbone" row and a separate "cluster_member" row both linking
-- the same two ids) collide once they'd both become the same new kind.
-- Deduplicate within each target group first (keep the oldest row per
-- (from_vov_id, to_vov_id) pair, drop the rest) before the UPDATE, or the
-- UPDATE fails outright on the first such collision.

delete from mov_relations
 where id in (
    select id from (
        select id, row_number() over (
            partition by from_vov_id, to_vov_id
            order by created_at, id
        ) as rn
        from mov_relations
        where kind in ('cluster_backbone', 'cluster_member')
    ) ranked
    where rn > 1
 );

update mov_relations
   set kind = 'Link_Subject_Cluster'
 where kind in ('cluster_backbone', 'cluster_member');

delete from mov_relations
 where id in (
    select id from (
        select id, row_number() over (
            partition by from_vov_id, to_vov_id
            order by created_at, id
        ) as rn
        from mov_relations
        where kind not in ('Link_Subject_Cluster', 'Link_Identity_Part', 'Link_Valence_Load')
    ) ranked
    where rn > 1
 );

update mov_relations
   set kind = 'Link_Valence_Load'
 where kind not in ('Link_Subject_Cluster', 'Link_Identity_Part', 'Link_Valence_Load');

-- Part 3 (OPTIONAL, not run by default): relabel existing "Person" rows to
-- "Sentient" for consistency with the new vocabulary. Purely cosmetic —
-- object_nature isn't branched on anywhere in this codebase — so this is
-- commented out; uncomment and run deliberately if you want it.
--
-- update mov_objects set object_nature = 'Sentient' where object_nature = 'Person';
