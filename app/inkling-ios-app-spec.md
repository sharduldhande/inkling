# Inkling iOS App — Build Specification (v2.0, 2026-07-18)

You are building a complete, working iOS app, plus a small server amendment it
depends on. Follow this spec exactly. Where this spec makes a decision, do not
substitute your own. Do not add features not listed here. Do not ask
clarifying questions if the answer is in this document. Where this document
says "byte-exact", the string is copied from the live server source and a
plausible-but-different string will fail against it.

Build order is Part A (server), then Part B (app), then Part C (voice).
Part C may be deferred; Parts A and B may not be reordered — the app's panes
1 and 3 are unbuildable and untestable without Part A live.

---

# PART A — Server amendment (health tables + three read endpoints)

Target repository: `inkling-server`. The app's chat pane talks to endpoints
that already exist. The other two panes need server work that does not exist
yet: typed health tables, one migration, three **read-only** endpoints, and
four model commands. This part is the contract; the implementation must match
it exactly.

Division of labor (fixed): the migration, the three endpoints, and
`tests/test_panes.py` are implemented by Codex from this contract. The four
commands and their integration (`commands.py`, `validate.py`, `apply.py`,
context/recall/prompt updates) are implemented by Claude and are specified
here only as far as the wire/data contract requires.

Design constraints (do not "improve" on them):

- **Chat stays the sole user-facing write path.** The new endpoints add no
  verbs. Anything the user wants changed, they say in chat.
- **Typed columns, not unit conventions.** Health data lives in three new
  tables (`intake`, `workouts`, `body_metrics`), NOT in `events`. A meal is
  one row. There are no magic unit strings anywhere in the system.
- Shapes follow the existing `/history` precedent: **ids are strings**,
  **timestamps are epoch milliseconds**, auth failures return 401 with the
  bare body `unauthorized`. Every `numeric` column is cast `::float8` in SQL
  (psycopg's `Decimal` cannot be JSON-serialized; this fails at runtime, not
  in typecheck).
- The agenda window is 48 hours because `inkling/scheduler/scheduler.py`
  materializes recurring occurrences exactly that far ahead
  (`HORIZON = timedelta(hours=48)`). Do not change either number.
- Day bucketing happens **in the user's timezone** (`users.timezone`), in
  SQL, server-side. The phone's clock never decides which day a meal
  belongs to.

## A1. Migration `inkling/db/migrations/005_health.sql`

Follow the structure of 002/003/004 (schema, then RLS, then grants — all in
this one file since migrations are applied in filename order as single units).

```sql
-- targets: {"kcal_quota": 2200, "protein_quota_g": 140}; absent key = unset
ALTER TABLE users ADD COLUMN targets jsonb NOT NULL DEFAULT '{}';

CREATE TABLE intake (
  id            bigserial PRIMARY KEY,
  user_id       text NOT NULL REFERENCES users(id),
  occurred_at   timestamptz NOT NULL,
  note          text NOT NULL,
  kcal          numeric,
  protein_g     numeric,
  carb_g        numeric,
  fat_g         numeric,
  supersedes_id bigint REFERENCES intake(id),
  source        text NOT NULL DEFAULT 'chat',
  source_ref    text,
  turn_id       bigint,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON intake (user_id, occurred_at);
CREATE UNIQUE INDEX intake_source_ref
  ON intake (user_id, source_ref) WHERE source_ref IS NOT NULL;

CREATE TABLE workouts (
  id            bigserial PRIMARY KEY,
  user_id       text NOT NULL REFERENCES users(id),
  occurred_at   timestamptz NOT NULL,
  activity_type text NOT NULL,
  duration_min  numeric NOT NULL,
  kcal_burned   numeric,
  distance_km   numeric,
  note          text,
  source        text NOT NULL DEFAULT 'chat',   -- chat | healthkit
  source_ref    text,                           -- HealthKit UUID for ingested rows
  turn_id       bigint,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON workouts (user_id, occurred_at);
CREATE UNIQUE INDEX workouts_source_ref
  ON workouts (user_id, source_ref) WHERE source_ref IS NOT NULL;

CREATE TABLE body_metrics (
  id            bigserial PRIMARY KEY,
  user_id       text NOT NULL REFERENCES users(id),
  occurred_at   timestamptz NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('weight', 'height')),
  value         numeric NOT NULL,
  unit          text NOT NULL,
  source        text NOT NULL DEFAULT 'chat',
  source_ref    text,
  turn_id       bigint,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON body_metrics (user_id, occurred_at);
CREATE UNIQUE INDEX body_metrics_source_ref
  ON body_metrics (user_id, source_ref) WHERE source_ref IS NOT NULL;
```

Then, mirroring 003's pattern exactly for the three new tables: `ENABLE ROW
LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, and a `tenant_isolation` policy
`USING (user_id = current_setting('app.user_id', true)) WITH CHECK (same)`.

Then grants, mirroring 004's philosophy:

```sql
-- Append-only surfaces: supersession is a new row, so no UPDATE, no DELETE.
GRANT SELECT, INSERT ON intake, workouts, body_metrics TO runtime_role;
-- SetTargets is the ONLY runtime write to users, and only to this column.
GRANT UPDATE (targets) ON users TO runtime_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO runtime_role;
```

(The sequence grant must be repeated here — 004's ran before these sequences
existed.)

## A2. Endpoints: new file `inkling/api/routes_panes.py`

Model every handler on `routes_history.py`: resolve the user via
`request.app.state.roster.user_for_header(authorization)`, bare-401 on
failure, one `async with request.app.state.db.with_tenant(user_id)` block,
`JSONResponse` out. Wire into `server.py` with an import and
`app.include_router(routes_panes.router)` after the calendar router. No
query parameters on any of the three endpoints — all windows are fixed
server-side constants.

### A2a. `GET /agenda` — overdue + next 48h

Response:

```json
{"items":[
  {"id":"c:12","kind":"commitment","title":"Pay electricity bill","body":null,"due":1752829200000,"overdue":true},
  {"id":"o:98","kind":"recurring","title":"Evening review","body":null,"due":1752861600000,"overdue":false},
  {"id":"g:evt-abc","kind":"calendar","title":"Dentist","starts":1752858000000,"ends":1752861600000},
  {"id":"c:11","kind":"commitment","title":"Book dentist","body":null,"due":1752822000000,"done":true,"done_ts":1752825600000}
]}
```

One flat list: live items sorted ascending by `due`/`starts` (overdue sorts
first naturally), truncated to 100 — then recently completed commitments
appended after them, newest completion first. Four sources:

1. One-shot commitments: `status='open' AND due_at IS NOT NULL AND
   due_at <= now() + interval '48 hours'` — no lower bound (overdue is
   unbounded backwards; an open commitment nags until resolved in chat).
   `overdue` = `due_at < now()`. Id `'c:' || id`.
