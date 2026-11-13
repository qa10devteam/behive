"""E2E finding 2026-10-23: operator seeds must ADD to automatic harvest, not replace it.

Mission hive_1792741904_a6ba24: no scouted source met score_relevance>=5 and
score_total>=15, so the legacy relaxed top-N fallback should have run. Because
11 seeds made the strict query non-empty, harvest loaded only the seeds and
253 scouted sources were skipped.
"""
import uuid

from behive.engine import harvest, prescout
from test_repair1022_support import live_db
import pytest

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness


def _load(mission):
    bee = harvest.HarvesterBee(mission)
    try:
        return bee._load_pending_sources()
    finally:
        bee.con.close()


def test_seeds_do_not_suppress_relaxed_fallback_for_unscored_sources(live_db):
    mission = 'seedfallback_' + uuid.uuid4().hex
    seed = 'https://operator.example.test/wms-offer'
    with live_db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hive_sources (id,mission_id,url,status,score_total,score_relevance) VALUES "
            "(1,%s,'https://auto.example.test/a','scouted',12,4),"
            "(2,%s,'https://auto.example.test/b','scouted',9,2),"
            "(3,%s,'https://auto.example.test/done','done',30,9)",
            (mission, mission, mission))
    live_db.conn.commit()
    prescout.BeeMasterGate(mission)._add_seed_urls([{'url': seed}])
    urls = [r['url'] for r in _load(mission)]
    assert urls[0] == seed, 'operator seeds are harvested first'
    assert urls[1:] == ['https://auto.example.test/a', 'https://auto.example.test/b'], \
        'relaxed fallback must still load automatic sources when none pass strict thresholds'


def test_seeds_plus_strict_passing_sources_do_not_pull_fallback(live_db):
    mission = 'seedstrict_' + uuid.uuid4().hex
    seed = 'https://operator.example.test/telemetry'
    with live_db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hive_sources (id,mission_id,url,status,score_total,score_relevance) VALUES "
            "(1,%s,'https://auto.example.test/strong','scouted',40,9),"
            "(2,%s,'https://auto.example.test/weak','scouted',3,1)",
            (mission, mission))
    live_db.conn.commit()
    prescout.BeeMasterGate(mission)._add_seed_urls([{'url': seed}])
    assert [r['url'] for r in _load(mission)] == [seed, 'https://auto.example.test/strong']


def test_no_seeds_keeps_legacy_fallback(live_db):
    mission = 'noseed_' + uuid.uuid4().hex
    with live_db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hive_sources (id,mission_id,url,status,score_total,score_relevance) VALUES "
            "(1,%s,'https://auto.example.test/x','scouted',5,1)", (mission,))
    live_db.conn.commit()
    assert [r['url'] for r in _load(mission)] == ['https://auto.example.test/x']
