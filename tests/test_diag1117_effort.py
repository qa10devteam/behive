"""Reasoning effort per mission, and optionally another one for the synthesis (I-EFFORT).

Default is unchanged: no field, no header, nothing in the child environment. An
effort is one of low|medium|high; it reaches the bridge only as X-BeHive-Effort
(merged next to the mission/phase headers, never replacing them). The litellm
boundary is patched as in test_diag1117_mission_tag.py; phase children are real
processes; the config round trip on PostgreSQL is in test_diag1117_effort_pg.py.
"""
import asyncio
import contextlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from behive.engine import llm

_MISSION = "hive_1794840107_30f089"
_EFFORT_ENV = ("BEHIVE_REASONING_EFFORT", "BEHIVE_SYNTH_EFFORT")


def _reply(text="ok"):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=text))])


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setenv("BEHIVE_MODEL", "openai/behive-test-double")
    for name in ("BEHIVE_LLM_STRICT", "BEHIVE_LLM_TIMEOUT", "BEHIVE_PHASE_DEADLINE",
                 "BEHIVE_LLM_CODEX_BRIDGE", "OPENAI_API_BASE", "BEHIVE_MISSION_ID",
                 "BEHIVE_PHASE", *_EFFORT_ENV):
        monkeypatch.delenv(name, raising=False)
    if getattr(llm, "_mission_phase", None) is not None:
        llm._mission_phase.set(None)


def _headers(call):
    return call.kwargs.get("extra_headers")


# ── request validation (server) ─────────────────────────────────────────────

def test_request_accepts_the_three_efforts_and_defaults_to_none():
    from behive.server import ResearchRequest

    plain = ResearchRequest(query="q")
    assert plain.reasoning_effort is None and plain.synthesis_effort is None
    for value in ("low", "medium", "high"):
        req = ResearchRequest(query="q", reasoning_effort=value, synthesis_effort=value)
        assert (req.reasoning_effort, req.synthesis_effort) == (value, value)


@pytest.mark.parametrize("field", ["reasoning_effort", "synthesis_effort"])
@pytest.mark.parametrize("bad", ["xhigh", "LOW", "Medium", "", " low", "none", "low,high", 1, True, ["low"]])
def test_request_rejects_any_other_value_with_422(field, bad):
    from fastapi.testclient import TestClient
    from behive.server import app

    with patch("behive.server.get_db") as get_db, patch("behive.server._run_pipeline") as pipeline:
        response = TestClient(app, raise_server_exceptions=False).post(
            "/research", json={"query": "effort validation", field: bad})
    assert response.status_code == 422, response.text
    get_db.assert_not_called()
    pipeline.assert_not_called()


# ── MCP research_topic ──────────────────────────────────────────────────────

def _post_capture(monkeypatch):
    sent = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, timeout=None):
            sent["json"] = json
            return MagicMock(json=lambda: {"job_id": "x"})

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    return sent


def test_mcp_passes_the_two_efforts_on(monkeypatch):
    from behive import mcp_server

    sent = _post_capture(monkeypatch)
    asyncio.run(mcp_server._tool_research(1, {
        "query": "q", "depth": 2, "reasoning_effort": "medium", "synthesis_effort": "high"}))
    assert sent["json"] == {"query": "q", "depth": 2, "force": True,
                            "reasoning_effort": "medium", "synthesis_effort": "high"}


def test_mcp_without_the_fields_posts_exactly_what_it_did_before(monkeypatch):
    from behive import mcp_server

    sent = _post_capture(monkeypatch)
    asyncio.run(mcp_server._tool_research(1, {"query": "q"}))
    assert sent["json"] == {"query": "q", "depth": 3, "force": True}


def test_mcp_tool_schema_lists_the_efforts():
    from behive import mcp_server

    result = asyncio.run(mcp_server.mcp_endpoint(mcp_server.MCPRequest(
        jsonrpc="2.0", id=1, method="tools/list")))
    tool = next(t for t in result["result"]["tools"] if t["name"] == "research_topic")
    props = tool["inputSchema"]["properties"]
    for name in ("reasoning_effort", "synthesis_effort"):
        assert props[name]["enum"] == ["low", "medium", "high"]
    assert tool["inputSchema"]["required"] == ["query"]


# ── X-BeHive-Effort at the litellm boundary ─────────────────────────────────

