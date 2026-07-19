"""Command validation and application (§9.2).

Runs as runtime_role against the Neon test branch, the shape production uses —
so these tests also prove the §4 grants actually permit what apply.py does.
"""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest
import pytest_asyncio

from inkling.db.client import Database
from inkling.db.queries import users as users_q
from inkling.turns import ingress
from inkling.turns.commands import parse_commands
from inkling.turns.commit import commit_turn
from inkling.turns.validate import (
    WORKING_STATE_TOKEN_CAP,
    _has_unresolved_children,
    estimate_tokens,
    validate_commands,
)
from inkling.inference.gateway import TurnResult

from tests.conftest import USER_A, USER_B


def _set_calendar_enabled(dsn: str, user_id: str, enabled: bool) -> None:
    """FORCE RLS applies to the owner too: the tenant must be set to write."""
    with psycopg.connect(dsn) as conn:
        conn.execute("BEGIN")
        conn.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
        cur = conn.execute(
            "UPDATE users SET calendar_enabled = %s WHERE id = %s RETURNING id",
            (enabled, user_id),
        )
        assert cur.fetchone() is not None, "RLS silently swallowed the update"
        conn.execute("COMMIT")


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    """Empty the domain tables between tests (owner conn: no DELETE grant, D10)."""
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log, "
            "events, habits, deliveries, commitment_occurrences, commitments, "
            "docs, scheduled_wakeups RESTART IDENTITY CASCADE"
        )
        conn.commit()
    for uid in (USER_A, USER_B):
        _set_calendar_enabled(dsn, uid, False)
    yield


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _rows(db: Database, user_id: str, sql: str, args=()):
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


async def _validate(db: Database, user_id: str, raw: list[dict]):
    commands = parse_commands(raw)
    assert len(commands) == len(raw), "fixture didn't parse under §9.1"
    async with db.with_tenant(user_id) as conn:
        user = await users_q.get(conn, user_id)
        return await validate_commands(conn, user=user, commands=commands)


async def _run_turn(db: Database, user_id: str, raw: list[dict], text: str = "ok"):
    """Drive a real turn through commit_turn — the production path."""
    sub = await ingress.submit(
        db, user_id=user_id, source="chat", payload={"text": "hello"}
    )
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute(
            "SELECT * FROM triggers WHERE id = %s", (sub.trigger_id,)
        )
        trigger = await cur.fetchone()
        cur = await conn.execute(
            "INSERT INTO turns (user_id, trigger_id, status) "
            "VALUES (%s, %s, 'running') RETURNING id",
            (user_id, sub.trigger_id),
        )
        turn_id = int((await cur.fetchone())["id"])

    result = TurnResult(
        text=text,
        commands=parse_commands(raw),
        model="stub",
        input_tokens=0,
        output_tokens=0,
    )
    events = await commit_turn(
        db,
        user_id=user_id,
        trigger=trigger,
        turn_id=turn_id,
        result=result,
        next_seq=1,
        actor="test",
    )
    return turn_id, events


# --- the closure guard heuristic (§9.2), unit-level -------------------------


def test_closure_guard_flags_open_child_under_matching_parent():
    doc = """
# School
- module 1 exam prep
    - hw2 draft
    - [x] readings
"""
    assert _has_unresolved_children(doc, [None, "module 1 exam prep"]) is True


def test_closure_guard_allows_parent_whose_children_are_all_closed():
    doc = """
# School
- module 1 exam prep
    - [x] hw2 draft
    - ~~readings~~
"""
    assert _has_unresolved_children(doc, [None, "module 1 exam prep"]) is False


def test_closure_guard_ignores_siblings_after_dedent():
    doc = """
- module 1 exam prep
    - [x] hw2
- unrelated open thing
"""
    assert _has_unresolved_children(doc, [None, "module 1 exam prep"]) is False


def test_closure_guard_passes_when_commitment_absent_from_working_state():
    assert _has_unresolved_children("- something else\n", [None, "gym"]) is False


def test_token_estimate_is_roughly_four_chars():
    assert estimate_tokens("a" * 400) == 100


# --- AppendEvent ------------------------------------------------------------


