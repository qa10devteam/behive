"""Static defects from the 2026-11-12 diagnosis (task 6) and their runtime effect.

ruff F821/F811/F601 must stay clean on src/. Two of the findings were real runtime
bugs, not only lint: queen_feedback's retry wrapper shadowed the imported connect and
called itself, and prescout referenced removed DuckDB/hive2 modules.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_ruff_reports_no_undefined_redefined_or_repeated_names():
    if importlib.util.find_spec("ruff") is None:
        pytest.skip("ruff not installed (dev extra)")
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(ROOT / "src"), "--select", "F821,F811,F601",
         "--output-format", "concise", "--no-cache"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


class _FlakyConnect:
    def __init__(self, failures):
        self.failures = failures
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if len(self.calls) <= self.failures:
            raise OSError("database is starting up")
        return "connection"


@pytest.mark.timeout(20)
def test_queen_feedback_retry_wrapper_reaches_the_database_without_recursion(monkeypatch):
    monkeypatch.undo()  # drop the suite-wide connection patches: test the real wrapper
    from behive.engine import db as engine_db
    from behive.engine import queen_feedback

    flaky = _FlakyConnect(failures=2)
    monkeypatch.setattr(engine_db, "connect", flaky)
    monkeypatch.setattr(queen_feedback.time, "sleep", lambda seconds: None)
    assert queen_feedback._db_connect(read_only=True, retries=3) == "connection"
    assert flaky.calls == [((), {"read_only": True})] * 3


@pytest.mark.timeout(20)
def test_queen_feedback_retry_wrapper_gives_up_with_runtime_error(monkeypatch):
    monkeypatch.undo()
    from behive.engine import db as engine_db
    from behive.engine import queen_feedback

    flaky = _FlakyConnect(failures=10)
    monkeypatch.setattr(engine_db, "connect", flaky)
    monkeypatch.setattr(queen_feedback.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="after 2 attempts"):
        queen_feedback._db_connect(read_only=False, retries=2)
    assert len(flaky.calls) == 2


@pytest.mark.parametrize("module,call,kwargs", [
    ("behive.engine.falsifier", "_db_connect", {"read_only": True}),
])
def test_wrappers_resolve_connect_at_call_time(monkeypatch, module, call, kwargs):
    monkeypatch.undo()
    from behive.engine import db as engine_db
    mod = importlib.import_module(module)
    flaky = _FlakyConnect(failures=0)
    monkeypatch.setattr(engine_db, "connect", flaky)
    assert getattr(mod, call)(**kwargs) == "connection"
    assert flaky.calls == [((), kwargs)]


def test_queen_calibrator_connection_helper_is_read_only(monkeypatch):
    monkeypatch.undo()
    from behive.engine import db as engine_db
    from behive.engine.queen_calibrator import QueenCalibrator

    flaky = _FlakyConnect(failures=0)
    monkeypatch.setattr(engine_db, "connect", flaky)
    calibrator = QueenCalibrator.__new__(QueenCalibrator)
    assert calibrator._conn() == "connection"
    assert flaky.calls == [((), {"read_only": True})]


def test_content_quality_shim_points_at_the_existing_module():
    from behive.compat.shims import SHIM_MAP

    assert SHIM_MAP["hive2_content_quality"] == "behive.engine.content_quality"
    assert importlib.util.find_spec(SHIM_MAP["hive2_content_quality"]) is not None
    missing = sorted(name for name, target in SHIM_MAP.items() if importlib.util.find_spec(target) is None)
    assert missing == []