@pytest.mark.parametrize("value", ["low", "medium", "high"])
@patch("litellm.completion")
def test_a_valid_effort_adds_one_header_next_to_the_mission_headers(completion, model, value):
    completion.return_value = _reply()
    with llm.mission_scope(_MISSION, "process"), llm.effort_scope(value):
        assert llm.complete("q") == "ok"
    assert _headers(completion.call_args) == {
        "X-BeHive-Mission": _MISSION,
        "X-BeHive-Phase": "process",
        "X-BeHive-Effort": value,
    }


@patch("litellm.acompletion", new_callable=AsyncMock)
def test_async_call_carries_the_effort_header_too(acompletion, model):
    acompletion.return_value = _reply()
    with llm.mission_scope(_MISSION, "process"), llm.effort_scope("high"):
        assert asyncio.run(llm.complete_async("q")) == "ok"
    assert _headers(acompletion.call_args) == {
        "X-BeHive-Mission": _MISSION,
        "X-BeHive-Phase": "process",
        "X-BeHive-Effort": "high",
    }


@patch("litellm.completion")
def test_effort_without_a_mission_tag_is_sent_alone(completion, model):
    completion.return_value = _reply()
    with llm.effort_scope("medium"):
        llm.complete("q")
    assert _headers(completion.call_args) == {"X-BeHive-Effort": "medium"}


@patch("litellm.completion")
def test_default_path_sends_no_effort_header_and_no_extra_headers(completion, model):
    completion.return_value = _reply()
    llm.complete("q")
    assert _headers(completion.call_args) is None
    with llm.mission_scope(_MISSION, "plan"):
        llm.complete("q")
    assert _headers(completion.call_args) == {"X-BeHive-Mission": _MISSION, "X-BeHive-Phase": "plan"}
    assert llm.effort_value() is None


@pytest.mark.parametrize("bad", ["xhigh", "LOW", "Medium", "", " low", "low ", "none", "low,high",
                                 "low\r\nX-Evil: 1", "ustawa o krajowym systemie", None])
@patch("litellm.completion")
def test_only_the_three_values_ever_become_a_header(completion, model, monkeypatch, bad):
    completion.return_value = _reply()
    with llm.effort_scope(bad):
        llm.complete("q")
    assert _headers(completion.call_args) is None
    monkeypatch.setenv("BEHIVE_REASONING_EFFORT", "" if bad is None else bad)
    llm.complete("q")
    assert _headers(completion.call_args) is None
    assert llm.effort_value() is None


@patch("litellm.completion")
def test_an_invalid_scope_does_not_fall_through_to_a_valid_environment(completion, model, monkeypatch):
    monkeypatch.setenv("BEHIVE_REASONING_EFFORT", "high")
    completion.return_value = _reply()
    with llm.effort_scope("xhigh"):
        llm.complete("q")
    assert _headers(completion.call_args) is None


@patch("litellm.completion")
def test_the_environment_of_a_phase_child_sets_the_header(completion, model, monkeypatch):
    monkeypatch.setenv("BEHIVE_MISSION_ID", _MISSION)
    monkeypatch.setenv("BEHIVE_PHASE", "process")
    monkeypatch.setenv("BEHIVE_REASONING_EFFORT", "medium")
    completion.return_value = _reply()
    llm.complete("q")
    assert _headers(completion.call_args) == {
        "X-BeHive-Mission": _MISSION, "X-BeHive-Phase": "process", "X-BeHive-Effort": "medium"}


def test_headers_are_merged_into_extra_headers_never_replacing_them():
    kwargs = {"extra_headers": {"X-Other": "kept", "X-BeHive-Effort": "stale"}}
    with llm.mission_scope(_MISSION, "synth"), llm.effort_scope("high"):
        llm._attach_mission_headers(kwargs)
    assert kwargs["extra_headers"] == {
        "X-Other": "kept",
        "X-BeHive-Mission": _MISSION,
        "X-BeHive-Phase": "synth",
        "X-BeHive-Effort": "high",
    }


@pytest.mark.parametrize("phase,expected", [
    ("process", "low"), ("plan", "low"), ("gap-fill", "low"),
    ("synth", "high"), ("queen-synthesis", "high"), ("synth-fallback", "high"), ("synth-final", "high"),
])
def test_synthesis_effort_applies_only_to_synthesis_phases(model, phase, expected):
    with llm.effort_scope("low", "high"), llm.mission_scope(_MISSION, phase):
        assert llm.effort_value() == expected


def test_synthesis_effort_falls_back_to_the_mission_effort(model):
    with llm.effort_scope("medium"), llm.mission_scope(_MISSION, "synth"):
        assert llm.effort_value() == "medium"
    with llm.effort_scope("medium", "bogus"), llm.mission_scope(_MISSION, "synth"):
        assert llm.effort_value() == "medium"
    with llm.effort_scope(None, "high"), llm.mission_scope(_MISSION, "process"):
        assert llm.effort_value() is None
    with llm.effort_scope(None, "high"), llm.mission_scope(_MISSION, "synth"):
        assert llm.effort_value() == "high"