async def test_append_event_applies_and_is_readable(db: Database):
    await _run_turn(
        db,
        USER_A,
        [{"type": "AppendEvent", "kind": "note", "note": "ran 5k"}],
    )
    rows = await _rows(db, USER_A, "SELECT * FROM events")
    assert len(rows) == 1
    assert rows[0]["note"] == "ran 5k"
    assert rows[0]["kind"] == "note"


async def test_append_event_in_the_future_is_rejected(db: Database):
    """Non-note kinds record what happened, never what will (§9.2).

    kind='note' is the §14 exception — day-keyed pacing notes are future-
    dated by design; test_ingestion.py owns that behavior and its caps.
    """
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "AppendEvent",
                "kind": "checkin",
                "note": "will run",
                "occurred_at": (_now() + timedelta(hours=2)).isoformat(),
            }
        ],
    )
    assert verdict.accepted == []
    assert "future" in verdict.rejected[0].reason


async def test_append_event_slightly_in_the_future_is_allowed(db: Database):
    """Clock skew between the model's clock and ours is not an error."""
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "AppendEvent",
                "kind": "note",
                "note": "just ran",
                "occurred_at": (_now() + timedelta(minutes=2)).isoformat(),
            }
        ],
    )
    assert len(verdict.accepted) == 1


async def test_append_event_backdated_is_allowed(db: Database):
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "AppendEvent",
                "kind": "note",
                "note": "ran yesterday",
                "occurred_at": (_now() - timedelta(days=1)).isoformat(),
            }
        ],
    )
    assert len(verdict.accepted) == 1


async def test_append_event_with_unknown_habit_is_rejected(db: Database):
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "AppendEvent",
                "kind": "habit_done",
                "habit_id": "nope",
                "note": "done",
            }
        ],
    )
    assert "unknown habit" in verdict.rejected[0].reason


async def test_append_event_cannot_reference_another_users_habit(db: Database):
    """RLS makes B's habit invisible to A — validation turns that into a reason."""
    async with db.with_tenant(USER_B) as conn:
        await conn.execute(
            "INSERT INTO habits (user_id, id, title) VALUES (%s, 'gym', 'Gym')",
            (USER_B,),
        )
    verdict = await _validate(
        db,
        USER_A,
        [{"type": "AppendEvent", "kind": "habit_done", "habit_id": "gym", "note": "x"}],
    )
    assert "unknown habit" in verdict.rejected[0].reason


# --- UpsertCommitment -------------------------------------------------------


async def test_upsert_creates_commitment_and_first_occurrence(db: Database):
    due = _now() + timedelta(days=1)
    await _run_turn(
        db,
        USER_A,
        [
            {
                "type": "UpsertCommitment",
                "title": "file taxes",
                "due_at": due.isoformat(),
                "lead_time": "PT30M",
            }
        ],
    )
    rows = await _rows(db, USER_A, "SELECT * FROM commitments")
    assert rows[0]["title"] == "file taxes"
    assert rows[0]["status"] == "open"
    assert rows[0]["version"] == 1

    occ = await _rows(db, USER_A, "SELECT * FROM commitment_occurrences")
    assert len(occ) == 1, "an open dated commitment must have an occurrence (§4.6)"
    assert occ[0]["fire_at"] == due - timedelta(minutes=30)


async def test_upsert_without_due_at_creates_no_occurrence(db: Database):
    await _run_turn(db, USER_A, [{"type": "UpsertCommitment", "title": "someday"}])
    assert await _rows(db, USER_A, "SELECT * FROM commitment_occurrences") == []


async def test_upsert_update_bumps_version_and_keeps_omitted_fields(db: Database):
    await _run_turn(
        db,
        USER_A,
        [{"type": "UpsertCommitment", "title": "draft", "body": "the original body"}],
    )
    await _run_turn(
        db,
        USER_A,
        [
            {
                "type": "UpsertCommitment",
                "id": 1,
                "expected_version": 1,
                "title": "draft v2",
            }
        ],
    )
    rows = await _rows(db, USER_A, "SELECT * FROM commitments")
    assert rows[0]["title"] == "draft v2"
    assert rows[0]["body"] == "the original body"
    assert rows[0]["version"] == 2


