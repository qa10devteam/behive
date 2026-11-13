"""Acceptance for diag task 1 on a private PostgreSQL: a process phase that times out
never yields done, and the API and MCP say why.

Real: run_phase with a real child process that outlives BEHIVE_PHASE_TIMEOUT,
_run_process_phase, cmd_run, _mark_mission_error, claims salvage, the API finalizer
(server._run_pipeline_inner), GET /research/{id}/status and /report (FastAPI
TestClient) and the MCP tools mission_status / get_report — all on canonical DDL
from init-db.sql in a UUID schema of the private socket-only cluster.
Controlled: planner and query preprocessing (no model), scout/harvest (rows inserted
directly), the API's worker spawn (runs cmd_run in-process), the SR bridge.
"""
import asyncio
import os
import re
import sys
import types
from pathlib import Path

import pytest

from test_claim_persistence_pg import db  # noqa: F401  (private-cluster fixture, skips without DSN)

pytestmark = pytest.mark.pg_harness

TOPIC = "Solar capacity"  # topic of mission 'm' created by the db fixture
URL = "https://example.test/solar/{}"


def _canonical(db, *tables):
    ddl = (Path(__file__).parents[1] / "src/behive/init-db.sql").read_text()
    with db.cursor() as cur:
        cur.execute("CREATE SEQUENCE IF NOT EXISTS hive_events_seq")
        for table in tables:
            match = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", ddl, re.S)
            assert match, table
            cur.execute(match.group())
    db.commit()


@pytest.fixture
def mission(db, monkeypatch):
    from behive.engine import db as engine_db
    from behive.engine import orchestrator
    from behive import server

    _canonical(db, "hive_sources", "hive_events")
    dsn = os.environ["BEHIVE_DB_URL"]  # the db fixture's UUID-schema DSN
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setattr(engine_db, "DSN", dsn)
    monkeypatch.setattr(engine_db, "_pool", None)
    with db.cursor() as cur:
        cur.execute("UPDATE hive_missions SET status='queued', phase='queued' WHERE id='m'")
        for i in range(3):
            text = " ".join(
                f"Solar capacity in region {i} reached {100 + j} megawatts during the documented "
                f"annual reporting period of 2024." for j in range(4))
            cur.execute("INSERT INTO hive_content (mission_id, url, raw_text, title, word_count) "
                        "VALUES ('m', %s, %s, 'Solar capacity report', 120)", (URL.format(i), text))
        cur.execute("INSERT INTO hive_sources (mission_id, url) VALUES ('m', %s)", (URL.format(0),))
    db.commit()

    # cmd_run without a model: fixed refined topic, empty plan, no primer.
    monkeypatch.setenv("BEHIVE_MISSION_ID", "m")
    monkeypatch.setenv("BEHIVE_MAX_RETRIES", "2")  # a phase failure must still run once only
    monkeypatch.setitem(sys.modules, "behive.engine.preprocessor", types.SimpleNamespace(
        preprocess_query=lambda *a, **k: types.SimpleNamespace(ok=True, refined_topic=TOPIC, specificity_score=0.8)))
    monkeypatch.setitem(sys.modules, "behive.engine.queen_primer", types.SimpleNamespace(
        QueenPrimer=lambda mid: types.SimpleNamespace(load_for_topic=lambda *a, **k: None)))
    monkeypatch.setattr(orchestrator, "QueenPlanner", lambda *a, **k: types.SimpleNamespace(plan=lambda: []))
    server._mission_events.pop("m", None)
    yield db
    if engine_db._pool is not None:
        engine_db._pool.closeall()
        engine_db._pool = None