2. Recurring commitments via materialized occurrences: join
   `commitment_occurrences o` to `commitments c` (`c.status='open' AND
   c.recurrence IS NOT NULL`), `o.status IN ('pending','eligible')`,
   `o.fire_at <= now() + interval '48 hours'`. `due` = fire_at, id
   `'o:' || o.id`, kind `recurring`.
3. Calendar cache: `starts_at IS NOT NULL AND coalesce(ends_at, starts_at)
   >= now() AND starts_at <= now() + interval '48 hours'`; title =
   `coalesce(summary, '(untitled)')`, id `'g:' || provider_event_id`, kind
   `calendar`; `ends` null when NULL.
4. Recently completed commitments: `status='done' AND updated_at >= now() -
   interval '24 hours'`, ordered `updated_at DESC`, limit 50. Shape:
   `{"id","kind":"commitment","title","body","due","done":true,"done_ts"}`
   — `due` nullable (undated commitments complete too), `done_ts` =
   `updated_at` (CompleteCommitment with outcome `done` is the only writer
   that moves a commitment to `done`, bumping `updated_at`; outcome
   `dropped` lands as `cancelled`, which never surfaces — a dropped plan
   is not a false checkmark). Live items never carry a `done` key.
   Completion still happens ONLY via chat; this is a read-side projection
   so done tasks show checked off for a day instead of vanishing.
5. Recurring done-today: habit_done events carrying `commitment_id`
   (events column added in 007) joined to their open recurring
   commitment, event `occurred_at` on TODAY in the user's stored
   timezone (local day, not rolling — yesterday evening's habit is not
   "done" this morning). `DISTINCT ON (c.id)` newest event; limit 50;
   appended last, newest completion first. Shape:
   `{"id":"c:<id>","kind":"recurring","title","body","due":null,
   "done":true,"done_ts":<event occurred_at ms>}`. The commitment stays
   open — tomorrow's occurrence still lists live; both rows showing
   (done today + next upcoming) is intended.

### A2b. `GET /measurements` — 7-day typed ledger

Response — one list, newest-first by `ts`, max 500, discriminated by `type`:

```json
{"entries":[
  {"id":"i:12","type":"meal","note":"dal + rice","kcal":700.0,"protein_g":24.0,"carb_g":110.0,"fat_g":12.0,"ts":1752829200000},
  {"id":"w:3","type":"workout","activity":"running","duration_min":30.0,"kcal_burned":260.0,"distance_km":5.0,"note":"easy pace","ts":1752825600000},
  {"id":"b:5","type":"metric","kind":"weight","value":82.4,"unit":"kg","ts":1752739200000}
]}
```

- `intake` rows: last 7 days by `occurred_at`, **hiding superseded rows**
  (`NOT EXISTS (SELECT 1 FROM intake s WHERE s.user_id = i.user_id AND
  s.supersedes_id = i.id)`). Nullable: kcal, protein_g, carb_g, fat_g.
- `workouts` rows: last 7 days. Nullable: kcal_burned, distance_km, note.
- `body_metrics` rows: last 7 days. Nothing nullable.
- All numerics `::float8`; all ids prefixed (`i:`/`w:`/`b:`) and stringified;
  `ts` = `(extract(epoch from occurred_at) * 1000)::bigint`.

### A2c. `GET /days` — 84 daily aggregates + targets + weight

Response:

```json
{"days":[
  {"date":"2026-04-26","kcal":0.0,"protein_g":0.0,"exercised":false,"logged":false},
  {"date":"2026-07-18","kcal":1240.0,"protein_g":78.0,"exercised":true,"logged":true}
 ],
 "targets":{"kcal_quota":2200,"protein_quota_g":140},
 "weight":{"value":82.4,"unit":"kg","ts":1752739200000}}
```

- Exactly 84 `days` rows, oldest first, ending on **today in the user's
  timezone** (`SELECT timezone FROM users WHERE id = %s`, bucketed via
  `(occurred_at AT TIME ZONE <tz>)::date` and a `generate_series` over the
  84 dates so gap days appear with zeros).
- `kcal`/`protein_g`: sums over non-superseded intake (same NOT EXISTS as
  A2b), `coalesce(sum(...), 0)::float8`.
- `exercised`: true when any `workouts` row falls on that local date
  (source irrelevant — chat and healthkit both count).
- `logged`: true when any non-superseded `intake` row falls on that date.
- `targets`: the `users.targets` jsonb verbatim (`{}` when unset — the app
  must handle absent keys).
- `weight`: most recent `body_metrics` row with `kind='weight'` (any age),
  or JSON `null` when none exists.

## A3. Commands (wire contract only; implementation is Claude's)

Four new commands join the `emit_commands` discriminated union. The app
never sees these directly — they matter here because the panes display what
they write:

- `LogMeal {note, kcal?, protein_g?, carb_g?, fat_g?, occurred_at?,
  supersedes_id?}` → one `intake` row; supersession replaces a wrong meal.
  Bounds: kcal 0–5000, each macro 0–500.
- `LogWorkout {activity_type, duration_min, kcal_burned?, distance_km?,
  note?, occurred_at?}` → one `workouts` row, source `chat`. Bounds:
  duration 1–600. `activity_type` from a pinned list of
  HKWorkoutActivityType-compatible names.
- `LogWeight {value, unit, occurred_at?}` → one `body_metrics` row, kind
  `weight`; unit `kg`|`lb`; bounds 20–400 kg-equivalent.
- `SetTargets {kcal_quota?, protein_quota_g?}` → jsonb merge into
  `users.targets`. Bounds: kcal 800–6000, protein 20–400. Rejected in
  consolidation turns.

## A4. Tests: new file `tests/test_panes.py`

Follow the harness conventions exactly: `migrated`/`dsn`/`db` fixtures from
`tests/conftest.py`; seed as the owner inside a
`BEGIN; SELECT set_config('app.user_id', %s, true); ...; COMMIT` block
(FORCE RLS applies to the owner); build the app with
`create_app(db, Roster({USER_A: ..., USER_B: ...}))` (no supervisor) and an
`httpx.AsyncClient(transport=ASGITransport(app=app))`. Truncate
`commitment_occurrences, commitments, calendar_events_cache, intake,
workouts, body_metrics RESTART IDENTITY CASCADE` in the seed fixture.
Set `targets` for USER_A during seeding via
`UPDATE users SET targets = '{"kcal_quota": 2200, "protein_quota_g": 140}'`.

Required cases (at minimum):

1. `/agenda`, `/measurements`, `/days` each 401 with bare body
   `unauthorized` when unauthenticated.
2. `/agenda`: overdue open commitment first with `overdue:true`; >48h
   commitments absent; in-window calendar event and pending occurrence of a
   recurring commitment present, correctly typed (`kind`, id prefixes, exact
   key sets per A2a); live items ascending; a commitment completed within
   24h appended after the live items with `done:true` and `done_ts`;
   commitments completed >24h ago absent.
