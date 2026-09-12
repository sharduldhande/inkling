"""Operator seed: adding a user is a seed row + credentials (§0).

Runs on the migrate connection because FORCE RLS applies to the owner too:
the insert must happen with app.user_id set to the new user's id.
"""

import json

import psycopg


def seed_user(
    dsn: str,
    *,
    user_id: str,
    display_name: str,
    timezone: str,
    ntfy_topic: str | None = None,
    inference_profile: dict,
) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
        conn.execute(
            """
            INSERT INTO users (id, display_name, timezone, ntfy_topic, inference_profile)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (user_id, display_name, timezone, ntfy_topic, json.dumps(inference_profile)),
        )
        conn.commit()
