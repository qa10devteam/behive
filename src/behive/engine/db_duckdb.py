"""
behive.engine.db_duckdb — DuckDB backend for zero-setup local research.

Drop-in replacement for db.py PgConnection interface.
Stores data in ~/.behive/research.duckdb (configurable via BEHIVE_DUCKDB_PATH).

Usage:
    from behive.engine.db_duckdb import connect, DuckDBConnection
    db = connect()
    db.execute("SELECT * FROM hive_claims WHERE mission_id = ?", [mid])
    rows = db.fetchall()
"""

import os
import logging
import threading
from pathlib import Path
from contextlib import contextmanager
from typing import Optional, Any

log = logging.getLogger(__name__)

# ─── Configuration ──────────────────────────────────────────────────────────

DEFAULT_DB_PATH = os.path.expanduser("~/.behive/research.duckdb")
_db_path = os.environ.get("BEHIVE_DUCKDB_PATH", DEFAULT_DB_PATH)

# Thread-local storage for connections (DuckDB is not thread-safe by default)
_local = threading.local()


def _get_db_path() -> str:
    """Get DuckDB file path, creating directory if needed."""
    path = os.environ.get("BEHIVE_DUCKDB_PATH", DEFAULT_DB_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _ensure_schema(conn) -> None:
    """Create tables if they don't exist (DuckDB dialect)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hive_missions (
            mission_id VARCHAR PRIMARY KEY,
            query VARCHAR NOT NULL,
            depth INTEGER DEFAULT 3,
            status VARCHAR DEFAULT 'new',
            phase VARCHAR DEFAULT 'scout',
            config JSON DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            claims_count INTEGER DEFAULT 0,
            avg_quality DOUBLE DEFAULT 0.0,
            duration_seconds DOUBLE DEFAULT 0.0,
            error_message VARCHAR
        )
    """)
    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_sources START 1
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hive_sources (
            id INTEGER DEFAULT nextval('seq_sources'),
            mission_id VARCHAR NOT NULL,
            url VARCHAR NOT NULL,
            domain VARCHAR,
            title VARCHAR,
            relevance_score DOUBLE DEFAULT 0.0,
            source_type VARCHAR DEFAULT 'web',
            discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_content START 1
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hive_content (
            id INTEGER DEFAULT nextval('seq_content'),
            mission_id VARCHAR NOT NULL,
            source_url VARCHAR,
            raw_text VARCHAR,
            clean_text VARCHAR,
            word_count INTEGER DEFAULT 0,
            language VARCHAR DEFAULT 'en',
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_claims START 1
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hive_claims (
            id INTEGER DEFAULT nextval('seq_claims'),
            mission_id VARCHAR NOT NULL,
            claim VARCHAR NOT NULL,
            confidence DOUBLE DEFAULT 0.5,
            quality_score DOUBLE DEFAULT 0.5,
            source_url VARCHAR,
            claim_type VARCHAR DEFAULT 'factual',
            is_garbage BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_entities START 1
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hive_entities (
            id INTEGER DEFAULT nextval('seq_entities'),
            mission_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            entity_type VARCHAR DEFAULT 'unknown',
            description VARCHAR,
            mention_count INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_triples START 1
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hive_triples (
            id INTEGER DEFAULT nextval('seq_triples'),
            mission_id VARCHAR NOT NULL,
            subject VARCHAR NOT NULL,
            predicate VARCHAR NOT NULL,
            object VARCHAR NOT NULL,
            confidence DOUBLE DEFAULT 0.5,
            source_claim_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_events START 1
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hive_events (
            id INTEGER DEFAULT nextval('seq_events'),
            mission_id VARCHAR NOT NULL,
            event_type VARCHAR NOT NULL,
            phase VARCHAR,
            message VARCHAR,
            data JSON,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hive_domain_reputation (
            domain VARCHAR PRIMARY KEY,
            reputation_score DOUBLE DEFAULT 0.5,
            category VARCHAR,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_bee_results START 1
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hive_bee_results (
            id INTEGER DEFAULT nextval('seq_bee_results'),
            mission_id VARCHAR NOT NULL,
            bee_id VARCHAR,
            task_type VARCHAR,
            status VARCHAR DEFAULT 'pending',
            result JSON,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    log.debug("DuckDB schema ensured at %s", _get_db_path())


class DuckDBConnection:
    """
    DuckDB connection wrapper — same interface as PgConnection.
    Supports: execute(sql, params), fetchone(), fetchall(), close().
    """

    def __init__(self, read_only: bool = False):
        import duckdb
        self._path = _get_db_path()
        self._conn = duckdb.connect(self._path, read_only=read_only)
        self._result = None
        self._read_only = read_only
        # Auto-create schema on first connection
        if not read_only:
            _ensure_schema(self._conn)

    def execute(self, sql: str, params=None) -> 'DuckDBConnection':
        """Execute SQL. Returns self for chaining (.fetchall())."""
        # Adapt PostgreSQL-specific syntax to DuckDB
        sql = self._adapt_sql(sql)

        # Convert %s placeholders to ? (DuckDB uses ?)
        if params and '%s' in sql:
            sql = sql.replace('%s', '?')

        try:
            if params:
                self._result = self._conn.execute(sql, params if isinstance(params, (list, tuple)) else [params])
            else:
                self._result = self._conn.execute(sql)
        except Exception as e:
            log.debug(f"DuckDB execute error: {e}")
            raise
        return self

    def fetchone(self) -> tuple | None:
        """Return first row or None."""
        if self._result:
            row = self._result.fetchone()
            return row
        return None

    def fetchall(self) -> list:
        """Return all rows."""
        if self._result:
            return self._result.fetchall()
        return []

    def fetchdf(self) -> object:
        """Return result as pandas DataFrame."""
        if self._result:
            return self._result.fetchdf()
        import pandas as pd
        return pd.DataFrame()

    def executemany(self, sql: str, params_list) -> 'DuckDBConnection':
        """Execute SQL for each set of params."""
        sql = self._adapt_sql(sql)
        if params_list and '%s' in sql:
            sql = sql.replace('%s', '?')
        try:
            self._conn.executemany(sql, params_list)
        except Exception as e:
            log.debug(f"DuckDB executemany error: {e}")
            raise
        return self

    @property
    def description(self) -> list | None:
        """Column descriptions."""
        if self._result:
            return self._result.description
        return None

    def commit(self) -> None:
        """DuckDB auto-commits by default."""
        pass

    def rollback(self) -> None:
        """DuckDB auto-commits — rollback is no-op in autocommit mode."""
        pass

    def close(self) -> None:
        """Close the connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def cursor(self) -> object:
        """Return self (DuckDB connection IS the cursor)."""
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _adapt_sql(sql: str) -> str:
        """Convert PostgreSQL-specific SQL to DuckDB dialect."""
        import re

        # SERIAL → handled by sequences above
        # JSONB → JSON (DuckDB uses JSON natively)
        sql = sql.replace("::jsonb", "::JSON")
        sql = sql.replace("JSONB", "JSON")

        # array_append → list_append (DuckDB native)
        sql = sql.replace("array_append(", "list_append(")

        # ILIKE → ilike (DuckDB supports it natively)
        # ON CONFLICT DO NOTHING → INSERT OR IGNORE
        if "ON CONFLICT" in sql.upper() and "DO NOTHING" in sql.upper():
            sql = re.sub(r'ON CONFLICT\s*\([^)]*\)\s*DO NOTHING', 'ON CONFLICT DO NOTHING', sql, flags=re.IGNORECASE)

        # NOW() works in DuckDB
        # CURRENT_TIMESTAMP works in DuckDB

        # DESCRIBE → DESCRIBE (DuckDB supports it)
        # SHOW TABLES → SHOW TABLES (DuckDB supports it)

        return sql


# ─── Public API ─────────────────────────────────────────────────────────────

def connect(database: str = None, read_only: bool = False) -> DuckDBConnection:
    """
    Create a DuckDB connection.
    `database` param ignored (always uses BEHIVE_DUCKDB_PATH).
    """
    return DuckDBConnection(read_only=read_only)


def get_db_path() -> str:
    """Return the path to the DuckDB file."""
    return _get_db_path()


def is_available() -> bool:
    """Check if DuckDB is installed."""
    try:
        import duckdb
        return True
    except ImportError:
        return False
