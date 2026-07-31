"""
behive.engine.db — PostgreSQL connection layer for HIVE pipeline.

Connection configured via environment variables:
  BEHIVE_DB_URL=postgresql://user:pass@host:5432/dbname
  or individual HIVE_PG_USER, HIVE_PG_PASSWORD, HIVE_PG_DATABASE, HIVE_PG_HOST, HIVE_PG_PORT

Usage:
    from behive.engine.db import get_pg_connection, get_pg_pool

Features vs PostgreSQL:
    - MVCC: unlimited concurrent readers + writers
    - No lock files, no stale PID issues
    - ON CONFLICT upsert natively
    - Connection pooling (psycopg2.pool)
    - Transaction isolation (READ COMMITTED default)
"""

import os
import psycopg2
import psycopg2.extras
import logging


log = logging.getLogger(__name__)
# Module-level constants for compat with legacy hive2_db imports
DB_BACKEND = "postgres"
DB_PATH = os.environ.get("BEHIVE_DB_URL", "")
import psycopg2.pool
import json
import threading
from contextlib import contextmanager
from typing import Optional, Any

# ─── Configuration ──────────────────────────────────────────────────────────
# Priority: BEHIVE_DB_URL (connection string) > individual HIVE_PG_* vars > defaults

_db_url = os.environ.get("BEHIVE_DB_URL", "")

if _db_url and _db_url.startswith("postgresql://"):
    # Parse: postgresql://user:password@host:port/dbname
    import re as _re
    _m = _re.match(r'postgresql://([^:]+):([^@]*)@([^:]+):(\d+)/(.+)', _db_url)
    if _m:
        PG_USER = _m.group(1)
        PG_PASSWORD = _m.group(2)
        PG_HOST = _m.group(3)
        PG_PORT = int(_m.group(4))
        PG_DATABASE = _m.group(5)
    else:
        # Fallback to individual vars
        PG_HOST = os.environ.get("HIVE_PG_HOST", "127.0.0.1")
        PG_PORT = int(os.environ.get("HIVE_PG_PORT", "5432"))
        PG_USER = os.environ.get("HIVE_PG_USER", "behive")
        PG_PASSWORD = os.environ.get("HIVE_PG_PASSWORD", "behive2026")
        PG_DATABASE = os.environ.get("HIVE_PG_DATABASE", "behive")
else:
    PG_HOST = os.environ.get("HIVE_PG_HOST", "127.0.0.1")
    PG_PORT = int(os.environ.get("HIVE_PG_PORT", "5432"))
    PG_USER = os.environ.get("HIVE_PG_USER", "behive")
    PG_PASSWORD = os.environ.get("HIVE_PG_PASSWORD", "behive2026")
    PG_DATABASE = os.environ.get("HIVE_PG_DATABASE", "behive")

DSN = f"host={PG_HOST} port={PG_PORT} user={PG_USER} password={PG_PASSWORD} dbname={PG_DATABASE}"

# ─── Connection Pool (thread-safe) ─────────────────────────────────────────

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def get_pg_pool(minconn: int = 2, maxconn: int = 20) -> psycopg2.pool.ThreadedConnectionPool:
    """Get or create the global connection pool."""
    global _pool
    if _pool is None or _pool.closed:
        with _pool_lock:
            if _pool is None or _pool.closed:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=minconn,
                    maxconn=maxconn,
                    host=PG_HOST,
                    port=PG_PORT,
                    user=PG_USER,
                    password=PG_PASSWORD,
                    database=PG_DATABASE,
                )
    return _pool


def get_pg_connection() -> psycopg2.extensions.connection:
    """Get a single connection (non-pooled). Caller must close it."""
    return psycopg2.connect(DSN)


@contextmanager
def pg_connection() -> object:
    """Context manager for pooled connections. Auto-returns to pool."""
    pool = get_pg_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextmanager
def pg_cursor(commit: bool = True) -> object:
    """Context manager for a cursor with auto-commit/rollback."""
    with pg_connection() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


# ─── PostgreSQL-Compatible Interface ───────────────────────────────────────────
# Drop-in replacement for code that calls con.execute(sql, params).fetchall()

