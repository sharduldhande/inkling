-- A calendar deadline the user says is handled (e.g. a Canvas assignment
-- submitted through Gradescope, which Canvas never learns about). The feed
-- stays read-only; this is Inkling's own annotation on its cached copy, so
-- the sync upsert never touches it. NULL = not marked.
ALTER TABLE calendar_events_cache ADD COLUMN done_at timestamptz;
