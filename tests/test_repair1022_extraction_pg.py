"""A3 diagnostics on real disposable PG via core harness; never production.

Only the provider boundary is substituted. Captured [] and explicitly synthetic
positive/negative controls execute the real provenance reads and atomic writer.
"""
import asyncio
import json
import logging
import os
from urllib.parse import parse_qs, urlparse
import uuid

import psycopg2
from psycopg2 import sql
import pytest
from behive.engine.process import ProcessingDrones
from test_repair1022_extraction_diagnostics import (
    CAPTURED_RESPONSE, CAPTURED_SOURCE, CAPTURED_TOPIC, CAPTURED_URL, CLEAN_CLAIM,
)

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness


@pytest.fixture
def private_pg(monkeypatch):
    dsn = os.environ['CLAIM_TEST_PG_DSN']
    socket_path = parse_qs(urlparse(dsn).query).get('host', [''])[0]
    assert socket_path.startswith('/tmp/behive-claim-pg-repair-'), 'Private core harness required'
    schema = 'repair1022_b_' + uuid.uuid4().hex
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute('SELECT current_database(),inet_server_addr()')
            assert cur.fetchone() == ('postgres', None)
            cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            cur.execute(sql.SQL('SET search_path = {}').format(sql.Identifier(schema)))
            cur.execute('CREATE TABLE hive_content (mission_id text,url text)')
            cur.execute('CREATE TABLE hive_sources (mission_id text,url text)')
            cur.execute('CREATE TABLE hive_missions (id text PRIMARY KEY,config jsonb)')
            cur.execute("INSERT INTO hive_missions VALUES ('repair1022-b','{}')")
            cur.execute('''CREATE TABLE hive_claims (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                mission_id text,claim text,evidence text,source_url text,claim_type text,
                confidence double precision,quality_score double precision,
                source_operation text,is_garbage boolean,extracted_at timestamptz)''')
            cur.execute('INSERT INTO hive_content VALUES (%s,%s)', ('repair1022-b', CAPTURED_URL))
        def connect():
            return psycopg2.connect(dsn, options='-c search_path=' + schema)
        monkeypatch.setattr('behive.engine.db.get_pg_connection', connect)
        yield conn
    finally:
        with conn, conn.cursor() as cur:
            cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            cur.execute('SELECT count(*) FROM pg_namespace WHERE nspname=%s', (schema,))
            assert cur.fetchone() == (0,)
        conn.close()


def drones():
    instance = object.__new__(ProcessingDrones)
    instance.mission_id = 'repair1022-b'
    return instance


def phase_summary(caplog):
    records = [r for r in caplog.records if hasattr(r, 'phase_a3_counts')]
    assert len(records) == 1, 'A3 must explain provenance, cap and persistence counts even on zero/failure'
    return records[0]


def test_a3_zero_candidates_reports_provenance_cap_and_valid_empty_response(private_pg, monkeypatch, caplog):
    from behive.engine import source_extraction
    monkeypatch.setattr(source_extraction, 'MAX_DOCUMENTS', 2)
    monkeypatch.setattr('behive.engine.llm.complete', lambda **kwargs: CAPTURED_RESPONSE)
    doc = {'url': CAPTURED_URL, 'raw_text': CAPTURED_SOURCE}
    # Three distinct pages, rather than three copies of one attempted URL.
    docs = [dict(doc, url=CAPTURED_URL + f'?a3-page={i}') for i in range(3)]
    with private_pg, private_pg.cursor() as cur:
        cur.executemany('INSERT INTO hive_content VALUES (%s,%s)',
                        [('repair1022-b', d['url']) for d in docs])
    with caplog.at_level(logging.DEBUG):
        count = asyncio.run(drones()._extract_source_claims(
            docs + [{'url': 'https://not-harvested.test', 'raw_text': CAPTURED_SOURCE}], CAPTURED_TOPIC))
    assert count == 0
    record = phase_summary(caplog)
    assert record.phase_a3_status == 'completed'
    assert record.levelno == logging.WARNING
    assert record.phase_a3_counts == {
        'documents_input': 4, 'documents_with_provenance': 3,
        'documents_selected': 2, 'documents_capped': 1,
        'documents_already_attempted': 0, 'documents_deadline_deferred': 0,
        'claims_extracted': 0, 'claims_duplicates': 0, 'claims_saved': 0, 'pending': 1,
    }
    with private_pg, private_pg.cursor() as cur:
        cur.execute('SELECT count(*) FROM hive_claims')
        assert cur.fetchone() == (0,)


def test_a3_persistence_summary_matches_real_committed_rows(private_pg, monkeypatch, caplog):
    # Explicitly synthetic quote control, not a captured mission/model claim.
    response = json.dumps([{'claim': CLEAN_CLAIM, 'evidence': CLEAN_CLAIM, 'confidence': .9}])
    monkeypatch.setattr('behive.engine.llm.complete', lambda **kwargs: response)
    other_url = 'https://synthetic-other.example.test/report'
    with private_pg, private_pg.cursor() as cur:
        cur.execute('INSERT INTO hive_content VALUES (%s,%s)', ('repair1022-b', other_url))
    with caplog.at_level(logging.DEBUG):
        count = asyncio.run(drones()._extract_source_claims(
            [{'url': CAPTURED_URL, 'raw_text': CLEAN_CLAIM},
             {'url': other_url, 'raw_text': CLEAN_CLAIM}], CAPTURED_TOPIC))
    assert count == 1
    record = phase_summary(caplog)
    assert record.phase_a3_status == 'completed'
    assert record.levelno == logging.INFO
    assert record.phase_a3_counts['claims_extracted'] == 2
    assert record.phase_a3_counts['claims_duplicates'] == record.phase_a3_counts['claims_saved'] == 1
    with private_pg, private_pg.cursor() as cur:
        cur.execute('SELECT claim,source_url FROM hive_claims')
        rows = cur.fetchall()
        assert len(rows) == 1 and rows[0][0] == CLEAN_CLAIM
        assert rows[0][1] in (CAPTURED_URL, other_url)


def test_a3_failure_summary_exposes_stage_and_type_not_provider_payload(private_pg, monkeypatch, caplog):
    def fail(**kwargs):
        raise RuntimeError('PRIVATE_PROVIDER_TOKEN')
    monkeypatch.setattr('behive.engine.llm.complete', fail)
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match=r'failed \(RuntimeError\)'):
        asyncio.run(drones()._extract_source_claims(
            [{'url': CAPTURED_URL, 'raw_text': CAPTURED_SOURCE}], CAPTURED_TOPIC))
    record = phase_summary(caplog)
    assert record.phase_a3_status == 'failed'
    assert record.levelno == logging.ERROR
    assert record.phase_a3_stage == 'extraction'
    assert record.phase_a3_error_type == 'RuntimeError'
    assert record.phase_a3_counts['documents_selected'] == 1
    assert record.phase_a3_counts['claims_saved'] == 0
    assert record.exc_info is None
    assert 'PRIVATE_PROVIDER_TOKEN' not in caplog.text
    with private_pg, private_pg.cursor() as cur:
        cur.execute('SELECT count(*) FROM hive_claims')
        assert cur.fetchone() == (0,)
