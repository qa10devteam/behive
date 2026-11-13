"""Conditional lifecycle UPDATE runs in SQLite; JSONB assignment is adapted only."""
import pytest
from behive.engine import orchestrator as orch
from test_pipeline_failures import DB


class MarkDB(DB):
    def execute(self, sql, params=()):
        if "jsonb_build_object('error_message'" in sql:
            self.error_message = params[2]
            sql = sql.replace("quality_metrics = COALESCE(quality_metrics, '{}'::jsonb) || jsonb_build_object('error_message', %s)", 'quality_metrics = %s')
        return super().execute(sql,params)


@pytest.fixture
def db(monkeypatch):
    db = MarkDB()
    db.db.execute('ALTER TABLE hive_missions ADD COLUMN quality_metrics TEXT')
    monkeypatch.setattr(orch,'_connect_db',lambda: db)
    return db


@pytest.mark.parametrize('sources,phase', [(0,'empty_scout'),(2,'empty_claims')])
def test_mark_empty_uses_actual_missing_stage(db,sources,phase):
    assert orch._mark_mission_error('m',f'empty_mission sources={sources} claims=0')
    assert db.state == ('empty',phase)


def test_mark_error_never_persists_provider_body(db):
    assert orch._mark_mission_error('m','Authorization Bearer secret; request_body=private')
    assert db.error_message == 'Mission execution failed'


@pytest.mark.parametrize('state', ['cancelled','done'])
def test_mark_error_preserves_terminal_product_or_cancellation(db,state):
    if state == 'done':  # a done product = report + useful claim (since 2026-11-13)
        db.claim()
        db.execute("UPDATE hive_missions SET synthesis='report'")
    db.execute('UPDATE hive_missions SET status=?,phase=?',(state,state))
    db.commit()
    assert not orch._mark_mission_error('m','synthesis failed')
    assert db.state == (state,state)


def test_mark_empty_cannot_overwrite_done_with_evidence(db):
    db.claim()
    db.execute("UPDATE hive_missions SET status='done',phase='done'")
    db.commit()
    assert not orch._mark_mission_error('m','empty_mission sources=0 claims=0')
    assert db.state == ('done','done')


def test_mark_failure_corrects_provisional_done_without_product(db):
    db.execute("UPDATE hive_missions SET status='done',phase='done'")
    db.commit()
    assert orch._mark_mission_error('m','synthesis failed')
    assert db.state == ('error','error')
