"""Shared test fixtures."""
import pytest


@pytest.fixture(autouse=True)
def reset_db_state():
    """Reset DB state between tests."""
    import behive.engine.db_helpers as dbh
    old_pg = dbh._pg_available
    old_hive = dbh._hive_db
    yield
    dbh._pg_available = old_pg
    dbh._hive_db = old_hive
