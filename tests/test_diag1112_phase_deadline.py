"""Process phase vs. its deadline (diag 2026-11-12, task 2 a/b/c/e) — offline.

12.11: the process phase was SIGKILLed at 600 s with 0 documents processed. Model
calls waited up to BEHIVE_LLM_TIMEOUT (300 s) regardless of the phase deadline, the
relevance gate had no deadline at all, and run_phase gave the child no time between
its deadline and the kill.

Real llm.complete / complete_async, RelevanceFilter, source extraction and run_phase
(with real child processes); only the provider boundary (litellm) and the clock are
replaced. No network, no model.
"""
import asyncio
import json
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from behive.engine import llm


def _reply(text):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=text))])


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(llm, "_now", lambda: now["t"])
    monkeypatch.setenv("BEHIVE_MODEL", "openai/behive-test-double")
    for name in ("BEHIVE_LLM_STRICT", "BEHIVE_LLM_TIMEOUT", "BEHIVE_LLM_MIN_CALL_S",
                 "BEHIVE_PHASE_DEADLINE", "BEHIVE_LLM_CODEX_BRIDGE", "OPENAI_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)  # retry/backoff pauses
    return now


def _deadline(monkeypatch, clock, seconds):
    monkeypatch.setenv("BEHIVE_PHASE_DEADLINE", str(clock["t"] + seconds))


# ── (a) every call's timeout fits the phase deadline ──────────────────────────

@pytest.mark.parametrize("strict", [False, True])
@patch("litellm.completion")
def test_call_timeout_is_capped_by_the_phase_deadline(completion, monkeypatch, clock, strict):
    if strict:
        monkeypatch.setenv("BEHIVE_LLM_STRICT", "1")
    monkeypatch.setenv("BEHIVE_LLM_TIMEOUT", "300")
    _deadline(monkeypatch, clock, 100)
    completion.return_value = _reply("ok")
    assert llm.complete("q") == "ok"
    assert completion.call_args.kwargs["timeout"] == pytest.approx(100 - llm.DEADLINE_MARGIN_S)


@patch("litellm.completion")
def test_configured_timeout_is_kept_when_shorter_or_without_a_deadline(completion, monkeypatch, clock):
    monkeypatch.setenv("BEHIVE_LLM_TIMEOUT", "30")
    completion.return_value = _reply("ok")
    llm.complete("q")
    assert completion.call_args.kwargs["timeout"] == 30
    _deadline(monkeypatch, clock, 500)
    llm.complete("q")
    assert completion.call_args.kwargs["timeout"] == 30


@patch("litellm.acompletion", new_callable=AsyncMock)
def test_async_completion_is_capped_too(acompletion, monkeypatch, clock):
    monkeypatch.setenv("BEHIVE_LLM_STRICT", "1")
    monkeypatch.setenv("BEHIVE_LLM_TIMEOUT", "300")
    _deadline(monkeypatch, clock, 80)
    acompletion.return_value = _reply("ok")
    assert asyncio.run(llm.complete_async("q")) == "ok"
    assert acompletion.call_args.kwargs["timeout"] == pytest.approx(80 - llm.DEADLINE_MARGIN_S)


# ── (c) no new call after the deadline ────────────────────────────────────────

@pytest.mark.parametrize("strict", [False, True])
@patch("litellm.completion")
def test_no_call_starts_when_the_deadline_leaves_too_little_time(completion, monkeypatch, clock, strict):
    if strict:
        monkeypatch.setenv("BEHIVE_LLM_STRICT", "1")
    _deadline(monkeypatch, clock, llm.DEADLINE_MARGIN_S + 1)
    with pytest.raises(llm.PhaseDeadlineExceeded):
        llm.complete("q")
    completion.assert_not_called()


@patch("litellm.acompletion", new_callable=AsyncMock)
def test_no_async_call_starts_after_the_deadline(acompletion, monkeypatch, clock):
    _deadline(monkeypatch, clock, -1)
    with pytest.raises(llm.PhaseDeadlineExceeded):
        asyncio.run(llm.complete_async("q"))
    acompletion.assert_not_called()


@patch("litellm.completion")
def test_non_strict_retry_is_not_started_after_the_deadline(completion, monkeypatch, clock):
    _deadline(monkeypatch, clock, 60)

    def slow_failure(**kwargs):
        clock["t"] += 55  # the failed attempt used the rest of the phase
        raise TimeoutError("provider timeout")
    completion.side_effect = slow_failure
    with pytest.raises(llm.PhaseDeadlineExceeded):
        llm.complete("q")
    assert completion.call_count == 1  # no retry, no direct-API fallback call


class RateLimitError(Exception):
    status_code = 429


@patch("litellm.completion")
def test_strict_busy_wait_stops_at_the_deadline(completion, monkeypatch, clock):
    monkeypatch.setenv("BEHIVE_LLM_STRICT", "1")
    _deadline(monkeypatch, clock, 40)

    def busy(**kwargs):
        clock["t"] += 15
        raise RateLimitError("Local concurrency limit reached; retry later.")
    completion.side_effect = busy
    with pytest.raises(llm.PhaseDeadlineExceeded):
        llm.complete("q")
    assert completion.call_count <= 2
    assert all(call.kwargs["timeout"] > 0 for call in completion.call_args_list)


# ── (b) the relevance gate defers, never rejects or auto-admits, at the deadline

def _docs(n):
    return [{"url": f"https://example.test/{i}", "title": f"Solar capacity Poland {i}",
             "raw_text": "Solar capacity in Poland reached 17 GW in 2024 according to the regulator. " * 4}
            for i in range(n)]


@pytest.mark.parametrize("how", ["refused", "timed_out"])
@patch("litellm.completion")
def test_relevance_batches_after_the_deadline_are_deferred(completion, monkeypatch, clock, how):
    from behive.engine.process import RelevanceFilter

    monkeypatch.setenv("BEHIVE_GATE_CONCURRENCY", "1")
    _deadline(monkeypatch, clock, 120)
    calls = []

    def judge(**kwargs):
        calls.append(kwargs["timeout"])
        payload = json.loads(kwargs["messages"][-1]["content"].split("\n", 1)[1])
        if len(calls) == 1:
            clock["t"] += 100  # first batch: answered, uses most of the phase
            return _reply(json.dumps([{"index": d["index"], "grade": 2, "reason": "on topic"}
                                      for d in payload["documents"]]))
        clock["t"] += kwargs["timeout"] + 1  # second batch: the capped timeout fires
        raise type("Timeout", (Exception,), {})("request timed out")
    completion.side_effect = judge
    if how == "refused":
        monkeypatch.setenv("BEHIVE_LLM_MIN_CALL_S", "30")  # 15 s left after batch 1: not started
    docs = _docs(20)
    kept = asyncio.run(RelevanceFilter("Solar capacity Poland").filter(docs))

    assert len(calls) == (1 if how == "refused" else 2)
    assert [d["url"] for d in kept] == [d["url"] for d in docs[:15]]
    deferred = [d for d in docs[15:]]
    assert {d["_relevance_method"] for d in deferred} == {"deadline_deferred"}
    assert all(d["_relevance_grade"] is None for d in deferred)  # no invented grade
    assert not any(d in kept for d in deferred)  # no keyword-fallback admission


def test_gate_without_deadline_still_uses_keyword_fallback_for_provider_errors(monkeypatch, clock):
    """Unchanged behaviour for real provider failures (not deadline)."""
    from behive.engine.process import RelevanceFilter

    async def failing(self, chunk):
        raise ValueError("Invalid relevance batch shape")
    monkeypatch.setattr(RelevanceFilter, "score_batch", failing)
    docs = _docs(3)
    kept = asyncio.run(RelevanceFilter("Solar capacity Poland").filter(docs))
    assert {d["_relevance_method"] for d in docs} == {"keyword_fallback"}
    assert len(kept) == 3


# ── (b) source extraction: a call cut by the deadline is not a fatal failure ──

@patch("litellm.completion")
def test_extraction_timeout_at_the_deadline_is_deferred_not_fatal(completion, monkeypatch, clock):
    from behive.engine import source_extraction

    monkeypatch.setattr(source_extraction, "DEADLINE_RESERVE_S", 0.0)
    monkeypatch.setattr(source_extraction, "EXPECTED_DOCUMENT_FLOOR_S", 1.0)
    monkeypatch.setenv("BEHIVE_A3_CONCURRENCY", "1")
    monkeypatch.setenv("BEHIVE_LLM_TIMEOUT", "300")  # the cap, not the setting, ends the call
    _deadline(monkeypatch, clock, 200)
    committed = []

    def extract(**kwargs):
        clock["t"] += kwargs["timeout"] + 1  # the capped call times out at the deadline
        raise type("Timeout", (Exception,), {})("request timed out")
    completion.side_effect = extract
    docs = [{"url": f"https://example.test/{i}", "raw_text": "Solar capacity reached 17 GW in 2024."}
            for i in range(3)]
    progress = source_extraction.ExtractionProgress()
    claims = asyncio.run(source_extraction.extract_source_claims(
        docs, "Solar capacity", on_document=lambda url, rows: committed.append(url),
        clock=llm._now, progress=progress))
    assert claims == []
    assert progress.status == "partial"
    assert completion.call_count == 1  # no retry of the cut call, no further page started
    assert completion.call_args.kwargs["timeout"] == pytest.approx(200 - llm.DEADLINE_MARGIN_S)
    assert committed == []  # the interrupted page is not marked attempted: next pass retries it
    assert progress.counts["documents_deadline_deferred"] == 3
    assert progress.counts["documents_failed"] == 0


# ── (e) run_phase: earlier child deadline, SIGTERM, then SIGKILL ──────────────

def _child(monkeypatch, code):
    from behive.engine import orchestrator
    monkeypatch.setattr(orchestrator, "_resolve_cmd", lambda script: [sys.executable, "-c", code])
    monkeypatch.delenv("BEHIVE_PHASE_TIMEOUT", raising=False)


def test_child_deadline_leaves_a_grace_before_the_kill(monkeypatch, tmp_path):
    from behive.engine.orchestrator import run_phase
    out = tmp_path / "left"
    monkeypatch.setenv("DIAG_OUT", str(out))
    _child(monkeypatch, "import os, time; open(os.environ['DIAG_OUT'], 'w')"
                        ".write(str(float(os.environ['BEHIVE_PHASE_DEADLINE']) - time.time()))")
    run_phase("hive2_process.py", ["diag1112-grace"], "process", timeout=40)
    left = float(out.read_text())
    assert 40 - 10 - 2 <= left <= 40 - 10 + 0.5  # grace = min(max(15, 4), 60, 40/4) = 10 s


@pytest.mark.timeout(30)
def test_timeout_sends_sigterm_before_sigkill(monkeypatch, tmp_path):
    from behive.engine.orchestrator import run_phase
    out = tmp_path / "term"
    monkeypatch.setenv("DIAG_OUT", str(out))
    _child(monkeypatch, "import os, signal, sys, time\n"
                        "def bye(*_):\n    open(os.environ['DIAG_OUT'], 'w').write('term'); sys.exit(0)\n"
                        "signal.signal(signal.SIGTERM, bye)\ntime.sleep(60)")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_phase("hive2_process.py", ["diag1112-term"], "process", timeout=2)
    assert out.read_text() == "term"
    assert time.monotonic() - started < 2 + 3


@pytest.mark.timeout(30)
def test_child_ignoring_sigterm_is_killed_after_a_short_wait(monkeypatch, tmp_path):
    from behive.engine.orchestrator import run_phase
    _child(monkeypatch, "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_phase("hive2_process.py", ["diag1112-kill"], "process", timeout=2)
    elapsed = time.monotonic() - started
    assert 2 + 1 - 0.3 <= elapsed < 2 + 1 + 3  # term wait = min(10, max(1, 2/10)) = 1 s