3. `/measurements`: superseded meal hidden, superseding meal shown with its
   macros as floats; workout and weight rows present with exact key sets
   per A2b; 8-day-old rows absent; newest-first.
4. `/days`: exactly 84 rows; a meal logged at a UTC instant that falls on a
   different local calendar day in `America/New_York` (the conftest seed
   timezone) buckets to the LOCAL day; gap days present with
   `kcal 0.0 / logged false`; `exercised` true on the workout day;
   `targets` echoed; `weight` is the latest weigh-in; `weight` null and
   `targets` `{}` for a user with no data.
5. Tenant isolation: USER_B sees empty `items`/`entries` and all-zero days.

## A5. Verify

```
uv run pytest tests/test_panes.py -q   # all new tests green
uv run pytest -q                       # full suite still green
```

Part A adds tables and routes only; any existing-test failure means the
contract was deviated from.

---

# PART B — The app

## B1. What this app is

A three-pane SwiftUI client for "Inkling", a personal agent that runs on the
user's own server. The server owns the entire conversation and all state;
this app is a thin window onto it. There is exactly ONE user, ONE server, ONE
conversation. No accounts, no onboarding flow, no multi-chat, no chat drawer.

The three panes, as three tabs:

1. **Today** — read-only agenda: overdue commitments plus everything due or
   scheduled in the next 48 hours (commitments, recurring occurrences,
   calendar events).
2. **Chat** — the only pane that writes. Every mutation in the user's life —
   marking something done, logging food, rescheduling — is a sentence typed
   (or spoken, Part C) here. The server's proactive nudges also appear here,
   as assistant messages, via history sync.
3. **Log** — read-only mirror of the last 7 days of measurements (calories,
   exercise, weigh-ins) exactly as the server recorded them.

Design law, carried from the product decision that shaped the server: **no
browsable todo lists and no buttons that mutate state.** A large list remains
unread; work gets done in the moment, through conversation. Tapping a row in
Today or Log therefore never completes/edits/deletes anything — it drops a
reference into the chat input so the user can say what they want done
(tap-to-chat, §B7).

## B2. Hard constraints (violating any of these is a failed build)

- SwiftUI. iOS deployment target 26.0, built with the Xcode 26 SDK. The app
  adopts the system Liquid Glass design: tab/nav bars and sheets take it
  automatically from the SDK; custom floating controls (send button, "↓ New"
  pill, transient banners/captions) use `.glassEffect()` /
  `.buttonStyle(.glass)`. Content surfaces (message bubbles, quota tiles,
  ledger rows, heatmaps) stay on standard backgrounds per the HIG — glass is
  the control layer, not the content layer.
- Paid-team signing (LJA3588388) since 2026-09-11: the ONE entitlement is
  `aps-environment` (Inkling.entitlements) for native push. Still NO Sign in
  with Apple, NO App Groups, NO iCloud, NO background modes, NO HealthKit.
  Push is registration + tap only (PushRegistrar.swift): the app requests
  permission, POSTs its APNs token to `/devices` on every launch with the
  environment read from the embedded provisioning profile, and routes a tap
  into the same `inklingOpenURL` path as the URL scheme. No action buttons.
- No third-party dependencies. No SwiftPM packages, no CocoaPods. URLSession
  only for networking.
- No HTML/markdown rendering. Message text is plain text (SwiftUI `Text`,
  `.textSelection(.enabled)`).
- Never log, print, or display the bearer token (or, Part C, the Deepgram
  key) after entry.
- The panes never write. The only network verbs the app ever issues are
  `GET` and `POST /chat`.

## B3. Server contract (do not change, do not mock)

Base URL default: `https://inkling.example.com` — user-editable in
Settings (§B9); the deployed vhost may differ and nothing else in the app may
depend on the value.

Auth on every endpoint except `/health`: header
`Authorization: Bearer <token>`. The scheme word is byte-exact
uppercase-B `Bearer` — the server does a constant-time full-string compare
and `bearer` fails. Auth failure → status 401, body exactly `unauthorized`
(bare string, no JSON). On any 401: show the alert
"Unauthorized — check your token in Settings." and do not retry
automatically.

Unknown paths return 404 with an empty body.

### B3a. `GET /health`

```
→ 200 {"status":"ok","busy":false}
```

Unauthenticated calls succeed with `busy:false`; with a valid token, `busy`
is true while a turn is running for this user. Because it never 401s, it
CANNOT validate a token — see §B9 for what "Test connection" must call
instead.

### B3b. `GET /history?since=<unix-ms>`

```
→ 200 {"messages":[{"id":"41","role":"user","text":"...","ts":1752829200000}, ...]}
```

- Returns transcript rows with `ts` strictly greater than `since`,
  oldest-first, capped at the most recent 200. `since=0` returns the most
  recent 200.
- `id` is a **string** (stringified server bigint — decode as `String`, a
  Swift `String` field will not decode a JSON number and vice versa).
- `ts` is epoch **milliseconds**.
- `role` is `user` or `assistant` today, but the server schema reserves
  `system_nudge`. Decode `role` as a `String`, not an enum; render any value
  other than `user` with assistant styling. A strict enum decode here is a
  latent crash.

### B3c. `POST /chat` (SSE)

```
POST /chat
Headers: Authorization: Bearer <token>, Content-Type: application/json
Body:    {"message": "<plain string>"}
```

Success is a `text/event-stream` response. Frames are lines of the form
`data: <json>\n\n`, payloads in order of appearance:

| Payload | Meaning | App behavior |
|---|---|---|
| `{"text":"..."}` | A chunk of reply text; may repeat. | Append to the in-progress assistant bubble, concatenated in arrival order, no separator added. |
| `{"done":true}` | Reply text complete. | Stop the typing indicator; mark bubble done. |
| `{"exit":0}` | Turn finished; non-zero = server-side error. | If non-zero: mark the bubble errored ("Something went wrong on the server."). Stream then closes. |

A stream may also open and deliver an error as text: `{"text":"<error>"}`,
`{"done":true}`, `{"exit":1}` — the table above already handles it.

**Parsing rules (implement exactly):** read with `URLSession.bytes(for:)`,
iterate `lines`. A line starting with `data: ` → strip the prefix, parse as
JSON; ignore lines that fail to parse, ignore empty lines, ignore any other
SSE field (`event:`, `id:`, `retry:`) if one ever appears. Never buffer the
whole response; dispatch UI updates to the MainActor as chunks arrive.

**Error statuses (check BEFORE reading the body as SSE):**

- `401` → the standard unauthorized alert. Do not retry.
- `429`, body byte-exact `brain is busy, try again in a minute`
  (Content-Type `text/plain`) → **the message was NOT lost.** This server
  persists the message as a pending trigger and answers it when the current
  turn finishes. Follow the queued flow in §B8.3. Do NOT resend — a resend
  creates a second turn and the user gets two answers.
