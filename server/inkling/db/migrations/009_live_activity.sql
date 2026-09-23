-- "Next up" Live Activity (§11): one persistent lock-screen card per user,
-- driven entirely by the server over APNs liveactivity pushes.
--
-- start_token is the app's push-to-start token (lets the server create an
-- activity while the app isn't running); update_token/activity_id belong to
-- the activity currently on screen and are reported back by the app when it
-- is woken for it. iOS ends an activity after 8 hours, so started_at drives a
-- restart before that; content is the last content-state sent, so a pass
-- only pushes when what the card should say actually changed.

CREATE TABLE live_activity (
  user_id       text PRIMARY KEY REFERENCES users(id),
  environment   text NOT NULL CHECK (environment IN ('development', 'production')),
  start_token   text,
  update_token  text,
  activity_id   text,
  started_at    timestamptz,
  content       jsonb,
  pushed_at     timestamptz,
  updated_at    timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE live_activity ENABLE ROW LEVEL SECURITY;
ALTER TABLE live_activity FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON live_activity
  USING (user_id = current_setting('app.user_id', true))
  WITH CHECK (user_id = current_setting('app.user_id', true));

GRANT SELECT, INSERT, UPDATE ON live_activity TO runtime_role;
