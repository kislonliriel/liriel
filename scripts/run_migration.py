"""
Runs a .sql migration file against the database configured in .env (either
PGHOST/PGUSER/PGPASSWORD/... or DATABASE_URL).

Usage:
    python scripts/run_migration.py migrations/001_init.sql

Only needed if you don't want to paste the SQL into the Supabase web SQL
editor by hand — functionally identical either way.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2  # noqa: E402

from config import settings  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/run_migration.py <path_to_sql_file>")
        sys.exit(1)

    sql_path = Path(sys.argv[1])
    if not sql_path.exists():
        print(f"File not found: {sql_path}")
        sys.exit(1)

    if not settings.has_discrete_pg_config and not settings.database_url:
        print(
            "No database configuration found in .env "
            "(PGHOST/PGUSER/PGPASSWORD/... or DATABASE_URL). Configure it before running the migration."
        )
        sys.exit(1)

    sql = sql_path.read_text(encoding="utf-8")

    print("Connecting to the database configured in .env...")
    if settings.has_discrete_pg_config:
        conn = psycopg2.connect(
            host=settings.pg_host,
            port=settings.pg_port,
            dbname=settings.pg_database,
            user=settings.pg_user,
            password=settings.pg_password,
        )
    else:
        conn = psycopg2.connect(settings.database_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            print(f"Running {sql_path}...")
            cur.execute(sql)
        print("Migration applied successfully.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