- Any other non-200 → mark the user bubble `failed` with a Retry button
  (§B8.4).
- Transport failure before any response arrives → also `failed` + Retry
  (the request never reached the server; resending is safe).
- Network failure mid-stream → keep the partial text; follow §B8.5.

### B3d. `GET /agenda` (Part A)

```
→ 200 {"items":[
  {"id":"c:12","kind":"commitment","title":"Pay electricity bill","body":null,"due":1752829200000,"overdue":true},
  {"id":"o:98","kind":"recurring","title":"Evening review","body":null,"due":1752861600000,"overdue":false},
  {"id":"g:evt-abc","kind":"calendar","title":"Dentist","starts":1752858000000,"ends":1752861600000},
  {"id":"c:11","kind":"commitment","title":"Book dentist","body":null,"due":1752822000000,"done":true,"done_ts":1752825600000}
]}
```

- One flat list: live items sorted ascending by `due`/`starts` (overdue
  items sort first naturally — their timestamps are in the past), max 100,
  then commitments completed in the last 24 hours appended after them,
  newest completion first, max 50.
- Window: everything overdue plus 48 hours ahead; done items by completion
  time, not due. Fixed server-side; the app sends no query parameters.
- `body` and `ends` are nullable. `kind` is one of exactly
  `commitment | recurring | calendar` — but decode as `String` and render
  unknown kinds like `commitment` (same forward-compatibility rule as
  `role`).
- `done` (Bool) and `done_ts` (ms) appear only on completed items; absent
  means live. `due` is nullable on done items (undated commitments
  complete too; recurring done rows always send `due:null`). A
  `kind:"recurring"` done row means "checked off today" — the loop stays
  open and its next occurrence may also list live; both showing is
  intended. Completion happens ONLY via chat — the model closes one-shots
  with CompleteCommitment (outcome `done` → shows here; outcome `dropped` →
  `cancelled`, never shown) and checks recurring days off with a habit_done
  event naming the commitment; the app never writes. Closure records the
  USER's verdict — a lapsed deadline stays open and overdue.

### B3e. `GET /measurements` (Part A)

```
→ 200 {"entries":[
  {"id":"i:12","type":"meal","note":"dal + rice","kcal":700.0,"protein_g":24.0,"carb_g":110.0,"fat_g":12.0,"ts":1752829200000},
  {"id":"w:3","type":"workout","activity":"running","duration_min":30.0,"kcal_burned":260.0,"distance_km":5.0,"note":"easy pace","ts":1752825600000},
  {"id":"b:5","type":"metric","kind":"weight","value":82.4,"unit":"kg","ts":1752739200000}
]}
```

- Last 7 days, newest-first, max 500. Superseded meals are already filtered
  out server-side — display what arrives, dedupe nothing.
- Discriminator `type` ∈ `meal | workout | metric` — decode as `String`;
  entries with an unknown `type` are dropped silently (forward compat), the
  decode of the list must never fail wholesale.
- Nullable per type: meal — `kcal`,`protein_g`,`carb_g`,`fat_g`; workout —
  `kcal_burned`,`distance_km`,`note`; metric — nothing. All numbers are JSON
  numbers (Double); all ids are strings.

### B3f. `GET /days` (Part A)

```
→ 200 {"days":[{"date":"2026-07-18","kcal":1240.0,"protein_g":78.0,"exercised":true,"logged":true}, ...],
       "targets":{"kcal_quota":2200,"protein_quota_g":140},
       "weight":{"value":82.4,"unit":"kg","ts":1752739200000}}
```

- Exactly 84 `days` rows, oldest first, ending on today **in the server's
  notion of the user's timezone** — render the `date` strings as-is; never
  re-bucket by the phone's clock. The last row IS today.
- `targets` may be `{}` or have either key absent (quota unset). `weight`
  may be JSON `null`.
- This one response powers the Log pane's tiles and both heatmaps. The app
  does exactly two computations on it: `remaining = quota - today's total`
  (clamped at display time, §B6) and the per-cell color mapping. All other
  arithmetic already happened server-side.

### B3g. `POST /documents` (document ingestion, §14)

Two request shapes, one endpoint. Auth: same bearer header as everything.

```
multipart/form-data with one part named "file" (the PDF bytes; filename kept)
→ 202 {"document_id": 7, "title": "ISyE_syllabus.pdf", "status": "queued"}

application/json {"url": "https://leetcode.com/studyplan/top-interview-150/"}
→ 202 {"document_id": 8, "title": "Top Interview 150", "status": "queued"}
```

Errors: `401` unauthorized; `400` malformed body (missing `file` part /
`url` key); `413` file over 15MB; `415` other content types; `422` with
`{"error": "<reason>"}` when extraction fails (scanned/unreadable PDF,
unfetchable URL) — show `error` verbatim in the failed bubble.

202 means QUEUED, exactly like the chat 429 flow (§B8.3): the server runs
an asynchronous ingestion turn whose reply (the proposed schedule) arrives
via normal history sync as an assistant message. The app never resends on
202, never polls the document id, and gets no SSE for this — after a 202,
schedule a history sync a few seconds out and let §B8.6 do the rest.

### B3h. Endpoints that exist but which this app must NOT use

`GET /turns/{id}/events` (stream replay) — the live SSE stream never emits
`id:` fields, so the client never learns a turn id or sequence number and
cannot use replay. Recovery from dropped streams goes through history sync
(§B8.5) instead. `POST /calendar/webhook/google` is Google's, not yours.

## B4. Navigation shell

`RootView`: a `TabView` with exactly three tabs, in this order:

| Tab | Title | SF Symbol |
|---|---|---|
| 0 | Today | `calendar` |
| 1 | Chat | `message` |
| 2 | Log | `chart.bar` |

- Selected tab is `@State` in `RootView` (an enum `Tab: Int { today, chat,
  log }`), passed down where tap-to-chat needs to switch it.
- **Chat is the default tab on every cold launch.**
- Nav bar titles: "Today", "Inkling", "Log". The gear icon (→ SettingsSheet)
  appears as the trailing nav item on ALL three tabs.
- First-run: if no bearer token is stored, present SettingsSheet
  automatically on appear.

## B5. Today pane (`AgendaView`)

Data: `GET /agenda`, held in `AgendaStore`.

- Vertically split: REALTIME TASKS (one-shot commitments + calendar
  events, `kind != "recurring"`) on top, RECURRING HABITS below, in that
  sequence. Four list sections:
  - **"Overdue"** — live tasks with `overdue == true`. Section omitted
    entirely when empty.
  - **"Next 48 hours"** — every other live task. When empty, show a
    centered placeholder: calendar SF symbol, caption "Nothing due in the
    next 48 hours." (If everything is empty, only the placeholder shows.)
  - **"Done"** — tasks with `done == true`, in server order (newest
    completion first). Section omitted entirely when empty.
  - **"Recurring"** — ALL `kind == "recurring"` rows, live and done, in
    server order (live by fire time first, then done-today newest first).
    Section omitted entirely when empty.
