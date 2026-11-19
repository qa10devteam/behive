"""POST /research stores the efforts in hive_missions.config; the engine reads them back.

Private socket PostgreSQL, live-shaped DDL (tests/test_repair1022_support.py), no DDL of
ours: the efforts ride in the existing config jsonb next to depth and scale. The pipeline
itself is not started (the background task is replaced), nothing leaves the process.
"""
import json

import psycopg2
import pytest

from test_repair1022_support import live_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.pg_harness


@pytest.fixture
def api(live_db, monkeypatch):
    from fastapi.testclient import TestClient
    from behive import server

    started = []

    async def fake_pipeline(mission_id, topic, depth, scale=30):
        started.append((mission_id, topic, depth, scale))

    monkeypatch.delenv("BEHIVE_API_KEY", raising=False)
    monkeypatch.setattr(server, "get_db", lambda: psycopg2.connect(live_db.dsn))
    monkeypatch.setattr(server, "_run_pipeline", fake_pipeline)
    server._research_limiter.buckets.clear()
    server._rate_limiter.buckets.clear()
    yield TestClient(server.app, raise_server_exceptions=False), started, live_db
    server._research_limiter.buckets.clear()
    server._rate_limiter.buckets.clear()


def _config(live_db, mission_id):
    live_db.conn.rollback()
    with live_db.conn.cursor() as cur:
        cur.execute("SELECT config FROM hive_missions WHERE id = %s", (mission_id,))
        row = cur.fetchone()
    live_db.conn.rollback()
    assert row is not None
    config = row[0]
    return json.loads(config) if isinstance(config, str) else config


def _count(live_db):
    live_db.conn.rollback()
    with live_db.conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM hive_missions")
        n = cur.fetchone()[0]
    live_db.conn.rollback()
    return n


def test_both_efforts_are_stored_next_to_depth_and_scale(api):
    client, started, live_db = api
    response = client.post("/research", json={
        "query": "effort config", "depth": 2, "scale": 40,
        "reasoning_effort": "medium", "synthesis_effort": "high"})
    assert response.status_code == 200, response.text
    mission_id = response.json()["job_id"]
    assert _config(live_db, mission_id) == {
        "depth": 2, "scale": 40, "reasoning_effort": "medium", "synthesis_effort": "high"}
    assert [s[0] for s in started] == [mission_id]


def test_only_the_given_effort_is_stored(api):
    client, _, live_db = api
    only_mission = client.post("/research", json={"query": "mission only", "reasoning_effort": "high"}).json()["job_id"]
    assert _config(live_db, only_mission) == {"depth": 3, "scale": 30, "reasoning_effort": "high"}
    only_synth = client.post("/research", json={"query": "synth only", "synthesis_effort": "low"}).json()["job_id"]
    assert _config(live_db, only_synth) == {"depth": 3, "scale": 30, "synthesis_effort": "low"}


def test_without_the_fields_the_config_is_what_it_was(api):
    client, _, live_db = api
    mission_id = client.post("/research", json={"query": "default path", "depth": 4, "scale": 25}).json()["job_id"]
    assert _config(live_db, mission_id) == {"depth": 4, "scale": 25}
    nulls = client.post("/research", json={
        "query": "explicit nulls", "reasoning_effort": None, "synthesis_effort": None}).json()["job_id"]
    assert _config(live_db, nulls) == {"depth": 3, "scale": 30}


def test_a_bad_value_is_a_422_and_creates_no_mission(api):
    client, started, live_db = api
    before = _count(live_db)
    for body in ({"reasoning_effort": "xhigh"}, {"synthesis_effort": "LOW"}, {"reasoning_effort": ""}):
        response = client.post("/research", json={"query": "bad effort", **body})
        assert response.status_code == 422, response.text
    assert _count(live_db) == before
    assert started == []


def test_the_engine_reads_the_efforts_back_from_the_database(api):
    from behive.engine import orchestrator

    client, _, live_db = api
    both = client.post("/research", json={
        "query": "read back", "reasoning_effort": "high", "synthesis_effort": "low"}).json()["job_id"]
    plain = client.post("/research", json={"query": "read back plain"}).json()["job_id"]
    assert orchestrator._mission_efforts(both) == ("high", "low")
    assert orchestrator._mission_efforts(plain) == (None, None)
    assert orchestrator._mission_efforts("hive_1_000000") == (None, None)
    # A config that something else wrote (a bad value) never becomes an effort.
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute("UPDATE hive_missions SET config = %s::jsonb WHERE id = %s",
                    (json.dumps({"reasoning_effort": "xhigh", "synthesis_effort": ["low"]}), both))
    assert orchestrator._mission_efforts(both) == (None, None)
