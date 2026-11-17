"""Bridge slots: the relevance gate and source extraction keep 4 model calls in flight.

Patched at the llm.complete boundary (no network). Default, env 8 and env "x"
peak at 4 and never 5; env 1 peaks at 1. Gate batches and extraction pages are
exercised separately: ProcessingDrones awaits the gate (A1) before extraction (A3).
"""
import asyncio
import json
import threading
import time

import pytest

from behive.engine import llm, source_extraction
from behive.engine.process import RelevanceFilter

TOPIC = 'Solar capacity Poland'
GATE_BATCHES = 6  # CONTENT_BATCH_SIZE is 15; six batches can exceed 4 slots
EXTRACTION_PAGES = 8


def _gate_docs(n_batches):
    text = 'Solar capacity in Poland reached 17 GW in 2024 according to the regulator. '
    return [{'url': f'https://example.test/{i}', 'title': f'Solar capacity Poland {i}',
             'raw_text': text * 4} for i in range(15 * n_batches)]


def _extraction_docs(n):
    return [{'url': f'https://source{i}.example.test/report',
             'raw_text': f'Acme supplied {i + 42} warehouse monitoring systems in Poland during 2025.'}
            for i in range(n)]


def _install_slow_complete(monkeypatch, delay=0.08):
    lock = threading.Lock()
    state = {'active': 0, 'peak': 0, 'calls': 0, 'saw_five': False}

    def complete(prompt, **kwargs):
        with lock:
            state['active'] += 1
            state['calls'] += 1
            state['peak'] = max(state['peak'], state['active'])
            state['saw_five'] = state['saw_five'] or state['active'] >= 5
        try:
            time.sleep(delay)
            if prompt.lstrip().startswith('Evaluate'):
                payload = json.loads(prompt.split('\n', 1)[1])
                return json.dumps([
                    {'index': doc['index'], 'grade': 1, 'reason': 'useful evidence'}
                    for doc in payload['documents']
                ])
            return '[]'
        finally:
            with lock:
                state['active'] -= 1

    monkeypatch.setattr(llm, 'complete', complete)
    return state


@pytest.fixture(autouse=True)
def clean_caps(monkeypatch):
    monkeypatch.delenv('BEHIVE_GATE_CONCURRENCY', raising=False)
    monkeypatch.delenv('BEHIVE_A3_CONCURRENCY', raising=False)
    monkeypatch.delenv('BEHIVE_PHASE_DEADLINE', raising=False)
    monkeypatch.delenv('BEHIVE_LLM_STRICT', raising=False)


@pytest.mark.parametrize('env_value,expected', [
    (None, 4), ('8', 4), ('1', 1), ('x', 4),
])
def test_gate_reaches_four_in_flight_and_never_five(monkeypatch, env_value, expected):
    if env_value is not None:
        monkeypatch.setenv('BEHIVE_GATE_CONCURRENCY', env_value)
    state = _install_slow_complete(monkeypatch)
    asyncio.run(RelevanceFilter(TOPIC).filter(_gate_docs(GATE_BATCHES)))
    assert state['calls'] == GATE_BATCHES
    assert state['peak'] == expected
    assert state['peak'] <= 4
    assert state['saw_five'] is False
    assert state['active'] == 0


@pytest.mark.parametrize('env_value,expected', [
    (None, 4), ('8', 4), ('1', 1), ('x', 4),
])
def test_extraction_reaches_four_in_flight_and_never_five(monkeypatch, env_value, expected):
    if env_value is not None:
        monkeypatch.setenv('BEHIVE_A3_CONCURRENCY', env_value)
    state = _install_slow_complete(monkeypatch)
    docs = _extraction_docs(EXTRACTION_PAGES)
    claims = asyncio.run(source_extraction.extract_source_claims(docs, TOPIC))
    assert claims == []
    assert state['calls'] == EXTRACTION_PAGES
    assert state['peak'] == expected
    assert state['peak'] <= 4
    assert state['saw_five'] is False
    assert state['active'] == 0
