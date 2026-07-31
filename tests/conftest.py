"""Shared test fixtures."""
import pytest
import os


@pytest.fixture(autouse=True)
def reset_db_state():
    """Reset DB state between tests."""
    import behive.engine.db_helpers as dbh
    old_pg = dbh._pg_available
    old_mod = dbh._db_mod
    yield
    dbh._pg_available = old_pg
    dbh._db_mod = old_mod


@pytest.fixture
def pg_conn():
    """Real PostgreSQL connection for integration tests."""
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432,
            user="hive_app",
            password=os.environ.get("HIVE_PG_PASSWORD", "bH9_xK2m_pR7v_2026"),
            dbname="hive",
        )
        conn.autocommit = False
        yield conn
        conn.rollback()
        conn.close()
    except Exception:
        pytest.skip("PostgreSQL not available")


@pytest.fixture
def pg_cursor(pg_conn):
    """Cursor from the pg_conn fixture."""
    import psycopg2.extras
    cur = pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    yield cur
    cur.close()
