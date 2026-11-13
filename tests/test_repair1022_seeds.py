"""A1: real PG admission/routing; only remote/phase dispatch is controlled."""
import asyncio
import json
from pathlib import Path
import subprocess
import time
import uuid

import pytest

from behive.engine import harvest, orchestrator, prescout
from test_repair1022_support import live_db

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness


class DispatchReached(BaseException):
    """Stop at a real I/O boundary without continuing into remote work."""


def test_beemaster_seeds_enter_harvest_queue_idempotently(live_db):
    mission = 'seeds_' + uuid.uuid4().hex
    url = 'https://operator.example.test/report?part=1&lang=en'
    gate = prescout.BeeMasterGate(mission)
    seeds = [{'url': url, 'title': 'Operator research'}, {'url': url}, {'url': ''}]
    gate._add_seed_urls(seeds)
    gate._add_seed_urls(seeds)
    with live_db.conn.cursor() as cur:
        cur.execute('SELECT id,url,status,source_type,harvest_method FROM hive_sources WHERE mission_id=%s', (mission,))
        rows = cur.fetchall()
        assert len(rows) == 1, 'Operator seeds must enter hive_sources, not just the routing map'
        assert rows[0] == (500, url, 'pending', 'operator_seed', 'bee_master_seed')
        cur.execute("SELECT to_regclass('hive_sources_seq')")
        assert cur.fetchone()[0] is None, 'Exercise the actual scout sequence, not the stale CREATE default'
    bee = harvest.HarvesterBee(mission)
    try:
        assert [r['url'] for r in bee._load_pending_sources()] == [url]
    finally:
        bee.con.close()


def test_existing_scout_source_is_promoted_without_duplicate_or_done_requeue(live_db):
    mission = 'existing_' + uuid.uuid4().hex
    url = 'https://operator.example.test/existing'
    done_url = 'https://operator.example.test/already-done'
    with live_db.conn.cursor() as cur:
        cur.execute("INSERT INTO hive_sources (id,mission_id,url,status,score_total,score_relevance) VALUES (1,%s,%s,'scouted',0,0),(2,%s,%s,'done',80,20),(3,'foreign',%s,'scouted',80,20)", (mission, url, mission, done_url, url))
    live_db.conn.commit()
    prescout.BeeMasterGate(mission)._add_seed_urls([{'url': url}, {'url': done_url}])
    bee = harvest.HarvesterBee(mission)
    try:
        assert [r['url'] for r in bee._load_pending_sources()] == [url], 'Explicit seeds bypass automatic relevance triage'
    finally:
        bee.con.close()
    with live_db.conn.cursor() as cur:
        cur.execute('SELECT id,status,source_type FROM hive_sources WHERE mission_id=%s ORDER BY id', (mission,))
        assert cur.fetchall() == [(1, 'scouted', 'operator_seed'), (2, 'done', 'operator_seed')]
        cur.execute("SELECT source_type FROM hive_sources WHERE mission_id='foreign'")
        assert cur.fetchone()[0] is None


def test_operator_seed_bypasses_prescout_skip_but_ordinary_source_does_not(live_db, monkeypatch):
    mission = 'skip_' + uuid.uuid4().hex
    seed = 'https://operator.example.test/skip-seed'
    ordinary = 'https://ordinary.example.test/skip'
    with live_db.conn.cursor() as cur:
        cur.execute("INSERT INTO hive_sources (id,mission_id,url,status,source_type,score_total,score_relevance) VALUES (1,%s,%s,'pending','operator_seed',80,20),(2,%s,%s,'pending','ddg',70,20)", (mission, seed, mission, ordinary))
    live_db.conn.commit()
    routing = Path(f'/tmp/hive_routing_{mission}.json')
    routing.write_text(json.dumps([{'url': u, 'routing_final': 'skip'} for u in (seed, ordinary)]))
    async def remote_fetch(self, url):
        assert url == seed
        raise DispatchReached()
    monkeypatch.setattr(harvest.HarvesterBee, '_harvest_web', remote_fetch)
    bee = harvest.HarvesterBee(mission)
    try:
        pending = {r['url']: r for r in bee._load_pending_sources()}
        with pytest.raises(DispatchReached):
            asyncio.run(bee._harvest_one(pending[seed]))
        assert asyncio.run(bee._harvest_one(pending[ordinary]))['method'] == 'prescout_skip'
    finally:
        bee.con.close()
        routing.unlink(missing_ok=True)