async def test_upsert_update_with_stale_version_is_rejected(db: Database):
    await _run_turn(db, USER_A, [{"type": "UpsertCommitment", "title": "thing"}])
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "UpsertCommitment",
                "id": 1,
                "expected_version": 7,
                "title": "clobber",
            }
        ],
    )
    assert "current 1" in verdict.rejected[0].reason


async def test_upsert_update_without_expected_version_is_rejected(db: Database):
    await _run_turn(db, USER_A, [{"type": "UpsertCommitment", "title": "thing"}])
    verdict = await _validate(
        db, USER_A, [{"type": "UpsertCommitment", "id": 1, "title": "clobber"}]
    )
    assert "requires expected_version" in verdict.rejected[0].reason


async def test_upsert_with_both_due_at_and_recurrence_is_rejected(db: Database):
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "UpsertCommitment",
                "title": "confused",
                "due_at": _now().isoformat(),
                "recurrence": "FREQ=DAILY",
            }
        ],
    )
    assert "not both" in verdict.rejected[0].reason


async def test_cannot_touch_another_users_commitment(db: Database):
    await _run_turn(db, USER_B, [{"type": "UpsertCommitment", "title": "b's thing"}])
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "UpsertCommitment",
                "id": 1,
                "expected_version": 1,
                "title": "stolen",
            }
        ],
    )
    assert "not found" in verdict.rejected[0].reason
    rows = await _rows(db, USER_B, "SELECT * FROM commitments")
    assert rows[0]["title"] == "b's thing"


# --- CompleteCommitment and the closure guard -------------------------------


async def test_complete_closes_commitment_and_records_why(db: Database):
    await _run_turn(db, USER_A, [{"type": "UpsertCommitment", "title": "gym"}])
    await _run_turn(
        db,
        USER_A,
        [
            {
                "type": "CompleteCommitment",
                "id": 1,
                "expected_version": 1,
                "reason": "went this morning",
            }
        ],
    )
    rows = await _rows(db, USER_A, "SELECT * FROM commitments")
    assert rows[0]["status"] == "done"
    assert rows[0]["version"] == 2

    events = await _rows(db, USER_A, "SELECT * FROM events WHERE kind = 'system'")
    assert "went this morning" in events[0]["note"]


async def test_completing_an_already_done_commitment_is_rejected(db: Database):
    await _run_turn(db, USER_A, [{"type": "UpsertCommitment", "title": "gym"}])
    await _run_turn(
        db,
        USER_A,
        [{"type": "CompleteCommitment", "id": 1, "expected_version": 1, "reason": "y"}],
    )
    verdict = await _validate(
        db,
        USER_A,
        [{"type": "CompleteCommitment", "id": 1, "expected_version": 2, "reason": "y"}],
    )
    assert "already done" in verdict.rejected[0].reason


async def test_complete_is_blocked_while_working_state_shows_open_children(
    db: Database,
):
    await _run_turn(
        db, USER_A, [{"type": "UpsertCommitment", "title": "module 1 exam prep"}]
    )
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO docs (user_id, kind, version, content, written_by) "
            "VALUES (%s, 'working_state', 1, %s, 'human')",
            (USER_A, "# School\n- module 1 exam prep\n    - hw2 still open\n"),
        )
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "CompleteCommitment",
                "id": 1,
                "expected_version": 1,
                "reason": "done I think",
            }
        ],
    )
    assert verdict.rejected[0].reason == "children unresolved — confirm with user"

    rows = await _rows(db, USER_A, "SELECT * FROM commitments")
    assert rows[0]["status"] == "open", "a rejected command must change nothing"


# --- SnoozeCommitment -------------------------------------------------------


async def test_snooze_moves_the_pending_occurrence(db: Database):
    due = _now() + timedelta(hours=1)
    await _run_turn(
        db,
        USER_A,
        [{"type": "UpsertCommitment", "title": "call mum", "due_at": due.isoformat()}],
    )
    later = _now() + timedelta(days=1)
    await _run_turn(
        db,
        USER_A,
        [
            {
                "type": "SnoozeCommitment",
                "id": 1,
                "expected_version": 1,
                "until": later.isoformat(),
            }
        ],
    )
    occ = await _rows(db, USER_A, "SELECT * FROM commitment_occurrences")
    assert len(occ) == 1, "snooze moves the reminder, it doesn't add one"
    assert occ[0]["fire_at"] == later
    assert occ[0]["status"] == "pending"