def test_scope_is_restored_on_exit(model):
    with llm.effort_scope("high"):
        with llm.effort_scope("low"):
            assert llm.effort_value() == "low"
        assert llm.effort_value() == "high"
    assert llm.effort_value() is None


def test_synthesis_child_environment_picks_the_synthesis_effort(model, monkeypatch):
    monkeypatch.setenv("BEHIVE_REASONING_EFFORT", "low")
    monkeypatch.setenv("BEHIVE_SYNTH_EFFORT", "high")
    monkeypatch.setenv("BEHIVE_PHASE", "synth-final")
    assert llm.effort_value() == "high"
    monkeypatch.setenv("BEHIVE_PHASE", "process")
    assert llm.effort_value() == "low"
    monkeypatch.setenv("BEHIVE_PHASE", "queen-synthesis")
    monkeypatch.setenv("BEHIVE_SYNTH_EFFORT", "bogus")
    assert llm.effort_value() == "low"


# ── phase children (real processes) ─────────────────────────────────────────

_DUMP_ENV = (
    "import os\n"
    "keys = ('BEHIVE_REASONING_EFFORT', 'BEHIVE_SYNTH_EFFORT', 'BEHIVE_PHASE')\n"
    "open(os.environ['DIAG_OUT'], 'w').write(\n"
    "    '\\n'.join(k + '=' + os.environ.get(k, '<unset>') for k in keys))"
)


def _child_env(monkeypatch, tmp_path, script, phase, scope=None):
    from behive.engine import orchestrator

    out = tmp_path / ("child-env-" + phase)
    monkeypatch.setenv("DIAG_OUT", str(out))
    monkeypatch.delenv("BEHIVE_PHASE_TIMEOUT", raising=False)
    monkeypatch.setattr(orchestrator, "_resolve_cmd", lambda s: [sys.executable, "-c", _DUMP_ENV])
    with scope if scope is not None else contextlib.nullcontext():
        orchestrator.run_phase(script, ["diag1117-effort"], phase, timeout=40)
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


def test_phase_children_get_the_mission_effort_and_the_synthesis_one(model, monkeypatch, tmp_path):
    scope = llm.effort_scope("medium", "high")
    seen = _child_env(monkeypatch, tmp_path, "hive2_process.py", "process", scope)
    assert seen == {"BEHIVE_REASONING_EFFORT": "medium", "BEHIVE_SYNTH_EFFORT": "high",
                    "BEHIVE_PHASE": "process"}
    # What that child's llm.py resolves from its environment.
    for name, value in seen.items():
        monkeypatch.setenv(name, value)
    assert llm.effort_value() == "medium"
    monkeypatch.setenv("BEHIVE_PHASE", "synth-final")
    assert llm.effort_value() == "high"


def test_synthesis_child_without_a_synthesis_effort_uses_the_mission_effort(model, monkeypatch, tmp_path):
    seen = _child_env(monkeypatch, tmp_path, "hive2_synth.py", "synth-final", llm.effort_scope("medium"))
    assert seen == {"BEHIVE_REASONING_EFFORT": "medium", "BEHIVE_SYNTH_EFFORT": "<unset>",
                    "BEHIVE_PHASE": "synth-final"}
    for name, value in seen.items():
        monkeypatch.setenv(name, value)
    assert llm.effort_value() == "medium"


def test_default_phase_child_has_no_effort_in_its_environment(model, monkeypatch, tmp_path):
    seen = _child_env(monkeypatch, tmp_path, "hive2_process.py", "process")
    assert seen == {"BEHIVE_REASONING_EFFORT": "<unset>", "BEHIVE_SYNTH_EFFORT": "<unset>",
                    "BEHIVE_PHASE": "process"}


def test_invalid_efforts_never_reach_a_child(model, monkeypatch, tmp_path):
    seen = _child_env(monkeypatch, tmp_path, "hive2_process.py", "process", llm.effort_scope("xhigh", "LOW"))
    assert seen["BEHIVE_REASONING_EFFORT"] == "<unset>" and seen["BEHIVE_SYNTH_EFFORT"] == "<unset>"


# ── orchestrator: in-process synthesis and the mission run ──────────────────