- Row layout (all kinds): title in body font, one line, truncating tail;
  below it a caption line with the time. A small SF symbol before the
  title: `checkmark.circle.fill` in `.green` for done rows (any kind);
  otherwise `calendar` for `calendar`-kind rows and an open `circle` in
  `.secondary` for the rest — open circle vs green check is how
  done/not-done reads at a glance (no `repeat` glyph; the Recurring
  header already says it). No strikethrough; done titles stay in primary
  color.
- Caption formatting rules (implement exactly, using the device's locale
  and time zone):
  - Overdue: `"Overdue — was due " + RelativeDateTimeFormatter` output
    (`.named` style, e.g. "3 hours ago"). Caption in `.red`.
  - Due/starting within the next 24h: RelativeDateTimeFormatter `.named`
    ("in 3 hours").
  - Beyond 24h: abbreviated weekday + time via
    `Date.FormatStyle().weekday(.abbreviated).hour().minute()` ("Sat 9:00 AM").
  - `calendar` rows with a non-null `ends` on the same day append the end
    time: "in 3 hours until 6:00 PM" / "Sat 5:00–6:00 PM".
  - Done: `"Done " + RelativeDateTimeFormatter` output on `done_ts`
    ("Done 2 hours ago"); bare "Done" if `done_ts` is somehow absent.
    Caption in `.secondary`.
- `body`, when non-null, is NOT shown in the row (rows stay one-line); it is
  included in the tap-to-chat prefill (§B7).
- No swipe actions, no context menus, no delete. Read-only — the done
  checkmark is a server-computed fact, never a tappable control; marking
  done happens by telling the chat.
- Refresh policy (identical for Log, define once): fetch on first appear;
  on every `scenePhase == .active` transition; after every chat turn that
  completes (`done` frame received) — a turn may have created or completed
  commitments; after any history sync that added messages; and on
  pull-to-refresh. Failures keep the last-known list and show a one-line
  banner "Couldn't refresh — showing cached data." for 3 seconds. Responses
  are cached to disk (§B10) so a cold offline launch still renders.

## B6. Log pane (`LogView`)

Data: `GET /days` (tiles + heatmaps) and `GET /measurements` (ledger), both
held in `LogStore`, fetched together. Same refresh policy and offline-cache
behavior as §B5. Top to bottom, one scrolling view:

### B6.1 Quota tiles

Two tiles side by side, then a weight chip:

- **Calories**: big number = `kcal_quota - today.kcal` rounded to an Int,
  suffix "left", sub-caption "of 2,200 kcal". When over quota, the number
  shows the overage: "180 over" with the number in `.orange`. When
  `kcal_quota` is absent from `targets`: tile shows today's total ("1,240
  kcal today") with sub-caption "Set a quota in chat".
- **Protein**: identical rules with `protein_quota_g` and today.protein_g,
  suffixes "g left" / "g over" / "g today", sub-caption "of 140 g".
- **Weight chip** (full width, below the tiles): `scalemass` SF symbol,
  latest value ("82.4 kg"), caption "weighed <RelativeDateTimeFormatter
  .named> " ("3 days ago"). Hidden entirely when `weight` is null.
- Today's totals come from the LAST element of `days` (§B3f) — never from
  summing ledger entries.

### B6.2 Activity heatmaps (GitHub-style, side by side)

Two 7-row × 10-column grids sharing one week alignment, labels "Calories"
and "Exercise" above each, 12pt cells with 4pt gaps. Cells come from the
`days` rows (the server sends 84; the grid shows the most recent ~10
Monday-aligned weeks — fewer, larger cells beat 12 cramped columns),
column-major: rows Monday (top) → Sunday (bottom), oldest week leftmost.
The last column may be partial — cells after today render as empty space
(clear, no border).

- **Calories cell colors**: `logged == false` → red; `logged` and
  `kcal <= kcal_quota` → green; `kcal > kcal_quota` → yellow/orange.
  With no `kcal_quota` set, every logged day is green (no judgment without
  a quota). Use asset-catalog colors with light+dark variants; do not use
  raw `Color.red/.green` (they clip in dark mode).
- **Exercise cell colors**: `exercised == true` → green; false → unfilled
  (`systemFill`, no border — a stroked outline reads far too heavy at this
  cell size, and fainter fills vanish against the card background). Binary;
  no red, no yellow — a rest day is not a failure. Both grids sit together
  in one `secondarySystemBackground` rounded card matching the quota tiles.
  When `days` is empty (never synced), all 84 cells render as unfilled
  placeholder dots rather than a blank card.
- **Neutral prehistory**: cells for dates before the FIRST day with any
  data (`logged || exercised`, scanning oldest→newest across the whole
  span) render unfilled in BOTH grids — the app's birthday must not paint
  a wall of red to its left. If no day has data, both grids are entirely
  unfilled.
- **Today**: last in-span cell, ringed (1.5pt `primary` stroke, 1pt
  offset). In the calories grid today is NEVER red — the day isn't over;
  if unlogged so far it renders unfilled-with-ring.
- No axis labels, no numbers, no month ticks, no tap targets on cells.
  The grids are ambient, not interactive.

### B6.3 Ledger

- `entries` grouped into list sections by calendar day of `ts` (device
  time zone is fine here — raw timestamps, not server day-buckets), newest
  day first. Section headers: "Today", "Yesterday", then
  `Date.FormatStyle().weekday(.wide).day().month()` ("Tuesday 15 Jul").
- Row per type — leading SF symbol, time caption, text, trailing value:
  - `meal`: `fork.knife`; text = `note`; trailing = "<kcal> kcal" when
    kcal non-null (macros appear in a second caption line
    "P 24 · C 110 · F 12" listing only non-null macros; line omitted when
    all are null).
  - `workout`: `figure.run`; text = `note ?? activity`; trailing =
    "<duration_min> min", with "· <kcal_burned> kcal" appended when
    non-null.
  - `metric`: `scalemass`; text = `kind`; trailing = "<value> <unit>".
  - Number formatting everywhere: at most one fraction digit, no trailing
    ".0".
- Empty state: `fork.knife` symbol, caption "Nothing logged in the last 7
  days. Tell Inkling what you ate or did."
- Read-only. The ONLY arithmetic this pane ever performs is the two
  computations named in §B3f; totals, day-bucketing, and supersession all
  arrive precomputed.

## B7. Tap-to-chat (the only interaction on panes 1 and 3)

Tapping any row in Today or Log:

