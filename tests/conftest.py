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


@pytest.fixture(autouse=True)
def reset_db_pool():
    """Reset the global psycopg2 pool after each test to prevent exhaustion."""
    yield
    try:
        import behive.engine.db as db_mod
        if hasattr(db_mod, '_pool') and db_mod._pool and not db_mod._pool.closed:
            db_mod._pool.closeall()
            db_mod._pool = None
    except Exception:
        pass


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


@pytest.fixture
def mock_db():
    """Provide a fully mocked DB connection for unit tests."""
    from unittest.mock import MagicMock
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = {"id": "test_1", "status": "done"}
    cursor.fetchall.return_value = []
    cursor.rowcount = 0
    cursor.description = [("id",), ("status",)]
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


@pytest.fixture
def mock_llm():
    """Mock LLM completion for tests that need it."""
    from unittest.mock import patch, MagicMock
    with patch("behive.engine.llm.litellm") as mock_litellm:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content='{"claims": []}'))]
        mock_litellm.completion.return_value = mock_response
        yield mock_litellm
