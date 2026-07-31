"""Database helpers for HIVE pipeline.

Extracted from orchestrator.py to reduce god-function size.
Provides: _ensure_pg, _connect_db, _init_db, _get_mission, _create_mission, new_mission_id.
"""

import logging
import hashlib
import time
from datetime import datetime

log = logging.getLogger(__name__)

# Module-level state
_pg_available: bool | None = None
_hive_db = None


def _ensure_pg() -> bool:
    """Lazy PostgreSQL availability check."""
    global _pg_available, _hive_db
    if _pg_available is not None:
        return _pg_available
    try:
        import hive2_db
        _hive_db = hive2_db
        _pg_available = (hive2_db.DB_BACKEND == "postgres")
    except Exception:
        try:
            from behive.engine import db as _db_mod
            _hive_db = _db_mod
            _pg_available = (_db_mod.DB_BACKEND == "postgres")
        except Exception:
            _pg_available = False
    return _pg_available


def _connect_db(read_only: bool = False):
    """Connect to the active database backend (PostgreSQL only)."""
    _ensure_pg()
    if _pg_available and _hive_db:
        return _hive_db.connect(read_only=read_only)
    raise RuntimeError(
        "PostgreSQL not available. Ensure PostgreSQL is running and configured.\n"
        "Set: HIVE_PG_HOST, HIVE_PG_PORT, HIVE_PG_USER, HIVE_PG_PASSWORD, HIVE_PG_DATABASE\n"
        "Or: BEHIVE_DB_URL=postgresql://user:***@host:5432/dbname\n"
        "For Docker: docker compose up -d"
    )


def _get_pg():
    """Legacy stub — DuckDB removed, use behive.engine.db.connect() instead."""
    raise RuntimeError("DuckDB removed from HIVE. Use behive.engine.db.connect() for PostgreSQL.")


def _strip_sql_comments(sql: str) -> str:
    """Strip leading/inline -- comments from a SQL statement block."""
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _init_db(con=None):
    """Ensure HIVE schema exists. PostgreSQL schema already created during migration."""
    _ensure_pg()
    if _pg_available:
        return
    raise RuntimeError(
        "PostgreSQL required but not available.\n"
        "Quick start: docker compose up -d\n"
        "Or set BEHIVE_DB_URL=postgresql://user:***@host:5432/dbname"
    )


def _get_mission(mission_id: str) -> dict | None:
    """Get mission from DB by ID."""
    con = _connect_db(read_only=True)
    try:
        rows = con.execute(
            "SELECT * FROM hive_missions WHERE id = ?", [mission_id]
        ).fetchall()
        if not rows:
            return None
        cols = [desc[0] for desc in con.description]
        return dict(zip(cols, rows[0]))
    finally:
        con.close()


def _create_mission(mission_id: str, topic: str, think_mode: bool = False):
    """Create a new mission in the database."""
    con = _connect_db()
    try:
        con.execute(
            "INSERT INTO hive_missions (id, topic, status, think_mode, created_at) "
            "VALUES (?, ?, 'scout', ?, CURRENT_TIMESTAMP)",
            [mission_id, topic, think_mode]
        )
        con.commit()
    finally:
        con.close()


def _get_unresolved_gaps(mission_id: str) -> list:
    """Get unresolved knowledge gaps for a mission."""
    con = _connect_db(read_only=True)
    try:
        rows = con.execute(
            "SELECT gap_text FROM hive_gaps WHERE mission_id = ? AND resolved = FALSE",
            [mission_id]
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def new_mission_id(topic: str) -> str:
    """Generate a deterministic mission ID from topic + timestamp."""
    ts = int(time.time())
    slug = hashlib.md5(topic.encode()).hexdigest()[:12]
    return f"hive_{ts}_{slug}"
