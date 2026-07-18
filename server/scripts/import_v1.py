"""v1 → v2 data import (§17 step 3). Run with the OWNER url, once, per user.

Three artifacts leave v1 (habit-enforcer-server):
  transcript.jsonl  — chat history            → transcript rows
  goals.md / daily_plan.md                    → docs (version 1)
  log.md            — the daily log           → events rows

The log.md path is deliberately two-phase ("LLM-assisted parse, HUMAN-
REVIEWED" — §17): `parse` emits a JSON proposal you edit/approve (optionally
after improving it with an LLM pass), `load` inserts only what the reviewed
file says. Nothing goes from log.md to the database without eyes on it.
working_state and initial commitments are NOT imported by script at all: §17
requires manual review of every open loop — draft them by hand (or via a
chat/consolidation turn) after import.

Usage:
  python scripts/import_v1.py transcript --user shardul --file transcript.jsonl
  python scripts/import_v1.py docs --user shardul --goals goals.md --plan daily_plan.md
  python scripts/import_v1.py parse --file log.md --out events-proposed.json \
      [--since 2026-04-01]        # default: last 3 months (§19.5)
  # ... review/edit events-proposed.json (or run an LLM pass over it) ...
  python scripts/import_v1.py load --user shardul --file events-reviewed.json

Idempotent: every insert carries source 'import:v1'; re-running `load` or
`transcript` first deletes previous 'import:v1' rows for that user (owner
connection; the runtime role couldn't and shouldn't).
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

# Matches v1 log.md conventions: "## 2026-07-04" date headers with "- item"
# bullets under them; a leading "HH:MM" on a bullet becomes the time.
DATE_RE = re.compile(r"^#{1,3}\s*(\d{4}-\d{2}-\d{2})")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s+")


def _owner_conn() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL_MIGRATE")
    if not url:
        sys.exit("set DATABASE_URL_MIGRATE (owner url) to import")
    return psycopg.connect(url)


def _tenant(conn: psycopg.Connection, user_id: str) -> None:
    conn.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))


def cmd_parse(args: argparse.Namespace) -> None:
    since = (
        datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        if args.since
        else datetime.now(timezone.utc) - timedelta(days=90)
    )
    proposed = []
    current_day: str | None = None
    for line in Path(args.file).read_text().splitlines():
        if m := DATE_RE.match(line.strip()):
            current_day = m[1]
            continue
        stripped = line.strip().lstrip("-*+ ").strip()
        if not stripped or current_day is None:
            continue
        day = datetime.fromisoformat(current_day).replace(tzinfo=timezone.utc)
        if day < since:
            continue
        occurred = day.replace(hour=12)  # midday unless the bullet has a time
        if t := TIME_RE.match(stripped):
            occurred = day.replace(hour=int(t[1]) % 24, minute=int(t[2]))
            stripped = stripped[t.end():]
        proposed.append({
            "occurred_at": occurred.isoformat(),
            "kind": "note",  # reviewer upgrades to habit_done/measurement/…
            "habit_id": None,
            "note": stripped,
        })
    Path(args.out).write_text(json.dumps(proposed, indent=2))
    print(
        f"proposed {len(proposed)} events since {since.date()} -> {args.out}\n"
        "REVIEW IT (fix kinds, habit ids, drop noise) before `load`."
    )


def cmd_load(args: argparse.Namespace) -> None:
    rows = json.loads(Path(args.file).read_text())
    with _owner_conn() as conn:
        with conn.transaction():
            _tenant(conn, args.user)
            deleted = conn.execute(
                "DELETE FROM events WHERE user_id=%s AND source='import:v1'",
                (args.user,),
            ).rowcount
            for row in rows:
                conn.execute(
                    "INSERT INTO events (user_id, occurred_at, kind, habit_id, "
                    "note, source) VALUES (%s, %s, %s, %s, %s, 'import:v1')",
                    (
                        args.user, row["occurred_at"], row["kind"],
                        row.get("habit_id"), row["note"],
                    ),
                )
    print(f"loaded {len(rows)} events (replaced {deleted} previous import rows)")


def cmd_transcript(args: argparse.Namespace) -> None:
    lines = Path(args.file).read_text().splitlines()
    with _owner_conn() as conn:
        with conn.transaction():
            _tenant(conn, args.user)
            # transcript has no source column; the import marker rides an
            # events row so a re-import can be located and reasoned about.
            inserted = 0
            for line in lines:
                if not line.strip():
                    continue
                entry = json.loads(line)
                conn.execute(
                    "INSERT INTO transcript (user_id, ts, role, text) "
                    "VALUES (%s, %s, %s, %s)",
                    (
                        args.user,
                        entry.get("ts") or entry.get("timestamp"),
                        "assistant" if entry.get("role") in ("assistant", "model")
                        else "user",
                        entry.get("text") or entry.get("content", ""),
                    ),
                )
                inserted += 1
            conn.execute(
                "INSERT INTO events (user_id, occurred_at, kind, note, source) "
                "VALUES (%s, now(), 'system', %s, 'import:v1')",
                (args.user, f"imported {inserted} v1 transcript lines"),
            )
    print(f"imported {inserted} transcript lines")


def cmd_docs(args: argparse.Namespace) -> None:
    with _owner_conn() as conn:
        with conn.transaction():
            _tenant(conn, args.user)
            for kind, path in (("goals", args.goals), ("daily_plan", args.plan)):
                if not path:
                    continue
                conn.execute(
                    "INSERT INTO docs (user_id, kind, version, content, written_by) "
                    "SELECT %s, %s, COALESCE(MAX(version), 0) + 1, %s, 'human' "
                    "FROM docs WHERE user_id=%s AND kind=%s",
                    (args.user, kind, Path(path).read_text(), args.user, kind),
                )
                print(f"imported {kind} from {path}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="import_v1")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="log.md -> reviewable JSON proposal")
    p.add_argument("--file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--since", help="ISO date; default last 90 days (§19.5)")

    p = sub.add_parser("load", help="reviewed JSON -> events rows")
    p.add_argument("--user", required=True)
    p.add_argument("--file", required=True)

    p = sub.add_parser("transcript", help="transcript.jsonl -> transcript rows")
    p.add_argument("--user", required=True)
    p.add_argument("--file", required=True)

    p = sub.add_parser("docs", help="goals.md/daily_plan.md -> docs v1")
    p.add_argument("--user", required=True)
    p.add_argument("--goals")
    p.add_argument("--plan")

    args = parser.parse_args()
    {"parse": cmd_parse, "load": cmd_load,
     "transcript": cmd_transcript, "docs": cmd_docs}[args.cmd](args)


if __name__ == "__main__":
    main()
