"""Honest mission status (diag 2026-11-12, task 1) — real finalizers, SQLite boundary.

done   only with a non-blank report AND at least one eligible claim that is not
       regex_fallback salvage;
error  with phase process_timeout / process_error when the process phase timed out or
       failed (regex salvage material does not change that);
empty  with phase empty_claims when no useful claim exists.

The real server._run_pipeline_inner, orchestrator.cmd_run/_run_process_phase and
_mark_mission_error run; only the pipeline subprocess, planner and DB transport are
replaced (SQLite executes the production predicates). The PostgreSQL end-to-end
proof is tests/test_diag1112_mission_status_pg.py.
"""
import subprocess

import pytest

from behive import hygiene, server
from behive.engine import orchestrator
from test_mission_completion_lifecycle import BoundaryDB, run_server
from test_mission_completion_orchestrator import ErrorBoundaryDB, run_cmd

REGEX = "regex_fallback"


def _regex_claims(db, n=3):
    for i in range(n):
        db.claim(text=f"Number of Pages: 15{i} www.example.gov report published in 2024.", claim_type=REGEX)


def _worker_marks(db, status, phase):
    db.execute("UPDATE hive_missions SET status=?, phase=? WHERE id='m'", (status, phase))
    db.commit()


# ── API finalizer (server._run_pipeline_inner) ─────────────────────────────────

@pytest.mark.parametrize("rc", [0, 1])
def test_regex_salvage_only_is_empty_even_with_a_report(monkeypatch, rc):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    _regex_claims(db)
    db.report()
    events, bridges = run_server(monkeypatch, db, rc=rc)
    assert (db.status, db.phase) == (("empty", "empty_claims") if rc == 0 else ("error", "pipeline_failed"))
    assert not bridges
    assert not any(event == "done" for event, _ in events)


@pytest.mark.parametrize("phase", sorted(hygiene.PROCESS_FAILURE_PHASES))
@pytest.mark.parametrize("material", ["regex", "useful_with_report"])
def test_worker_process_failure_is_never_done(monkeypatch, phase, material):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    if material == "regex":
        _regex_claims(db)
    else:
        for _ in range(3):
            db.claim()
        db.report()

    real_spawn_marks = []

    def mark_worker(*_args):
        _worker_marks(db, "error", phase)
        real_spawn_marks.append(phase)

    events, bridges = _run_server_with_worker(monkeypatch, db, rc=1, on_spawn=mark_worker)
    assert real_spawn_marks == [phase]
    assert (db.status, db.phase) == ("error", phase)
    assert not bridges
    assert not any(event == "done" for event, _ in events)
    error_events = [data for event, data in _server_events() if event == "error"]
    assert error_events and error_events[-1]["reason"] == hygiene.MISSION_REASONS[phase]


def test_useful_claims_without_report_are_synthesis_failed(monkeypatch):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    db.claim()
    events, bridges = run_server(monkeypatch, db)
    assert (db.status, db.phase) == ("error", "synthesis_failed")
    assert not bridges


def test_useful_claim_and_report_complete_and_bridge(monkeypatch):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    _regex_claims(db, 2)
    db.claim()
    db.report()
    events, bridges = run_server(monkeypatch, db)
    assert (db.status, db.phase) == ("done", "done")
    assert bridges == [("m", "done")]


def test_blank_report_is_not_a_report(monkeypatch):
    db = BoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    db.claim()
    db.report(" \n\t ")
    run_server(monkeypatch, db)
    assert (db.status, db.phase) == ("error", "synthesis_failed")


def test_provisional_false_done_written_by_worker_is_corrected(monkeypatch):
    """A legacy writer may set done during this invocation: regex material is not a product."""
    db = BoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    _regex_claims(db)
    db.report()
    events, bridges = run_server(monkeypatch, db, worker_done=True)
    assert (db.status, db.phase) == ("empty", "empty_claims")
    assert not bridges


# ── Orchestrator (cmd_run / _run_process_phase / _mark_mission_error) ─────────

def _process_phase_pipeline(monkeypatch, exc, calls):
    def fake_run_phase(script, args, phase_name, timeout=600):
        calls.append(phase_name)
        if phase_name == "process":
            raise exc
        return 0.0

    def pipeline(mission_id, *args, **kwargs):
        calls.append("pipeline")
        orchestrator._run_process_phase(mission_id, False, {})

    monkeypatch.setattr(orchestrator, "run_phase", fake_run_phase)
    monkeypatch.setattr(orchestrator, "_run_synthesis",
                        lambda *a, **k: pytest.fail("no synthesis after a failed process phase"))
    return pipeline


