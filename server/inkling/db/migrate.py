"""Migration runner: applies numbered .sql files in order, once each.

Runs on DATABASE_URL_MIGRATE (migrate_role owns all objects; the runtime role
never sees this connection string). Each migration file is applied in its own
transaction together with its schema_migrations bookkeeping row, so a failed
migration leaves nothing half-applied.
"""

import logging
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def apply_all(dsn: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations; returns the filenames applied this run."""
    applied_now: list[str] = []
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              filename   text PRIMARY KEY,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()

        already = {
            row[0]
            for row in conn.execute("SELECT filename FROM schema_migrations")
        }
        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in already:
                continue
            log.info("applying migration %s", path.name)
            conn.execute(path.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)",
                (path.name,),
            )
            conn.commit()
            applied_now.append(path.name)
    return applied_now
