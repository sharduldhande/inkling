"""Model-directed recall (post-spec amendment): the read tool + §8 slimming.

Runs as runtime_role against the Neon test branch.
"""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest_asyncio

from inkling.db.client import Database
from inkling.inference.gateway import StubGateway
from inkling.turns import ingress
from inkling.turns.context import assemble_context, references_past
from inkling.turns.recall import (
    MAX_RECALLS_PER_TURN,
    make_recall_handler,
    run_recall,
)
from inkling.turns.worker import Worker

from tests.conftest import USER_A, USER_B

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log, "
            "events, docs, commitment_occurrences, commitments, "
            "scheduled_wakeups, deliveries RESTART IDENTITY CASCADE"
        )
        conn.commit()
    yield


async def _seed_event(
    db: Database, user_id: str, note: str, *, kind: str = "note",
    value_num: float | None = None, days_ago: int = 10,
):
    async with db.with_tenant(user_id) as conn:
        await conn.execute(
            "INSERT INTO events (user_id, occurred_at, kind, value_num, unit, note) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, NOW - timedelta(days=days_ago), kind,
             value_num, "cal" if value_num else None, note),
        )


# --- the recall query itself -------------------------------------------------


async def test_recall_finds_events_and_transcript_by_query(db: Database):
    await _seed_event(db, USER_A, "ran the Peachtree race in 55 minutes")
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO transcript (user_id, role, text) "
            "VALUES (%s, 'assistant', 'great Peachtree result!')",
            (USER_A,),
        )

    result = await run_recall(db, USER_A, {"query": "peachtree race"})

    assert "ran the Peachtree race in 55 minutes" in result
    assert "assistant said: great Peachtree result!" in result


async def test_recall_is_tenant_scoped(db: Database):
    await _seed_event(db, USER_B, "ran the Peachtree race")

    assert await run_recall(db, USER_A, {"query": "peachtree"}) == (
        "nothing recalled for that query"
    )


async def test_recall_filters_kinds_and_dates(db: Database):
    await _seed_event(db, USER_A, "burrito", kind="measurement",
                      value_num=800, days_ago=2)
    await _seed_event(db, USER_A, "thinking about burritos", kind="note", days_ago=2)
    await _seed_event(db, USER_A, "burrito", kind="measurement",
                      value_num=700, days_ago=40)

    only_measurements = await run_recall(
        db, USER_A, {"query": "burrito", "kinds": ["measurement"]}
    )
    assert "measurement" in only_measurements
    assert "thinking about" not in only_measurements

    recent = await run_recall(
        db, USER_A,
        {"query": "burrito", "kinds": ["measurement"],
         "since": (NOW - timedelta(days=7)).isoformat()},
    )
    assert "800" in recent and "700" not in recent


async def test_recall_date_range_without_query_lists_chronologically(db: Database):
    await _seed_event(db, USER_A, "gym", kind="habit_done", days_ago=3)
    await _seed_event(db, USER_A, "old gym", kind="habit_done", days_ago=60)

    result = await run_recall(
        db, USER_A,
        {"query": "", "since": (NOW - timedelta(days=7)).isoformat()},
    )

    assert "gym" in result and "old gym" not in result


async def test_recall_without_query_or_range_asks_for_one(db: Database):
    assert "needs a query or a date range" in await run_recall(db, USER_A, {"query": ""})


# --- the handler: budget + degradation ---------------------------------------


async def test_handler_enforces_the_per_turn_budget(db: Database):
    await _seed_event(db, USER_A, "something")
    handler = make_recall_handler(db, USER_A)

    for _ in range(MAX_RECALLS_PER_TURN):
        assert "budget" not in await handler({"query": "something"})
    over = await handler({"query": "something"})

    assert "recall budget exhausted" in over


async def test_handler_degrades_errors_to_honest_text(db: Database):
    handler = make_recall_handler(db, USER_A)

    # since is unparseable AND the query is empty → run_recall's error path
    # is exercised via a genuinely broken call shape.
    result = await handler({"query": None, "kinds": 7})

    assert "work with what you have" in result or "needs a query" in result


# --- worker wiring ------------------------------------------------------------


async def test_worker_wires_recall_into_the_turn(db: Database):
    await _seed_event(db, USER_A, "ran the Peachtree race in 55 minutes")
    gateway = StubGateway(
        reply="You ran it in 55 minutes.",
        recalls=[{"query": "peachtree time"}],
    )
    await ingress.submit(
        db, user_id=USER_A, source="chat",
        payload={"text": "how fast was my Peachtree?"},
    )

    assert await Worker(db, USER_A, gateway).tick() is True

    [result] = gateway.calls[0]["recall_results"]
    assert "55 minutes" in result
    assert [t.name for t in gateway.calls[0]["tools"]] == ["emit_commands", "recall"]


# --- §8 slimming: measurements out of ambient context -------------------------


async def test_todays_measurements_collapse_to_one_line(db: Database):
    for note, cal in (("burrito", 800), ("oatmeal", 350), ("smoothie", 400)):
        await _seed_event(db, USER_A, note, kind="measurement",
                          value_num=cal, days_ago=0)
    await _seed_event(db, USER_A, "finished essay draft", kind="checkin", days_ago=0)

    got = await assemble_context(
        db, user_id=USER_A,
        trigger={"id": 1, "source": "chat", "source_ref": None,
                 "payload": {"text": "hows my day"}},
    )

    assert "finished essay draft" in got.context  # loop events still ambient
    assert "burrito" not in got.context  # food is memory, not ambient context
    assert "measurements: 3 logged today" in got.context


def test_gate_treats_food_and_exercise_words_as_recall_cues():
    assert references_past("how many calories today?")
    assert references_past("what should I eat for dinner")
    assert references_past("weekly workout check")
    assert not references_past("move the dentist appointment to friday")