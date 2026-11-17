"""Four in-flight A3 pages, serialized SQL, clean draining and exact progress.

All providers are fakes; persistence uses the private-PG runner only.
"""
import asyncio
import logging
import threading
import time

import pytest

from behive.engine import db, llm, source_extraction
from test_a3_incremental_pg import (
    Clock, TOPIC, documents, drones, readback, response, seed, summary,
)
from test_repair1022_support import live_db

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness


@pytest.fixture(autouse=True)
def controlled_edges(monkeypatch):
    monkeypatch.delenv('BEHIVE_PHASE_DEADLINE', raising=False)
    monkeypatch.delenv('BEHIVE_A3_CONCURRENCY', raising=False)
    monkeypatch.setattr(source_extraction, 'TRANSIENT_RETRY_DELAY_S', 0)
    monkeypatch.setattr(llm, 'complete', lambda **kw: pytest.fail('Unconfigured model boundary'))


@pytest.mark.parametrize('value,expected', [(None, 4), ('1', 1), ('2', 2), ('99', 4), ('0', 1), ('-5', 1), ('bad', 4)])
def test_parallelism_is_default_four_and_clamped(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv('BEHIVE_A3_CONCURRENCY', value)
    docs = documents(6)
    lock = threading.Lock()
    active = peak = 0
    called, committed, callback_threads = [], [], []
    loop_thread = threading.get_ident()
    def complete(prompt, **kw):
        nonlocal active, peak
        doc = next(d for d in docs if d['raw_text'] in prompt)
        with lock:
            active += 1
            peak = max(peak, active)
            called.append(doc['url'])
        time.sleep(.01)
        with lock:
            active -= 1
        return response(doc)
    def commit(url, claims):
        callback_threads.append(threading.get_ident())
        assert len(claims) == 1
        committed.append(url)
    monkeypatch.setattr(llm, 'complete', complete)
    progress = source_extraction.ExtractionProgress()
    result = asyncio.run(source_extraction.extract_source_claims(docs, TOPIC, on_document=commit, progress=progress))
    assert peak == expected and active == 0
    assert sorted(called) == sorted(committed) == sorted(d['url'] for d in docs)
    assert len(set(committed)) == 6 and callback_threads == [loop_thread] * 6
    assert [c.source_url for c in result] == [d['url'] for d in docs]
    assert progress.status == 'completed'
    assert {k: progress.counts[k] for k in ('model_calls', 'responses', 'accepted', 'documents_considered')} == {
        'model_calls': 6, 'responses': 6, 'accepted': 6, 'documents_considered': 6}


@pytest.mark.parametrize('column_type', ['json', 'jsonb'])
def test_parallel_pages_commit_once_on_event_loop_with_pending_cap(live_db, monkeypatch, caplog, column_type):
    docs = documents(63)
    seed(live_db, docs, column_type)
    original = db.get_pg_connection
    db_threads = []
    def connect():
        db_threads.append(threading.get_ident())
        return original()
    monkeypatch.setattr(db, 'get_pg_connection', connect)
    calls = []
    refill = docs[source_extraction._concurrency()]
    def complete(prompt, **kw):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        if doc is refill:
            assert readback(live_db)[0], 'A free slot must be refilled only after a per-page commit'
        calls.append(doc['url'])
        time.sleep(.002)
        return response(doc)
    monkeypatch.setattr(llm, 'complete', complete)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(drones()._extract_source_claims(docs, TOPIC)) == 60
    rows, config = readback(live_db)
    assert len(rows) == len({r[1] for r in rows}) == 60
    assert config['source_extraction']['pending'] == 3
    assert config['source_extraction']['attempted_urls'] == sorted(d['url'] for d in docs[:60])
    assert set(db_threads) == {threading.get_ident()}
    assert summary(caplog).phase_a3_counts['claims_saved'] == 60
    assert summary(caplog).phase_a3_counts['documents_capped'] == 3
    assert summary(caplog).phase_a3_counts['pending'] == 3
    assert asyncio.run(drones()._extract_source_claims(docs, TOPIC)) == 3
    assert readback(live_db)[1]['source_extraction']['pending'] == 0
    assert sorted(calls) == sorted(d['url'] for d in docs)


@pytest.mark.parametrize('failure', ['provider', 'json', 'shape'])
def test_fatal_page_drains_inflight_sibling_and_keeps_committed_pages(live_db, monkeypatch, caplog, failure):
    # Choreography is the supported two-slot setting (one replacement sibling),
    # not the default of 4.
    monkeypatch.setenv('BEHIVE_A3_CONCURRENCY', '2')
    docs = documents(5)
    seed(live_db, docs)
    second_started = threading.Event()
    third_started = threading.Event()
    fatal_returned = threading.Event()
    calls = []
    def complete(prompt, **kw):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        calls.append(doc['url'])
        if doc is docs[0]:
            second_started.wait(.3)
            return response(doc)
        if doc is docs[1]:
            second_started.set()
            third_started.wait(.3)
            assert readback(live_db)[0], 'First sibling must already be committed'
            fatal_returned.set()
            if failure == 'provider':
                raise TimeoutError('PRIVATE_PROVIDER_BODY')
            return 'PRIVATE_PROVIDER_BODY' if failure == 'json' else '{}'
        if doc is docs[2]:
            third_started.set()
            assert fatal_returned.wait(1)
            time.sleep(.01)
            return response(doc)
        pytest.fail('No page may be admitted after a fatal result')
    monkeypatch.setattr(llm, 'complete', complete)
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        asyncio.run(drones()._extract_source_claims(docs, TOPIC))
    rows, config = readback(live_db)
    assert {r[1] for r in rows} == {docs[0]['url'], docs[2]['url']}
    attempted = {docs[0]['url'], docs[2]['url']}
    if failure != 'provider':
        attempted.add(docs[1]['url'])
    assert config['source_extraction']['attempted_urls'] == sorted(attempted)
    assert config['source_extraction']['pending'] == 5 - len(attempted)
    assert len(calls) == 3
    assert summary(caplog).phase_a3_status == 'failed'
    assert summary(caplog).phase_a3_counts['claims_saved'] == 2
    assert 'PRIVATE_PROVIDER_BODY' not in caplog.text


def test_deadline_accounts_for_slow_inflight_sibling(live_db, monkeypatch, caplog):
    # Admission estimate is checked at the supported two-slot setting; the
    # default of 4 would admit the later pages before the slow sibling is visible.
    monkeypatch.setenv('BEHIVE_A3_CONCURRENCY', '2')
    docs = documents(5)
    seed(live_db, docs)
    clock = Clock()
    second_started, third_returned = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setenv('BEHIVE_PHASE_DEADLINE', '1110')
    def complete(prompt, **kw):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        calls.append(doc['url'])
        if doc is docs[0]:
            second_started.wait(.3)
            clock.now = 1001
        elif doc is docs[1]:
            second_started.set()
            third_returned.wait(.3)
            time.sleep(.02)
        elif doc is docs[2]:
            clock.now = 1030
            third_returned.set()
        else:
            pytest.fail('Slow in-flight work must be included in admission estimate')
        return response(doc)
    monkeypatch.setattr(llm, 'complete', complete)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(drones()._extract_source_claims(docs, TOPIC, clock=clock)) == 3
    record = summary(caplog)
    assert record.phase_a3_status == 'partial'
    assert record.phase_a3_counts['documents_deadline_deferred'] == 2
    assert record.phase_a3_counts['claims_saved'] == 3
    assert readback(live_db)[1]['source_extraction']['pending'] == 2
    assert sorted(calls) == sorted(d['url'] for d in docs[:3])


def test_three_adjacent_failed_pages_stop_admission_and_drain(monkeypatch):
    # Failed-page slack (at most one extra sibling) encodes the old default of 2.
    monkeypatch.setenv('BEHIVE_A3_CONCURRENCY', '2')
    docs = documents(8)
    class BadGatewayError(Exception):
        pass
    calls = []
    def complete(prompt, **kw):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        calls.append(doc['url'])
        if doc is docs[0]:
            return response(doc)
        raise BadGatewayError('private')
    monkeypatch.setattr(llm, 'complete', complete)
    commits = []
    progress = source_extraction.ExtractionProgress()
    with pytest.raises(BadGatewayError):
        asyncio.run(source_extraction.extract_source_claims(docs, TOPIC,
            on_document=lambda url, claims: commits.append(url), progress=progress))
    assert commits == [docs[0]['url']]
    assert progress.status == 'failed'
    assert 3 <= progress.counts['documents_failed'] <= 4
    assert len(calls) == 1 + 2 * progress.counts['documents_failed']
    assert set(calls) <= {d['url'] for d in docs[:5]}


def test_cancellation_drains_threads_before_returning(monkeypatch):
    docs = documents(2)
    started, release = threading.Event(), threading.Event()
    active = []
    def complete(prompt, **kw):
        active.append(threading.get_ident())
        started.set()
        assert release.wait(1)
        active.pop()
        return '[]'
    monkeypatch.setattr(llm, 'complete', complete)
    async def run():
        task = asyncio.create_task(source_extraction.extract_source_claims(docs, TOPIC))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == [], 'No completion threads may outlive extraction cancellation'
    asyncio.run(run())


def test_pending_counts_distinct_provenance_pages_not_attempted_or_rejected(live_db, monkeypatch):
    docs = documents(4)
    seed(live_db, docs[:2], config={'keep': {'unrelated': True},
        'source_extraction': {'version': 1, 'attempted_urls': [docs[0]['url']]}})
    # docs[2:] have no provenance, and one harvested URL is repeated.
    monkeypatch.setattr(llm, 'complete', lambda **kw: '[]')
    assert asyncio.run(drones()._extract_source_claims([docs[0], docs[1], docs[1], *docs[2:]], TOPIC)) == 0
    config = readback(live_db)[1]
    assert config['source_extraction'] == {'version': 1,
        'attempted_urls': [docs[0]['url'], docs[1]['url']], 'pending': 0}
    assert config['keep'] == {'unrelated': True}


def test_parallel_persistence_failure_keeps_sibling_commit_and_failure_stage(live_db, monkeypatch, caplog):
    # Sibling drain is scripted for the supported two-slot setting, not the default of 4.
    monkeypatch.setenv('BEHIVE_A3_CONCURRENCY', '2')
    docs = documents(5)
    seed(live_db, docs)
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute('ALTER TABLE hive_claims ADD CHECK (source_url <> %s)', (docs[1]['url'],))
    second_started, third_started, second_returned = (threading.Event() for _ in range(3))
    def complete(prompt, **kw):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        if doc is docs[0]:
            second_started.wait(.3)
        elif doc is docs[1]:
            second_started.set()
            third_started.wait(.3)
            second_returned.set()
        elif doc is docs[2]:
            third_started.set()
            assert second_returned.wait(1)
            time.sleep(.05)
        else:
            pytest.fail('No new work after persistence failure')
        return response(doc)
    monkeypatch.setattr(llm, 'complete', complete)
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match='CheckViolation'):
        asyncio.run(drones()._extract_source_claims(docs, TOPIC))
    rows, config = readback(live_db)
    assert {r[1] for r in rows} == {docs[0]['url'], docs[2]['url']}
    assert config['source_extraction']['attempted_urls'] == sorted(r[1] for r in rows)
    assert config['source_extraction']['pending'] == 3
    assert summary(caplog).phase_a3_counts['claims_saved'] == 2
    assert summary(caplog).phase_a3_stage == 'persistence'


def test_skipped_input_does_not_reset_consecutive_provider_failures(monkeypatch):
    monkeypatch.setenv('BEHIVE_A3_CONCURRENCY', '1')
    docs = documents(6)
    docs[1]['url'] = docs[3]['url'] = 'file:///invalid-input'
    class BadGatewayError(Exception):
        pass
    calls = []
    def complete(prompt, **kw):
        doc = next(d for d in docs if d['raw_text'] in prompt)
        calls.append(doc['url'])
        if doc is docs[5]:
            return response(doc)
        raise BadGatewayError('private')
    monkeypatch.setattr(llm, 'complete', complete)
    progress = source_extraction.ExtractionProgress()
    with pytest.raises(BadGatewayError):
        asyncio.run(source_extraction.extract_source_claims(docs, TOPIC, progress=progress))
    assert progress.counts['model_calls'] == len(calls) == 6
    assert progress.counts['documents_failed'] == 3
    assert progress.counts['documents_skipped'] == 2
    assert docs[5]['url'] not in calls
