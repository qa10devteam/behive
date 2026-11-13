"""Real completion functions; controlled in-memory DB/subprocess boundaries.

No PostgreSQL, network, LLM, bridge, or pipeline subprocess is contacted.
SQLite executes the production completion predicate/conditional UPDATE SQL.
"""
import asyncio
import sqlite3

import pytest

from behive import server
from behive.engine import orchestrator


class BoundaryDB:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.create_function('chr', 1, chr)
        # Columns used by the completion predicates (diag 2026-11-12 contract:
        # done needs a non-blank synthesis and a claim other than regex_fallback).
        self.db.executescript('''
            CREATE TABLE hive_missions (id TEXT PRIMARY KEY, status TEXT, phase TEXT, synthesis TEXT);
            INSERT INTO hive_missions VALUES ('m', 'running', 'scout', NULL);
            CREATE TABLE hive_sources (mission_id TEXT, url TEXT);
            CREATE TABLE hive_content (mission_id TEXT, url TEXT);
            CREATE TABLE hive_claims (mission_id TEXT, claim TEXT, source_url TEXT,
                is_garbage BOOLEAN, quality_score REAL, claim_type TEXT);
        ''')
        self.db.commit()
        self.committed_status = 'running'
        self.row = None
        self.fail_commit = False

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        # PostgreSQL placeholder/whitespace syntax only; real SQL evaluation.
        sql = sql.replace('%s', '?')
        self.row = self.db.execute(sql, params)
        return self

    def fetchone(self):
        return self.row.fetchone()

    def commit(self):
        if self.fail_commit:
            self.db.rollback()
            raise RuntimeError('controlled commit failure')
        self.db.commit()
        self.committed_status = self.status

    def rollback(self):
        self.db.rollback()

    def close(self):
        pass  # Boundary remains inspectable for assertions.

    @property
    def status(self):
        return self.db.execute("SELECT status FROM hive_missions WHERE id='m'").fetchone()[0]

    @property
    def phase(self):
        return self.db.execute("SELECT phase FROM hive_missions WHERE id='m'").fetchone()[0]

    def claim(self, text='A documented claim about the 2025 research result.', url='api://doc', garbage=False,
              claim_type='fact'):
        self.db.execute('INSERT INTO hive_claims VALUES (?, ?, ?, ?, ?, ?)', ('m', text, url, garbage, .8, claim_type))
        self.db.commit()

    def report(self, text='# Report\n\nA documented 2025 research finding [1].'):
        self.db.execute("UPDATE hive_missions SET synthesis=? WHERE id='m'", (text,))
        self.db.commit()


