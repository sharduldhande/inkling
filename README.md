# Inkling

**A proactive AI assistant you use through a native iOS app.** Inkling tracks deadlines, habits and goals, fits them into open calendar gaps, and tells you what to work on right now, on the lock screen and through push notifications, without being asked. Built so **the model can never be the reason a reminder is missed**, and daily-driven on my own server and iPhone.

| Chat & proactive nudges | Today agenda | Nutrition & activity log |
| :---: | :---: | :---: |
| <img src="app/docs/screenshots/chat.png" width="250" alt="Inkling chat tab with proactive nudges"> | <img src="app/docs/screenshots/today.png" width="250" alt="Inkling Today agenda"> | <img src="app/docs/screenshots/log.png" width="250" alt="Inkling Log tab with quotas and heatmaps"> |

## Two parts

| | |
|---|---|
| **iOS app** ([`app/`](app/)) | The client and the whole user experience: chat by text, voice or photo, the Today agenda, the nutrition and activity Log, push notifications, and a lock-screen Live Activity. Swift and SwiftUI on iOS 26, **zero third-party packages**. |
| **Server** ([`server/`](server/)) | The agent behind it: owns all state, runs each Claude turn, validates what the model proposes, schedules reminders and sends the pushes. Python, FastAPI, PostgreSQL. |

The app never decides anything on its own and the server never draws UI: the app streams, renders and registers for pushes; the server thinks, schedules and remembers.

<p align="center"><img src="docs/architecture.svg" width="860" alt="Inkling architecture: the iOS app talks to the server; triggers queue in Postgres, a worker runs Claude, Python validates and commits, and pushes go back through APNs"></p>

1. **Something starts a turn:** you send a message, a reminder comes due, or the calendar changes. Each becomes a trigger row in Postgres.
2. **A worker claims it** (`FOR UPDATE SKIP LOCKED`) and reads a fresh snapshot of your state.
3. **Claude replies** and proposes any changes as typed `commands[]`.
4. **Plain Python validates** each command and commits the state and any pushes in one transaction. The model never writes to the database.
5. **The push sender** delivers notifications and Live Activity updates to the phone through APNs.

## The iOS app

- **Chat** is the one place you write: text, live voice dictation or a photo. The assistant's own nudges land in the same thread, so a reminder can be answered with "done" or "move it to 7".
- **Today** shows what's overdue, the next 48 hours of commitments and calendar events, and recurring habits. Tap any row to mark it done or open it in chat.
- **Log** shows calories and protein left for the day, the latest weigh-in, 10-week calorie and exercise heatmaps, and recent meals and workouts.
- **Lock screen** keeps a Now / Next Live Activity that the server starts and updates over push, with countdowns that tick on their own.

How it's built:

- **Resumable SSE streaming and honest mobile failure states.** The server writes seq-numbered `turn_events` rows so reconnecting clients replay missed tokens via `Last-Event-ID`, while the iOS client reads `URLSession.bytes(for:)` unbuffered line by line. On `429 busy` (meaning the server already queued the message), the bubble moves to `.queued` and polls `/health` every 10 seconds instead of duplicating the turn; a 90-second stall watchdog catches mid-stream drops across app suspension, and retry appears only when a request provably never reached the server.
- **End-to-end lock-screen Live Activity lifecycle.** `LiveActivitySync` derives Now (in-progress calendar event, else today's focus, else earliest deadline) and Next, pushes only on state change, and restarts the activity at 7h 45m before iOS's 8-hour limit. On iOS, `LiveActivityRegistrar` starts in `didFinishLaunching` so background push-to-start wakes still report update tokens back to the server, while `Shared/NextUpAttributes.swift` decodes the APNs payload directly with Unix-second timestamps and self-ticking `.offset` countdowns.
- **Live voice dictation and server-first local caches.** `DeepgramTranscriptionService` taps `AVAudioEngine`, resamples on the audio thread to 16 kHz mono linear16 PCM via `AVAudioConverter`, and streams over `URLSessionWebSocketTask` to Deepgram (`nova-3`), waiting up to 1 second on `CloseStream` so trailing words are not clipped. On-disk caches paint Chat, Today, and Log immediately on launch while history reconciliation merges optimistic bubbles against bigint server IDs and defers syncs arriving mid-stream.
- **No third-party code.** SwiftUI, Observation, ActivityKit, WidgetKit, AVFoundation, `URLSession` and Keychain Services only. `PushRegistrar` reads `aps-environment` from the embedded provisioning profile at runtime, so debug and release builds register with the right APNs gateway without build flags.

## The server

- **Model proposes, Python validates and commits.** The LLM never writes to the database. It emits a reply plus a JSON `commands[]` array through the `emit_commands` tool (never parsed from prose), decoded into a 16-member Pydantic discriminated union (`extra="forbid"`). Inside one tenant transaction, deterministic validators enforce ownership, legal transitions, optimistic-concurrency versions, health bounds, unresolved subtasks, and hallucinated push claims; rejected commands are logged to `audit_log` and noted in the reply without failing the turn.
- **Fresh, budgeted context and Postgres as the queue.** Turns never reuse stateful CLI sessions (`--continue`). One read transaction assembles a fixed stable-to-volatile snapshot (clock, goals, working state, daily plan, 48h commitments, 7-day calendar, today's health totals, 40-message transcript tail, optional FTS recall) and closes before inference starts so no DB connection stays open across a model call. Workers claim triggers with `FOR UPDATE SKIP LOCKED` and 90-second heartbeat-extended leases (no advisory locks or `LISTEN/NOTIFY`, so it works behind transaction-mode poolers), routing each user to Claude Agent SDK, Anthropic Messages API, or Codex CLI with no restart.
- **Deterministic scheduling and transactional push delivery.** An in-memory min-heap holds the next 48 hours of occurrences, wakeups, presence check-ins, and nightly consolidation; every fire is a DB compare-and-set with an idempotency key, catching up missed items on boot (`late`) and deferring quiet hours in the user's timezone. APNs pushes leave only after commit through a leased outbox drainer with boot orphan recovery, a 1m / 5m / 15m / 1h retry ladder, and `ntfy` dead-letter alerts.
- **Hard tenant isolation and least-privilege boundaries.** Every user table enforces `FORCE ROW LEVEL SECURITY` via transaction-local `app.user_id`, the `runtime_role` has no DDL and no `DELETE` on user data (ledgers like `events`, `transcript`, and `audit_log` are insert-only), and `tests/test_isolation.py` enumerates `information_schema` so adding a table without isolation coverage fails CI. Calendar sync exposes a single read-only `snapshot()` method behind a deterministic 72-hour relevance gate, and `/mcp/<secret>` exposes a single write-only `add_reminder` tool with constant-time auth and a bare 404 on mismatch.

## Layout

| Path | What |
|---|---|
| [`app/Inkling/`](app/Inkling/) | SwiftUI views (`Chat`, `Today`, `Log`, `Settings`), SSE client, stores/caches, Deepgram voice service, APNs + Live Activity registrars |
| [`app/InklingWidgets/`](app/InklingWidgets/), [`app/Shared/`](app/Shared/) | Lock-screen Now / Next card + Dynamic Island widget and shared `NextUpAttributes` wire contract |
| [`server/inkling/api/`](server/inkling/api/) | FastAPI routes: streaming `/chat`, `/turns/{id}/events`, `/agenda`, `/days`, `/documents`, `/live-activity`, `/mcp/{secret}` |
| [`server/inkling/turns/`](server/inkling/turns/) | Trigger worker/leases, context snapshot, 16 Pydantic commands, domain validator, transactional commit, `recall` tool |
| [`server/inkling/inference/`](server/inkling/inference/) | `InferenceGateway` protocol + Claude Agent SDK, Anthropic Messages API, and Codex CLI adapters |
| [`server/inkling/scheduler/`](server/inkling/scheduler/) | Min-heap timer, RRULE materialization, quiet-hours deferral, presence check-in |
| [`server/inkling/delivery/`](server/inkling/delivery/) | Transactional outbox drainer, APNs HTTP/2 client, Now / Next Live Activity sync, `ntfy` alerts |
| [`server/inkling/calendar_/`](server/inkling/calendar_/), [`jobs/`](server/inkling/jobs/), [`db/`](server/inkling/db/) | Read-only ICS sync + relevance gate; nightly consolidation, encrypted `pg_dump` + restore drill; async `psycopg 3` pool + 11 SQL migrations |
| [`server/tests/`](server/tests/) | 405 pytest tests across 24 modules (commands, scheduler, delivery, Live Activity, calendar, MCP, RLS isolation) |

## Running it

**iOS app** (Xcode 26, iOS 26 SDK): open [`app/Inkling.xcodeproj`](app/Inkling.xcodeproj), select your signing team and bundle IDs for `Inkling` and `InklingWidgets`, and run the `Inkling` scheme. Chat, Today, and Log run in the Simulator against any reachable server; APNs and Live Activity pushes require a physical device and an Apple Developer team. On first launch, Settings prompts for your server URL, bearer token, and optional Deepgram key (stored in Keychain).

**Server** (Python 3.12+, PostgreSQL 16; runs as one process via `all` or 6 roles: `api`, `worker`, `scheduler`, `delivery`, `calendar`, `jobs`):

```sh
cd server && uv sync
cp .env.example .env            # Postgres URLs, token files, optional APNs/S3 settings
uv run python -m inkling migrate
uv run python -m inkling all

# Tests run against a real Postgres DB (URL must include "test"; schema is recreated per run)
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/inkling_test uv run pytest
```

## About this repository

This is a sanitized public mirror of my private server and iOS repositories. Commit history is preserved, deployment hostnames and infrastructure IDs are stripped, and GitHub Actions runs the full server test suite against a throwaway PostgreSQL 16 container on every push.