async def test_snoozing_an_undated_commitment_creates_the_reminder(db: Database):
    """'Remind me later' on an undated commitment must not mean 'never'."""
    await _run_turn(db, USER_A, [{"type": "UpsertCommitment", "title": "someday"}])
    later = _now() + timedelta(days=2)
    await _run_turn(
        db,
        USER_A,
        [
            {
                "type": "SnoozeCommitment",
                "id": 1,
                "expected_version": 1,
                "until": later.isoformat(),
            }
        ],
    )
    occ = await _rows(db, USER_A, "SELECT * FROM commitment_occurrences")
    assert len(occ) == 1
    assert occ[0]["fire_at"] == later


# --- UpdateWorkingState -----------------------------------------------------


async def test_update_working_state_writes_a_new_version(db: Database):
    await _run_turn(
        db,
        USER_A,
        [
            {
                "type": "UpdateWorkingState",
                "new_content": "# School\n- hw2 (as of 2026-07-17)\n",
                "reason": "logged hw2",
            }
        ],
    )
    rows = await _rows(db, USER_A, "SELECT * FROM docs ORDER BY version")
    assert rows[0]["version"] == 1
    assert rows[0]["kind"] == "working_state"
    assert rows[0]["written_by"].startswith("agent:turn:")


async def test_working_state_versions_accumulate_rather_than_overwrite(db: Database):
    for i in range(2):
        await _run_turn(
            db,
            USER_A,
            [
                {
                    "type": "UpdateWorkingState",
                    "new_content": f"# School\n- hw{i} (as of 2026-07-17)\n",
                    "reason": "edit",
                }
            ],
        )
    rows = await _rows(db, USER_A, "SELECT * FROM docs ORDER BY version")
    assert [r["version"] for r in rows] == [1, 2]


async def test_working_state_over_token_cap_is_rejected(db: Database):
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "UpdateWorkingState",
                "new_content": "x " * (WORKING_STATE_TOKEN_CAP * 4),
                "reason": "dump everything",
            }
        ],
    )
    assert "over the" in verdict.rejected[0].reason


async def test_wholesale_rewrite_is_rejected_without_consolidation_reason(
    db: Database,
):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO docs (user_id, kind, version, content, written_by) "
            "VALUES (%s, 'working_state', 1, %s, 'human')",
            (USER_A, "# School\n- module 1\n- module 2\n- module 3\n"),
        )
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "UpdateWorkingState",
                "new_content": "# Nothing to see\n",
                "reason": "tidying up",
            }
        ],
    )
    assert "edit it incrementally" in verdict.rejected[0].reason


async def test_wholesale_rewrite_is_allowed_for_consolidation(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO docs (user_id, kind, version, content, written_by) "
            "VALUES (%s, 'working_state', 1, %s, 'human')",
            (USER_A, "# School\n- module 1\n- module 2\n- module 3\n"),
        )
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "UpdateWorkingState",
                "new_content": "# Nothing to see\n",
                "reason": "consolidation: archived closed loops",
            }
        ],
    )
    assert len(verdict.accepted) == 1


async def test_incremental_edit_is_allowed(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO docs (user_id, kind, version, content, written_by) "
            "VALUES (%s, 'working_state', 1, %s, 'human')",
            (USER_A, "# School\n- module 1\n- module 2\n- module 3\n"),
        )
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "UpdateWorkingState",
                "new_content": "# School\n- module 1\n- module 2\n- module 3\n- module 4\n",
                "reason": "added module 4",
            }
        ],
    )
    assert len(verdict.accepted) == 1


async def test_update_working_state_requires_a_reason(db: Database):
    verdict = await _validate(
        db,
        USER_A,
        [{"type": "UpdateWorkingState", "new_content": "# School\n", "reason": "  "}],
    )
    assert "reason is required" in verdict.rejected[0].reason