class EmptyOutput:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def run_server(monkeypatch, db, *, rc=0, cancel=False, spawn_error=False, worker_done=False, commit_failure=False):
    events, bridges = [], []

    async def spawn(*args, **kwargs):
        if cancel:
            db.execute("UPDATE hive_missions SET status='cancelled', phase='cancelled' WHERE id='m'")
            db.commit()
        if worker_done:
            db.execute("UPDATE hive_missions SET status='done', phase='done' WHERE id='m'")
            db.commit()
        if spawn_error:
            raise RuntimeError('controlled spawn failure')
        if commit_failure:
            db.fail_commit = True

        class Proc:
            stdout = EmptyOutput()
            returncode = rc

            async def wait(self):
                return rc
        return Proc()

    import behive.engine.claims_fallback as fallback
    monkeypatch.setattr(fallback, 'salvage_claims_from_content', lambda mid: 0)
    monkeypatch.setattr(server, 'get_db', lambda: db)
    monkeypatch.setattr(server.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(server, '_emit_event', lambda mid, event, data: events.append((event, db.committed_status)))
    monkeypatch.setattr(server, '_bridge_done_mission', lambda mid: bridges.append((mid, db.committed_status)))
    asyncio.run(server._run_pipeline_inner('m', 'topic', 1))
    return events, bridges


def test_api_content_claim_completes_without_scout_rows(monkeypatch):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    db.claim()
    db.report()  # done needs a report since 2026-11-13 (diag task 1)
    events, bridges = run_server(monkeypatch, db)
    assert db.status == 'done'
    assert ('done', 'done') in events  # Commit precedes publication.
    assert bridges == [('m', 'done')]


@pytest.mark.parametrize('rc', [0, 1])
@pytest.mark.parametrize('kind', ['orphan', 'blob', 'blob_whitespace', 'garbage', 'blank', 'blank_whitespace', 'other_mission'])
def test_ineligible_claims_never_complete(monkeypatch, kind, rc):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_sources VALUES ('m', 'web://unrelated')")
    if kind != 'orphan':
        db.execute('INSERT INTO hive_content VALUES (?, ?)', ('other' if kind == 'other_mission' else 'm', 'api://doc'))
    for _ in range(3):
        db.claim(text={'blob': ' {"facts":[]}', 'blob_whitespace': '\n\t {"facts":[]}', 'blank': '  ', 'blank_whitespace': '\n\t '}.get(kind, 'A factual 2025 claim.'), garbage=kind == 'garbage')
    events, bridges = run_server(monkeypatch, db, rc=rc)
    assert db.status == ('empty' if rc == 0 else 'error')
    assert not bridges
    assert not any(event == 'done' for event, status in events)


def test_cancelled_finalization_publishes_nothing(monkeypatch):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_sources VALUES ('m', 'api://doc')")
    db.claim()
    events, bridges = run_server(monkeypatch, db, cancel=True)
    assert db.status == 'cancelled'
    assert not bridges
    assert not any(event in ('done', 'empty', 'error') for event, status in events)


def test_exception_preserves_cancellation(monkeypatch):
    db = BoundaryDB()
    events, bridges = run_server(monkeypatch, db, cancel=True, spawn_error=True)
    assert db.status == 'cancelled'
    assert not any(event == 'error' for event, status in events)


def test_salvaged_nonzero_pipeline_bridges_committed_product(monkeypatch):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    for _ in range(3):
        db.claim()
    db.report()  # done needs a report since 2026-11-13 (diag task 1)
    events, bridges = run_server(monkeypatch, db, rc=1)
    assert db.status == 'done'
    assert ('done', 'done') in events
    assert bridges == [('m', 'done')]


def test_replayed_done_does_not_emit_or_bridge(monkeypatch):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    db.claim()
    db.execute("UPDATE hive_missions SET status='done', phase='done'")
    db.commit()
    events, bridges = run_server(monkeypatch, db)
    assert db.status == 'done'
    assert not bridges
    assert not any(event == 'done' for event, status in events)



def test_worker_provisional_done_is_finalized_and_bridged(monkeypatch):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    db.claim()
    db.report()  # done needs a report since 2026-11-13 (diag task 1)
    events, bridges = run_server(monkeypatch, db, worker_done=True)
    assert ('done', 'done') in events
    assert bridges == [('m', 'done')]


def test_failed_finalization_commit_never_publishes_done(monkeypatch):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    db.claim()
    db.report()  # a complete product: only the commit failure may block done
    events, bridges = run_server(monkeypatch, db, commit_failure=True)
    assert not bridges
    assert not any(event == 'done' for event, status in events)
    assert db.status == 'running'


def test_worker_failure_marks_error_after_initial_connection_closed(monkeypatch):
    db = BoundaryDB()
    real_rollback = db.rollback
    closed = False

    def close():
        nonlocal closed
        closed = True

    def rollback():
        if closed:
            raise RuntimeError('rollback on closed connection')
        real_rollback()

    def connect():
        nonlocal closed
        closed = False
        return db

    monkeypatch.setattr(db, 'close', close)
    monkeypatch.setattr(db, 'rollback', rollback)
    # run_server installs get_db; hook it via cursor so reopening clears closed.
    real_cursor = db.cursor
    monkeypatch.setattr(db, 'cursor', lambda: (connect(), real_cursor())[1])
    events, bridges = run_server(monkeypatch, db, spawn_error=True)
    assert db.status == 'error'
    assert ('error', 'error') in events


def test_phase_progress_cannot_overwrite_cancelled_phase(monkeypatch):
    db = BoundaryDB()
    db.execute("UPDATE hive_missions SET status='cancelled', phase='cancelled'")
    db.commit()
    monkeypatch.setattr(server, 'get_db', lambda: db)
    server._update_phase('m', 'synth')
    assert db.db.execute("SELECT phase FROM hive_missions").fetchone()[0] == 'cancelled'



def test_completion_rechecks_evidence_in_terminal_update(monkeypatch):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    db.claim()
    db.report()  # a complete product: only the concurrent deletion may block done
    real_execute = db.execute

    def execute(sql, params=()):
        if sql.startswith('UPDATE hive_missions SET status = %s') and params[0] == 'done':
            # Controlled concurrent evidence deletion after aggregate read.
            db.db.execute('DELETE FROM hive_claims')
            db.db.commit()
        return real_execute(sql, params)

    monkeypatch.setattr(db, 'execute', execute)
    events, bridges = run_server(monkeypatch, db)
    assert db.status != 'done'
    assert not bridges
    assert not any(event == 'done' for event, status in events)
