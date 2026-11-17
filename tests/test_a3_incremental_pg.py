"""Phase A3 contract: real private SQL, fake completion/time, no live missions.

Run only with run_existing_pg_regressions.py --noconftest / unshare --net.
"""
import asyncio
from contextlib import closing
import json
import logging
import os
import signal
import subprocess
import sys

import psycopg2
import pytest

from behive.engine import llm, source_extraction
from behive.engine.process import ProcessingDrones
from test_repair1022_support import live_db

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness

MISSION = 'a3-incremental'
TOPIC = 'Acme warehouse monitoring systems'


def documents(n):
    return [{'url': f'https://source{i}.example.test/report',
             'raw_text': f'Acme supplied {i + 42} warehouse monitoring systems in Poland during 2025.',
             'title': 'Acme warehouse monitoring systems', 'source_id': str(i + 1)}
            for i in range(n)]


def response(doc):
    return json.dumps([{'claim': doc['raw_text'], 'evidence': doc['raw_text'], 'confidence': .9}])


def seed(db, docs, column_type='jsonb', config=None):
    with db.conn, db.conn.cursor() as cur:
        if column_type == 'json':
            cur.execute('ALTER TABLE hive_missions ALTER COLUMN config TYPE json USING config::json')
        cur.execute('CREATE SEQUENCE IF NOT EXISTS hive_facts_seq START 1000')
        cur.execute("INSERT INTO hive_missions (id,topic,status,config) VALUES (%s,%s,'process',%s)",
                    (MISSION, TOPIC, json.dumps(config or {'keep': {'unrelated': True}})))
    append_docs(db, docs)


def append_docs(db, docs):
    with db.conn, db.conn.cursor() as cur:
        cur.execute('SELECT COALESCE(MAX(id),0) FROM hive_content')
        first = cur.fetchone()[0] + 1
        for i, doc in enumerate(docs, first):
            cur.execute('''INSERT INTO hive_content (id,mission_id,url,title,raw_text,word_count)
                           VALUES (%s,%s,%s,%s,%s,100)''',
                        (i, MISSION, doc['url'], doc['title'], doc['raw_text']))


def drones():
    instance = ProcessingDrones.__new__(ProcessingDrones)
    instance.mission_id = MISSION
    return instance


def readback(db):
    # Independent connection is essential: a connection reading its own INSERT
    # would incorrectly certify the old all-or-nothing transaction.
    with closing(psycopg2.connect(db.dsn)) as conn, conn, conn.cursor() as cur:
        cur.execute('SELECT claim,source_url,claim_type,source_operation FROM hive_claims ORDER BY id')
        rows = cur.fetchall()
        cur.execute('SELECT config FROM hive_missions WHERE id=%s', (MISSION,))
        return rows, cur.fetchone()[0]


def summary(caplog):
    return [r for r in caplog.records if hasattr(r, 'phase_a3_counts')][-1]


@pytest.fixture(autouse=True)
def controlled_edges(monkeypatch):
    # These incremental/ordering tests exercise the supported one-slot setting;
    # default-four persistence and clean draining live in test_a3_parallel_pg.py.
    monkeypatch.setenv('BEHIVE_A3_CONCURRENCY', '1')
    monkeypatch.delenv('BEHIVE_PHASE_DEADLINE', raising=False)
    monkeypatch.setattr(source_extraction, 'TRANSIENT_RETRY_DELAY_S', 0)
    def blocked(*args, **kwargs):
        raise AssertionError('Unconfigured completion boundary')
    monkeypatch.setattr(llm, 'complete', blocked)


def test_one_slot_finished_document_commits_before_next_completion(live_db, monkeypatch):
    docs = documents(2)
    seed(live_db, docs)
    calls = []
    def complete(prompt, **kwargs):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        if calls:
            rows, config = readback(live_db)
            assert rows == [(docs[0]['raw_text'], docs[0]['url'], 'source_extraction', 'byok_source_extraction')], \
                'Document 1 must be committed before document 2 inference'
            assert config['source_extraction'] == {'version': 1, 'attempted_urls': [docs[0]['url']], 'pending': 1}
        calls.append(doc)
        return response(doc)
    monkeypatch.setattr(llm, 'complete', complete)
    assert asyncio.run(drones()._extract_source_claims(docs, TOPIC)) == 2
    assert calls == docs
    assert len(readback(live_db)[0]) == 2


