"""Real cmd_run/error writers with controlled phase and SQL DB boundaries.

No shared DB writes: reuse SQLite boundary, stub planning/phase subprocesses only.
"""
import sys
import types

import pytest

from behive.engine import orchestrator
from test_mission_completion_lifecycle import BoundaryDB


class ErrorBoundaryDB(BoundaryDB):
    def execute(self, sql, params=()):
        # PostgreSQL JSON metadata merge is outside these lifecycle assertions. The
        # no-op must not touch status/phase: SQLite keeps the rightmost duplicate
        # assignment, so 'phase = phase' silently undid every phase write.
        sql = sql.replace("quality_metrics = COALESCE(quality_metrics, '{}'::jsonb) || jsonb_build_object('error_message', %s)", "synthesis = synthesis")
        if len(params) == 5:
            params = [params[0], params[1], params[3], params[4]]
        elif len(params) == 4 and 'synthesis = synthesis' in sql:
            params = [params[0], params[1], params[3]]
        return super().execute(sql, params)


def run_cmd(monkeypatch, db, tmp_path, *, pipeline=None, retries='1'):
    import behive.engine.claims_fallback as fallback
    monkeypatch.setenv('BEHIVE_MISSION_ID', 'm')
    monkeypatch.setenv('BEHIVE_MAX_RETRIES', retries)
    monkeypatch.setattr(orchestrator, '_init_db', lambda _: None)
    monkeypatch.setattr(orchestrator, 'cleanup_stuck_missions', lambda **kwargs: 0)
    monkeypatch.setattr(orchestrator, '_connect_db', lambda **kwargs: db)
    monkeypatch.setattr(orchestrator, '_create_mission', lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, '_emit', lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, '_run_pipeline_phases', pipeline or (lambda *args, **kwargs: None))
    monkeypatch.setattr(orchestrator.time, 'sleep', lambda seconds: None)  # retry pause
    monkeypatch.setattr(fallback, 'salvage_claims_from_content', lambda mid: 0)
    monkeypatch.setitem(sys.modules, 'behive.engine.preprocessor', types.SimpleNamespace(
        preprocess_query=lambda *args, **kwargs: types.SimpleNamespace(ok=True, refined_topic='topic', specificity_score=0.8)))
    monkeypatch.setitem(sys.modules, 'behive.engine.queen_primer', types.SimpleNamespace(
        QueenPrimer=lambda mid: types.SimpleNamespace(load_for_topic=lambda *args, **kwargs: None)))
    monkeypatch.setattr(orchestrator, 'QueenPlanner', lambda *args, **kwargs: types.SimpleNamespace(plan=lambda: []))
    # Keep the real planner output write local to this test's temporary directory.
    import builtins
    real_open = builtins.open
    monkeypatch.setattr(orchestrator, 'open', lambda path, *args, **kwargs: real_open(tmp_path / 'plan.json', *args, **kwargs), raising=False)
    orchestrator.cmd_run('topic')


def test_cmd_run_api_provenance_is_not_empty(monkeypatch, tmp_path):
    db = ErrorBoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    db.claim()
    db.report()  # done needs a report since 2026-11-13 (diag task 1)
    run_cmd(monkeypatch, db, tmp_path)  # Must not exit 1 for an API-only mission.
    assert db.status == 'done'
    assert db.committed_status == 'done'


@pytest.mark.parametrize('kind', ['blob', 'garbage', 'orphan'])
def test_cmd_run_rejects_ineligible_provisional_done(monkeypatch, tmp_path, kind):
    db = ErrorBoundaryDB()
    db.execute("INSERT INTO hive_sources VALUES ('m', 'api://doc')")
    db.claim(text=' {"facts":[]}' if kind == 'blob' else 'A 2025 claim.',
             garbage=kind == 'garbage', url='missing://doc' if kind == 'orphan' else 'api://doc')
    db.execute("UPDATE hive_missions SET status='done', phase='done'")
    db.commit()
    with pytest.raises(SystemExit) as exc:
        run_cmd(monkeypatch, db, tmp_path)
    assert exc.value.code == 1
    assert db.status == 'empty'


def test_empty_error_can_correct_invalid_done(monkeypatch):
    db = ErrorBoundaryDB()
    db.execute("UPDATE hive_missions SET status='done', phase='done'")
    db.commit()
    monkeypatch.setattr(orchestrator, '_connect_db', lambda **kwargs: db)
    orchestrator._mark_mission_error('m', 'empty_mission sources=0 claims=0')
    assert db.status == 'empty'


@pytest.mark.parametrize('status', ['running', 'done', 'cancelled'])
def test_error_writer_preserves_proven_done_or_cancellation(monkeypatch, status):
    db = ErrorBoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    db.claim()
    db.report()  # a proven done = report + useful claim (contract since 2026-11-13)
    db.execute('UPDATE hive_missions SET status=?, phase=?', (status, status))
    db.commit()
    monkeypatch.setattr(orchestrator, '_connect_db', lambda **kwargs: db)
    orchestrator._mark_mission_error('m', 'empty_mission sources=0 claims=0')
    assert db.status == status
    orchestrator._mark_mission_error('m', 'worker timeout')
    assert db.status == ('error' if status == 'running' else status)



def test_failed_evidence_read_is_error_not_empty(monkeypatch, tmp_path):
    db = ErrorBoundaryDB()
    real_execute = db.execute

    def execute(sql, params=()):
        if sql.startswith('SELECT COUNT(*) FROM hive_claims'):
            raise RuntimeError('controlled evidence read failure')
        return real_execute(sql, params)

    monkeypatch.setattr(db, 'execute', execute)
    with pytest.raises(SystemExit) as exc:
        run_cmd(monkeypatch, db, tmp_path)
    assert exc.value.code == 1
    assert db.status == 'error'


def test_success_finalizer_preserves_cancellation(monkeypatch, tmp_path):
    db = ErrorBoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    db.claim()
    db.execute("UPDATE hive_missions SET status='cancelled', phase='cancelled'")
    db.commit()
    run_cmd(monkeypatch, db, tmp_path)
    assert db.status == 'cancelled'
