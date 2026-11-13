"""Isolated real-PostgreSQL regressions; run with unittest, never tests/conftest.

PYTHONPATH=src /root/behive/.venv/bin/python -m unittest discover -s tests/repair -v
The harness creates its OWN socket-only cluster; it never uses a caller's DSN.
LLM/network boundaries are replaced, not the SQL engine or claim writers.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import psycopg2
import pytest

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness

_CLUSTER = None
_PREVIOUS_ENV = {}
_PREVIOUS_DB = None  # (behive.engine.db.DSN, behive.engine.db._pool) of the shared process
URL = "https://example.test/report"
CLAIM = "Acme revenue reached 42 million euros in 2025 according to the annual financial report."
CLAIMS = [CLAIM, "Acme employed 1200 engineers in 2025 according to the published annual financial report.",
          "Acme exports increased by 25 percent in 2025 according to the annual financial report."]


def _runuser():
    # /usr/sbin is not on every test PATH (the isolated main suite uses /usr/bin:/bin).
    return shutil.which("runuser") or "/usr/sbin/runuser"


def setUpModule():
    global _CLUSTER
    _CLUSTER = Path(tempfile.mkdtemp(prefix="behive-claims-test-", dir="/tmp"))
    bindir = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
    prefix = []
    if os.geteuid() == 0:
        import pwd
        user = pwd.getpwnam("postgres")
        os.chown(_CLUSTER, user.pw_uid, user.pw_gid)
        prefix = [_runuser(), "-u", "postgres", "--"]
    subprocess.run(prefix + [str(bindir / "initdb"), "-D", str(_CLUSTER / "data"), "-A", "trust", "-U", "postgres", "--no-locale"],
                   check=True, cwd="/tmp", stdout=subprocess.DEVNULL)
    subprocess.run(prefix + [str(bindir / "pg_ctl"), "-D", str(_CLUSTER / "data"), "-l", str(_CLUSTER / "postgres.log"),
                            "-o", f"-F -k {_CLUSTER} -p 55439 -c listen_addresses=''", "-w", "start"],
                   check=True, cwd="/tmp", stdout=subprocess.DEVNULL)
    # URI needed by engine.db; hostname is the private Unix socket, never TCP.
    from urllib.parse import quote
    dsn = f"postgresql://postgres@/postgres?host={quote(str(_CLUSTER), safe='')}&port=55439"
    for name, value in {"BEHIVE_DB_URL": dsn, "DATABASE_URL": dsn, "BEHIVE_BACKEND": "postgres"}.items():
        _PREVIOUS_ENV[name] = os.environ.get(name)
        os.environ[name] = value
    # behive.engine.db computes DSN at import. In a shared pytest process (PG harness:
    # all *_pg.py files + tests/repair) it was imported earlier with another DSN, so
    # the writers hit the wrong database (UndefinedTable). Bind it to this cluster
    # and restore the previous binding in tearDownModule.
    global _PREVIOUS_DB
    from behive.engine import db as engine_db
    _PREVIOUS_DB = (engine_db.DSN, engine_db._pool)
    engine_db.DSN = dsn
    engine_db._pool = None
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT current_database(), inet_server_addr()")
        assert cur.fetchone() == ("postgres", None), "not the isolated socket cluster"
        cur.execute("""
          CREATE SEQUENCE hive_facts_seq;
          CREATE TABLE hive_claims (
            id bigint NOT NULL PRIMARY KEY, mission_id text, claim text, evidence text,
            source_url text, claim_type text, confidence double precision, extracted_at timestamp,
            quality_score double precision, source_operation text, is_garbage boolean,
            evidence_flags text);
          CREATE TABLE hive_content (id bigint, mission_id text, url text, raw_text text, title text, word_count int);
          CREATE TABLE hive_sources (mission_id text, url text);
          CREATE TABLE hive_facts (id bigint PRIMARY KEY, mission_id text, value double precision,
            unit text, context text, sources jsonb, confidence text);
          CREATE TABLE hive_missions (id text PRIMARY KEY, topic text, config jsonb);
        """)
    conn.close()


def tearDownModule():
    if _CLUSTER is None:
        return
    from behive.engine import db
    if db._pool:
        db._pool.closeall()
        db._pool = None
    if _PREVIOUS_DB is not None:
        db.DSN, db._pool = _PREVIOUS_DB
    bindir = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
    prefix = [_runuser(), "-u", "postgres", "--"] if os.geteuid() == 0 else []
    subprocess.run(prefix + [str(bindir / "pg_ctl"), "-D", str(_CLUSTER / "data"), "-m", "fast", "-w", "stop"],
                   check=True, cwd="/tmp", stdout=subprocess.DEVNULL)
    shutil.rmtree(_CLUSTER)
    for key, old in _PREVIOUS_ENV.items():
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


class ClaimsRepairTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('Network disabled in repair tests')))
        self.enterContext(patch('behive.engine.llm.complete', side_effect=AssertionError('Live inference disabled in repair tests')))
        self.conn = psycopg2.connect(os.environ["BEHIVE_DB_URL"])
        self.addCleanup(self.conn.close)
        with self.conn, self.conn.cursor() as cur:
            cur.execute("TRUNCATE hive_claims, hive_content, hive_facts, hive_sources, hive_missions")
            cur.execute("ALTER SEQUENCE hive_facts_seq RESTART WITH 1000")
            cur.execute("INSERT INTO hive_missions (id,topic) VALUES ('repair', 'Acme revenue')")
            cur.execute("INSERT INTO hive_content VALUES (1, 'repair', %s, %s, 'Annual report 2025', 500)",
                        (URL, ' '.join(CLAIMS)))
            cur.execute("INSERT INTO hive_facts VALUES (nextval('hive_facts_seq'), 'repair', 42, 'million', %s, %s, 'HIGH')",
                        (CLAIM, json.dumps([URL])))

    def rows(self):
        with self.conn, self.conn.cursor() as cur:
            cur.execute("SELECT id, claim, evidence, source_url, claim_type, source_operation FROM hive_claims ORDER BY id")
            return cur.fetchall()

    def eligible_count(self):
        from behive.hygiene import SQL_COMPLETION_CLAIM
        with self.conn, self.conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM hive_claims c WHERE {SQL_COMPLETION_CLAIM}")
            return cur.fetchone()[0]

    def test_content_fallback_allocates_ids_without_column_default(self):
        from behive.engine.claims_fallback import salvage_claims_from_content
        self.assertEqual(salvage_claims_from_content('repair'), 3)
        self.assertEqual(salvage_claims_from_content('repair'), 0)
        rows = self.rows()
        self.assertTrue(all(r[0] > 1000 for r in rows))
        self.assertEqual(len({r[0] for r in rows}), 3)
        self.assertTrue(all(r[3:] == (URL, 'regex_fallback', 'claims_fallback') for r in rows))
        self.assertEqual(self.eligible_count(), 3)

    def test_facts_fallback_allocates_ids_without_column_default(self):
        from behive.engine.facts_to_claims import persist_facts_as_claims
        self.assertEqual(persist_facts_as_claims('repair'), 1)
        self.assertEqual(persist_facts_as_claims('repair'), 0)
        row, = self.rows()
        self.assertGreater(row[0], 1000)
        self.assertEqual(row[1:], (CLAIM, CLAIM, URL, 'fact_persist', 'facts_to_claims'))
        self.assertEqual(self.eligible_count(), 1)

    def test_cross_validation_query_uses_real_postgres(self):
        from behive.engine.db import PgConnection
        from behive.engine.process import ClaimCrossValidationDrone
        con = PgConnection()
        self.addCleanup(con.close)
        with patch('behive.engine.process.log.error') as errors:
            self.assertEqual(ClaimCrossValidationDrone().run(con, 'repair'), 0)
        errors.assert_not_called()

    def test_concurrent_fallback_writers_deduplicate_same_mission(self):
        from behive.engine.claims_fallback import salvage_claims_from_content
        from behive.engine.facts_to_claims import persist_facts_as_claims
        import threading
        barrier = threading.Barrier(12)
        def write(i):
            barrier.wait(timeout=10)
            if i % 2:
                return persist_facts_as_claims('repair')
            return salvage_claims_from_content('repair')
        with ThreadPoolExecutor(max_workers=12) as executor:
            counts = list(executor.map(write, range(12)))
        rows = self.rows()
        self.assertEqual(sum(counts), 3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r[1] for r in rows}), 3)
        self.assertEqual(len({r[0] for r in rows}), 3)
        self.assertEqual(self.eligible_count(), 3)

    def test_ids_remain_unique_across_concurrent_missions_and_facts(self):
        from behive.engine.facts_to_claims import persist_facts_as_claims
        from behive.engine.claims_fallback import salvage_claims_from_content
        with self.conn, self.conn.cursor() as cur:
            for i in range(8):
                mission = f'repair_{i}'
                # Salvage judges relevance against the mission topic (2026-10-23).
                cur.execute("INSERT INTO hive_missions (id,topic) VALUES (%s, 'Acme revenue')", (mission,))
                cur.execute("INSERT INTO hive_content VALUES (%s, %s, %s, %s, 'Annual report', 500)",
                            (i + 2, mission, URL, ' '.join(CLAIMS)))
                cur.execute("INSERT INTO hive_facts VALUES (nextval('hive_facts_seq'), %s, 42, 'million', %s, %s, 'HIGH')",
                            (mission, CLAIM, json.dumps([URL])))
        def write(i):
            mission = f'repair_{i}'
            return persist_facts_as_claims(mission, 'Acme') + salvage_claims_from_content(mission)
        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(sum(executor.map(write, range(8))), 24)
        rows = self.rows()
        self.assertEqual(len(rows), 24)
        self.assertEqual(len({row[0] for row in rows}), 24)
        with self.conn, self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM hive_claims c JOIN hive_facts f ON c.id = f.id")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_source_schema_is_not_serial_or_identity(self):
        with self.conn, self.conn.cursor() as cur:
            cur.execute("SELECT is_nullable, column_default, is_identity FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='hive_claims' AND column_name='id'")
            self.assertEqual(cur.fetchone(), ('NO', None, 'NO'))
            cur.execute("SELECT pg_get_serial_sequence('hive_claims', 'id')")
            self.assertEqual(cur.fetchone(), (None,))

    def test_finalize_does_not_hide_facts_persistence_error(self):
        from behive.engine.process import ProcessingDrones
        drones = object.__new__(ProcessingDrones)
        drones.mission_id = 'repair'
        # Reject a real INSERT rather than replacing the writer with a mock.
        with self.conn, self.conn.cursor() as cur:
            cur.execute("ALTER SEQUENCE hive_facts_seq RESTART WITH 1000")
            cur.execute("INSERT INTO hive_claims (id, mission_id) VALUES (1000, 'different')")
        from behive.engine.db import PgConnection
        drones.con = PgConnection()
        self.addCleanup(drones.con.close)
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            drones._finalize(1, 0, 0, 0, 0)

    def test_fallback_insert_error_rolls_back_all_claims(self):
        from behive.engine.claims_fallback import salvage_claims_from_content
        with self.conn, self.conn.cursor() as cur:
            cur.execute("ALTER TABLE hive_claims ADD CHECK (claim NOT LIKE '%1200%')")
        try:
            with self.assertRaises(psycopg2.errors.CheckViolation):
                salvage_claims_from_content('repair')
            self.assertEqual(self.rows(), [])
        finally:
            with self.conn, self.conn.cursor() as cur:
                cur.execute("ALTER TABLE hive_claims DROP CONSTRAINT hive_claims_claim_check")

    def test_facts_hygiene_and_topic_filter_stay_strict(self):
        from behive.engine.facts_to_claims import persist_facts_as_claims
        with self.conn, self.conn.cursor() as cur:
            for context in ['{\"revenue\": 42}', 'Revenue padding-top: 42; body { css }',
                            'Acme revenue is substantial according to its annual financial report.',
                            'Othercompany employs 500 people according to its annual financial report.']:
                cur.execute("INSERT INTO hive_facts VALUES (nextval('hive_facts_seq'), 'repair', 42, '', %s, %s, 'HIGH')",
                            (context, json.dumps([URL])))
        self.assertEqual(persist_facts_as_claims('repair'), 1)
        self.assertEqual(self.rows()[0][1], CLAIM)

    def source_drones(self):
        from behive.engine.process import ProcessingDrones
        drones = object.__new__(ProcessingDrones)
        drones.mission_id = 'repair'
        return drones

    def test_all_writers_preserve_serial_custom_default_and_identity(self):
        from behive.engine.claims_fallback import salvage_claims_from_content
        from behive.engine.facts_to_claims import persist_facts_as_claims
        response = json.dumps([{'claim': CLAIM, 'evidence': CLAIM, 'confidence': 0.9}])
        with self.conn, self.conn.cursor() as cur:
            cur.execute('CREATE SEQUENCE repair_claim_ids START 5000')
        try:
            for mode in ('serial', 'custom_default', 'identity_always', 'identity_by_default'):
                with self.conn, self.conn.cursor() as cur:
                    if mode.startswith('identity'):
                        generation = 'ALWAYS' if mode == 'identity_always' else 'BY DEFAULT'
                        cur.execute(f'ALTER TABLE hive_claims ALTER COLUMN id ADD GENERATED {generation} AS IDENTITY (START WITH 5000)')
                    else:
                        cur.execute("ALTER TABLE hive_claims ALTER COLUMN id SET DEFAULT nextval('repair_claim_ids')")
                        if mode == 'serial':
                            cur.execute('ALTER SEQUENCE repair_claim_ids OWNED BY hive_claims.id')
                try:
                    for writer in ('facts', 'content', 'source'):
                        with self.subTest(mode=mode, writer=writer):
                            with self.conn, self.conn.cursor() as cur:
                                cur.execute('TRUNCATE hive_claims')
                                # Each allocator mode is an independent extraction, not a later mission round.
                                cur.execute("UPDATE hive_missions SET config=NULL WHERE id='repair'")
                                cur.execute("SELECT last_value, is_called FROM hive_facts_seq")
                                shared_before = cur.fetchone()
                            if writer == 'facts':
                                count = persist_facts_as_claims('repair')
                            elif writer == 'content':
                                count = salvage_claims_from_content('repair')
                            else:
                                with patch('behive.engine.llm.complete', return_value=response):
                                    count = asyncio.run(self.source_drones()._extract_source_claims(
                                        [{'url': URL, 'raw_text': CLAIM}], 'Acme'))
                            self.assertGreater(count, 0)
                            self.assertTrue(all(row[0] >= 5000 for row in self.rows()))
                            with self.conn, self.conn.cursor() as cur:
                                cur.execute("SELECT last_value, is_called FROM hive_facts_seq")
                                self.assertEqual(cur.fetchone(), shared_before)
                finally:
                    with self.conn, self.conn.cursor() as cur:
                        if mode.startswith('identity'):
                            cur.execute('ALTER TABLE hive_claims ALTER COLUMN id DROP IDENTITY')
                        else:
                            cur.execute('ALTER TABLE hive_claims ALTER COLUMN id DROP DEFAULT')
                            cur.execute('ALTER SEQUENCE repair_claim_ids OWNED BY NONE')
        finally:
            with self.conn, self.conn.cursor() as cur:
                cur.execute('DROP SEQUENCE repair_claim_ids')

    def test_id_helper_resolves_actual_search_path_relation_without_cache(self):
        from behive.engine.claim_ids import claim_id_expression
        with self.conn, self.conn.cursor() as cur:
            cur.execute('CREATE SCHEMA repair_shadow')
            cur.execute('CREATE TABLE repair_shadow.hive_claims (id bigint NOT NULL PRIMARY KEY)')
            cur.execute('CREATE SEQUENCE repair_shadow.hive_facts_seq START 7000')
        try:
            # Opposite layouts in shadow/public, then switch them. An unqualified
            # information_schema lookup or global cache would pick the wrong one.
            for shadow_has_default in (False, True):
                with self.conn, self.conn.cursor() as cur:
                    if shadow_has_default:
                        cur.execute('ALTER TABLE public.hive_claims ALTER COLUMN id DROP DEFAULT')
                        cur.execute('ALTER TABLE repair_shadow.hive_claims ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (START 8000)')
                    else:
                        cur.execute("ALTER TABLE public.hive_claims ALTER COLUMN id SET DEFAULT nextval('public.hive_facts_seq')")
                    cur.execute('SET LOCAL search_path = repair_shadow, public')
                    expected = 'DEFAULT' if shadow_has_default else "nextval('hive_facts_seq')"
                    self.assertEqual(claim_id_expression(cur), expected)
                    cur.execute(f'INSERT INTO hive_claims (id) VALUES ({claim_id_expression(cur)}) RETURNING id')
                    row = cur.fetchone()
                    self.assertIsNotNone(row)
                    assert row is not None
                    self.assertEqual(row[0], 8000 if shadow_has_default else 7000)
                    cur.execute('SET LOCAL search_path = public')
                    expected_public = "nextval('hive_facts_seq')" if shadow_has_default else 'DEFAULT'
                    self.assertEqual(claim_id_expression(cur), expected_public)
        finally:
            with self.conn, self.conn.cursor() as cur:
                cur.execute('ALTER TABLE public.hive_claims ALTER COLUMN id DROP DEFAULT')
                cur.execute('DROP SCHEMA repair_shadow CASCADE')

    def test_source_default_works_when_shared_sequence_is_absent(self):
        response = json.dumps([{'claim': CLAIM, 'evidence': CLAIM, 'confidence': 0.9}])
        with self.conn, self.conn.cursor() as cur:
            cur.execute('DROP SEQUENCE hive_facts_seq')
            cur.execute('ALTER TABLE hive_claims ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (START 9000)')
        try:
            with patch('behive.engine.llm.complete', return_value=response):
                self.assertEqual(asyncio.run(self.source_drones()._extract_source_claims(
                    [{'url': URL, 'raw_text': CLAIM}], 'Acme')), 1)
            self.assertEqual(self.rows()[0][0], 9000)
        finally:
            with self.conn, self.conn.cursor() as cur:
                cur.execute('ALTER TABLE hive_claims ALTER COLUMN id DROP IDENTITY')
                cur.execute('CREATE SEQUENCE hive_facts_seq START 1000')

    def test_concurrent_source_and_fallback_writers_deduplicate(self):
        from behive.engine.claims_fallback import salvage_claims_from_content
        from behive.engine.facts_to_claims import persist_facts_as_claims
        import threading
        barrier = threading.Barrier(12)
        response = json.dumps([{'claim': c, 'evidence': c, 'confidence': 0.9} for c in CLAIMS])
        def write(i):
            barrier.wait(timeout=10)
            if i % 3 == 0:
                return persist_facts_as_claims('repair')
            if i % 3 == 1:
                return salvage_claims_from_content('repair')
            return asyncio.run(self.source_drones()._extract_source_claims(
                [{'url': URL, 'raw_text': ' '.join(CLAIMS)}], 'Acme'))
        with patch('behive.engine.llm.complete', return_value=response):
            with ThreadPoolExecutor(max_workers=12) as executor:
                self.assertEqual(sum(executor.map(write, range(12))), 3)
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r[0] for r in rows}), 3)
        self.assertEqual(len({r[1] for r in rows}), 3)
        self.assertEqual(self.eligible_count(), 3)

    def test_source_extraction_releases_connection_before_network(self):
        from behive.engine.db import get_pg_connection
        opened = []
        def connect():
            conn = get_pg_connection()
            opened.append(conn)
            return conn
        def complete(**kwargs):
            self.assertTrue(opened)
            self.assertTrue(all(conn.closed for conn in opened), 'PG connection held across LLM await')
            return json.dumps([{'claim': CLAIM, 'evidence': CLAIM, 'confidence': 0.9}])
        with patch('behive.engine.db.get_pg_connection', side_effect=connect), \
             patch('behive.engine.llm.complete', side_effect=complete):
            self.assertEqual(asyncio.run(self.source_drones()._extract_source_claims(
                [{'url': URL, 'raw_text': CLAIM}], 'Acme')), 1)
        self.assertTrue(all(conn.closed for conn in opened))

    def test_source_extraction_revalidates_provenance_after_network(self):
        def complete(**kwargs):
            # Simulate harvested provenance being removed while the request is in flight.
            with psycopg2.connect(os.environ['BEHIVE_DB_URL']) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM hive_content WHERE mission_id = 'repair'")
            return json.dumps([{'claim': CLAIM, 'evidence': CLAIM, 'confidence': 0.9}])
        with patch('behive.engine.llm.complete', side_effect=complete):
            with self.assertRaisesRegex(RuntimeError, 'ValueError'):
                asyncio.run(self.source_drones()._extract_source_claims(
                    [{'url': URL, 'raw_text': CLAIM}], 'Acme'))
        self.assertEqual(self.rows(), [])

    def test_source_errors_do_not_surface_provider_secrets(self):
        import traceback
        secret = 'api_key=DO_NOT_SURFACE_THIS_SECRET'
        with patch('behive.engine.llm.complete', side_effect=RuntimeError(secret)):
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(self.source_drones()._extract_source_claims(
                    [{'url': URL, 'raw_text': CLAIM}], 'Acme'))
        self.assertIn('RuntimeError', str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, ''.join(traceback.format_exception(caught.exception)))
        self.assertEqual(self.rows(), [])

    def test_source_extraction_invalid_json_raises_without_claims(self):
        drones = self.source_drones()
        with patch('behive.engine.llm.complete', return_value='truncated [{'):
            with self.assertRaisesRegex(RuntimeError, 'ValueError'):
                asyncio.run(drones._extract_source_claims([{'url': URL, 'raw_text': CLAIM}], 'Acme'))
        self.assertEqual(self.rows(), [])

    def test_source_extraction_provider_error_propagates(self):
        drones = self.source_drones()
        with patch('behive.engine.llm.complete', side_effect=RuntimeError('provider unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'RuntimeError'):
                asyncio.run(drones._extract_source_claims([{'url': URL, 'raw_text': CLAIM}], 'Acme'))
        self.assertEqual(self.rows(), [])

    def test_source_extraction_rejects_fabricated_or_dirty_output(self):
        drones = self.source_drones()
        output = [{'claim': CLAIM, 'evidence': CLAIM, 'confidence': 0.9},
                  {'claim': CLAIM.replace('42', '99'), 'evidence': CLAIM, 'confidence': 1},
                  {'claim': CLAIM, 'evidence': 'invented supporting quote', 'confidence': 0.9},
                  {'claim': '{\"revenue\": 42}', 'evidence': CLAIM, 'confidence': 0.9},
                  {'claim': CLAIM, 'evidence': CLAIM, 'confidence': float('nan')}]
        with patch('behive.engine.llm.complete', return_value=json.dumps(output)):
            self.assertEqual(asyncio.run(drones._extract_source_claims([{'url': URL, 'raw_text': CLAIM}], 'Acme')), 1)
        self.assertEqual(self.rows()[0][1:4], (CLAIM, CLAIM, URL))
        self.assertEqual(self.rows()[0][4:], ('source_extraction', 'byok_source_extraction'))

    def test_source_extraction_requires_mission_provenance_before_model(self):
        drones = self.source_drones()
        with patch('behive.engine.llm.complete') as complete:
            self.assertEqual(asyncio.run(drones._extract_source_claims([
                {'url': 'https://unharvested.test', 'raw_text': CLAIM}], 'Acme')), 0)
        complete.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_source_extraction_deduplicates_against_fallback(self):
        from behive.engine.facts_to_claims import persist_facts_as_claims
        persist_facts_as_claims('repair')
        drones = self.source_drones()
        with patch('behive.engine.llm.complete', return_value=json.dumps([
                {'claim': CLAIM, 'evidence': CLAIM, 'confidence': 0.9}])):
            self.assertEqual(asyncio.run(drones._extract_source_claims([{'url': URL, 'raw_text': CLAIM}], 'Acme')), 0)
        self.assertEqual(len(self.rows()), 1)

    def test_source_extraction_insert_failure_rolls_back_only_current_document(self):
        drones = self.source_drones()
        second_url = URL + '/second'
        with self.conn, self.conn.cursor() as cur:
            cur.execute("ALTER TABLE hive_claims ADD CHECK (claim NOT LIKE '%1200%')")
            cur.execute("INSERT INTO hive_content (id,mission_id,url,raw_text) VALUES (2,'repair',%s,%s)",
                        (second_url, ' '.join(CLAIMS[1:])))
        def complete(prompt, **kwargs):
            # Doc 2 inserts one valid row before its next row fails. Doc 1 must
            # survive, but neither doc 2 row nor its attempted marker can commit.
            selected = [CLAIM] if CLAIM in prompt else [CLAIMS[2], CLAIMS[1]]
            return json.dumps([{'claim': c, 'evidence': c, 'confidence': 0.9} for c in selected])
        try:
            with patch('behive.engine.llm.complete', side_effect=complete):
                with self.assertRaisesRegex(RuntimeError, 'CheckViolation'):
                    asyncio.run(drones._extract_source_claims([
                        {'url': URL, 'raw_text': CLAIM},
                        {'url': second_url, 'raw_text': ' '.join(CLAIMS[1:])}], 'Acme'))
            self.assertEqual([row[1] for row in self.rows()], [CLAIM])
            with self.conn, self.conn.cursor() as cur:
                cur.execute("SELECT config FROM hive_missions WHERE id='repair'")
                self.assertEqual(cur.fetchone()[0]['source_extraction']['attempted_urls'], [URL])
        finally:
            with self.conn, self.conn.cursor() as cur:
                cur.execute("ALTER TABLE hive_claims DROP CONSTRAINT hive_claims_claim_check")

    def test_source_extraction_has_bounded_sequential_calls(self):
        from behive.engine.source_extraction import extract_source_claims, MAX_DOCUMENTS, MAX_SOURCE_CHARS
        from unittest.mock import Mock
        complete = Mock(return_value='[]')
        doc = {'url': URL, 'raw_text': CLAIM + ' x' * 6000}
        with patch('behive.engine.llm.complete', complete):
            self.assertEqual(asyncio.run(extract_source_claims([doc] * (MAX_DOCUMENTS + 1), 'Acme')), [])
        self.assertEqual(complete.call_count, MAX_DOCUMENTS)
        for call in complete.call_args_list:
            self.assertEqual(call.kwargs['stage'], 'process')
            self.assertEqual(call.kwargs['max_tokens'], 1024)
            self.assertLessEqual(len(call.kwargs['prompt'].split('SOURCE:\n')[1]), MAX_SOURCE_CHARS)
            self.assertNotIn('model', call.kwargs)

    def test_phase_a3_extracts_grounded_claims_without_legacy_modules(self):
        from behive.engine import process
        from behive.engine.db import PgConnection
        con = PgConnection()
        self.addCleanup(con.close)
        drones = object.__new__(process.ProcessingDrones)
        drones.con, drones.mission_id, drones.debug, drones.max_ops = con, 'repair', False, 1
        drones.relevance_threshold = 0
        doc = {'source_id': '1', 'url': URL, 'raw_text': ' '.join(CLAIMS), 'title': 'Acme revenue'}
        response = json.dumps([{'claim': c, 'evidence': c, 'confidence': 0.9} for c in CLAIMS])
        with patch.object(drones, '_load_content', return_value=[doc]), \
             patch.object(drones, '_get_mission_topic', return_value='Acme revenue'), \
             patch.object(drones, '_fallback_extract', new=AsyncMock(return_value={})), \
             patch.object(drones, '_finalize'), patch.object(drones, '_print_summary'), \
             patch.object(process.RelevanceFilter, 'filter', new=AsyncMock(return_value=[doc])), \
             patch.object(process.OperationRouter, '_load_feedback_fitness', return_value={}), \
             patch.object(process.DeduplicationDrone, 'run', return_value=0), \
             patch.object(process.FactValidationDrone, 'run', return_value=0), \
             patch.object(process.ClaimCrossValidationDrone, 'run', return_value=0), \
             patch.object(process.GapDrone, 'run', return_value=[]), \
             patch('hive_api_scout.scout_apis', new=AsyncMock(return_value=[])), \
             patch('behive.engine.llm.complete', return_value=response) as complete:
            asyncio.run(drones._run_async())
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(self.eligible_count(), 3)
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(complete.call_args.kwargs['stage'], 'process')
        self.assertTrue(all(r[2] == r[1] and r[3] == URL for r in self.rows()))


if __name__ == '__main__':
    unittest.main()
