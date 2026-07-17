# inkling-server

Implementation of the Inkling spec (`../inkling-architecture.md`).

## Dev setup

```sh
uv sync
```

## Tests

The suite needs real Postgres (RLS semantics) and runs against a Neon test
branch — it drops and recreates the schema, and refuses any URL whose host or
database name lacks a "test" marker.

```sh
export TEST_DATABASE_URL=$(neonctl connection-string inkling-test \
  --project-id broad-sound-43290851 --database-name inkling_test \
  --role-name neondb_owner)
uv run pytest
```

## Operator commands

```sh
python -m inkling migrate   # apply migrations (uses DATABASE_URL_MIGRATE)
python -m inkling seed --user shardul --name Shardul --tz America/New_York \
  --ntfy-topic <topic> --inference-profile '{"type":"subscription","config_dir":"..."}'
```
