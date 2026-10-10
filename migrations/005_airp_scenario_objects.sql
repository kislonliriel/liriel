-- Liriel — AIRP: widen mov_objects.object_nature to allow "ScenarioData"
-- rows (MS §6.10): the cluster-backbone objects the AIRP mechanism writes
-- during Query 2 (MOV_MAINMEMORY_UPDATE) as its own condensed reading of a
-- topic, related to one another and to the rest of the cluster so the
-- whole matter can be found again in the MainMemory long after it stops
-- being the matter at hand (MS §7.4).
--
-- Only the CHECK constraint changes here — everything else about the
-- column (type, nullability, default) is untouched, and no existing row
-- is affected: this purely widens an enum, same as 002_metascheme_
-- alignment.sql did when it first defined this constraint.
--
-- Run after 001-004.

alter table mov_objects drop constraint mov_objects_object_nature_check;

alter table mov_objects add constraint mov_objects_object_nature_check
    check (object_nature in
           ('PCI', 'Person', 'Objective', 'Situation', 'Thing',
            'Idea', 'Event', 'Memory', 'Group', 'Animal',
            'Entity', 'Attribute', 'Self-Process', 'ScenarioData'));