class PgConnection:
    """
    PostgreSQL wrapper around psycopg2.
    Supports: execute(sql, params), fetchone(), fetchall(), close().
    Thread-safe via connection pool.
    """
    
    def __init__(self, read_only: bool = False):
        self._pool = get_pg_pool()
        self._conn = self._pool.getconn()
        self._conn.autocommit = read_only  # read_only = autocommit (no txn overhead)
        self._cursor = None
        self._read_only = read_only
    
    def execute(self, sql: str, params=None) -> 'PgConnection':
        """Execute SQL. Returns self for chaining (.fetchall())."""
        if self._cursor:
            self._cursor.close()
        self._cursor = self._conn.cursor()
        
        # Legacy compat: ? placeholders → %s for psycopg2
        # Convert ? → %s for compatibility
        if params and '?' in sql:
            sql = sql.replace('?', '%s')
        
        # Handle legacy SQL quirks (from DuckDB migration)
        sql = self._adapt_sql(sql)
        
        try:
            self._cursor.execute(sql, params)
            if not self._read_only:
                self._conn.commit()
        except Exception as e:
            log.debug(f"Exception in db.py: {e}")
            self._conn.rollback()
            raise
        return self
    
    def fetchone(self) -> tuple | None:
        """Execute query and return first row or None."""
        if self._cursor:
            return self._cursor.fetchone()
        return None
    
    def executemany(self, sql: str, params_list) -> 'PgConnection':
        """Execute SQL for each set of params. Returns self."""
        if self._cursor:
            self._cursor.close()
        self._cursor = self._conn.cursor()
        
        # Legacy compat: ? placeholders → %s for psycopg2
        if params_list and '?' in sql:
            sql = sql.replace('?', '%s')
        
        sql = self._adapt_sql(sql)
        
        try:
            from psycopg2.extras import execute_batch
            execute_batch(self._cursor, sql, params_list)
            if not self._read_only:
                self._conn.commit()
        except Exception as e:
            log.debug(f"Exception in db.py: {e}")
            self._conn.rollback()
            raise
        return self
    
    def fetchall(self) -> list:
        """Execute query and return all rows."""
        if self._cursor:
            return self._cursor.fetchall()
        return []
    
    def fetchdf(self) -> object:
        """PostgreSQL .fetchdf() compatibility → returns pandas DataFrame."""
        import pandas as pd
        if self._cursor:
            cols = [desc[0] for desc in self._cursor.description]
            rows = self._cursor.fetchall()
            return pd.DataFrame(rows, columns=cols)
        return pd.DataFrame()
    
    @property
    def description(self) -> list | None:
        """Cursor description (column names + types)."""
        if self._cursor:
            return self._cursor.description
        return None
    
    def commit(self) -> None:
        """Commit the current transaction."""
        self._conn.commit()
    
    def rollback(self) -> None:
        """Rollback the current transaction."""
        self._conn.rollback()
    
    def close(self) -> None:
        """Close the database connection."""
        if self._cursor:
            self._cursor.close()
            self._cursor = None
        if self._conn:
            self._pool.putconn(self._conn)
            self._conn = None
    
    def cursor(self) -> object:
        """Return a new cursor (PostgreSQL .cursor() compat)."""
        return self._conn.cursor()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()
    
    @staticmethod
    def _adapt_sql(sql: str) -> str:
        """Convert PostgreSQL-specific SQL to PostgreSQL."""
        stripped = sql.strip()
        upper = stripped.upper()
        
        # DESCRIBE table → information_schema query
        if upper.startswith("DESCRIBE "):
            table_name = stripped.split(None, 1)[1].strip().strip('"').strip("'")
            return f"SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name='{table_name}' ORDER BY ordinal_position"
        
        # SHOW TABLES → pg_tables
        if upper == "SHOW TABLES":
            return "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        
        # list_append → array_append
        sql = sql.replace("list_append(", "array_append(")
        return sql


# ─── Migration Helper ──────────────────────────────────────────────────────

def connect(database: str = None, read_only: bool = False) -> PgConnection:
    """
    Primary PostgreSQL connection factory..
    Ignores `database` param (always connects to hive DB).
    `read_only` sets autocommit mode (no write txn overhead).
    """
    return PgConnection(read_only=read_only)


# ─── Utility Functions ─────────────────────────────────────────────────────

def insert_batch(table: str, rows: list[dict], on_conflict: str = "DO NOTHING") -> None:
    """Batch insert with ON CONFLICT handling."""
    if not rows:
        return 0
    
    cols = list(rows[0].keys())
    col_list = ','.join([f'"{c}"' for c in cols])
    placeholders = ','.join(['%s'] * len(cols))
    
    sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) ON CONFLICT {on_conflict}'
    
    values = [tuple(json.dumps(v) if isinstance(v, (dict, list)) else v for v in [row.get(c) for c in cols]) for row in rows]
    
    with pg_connection() as conn:
        cur = conn.cursor()
        psycopg2.extras.execute_batch(cur, sql, values, page_size=1000)
        conn.commit()
        cur.close()
    
    return len(values)


def upsert_batch(table: str, rows: list[dict], conflict_cols: list[str], update_cols: list[str] = None) -> None:
    """Batch upsert (INSERT ... ON CONFLICT ... DO UPDATE)."""
    if not rows:
        return 0
    
    cols = list(rows[0].keys())
    col_list = ','.join([f'"{c}"' for c in cols])
    placeholders = ','.join(['%s'] * len(cols))
    conflict_list = ','.join([f'"{c}"' for c in conflict_cols])
    
    if update_cols is None:
        update_cols = [c for c in cols if c not in conflict_cols]
    
    update_clause = ','.join([f'"{c}"=EXCLUDED."{c}"' for c in update_cols])
    
    sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) ON CONFLICT ({conflict_list}) DO UPDATE SET {update_clause}'
    
    values = [tuple(json.dumps(v) if isinstance(v, (dict, list)) else v for v in [row.get(c) for c in cols]) for row in rows]
    
    with pg_connection() as conn:
        cur = conn.cursor()
        psycopg2.extras.execute_batch(cur, sql, values, page_size=1000)
        conn.commit()
        cur.close()
    
    return len(values)


# ─── Health Check ──────────────────────────────────────────────────────────

def health_check() -> dict:
    """Check PostgreSQL connectivity and basic stats."""
    try:
        with pg_cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()['version']
            cur.execute("""
                SELECT relname as table_name, n_live_tup as row_count 
                FROM pg_stat_user_tables 
                ORDER BY n_live_tup DESC LIMIT 10
            """)
            tables = cur.fetchall()
        return {
            "status": "healthy",
            "version": version,
            "top_tables": [dict(t) for t in tables],
        }
    except Exception as e:
        log.debug(f"Exception in db.py: {e}")
        return {"status": "error", "error": str(e)}


if __name__ == "__main__":
    result = health_check()
    print(json.dumps(result, indent=2, default=str))