def test_in_process_synthesis_runs_with_the_synthesis_effort(model, monkeypatch):
    from behive.engine import orchestrator, claim_synthesis

    seen = {}

    def persist(mission_id):
        seen["effort"] = llm.effort_value()
        seen["tag"] = llm.mission_tag()
        return {"chars": 20, "persisted": True, "metadata": {"method": "claim"}}

    con = MagicMock()
    con.execute.return_value.fetchone.return_value = (3,)
    monkeypatch.setattr(claim_synthesis, "persist_claim_synthesis", persist)
    monkeypatch.setattr(orchestrator, "_connect_db", lambda read_only=False: con)
    monkeypatch.setattr(orchestrator, "_emit", lambda *a, **k: None)
    with llm.effort_scope("low", "high"):
        orchestrator._run_synthesis(_MISSION, "topic", {})
    assert seen == {"effort": "high", "tag": (_MISSION, "synth")}
    with llm.effort_scope("medium"):
        orchestrator._run_synthesis(_MISSION, "topic", {})
    assert seen["effort"] == "medium"
    orchestrator._run_synthesis(_MISSION, "topic", {})
    assert seen["effort"] is None


@pytest.mark.parametrize("config,expected", [
    ({"reasoning_effort": "high", "synthesis_effort": "low"}, ("high", "low")),
    ({"reasoning_effort": "medium"}, ("medium", None)),
    ({"synthesis_effort": "high"}, (None, "high")),
    ({"depth": 3, "scale": 30}, (None, None)),
    ({"reasoning_effort": "xhigh", "synthesis_effort": 3}, (None, None)),
    ('{"reasoning_effort": "low"}', ("low", None)),
    ("not json", (None, None)),
    (None, (None, None)),
])
def test_mission_efforts_come_from_the_stored_config(monkeypatch, config, expected):
    from behive.engine import orchestrator

    con = MagicMock()
    con.execute.return_value.fetchone.return_value = (config,)
    monkeypatch.setattr(orchestrator, "_connect_db", lambda read_only=False: con)
    assert orchestrator._mission_efforts(_MISSION) == expected


def test_mission_efforts_default_when_the_mission_or_the_database_is_missing(monkeypatch):
    from behive.engine import orchestrator

    con = MagicMock()
    con.execute.return_value.fetchone.return_value = None
    monkeypatch.setattr(orchestrator, "_connect_db", lambda read_only=False: con)
    assert orchestrator._mission_efforts(_MISSION) == (None, None)

    def down(read_only=False):
        raise RuntimeError("no database")
    monkeypatch.setattr(orchestrator, "_connect_db", down)
    assert orchestrator._mission_efforts(_MISSION) == (None, None)


@pytest.mark.parametrize("efforts,expected", [
    (("high", "low"), ("high", "high")),   # (plan call, phases call)
    ((None, None), (None, None)),
])
def test_cmd_run_binds_the_mission_effort_for_planning_and_the_phases(model, monkeypatch, efforts, expected):
    from behive.engine import orchestrator

    seen = {}

    class _Planner:
        def __init__(self, *a, **k):
            pass

        def plan(self):
            seen["plan"] = llm.effort_value()
            return []

    def phases(*args, **kwargs):
        seen["phases"] = llm.effort_value()
        with llm.mission_scope(_MISSION, "synth"):
            seen["synth"] = llm.effort_value()

    monkeypatch.setenv("BEHIVE_MISSION_ID", _MISSION)
    monkeypatch.setattr(orchestrator, "cleanup_stuck_missions", lambda **k: 0)
    monkeypatch.setattr(orchestrator, "QueenPlanner", _Planner)
    monkeypatch.setattr(orchestrator, "_mission_efforts", lambda mission_id: efforts)
    monkeypatch.setattr(orchestrator, "_create_mission", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_connect_db", lambda read_only=False: MagicMock())
    monkeypatch.setattr(orchestrator, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_run_pipeline_phases", phases)
    monkeypatch.setattr(orchestrator, "_finalize_completed_pipeline", lambda mission_id: None)
    monkeypatch.setitem(sys.modules, "behive.engine.preprocessor", MagicMock(
        preprocess_query=lambda topic, **k: MagicMock(ok=True, refined_topic=topic, specificity_score=0.9)))
    monkeypatch.setitem(sys.modules, "behive.engine.queen_primer", MagicMock(
        QueenPrimer=lambda mission_id: MagicMock(load_for_topic=lambda *a, **k: None)))

    orchestrator.cmd_run("effort topic", force=True)

    assert seen["plan"] == expected[0] and seen["phases"] == expected[1]
    assert seen["synth"] == (efforts[1] or efforts[0])
    assert llm.effort_value() is None  # the binding ends with the run