1. Builds a prefill string:
   - Agenda commitment/recurring: `About "<title>": ` — with `body`
     appended in parentheses before the colon when non-null:
     `About "<title>" (<body>): `.
   - Agenda calendar row: `About the "<title>" calendar event: `.
   - Log meal row: `About the "<note>" meal (<kcal> kcal) from <relative
     time>: ` (the parenthetical omitted when kcal is null) — e.g.
     `About the "dal + rice" meal (700 kcal) from 3 hours ago: `.
   - Log workout row: `About the <activity> workout (<duration_min> min)
     from <relative time>: `.
   - Log metric row: `About the <value> <unit> <kind> entry from <relative
     time>: ` — e.g. `About the 82.4 kg weight entry from 3 days ago: `.
2. Sets the chat input field to that string (replacing any unsent draft —
   acceptable; drafts are not precious in a one-user chat).
3. Switches the TabView selection to Chat and focuses the input field
   (keyboard up).
4. **Never auto-sends.** The user finishes the sentence.

## B8. Chat pane (`ChatView` + `ChatStore`)

### B8.1 Display

- Vertical scroll of bubbles: user messages right-aligned (accent-color
  background), assistant left-aligned (secondary system background).
  Timestamps in small gray text under each bubble, time only
  (`Date.FormatStyle` hour+minute, e.g. "9:41 PM").
- While streaming: the assistant bubble grows in place; before the first
  `text` frame, show three animated dots in the bubble.
- Auto-scroll to bottom on new content ONLY if the user is already within
  ~100pt of the bottom; otherwise show a "↓ New" pill button bottom-right
  that scrolls down when tapped.
- Input bar pinned to bottom: `TextField(axis: .vertical)`, lineLimit 1...5,
  send button (`paperplane.fill`). Send disabled when the trimmed text is
  empty or a turn is currently streaming (one in-flight turn at a time,
  client-enforced; the server enforces it too).
- Keyboard: `.scrollDismissesKeyboard(.interactively)`.
- Manual refresh: an `arrow.clockwise` button in the Chat nav bar (next to
  the gear) — NOT pull-to-refresh, which is unreachable at the top of a
  long transcript. Tap → `GET /health`; on success show a transient
  checkmark + "Inkling online" caption for 2 seconds and then run a history
  sync; on failure "Inkling unreachable". This is the only health UI.

### B8.2 Message model and states

`ChatMessage`: `localID: UUID`, `serverID: String?`, `role: String`
(`"user"`/`"assistant"`; render unknowns as assistant), `text: String`,
`date: Date` (local) , `serverTS: Int?` (epoch ms), `state`, and
`voiceOriginated: Bool` (Part C; always false until then).

`state` enum: `sent` (user msg, delivered — a stream opened for it),
`queued` (user msg, got 429), `failed` (user msg, never reached server),
`streaming` (assistant, in progress), `done` (assistant, complete),
`incomplete` (assistant, stream dropped), `errored` (assistant, non-zero
exit).

### B8.3 The 429 "queued" flow (differs from any v1 instinct — implement exactly)

On 429 the server has SAVED the message and will answer it when the current
turn (usually a scheduled job) finishes. Therefore:

- Mark the user bubble `queued`, caption under it: "Queued — Inkling is
  busy; it'll answer shortly." No Retry button. Never resend.
- Start polling `GET /health` every 10 seconds, up to 18 attempts (3
  minutes). The moment a poll returns `busy:false` — or the attempts run
  out — stop polling and run a history sync (§B8.6). The sync brings back
  both the user message (server-recorded) and the assistant's answer;
  reconciliation (§B8.6) replaces the local `queued` bubble.
- Only one queued-poll loop at a time; a second 429 while one is running
  just marks its bubble `queued` and lets the existing loop cover both.
- Sending is allowed while a message is queued (the field re-enables once
  the 429 response is processed); a follow-up send will most likely 429 into
  the same flow, which is fine.

### B8.4 Failed sends

`failed` (transport error before any HTTP response, or a non-200/401/429
status): caption "Not sent." + Retry button. Retry resends the SAME text as
a NEW POST and flips the bubble back to `sent`. Retry is safe here precisely
because no response means no persisted trigger; do not add retry anywhere
else.

### B8.5 Mid-stream drops

Network failure after the stream opened but before `done`: keep the partial
text, mark the assistant bubble `incomplete` with footnote "connection
dropped — reply may be incomplete". Detection must NOT rely on URLSession's
timeout alone — it resets across app suspensions (seen live: a server
crash left typing dots animating 6+ minutes and `isTurnInFlight` stuck).
A watchdog marks the turn incomplete after 90 s with no stream frame, and
a cold launch demotes any cached `.streaming` message to `incomplete`
(no task exists behind it). A server crash mid-turn also ends here: the
server marks the crashed trigger `error` and does not re-run it, so the
bubble stays incomplete and the user re-asks. Do NOT offer retry (the server finishes
the turn and commits state server-side; a resend would run a second turn).
Then schedule ONE history sync 5 seconds later: the completed reply is in
the transcript by then and reconciliation replaces the partial bubble via
the prefix rule below.

### B8.6 History sync (server is the source of truth)

`ChatStore.syncHistory()` — invoke at exactly these moments: on launch after
loading the local cache; on every transition to `scenePhase == .active`;
after the refresh button's health check succeeds; when a queued-poll loop ends
(§B8.3); 5 seconds after a mid-stream drop (§B8.5). If a stream is in
flight when a sync would run, DEFER it until the stream completes; deferred
syncs run once, not queued in multiples. Never mutate the message list
mid-stream.

Fetch `GET /history?since=<largest serverTS known locally, else 0>`, then
merge (implement exactly):

1. Dedupe by `serverID`.
2. **Reconciliation:** a fetched entry with an id no local message has, that
   matches a local message with `serverID == nil` — same role, `ts` within
   ±3 minutes of the local `date`, and (server text == local text OR server
   text `hasPrefix` local text OR local text is empty) — REPLACES that local
   message, adopting the server id, ts, and full text, with state `sent`
   (user) / `done` (assistant). The prefix clause is what heals
   `incomplete` partial bubbles and empty errored bubbles; the equal clause
   heals streamed turns and `queued` user messages.
3. Everything else fetched is appended as new messages (this is how
   server-originated nudges arrive; role per §B3b).
4. Sort by server ts ascending; messages without a server ts (unsent /
   failed / in-flight) sort by local date, interleaved.
5. Persist the merged list to the local cache (§B10), trimmed to the most
   recent 200.
6. If the sync added any messages, notify Agenda/Log stores to refresh
   (§B5 policy).

Synced-in messages obey the auto-scroll rule (§B8.1).

### B8.7 Document attach (paperclip)

A paperclip button sits left of the text field in the chat input bar, same
glass treatment as the send button (§B2).

