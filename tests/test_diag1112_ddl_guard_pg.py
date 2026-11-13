"""No DDL from non-owner roles for columns that already exist (diag 2026-11-12, task 5).

Production runs as hive_app, which does not own hive_claims. PostgreSQL checks table
ownership before IF NOT EXISTS, so `ALTER TABLE hive_claims ADD COLUMN IF NOT EXISTS
verified/evidence_flags` logged "must be owner of table hive_claims" on every pass
(27 ERROR lines on 10.11). The writers must consult information_schema first.

Private socket-only PostgreSQL (run_suite.sh pg / CLAIM_TEST_PG_DSN), live-shaped DDL
in a UUID schema, a real non-owner LOGIN role. Assertions use both the SQL the code
sent and the server log of the private cluster.
"""
import os
import uuid

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import make_dsn, parse_dsn

from test_repair1022_support import live_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.pg_harness

MISSION = "diag1112-ddl-guard"


class Recording:
    """Delegates to a real PgConnection and records every SQL statement sent."""

    def __init__(self, inner):
        self.inner = inner
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(" ".join(statement.split()))
        self.inner.execute(statement, params)
        return self

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def ddl(self):
        return [s for s in self.statements if s.upper().startswith(("ALTER ", "CREATE ", "DROP "))]


@pytest.fixture
def server_log(live_db):
    with live_db.conn.cursor() as cur:
        cur.execute("SELECT current_setting('data_directory')")
        data_dir = cur.fetchone()[0]
    path = os.path.join(os.path.dirname(data_dir), "postgres.log")
    if not os.access(path, os.R_OK):
        pytest.skip("private cluster log not readable")
    start = os.path.getsize(path)

    def read_new():
        with open(path, encoding="utf-8", errors="replace") as handle:
            handle.seek(start)
            return handle.read()
    return read_new


@pytest.fixture
def app_role(live_db, monkeypatch):
    """A LOGIN role with DML grants but no ownership, like production hive_app."""
    from behive.engine import db as engine_db

    role = "diag_app_" + uuid.uuid4().hex[:12]
    admin_dsn = os.environ["CLAIM_TEST_PG_DSN"]
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(live_db.name), sql.Identifier(role)))
        cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(
            sql.Identifier(live_db.name), sql.Identifier(role)))
        cur.execute(sql.SQL("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
            sql.Identifier(live_db.name), sql.Identifier(role)))
    parsed = parse_dsn(live_db.dsn)
    parsed["user"] = role
    app_dsn = make_dsn(**parsed)
    monkeypatch.setattr(engine_db, "DSN", app_dsn)
    monkeypatch.setattr(engine_db, "_pool", None)
    try:
        yield app_dsn
    finally:
        if engine_db._pool is not None:
            engine_db._pool.closeall()
            engine_db._pool = None
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        admin.close()


def _app_connection():
    from behive.engine.db import PgConnection
    return Recording(PgConnection(read_only=False))


def test_positive_control_old_alter_by_non_owner_is_logged_as_error(app_role, server_log):
    con = _app_connection()
    try:
        with pytest.raises(psycopg2.Error):
            con.execute("ALTER TABLE hive_claims ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT FALSE")
    finally:
        con.close()
    assert "must be owner of table hive_claims" in server_log()


def test_falsifier_corroboration_sends_no_ddl_for_existing_column(app_role, server_log):
    from behive.engine.falsifier import cross_source_corroboration

    con = _app_connection()
    try:
        cross_source_corroboration(con, MISSION)
    finally:
        con.close()
    assert con.ddl() == []
    assert any("information_schema.columns" in s for s in con.statements)
    assert "must be owner" not in server_log()


def test_cross_validation_drone_sends_no_ddl_for_existing_column(app_role, server_log):
    from behive.engine.process import ClaimCrossValidationDrone

    con = _app_connection()
    try:
        ClaimCrossValidationDrone().run(con, MISSION)
    finally:
        con.close()
    assert con.ddl() == []
    assert "must be owner" not in server_log()


def test_missing_column_is_still_added_for_owner(live_db):
    from behive.engine.db import PgConnection
    from behive.engine.db_helpers import column_exists, ensure_column

    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute("ALTER TABLE hive_claims DROP COLUMN evidence_flags")
    con = Recording(PgConnection(read_only=False))
    try:
        assert column_exists(con, "hive_claims", "evidence_flags") is False
        assert ensure_column(con, "hive_claims", "evidence_flags", "VARCHAR") is True
        assert column_exists(con, "hive_claims", "evidence_flags") is True
        assert ensure_column(con, "hive_claims", "evidence_flags", "VARCHAR") is False
    finally:
        con.close()
    assert [s for s in con.ddl()] == ["ALTER TABLE hive_claims ADD COLUMN IF NOT EXISTS evidence_flags VARCHAR"]


def test_missing_column_for_non_owner_fails_inside_callers_guard(live_db, app_role):
    """No privilege to add a really missing column: the writer keeps running."""
    from behive.engine.process import ClaimCrossValidationDrone

    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute("ALTER TABLE hive_claims DROP COLUMN evidence_flags")
    con = _app_connection()
    try:
        result = ClaimCrossValidationDrone().run(con, MISSION)
    finally:
        con.close()
    assert con.ddl() == ["ALTER TABLE hive_claims ADD COLUMN IF NOT EXISTS evidence_flags VARCHAR"]
    assert result == 0


def test_column_check_is_schema_scoped(live_db):
    """A same-named table in another schema must not satisfy the check."""
    from behive.engine.db import PgConnection
    from behive.engine.db_helpers import column_exists

    other = "diag_other_" + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(os.environ["CLAIM_TEST_PG_DSN"])
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(other)))
            cur.execute(sql.SQL("CREATE TABLE {}.hive_claims (id int, only_elsewhere text)").format(sql.Identifier(other)))
        con = PgConnection(read_only=True)
        try:
            assert column_exists(con, "hive_claims", "only_elsewhere") is False
            assert column_exists(con, "hive_claims", "verified") is True
        finally:
            con.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(other)))
        admin.close()
