"""Content gate runs up to BEHIVE_GATE_CONCURRENCY (default 4, clamp 1..4) batches at once."""
import asyncio
import json
import threading
import time

import pytest

from behive.engine import llm
from behive.engine.process import RelevanceFilter
from test_content_relevance import PAGES, TOPIC, payload_of, recorded_rows_for

N_BATCHES = 5


def many_docs():
    # 15 * N_BATCHES docs, every one a keyword candidate -> exactly N_BATCHES model calls.
    return [dict(PAGES['documents'][i % 8], _copy=i) for i in range(15 * N_BATCHES)]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv('BEHIVE_GATE_CONCURRENCY', raising=False)
    monkeypatch.delenv('BEHIVE_LLM_STRICT', raising=False)


def install(monkeypatch, fail_when=None, delay=0.05):
    lock = threading.Lock()
    state = {'active': 0, 'peak': 0, 'calls': 0}

    def replay(prompt, **kwargs):
        payload = payload_of(prompt)
        with lock:
            state['calls'] += 1
            state['active'] += 1
            state['peak'] = max(state['peak'], state['active'])
        try:
            time.sleep(delay)  # keep calls overlapping so concurrency is observable
            if fail_when and fail_when(payload):
                raise RuntimeError('boom')
            return json.dumps(recorded_rows_for(payload))
        finally:
            with lock:
                state['active'] -= 1

    monkeypatch.setattr(llm, 'complete', replay)
    return state


def run(docs):
    kept = asyncio.run(RelevanceFilter(TOPIC).filter(docs))
    return kept


def test_default_allows_four_calls_in_flight(monkeypatch):
    state = install(monkeypatch)
    run(many_docs())
    assert state['calls'] == N_BATCHES
    assert state['peak'] == 4


def test_concurrency_one_serialises(monkeypatch):
    monkeypatch.setenv('BEHIVE_GATE_CONCURRENCY', '1')
    state = install(monkeypatch)
    run(many_docs())
    assert state['calls'] == N_BATCHES
    assert state['peak'] == 1


@pytest.mark.parametrize('value,expected', [
    ('1', 1), ('2', 2), ('3', 3), ('99', 4), ('0', 1), ('-4', 1),
    ('abc', 4), ('', 4), ('1.5', 4), (' 1 ', 1),
])
def test_value_is_clamped_and_invalid_defaults_to_four(monkeypatch, value, expected):
    monkeypatch.setenv('BEHIVE_GATE_CONCURRENCY', value)
    state = install(monkeypatch)
    run(many_docs())
    assert state['peak'] == expected


def test_results_and_order_match_sequential_grading(monkeypatch):
    monkeypatch.setenv('BEHIVE_GATE_CONCURRENCY', '1')
    install(monkeypatch, delay=0)
    seq_docs = many_docs()
    seq = run(seq_docs)
    monkeypatch.delenv('BEHIVE_GATE_CONCURRENCY')
    install(monkeypatch, delay=0.02)
    par_docs = many_docs()
    par = run(par_docs)
    assert [d['_copy'] for d in par] == [d['_copy'] for d in seq]
    for a, b in zip(seq_docs, par_docs):
        for key in ('_relevance_grade', '_relevance_reason', '_relevance_method', '_relevance_score'):
            assert a[key] == b[key]


def test_one_failed_parallel_batch_falls_back_for_that_batch_only(monkeypatch):
    docs = many_docs()
    # Batches start at i%8 offsets 0,7,6,5,4, so their URL sequences are distinct.
    bad = [d['url'] for d in docs[15:30]]

    def fail_second(payload):
        return [d['url'] for d in payload['documents']] == bad

    state = install(monkeypatch, fail_when=fail_second)
    kept = run(docs)
    assert state['calls'] == N_BATCHES and state['peak'] == 4
    methods = [d['_relevance_method'] for d in docs]
    assert set(methods[15:30]) == {'keyword_fallback'}
    assert all(m == 'model' for m in methods[:15] + methods[30:])
    assert all(d['_relevance_grade'] is None for d in docs[15:30])
    assert all(d['_relevance_grade'] is not None for d in docs[:15] + docs[30:])
    assert all(d['_relevance_reason'] == 'model_error:RuntimeError' for d in docs[15:30])
    assert any(d['_relevance_grade'] is not None for d in kept)
