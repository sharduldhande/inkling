-- APNs device registry (§11). One row per (user, device token); the app
-- re-POSTs its token on every launch, so rows are upserted on token and
-- carry last_seen_at. Revocation is an UPDATE (revoked_at), never a DELETE:
-- runtime_role has no DELETE on user data (D10), and a revoked token that the
-- app re-registers simply comes back to life.
--
-- environment is per device because it is a property of how that install was
-- signed (development profile → sandbox host, App Store/Ad Hoc → production);
-- the server cannot know it, so the app reads its embedded profile and says.

CREATE TABLE devices (
  id             bigserial PRIMARY KEY,
  user_id        text NOT NULL REFERENCES users(id),
  platform       text NOT NULL DEFAULT 'ios',
  token          text NOT NULL UNIQUE,   -- APNs device token, lowercase hex
  environment    text NOT NULL CHECK (environment IN ('development', 'production')),
  app_version    text,
  registered_at  timestamptz NOT NULL DEFAULT now(),
  last_seen_at   timestamptz NOT NULL DEFAULT now(),
  revoked_at     timestamptz,
  revoke_reason  text
);
CREATE INDEX devices_active_idx ON devices (user_id) WHERE revoked_at IS NULL;

-- 003's RLS loop already ran; new user tables carry their own policy.
ALTER TABLE devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE devices FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON devices
  USING (user_id = current_setting('app.user_id', true))
  WITH CHECK (user_id = current_setting('app.user_id', true));

GRANT SELECT, INSERT, UPDATE ON devices TO runtime_role;
-- 004's ALL SEQUENCES grant covered only sequences that existed then.
GRANT USAGE, SELECT ON SEQUENCE devices_id_seq TO runtime_role;

-- Legacy: user pushes no longer address a topic. Kept nullable (not dropped)
-- so a rollback to the ntfy release finds the column populated.
ALTER TABLE users ALTER COLUMN ntfy_topic DROP NOT NULL;