- Tap → `.fileImporter` restricted to `UTType.pdf`. On pick, POST the bytes
  as `multipart/form-data` to `/documents` (§B3g).
- Long-press → an alert with a single text field, title "Ingest a web
  page", prefilled from the pasteboard when it holds an http(s) URL. Submit
  → JSON `{"url": ...}` to the same endpoint.
- On send, append a local user bubble `[document: <name-or-url>]` in state
  `sending`. On 202 → state `sent` (it will NOT be healed by history sync —
  the server transcript has no matching user line — so it keeps its local
  ts and stays; exempt it from §B8.6 step 2 matching by giving it a
  synthetic `serverID` of `"doc-<document_id>"`). On 4xx/422 → state
  `failed` with the server's `error` string underneath, tap to retry.
- After a 202, trigger a history sync after 5 s and again after 20 s (the
  extraction turn takes model-inference time); the proposal bubble arrives
  as a normal assistant message. No SSE, no polling loop.
- The proposal asks the user to confirm — that's just typing in chat.
  Nothing else in the app changes; commitments appear on the Today pane via
  the ordinary refresh after the user confirms.

## B9. SettingsSheet (modal sheet; the only screen outside the tabs)

- Field 1: Server URL. Default `https://inkling.example.com`. Validate
  it parses as a URL with an `https` scheme before saving; reject otherwise
  with inline red text "Must be an https:// URL".
- Field 2: Bearer token, `SecureField`. Placeholder shows "••••••• (saved)"
  when one exists; typing replaces it.
- "Test connection" button: calls `GET /history?since=<current epoch ms>`
  with the CURRENT field values (not the saved ones). 200 → green "OK".
  401 → red "Token rejected". Anything else → red "Unreachable". (Not
  `/health` — it succeeds without auth and would green-light a bad token.)
- "Clear local caches" button, destructive style, confirmation dialog:
  deletes the chat, agenda, and log cache files. Does not touch the server
  or the Keychain.
- Save button → persist (§B10), dismiss.
- Part C adds two voice fields (§C2). Nothing else. No theme toggle, no
  font size.

## B10. Persistence

- Bearer token: Keychain, `kSecClassGenericPassword`, service
  `com.sharduldhande.inkling`, account `chat-token`, accessibility
  `kSecAttrAccessibleAfterFirstUnlock`. Write your own ~40-line wrapper; no
  library.
- Server URL: `UserDefaults`, key `serverURL`.
- Display caches, all JSON files in the app's Documents directory, all
  convenience-only (the durable truth lives on the server):
  - `chat-history.json` — the merged message list, most recent 200.
  - `agenda-cache.json` — last successful `/agenda` response.
  - `log-cache.json` — last successful `/measurements` response.
  Load each on launch before the first network fetch; overwrite on every
  successful fetch/merge.

## B11. URL scheme

Register scheme `inkling` (URL Types → Schemes: `inkling`, identifier
`com.sharduldhande.inkling.urlscheme`). Handle via `.onOpenURL`: for ANY
incoming `inkling://` URL — including exactly `inkling://open` — switch to
the Chat tab and trigger a history sync. Unknown hosts/paths: same behavior,
never crash. (A push notification tap posts the same `inklingOpenURL`
notification from the UNUserNotificationCenter delegate; the body was already
shown by the system — there is nothing to fetch beyond the normal sync.)

## B12. Project structure (create exactly these files)

```
Inkling/
  InklingApp.swift       // @main, WindowGroup { RootView() }, .onOpenURL here
  RootView.swift         // TabView shell (§B4), first-run settings presentation
  Models.swift           // ChatMessage (§B8.2), AgendaItem, LogEntry, decoders
  InklingClient.swift    // health(), fetchHistory(since:), fetchAgenda(),
                         // fetchMeasurements(), fetchDays(), streamChat(message:)
                         //   -> AsyncThrowingStream<ChatEvent, Error>
                         // ChatEvent enum: .text(String), .done, .exit(Int)
                         // errors distinguish: .unauthorized, .busy429,
                         // .transport, .status(Int)
  ChatStore.swift        // @Observable: messages, send(), queued-poll loop,
                         // syncHistory() + merge, cache load/save
  AgendaStore.swift      // @Observable: items, refresh(), cache load/save
  LogStore.swift         // @Observable: entries + days/targets/weight,
                         // refresh() (fetches /days + /measurements),
                         // cache load/save
  KeychainHelper.swift   // minimal generic-password get/set/delete
  ChatView.swift         // §B8
  AgendaView.swift       // §B5
  LogView.swift          // §B6
  SettingsSheet.swift    // §B9
  MessageBubble.swift    // bubble + state captions/footnotes
```

Bundle identifier: `com.sharduldhande.inkling`. Display name: "Inkling".
All user-visible strings hardcoded English; no localization scaffolding. No
launch-screen artwork; no app icon required (may be added later).

## B13. Behavior details — do not improvise

- Exactly one in-flight `POST /chat` at a time, client-enforced by the
  disabled send button.
- The user's message appears in the list immediately on send (state `sent`
  optimistically; corrected to `queued`/`failed` by the response).
- Stores refresh but NEVER write. No mark-done, no snooze, no delete,
  anywhere.
- Do not implement read receipts, message editing/deletion, reactions,
  images, markdown rendering, haptics beyond default button feedback, or
  notification handling of any kind. No charts beyond §B6.2's two heatmaps;
  no totals beyond §B6.1's tiles.
- Decode all server ids as `String`, all timestamps as epoch-millisecond
  `Int`, all `role`/`kind`/`type` discriminators as `String` with lenient
  fallbacks (§B3b/§B3d/§B3e/§B3f).
- The Log pane's two permitted computations (§B3f) are the ONLY client-side
  arithmetic over server data anywhere in the app.
- Every JSONDecoder in the app uses default key decoding (the wire is
  already lowercase snake-free); never `.convertFromSnakeCase`.

## B14. Acceptance checklist (verify each before declaring Part B done)

1. Fresh install → SettingsSheet auto-presents → paste token → Test
   connection shows OK; wrong token shows "Token rejected".
2. Cold launch lands on Chat tab; sending "status?" shows the user bubble
   instantly, typing dots, streamed reply growing, clean finish.
3. Send during an active stream is impossible (button disabled).
4. Send while the server is mid-job (or simulate a 429) → bubble shows
   "Queued…", NO retry button, and within ~3 minutes the queued bubble and
   its answer both appear via sync, with no duplicate user message.
5. Airplane mode before send → `failed` + Retry; Retry after reconnect
   works. Airplane mode mid-stream → partial text kept, "connection
   dropped" footnote, no retry — and after reconnecting + foregrounding,
   the full reply replaces the partial bubble (prefix reconciliation).
6. A nudge created server-side while the app is backgrounded appears as an
   assistant bubble, in chronological position, on foregrounding.
