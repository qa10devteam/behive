"""Busy-only backpressure at the fake LiteLLM boundary; never a live bridge."""
import asyncio
import logging
from types import SimpleNamespace

import httpx
from litellm import RateLimitError
import pytest

from test_llm_strict_compat import harness, invoke, called

BUSY = 'Local concurrency limit reached; retry later.'
UPSTREAM = 'Upstream quota/rate limit denied the request.'


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    async def asleep(self, seconds):
        self.sleep(seconds)


def rate_error(code='bridge_busy', message=BUSY, *, structured=True):
    response = httpx.Response(429, json={'error': {'code': code, 'message': message}},
                              request=httpx.Request('POST', 'http://bridge.invalid/v1/chat/completions'))
    error = RateLimitError(message=message, llm_provider='openai', model='gpt-6.1-sol')
    if structured:
        # Some SDK/LiteLLM versions retain the HTTP response, others replace it
        # with an empty 429. Exercise both real error shapes explicitly.
        error.response = response
    return error


@pytest.fixture
def busy_harness(harness, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(harness.llm, 'time', SimpleNamespace(
        time=lambda: clock.now, monotonic=lambda: clock.now, sleep=clock.sleep), raising=False)
    monkeypatch.setattr(asyncio, 'sleep', clock.asleep)
    return harness, clock


@pytest.mark.parametrize('mode', ['sync', 'async'])
@pytest.mark.parametrize('strict', [True])
@pytest.mark.parametrize('structured', [False, True])
def test_local_busy_twice_waits_then_returns_on_same_route(busy_harness, monkeypatch, caplog, mode, strict, structured):
    h, clock = busy_harness
    if strict:
        monkeypatch.setenv('BEHIVE_LLM_STRICT', '1')
    fn = called(h, mode)
    fn.side_effect = [rate_error(structured=structured), rate_error(structured=structured), fn.return_value]
    with caplog.at_level(logging.DEBUG):
        assert invoke(h, mode) == 'OK'
    assert clock.sleeps == [5, 10]
    assert fn.call_count == 3
    assert all(c.kwargs == fn.call_args_list[0].kwargs for c in fn.call_args_list)
    h.direct.assert_not_called()
    assert 'private-prompt' not in caplog.text and BUSY not in caplog.text


@pytest.mark.parametrize('mode', ['sync', 'async'])
@pytest.mark.parametrize('strict', [True])
@pytest.mark.parametrize('error', [
    rate_error('upstream_rate_limit', UPSTREAM),
    rate_error('upstream_rate_limit', UPSTREAM + ' ' + BUSY),
    rate_error('rate_limit', '429 provider quota exceeded'),
    RuntimeError('429'), TimeoutError('private-prompt'), ValueError('private-prompt'),
    RuntimeError(BUSY),
])
def test_nonbusy_errors_are_terminal_without_sleep_retry_or_reroute(busy_harness, monkeypatch, mode, strict, error):
    h, clock = busy_harness
    if strict:
        monkeypatch.setenv('BEHIVE_LLM_STRICT', '1')
    fn = called(h, mode)
    fn.side_effect = error
    with pytest.raises(type(error)) as caught:
        invoke(h, mode)
    assert caught.value is error
    fn.assert_called_once()
    assert clock.sleeps == []
    h.direct.assert_not_called()


@pytest.mark.parametrize('mode', ['sync', 'async'])
@pytest.mark.parametrize('budget,expected_sleeps,attempts', [
    ('0', [], 1), ('12', [5, 7], 3), ('90', [5, 10, 20, 30, 25], 6),
])
def test_busy_wait_budget_is_bounded(busy_harness, monkeypatch, mode, budget, expected_sleeps, attempts):
    h, clock = busy_harness
    monkeypatch.setenv('BEHIVE_LLM_STRICT', '1')
    monkeypatch.setenv('BEHIVE_LLM_BUSY_WAIT_S', budget)
    error = rate_error()
    called(h, mode).side_effect = error
    with pytest.raises(RateLimitError) as caught:
        invoke(h, mode)
    assert caught.value is error
    assert clock.sleeps == expected_sleeps
    assert called(h, mode).call_count == attempts
    h.direct.assert_not_called()


# Contract since 2026-11-13 (diag task 2): every call's timeout ends 5 s before the
# phase deadline, no call starts with less than 10 s left, and the busy wait never
# sleeps into that window. (Before, the first call was uncapped and could start 1 s
# before the deadline.)
@pytest.mark.parametrize('mode', ['sync', 'async'])
def test_busy_wait_never_crosses_phase_deadline_margin(busy_harness, monkeypatch, mode):
    h, clock = busy_harness
    monkeypatch.setenv('BEHIVE_LLM_STRICT', '1')
    monkeypatch.setenv('BEHIVE_PHASE_DEADLINE', '1030')
    called(h, mode).side_effect = rate_error()
    with pytest.raises(h.llm.PhaseDeadlineExceeded):  # stopped by the deadline, not the busy budget
        invoke(h, mode)
    assert clock.sleeps == [5, 10]
    assert called(h, mode).call_count == 3
    # Calls start at 1000, 1005 and 1015; each ends by deadline - 5 s.
    assert [c.kwargs['timeout'] for c in called(h, mode).call_args_list] == [25, 20, 10]
    assert clock.now <= 1030 - 5 - 10
    h.direct.assert_not_called()


@pytest.mark.parametrize('mode', ['sync', 'async'])
@pytest.mark.parametrize('deadline', ['1014', '1001'])
def test_no_call_starts_inside_the_phase_deadline_margin(busy_harness, monkeypatch, mode, deadline):
    h, clock = busy_harness
    PhaseDeadlineExceeded = h.llm.PhaseDeadlineExceeded  # the harness's own module copy
    monkeypatch.setenv('BEHIVE_LLM_STRICT', '1')
    monkeypatch.setenv('BEHIVE_PHASE_DEADLINE', deadline)
    called(h, mode).side_effect = rate_error()
    with pytest.raises(PhaseDeadlineExceeded):
        invoke(h, mode)
    assert called(h, mode).call_count == 0 and clock.sleeps == []
    h.direct.assert_not_called()


@pytest.mark.parametrize('mode', ['sync', 'async'])
def test_structured_busy_code_survives_message_translation(busy_harness, monkeypatch, mode):
    h, clock = busy_harness
    monkeypatch.setenv('BEHIVE_LLM_STRICT', '1')
    fn = called(h, mode)
    fn.side_effect = [rate_error(message='translated local rejection'), fn.return_value]
    assert invoke(h, mode) == 'OK'
    assert clock.sleeps == [5]


@pytest.mark.parametrize('error', [rate_error(), rate_error('upstream_rate_limit', UPSTREAM)])
def test_gate_never_rearms_busy_budget_or_retries_upstream(monkeypatch, error):
    from behive.engine import llm
    from behive.engine.process import RelevanceFilter
    from test_content_relevance import documents, TOPIC
    calls, sleeps = [], []
    monkeypatch.setenv('BEHIVE_LLM_STRICT', '1')
    # Existing gate-layer backoff must not hide the regression behind real sleep.
    monkeypatch.setattr(RelevanceFilter, 'RATE_LIMIT_RETRY_DELAY_S', 0, raising=False)
    async def sleep(delay):
        sleeps.append(delay)
    def fail(*args, **kwargs):
        calls.append(1)
        raise error
    monkeypatch.setattr(llm, 'complete', fail)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    kept = asyncio.run(RelevanceFilter(TOPIC).filter(documents()))
    assert calls == [1]
    assert sleeps == []
    assert all(d['_relevance_method'] == 'keyword_fallback' for d in kept)