def test_seed_only_beemaster_gate_admits_sources_without_existing_map(live_db):
    mission = 'empty_gate_' + uuid.uuid4().hex
    url = 'https://operator.example.test/seed-only'
    approved = prescout.BeeMasterGate(mission).gate(seed_urls=[{'url': url}])
    assert [entry['url'] for entry in approved] == [url]
    bee = harvest.HarvesterBee(mission)
    try:
        assert [row['url'] for row in bee._load_pending_sources()] == [url]
    finally:
        bee.con.close()


def test_direct_prescout_admits_seed_before_transport_failure(live_db, monkeypatch):
    mission = 'direct_' + uuid.uuid4().hex
    url = 'https://operator.example.test/direct-seed'
    async def failing_heads(urls):
        raise RuntimeError('controlled HEAD transport failure')
    monkeypatch.setattr(prescout, 'head_sweep', failing_heads)
    with pytest.raises(RuntimeError, match='controlled HEAD transport failure'):
        asyncio.run(prescout.run_prescout_pipeline(mission, seed_urls=[{'url': url}]))
    bee = harvest.HarvesterBee(mission)
    try:
        assert [row['url'] for row in bee._load_pending_sources()] == [url]
    finally:
        bee.con.close()


@pytest.mark.parametrize('scout_sources', [0, 1])
def test_orchestrator_keeps_seeds_when_prescout_head_sweep_fails(live_db, monkeypatch, scout_sources):
    from behive.engine import drones
    mission = 'nonfatal_' + uuid.uuid4().hex
    seed = 'https://operator.example.test/nonfatal'
    if scout_sources:
        with live_db.conn.cursor() as cur:
            cur.execute("INSERT INTO hive_sources (id,mission_id,url,domain,status,score_total,score_relevance) VALUES (1,%s,'https://scout.example.test/report','scout.example.test','scouted',50,10)", (mission,))
        live_db.conn.commit()
    monkeypatch.setenv('HIVE_BM_SEEDS', json.dumps([{'url': seed, 'title': 'Operator seed'}]))
    monkeypatch.setenv('BEHIVE_SCOUT_RETRIES', '1')
    monkeypatch.delenv('HIVE_BM_MANUAL', raising=False)
    # External scout/API processes and domain probes are not executed.
    monkeypatch.setattr(orchestrator.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout='[]', stderr=''))
    monkeypatch.setattr(drones, 'run_recon', lambda *a, **k: {})
    head_calls = []
    async def failing_heads(urls):
        head_calls.extend(urls)
        raise RuntimeError('controlled HEAD transport failure')
    monkeypatch.setattr(prescout, 'head_sweep', failing_heads)
    queued = []
    def phase_dispatch(script, *a, **k):
        assert script == 'hive2_harvest.py'
        bee = harvest.HarvesterBee(mission)
        try:
            queued.extend(r['url'] for r in bee._load_pending_sources())
        finally:
            bee.con.close()
        raise DispatchReached()
    monkeypatch.setattr(orchestrator, 'run_phase', phase_dispatch)
    try:
        with pytest.raises(DispatchReached):
            orchestrator._run_pipeline_phases(mission, 'Operator research', [], {}, time.time(), False, False, 1, 'Operator research', None, 'unused-plan.json')
        assert seed in queued, 'PreScout is optional; operator source admission is not'
        assert seed in head_calls
    finally:
        Path(f'/tmp/hive_api_scout_{mission}.py').unlink(missing_ok=True)
        Path(f'/tmp/hive_routing_{mission}.json').unlink(missing_ok=True)
