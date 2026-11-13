"""Real relevance decisions -> persisted gate -> salvage, private socket PG only."""
import asyncio
import json

import pytest

from behive.engine import claims_fallback, llm
from behive.engine.process import ProcessingDrones, RelevanceFilter
from test_content_relevance import CAPTURE, PAGES, SUPPLIER, TOPIC, documents, payload_of, recorded_rows_for
from test_repair1022_support import live_db

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness


@pytest.mark.parametrize('column_type', ['jsonb', 'json'])
@pytest.mark.parametrize('outcome', ['mixed', 'rejected', 'failed', 'bypass'])
def test_persisted_gate_is_exact_filter_output_with_model_grades(live_db, monkeypatch, column_type, outcome):
    mission = 'content-relevance-gate'
    docs = documents()
    if outcome == 'rejected':
        docs = [doc for i, doc in enumerate(docs) if i != 2]
    elif outcome == 'bypass':
        docs[0]['_operator_seed'] = True
        docs[1]['_from_api'] = True
        docs[1]['relevance_score'] = 0.9

    calls = []

    def replay(prompt, **kwargs):
        payload = payload_of(prompt)
        calls.append(payload)
        if outcome == 'failed':
            raise TimeoutError('explicit injected provider failure, not a real response')
        return json.dumps(recorded_rows_for(payload))

    monkeypatch.setattr(llm, 'complete', replay)
    with live_db.conn, live_db.conn.cursor() as cur:
        if column_type == 'json':
            cur.execute('ALTER TABLE hive_missions ALTER COLUMN config TYPE json USING config::json')
        cur.execute('CREATE SEQUENCE hive_facts_seq START 1000')
        cur.execute("INSERT INTO hive_missions (id,topic,status,config) VALUES (%s,%s,'process',%s)",
                    (mission, TOPIC, json.dumps({'unrelated': {'preserved': True}})))
        for i, doc in enumerate(docs, 1):
            cur.execute('''INSERT INTO hive_content (id,mission_id,url,title,raw_text,word_count)
                           VALUES (%s,%s,%s,%s,%s,%s)''',
                        (i, mission, doc['url'], doc['title'], doc['raw_text'],
                         doc.get('word_count') or len(doc['raw_text'].split())))
    kept = asyncio.run(RelevanceFilter(TOPIC).filter(docs))
    instance = ProcessingDrones.__new__(ProcessingDrones)
    instance.mission_id = mission
    instance._record_relevance_gate(docs, kept)
    # Exact persisted target readback (including legacy json column support).
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute('SELECT config FROM hive_missions WHERE id=%s', (mission,))
        config = cur.fetchone()[0]
    gate = config['relevance_gate']
    assert config['unrelated'] == {'preserved': True}
    assert gate['version'] == 1  # accepted_urls contract has not changed
    assert gate['considered'] == len(docs)
    assert gate['accepted_urls'] == sorted({doc['url'] for doc in kept})
    assert gate['grades_by_url'] == {doc['url']: doc['_relevance_grade'] for doc in docs
                                     if doc['_relevance_grade'] is not None}
    if outcome == 'mixed':
        assert gate['accepted_urls'] == [SUPPLIER]
        assert gate['grades_by_url'][PAGES['documents'][0]['url']] == 0
        assert gate['grades_by_url'][SUPPLIER] == 2
    elif outcome == 'rejected':
        assert gate['accepted_urls'] == []
    elif outcome == 'failed':
        assert len(gate['accepted_urls']) == 6 and gate['grades_by_url'] == {}
    else:
        assert set(gate['accepted_urls']) == {docs[0]['url'], docs[1]['url'], SUPPLIER}
        assert docs[0]['url'] not in gate['grades_by_url']
        assert docs[1]['url'] not in gate['grades_by_url']
    assert len(calls) == 1
    # Existing unmodified salvage consumes accepted_urls, not new grade fields.
    saved = claims_fallback.salvage_claims_from_content(mission)
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute('SELECT source_url FROM hive_claims WHERE mission_id=%s', (mission,))
        claim_urls = {row[0] for row in cur.fetchall()}
    assert claim_urls <= set(gate['accepted_urls'])
    if outcome == 'rejected':
        assert saved == 0 and claim_urls == set()
    elif outcome == 'mixed':
        assert saved > 0 and claim_urls == {SUPPLIER}
