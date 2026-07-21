-- A habit_done event can name the recurring commitment it fulfills, so
-- "did my french practice" checks off today's occurrence instead of leaving
-- the ledger and the never-forget machinery unaware of each other. The
-- commitment stays open (CompleteCommitment ends the whole loop); the event
-- is the daily record, and /agenda projects it as done-today.
ALTER TABLE events ADD COLUMN commitment_id bigint REFERENCES commitments(id);

CREATE INDEX ON events (user_id, commitment_id, occurred_at)
  WHERE commitment_id IS NOT NULL;
