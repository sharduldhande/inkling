"""Nightly backup (§13.2): pg_dump → gpg → S3, then the markdown export.

Order matters and is the spec's: the encrypted dump reaches object storage
first (the thing that makes the system rebuildable), THEN the human-readable
export commits to EXPORT_GIT_DIR. Any failure alerts the operator topic
directly over ntfy — the deliveries outbox is per-user machinery and a backup
has no user; an alert that itself fails can only be logged. Success writes an
audit_log row carrying dump size + sha256, which is what the restore drill
(§13.3) later verifies against.

Retention is 7 daily / 8 weekly / 12 monthly: every run writes daily/, plus
weekly/ on Sundays and monthly/ on the 1st, then prunes each prefix oldest-
first (datestamped keys sort chronologically).

The subprocess steps (pg_dump, gpg) and the store are injectable; tests run
the whole pipeline with stubs, and the real defaults are one command each.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from inkling.db.client import Database
from inkling.db.queries import docs as docs_q
from inkling.db.queries import events as events_q

log = logging.getLogger(__name__)

RETENTION = {"daily/": 7, "weekly/": 8, "monthly/": 12}
DOC_KINDS = ("goals", "patterns", "digest7", "working_state", "daily_plan")

Step = Callable[..., Awaitable[None]]


class BackupError(Exception):
    pass


@dataclass
class BackupResult:
    ok: bool
    checksum: str = ""
    dump_bytes: int = 0
    uploaded: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    exported: list[str] = field(default_factory=list)
    error: str = ""


async def _run(*argv: str) -> None:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise BackupError(
            f"{argv[0]} exited {process.returncode}: {stderr.decode()[:400]}"
        )


async def default_dump(database_url: str, out: Path) -> None:
    await _run("pg_dump", "-Fc", "-d", database_url, "-f", str(out))


async def default_encrypt(recipient: str, src: Path, out: Path) -> None:
    await _run(
        "gpg", "--batch", "--yes", "--trust-model", "always",
        "--encrypt", "-r", recipient, "-o", str(out), str(src),
    )


def backup_keys(now: datetime) -> list[str]:
    stamp = now.strftime("%Y%m%dT%H%M")
    keys = [f"daily/inkling-{stamp}.dump.gpg"]
    if now.weekday() == 6:  # Sunday
        keys.append(f"weekly/inkling-{stamp}.dump.gpg")
    if now.day == 1:
        keys.append(f"monthly/inkling-{stamp}.dump.gpg")
    return keys


async def prune(s3: Any, *, retention: dict[str, int] | None = None) -> list[str]:
    removed = []
    for prefix, keep in (retention or RETENTION).items():
        keys = await s3.list(prefix)
        for key in keys[:-keep] if len(keys) > keep else []:
            await s3.delete(key)
            removed.append(key)
    return removed


async def export_markdown(
    db: Database,
    *,
    user_ids: list[str],
    export_dir: Path,
    now: datetime,
    git: bool = True,
) -> list[str]:
    """Current docs + a last-24h events digest per user, committed to git.

    This is §13.2's human-readable layer (and where §13.1's doc diffs become
    reviewable: each nightly commit diffs against the last).
    """
    written = []
    for user_id in user_ids:
        user_dir = export_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        async with db.with_tenant(user_id) as conn:
            docs = await docs_q.current_many(conn, list(DOC_KINDS))
            day_events = await events_q.between(
                conn, start=now - timedelta(hours=24), end=now, limit=500
            )
        for kind in DOC_KINDS:
            row = docs.get(kind)
            if row is None:
                continue
            path = user_dir / f"{kind}.md"
            path.write_text(
                f"<!-- v{row['version']} · {row['written_by']} · exported "
                f"{now.date().isoformat()} -->\n{row['content']}\n"
            )
            written.append(str(path.relative_to(export_dir)))
        digest = user_dir / "events-last-24h.md"
        lines = [
            f"- {e['occurred_at'].isoformat()} **{e['kind']}**"
            + (f" {e['habit_id']}" if e["habit_id"] else "")
            + (f": {e['note']}" if e["note"] else "")
            for e in day_events
        ]
        digest.write_text(
            f"# Events {now.date().isoformat()} (last 24h)\n\n"
            + ("\n".join(lines) or "(none)")
            + "\n"
        )
        written.append(str(digest.relative_to(export_dir)))

    if git and (export_dir / ".git").exists():
        await _run("git", "-C", str(export_dir), "add", "-A")
        diff = await asyncio.create_subprocess_exec(
            "git", "-C", str(export_dir), "diff", "--cached", "--quiet"
        )
        if await diff.wait() != 0:  # something staged
            await _run(
                "git", "-C", str(export_dir), "commit",
                "-m", f"nightly export {now.date().isoformat()}",
            )
            remotes = await asyncio.create_subprocess_exec(
                "git", "-C", str(export_dir), "remote",
                stdout=asyncio.subprocess.PIPE,
            )
            out, _ = await remotes.communicate()
            if out.strip():
                await _run("git", "-C", str(export_dir), "push")
    return written


async def run_backup(
    db: Database,
    *,
    database_url: str,
    gpg_recipient: str,
    s3: Any,
    user_ids: list[str],
    export_dir: str,
    workdir: Path,
    dump: Step = default_dump,
    encrypt: Step = default_encrypt,
    alert: Callable[[str], Awaitable[None]] | None = None,
    now: datetime | None = None,
) -> BackupResult:
    now = now or datetime.now(timezone.utc)
    result = BackupResult(ok=False)
    try:
        if s3 is None:
            raise BackupError("no S3 target configured (BACKUP_S3_*)")
        if not gpg_recipient:
            raise BackupError("no BACKUP_GPG_RECIPIENT configured")

        dump_path = workdir / "inkling.dump"
        encrypted_path = workdir / "inkling.dump.gpg"
        await dump(database_url, dump_path)
        await encrypt(gpg_recipient, dump_path, encrypted_path)
        data = encrypted_path.read_bytes()
        result.checksum = hashlib.sha256(data).hexdigest()
        result.dump_bytes = len(data)

        for key in backup_keys(now):
            await s3.put(key, data)
            result.uploaded.append(key)
        await s3.put(result.uploaded[0] + ".sha256", result.checksum.encode())
        result.pruned = await prune(s3)

        if export_dir:
            result.exported = await export_markdown(
                db, user_ids=user_ids, export_dir=Path(export_dir), now=now
            )

        async with db.system() as conn:
            await conn.execute(
                "INSERT INTO audit_log (actor, action, detail) "
                "VALUES ('job:backup', 'backup_ok', %s)",
                (
                    json.dumps({
                        "bytes": result.dump_bytes,
                        "sha256": result.checksum,
                        "keys": result.uploaded,
                        "pruned": result.pruned,
                    }),
                ),
            )
        result.ok = True
        log.info(
            "backup ok: %d bytes, sha256 %s, keys %s",
            result.dump_bytes, result.checksum[:12], result.uploaded,
        )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"[:500]
        log.exception("backup failed")
        async with db.system() as conn:
            await conn.execute(
                "INSERT INTO audit_log (actor, action, detail) "
                "VALUES ('job:backup', 'backup_failed', %s)",
                (json.dumps({"error": result.error}),),
            )
        if alert is not None:
            try:
                await alert(f"backup FAILED: {result.error}")
            except Exception:
                log.exception("backup failure alert also failed")
    finally:
        for path in (workdir / "inkling.dump", workdir / "inkling.dump.gpg"):
            path.unlink(missing_ok=True)
    return result
