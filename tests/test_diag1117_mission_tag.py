"""Mission id and phase on every model call (I-BRIDGE-TAG).

The bridge can attribute a call only if the request carries the mission id and
the phase, and nothing else. Headers are attached at the litellm boundary when
both values match strict patterns. run_phase must hand both to a real child.
"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from behive.engine import llm


_MISSION = "hive_1794840107_30f089"
_PHASE = "process"


def _reply(text="ok"):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=text))])


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setenv("BEHIVE_MODEL", "openai/behive-test-double")
    for name in ("BEHIVE_LLM_STRICT", "BEHIVE_LLM_TIMEOUT", "BEHIVE_PHASE_DEADLINE",
                 "BEHIVE_LLM_CODEX_BRIDGE", "OPENAI_API_BASE", "BEHIVE_MISSION_ID",
                 "BEHIVE_PHASE"):
        monkeypatch.delenv(name, raising=False)
    if getattr(llm, "_mission_phase", None) is not None:
        llm._mission_phase.set(None)


def _headers(call):
    return call.kwargs.get("extra_headers")


@patch("litellm.completion")
def test_valid_mission_and_phase_are_the_only_extra_headers(completion, monkeypatch, model):
    monkeypatch.setenv("BEHIVE_MISSION_ID", _MISSION)
    monkeypatch.setenv("BEHIVE_PHASE", _PHASE)
    completion.return_value = _reply()
    assert llm.complete("q") == "ok"
    assert _headers(completion.call_args) == {
        "X-BeHive-Mission": _MISSION,
        "X-BeHive-Phase": _PHASE,
    }


@patch("litellm.acompletion", new_callable=AsyncMock)
def test_async_call_carries_the_same_two_headers(acompletion, monkeypatch, model):
    monkeypatch.setenv("BEHIVE_MISSION_ID", _MISSION)
    monkeypatch.setenv("BEHIVE_PHASE", "synth")
    acompletion.return_value = _reply()
    assert asyncio.run(llm.complete_async("q")) == "ok"
    assert _headers(acompletion.call_args) == {
        "X-BeHive-Mission": _MISSION,
        "X-BeHive-Phase": "synth",
    }


@pytest.mark.parametrize("mission,phase", [
    ("not-a-mission", _PHASE),
    ("hive_1794840107_30f089 extra", _PHASE),
    ("HIVE_1794840107_30f089", _PHASE),
    (_MISSION, "Process"),
    (_MISSION, "process phase"),
    (_MISSION, "a" * 33),
    ("", _PHASE),
    (_MISSION, ""),
])
@patch("litellm.completion")
def test_invalid_mission_or_phase_sends_no_headers(completion, monkeypatch, model, mission, phase):
    monkeypatch.setenv("BEHIVE_MISSION_ID", mission)
    monkeypatch.setenv("BEHIVE_PHASE", phase)
    completion.return_value = _reply()
    llm.complete("topic text that must not be a header")
    assert _headers(completion.call_args) is None
    sent = completion.call_args.kwargs
    assert "topic text" not in str(sent.get("extra_headers"))


@patch("litellm.completion")
def test_in_process_scope_tags_the_call_without_env(completion, monkeypatch, model):
    completion.return_value = _reply()
    with llm.mission_scope(_MISSION, "plan"):
        llm.complete("q")
    assert _headers(completion.call_args) == {
        "X-BeHive-Mission": _MISSION,
        "X-BeHive-Phase": "plan",
    }
    llm.complete("q")
    assert _headers(completion.call_args) is None


@patch("litellm.completion")
def test_invalid_scope_does_not_fall_through_to_a_valid_env(completion, monkeypatch, model):
    monkeypatch.setenv("BEHIVE_MISSION_ID", _MISSION)
    monkeypatch.setenv("BEHIVE_PHASE", _PHASE)
    completion.return_value = _reply()
    with llm.mission_scope("topic text", "plan"):
        llm.complete("q")
    assert _headers(completion.call_args) is None


def test_run_phase_passes_mission_and_phase_to_a_real_child(monkeypatch, tmp_path):
    from behive.engine import orchestrator
    from behive.engine.orchestrator import run_phase

    out = tmp_path / "child-env"
    monkeypatch.setenv("DIAG_OUT", str(out))
    monkeypatch.setenv("BEHIVE_MISSION_ID", _MISSION)
    monkeypatch.setenv("BEHIVE_PHASE", "not-this")
    monkeypatch.delenv("BEHIVE_PHASE_TIMEOUT", raising=False)
    monkeypatch.setattr(orchestrator, "_resolve_cmd", lambda script: [
        sys.executable, "-c",
        "import os\n"
        "open(os.environ['DIAG_OUT'], 'w').write("
        "os.environ.get('BEHIVE_MISSION_ID', '') + '\\n' + os.environ.get('BEHIVE_PHASE', ''))",
    ])
    run_phase("hive2_process.py", ["diag1117-tag"], "process", timeout=40)
    mission, phase = out.read_text().splitlines()
    assert mission == _MISSION
    assert phase == "process"


def test_run_phase_prefers_a_mission_id_in_the_child_args(monkeypatch, tmp_path):
    from behive.engine import orchestrator
    from behive.engine.orchestrator import run_phase

    out = tmp_path / "child-args"
    gap = "hive_1794840999_abc123"
    monkeypatch.setenv("DIAG_OUT", str(out))
    monkeypatch.setenv("BEHIVE_MISSION_ID", _MISSION)
    monkeypatch.delenv("BEHIVE_PHASE_TIMEOUT", raising=False)
    monkeypatch.setattr(orchestrator, "_resolve_cmd", lambda script: [
        sys.executable, "-c",
        "import os\n"
        "open(os.environ['DIAG_OUT'], 'w').write("
        "os.environ.get('BEHIVE_MISSION_ID', '') + '\\n' + os.environ.get('BEHIVE_PHASE', ''))",
    ])
    run_phase("hive2_harvest.py", ["--mission", gap], "gap-1-harvest", timeout=40)
    mission, phase = out.read_text().splitlines()
    assert mission == gap
    assert phase == "gap-1-harvest"
