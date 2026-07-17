-- Row-level security: ENABLE + FORCE on every user table (§3) so even the
-- owning role is subject to policies. Tenant context arrives per transaction
-- via set_config('app.user_id', ..., true); with no tenant set,
-- current_setting(..., true) is NULL and every policy evaluates false.

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'triggers', 'turns', 'turn_events', 'events', 'habits',
    'commitments', 'commitment_occurrences', 'deliveries', 'docs',
    'transcript', 'scheduled_wakeups', 'calendar_sync_state',
    'calendar_events_cache', 'ingested_documents', 'memory_facts'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format($p$
      CREATE POLICY tenant_isolation ON %I
        USING (user_id = current_setting('app.user_id', true))
        WITH CHECK (user_id = current_setting('app.user_id', true))
    $p$, t);
  END LOOP;
END $$;

-- users has no user_id column; its id IS the tenant key.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON users
  USING (id = current_setting('app.user_id', true))
  WITH CHECK (id = current_setting('app.user_id', true));
