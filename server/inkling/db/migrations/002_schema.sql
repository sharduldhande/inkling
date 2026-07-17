-- Core schema (§4, normative DDL). Conventions: all timestamps timestamptz;
-- every table carries user_id text NOT NULL except pure-system tables.

-- 4.1 users
CREATE TABLE users (
  id                 text PRIMARY KEY,            -- e.g. 'shardul'
  display_name       text NOT NULL,
  timezone           text NOT NULL,               -- IANA, e.g. 'America/New_York'
  quiet_start        time NOT NULL DEFAULT '03:00',
  quiet_end          time NOT NULL DEFAULT '11:59',
  ntfy_topic         text NOT NULL,
  inference_profile  jsonb NOT NULL,
  calendar_enabled   boolean NOT NULL DEFAULT false,
  created_at         timestamptz NOT NULL DEFAULT now()
);

-- 4.2 triggers (ingress inbox)
CREATE TABLE triggers (
  id               bigserial PRIMARY KEY,
  user_id          text NOT NULL REFERENCES users(id),
  source           text NOT NULL,     -- chat | wakeup | occurrence | calendar | job
  source_ref       text,              -- wakeup id / occurrence id / provider event id
  idempotency_key  text NOT NULL,
  payload          jsonb NOT NULL,
  priority         smallint NOT NULL DEFAULT 3,
    -- 1 chat · 2 occurrence · 3 calendar/wakeup · 4 maintenance
  received_at      timestamptz NOT NULL DEFAULT now(),
  status           text NOT NULL DEFAULT 'pending',
    -- pending | running | done | error | coalesced
  turn_id          bigint,
  UNIQUE (user_id, idempotency_key)
);
CREATE INDEX ON triggers (user_id, status, priority, received_at);

-- 4.3 turns and turn_events (persisted stream)
CREATE TABLE turns (
  id           bigserial PRIMARY KEY,
  user_id      text NOT NULL REFERENCES users(id),
  trigger_id   bigint NOT NULL REFERENCES triggers(id),
  status       text NOT NULL DEFAULT 'running',   -- running | ok | error
  leased_by    text,                              -- worker id
  lease_until  timestamptz,                       -- heartbeat-extended
  model        text,                              -- recorded per turn (gateway fills)
  input_tokens int,
  output_tokens int,
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  error        text
);

CREATE TABLE turn_events (
  id       bigserial PRIMARY KEY,
  user_id  text NOT NULL,
  turn_id  bigint NOT NULL REFERENCES turns(id),
  seq      int  NOT NULL,                -- per-turn ordinal for Last-Event-ID
  kind     text NOT NULL,                -- text_delta | done | exit | error
  data     jsonb NOT NULL,
  ts       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (turn_id, seq)
);

-- 4.4 events (episodic, append-only — runtime grants are SELECT/INSERT only, D10)
CREATE TABLE events (
  id            bigserial PRIMARY KEY,
  user_id       text NOT NULL REFERENCES users(id),
  occurred_at   timestamptz NOT NULL,     -- when it happened in the world
  ingested_at   timestamptz NOT NULL DEFAULT now(),  -- when we learned of it
  kind          text NOT NULL,  -- habit_done|habit_skipped|measurement|checkin|
                                -- note|correction|ingestion|system
  habit_id      text,
  value_num     numeric,
  unit          text,
  note          text,
  corrects_id   bigint REFERENCES events(id),
  source        text NOT NULL DEFAULT 'conversation',
  turn_id       bigint,
  search        tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(note,''))) STORED
);
CREATE INDEX ON events (user_id, occurred_at);
CREATE INDEX ON events (user_id, kind, habit_id, occurred_at);
CREATE INDEX events_fts ON events USING gin (search);

-- 4.5 habits
CREATE TABLE habits (
  user_id text NOT NULL REFERENCES users(id),
  id      text NOT NULL,
  title   text NOT NULL,
  cadence text,                     -- RRULE or informal
  active  boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, id)
);

-- 4.6 commitments, occurrences, deliveries (the never-forget machinery)
CREATE TABLE commitments (
  id          bigserial PRIMARY KEY,
  user_id     text NOT NULL REFERENCES users(id),
  title       text NOT NULL,
  body        text,
  due_at      timestamptz,          -- one-shot
  recurrence  text,                 -- RRULE (mutually exclusive with due_at)
  lead_time   interval NOT NULL DEFAULT '0',
  status      text NOT NULL DEFAULT 'open',  -- open|done|cancelled
  version     int  NOT NULL DEFAULT 1,       -- optimistic concurrency
  source      text NOT NULL,        -- conversation|ingestion:<id>|agent
  source_ref  text,
  notify_policy jsonb NOT NULL DEFAULT '{"repeat_after_min":15,"escalate_after_min":45,"stop_on_ack":true,"quiet_hours":"defer"}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (due_at IS NULL OR recurrence IS NULL)
);