async def test_model_cannot_write_the_goals_doc(db: Database):
    """§4.7/§9.1: there is deliberately no command that targets goals."""
    assert parse_commands(
        [{"type": "UpdateDoc", "kind": "goals", "new_content": "mine now"}]
    ) == []


async def test_daily_plan_is_writable_from_a_normal_turn(db: Database):
    """The day-start/presence machinery owns today's plan — no job required."""
    await _run_turn(
        db,
        USER_A,
        [{
            "type": "UpdateDoc", "kind": "daily_plan",
            "new_content": "- improv 11:00\n- ship §14 draft",
            "reason": "first contact: shaped the day",
        }],
    )
    rows = await _rows(
        db, USER_A, "SELECT * FROM docs WHERE kind = 'daily_plan'"
    )
    assert len(rows) == 1
    assert "improv" in rows[0]["content"]


async def test_patterns_doc_stays_consolidation_only(db: Database):
    verdict = await _validate(
        db,
        USER_A,
        [{
            "type": "UpdateDoc", "kind": "patterns",
            "new_content": "rewritten", "reason": "tidying",
        }],
    )
    assert "only the consolidation job" in verdict.rejected[0].reason


async def test_daily_plan_respects_its_token_cap(db: Database):
    verdict = await _validate(
        db,
        USER_A,
        [{
            "type": "UpdateDoc", "kind": "daily_plan",
            "new_content": "plan " * 800, "reason": "day shape",
        }],
    )
    assert "over its" in verdict.rejected[0].reason


# --- wakeups ----------------------------------------------------------------


async def test_schedule_wakeup_by_offset(db: Database):
    before = _now()
    await _run_turn(
        db,
        USER_A,
        [{"type": "ScheduleWakeup", "offset_minutes": 90, "purpose": "check in"}],
    )
    rows = await _rows(db, USER_A, "SELECT * FROM scheduled_wakeups")
    assert rows[0]["status"] == "active"
    assert rows[0]["purpose"] == "check in"
    assert rows[0]["fire_at"] >= before + timedelta(minutes=89)


async def test_schedule_wakeup_needs_exactly_one_of_fire_at_or_offset(db: Database):
    neither = await _validate(
        db, USER_A, [{"type": "ScheduleWakeup", "purpose": "x"}]
    )
    assert "either fire_at or offset_minutes" in neither.rejected[0].reason

    both = await _validate(
        db,
        USER_A,
        [
            {
                "type": "ScheduleWakeup",
                "fire_at": (_now() + timedelta(hours=1)).isoformat(),
                "offset_minutes": 60,
                "purpose": "x",
            }
        ],
    )
    assert "not both" in both.rejected[0].reason


async def test_wakeup_in_the_past_is_rejected(db: Database):
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "ScheduleWakeup",
                "fire_at": (_now() - timedelta(hours=1)).isoformat(),
                "purpose": "x",
            }
        ],
    )
    assert "in the past" in verdict.rejected[0].reason


async def test_cancel_wakeup(db: Database):
    await _run_turn(
        db, USER_A, [{"type": "ScheduleWakeup", "offset_minutes": 30, "purpose": "x"}]
    )
    await _run_turn(db, USER_A, [{"type": "CancelWakeup", "id": 1}])
    rows = await _rows(db, USER_A, "SELECT * FROM scheduled_wakeups")
    assert rows[0]["status"] == "cancelled"


async def test_cancel_unknown_wakeup_is_rejected(db: Database):
    verdict = await _validate(db, USER_A, [{"type": "CancelWakeup", "id": 999}])
    assert "not found" in verdict.rejected[0].reason


async def test_cannot_cancel_another_users_wakeup(db: Database):
    await _run_turn(
        db, USER_B, [{"type": "ScheduleWakeup", "offset_minutes": 30, "purpose": "b"}]
    )
    verdict = await _validate(db, USER_A, [{"type": "CancelWakeup", "id": 1}])
    assert "not found" in verdict.rejected[0].reason
    rows = await _rows(db, USER_B, "SELECT * FROM scheduled_wakeups")
    assert rows[0]["status"] == "active"


