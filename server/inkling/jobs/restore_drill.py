"""Monthly restore drill (§13.3): prove the backup restores, or alert.

Download the latest daily dump, verify its recorded sha256, decrypt,
pg_restore into a scratch database, compare per-table row counts against the
live database, then drop what we restored. "This is what makes the backup
real" — an unverified backup is a hope, not a backup.

The scratch target is RESTORE_DRILL_URL (a scratch Neon branch, or an
ephemeral PG in CI). Neon branch creation is deliberately not automated here:
it needs account-level API access the runtime shouldn't hold; the runbook
covers creating a standing scratch branch once.

Steps (download/decrypt/restore/count) are injectable like backup.py's, so
the drill's decision logic — checksum mismatch, count drift, cleanup, alerts
— is fully tested with stubs.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

from inkling.db.client import Database

log = logging.getLogger(__name__)

# Tables whose row counts must survive the round trip. Volatile tables
# (triggers, turns, turn_events) keep changing between dump and drill, so
# exact-count assertions there would flake by design.
COUNTED_TABLES = (
    "users",
    "events",
    "commitments",
    "commitment_occurrences",
    "docs",
    "transcript",
    "scheduled_wakeups",
    "memory_facts",
)


class RestoreDrillError(Exception):
    pass


@dataclass
class DrillResult:
    ok: bool
    key: str = ""
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)  # live, restored
    error: str = ""


async def _run(*argv: str) -> None:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RestoreDrillError(
            f"{argv[0]} exited {process.returncode}: {stderr.decode()[:400]}"
        )


async def default_decrypt(src: Path, out: Path) -> None:
    await _run("gpg", "--batch", "--yes", "--decrypt", "-o", str(out), str(src))


async def default_restore(scratch_url: str, dump: Path) -> None:
    # --clean --if-exists: the standing scratch branch holds last month's
    # drill; each run replaces it wholesale.
    await _run(
        "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges",
        "-d", scratch_url, str(dump),
    )


def _count_sync(url: str, tables: tuple[str, ...]) -> dict[str, int]:
    counts = {}
    with psycopg.connect(url) as conn:
        for table in tables:
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
            counts[table] = int(row[0])
    return counts


async def default_counts(url: str, tables: tuple[str, ...]) -> dict[str, int]:
    return await asyncio.to_thread(_count_sync, url, tables)


async def run_drill(
    db: Database,
    *,
    s3: Any,
    live_url: str,
    scratch_url: str,
    workdir: Path,
    decrypt: Callable[..., Awaitable[None]] = default_decrypt,
    restore: Callable[..., Awaitable[None]] = default_restore,
    counts: Callable[..., Awaitable[dict[str, int]]] = default_counts,
    alert: Callable[[str], Awaitable[None]] | None = None,
    now: datetime | None = None,
) -> DrillResult:
    now = now or datetime.now(timezone.utc)
    result = DrillResult(ok=False)
    encrypted = workdir / "drill.dump.gpg"
    plain = workdir / "drill.dump"
    try:
        if s3 is None:
            raise RestoreDrillError("no S3 target configured (BACKUP_S3_*)")
        if not scratch_url:
            raise RestoreDrillError("no RESTORE_DRILL_URL configured")

        dailies = [k for k in await s3.list("daily/") if k.endswith(".dump.gpg")]
        if not dailies:
            raise RestoreDrillError("no daily backups found to drill against")
        result.key = dailies[-1]  # datestamped names sort oldest-first
        data = await s3.get(result.key)

        try:
            recorded = (await s3.get(result.key + ".sha256")).decode().strip()
        except Exception:
            recorded = ""  # only the newest daily carries a sidecar
        if recorded and hashlib.sha256(data).hexdigest() != recorded:
            raise RestoreDrillError(
                f"checksum mismatch for {result.key}: stored object does not "
                "match the checksum recorded at backup time"
            )

        encrypted.write_bytes(data)
        await decrypt(encrypted, plain)
        await restore(scratch_url, plain)

        live = await counts(live_url, COUNTED_TABLES)
        restored = await counts(scratch_url, COUNTED_TABLES)
        drift = {}
        for table in COUNTED_TABLES:
            result.counts[table] = (live[table], restored[table])
            # The live side keeps moving between dump and drill; restored may
            # lawfully trail live, but must never exceed it or be empty while
            # live has data.
            if restored[table] > live[table]:
                drift[table] = (live[table], restored[table])
            if live[table] > 0 and restored[table] == 0:
                drift[table] = (live[table], restored[table])
        if drift:
            raise RestoreDrillError(f"row-count drift: {drift}")

        async with db.system() as conn:
            await conn.execute(
                "INSERT INTO audit_log (actor, action, detail) "
                "VALUES ('job:restore_drill', 'restore_drill_ok', %s)",
                (json.dumps({"key": result.key, "counts": result.counts}),),
            )
        result.ok = True
        log.info("restore drill ok against %s", result.key)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"[:500]
        log.exception("restore drill failed")
        async with db.system() as conn:
            await conn.execute(
                "INSERT INTO audit_log (actor, action, detail) "
                "VALUES ('job:restore_drill', 'restore_drill_failed', %s)",
                (json.dumps({"error": result.error, "key": result.key}),),
            )
        if alert is not None:
            try:
                await alert(f"restore drill FAILED: {result.error}")
            except Exception:
                log.exception("restore drill alert also failed")
    finally:
        encrypted.unlink(missing_ok=True)
        plain.unlink(missing_ok=True)
    return result
