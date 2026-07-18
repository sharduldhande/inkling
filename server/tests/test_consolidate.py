"""Nightly consolidation (§13.1): job scheduling, job-only commands, routing.

Runs as runtime_role against the Neon test branch.
"""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest_asyncio

from inkling.db.client import Database
from inkling.db.queries import facts as facts_q
from inkling.db.queries import users as users_q
from inkling.inference.gateway import StubGateway, TurnResult
from inkling.jobs.consolidate import CONSOLIDATION_PROMPT, is_consolidation_trigger
from inkling.scheduler.scheduler import Scheduler
from inkling.turns import ingress
from inkling.turns.commands import parse_commands
from inkling.turns.commit import commit_turn
from inkling.turns.validate import validate_commands
from inkling.turns.worker import Worker

from tests.conftest import USER_A

UTC = timezone.utc
# 14:00 EDT: today's 02:30 local (06:30 UTC) is >6h gone — outside catch-up.
T_AFTERNOON = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)
# 03:00 EDT: 30 min after 02:30 local, inside catch-up AND inside quiet hours.
T_NIGHT = datetime(2026, 7, 17, 7, 0, tzinfo=UTC)
# Tomorrow's 02:30 EDT.
T_TOMORROW_0230 = datetime(2026, 7, 18, 6, 30, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log, "
            "events, docs, commitment_occurrences, commitments, "
            "scheduled_wakeups, deliveries, memory_facts "
            "RESTART IDENTITY CASCADE"
        )
        conn.commit()
    yield


async def _rows(db: Database, user_id: str, sql: str, args=()):
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


async def _run_job_turn(db: Database, raw_commands: list[dict], text: str = "done"):
    """Drive a consolidation trigger through commit_turn, the production path."""
    sub = await ingress.submit(
        db, user_id=USER_A, source="job",
        payload={"job": "consolidate", "text": "nightly"},
    )
    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute("SELECT * FROM triggers WHERE id = %s", (sub.trigger_id,))
        trigger = await cur.fetchone()
        cur = await conn.execute(
            "INSERT INTO turns (user_id, trigger_id, status) "
            "VALUES (%s, %s, 'running') RETURNING id",
            (USER_A, sub.trigger_id),
        )
        turn_id = int((await cur.fetchone())["id"])
    result = TurnResult(
        text=text, commands=parse_commands(raw_commands),
        model="stub", input_tokens=0, output_tokens=0,
    )
    events = await commit_turn(
        db, user_id=USER_A, trigger=trigger, turn_id=turn_id,
        result=result, next_seq=1, actor="test",
    )
    return turn_id, events


# --- scheduling (§10 job times) ---------------------------------------------


async def test_nightly_job_fires_at_0230_local_next_day(db: Database):
    sched = Scheduler(db, [USER_A])

    await sched.refresh(now=T_AFTERNOON)
    assert await sched.fire_due(now=T_AFTERNOON) == 0  # tonight's is tomorrow
    assert await sched.fire_due(now=T_TOMORROW_0230) == 1

    trigger = (await _rows(db, USER_A, "SELECT * FROM triggers"))[0]
    assert trigger["source"] == "job"
    assert trigger["priority"] == 4
    assert trigger["source_ref"] == "consolidate:2026-07-18"
    assert trigger["payload"]["job"] == "consolidate"
    assert trigger["payload"]["late"] is False


async def test_boot_inside_catchup_runs_tonights_pass_late_despite_quiet(db: Database):
    """03:00 local is inside quiet hours; jobs are exempt (silent maintenance)."""
    sched = Scheduler(db, [USER_A])

    await sched.refresh(now=T_NIGHT)
    assert await sched.fire_due(now=T_NIGHT) == 1

    trigger = (await _rows(db, USER_A, "SELECT * FROM triggers"))[0]
    assert trigger["source_ref"] == "consolidate:2026-07-17"
    assert trigger["payload"]["late"] is True


async def test_one_consolidation_per_local_date(db: Database):
    sched = Scheduler(db, [USER_A])
    await sched.refresh(now=T_NIGHT)
    await sched.fire_due(now=T_NIGHT)

    # Every later refresh re-proposes the same instance; the key absorbs it.
    await sched.refresh(now=T_NIGHT + timedelta(minutes=30))
    assert await sched.fire_due(now=T_NIGHT + timedelta(minutes=30)) == 0
    assert len(await _rows(db, USER_A, "SELECT 1 FROM triggers")) == 1


# --- job-only command gating (§13.1 via §9.2) -------------------------------


async def test_update_doc_is_rejected_outside_consolidation(db: Database):
    commands = parse_commands([
        {"type": "UpdateDoc", "kind": "patterns", "new_content": "x", "reason": "r"},
        {"type": "UpsertMemoryFact", "subject": "user", "predicate": "likes", "value": "tea"},
    ])
    async with db.with_tenant(USER_A) as conn:
        user = await users_q.get(conn, USER_A)
        verdict = await validate_commands(conn, user=user, commands=commands)

    assert verdict.accepted == []
    assert [r.reason for r in verdict.rejected] == [
        "only the consolidation job may rewrite patterns",
        "only the consolidation job may write memory_facts",
    ]


async def test_doc_caps_and_confidence_bounds_hold_even_in_consolidation(db: Database):
    commands = parse_commands([
        {"type": "UpdateDoc", "kind": "digest7", "new_content": "w " * 2000, "reason": "r"},
        {"type": "UpsertMemoryFact", "subject": "u", "predicate": "p", "value": "v",
         "confidence": 1.5},
    ])
    async with db.with_tenant(USER_A) as conn:
        user = await users_q.get(conn, USER_A)
        verdict = await validate_commands(
            conn, user=user, commands=commands, consolidation=True
        )

    assert verdict.accepted == []
    assert "over its 500 cap" in verdict.rejected[0].reason
    assert "not in [0, 1]" in verdict.rejected[1].reason