# --- Notify and calendar ----------------------------------------------------


async def test_notify_queues_a_delivery_and_sends_nothing(db: Database):
    turn_id, _ = await _run_turn(
        db, USER_A, [{"type": "Notify", "body": "don't forget the thing"}]
    )
    rows = await _rows(db, USER_A, "SELECT * FROM deliveries")
    assert rows[0]["status"] == "queued", "delivery happens after commit, via §11"
    assert rows[0]["sent_at"] is None
    assert rows[0]["destination"] == f"inkling-{USER_A}-test"
    assert rows[0]["idempotency_key"] == f"turn:{turn_id}:notify:0"


async def test_calendar_event_rejected_when_calendar_disabled(db: Database):
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "CreateCalendarEvent",
                "calendar_id": "primary",
                "summary": "lunch",
                "starts_at": (_now() + timedelta(days=1)).isoformat(),
                "ends_at": (_now() + timedelta(days=1, hours=1)).isoformat(),
            }
        ],
    )
    assert "not enabled" in verdict.rejected[0].reason


async def test_calendar_event_rejected_for_absurd_duration(db: Database, dsn: str):
    _set_calendar_enabled(dsn, USER_A, True)
    verdict = await _validate(
        db,
        USER_A,
        [
            {
                "type": "CreateCalendarEvent",
                "calendar_id": "primary",
                "summary": "lunch",
                "starts_at": (_now() + timedelta(days=1)).isoformat(),
                "ends_at": (_now() + timedelta(days=9)).isoformat(),
            }
        ],
    )
    assert "longer than a day" in verdict.rejected[0].reason


# --- how rejection reaches the user (§9.2 honest degradation) ---------------


async def test_rejected_command_notes_the_user_and_does_not_fail_the_turn(
    db: Database,
):
    turn_id, events = await _run_turn(
        db,
        USER_A,
        [
            {"type": "AppendEvent", "kind": "note", "note": "ran 5k"},
            {"type": "CancelWakeup", "id": 404},
        ],
        text="Logged your run and cancelled that reminder.",
    )

    turns = await _rows(db, USER_A, "SELECT * FROM turns WHERE id = %s", (turn_id,))
    assert turns[0]["status"] == "ok", "a rejected command is not a failed turn"

    # The good command still applied.
    assert len(await _rows(db, USER_A, "SELECT * FROM events")) == 1

    transcript = await _rows(
        db, USER_A, "SELECT * FROM transcript WHERE role = 'assistant'"
    )
    assert "(couldn't complete CancelWakeup:" in transcript[0]["text"]
    assert "Logged your run" in transcript[0]["text"]


async def test_rejection_note_reaches_the_live_stream_before_done(db: Database):
    """The deltas already streamed are the model's claim; the correction has to
    reach the same socket, not just the transcript."""
    _, events = await _run_turn(
        db, USER_A, [{"type": "CancelWakeup", "id": 404}], text="Cancelled it."
    )
    assert [e.kind for e in events] == ["text_delta", "done", "exit"]
    assert "couldn't complete CancelWakeup" in events[0].data["text"]
    assert [e.seq for e in events] == [1, 2, 3], "seq must stay contiguous (§5.5)"


async def test_clean_turn_streams_only_done_and_exit(db: Database):
    _, events = await _run_turn(
        db, USER_A, [{"type": "AppendEvent", "kind": "note", "note": "fine"}]
    )
    assert [e.kind for e in events] == ["done", "exit"]
    assert [e.seq for e in events] == [1, 2]


async def test_applied_and_rejected_commands_are_both_audited(db: Database):
    await _run_turn(
        db,
        USER_A,
        [
            {"type": "AppendEvent", "kind": "note", "note": "ran 5k"},
            {"type": "CancelWakeup", "id": 404},
        ],
    )
    rows = await _rows(db, USER_A, "SELECT * FROM audit_log ORDER BY id")
    actions = [r["action"] for r in rows]
    assert actions == ["command_applied", "command_rejected"]
    assert rows[1]["detail"]["reason"] == "wakeup 404 not found"
    assert rows[0]["detail"]["result"]["event_id"] == 1
