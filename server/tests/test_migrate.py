from inkling.db.migrate import apply_all


def test_migrations_are_idempotent(dsn, migrated):
    """A second run applies nothing and changes nothing."""
    assert apply_all(dsn) == []