@pytest.mark.parametrize('failure', ['provider', 'json', 'shape', 'three_transient'])
def test_later_fatal_failure_preserves_finished_documents(live_db, monkeypatch, caplog, failure):
    docs = documents(6)
    seed(live_db, docs)
    class BadGatewayError(Exception):
        pass
    calls = []
    def complete(prompt, **kwargs):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        calls.append(doc['url'])
        if doc in docs[:2]:
            return response(doc)
        if failure == 'provider':
            raise TimeoutError('PRIVATE_PROVIDER_TEXT')
        if failure == 'three_transient':
            raise BadGatewayError('PRIVATE_PROVIDER_TEXT')
        return 'PRIVATE_PROVIDER_TEXT' if failure == 'json' else '{}'
    monkeypatch.setattr(llm, 'complete', complete)
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        asyncio.run(drones()._extract_source_claims(docs, TOPIC))
    rows, config = readback(live_db)
    assert [r[0] for r in rows] == [d['raw_text'] for d in docs[:2]]
    attempted = docs[:3] if failure in ('json', 'shape') else docs[:2]
    assert config['source_extraction']['attempted_urls'] == sorted(d['url'] for d in attempted)
    assert config['keep'] == {'unrelated': True}
    assert summary(caplog).phase_a3_status == 'failed'
    assert summary(caplog).phase_a3_counts['claims_saved'] == 2
    assert 'PRIVATE_PROVIDER_TEXT' not in caplog.text
    assert len(calls) == (8 if failure == 'three_transient' else 3)


