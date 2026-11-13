"""A1/A3 round continuation through the real process dispatcher and private SQL.

Non-target A0/A2/B/finalize effects are isolated; A1, A3, config SQL and provider
boundary dispatch stay real. Not a complete live mission.
"""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from behive.engine import llm, process
from behive.engine.db import PgConnection
from test_a3_incremental_pg import (
    MISSION, TOPIC, append_docs, documents, readback, response, seed,
)
from test_repair1022_support import live_db

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness


def run_round(monkeypatch):
    instance = process.ProcessingDrones.__new__(process.ProcessingDrones)
    instance.mission_id, instance.debug, instance.max_ops = MISSION, False, 1
    instance.relevance_threshold = .45
    instance.con = PgConnection()
    monkeypatch.setitem(__import__('sys').modules, 'hive_api_scout',
                        SimpleNamespace(scout_apis=AsyncMock(return_value=[])))
    monkeypatch.setitem(__import__('sys').modules, 'hive3_batch_client', None)
    monkeypatch.setitem(__import__('sys').modules, 'hive3_bees', None)
    monkeypatch.setattr(process.OperationRouter, 'route', lambda *args: [])
    monkeypatch.setattr(process.OperationRouter, '_load_feedback_fitness', lambda *args: {})
    monkeypatch.setattr(instance, '_fallback_extract', AsyncMock(return_value={}))
    for cls in (process.DeduplicationDrone, process.FactValidationDrone, process.ClaimCrossValidationDrone):
        monkeypatch.setattr(cls, 'run', lambda *args: 0)
    monkeypatch.setattr(process.GapDrone, 'run', lambda *args: [])
    monkeypatch.setattr(instance, '_finalize', lambda *args, **kwargs: None)
    monkeypatch.setattr(instance, '_print_summary', lambda **kwargs: None)
    try:
        asyncio.run(instance._run_async())
    finally:
        instance.con.close()


def provider(monkeypatch, docs, grades, empty=()):
    calls = {'gate': [], 'a3': []}
    def complete(prompt, **kwargs):
        if prompt.startswith('Evaluate the following'):
            payload = json.loads(prompt.split('\n', 1)[1])
            calls['gate'].extend(d['url'] for d in payload['documents'])
            return json.dumps([{'index': d['index'], 'grade': grades[d['url']], 'reason': 'test judgment'}
                               for d in payload['documents']])
        doc = next(d for d in docs if d['raw_text'] in prompt)
        calls['a3'].append(doc['url'])
        return '[]' if doc['url'] in empty else response(doc)
    monkeypatch.setattr(llm, 'complete', complete)
    return calls


@pytest.mark.parametrize('column_type', ['jsonb', 'json'])
def test_second_process_round_reuses_gate_and_a3_attempts(live_db, monkeypatch, column_type):
    docs = documents(4)
    seed(live_db, docs[:3], column_type)
    grades = {d['url']: g for d, g in zip(docs, (2, 0, 1, 2))}
    calls = provider(monkeypatch, docs, grades, empty=[docs[0]['url']])
    run_round(monkeypatch)
    assert set(calls['gate']) == {d['url'] for d in docs[:3]}
    assert set(calls['a3']) == {docs[0]['url'], docs[2]['url']}
    calls['gate'].clear()
    calls['a3'].clear()
    append_docs(live_db, docs[3:])
    run_round(monkeypatch)
    assert calls == {'gate': [docs[3]['url']], 'a3': [docs[3]['url']]}
    rows, config = readback(live_db)
    assert {r[0] for r in rows} == {d['raw_text'] for d in docs[2:]}
    assert config['keep'] == {'unrelated': True}
    assert config['source_extraction']['attempted_urls'] == sorted(d['url'] for d in (docs[0], docs[2], docs[3]))
    gate = config['relevance_gate']
    assert gate['version'] == 1 and gate['considered'] == 4
    assert gate['grades_by_url'] == grades
    assert set(gate['accepted_urls']) == {d['url'] for d in docs if grades[d['url']] >= 1}


VALID_GATE = {'version': 1, 'considered': 2,
              'accepted_urls': [documents(2)[0]['url']],
              'grades_by_url': {d['url']: g for d, g in zip(documents(2), (2, 0))}}
BAD_GATES = [None, [], {},
             dict(VALID_GATE, version=True), dict(VALID_GATE, version=2),
             dict(VALID_GATE, considered='2'), dict(VALID_GATE, considered=-1),
             dict(VALID_GATE, accepted_urls='https://source0.example.test/report'),
             dict(VALID_GATE, accepted_urls=[1]),
             dict(VALID_GATE, grades_by_url=[]),
             dict(VALID_GATE, grades_by_url={documents(2)[0]['url']: 2, documents(2)[1]['url']: True}),
             dict(VALID_GATE, grades_by_url={documents(2)[0]['url']: 2, documents(2)[1]['url']: 3}),
             dict(VALID_GATE, grades_by_url={documents(2)[0]['url']: 2, '': 0})]


@pytest.mark.parametrize('gate', BAD_GATES)
def test_malformed_stored_gate_is_ignored_wholly(live_db, monkeypatch, gate):
    docs = documents(2)
    seed(live_db, docs, config={'keep': {'unrelated': True}, 'relevance_gate': copy.deepcopy(gate)})
    grades = {d['url']: 1 for d in docs}
    calls = provider(monkeypatch, docs, grades)
    run_round(monkeypatch)
    assert set(calls['gate']) == {d['url'] for d in docs}, 'No partially trusted cache'
    assert len(calls['gate']) == 2
    assert readback(live_db)[1]['relevance_gate']['grades_by_url'] == grades


def test_a3_can_progress_past_the_a2_document_cap(live_db, monkeypatch):
    docs = documents(81)
    seed(live_db, docs)
    calls = provider(monkeypatch, docs, {d['url']: 2 for d in docs})
    run_round(monkeypatch)
    assert len(calls['a3']) == 60
    calls['gate'].clear()
    calls['a3'].clear()
    run_round(monkeypatch)
    assert calls['gate'] == []
    assert len(calls['a3']) == 21
    assert len(readback(live_db)[0]) == 81
