"""Lifecycle/report regressions with real in-memory SQL; no PG/LLM/network."""
import asyncio
import sqlite3

import pytest
from fastapi import HTTPException
from behive import server


class DB:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.create_function('chr', 1, chr)
        self.db.executescript('''
            CREATE TABLE hive_missions(id TEXT PRIMARY KEY, status TEXT, phase TEXT, topic TEXT, synthesis TEXT);
            INSERT INTO hive_missions VALUES ('m','running','scout','topic',NULL);
            CREATE TABLE hive_sources(mission_id TEXT,url TEXT);
            CREATE TABLE hive_content(mission_id TEXT,url TEXT);
            CREATE TABLE hive_claims(mission_id TEXT,claim TEXT,source_url TEXT,is_garbage BOOLEAN,quality_score REAL,claim_type TEXT);
        ''')
        self.db.commit()
    def cursor(self): return self
    def execute(self, sql, params=()):
        self.row = self.db.execute(sql.replace('%s','?'),params)
        return self
    def fetchone(self): return self.row.fetchone()
    def commit(self):
        self.db.commit()
        self.committed_state = self.state
    def rollback(self): self.db.rollback()
    def close(self): pass
    @property
    def state(self): return self.db.execute('SELECT status,phase FROM hive_missions').fetchone()
    def claim(self, url='api://doc', evidence=True):
        if evidence:
            self.db.execute("INSERT INTO hive_content VALUES('m',?)", (url,))
        self.db.execute("INSERT INTO hive_claims(mission_id,claim,source_url,is_garbage,quality_score) VALUES('m','Documented factual claim.',?,false,0.8)", (url,))
        self.db.commit()


class Lines:
    def __init__(self): self.lines = iter([b'Process done\n', b'Traceback secret-token=not-for-clients\n'])
    def __aiter__(self): return self
    async def __anext__(self):
        try: return next(self.lines)
        except StopIteration: raise StopAsyncIteration


