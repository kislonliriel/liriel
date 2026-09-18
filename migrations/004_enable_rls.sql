-- Liriel — close the anonymous PostgREST API exposure flagged by
-- Supabase's Security Advisor ("RLS Disabled in Public") on all 6
-- application tables: movs, mov_objects, feeling_axes,
-- mov_object_valences, motivation_cycles, mov_relations.
--
-- This app never talks to Supabase's PostgREST/anon-key API — it only
-- ever connects directly to Postgres (config.py's PGHOST/PGUSER/
-- PGPASSWORD, via psycopg2/scripts/run_migration.py), as the role that
-- owns these tables (it's the same role every migration, including this
-- one, runs as). Postgres skips row-level security for a table's owner
-- by default (unless FORCE ROW LEVEL SECURITY is added, which this
-- migration deliberately does not do), so simply enabling RLS with no
-- policies is enough: it fully closes off the anon/authenticated
-- PostgREST roles Supabase's warning is actually about, while leaving
-- this app's own direct connection completely unaffected.
--
-- Run after 001_init.sql, 002_metascheme_alignment.sql and
-- 003_graph_of_traces.sql.

alter table movs                 enable row level security;
alter table mov_objects          enable row level security;
alter table feeling_axes         enable row level security;
alter table mov_object_valences  enable row level security;
alter table motivation_cycles    enable row level security;
alter table mov_relations        enable row level security;