CREATE TABLE commitment_occurrences (
  id             bigserial PRIMARY KEY,
  user_id        text NOT NULL REFERENCES users(id),
  commitment_id  bigint NOT NULL REFERENCES commitments(id),
  fire_at        timestamptz NOT NULL,       -- due_at - lead_time (or recurrence instance)
  status         text NOT NULL DEFAULT 'pending',
    -- pending → eligible → leased → surfaced | dead_letter ; also: skipped_done
  leased_by      text,
  lease_until    timestamptz,
  surfaced_turn  bigint,
  acknowledged_at timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (commitment_id, fire_at)             -- idempotent materialization
);
CREATE INDEX ON commitment_occurrences (status, fire_at);

CREATE TABLE deliveries (
  id               bigserial PRIMARY KEY,
  user_id          text NOT NULL REFERENCES users(id),
  occurrence_id    bigint REFERENCES commitment_occurrences(id),
  turn_id          bigint,                    -- non-occurrence notifications
  channel          text NOT NULL DEFAULT 'ntfy',
  destination      text NOT NULL,             -- topic
  body             text NOT NULL,
  idempotency_key  text NOT NULL UNIQUE,
  not_before       timestamptz,               -- quiet-hours deferral lands here
  expires_at       timestamptz,
  attempt_count    int NOT NULL DEFAULT 0,
  status           text NOT NULL DEFAULT 'queued',
    -- queued → leased → sent | retry_wait → (retry) | dead_letter
  provider_msg_id  text,
  last_error       text,
  sent_at          timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON deliveries (status, (coalesce(not_before, created_at)));

-- 4.7 docs (working state, patterns, plan, goals — versioned prose)
CREATE TABLE docs (
  id         bigserial PRIMARY KEY,
  user_id    text NOT NULL REFERENCES users(id),
  kind       text NOT NULL,     -- working_state|patterns|daily_plan|goals|digest7
  version    int  NOT NULL,
  content    text NOT NULL,     -- markdown
  written_by text NOT NULL,     -- agent:turn:<id> | job:consolidate | human
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, kind, version)
);

-- 4.8 transcript
CREATE TABLE transcript (
  id        bigserial PRIMARY KEY,
  user_id   text NOT NULL REFERENCES users(id),
  turn_id   bigint REFERENCES turns(id),
  ts        timestamptz NOT NULL DEFAULT now(),
  role      text NOT NULL,      -- user | assistant | system_nudge
  text      text NOT NULL,
  search    tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding vector(1536),       -- pgvector; NULL until FEATURE_EMBEDDINGS
  embed_model text              -- record model+dim with every vector
);
CREATE INDEX ON transcript (user_id, ts);
CREATE INDEX transcript_fts ON transcript USING gin (search);

-- 4.9 scheduled_wakeups
CREATE TABLE scheduled_wakeups (
  id            bigserial PRIMARY KEY,
  user_id       text NOT NULL REFERENCES users(id),
  fire_at       timestamptz,
  recurrence    text,
  purpose       text NOT NULL,
  created_by    text NOT NULL,   -- seed | agent:turn:<id> | event:<ref>
  respect_quiet boolean NOT NULL DEFAULT true,
  status        text NOT NULL DEFAULT 'active',  -- active|fired|cancelled
  CHECK (fire_at IS NOT NULL OR recurrence IS NOT NULL)
);

-- 4.10 calendar, ingestion, semantic facts, audit
CREATE TABLE calendar_sync_state (
  user_id text NOT NULL REFERENCES users(id),
  provider text NOT NULL DEFAULT 'google',
  calendar_id text NOT NULL,
  sync_cursor text,
  channel_id text,
  channel_expires_at timestamptz,
  last_synced_at timestamptz,
  last_full_resync_at timestamptz,
  oauth_token_file text NOT NULL,     -- path on disk, never the token itself
  PRIMARY KEY (user_id, provider, calendar_id)
);

CREATE TABLE calendar_events_cache (
  user_id text NOT NULL,
  provider_event_id text NOT NULL,
  calendar_id text NOT NULL,
  revision text,
  starts_at timestamptz,
  ends_at timestamptz,
  summary text,
  created_by_us boolean NOT NULL DEFAULT false,
  raw jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, provider_event_id)
);

CREATE TABLE ingested_documents (
  id bigserial PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(id),
  title text NOT NULL,
  storage_path text NOT NULL,
  extracted jsonb NOT NULL,
  confirmed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- typed semantic facts: written only by the consolidation job (never mid-turn)
CREATE TABLE memory_facts (
  id bigserial PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(id),
  subject text NOT NULL,
  predicate text NOT NULL,
  value text NOT NULL,
  condition text,
  confidence real NOT NULL DEFAULT 0.5,
  evidence_event_ids bigint[] NOT NULL DEFAULT '{}',
  first_seen timestamptz NOT NULL DEFAULT now(),
  last_confirmed timestamptz,
  invalidated_at timestamptz
);

-- pure-system table: user_id nullable, no RLS (operator/system view)
CREATE TABLE audit_log (
  id bigserial PRIMARY KEY,
  user_id text,
  actor text NOT NULL,       -- worker id | job | api
  action text NOT NULL,
  detail jsonb NOT NULL,
  ts timestamptz NOT NULL DEFAULT now()
);
