"""hive_sources_id_seq is never dropped by a scout (diag 2026-11-12, task 4).

Old _init_sequence ran DROP SEQUENCE + CREATE SEQUENCE ... START max(id)+500 for every
mission. Production: the runtime role does not own the sequence, so DROP logged
'must be owner' (and CREATE 'already exists') each mission. Fresh install: a second
concurrent scout could drop the sequence under the first one's inserts.

Private socket PostgreSQL (CLAIM_TEST_PG_DSN) with live-shaped DDL (hive_sources.id has
no default; the sequence is a separate object, as in production). Real SwarmScout
constructors and _save_source inserts, real roles, real server log.
"""
import os
import threading
import uuid

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import make_dsn, parse_dsn

from test_repair1022_support import live_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.pg_harness


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


def _sequence_state(conn):
    """(oid, last_value, is_called) or None. Ends its transaction: a read must not keep
    a lock on the sequence (the old DROP SEQUENCE would wait on it forever)."""
    conn.rollback()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('hive_sources_id_seq')::oid")
            oid = cur.fetchone()[0]
            if oid is None:
                return None
            cur.execute("SELECT last_value, is_called FROM hive_sources_id_seq")
            return (oid, *cur.fetchone())
    finally:
        conn.rollback()


def _scout(mission="diag1112-seq"):
    from behive.engine.scout import SwarmScout
    return SwarmScout(mission, "Solar capacity in Poland")


def _save(scout, n, prefix):
    saved = 0
    for i in range(n):
        saved += scout._save_source(f"https://example.test/{prefix}/{i}", f"Solar capacity {i}",
                                    "Solar capacity report", "web", "test")
    return saved


@pytest.fixture
def app_role(live_db, monkeypatch):
    """A LOGIN role like production hive_app: DML + sequence USAGE/SELECT/UPDATE, no ownership."""
    from behive.engine import db as engine_db

    role = "diag_seq_" + uuid.uuid4().hex[:12]
    admin = psycopg2.connect(os.environ["CLAIM_TEST_PG_DSN"])
    admin.autocommit = True
    schema = sql.Identifier(live_db.name)
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, sql.Identifier(role)))
        cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(
            schema, sql.Identifier(role)))
        cur.execute(sql.SQL("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
            schema, sql.Identifier(role)))
    parsed = parse_dsn(live_db.dsn)
    parsed["user"] = role
    monkeypatch.setattr(engine_db, "DSN", make_dsn(**parsed))
    monkeypatch.setattr(engine_db, "_pool", None)
    try:
        yield role
    finally:
        if engine_db._pool is not None:
            engine_db._pool.closeall()
            engine_db._pool = None
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        admin.close()


def test_non_owner_scout_sends_no_failing_ddl_and_keeps_the_sequence(live_db, app_role, server_log):
    before = _sequence_state(live_db.conn)
    scout = _scout()
    assert _save(scout, 5, "a") == 5
    after = _sequence_state(live_db.conn)
    assert after[0] == before[0]  # same object: never dropped/recreated
    log = server_log()
    assert "must be owner" not in log and "already exists" not in log and "ERROR" not in log, log[-2000:]


def test_sequence_behind_max_id_is_advanced_never_rewound(live_db):
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute("INSERT INTO hive_sources (id, mission_id, url) VALUES (10000, 'old', 'https://example.test/old')")
    oid_before = _sequence_state(live_db.conn)[0]
    scout = _scout()
    assert _save(scout, 1, "b") == 1
    oid, last_value, _ = _sequence_state(live_db.conn)
    assert oid == oid_before
    assert last_value == 10001  # advanced to max(id), next insert got max+1
    with live_db.conn, live_db.conn.cursor() as cur:  # far ahead: a new scout must not rewind it
        cur.execute("SELECT setval('hive_sources_id_seq', 50000, true)")
    _scout()
    assert _sequence_state(live_db.conn)[1] == 50000


def test_missing_sequence_is_created_once(live_db):
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute("DROP SEQUENCE hive_sources_id_seq")
    scout = _scout()
    assert _sequence_state(live_db.conn) is not None
    assert _save(scout, 3, "c") == 3


@pytest.mark.parametrize("fresh_install", [False, True])
def test_two_parallel_scouts_never_lose_inserts(live_db, server_log, fresh_install):
    if fresh_install:
        with live_db.conn, live_db.conn.cursor() as cur:
            cur.execute("DROP SEQUENCE hive_sources_id_seq")
            cur.execute("INSERT INTO hive_sources (id, mission_id, url) VALUES (700, 'old', 'https://example.test/old')")
    barrier = threading.Barrier(2)
    results, errors = {}, []
    a_started = threading.Event()

    def first():
        try:
            scout = _scout("diag1112-a")
            a_started.set()
            barrier.wait(10)
            results["a"] = _save(scout, 150, "a")
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))

    def second():
        try:
            a_started.wait(10)
            barrier.wait(10)
            scout = _scout("diag1112-b")  # initialises while the first one inserts
            results["b"] = _save(scout, 150, "b")
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    assert results == {"a": 150, "b": 150}
    live_db.conn.rollback()
    with live_db.conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT id) FROM hive_sources WHERE mission_id LIKE 'diag1112-%'")
        assert cur.fetchone() == (300, 300)
        cur.execute("SELECT MIN(id) FROM hive_sources WHERE mission_id LIKE 'diag1112-%'")
        assert cur.fetchone()[0] > (700 if fresh_install else 0)
    live_db.conn.rollback()
    log = server_log()
    assert "does not exist" not in log and "must be owner" not in log, log[-2000:]
