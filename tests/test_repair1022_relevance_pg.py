"""B2 operator provenance on captured live-shaped DDL, private socket PG only.

Uses the supplied guarded harness fixture, UUID schemas and actual loader SQL.
Only the external relevance-model boundary is replaced with a negative control.
"""
import asyncio

import pytest

from behive.engine import process
from test_repair1022_support import live_db

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness


@pytest.fixture
def loaded_documents(live_db):
    mission = 'repair1022-relevance'
    rows = [
        (10, mission, 'https://operator.example.test/seed', 0.2),
        (20, mission, 'https://ordinary.example.test/content', 0.9),
        (30, mission, 'https://foreign.example.test/same-url', None),
        (40, mission, 'https://content-only.example.test/api', 0.3),
        (50, 'foreign-mission', 'https://operator.example.test/seed', 1.0),
    ]
    with live_db.conn, live_db.conn.cursor() as cur:
        for id_, owner, url, quality in rows:
            cur.execute('''INSERT INTO hive_content
                (id,mission_id,url,title,raw_text,word_count,quality_score)
                VALUES (%s,%s,%s,'Operator-selected local evidence',%s,40,%s)''',
                        (id_, owner, url, 'A source intentionally outside the automatic topic vocabulary. ' * 4, quality))
        cur.execute('''INSERT INTO hive_sources (id,mission_id,url,source_type) VALUES
            (101,%s,'https://operator.example.test/seed','operator_seed'),
            (102,%s,'https://operator.example.test/seed','ddg'),
            (103,%s,'https://operator.example.test/seed','operator_seed'),
            (104,'foreign-mission','https://foreign.example.test/same-url','operator_seed'),
            (105,%s,'https://ordinary.example.test/content','ddg')''', (mission,) * 4)
    instance = object.__new__(process.ProcessingDrones)
    instance.mission_id = mission
    instance.con = process._db_connect_retry()
    try:
        yield instance._load_content()
    finally:
        instance.con.close()


def test_content_loader_carries_only_same_mission_url_seed_provenance(loaded_documents):
    assert [d['source_id'] for d in loaded_documents] == [20, 40, 10, 30], (
        'Keep quality ordering and content-only rows; duplicate sources must not multiply content')
    assert {d['source_id']: d.get('_operator_seed') for d in loaded_documents} == {
        10: True, 20: False, 30: False, 40: False}


def test_loaded_operator_seed_survives_keyword_and_negative_model_filters(loaded_documents, monkeypatch):
    seen = []
    async def reject(self, docs):
        seen.extend(d['url'] for d in docs)
        # Negative model (grade 0 for everything; contract since 2026-10-23).
        return [{'index': i, 'grade': 0, 'reason': 'test control'} for i in range(len(docs))]
    monkeypatch.setattr(process.RelevanceFilter, 'score_batch', reject)
    selected = asyncio.run(process.RelevanceFilter('WMS telemetry supplier pricing').filter(loaded_documents))
    assert [d['url'] for d in selected] == ['https://operator.example.test/seed']
    assert 'https://operator.example.test/seed' not in seen


@pytest.mark.parametrize('marker', [{'_operator_seed': True}, {'source_type': 'operator_seed'}])
def test_explicit_seed_marker_bypasses_all_relevance_models(marker, monkeypatch):
    async def forbidden(*args):
        pytest.fail('Operator-selected sources must never be submitted for model rejection')
    monkeypatch.setattr(process.RelevanceFilter, 'score_batch', forbidden)
    doc = {'url': 'https://operator.example.test/explicit', 'raw_text': 'Unrelated text. ' * 20, **marker}
    selected = asyncio.run(process.RelevanceFilter('WMS telemetry').filter([doc]))
    assert selected == [doc]


def test_seed_also_marked_as_api_is_returned_exactly_once(monkeypatch):
    async def forbidden(*args):
        pytest.fail('No external scoring is needed')
    monkeypatch.setattr(process.RelevanceFilter, 'score_batch', forbidden)
    doc = {'url': 'https://operator.example.test/api-seed', '_operator_seed': True,
           '_from_api': True, 'relevance_score': 0.0, 'raw_text': 'Operator evidence.'}
    assert asyncio.run(process.RelevanceFilter('WMS telemetry').filter([doc])) == [doc]
