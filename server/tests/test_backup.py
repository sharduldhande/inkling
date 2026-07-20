"""Backup (§13.2) and restore drill (§13.3): pipeline, retention, alerts.

The subprocess edges (pg_dump, gpg, pg_restore) are stubbed — what's under
test is every decision the jobs make around them: ordering, checksums,
retention pruning, git export, alerting, audit rows, and the runner's clock.
"""

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest_asyncio

from inkling.db.client import Database
from inkling.jobs.backup import backup_keys, export_markdown, prune, run_backup
from inkling.jobs.restore_drill import COUNTED_TABLES, run_drill
from inkling.jobs.runner import BACKUP_AT, DRILL_AT, JobsRunner, next_run
from inkling.lib.s3 import StubS3, sign_v4

from tests.conftest import USER_A, USER_B

UTC = timezone.utc
T = datetime(2026, 7, 17, 3, 10, tzinfo=UTC)  # a Friday


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log, "
            "events, docs RESTART IDENTITY CASCADE"
        )
        conn.commit()
    yield


async def _stub_dump(database_url: str, out: Path) -> None:
    out.write_bytes(b"FAKE-PG-DUMP " + database_url.encode()[:20])


async def _stub_encrypt(recipient: str, src: Path, out: Path) -> None:
    out.write_bytes(b"GPG:" + recipient.encode() + b":" + src.read_bytes())


async def _failing_dump(database_url: str, out: Path) -> None:
    raise RuntimeError("pg_dump: connection refused")


async def _audit_rows(db: Database, action: str):
    async with db.system() as conn:
        cur = await conn.execute(
            "SELECT * FROM audit_log WHERE action = %s ORDER BY id", (action,)
        )
        return await cur.fetchall()


# --- backup pipeline (§13.2) ------------------------------------------------


async def test_backup_dumps_encrypts_uploads_and_audits(db: Database, tmp_path):
    s3 = StubS3()

    result = await run_backup(
        db, database_url="postgres://x", gpg_recipient="ops@example.com",
        s3=s3, user_ids=[], export_dir="", workdir=tmp_path,
        dump=_stub_dump, encrypt=_stub_encrypt, now=T,
    )

    assert result.ok
    key = "daily/inkling-20260717T0310.dump.gpg"
    assert result.uploaded == [key]
    assert s3.store[key].startswith(b"GPG:ops@example.com:FAKE-PG-DUMP")
    assert s3.store[key + ".sha256"].decode() == result.checksum
    audits = await _audit_rows(db, "backup_ok")
    assert len(audits) == 1
    assert audits[0]["detail"]["sha256"] == result.checksum
    assert audits[0]["detail"]["bytes"] == result.dump_bytes
    assert not (tmp_path / "inkling.dump").exists()  # plaintext cleaned up


def test_backup_keys_promote_weekly_and_monthly():
    sunday = datetime(2026, 7, 19, 3, 10, tzinfo=UTC)
    first = datetime(2026, 8, 1, 3, 10, tzinfo=UTC)
    assert [k.split("/")[0] for k in backup_keys(T)] == ["daily"]
    assert [k.split("/")[0] for k in backup_keys(sunday)] == ["daily", "weekly"]
    assert [k.split("/")[0] for k in backup_keys(first)] == ["daily", "monthly"]


async def test_retention_prunes_oldest_beyond_policy():
    s3 = StubS3()
    for day in range(1, 11):  # 10 dailies, keep 7
        s3.store[f"daily/inkling-202607{day:02d}T0310.dump.gpg"] = b"x"
    s3.store["weekly/inkling-w1.dump.gpg"] = b"x"  # under the cap: untouched

    removed = await prune(s3)

    assert removed == [
        "daily/inkling-20260701T0310.dump.gpg",
        "daily/inkling-20260702T0310.dump.gpg",
        "daily/inkling-20260703T0310.dump.gpg",
    ]
    assert len([k for k in s3.store if k.startswith("daily/")]) == 7
    assert "weekly/inkling-w1.dump.gpg" in s3.store