def test_sigkill_during_next_completion_keeps_committed_document(live_db):
    docs = documents(2)
    seed(live_db, docs)
    child = '''
import asyncio,json,os,signal
from behive.engine import db, llm
from behive.engine.process import ProcessingDrones
db.DSN=os.environ['A3_TEST_DSN']
docs=json.loads(os.environ['A3_TEST_DOCS'])
def complete(prompt, **kwargs):
    if docs[1]['raw_text'] in prompt:
        os.kill(os.getpid(),signal.SIGKILL)
    text=docs[0]['raw_text']
    return json.dumps([dict(claim=text,evidence=text,confidence=.9)])
llm.complete=complete
d=ProcessingDrones.__new__(ProcessingDrones)
d.mission_id='a3-incremental'
asyncio.run(d._extract_source_claims(docs,'Acme warehouse monitoring systems'))
'''
    env = {**os.environ, 'BEHIVE_DB_URL': os.environ['CLAIM_TEST_PG_DSN'],
           'DATABASE_URL': os.environ['CLAIM_TEST_PG_DSN'], 'A3_TEST_DSN': live_db.dsn,
           'BEHIVE_BACKEND': 'postgres', 'A3_TEST_DOCS': json.dumps(docs)}
    result = subprocess.run([sys.executable, '-c', child], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == -signal.SIGKILL, result.stderr
    rows, config = readback(live_db)
    assert [r[0] for r in rows] == [docs[0]['raw_text']]
    assert config['source_extraction']['attempted_urls'] == [docs[0]['url']]


class Clock:
    def __init__(self):
        self.now = 1000.0
    def __call__(self):
        return self.now


@pytest.mark.parametrize('duration,deadline', [(20, '1100'), (40, '1170')])
def test_deadline_defers_remaining_documents_and_returns_partial(live_db, monkeypatch, caplog, duration, deadline):
    docs = documents(5)
    seed(live_db, docs)
    clock, calls = Clock(), []
    monkeypatch.setenv('BEHIVE_PHASE_DEADLINE', deadline)
    def complete(prompt, **kwargs):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        calls.append(doc)
        clock.now += duration
        return response(doc)
    monkeypatch.setattr(llm, 'complete', complete)
    with caplog.at_level(logging.DEBUG):
        count = asyncio.run(drones()._extract_source_claims(docs, TOPIC, clock=clock))
    assert count == 2 and calls == docs[:2]
    rows, config = readback(live_db)
    assert [r[0] for r in rows] == [d['raw_text'] for d in docs[:2]]
    record = summary(caplog)
    assert record.phase_a3_status == 'partial'
    assert record.phase_a3_counts['documents_deadline_deferred'] == 3
    assert record.phase_a3_counts['claims_saved'] == 2
    assert config['source_extraction']['attempted_urls'] == sorted(d['url'] for d in docs[:2])


@pytest.mark.parametrize('deadline', [None, '', 'garbage', 'nan', 'inf', '-inf'])
def test_absent_or_invalid_deadline_does_not_limit_extraction(live_db, monkeypatch, deadline):
    docs = documents(3)
    seed(live_db, docs)
    if deadline is not None:
        monkeypatch.setenv('BEHIVE_PHASE_DEADLINE', deadline)
    calls = []
    def complete(prompt, **kwargs):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        calls.append(doc)
        return response(doc)
    monkeypatch.setattr(llm, 'complete', complete)
    assert asyncio.run(drones()._extract_source_claims(docs, TOPIC)) == 3
    assert calls == docs


@pytest.mark.parametrize('column_type', ['jsonb', 'json'])
def test_later_round_skips_attempts_including_empty_and_rejected_responses(live_db, monkeypatch, caplog, column_type):
    docs = documents(4)
    seed(live_db, docs, column_type)
    calls = []
    def complete(prompt, **kwargs):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        calls.append(doc['url'])
        if doc is docs[0]:
            return '[]'
        if doc is docs[1]:
            return json.dumps([{'claim': 'invented claim', 'evidence': 'invented', 'confidence': .9}])
        return response(doc)
    monkeypatch.setattr(llm, 'complete', complete)
    assert asyncio.run(drones()._extract_source_claims(docs[:3], TOPIC)) == 1
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(drones()._extract_source_claims(docs, TOPIC)) == 1
    assert calls == [d['url'] for d in docs]
    assert summary(caplog).phase_a3_counts['documents_already_attempted'] == 3
    rows, config = readback(live_db)
    assert [r[0] for r in rows] == [d['raw_text'] for d in docs[2:]]
    assert config['source_extraction'] == {'version': 1, 'attempted_urls': sorted(d['url'] for d in docs), 'pending': 0}
    assert config['keep'] == {'unrelated': True}


def test_attempted_urls_are_removed_before_document_cap(live_db, monkeypatch):
    docs = documents(62)
    seed(live_db, docs)
    calls = []
    def complete(prompt, **kwargs):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        calls.append(doc['url'])
        return response(doc)
    monkeypatch.setattr(llm, 'complete', complete)
    assert asyncio.run(drones()._extract_source_claims(docs, TOPIC)) == 60
    assert asyncio.run(drones()._extract_source_claims(docs, TOPIC)) == 2
    assert calls == [d['url'] for d in docs]
    assert len(readback(live_db)[0]) == 62


def test_no_response_is_retried_on_later_round(live_db, monkeypatch, caplog):
    docs = documents(3)
    seed(live_db, docs)
    class BadGatewayError(Exception):
        pass
    calls = []
    fail = True
    def complete(prompt, **kwargs):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        calls.append(doc['url'])
        if fail and doc is docs[1]:
            raise BadGatewayError('not a model response')
        return response(doc)
    monkeypatch.setattr(llm, 'complete', complete)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(drones()._extract_source_claims(docs, TOPIC)) == 2
    assert summary(caplog).phase_a3_status == 'partial'
    assert readback(live_db)[1]['source_extraction']['attempted_urls'] == [docs[0]['url'], docs[2]['url']]
    fail = False
    calls.clear()
    assert asyncio.run(drones()._extract_source_claims(docs, TOPIC)) == 1
    assert calls == [docs[1]['url']]
    assert len(readback(live_db)[0]) == 3


@pytest.mark.parametrize('deadline,expected', [('1064', 0), ('1065', 1)])
def test_deadline_admission_boundary_is_inclusive(live_db, monkeypatch, caplog, deadline, expected):
    docs = documents(3)
    seed(live_db, docs)
    clock, calls = Clock(), []
    monkeypatch.setenv('BEHIVE_PHASE_DEADLINE', deadline)
    def complete(prompt, **kwargs):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        calls.append(doc)
        clock.now += 20
        return response(doc)
    monkeypatch.setattr(llm, 'complete', complete)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(drones()._extract_source_claims(docs, TOPIC, clock=clock)) == expected
    assert len(calls) == expected and len(readback(live_db)[0]) == expected
    assert summary(caplog).phase_a3_status == 'partial'
    assert summary(caplog).phase_a3_counts['documents_deadline_deferred'] == 3 - expected


def test_progress_write_failure_rolls_back_only_that_document(live_db, monkeypatch, caplog):
    docs = documents(2)
    seed(live_db, docs)
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute('''ALTER TABLE hive_missions ADD CHECK (
            NOT (COALESCE(config::jsonb->'source_extraction'->'attempted_urls','[]'::jsonb) ? %s))''',
                    (docs[1]['url'],))
    monkeypatch.setattr(llm, 'complete', lambda prompt, **kwargs:
                        response(next(d for d in docs if d['raw_text'] in prompt)))
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match='CheckViolation'):
        asyncio.run(drones()._extract_source_claims(docs, TOPIC))
    rows, config = readback(live_db)
    assert [r[0] for r in rows] == [docs[0]['raw_text']]
    assert config['source_extraction']['attempted_urls'] == [docs[0]['url']]
    assert summary(caplog).phase_a3_counts['claims_saved'] == 1
    assert summary(caplog).phase_a3_stage == 'persistence'


@pytest.mark.parametrize('state', [None, [], {}, {'version': 2, 'attempted_urls': []},
    {'version': True, 'attempted_urls': []}, {'version': 1, 'attempted_urls': 'bad'},
    {'version': 1, 'attempted_urls': [documents(1)[0]['url'], 1]}])
def test_malformed_attempt_state_is_not_partially_trusted(live_db, monkeypatch, state):
    docs = documents(1)
    seed(live_db, docs, config={'keep': {'unrelated': True}, 'source_extraction': state})
    calls = []
    def complete(**kwargs):
        calls.append(1)
        return response(docs[0])
    monkeypatch.setattr(llm, 'complete', complete)
    assert asyncio.run(drones()._extract_source_claims(docs, TOPIC)) == 1
    assert calls == [1]
    assert readback(live_db)[1]['source_extraction'] == {'version': 1, 'attempted_urls': [docs[0]['url']], 'pending': 0}
