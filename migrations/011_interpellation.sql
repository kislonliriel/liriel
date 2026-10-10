-- Rev 0007 part AZ (MS §6.14): a new Object nature, `Interpellation` -- the standing demand one Object (or a set of
-- them) directs at another that asks the target for an attitude, at least while it deals with the origin, and may change
-- the target's identity. It sits between its parties: one `Link_Identity_Part` bond from the Interpellation to each party,
-- the bond's `label` saying `origin` or `target`. A bond's `kind` has no CHECK constraint (migrations/003) and `label`
-- exists since 010, so the only schema change is the closed list of natures.
--
-- Run after 001-010. Safe to run twice.

alter table mov_objects drop constraint if exists mov_objects_object_nature_check;

alter table mov_objects add constraint mov_objects_object_nature_check
    check (object_nature in
           ('PCI', 'Person', 'Sentient', 'Objective', 'Situation', 'Thing',
            'Idea', 'Event', 'Memory', 'Group', 'Animal',
            'Entity', 'Attribute', 'Self-Process', 'ScenarioData', 'Identity', 'Interpellation'));