def run(monkeypatch, db, rc=0, salvage_failure=False, cancel=False, worker_done=False, worker_error=False):
    events, bridges = [], []
    async def spawn(*args, **kwargs):
        if cancel:
            db.execute("UPDATE hive_missions SET status='cancelled',phase='cancelled'")
            db.commit()
        if worker_error:
            db.execute("UPDATE hive_missions SET status='error',phase='error'")
            db.commit()
        if worker_done:
            db.execute("UPDATE hive_missions SET status='done',phase='done'")
            db.commit()
        class Proc:
            stdout = Lines()
            returncode = rc
            async def wait(self): return rc
        return Proc()
    def salvage(mid):
        if salvage_failure: raise RuntimeError('secret-token=not-for-clients')
        return 0
    import behive.engine.claims_fallback as fallback
    monkeypatch.setattr(server, 'get_db', lambda: db)
    monkeypatch.setattr(server.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(fallback, 'salvage_claims_from_content', salvage)
    monkeypatch.setattr(server, '_emit_event', lambda mid,event,data: events.append((event,data,db.state)))
    monkeypatch.setattr(server, '_bridge_done_mission', lambda mid: bridges.append(db.committed_state))
    asyncio.run(server._run_pipeline_inner('m','topic',1))
    return events,bridges


def test_nonzero_empty_worker_is_error_not_empty_scout(monkeypatch):
    db = DB()
    events, bridges = run(monkeypatch,db,rc=7)
    assert db.state == ('error','pipeline_failed')
    assert events[-1][0] == 'error' and events[-1][2][0] == 'error'
    assert events[-1][1]['exit_code'] == 7
    assert not bridges and 'secret-token' not in repr(events)


def test_salvage_exception_is_error_not_empty(monkeypatch):
    db = DB()
    events, bridges = run(monkeypatch,db,salvage_failure=True)
    assert db.state == ('error','claims_fallback_failed')
    assert 'secret-token' not in repr(events) and not bridges


@pytest.mark.parametrize('sources,phase', [(0,'empty_scout'),(2,'empty_claims')])
def test_clean_empty_identifies_missing_stage(monkeypatch,sources,phase):
    db = DB()
    for i in range(sources): db.execute('INSERT INTO hive_sources VALUES(?,?)',('m',f'api://{i}'))
    events, bridges = run(monkeypatch,db)
    assert db.state == ('empty',phase)
    assert events[-1][0] == 'empty' and not bridges


@pytest.mark.parametrize('rc,salvage_failure', [(7,False),(0,True)])
def test_error_cannot_overwrite_cancelled(monkeypatch,rc,salvage_failure):
    db = DB()
    events, bridges = run(monkeypatch,db,rc=rc,salvage_failure=salvage_failure,cancel=True)
    assert db.state == ('cancelled','cancelled')
    assert not bridges and not any(e in ('done','empty','error') for e,data,state in events)


def test_nonzero_sourced_salvage_remains_compatible(monkeypatch):
    db = DB()
    for _ in range(3): db.claim()
    db.execute("UPDATE hive_missions SET synthesis='report'")  # done needs a report since 2026-11-13
    db.commit()
    events, bridges = run(monkeypatch,db,rc=7)
    assert db.state == ('done','done') and bridges == [('done','done')]
    assert events[-1][1]['salvaged'] is True


def test_orphan_claims_cannot_mask_worker_failure(monkeypatch):
    db = DB()
    for _ in range(3): db.claim(evidence=False)
    events, bridges = run(monkeypatch,db,rc=7)
    assert db.state[0] == 'error' and not bridges


def test_provisional_done_without_product_corrected_to_error(monkeypatch):
    db = DB()
    events, bridges = run(monkeypatch,db,rc=7,worker_done=True)
    assert db.state == ('error','pipeline_failed')
    assert events[-1][0] == 'error' and not bridges


@pytest.mark.parametrize('status,phase', [('error','pipeline_failed'),('empty','empty_claims'),('cancelled','cancelled')])
def test_report_returns_terminal_response_not_202(monkeypatch,status,phase):
    db = DB()
    db.execute('UPDATE hive_missions SET status=?,phase=?',(status,phase))
    monkeypatch.setattr(server,'get_db',lambda: db)
    result = asyncio.run(server.get_report('m'))
    assert result.status_code == 409
    import json
    body = json.loads(result.body)
    assert body['status'] == status and body['phase'] == phase
    assert body['reason'] and isinstance(body['detail'],str)


def test_report_running_keeps_202(monkeypatch):
    db = DB()
    monkeypatch.setattr(server,'get_db',lambda: db)
    with pytest.raises(HTTPException) as error: asyncio.run(server.get_report('m'))
    assert error.value.status_code == 202


def test_report_done_keeps_payload(monkeypatch):
    db = DB()
    db.execute("UPDATE hive_missions SET status='done',phase='done',synthesis='report'")
    monkeypatch.setattr(server,'get_db',lambda: db)
    result = asyncio.run(server.get_report('m'))
    assert result['synthesis'] == 'report' and result['claims_count'] == 0


@pytest.mark.parametrize('status,phase', [('error','pipeline_failed'),('empty','empty_claims'),('cancelled','cancelled')])
def test_report_actual_route_is_terminal_and_redacted(monkeypatch,status,phase):
    import httpx
    db = DB()
    db.execute('UPDATE hive_missions SET status=?,phase=?',(status,phase))
    db.db.execute('ALTER TABLE hive_missions ADD COLUMN quality_metrics TEXT')
    db.execute('UPDATE hive_missions SET quality_metrics=?', ('{"error_message":"Authorization Bearer secret-provider-body"}',))
    monkeypatch.setattr(server,'get_db',lambda: db)
    monkeypatch.delenv('BEHIVE_API_KEY',raising=False)
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://offline.test') as client:
            return await client.get('/research/m/report')
    response = asyncio.run(request())
    assert response.status_code == 409 and response.json()['status'] == status
    assert 'secret-provider-body' not in response.text and 'Authorization' not in response.text


def test_report_actual_route_redacts_database_exception(monkeypatch):
    import httpx
    def fail(): raise RuntimeError('Authorization Bearer private-provider-request')
    monkeypatch.setattr(server,'get_db',fail)
    monkeypatch.delenv('BEHIVE_API_KEY',raising=False)
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://offline.test') as client:
            return await client.get('/research/m/report')
    response = asyncio.run(request())
    assert response.status_code == 500 and response.json() == {'detail':'Report unavailable'}
    assert 'Authorization' not in response.text and 'private-provider-request' not in response.text


def test_worker_explicit_clean_empty_keeps_empty_despite_exit_one(monkeypatch):
    db = DB()
    import behive.engine.claims_fallback as fallback
    def finish(mid):
        db.execute("UPDATE hive_missions SET status='empty',phase='empty_claims'")
        db.commit()
        return 0
    # Recovery runs in a thread: a separate connection is unsafe for SQLite, so
    # the controlled worker writes its empty transition during proc.wait instead.
    async def spawn(*args,**kwargs):
        class Proc:
            stdout = Lines()
            returncode = 1
            async def wait(self): finish('m'); return 1
        return Proc()
    monkeypatch.setattr(server,'get_db',lambda: db)
    monkeypatch.setattr(fallback,'salvage_claims_from_content',lambda mid: 0)
    monkeypatch.setattr(server.asyncio,'create_subprocess_exec',spawn)
    events=[]
    monkeypatch.setattr(server,'_emit_event',lambda mid,event,data: events.append(event))
    asyncio.run(server._run_pipeline_inner('m','topic',1))
    assert db.state[0] == 'empty' and events[-1] == 'empty'



def test_persisted_worker_error_is_not_clean_empty_even_with_exit_zero(monkeypatch):
    db = DB()
    events, bridges = run(monkeypatch,db,worker_error=True)
    assert db.state == ('error','pipeline_failed')
    assert events[-1][0] == 'error' and not bridges
