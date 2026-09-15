-- Calendar becomes read-only ICS feed sync (§12 rewrite): feeds are listed in a
-- per-user config file, polled on a timer, and diffed as snapshots. Nothing in
-- the system can write to a calendar; the tables only mirror what the feed says.
--
-- calendar_sync_state: one row per configured feed (provider 'ics'). The
-- oauth_token_file column belonged to the retired Google OAuth path; it is kept
-- nullable so old rows survive but nothing writes it. `enabled` is flipped to
-- false when a feed disappears from the config file (no DELETE grant, D10).
ALTER TABLE calendar_sync_state ALTER COLUMN oauth_token_file DROP NOT NULL;
ALTER TABLE calendar_sync_state ADD COLUMN summary text;
ALTER TABLE calendar_sync_state ADD COLUMN enabled boolean NOT NULL DEFAULT true;

-- calendar_events_cache: a removed or cancelled event is a status tombstone,
-- never a deleted row (runtime_role has no DELETE). all_day carries the
-- feed's date-only DTSTART so consumers render "all day" instead of 00:00.
ALTER TABLE calendar_events_cache
  ADD COLUMN status text NOT NULL DEFAULT 'confirmed';
ALTER TABLE calendar_events_cache
  ADD COLUMN all_day boolean NOT NULL DEFAULT false;

CREATE INDEX ON calendar_events_cache (user_id, starts_at)
  WHERE status <> 'cancelled';
