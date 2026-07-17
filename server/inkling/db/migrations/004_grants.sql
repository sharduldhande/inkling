-- runtime_role grants: SELECT/INSERT/UPDATE per table as needed; never DELETE
-- on user data (D10), never DDL. events is SELECT/INSERT only — corrections
-- are new rows referencing corrects_id, and the grant level enforces it.

GRANT SELECT ON users TO runtime_role;

GRANT SELECT, INSERT, UPDATE ON
  triggers,
  turns,
  habits,
  commitments,
  commitment_occurrences,
  deliveries,
  scheduled_wakeups,
  calendar_sync_state,
  calendar_events_cache,
  ingested_documents,
  memory_facts
TO runtime_role;

-- Append-only surfaces: no UPDATE, no DELETE.
GRANT SELECT, INSERT ON
  events,
  turn_events,
  docs,
  transcript,
  audit_log
TO runtime_role;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO runtime_role;