7. Today tab: an overdue commitment shows in a red "Overdue" section; a
   commitment due in 3 days does NOT appear; a calendar event tomorrow
   does. Empty state renders when the server returns no items. Telling the
   chat a task is done moves it to a "Done" section (green check, "Done
   moments ago") on the post-turn refresh; it drops off Today a day later.
   Recurring habits sit in their own "Recurring" section below the tasks;
   telling the chat a habit is done flips its circle to a green check for
   the rest of the local day and silences that day's reminder, while the
   habit's next occurrence still lists live.
8. Log tab: quota tiles show remaining vs quota from `/days` (and the
   "Set a quota in chat" state when targets are absent); both heatmaps
   render 12 weeks with correct colors, neutral prehistory, and a ringed
   never-red today; ledger entries grouped by day, newest first; a
   superseded meal shows only its replacement; values render like
   "650 kcal" (no trailing .0).
9. Tapping an agenda row and a log row each switch to Chat with the exact
   §B7 prefill, keyboard up, nothing auto-sent.
10. Kill and relaunch in airplane mode → all three panes render from disk
    caches.
11. `xcrun simctl openurl booted "inkling://open"` brings the app to the
    Chat tab without crashing.
12. Wrong token at runtime → single alert, no crash, no retry loop.
13. No entitlements beyond the defaults of a free personal team; zero
    third-party dependencies in the project.

---

# PART C — Voice (Deepgram STT + TTS)

Build only after B14 passes. Everything in Part B remains in force; nothing
about the server contract changes — voice input becomes a normal
`POST /chat`, and reply handling is unchanged.

## C1. Overview

- **STT:** live streaming transcription over WebSocket to Deepgram
  (`wss://api.deepgram.com/v1/listen`), interim results shown live as the
  user speaks.
- **TTS:** synthesize assistant replies via Deepgram Aura
  (`POST https://api.deepgram.com/v1/speak`) and play them.
- The app talks to Deepgram directly; the VPS is not involved.

## C2. Settings additions

- **Deepgram API Key** — `SecureField`; Keychain (same wrapper, same
  service, account `deepgram-key`). Never UserDefaults, never logged.
- **Auto-speak replies** — toggle, default ON (UserDefaults key
  `autoSpeak`).
- **Test Deepgram** button — TTS the word "test" and play it; inline green
  OK / red failure.

With no key set: mic button visible but dimmed/disabled; tapping it shows
the hint "Add a Deepgram API key in Settings." Text chat must work exactly
as before with no key set.

## C3. Mic UX (hold-to-talk + tap-to-toggle)

Mic button in the chat input bar, left of the send button. Two modes on one
button, disambiguated by a long-press threshold of 0.3 s:

1. **Press-and-hold:** record while held; on release, send the final
   transcript automatically.
2. **Tap-to-toggle:** tap starts recording, tap again stops and sends.

While recording: mic button filled red with a subtle pulse; the text field
shows the live transcript (`finalizedText + " " + interimText`, interim
replacing as results arrive). A visible ✕ cancel affordance discards
everything and sends nothing.

On stop: close the socket cleanly (send `{"type":"CloseStream"}`, wait up to
1 s for the last final result), trim whitespace; non-empty → send through
the EXACT same code path as a typed message, with the resulting
`ChatMessage.voiceOriginated = true` (local flag only; the wire bytes are
identical to typed). Empty → do nothing.

- `NSMicrophoneUsageDescription` in Info.plist: "Inkling uses the microphone
  to transcribe what you say into chat messages." Denied permission → alert
  directing to iOS Settings.
- The mic button is disabled while a turn is streaming (same rule as send).

## C4. STT implementation

- Capture with `AVAudioEngine` input-node tap; convert with
  `AVAudioConverter` to 16-bit little-endian linear PCM, 16 kHz, mono; send
  raw PCM as binary WebSocket messages in chunks of roughly 20–100 ms.
- `URLSessionWebSocketTask` to:

  ```
  wss://api.deepgram.com/v1/listen?model=nova-3&encoding=linear16&sample_rate=16000&channels=1&interim_results=true&smart_format=true&endpointing=300
  ```

  Header: `Authorization: Token <deepgramApiKey>`.
- Each incoming `Results` message: read
  `channel.alternatives[0].transcript` and `is_final`; accumulate
  `finalizedText` from finals, hold the latest interim separately.
- Errors (socket failure, 401): stop recording, KEEP any captured text in
  the input field for manual edit/send, brief non-blocking "Transcription
  failed" hint. Never crash, never lose captured text.
- `AVAudioSession`: `.playAndRecord`, `.defaultToSpeaker`,
  `.allowBluetooth`; deactivate when idle.

## C5. TTS implementation

- `POST https://api.deepgram.com/v1/speak?model=aura-2-thalia-en`, headers
  `Authorization: Token <key>`, `Content-Type: application/json`, body
  `{"text":"<reply text>"}`; response audio (MP3) played via `AVAudioPlayer`
  from in-memory `Data`.
- **Auto-speak rule:** user turn was `voiceOriginated` AND Auto-speak ON →
  when that reply's stream completes (`done`), speak the full reply. Typed
  messages never auto-speak. Synced-in nudge bubbles never auto-speak.
- **Tap-to-speak:** tapping any assistant bubble speaks it; tapping again
  stops. One playback at a time; starting a new one stops the current.
- Replies over ~2000 chars: split on sentence boundaries into ≤2000-char
  chunks, play sequentially.
- TTS failures: brief hint, never block chat, no retry loops.

New files: `DeepgramTranscriptionService.swift`, `DeepgramSpeechService.swift`.

## C6. Voice acceptance checklist

1. With NO Deepgram key: everything in B14 still passes; mic dimmed with
   hint.
2. Key entered → persists across relaunch (Keychain); mic enables.
3. Hold-to-talk and tap-to-toggle both: live words appear while speaking;
   stop → sends; reply streams normally.
4. Cancel while recording → nothing sent, input cleared.
5. Auto-speak ON + voice message → reply spoken after `done`; OFF → silent;
   typed → never spoken; nudges → never auto-spoken.
6. Tap assistant bubble → speaks; tap again → stops.
7. Network killed mid-recording → error hint, partial transcript preserved,
   no crash.
8. Mic permission denied → alert, text chat unaffected.

---

# PART D — Explicitly out of scope (do not build any of this)

- Lock-screen / notification action buttons (ntfy actions). Good future
  idea, blocked on server-side scoped one-time tokens — putting the bearer
  token in a push payload is credential exposure. Not in this build.
- HealthKit / automatic fitness ingestion. Future; stays on-device or as
  daily summaries per the server's design stances. Not in this build.
- Browsable full todo/habit lists, search screens, stats dashboards,
  charts, streaks, totals.
- Any second write path: no complete/snooze/edit buttons, no forms.
- Widgets, watch app, iPad layout work, push entitlements, background
  refresh.
