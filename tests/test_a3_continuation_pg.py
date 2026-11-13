"""PROCESS continuation with real config SQL/A1/A3; external phases controlled.

Not a live mission or a real process subprocess. Synthesis ownership is untouched.
"""
import builtins
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from behive.engine import llm, orchestrator
from test_a3_incremental_pg import MISSION, TOPIC, documents, readback, seed
from test_a3_round_progress_pg import provider, run_round
from test_repair1022_support import live_db


class SynthesisReached(BaseException):
    pass


@pytest.fixture
def pipeline_edges(live_db, monkeypatch, tmp_path):
    monkeypatch.delenv('BEHIVE_PHASE_DEADLINE', raising=False)
    monkeypatch.delenv('BEHIVE_A3_CONTINUATION_PASSES', raising=False)
    monkeypatch.delenv('HIVE_BM_SEEDS', raising=False)
    monkeypatch.setattr(orchestrator, '_emit', lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator.subprocess, 'run', lambda *a, **kw:
        SimpleNamespace(returncode=0, stdout='[]', stderr=''))
    # No unowned /tmp files: redirect only this test's orchestrator script write.
    monkeypatch.setattr(orchestrator, 'open', lambda path, *a, **kw:
        builtins.open(tmp_path / str(path).rsplit('/', 1)[-1], *a, **kw), raising=False)
    import behive.engine.drones as drones
    import behive.engine.prescout as prescout
    monkeypatch.setattr(drones, 'extract_domains_from_sources', lambda mid: [])
    monkeypatch.setattr(prescout, 'run_prescout_pipeline', AsyncMock(side_effect=RuntimeError('offline boundary')))
    def synth(*a, **kw):
        raise SynthesisReached
    monkeypatch.setattr(orchestrator, '_run_synthesis', synth)
    monkeypatch.setattr(orchestrator, '_llm_complete', lambda *a, **kw: pytest.fail('Unexpected model call'))
    return live_db


def scout_row(db):
    with db.conn, db.conn.cursor() as cur:
        cur.execute('INSERT INTO hive_sources (id,mission_id,url,domain) VALUES (1,%s,%s,%s)',
                    (MISSION, 'https://source0.example.test/report', 'source0.example.test'))


def run_pipeline():
    timings = {}
    with pytest.raises(SynthesisReached):
        orchestrator._run_pipeline_phases(MISSION, TOPIC, [], timings, time.time(),
            False, True, 1, TOPIC, None, 'unused-plan.json')
    return timings


def write_state(db, *, pending, attempted):
    with db.conn, db.conn.cursor() as cur:
        cur.execute('SELECT config FROM hive_missions WHERE id=%s', (MISSION,))
        config = cur.fetchone()[0]
        config['source_extraction'] = {'version': 1, 'attempted_urls': attempted, 'pending': pending}
        cur.execute('UPDATE hive_missions SET config=%s WHERE id=%s', (json.dumps(config), MISSION))


@pytest.mark.parametrize('column_type', ['json', 'jsonb'])
def test_orchestrator_continues_only_pending_pages_without_gate_model(pipeline_edges, monkeypatch, column_type):
    db = pipeline_edges
    docs = documents(63)
    seed(db, docs, column_type)
    scout_row(db)
    calls = provider(monkeypatch, docs, {d['url']: 2 for d in docs})
    phases, per_pass = [], {}
    def phase(script, args, name, **kw):
        phases.append((script, list(args), name))
        if name in ('process', 'process-continue'):
            before = {key: len(value) for key, value in calls.items()}
            run_round(monkeypatch)
            per_pass[name] = {key: value[before[key]:] for key, value in calls.items()}
        return 1.0
    monkeypatch.setattr(orchestrator, 'run_phase', phase)
    timings = run_pipeline()
    assert [p[2] for p in phases] == ['harvest', 'process', 'process-continue', 'falsifier']
    assert all(p[:2] == ('hive2_process.py', [MISSION, '--deep']) for p in phases if p[2].startswith('process'))
    assert per_pass['process-continue']['gate'] == []
    assert len(per_pass['process']['a3']) == 60
    assert len(per_pass['process-continue']['a3']) == 3
    assert not set(per_pass['process']['a3']) & set(per_pass['process-continue']['a3'])
    assert len(readback(db)[0]) == 63
    assert readback(db)[1]['source_extraction']['pending'] == 0
    assert timings['process'] == 2.0


@pytest.mark.parametrize('pending,passes,expected', [(0, '5', 0), (4, '0', 0), (4, '5', 1), (4, None, 1)])
def test_pending_zero_disabled_or_no_progress_stops_extra_passes(pipeline_edges, monkeypatch, pending, passes, expected):
    db = pipeline_edges
    docs = documents(5)
    seed(db, docs)
    scout_row(db)
    if passes is not None:
        monkeypatch.setenv('BEHIVE_A3_CONTINUATION_PASSES', passes)
    phases = []
    def phase(script, args, name, **kw):
        phases.append(name)
        if name == 'process':
            attempted = [d['url'] for d in docs] if pending == 0 else [docs[0]['url']]
            write_state(db, pending=pending, attempted=attempted)
        return 1.0
    monkeypatch.setattr(orchestrator, 'run_phase', phase)
    run_pipeline()
    assert phases.count('process-continue') == expected
    assert phases[-1] == 'falsifier'


