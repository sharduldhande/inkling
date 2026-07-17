-- Extensions and the runtime role.
-- pgvector is present from day one (schema includes transcript.embedding) but
-- FEATURE_EMBEDDINGS stays false; no code path writes vectors yet (D9/A5).
CREATE EXTENSION IF NOT EXISTS vector;

-- runtime_role owns nothing and gets narrow grants (004). Created NOLOGIN;
-- the operator attaches LOGIN + password out of band (or maps a Neon role).
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'runtime_role') THEN
    CREATE ROLE runtime_role NOLOGIN;
  END IF;
END $$;

-- Lets the migrating user (and tests) SET ROLE runtime_role.
GRANT runtime_role TO current_user;

GRANT USAGE ON SCHEMA public TO runtime_role;