def _run_api(monkeypatch, process_child, after_process=None):
    """Real API finalizer; its worker spawn runs the real cmd_run in-process."""
    from behive import server
    from behive.engine import orchestrator
    from test_mission_completion_lifecycle import EmptyOutput

    real_resolve = orchestrator._resolve_cmd
    monkeypatch.setattr(orchestrator, "_resolve_cmd", lambda script: (
        [sys.executable, "-c", process_child] if script == "hive2_process.py" else real_resolve(script)))
    attempts = []

    def pipeline(mission_id, topic, plan, timings, *args, **kwargs):
        attempts.append(mission_id)
        orchestrator._run_process_phase(mission_id, False, timings)  # real run_phase + child
        if after_process:
            after_process()

    monkeypatch.setattr(orchestrator, "_run_pipeline_phases", pipeline)
    bridges = []
    monkeypatch.setattr(server, "_bridge_done_mission", lambda mid: bridges.append(mid))

    async def spawn(*args, **kwargs):
        assert kwargs["env"]["BEHIVE_MISSION_ID"] == "m"
        box = {}

        def worker():
            try:
                orchestrator.cmd_run(TOPIC)
                box["rc"] = 0
            except SystemExit as exc:
                box["rc"] = exc.code if isinstance(exc.code, int) else 1
        await asyncio.to_thread(worker)

        class Proc:
            stdout = EmptyOutput()
            returncode = box["rc"]

            async def wait(self):
                return self.returncode
        return Proc()

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", spawn)
    asyncio.run(server._run_pipeline_inner("m", TOPIC, 1))
    return attempts, bridges


def _http(path):
    from fastapi.testclient import TestClient
    from behive import server
    return TestClient(server.app).get(path)


def _mcp(tool, arguments):
    from behive import mcp_server
    response = asyncio.run(mcp_server.mcp_endpoint(mcp_server.MCPRequest(
        id=7, method="tools/call", params={"name": tool, "arguments": arguments})))
    return response["result"]["content"][0]["text"], response["result"]["isError"]


def _row(db, sql, params=()):
    db.rollback()
    with db.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def test_process_timeout_ends_as_error_and_api_mcp_show_the_reason(mission, monkeypatch):
    from behive import server
    from behive.hygiene import MISSION_REASONS

    monkeypatch.setenv("BEHIVE_PHASE_TIMEOUT", "2")
    attempts, bridges = _run_api(monkeypatch, "import time; time.sleep(60)")

    assert attempts == ["m"]  # no whole-pipeline retry after a phase failure
    assert _row(mission, "SELECT status, phase FROM hive_missions WHERE id='m'") == ("error", "process_timeout")
    # Regex salvage still ran in the API finalizer: material exists, the mission is not done.
    assert _row(mission, "SELECT COUNT(*) FROM hive_claims WHERE mission_id='m' AND claim_type='regex_fallback'")[0] == 3
    assert bridges == []
    reason = MISSION_REASONS["process_timeout"]
    events = [(e["event"], e["data"]) for e in server._mission_events["m"]]
    assert ("error", {"message": reason, "reason": reason, "phase": "process_timeout",
                      "sources": 1, "claims": 0, "exit_code": 1}) in events
    assert not any(event == "done" for event, _ in events)
    assert _row(mission, "SELECT COUNT(*) FROM hive_events WHERE mission_id='m' AND phase='process' "
                         "AND event_type='error' AND data->>'error_type'='TimeoutExpired'")[0] == 1

    status = _http("/research/m/status")
    assert status.status_code == 200
    body = status.json()
    assert (body["status"], body["phase"], body["reason"]) == ("error", "process_timeout", reason)
    assert (body["report_available"], body["useful_claims"], body["fallback_claims"]) == (False, 0, 3)

    report = _http("/research/m/report")
    assert report.status_code == 409
    assert {k: report.json()[k] for k in ("status", "phase", "reason", "report_available")} == {
        "status": "error", "phase": "process_timeout", "reason": reason, "report_available": False}

    text, is_error = _mcp("mission_status", {"job_id": "m"})
    assert not is_error
    assert "Status: error" in text and "Phase: process_timeout" in text and f"Reason: {reason}" in text
    assert "Claims: 0 useful, 3 regex fallback" in text and "Report: not available" in text
    text, is_error = _mcp("get_report", {"mission_id": "m"})
    assert not is_error and text.startswith("No report") and reason in text


