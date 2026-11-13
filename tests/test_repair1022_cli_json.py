"""E2E finding 2026-10-23 (mission hive_1792741904_a6ba24): `behive research --json`
exited 0 but printed banner/phase lines before the JSON and reported
mission_id=null with 0 claims, because orchestrator.cmd_run returns None.
"""
import argparse
import asyncio
import json
import sys
import types

from behive import cli
from behive.client import BeHiveClient
from behive.models import ResearchResult
from test_repair1022_support import live_db
import pytest

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness


def test_local_research_reports_active_mission_when_cmd_run_returns_none(live_db, monkeypatch):
    monkeypatch.delenv('BEHIVE_API_URL', raising=False)
    with live_db.conn.cursor() as cur:
        cur.execute("""INSERT INTO hive_claims
            (id,mission_id,claim,confidence,quality_score,source_url,claim_type,is_garbage)
            VALUES (1,'hive_e2e_m','Firma Example oferuje system WMS Rewista.',0.9,0.9,
                    'https://seed.example.test/wms','source_extraction',false)""")
    live_db.conn.commit()

    def cmd_run(topic, **kwargs):
        cmd_run._active_mission_id = 'hive_e2e_m'
        return None

    monkeypatch.setitem(sys.modules, 'hive2', types.SimpleNamespace(cmd_run=cmd_run))
    result = asyncio.run(BeHiveClient(db_url=live_db.dsn)._research_local('WMS', 3, 30, True, 60))
    assert result.mission_id == 'hive_e2e_m'
    assert [c.text for c in result.claims] == ['Firma Example oferuje system WMS Rewista.']


def test_local_research_without_mission_id_is_error_not_empty_done(live_db, monkeypatch):
    monkeypatch.delenv('BEHIVE_API_URL', raising=False)

    def cmd_run(topic, **kwargs):
        return None

    monkeypatch.setitem(sys.modules, 'hive2', types.SimpleNamespace(cmd_run=cmd_run))
    result = asyncio.run(BeHiveClient(db_url=live_db.dsn)._research_local('WMS', 3, 30, True, 60))
    assert result.status == 'error'
    assert not result.mission_id


def test_cli_json_stdout_is_only_the_json_document(monkeypatch, capsys):
    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def research(self, topic, depth=3, scale=None):
            print('OUTPUT_LINES:30\nEXIT:0')  # what run_phase prints in-process
            return ResearchResult(topic=topic, mission_id='hive_x', status='done')

    import behive
    monkeypatch.setattr(behive, 'BeHiveClient', FakeClient)
    args = argparse.Namespace(topic='WMS', depth=3, scale=30, json=True, api=None, no_db=False)
    asyncio.run(cli.cmd_research(args))
    out, err = capsys.readouterr()
    assert json.loads(out)['mission_id'] == 'hive_x'
    assert 'Researching' in err and 'OUTPUT_LINES' in err