async def test_backup_failure_alerts_operator_and_audits(db: Database, tmp_path):
    alerts = []

    async def alert(msg: str) -> None:
        alerts.append(msg)

    result = await run_backup(
        db, database_url="postgres://x", gpg_recipient="ops@example.com",
        s3=StubS3(), user_ids=[], export_dir="", workdir=tmp_path,
        dump=_failing_dump, encrypt=_stub_encrypt, alert=alert, now=T,
    )

    assert not result.ok
    assert "connection refused" in result.error
    assert alerts and "backup FAILED" in alerts[0]
    assert len(await _audit_rows(db, "backup_failed")) == 1


async def test_unconfigured_s3_fails_loudly_not_silently(db: Database, tmp_path):
    alerts = []

    async def alert(msg: str) -> None:
        alerts.append(msg)

    result = await run_backup(
        db, database_url="postgres://x", gpg_recipient="ops@example.com",
        s3=None, user_ids=[], export_dir="", workdir=tmp_path,
        dump=_stub_dump, encrypt=_stub_encrypt, alert=alert, now=T,
    )

    assert not result.ok and "no S3 target" in result.error
    assert alerts


# --- markdown export (§13.2) ------------------------------------------------


async def test_export_writes_docs_and_digest_and_commits(db: Database, tmp_path):
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=export_dir, check=True)
    subprocess.run(
        ["git", "-C", str(export_dir), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(export_dir), "config", "user.name", "t"], check=True
    )
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO docs (user_id, kind, version, content, written_by) "
            "VALUES (%s, 'working_state', 3, '- essay draft', 'agent:turn:9')",
            (USER_A,),
        )
        await conn.execute(
            "INSERT INTO events (user_id, occurred_at, kind, note) "
            "VALUES (%s, now() - interval '2 hours', 'habit_done', 'gym')",
            (USER_A,),
        )

    written = await export_markdown(
        db, user_ids=[USER_A, USER_B], export_dir=export_dir,
        now=datetime.now(UTC),
    )

    ws = (export_dir / USER_A / "working_state.md").read_text()
    assert "- essay draft" in ws
    assert "v3" in ws  # provenance header
    digest = (export_dir / USER_A / "events-last-24h.md").read_text()
    assert "habit_done" in digest and "gym" in digest
    assert f"{USER_B}/events-last-24h.md" in written  # every user exports
    log_out = subprocess.run(
        ["git", "-C", str(export_dir), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "nightly export" in log_out


# --- restore drill (§13.3) --------------------------------------------------


def _drill_stubs(live: dict[str, int], restored: dict[str, int]):
    async def decrypt(src: Path, out: Path) -> None:
        out.write_bytes(src.read_bytes().removeprefix(b"GPG:"))

    restores = []

    async def restore(url: str, dump: Path) -> None:
        restores.append(url)

    async def counts(url: str, tables) -> dict[str, int]:
        base = live if url == "postgres://live" else restored
        return {t: base.get(t, 0) for t in tables}

    return decrypt, restore, counts, restores


async def test_drill_restores_latest_and_verifies_counts(db: Database, tmp_path):
    s3 = StubS3()
    s3.store["daily/inkling-20260716T0310.dump.gpg"] = b"GPG:old"
    s3.store["daily/inkling-20260717T0310.dump.gpg"] = b"GPG:new"
    import hashlib

    s3.store["daily/inkling-20260717T0310.dump.gpg.sha256"] = (
        hashlib.sha256(b"GPG:new").hexdigest().encode()
    )
    live = {t: 5 for t in COUNTED_TABLES}
    decrypt, restore, counts, restores = _drill_stubs(live, dict(live))

    result = await run_drill(
        db, s3=s3, live_url="postgres://live", scratch_url="postgres://scratch",
        workdir=tmp_path, decrypt=decrypt, restore=restore, counts=counts,
    )

    assert result.ok
    assert result.key == "daily/inkling-20260717T0310.dump.gpg"  # the latest
    assert restores == ["postgres://scratch"]
    async with db.system() as conn:
        cur = await conn.execute(
            "SELECT * FROM audit_log WHERE action = 'restore_drill_ok'"
        )
        assert len(await cur.fetchall()) == 1


async def test_drill_fails_on_checksum_mismatch(db: Database, tmp_path):
    s3 = StubS3()
    s3.store["daily/inkling-20260717T0310.dump.gpg"] = b"GPG:corrupted"
    s3.store["daily/inkling-20260717T0310.dump.gpg.sha256"] = b"0" * 64
    alerts = []

    async def alert(msg: str) -> None:
        alerts.append(msg)

    decrypt, restore, counts, _ = _drill_stubs({}, {})
    result = await run_drill(
        db, s3=s3, live_url="postgres://live", scratch_url="postgres://scratch",
        workdir=tmp_path, decrypt=decrypt, restore=restore, counts=counts,
        alert=alert,
    )

    assert not result.ok and "checksum mismatch" in result.error
    assert alerts and "restore drill FAILED" in alerts[0]


async def test_drill_fails_on_empty_restored_table(db: Database, tmp_path):
    s3 = StubS3()
    s3.store["daily/inkling-20260717T0310.dump.gpg"] = b"GPG:new"
    live = {t: 5 for t in COUNTED_TABLES}
    restored = dict(live)
    restored["events"] = 0  # live has 5, restore has none: the dump is broken
    decrypt, restore, counts, _ = _drill_stubs(live, restored)

    result = await run_drill(
        db, s3=s3, live_url="postgres://live", scratch_url="postgres://scratch",
        workdir=tmp_path, decrypt=decrypt, restore=restore, counts=counts,
    )

    assert not result.ok and "row-count drift" in result.error
    assert "events" in result.error


# --- the runner's clock (§13.2/3 timing) ------------------------------------


def test_next_run_daily_with_boot_catchup():
    before = datetime(2026, 7, 17, 2, 0, tzinfo=UTC)
    just_after = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)
    long_after = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
    assert next_run(before, BACKUP_AT) == T
    assert next_run(just_after, BACKUP_AT) == T  # within 6h: run late
    assert next_run(long_after, BACKUP_AT) == T + timedelta(days=1)


def test_next_run_monthly_targets_the_first():
    mid_month = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    assert next_run(mid_month, DRILL_AT, monthly=True) == datetime(
        2026, 8, 1, 4, 10, tzinfo=UTC
    )
    first_early = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
    assert next_run(first_early, DRILL_AT, monthly=True) == datetime(
        2026, 8, 1, 4, 10, tzinfo=UTC
    )


async def test_runner_tick_runs_each_job_once_per_period():
    ran = []

    async def backup(workdir: Path) -> None:
        ran.append("backup")

    async def drill(workdir: Path) -> None:
        ran.append("drill")

    runner = JobsRunner(backup=backup, drill=drill)
    at = datetime(2026, 8, 1, 4, 30, tzinfo=UTC)  # past both windows on the 1st

    await runner.tick(at)
    await runner.tick(at + timedelta(minutes=5))  # same period: no re-run

    assert ran == ["backup", "drill"]

    await runner.tick(at + timedelta(days=1))  # next day: backup only
    assert ran == ["backup", "drill", "backup"]


# --- SigV4 signing sanity ----------------------------------------------------


def test_sign_v4_is_deterministic_and_shaped_right():
    headers = sign_v4(
        method="PUT",
        url="https://s3.example.com/bucket/daily/x.gpg",
        region="us-east-1",
        access_key="AKIA_TEST",
        secret_key="secret",
        payload_hash="abc123",
        now=datetime(2026, 7, 17, 3, 10, tzinfo=UTC),
    )
    assert headers["x-amz-date"] == "20260717T031000Z"
    assert headers["Authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIA_TEST/20260717/us-east-1/s3/aws4_request"
    )
    again = sign_v4(
        method="PUT",
        url="https://s3.example.com/bucket/daily/x.gpg",
        region="us-east-1",
        access_key="AKIA_TEST",
        secret_key="secret",
        payload_hash="abc123",
        now=datetime(2026, 7, 17, 3, 10, tzinfo=UTC),
    )
    assert headers == again


def test_region_from_endpoint():
    from inkling.lib.s3 import region_from_endpoint

    assert region_from_endpoint("https://s3.us-east-005.backblazeb2.com") == "us-east-005"
    assert region_from_endpoint("https://s3.eu-central-003.backblazeb2.com") == "eu-central-003"
    # vendors without a region host segment fall back to the SigV4 default
    assert region_from_endpoint("https://accountid.r2.cloudflarestorage.com") == "us-east-1"
    assert region_from_endpoint("http://localhost:9000") == "us-east-1"