@pytest.mark.parametrize("exc,phase", [
    (subprocess.TimeoutExpired(["python", "-m", "behive.engine.process"], 600), "process_timeout"),
    (subprocess.CalledProcessError(1, ["python", "-m", "behive.engine.process"]), "process_error"),
    (OSError("spawn failed"), "process_error"),
])
def test_cmd_run_process_failure_marks_error_without_retry(monkeypatch, tmp_path, exc, phase):
    db = ErrorBoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    _regex_claims(db)
    db.report()
    calls = []
    pipeline = _process_phase_pipeline(monkeypatch, exc, calls)
    monkeypatch.setenv("BEHIVE_MAX_RETRIES", "3")
    with pytest.raises(SystemExit) as raised:
        run_cmd(monkeypatch, db, tmp_path, pipeline=pipeline, retries="3")
    assert raised.value.code == 1
    assert (db.status, db.phase) == ("error", phase)
    assert calls == ["pipeline", "process"]  # one attempt: no retry burning provider quota


def test_cmd_run_regex_only_is_empty_claims(monkeypatch, tmp_path):
    db = ErrorBoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    _regex_claims(db)
    db.report()
    with pytest.raises(SystemExit) as raised:
        run_cmd(monkeypatch, db, tmp_path)
    assert raised.value.code == 1
    assert (db.status, db.phase) == ("empty", "empty_claims")


def test_cmd_run_without_report_is_synthesis_failed(monkeypatch, tmp_path):
    db = ErrorBoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    db.claim()
    with pytest.raises(SystemExit) as raised:
        run_cmd(monkeypatch, db, tmp_path)
    assert raised.value.code == 1
    assert (db.status, db.phase) == ("error", "synthesis_failed")


def test_cmd_run_useful_claim_and_report_is_done(monkeypatch, tmp_path):
    db = ErrorBoundaryDB()
    db.execute("INSERT INTO hive_content VALUES ('m', 'api://doc')")
    _regex_claims(db)
    db.claim()
    db.report()
    run_cmd(monkeypatch, db, tmp_path)
    assert (db.status, db.phase) == ("done", "done")


def test_cmd_run_empty_scout_needs_no_sources_and_no_content(monkeypatch, tmp_path):
    db = ErrorBoundaryDB()
    with pytest.raises(SystemExit):
        run_cmd(monkeypatch, db, tmp_path)
    assert (db.status, db.phase) == ("empty", "empty_scout")


def test_mark_error_with_process_phase_and_reason(monkeypatch):
    db = ErrorBoundaryDB()
    monkeypatch.setattr(orchestrator, "_connect_db", lambda **kwargs: db)
    assert orchestrator._mark_mission_error("m", "process_timeout", phase="process_timeout") is True
    assert (db.status, db.phase) == ("error", "process_timeout")


def test_regex_material_cannot_block_empty_correction_of_false_done(monkeypatch):
    db = ErrorBoundaryDB()
    _regex_claims(db)
    db.report()
    _worker_marks(db, "done", "done")
    monkeypatch.setattr(orchestrator, "_connect_db", lambda **kwargs: db)
    orchestrator._mark_mission_error("m", "empty_mission sources=1 claims=0")
    assert (db.status, db.phase) == ("empty", "empty_claims")


# ── Shared reason helper ──────────────────────────────────────────────────────

def test_reason_helper():
    assert hygiene.mission_reason("error", "process_timeout") == hygiene.MISSION_REASONS["process_timeout"]
    assert hygiene.mission_reason("empty", "empty_claims") == hygiene.MISSION_REASONS["empty_claims"]
    assert hygiene.mission_reason("error", "unknown_phase") == "Mission execution failed."
    assert hygiene.mission_reason("running", "process") is None
    assert hygiene.mission_reason("done", "done", report_present=True, useful_claims=3) is None
    assert hygiene.mission_reason("done", "done", report_present=False, useful_claims=3) == hygiene.LEGACY_DONE_REASON
    assert hygiene.mission_reason("done", "done", report_present=True, useful_claims=0) == hygiene.LEGACY_DONE_REASON
    assert hygiene.classify_completion(useful_claims=0, report_present=True, sources=0, content=1) == ("empty", "empty_claims")


# helpers ---------------------------------------------------------------------

def _server_events():
    return [(entry["event"], entry["data"]) for entry in server._mission_events.get("m", [])]


def _run_server_with_worker(monkeypatch, db, *, rc, on_spawn):
    """run_server variant whose spawned worker writes its own terminal state."""
    import asyncio
    import behive.engine.claims_fallback as fallback
    from test_mission_completion_lifecycle import EmptyOutput

    server._mission_events.pop("m", None)
    bridges = []

    async def spawn(*args, **kwargs):
        on_spawn()

        class Proc:
            stdout = EmptyOutput()
            returncode = rc

            async def wait(self):
                return rc
        return Proc()

    monkeypatch.setattr(fallback, "salvage_claims_from_content", lambda mid: 0)
    monkeypatch.setattr(server, "get_db", lambda: db)
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(server, "_bridge_done_mission", lambda mid: bridges.append((mid, db.committed_status)))
    asyncio.run(server._run_pipeline_inner("m", "topic", 1))
    return [(event, None) for event, _ in _server_events()], bridges