def test_partial_process_without_useful_claims_is_empty_even_with_a_report(mission, monkeypatch):
    from behive.hygiene import MISSION_REASONS

    def legacy_report():
        with mission.cursor() as cur:  # e.g. a legacy synthesis written from regex material
            cur.execute("UPDATE hive_missions SET synthesis='# Report\n\nSolar capacity reached 100 MW.' WHERE id='m'")
        mission.commit()

    monkeypatch.setenv("BEHIVE_PHASE_TIMEOUT", "30")
    attempts, bridges = _run_api(monkeypatch, "pass", after_process=legacy_report)

    assert attempts == ["m"]
    assert _row(mission, "SELECT status, phase FROM hive_missions WHERE id='m'") == ("empty", "empty_claims")
    assert bridges == []
    body = _http("/research/m/status").json()
    assert (body["status"], body["phase"], body["reason"]) == ("empty", "empty_claims", MISSION_REASONS["empty_claims"])
    assert _http("/research/m/report").status_code == 409
    text, _ = _mcp("get_report", {"mission_id": "m"})
    assert text.startswith("No report") and MISSION_REASONS["empty_claims"] in text


def test_useful_claim_and_report_still_complete_as_done(mission, monkeypatch):
    def synthesis():
        with mission.cursor() as cur:
            cur.execute("INSERT INTO hive_claims (mission_id, claim, evidence, source_url, claim_type, confidence, "
                        "quality_score, is_garbage) VALUES ('m', %s, %s, %s, 'fact', 0.9, 0.9, false)",
                        ("Solar capacity in region 1 reached 101 megawatts in 2024.", "documented annual reporting",
                         URL.format(1)))
            cur.execute("UPDATE hive_missions SET synthesis='# Solar capacity\n\nRegion 1 reached 101 MW [1].' WHERE id='m'")
        mission.commit()

    monkeypatch.setenv("BEHIVE_PHASE_TIMEOUT", "30")
    attempts, bridges = _run_api(monkeypatch, "pass", after_process=synthesis)

    assert _row(mission, "SELECT status, phase FROM hive_missions WHERE id='m'") == ("done", "done")
    assert bridges == ["m"]
    body = _http("/research/m/status").json()
    assert (body["status"], body["reason"], body["report_available"], body["useful_claims"]) == ("done", None, True, 1)
    report = _http("/research/m/report")
    assert report.status_code == 200 and report.json()["report_available"] is True
    assert report.json()["synthesis"].startswith("# Solar capacity")
    text, _ = _mcp("get_report", {"mission_id": "m"})
    assert text.startswith("# Solar capacity")


def test_legacy_false_done_rows_are_explained_not_reported_as_results(mission):
    from behive.hygiene import LEGACY_DONE_REASON

    with mission.cursor() as cur:
        cur.execute("UPDATE hive_missions SET status='done', phase='done', synthesis=NULL WHERE id='m'")
        cur.execute("INSERT INTO hive_claims (mission_id, claim, source_url, claim_type, is_garbage) "
                    "VALUES ('m', 'Number of Pages: 158 www.example.gov report 2024.', %s, 'regex_fallback', false)",
                    (URL.format(0),))
    mission.commit()
    body = _http("/research/m/status").json()
    assert (body["status"], body["report_available"], body["reason"]) == ("done", False, LEGACY_DONE_REASON)
    report = _http("/research/m/report").json()
    assert (report["report_available"], report["reason"]) == (False, LEGACY_DONE_REASON)
    text, _ = _mcp("get_report", {"mission_id": "m"})
    assert text.startswith("No report") and LEGACY_DONE_REASON in text
