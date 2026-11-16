"""hive_content ids: API Scout and harvest share one allocator (diag follow-up 2026-11-16).

Before: API Scout inserted hive_content rows with id = MAX(id)+1 while harvest uses
nextval('hive_content_seq'). Once API Scout ran ahead of the sequence, later harvest
inserts hit taken ids (unique violation) and their documents were dropped (harvest logs
the error at debug level only). Production PostgreSQL log, 12.11: 8 duplicate-key errors
on hive_content_pkey in two harvest runs.

Private socket PostgreSQL (CLAIM_TEST_PG_DSN), live-shaped DDL (hive_content.id without a
default, hive_content_seq a separate sequence, as in production). Real orchestrator and
HarvesterBee writers; HarvesterBee.__init__ (network/routing setup) is skipped.
"""
import inspect
import re

import pytest

from test_repair1022_support import live_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.pg_harness

MISSION = "diag1112-content-ids"


def _api_docs(n, tag):
    return [{"url": f"https://api.example.test/{tag}/{i}", "title": f"API doc {i}",
             "raw_text": f"Structured record {i} for solar capacity.", "source": "Eurostat"} for i in range(n)]


def _harvest_results(n, tag):
    return [{"success": True, "text": f"Harvested page {i} about solar capacity in Poland.",
             "url": f"https://web.example.test/{tag}/{i}", "domain": "web.example.test", "word_count": 9,
             "language": "en", "quality_score": 0.5, "method": "httpx", "source_id": 500 + i} for i in range(n)]


def store_api_docs(docs):
    """The production API Scout writer; on a tree without the helper, its exact former SQL."""
    from behive.engine import orchestrator
    helper = getattr(orchestrator, "_store_api_scout_docs", None)
    if helper is not None:
        return helper(MISSION, docs)
    source = inspect.getsource(orchestrator._run_pipeline_phases)
    statement = re.search(r'"""(INSERT INTO hive_content \(id, mission_id, url, title, raw_text, '
                          r'harvest_method, domain\).*?)"""', source, re.S).group(1)
    con = orchestrator._connect_db()
    stored = 0
    try:
        for doc in docs:
            con.execute(statement, [MISSION, doc["url"], doc["title"], doc["raw_text"], doc["source"]])
            stored += 1
    finally:
        con.close()
    return stored


def harvest_flush(results):
    from behive.engine import harvest
    bee = harvest.HarvesterBee.__new__(harvest.HarvesterBee)
    bee.mission_id = MISSION
    bee._checkpointed = set()
    bee._flush_checkpoint(results)


def rows(live_db):
    live_db.conn.rollback()
    with live_db.conn.cursor() as cur:
        cur.execute("SELECT id, url, harvest_method FROM hive_content WHERE mission_id = %s ORDER BY id", (MISSION,))
        found = cur.fetchall()
    live_db.conn.rollback()
    return found


@pytest.fixture
def content_seq(live_db):
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute("CREATE SEQUENCE hive_content_seq START 1")
    return live_db


def test_api_scout_before_harvest_never_steals_harvest_ids(content_seq):
    assert store_api_docs(_api_docs(3, "a")) == 3
    harvest_flush(_harvest_results(4, "h"))
    found = rows(content_seq)
    assert len(found) == 7, found  # before the fix: harvest lost 3 of its 4 documents
    assert len({row[0] for row in found}) == 7
    assert sum(1 for row in found if row[2] == "httpx") == 4


def test_interleaved_writers_keep_every_document(content_seq):
    for round_ in range(3):
        store_api_docs(_api_docs(2, f"a{round_}"))
        harvest_flush(_harvest_results(3, f"h{round_}"))
    found = rows(content_seq)
    assert len(found) == 15 and len({row[0] for row in found}) == 15
    with content_seq.conn.cursor() as cur:
        cur.execute("SELECT last_value FROM hive_content_seq")
        assert cur.fetchone()[0] >= max(row[0] for row in found)  # the sequence owns every id
    content_seq.conn.rollback()
