"""No test may leave a BeHive pipeline child running (R-36).

The API's _run_pipeline starts `python -m behive.engine run ...` as a real subprocess, and
cmd_run begins with cleanup_stuck_missions(max_age_hours=1). A child left behind by one test
marks the 3-hour-old missions of a LATER test 'error' while that test runs. tests/conftest.py
therefore ends every test with a sweep: a still-running direct child of the pytest process
whose command line runs behive.engine (any module), the orchestrator or a hive2_ script is
terminated (killed after a short wait), the test is recorded, and pytest_terminal_summary
prints the list.

The children here are harmless `python -c` sleepers that carry the marker in an argument.
"""
import subprocess
import sys

import pytest

import conftest

SLEEP = "import time; time.sleep(60)"


def _spawn(*marker):
    return subprocess.Popen([sys.executable, "-c", SLEEP, *marker],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def spawned():
    procs = []

    def spawn(*marker):
        procs.append(_spawn(*marker))
        return procs[-1]

    yield spawn
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.fixture
def leaked(monkeypatch):
    """A clean record list, restored afterwards (the real one belongs to the session)."""
    record = []
    monkeypatch.setattr(conftest, "_LEAKED_CHILDREN", record)
    return record


@pytest.mark.parametrize("marker", [
    "behive.engine.scout",
    "behive.engine",
    "behive.engine.orchestrator",
    "/srv/x/src/behive/engine/harvest.py",
    "orchestrator",
    "hive2_pipeline.py",
])
def test_a_matching_child_is_terminated_and_recorded(spawned, leaked, marker):
    proc = spawned(marker)

    conftest._reap_leaked_children("tests/test_x.py::test_leaves_a_child")

    assert proc.wait(timeout=10) is not None
    assert [name for name, _cmd in leaked] == ["tests/test_x.py::test_leaves_a_child"]
    assert marker in leaked[0][1]


def test_an_unrelated_child_is_left_running_and_not_recorded(spawned, leaked):
    proc = spawned("some.other.module")

    conftest._reap_leaked_children("tests/test_x.py::test_unrelated")

    assert proc.poll() is None
    assert leaked == []


def test_a_child_that_ignores_sigterm_is_killed(leaked):
    stubborn = ("import signal, time, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", stubborn, "behive.engine.scout"],
                            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"  # the handler is installed

        conftest._reap_leaked_children("tests/test_x.py::test_stubborn", grace=0.5)

        assert proc.wait(timeout=10) == -9  # SIGKILL
        assert len(leaked) == 1
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


def test_a_child_that_already_ended_is_not_recorded(leaked):
    proc = subprocess.Popen([sys.executable, "-c", "pass", "behive.engine.scout"])
    proc.wait()

    conftest._reap_leaked_children("tests/test_x.py::test_finished")

    assert leaked == []


def test_the_terminal_summary_lists_the_tests_that_leaked(leaked):
    lines = []

    class Reporter:
        def section(self, title, **kw):
            lines.append("== " + title)

        def write_line(self, text, **kw):
            lines.append(text)

    conftest.pytest_terminal_summary(Reporter(), 0, None)
    assert lines == []  # empty list: nothing printed

    leaked.append(("tests/test_a.py::test_one", "python -m behive.engine run topic"))
    conftest.pytest_terminal_summary(Reporter(), 0, None)

    text = "\n".join(lines)
    assert "tests/test_a.py::test_one" in text
    assert "behive.engine run topic" in text


def test_the_sweep_is_an_autouse_fixture_of_every_test(request):
    assert "reap_pipeline_children" in request.fixturenames


def test_no_real_pipeline_replaces_the_servers_pipeline_starter(no_real_pipeline):
    import asyncio

    import behive.server

    assert asyncio.run(behive.server._run_pipeline("m", "topic", 1, 30)) is None
    assert behive.server._run_pipeline.__module__ == "conftest"