async def test_consolidation_turn_writes_docs_and_facts(db: Database):
    await _run_job_turn(db, [
        {"type": "UpdateDoc", "kind": "digest7",
         "new_content": "Quiet week; gym 3x.", "reason": "nightly digest"},
        {"type": "UpdateDoc", "kind": "patterns",
         "new_content": "Skips gym when travelling.", "reason": "confirmed again"},
        {"type": "UpsertMemoryFact", "subject": "user", "predicate": "prefers",
         "value": "morning workouts", "confidence": 0.8},
        {"type": "UpdateWorkingState", "new_content": "# Life\n- nothing open",
         "reason": "consolidation: closed the week's loops"},
    ])

    docs = await _rows(
        db, USER_A, "SELECT kind, version, content FROM docs ORDER BY kind"
    )
    assert [(d["kind"], d["version"]) for d in docs] == [
        ("digest7", 1), ("patterns", 1), ("working_state", 1)
    ]
    facts = await _rows(db, USER_A, "SELECT * FROM memory_facts")
    assert len(facts) == 1
    assert facts[0]["value"] == "morning workouts"
    applied = await _rows(
        db, USER_A,
        "SELECT detail FROM audit_log WHERE action = 'command_applied'",
    )
    assert len(applied) == 4


# --- fact upsert semantics --------------------------------------------------


async def test_fact_upsert_confirms_replaces_and_preserves_history(db: Database):
    async with db.with_tenant(USER_A) as conn:
        first = await facts_q.upsert(
            conn, user_id=USER_A, subject="user", predicate="lives_in",
            value="Atlanta", condition=None, confidence=0.6,
        )
        confirmed = await facts_q.upsert(
            conn, user_id=USER_A, subject="User", predicate="lives_in",
            value="atlanta", condition=None, confidence=0.9,
        )
        replaced = await facts_q.upsert(
            conn, user_id=USER_A, subject="user", predicate="lives_in",
            value="Boston", condition=None, confidence=0.7,
        )

    assert first["action"] == "created"
    assert confirmed == {"fact_id": first["fact_id"], "action": "confirmed"}
    assert replaced["action"] == "replaced"
    assert replaced["invalidated"] == [first["fact_id"]]

    rows = await _rows(db, USER_A, "SELECT * FROM memory_facts ORDER BY id")
    assert rows[0]["invalidated_at"] is not None  # history, not an edit
    assert rows[0]["last_confirmed"] is not None
    assert rows[0]["confidence"] == 0.9
    assert rows[1]["value"] == "Boston"
    assert rows[1]["invalidated_at"] is None


# --- worker routing ---------------------------------------------------------


def test_trigger_detection():
    assert is_consolidation_trigger(
        {"source": "job", "payload": {"job": "consolidate"}}
    )
    assert not is_consolidation_trigger({"source": "job", "payload": {"job": "backup"}})
    assert not is_consolidation_trigger({"source": "chat", "payload": {"text": "hi"}})


async def test_consolidation_turn_gets_its_own_prompt_and_day_context(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO transcript (user_id, role, text) VALUES (%s, 'user', %s)",
            (USER_A, "finished the essay draft tonight"),
        )
        await conn.execute(
            "INSERT INTO docs (user_id, kind, version, content, written_by) "
            "VALUES (%s, 'working_state', 1, '- essay draft', 'human')",
            (USER_A,),
        )
    await ingress.submit(
        db, user_id=USER_A, source="job",
        payload={"job": "consolidate", "text": "nightly"},
    )
    gateway = StubGateway(reply="consolidated")
    worker = Worker(db, USER_A, gateway)

    assert await worker.tick() is True

    call = gateway.calls[0]
    assert call["system"] == CONSOLIDATION_PROMPT
    assert "## Transcript (last 24h)" in call["context"]
    assert "finished the essay draft tonight" in call["context"]
    assert "## Working state (current)\n- essay draft" in call["context"]


async def test_consolidation_turn_can_recall_beyond_its_24h_window(db: Database):
    """The prompt tells it to; the plumbing must actually deliver (recall
    handlers are wired per turn regardless of trigger source)."""
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO events (user_id, occurred_at, kind, habit_id, note) "
            "VALUES (%s, now() - interval '6 days', 'habit_done', 'gym', 'lifted heavy')",
            (USER_A,),
        )
    await ingress.submit(
        db, user_id=USER_A, source="job",
        payload={"job": "consolidate", "text": "nightly"},
    )
    gateway = StubGateway(
        reply="digest updated", recalls=[{"query": "gym lifted"}]
    )

    assert await Worker(db, USER_A, gateway).tick() is True

    assert gateway.calls[0]["system"] == CONSOLIDATION_PROMPT
    assert "recall" in CONSOLIDATION_PROMPT  # the instruction exists
    [result] = gateway.calls[0]["recall_results"]
    assert "lifted heavy" in result  # a 6-day-old event, outside the window


async def test_over_cap_working_state_leads_to_a_consolidation_turn(db: Database):
    """§8 enqueues the job; the worker then runs it with the §13.1 prompt."""
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO docs (user_id, kind, version, content, written_by) "
            "VALUES (%s, 'working_state', 1, %s, 'human')",
            (USER_A, "- open loop\n" * 500),
        )
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})
    gateway = StubGateway(reply="ok")
    worker = Worker(db, USER_A, gateway)

    assert await worker.tick() is True  # chat turn; §8 assembly queues the job
    assert await worker.tick() is True  # the queued consolidation turn

    assert gateway.calls[1]["system"] == CONSOLIDATION_PROMPT
    assert "exceeds its 1200-token cap" in gateway.calls[1]["context"]