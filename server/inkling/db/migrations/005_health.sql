-- Typed health ledgers keep quantities unambiguous and leave events episodic.
-- Supersession is append-only: a correction points at the row it replaces.

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

-- FORCE keeps the owning role inside the same tenant boundary as runtime.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['intake', 'workouts', 'body_metrics'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format($p$
      CREATE POLICY tenant_isolation ON %I
        USING (user_id = current_setting('app.user_id', true))
        WITH CHECK (user_id = current_setting('app.user_id', true))
    $p$, t);
  END LOOP;
END $$;

-- Append-only surfaces: supersession is a new row, so no UPDATE, no DELETE.
GRANT SELECT, INSERT ON intake, workouts, body_metrics TO runtime_role;
-- SetTargets is the ONLY runtime write to users, and only to this column.
GRANT UPDATE (targets) ON users TO runtime_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO runtime_role;