def test_configured_continuations_stop_at_zero(pipeline_edges, monkeypatch):
    db = pipeline_edges
    docs = documents(4)
    seed(db, docs)
    scout_row(db)
    monkeypatch.setenv('BEHIVE_A3_CONTINUATION_PASSES', '8')
    phases, attempted = [], []
    def phase(script, args, name, **kw):
        phases.append(name)
        if name in ('process', 'process-continue'):
            attempted.append(docs[len(attempted)]['url'])
            write_state(db, pending=4 - len(attempted), attempted=attempted)
        return 1.0
    monkeypatch.setattr(orchestrator, 'run_phase', phase)
    run_pipeline()
    assert phases.count('process-continue') == 3
    assert readback(db)[1]['source_extraction']['pending'] == 0


def test_failed_continuation_preserves_claims_and_reaches_falsifier_synth(pipeline_edges, monkeypatch):
    db = pipeline_edges
    docs = documents(61)
    seed(db, docs)
    scout_row(db)
    provider(monkeypatch, docs, {d['url']: 2 for d in docs})
    monkeypatch.setenv('BEHIVE_A3_CONTINUATION_PASSES', '5')
    phases = []
    def phase(script, args, name, **kw):
        phases.append(name)
        if name == 'process':
            run_round(monkeypatch)
        if name == 'process-continue':
            raise RuntimeError('upstream_rate_limit must not be retried')
        return 1.0
    monkeypatch.setattr(orchestrator, 'run_phase', phase)
    run_pipeline()
    assert phases == ['harvest', 'process', 'process-continue', 'falsifier']
    assert len(readback(db)[0]) == 60
    assert readback(db)[1]['source_extraction']['pending'] == 1


def test_failed_initial_process_is_not_implicitly_retried(pipeline_edges, monkeypatch):
    db = pipeline_edges
    docs = documents(4)
    seed(db, docs)
    scout_row(db)
    write_state(db, pending=4, attempted=[])
    phases = []
    def phase(script, args, name, **kw):
        phases.append(name)
        if name == 'process':
            raise RuntimeError('upstream_rate_limit')
        return 1.0
    monkeypatch.setattr(orchestrator, 'run_phase', phase)
    # Since 2026-11-13 (diag task 1) a failed process phase ends the mission as
    # error/process_error: no continuation pass and no later phases either.
    with pytest.raises(orchestrator.PhaseFailure) as failure:
        run_pipeline()
    assert failure.value.phase == 'process_error'
    assert phases == ['harvest', 'process']


@pytest.mark.parametrize('state', [None, {}, {'version': 1, 'attempted_urls': []},
    {'version': 1, 'attempted_urls': [], 'pending': True},
    {'version': 2, 'attempted_urls': [], 'pending': 4},
    {'version': 1, 'attempted_urls': 'bad', 'pending': 4}])
def test_missing_or_malformed_progress_cannot_authorize_continuation(pipeline_edges, monkeypatch, state):
    db = pipeline_edges
    seed(db, documents(1), config={'source_extraction': state})
    scout_row(db)
    phases = []
    monkeypatch.setattr(orchestrator, 'run_phase', lambda script, args, name, **kw: phases.append(name) or 1.0)
    run_pipeline()
    assert phases == ['harvest', 'process', 'falsifier']


def drive_default_passes(db, monkeypatch, docs, progress):
    """Run the pipeline with NO continuation env; progress(n) -> (pending, attempted_count) after pass n."""
    seed(db, docs)
    scout_row(db)
    phases, done = [], []
    def phase(script, args, name, **kw):
        phases.append(name)
        if name in ('process', 'process-continue'):
            pending, attempted = progress(len(done))
            done.append(1)
            write_state(db, pending=pending, attempted=[d['url'] for d in docs[:attempted]])
        return 1.0
    monkeypatch.setattr(orchestrator, 'run_phase', phase)
    run_pipeline()
    return phases


def test_default_runs_two_continuation_passes_while_pending_remains(pipeline_edges, monkeypatch):
    # Default BEHIVE_A3_CONTINUATION_PASSES is 2 (was 1): pending 6 -> 4 -> 2 -> 1, third cap stops it.
    phases = drive_default_passes(pipeline_edges, monkeypatch, documents(8),
                                  lambda n: ([6, 4, 2, 1][n], n + 1))
    assert phases == ['harvest', 'process', 'process-continue', 'process-continue', 'falsifier']


def test_default_continuation_stops_early_when_pending_reaches_zero(pipeline_edges, monkeypatch):
    phases = drive_default_passes(pipeline_edges, monkeypatch, documents(8),
                                  lambda n: ([3, 0][n], n + 1))
    assert phases == ['harvest', 'process', 'process-continue', 'falsifier']


def test_default_continuation_stops_early_without_progress(pipeline_edges, monkeypatch):
    # The first continuation attempts nothing new -> the second default pass is not spent.
    phases = drive_default_passes(pipeline_edges, monkeypatch, documents(8),
                                  lambda n: ([6, 6, 6][n], [1, 1, 1][n]))
    assert phases == ['harvest', 'process', 'process-continue', 'falsifier']
