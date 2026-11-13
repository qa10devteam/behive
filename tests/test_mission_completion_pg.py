"""Lifecycle SQL on canonical tables in the private Unix-socket PostgreSQL.

Only pipeline subprocess and outbound bridge/event boundaries are controlled;
counts, conditional updates, commits and provenance joins use real PG16.
"""
from pathlib import Path
import re

import pytest

from test_claim_persistence_pg import db  # owned private-cluster fixture
from test_mission_completion_lifecycle import run_server


class RealBoundary:
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor()
        self.committed_status = self.status
        self.fail_commit = False

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.cur.execute(sql.replace('?', '%s'), params)
        return self

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def commit(self):
        self.conn.commit()
        self.committed_status = self.status

    def rollback(self):
        self.conn.rollback()

    def close(self):
        pass  # fixture retains the real connection for read-back and cleanup

    @property
    def status(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM hive_missions WHERE id='m'")
            return cur.fetchone()[0]


@pytest.mark.parametrize('case,expected', [
    ('api', 'done'), ('orphan', 'empty'), ('blob', 'empty'),
    ('garbage', 'empty'), ('cancel', 'cancelled'),
    ('salvage', 'done'), ('error_after_cancel', 'cancelled'),
])
def test_real_pg_completion_provenance_and_publication(db, monkeypatch, case, expected):
    ddl = (Path(__file__).parents[1] / 'src/behive/init-db.sql').read_text()
    match = re.search(r'CREATE TABLE IF NOT EXISTS hive_sources \(.*?\n\);', ddl, re.S)
    assert match is not None
    with db.cursor() as cur:
        cur.execute(match.group())
        cur.execute("UPDATE hive_missions SET status='running',phase='scout' WHERE id='m'")
        if case != 'orphan':
            cur.execute("INSERT INTO hive_content (mission_id,url,raw_text) VALUES ('m','api://doc','Documented 2025 report')")
        for _ in range(3 if case == 'salvage' else 1):
            cur.execute('INSERT INTO hive_claims (mission_id,claim,source_url,is_garbage,quality_score) VALUES (%s,%s,%s,%s,%s)',
                        ('m', '{"claims": []}' if case == 'blob' else 'A documented 2025 research finding.', 'api://doc', case == 'garbage', .8))
        # Since 2026-11-13 done also needs a non-blank report (diag task 1).
        cur.execute("UPDATE hive_missions SET synthesis='# Report' WHERE id='m'")
    db.commit()
    boundary = RealBoundary(db)
    events, bridges = run_server(monkeypatch, boundary,
                                rc=1 if case == 'salvage' else 0,
                                cancel=case in ('cancel', 'error_after_cancel'),
                                spawn_error=case == 'error_after_cancel')
    assert boundary.status == expected
    assert bridges == ([('m', 'done')] if expected == 'done' else [])
    if expected == 'cancelled':
        assert not any(event in ('done', 'error', 'empty') for event, _ in events)
    else:
        assert (expected, expected) in events


@pytest.mark.parametrize('eligible', [False, True])
def test_cleanup_only_recovers_evidence_backed_product(db, monkeypatch, eligible):
    from behive.engine import orchestrator
    ddl = (Path(__file__).parents[1] / 'src/behive/init-db.sql').read_text()
    match = re.search(r'CREATE TABLE IF NOT EXISTS hive_sources \(.*?\n\);', ddl, re.S)
    assert match is not None
    with db.cursor() as cur:
        cur.execute(match.group())
        cur.execute("UPDATE hive_missions SET status='running',created_at=NOW()-INTERVAL '3 hours' WHERE id='m'")
        cur.execute("INSERT INTO hive_content (mission_id,url,raw_text) VALUES ('m','api://doc','Documented report')")
        for _ in range(3):
            cur.execute('INSERT INTO hive_claims (mission_id,claim,source_url,is_garbage) VALUES (%s,%s,%s,false)',
                        ('m', 'A documented 2025 finding.' if eligible else '{"claims":[]}', 'api://doc'))
        # Recovery as done also needs a report since 2026-11-13 (diag task 1).
        cur.execute("UPDATE hive_missions SET synthesis='# Report' WHERE id='m'")
    db.commit()
    boundary = RealBoundary(db)
    monkeypatch.setattr(orchestrator, '_connect_db', lambda: boundary)
    errors = orchestrator.cleanup_stuck_missions()
    assert boundary.status == ('done' if eligible else 'error')
    assert errors == (0 if eligible else 1)


@pytest.mark.parametrize('status', ['running', 'cancelled', 'error'])
def test_legacy_synthesis_only_saves_report_not_terminal_state(db, status):
    from types import SimpleNamespace
    from behive.engine.synth import Queen
    ddl = (Path(__file__).parents[1] / 'src/behive/init-db.sql').read_text()
    match = re.search(r'CREATE TABLE IF NOT EXISTS hive_sources \(.*?\n\);', ddl, re.S)
    assert match is not None
    with db.cursor() as cur:
        cur.execute(match.group())
        cur.execute("UPDATE hive_missions SET status=%s,synthesis='Prior report' WHERE id='m'", (status,))
    db.commit()
    boundary = RealBoundary(db)
    queen = SimpleNamespace(mission_id='m', _db_connect=lambda **kw: boundary,
                            _connect_rw=lambda: boundary)
    Queen._mark_done(queen, report_text='New report')
    assert boundary.status == status
    with db.cursor() as cur:
        cur.execute("SELECT synthesis FROM hive_missions WHERE id='m'")
        assert cur.fetchone()[0] == ('New report' if status == 'running' else 'Prior report')
