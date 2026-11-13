"""Wikipedia search must identify itself on every request (diag 2026-11-12, task 3).

Wikimedia answers 403 to requests without a descriptive User-Agent. SearchEngine
passes one shared aiohttp session without default headers, so the header has to be
sent per request, not only when WikipediaBackend opens its own session.

Mock HTTP: a real aiohttp client talks to a real aiohttp server on 127.0.0.1. Only the
transport destination is redirected (test-only ClientSession subclass); the assertion
is on the User-Agent header the server actually received.
"""
import asyncio
import warnings

import aiohttp
import pytest
import yarl
from aiohttp import web

from behive.engine import search_backends as sb

EXPECTED_UA = "BeHive/0.6 (https://api.behive.site; wikipedia-fallback)"


def _redirecting_session(port):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)

        class Redirecting(aiohttp.ClientSession):
            """https://<lang>.wikipedia.org/... -> http://127.0.0.1:<port>/<lang>/..."""

            async def _request(self, method, str_or_url, **kwargs):
                url = yarl.URL(str(str_or_url))
                assert url.scheme == "https" and url.host.endswith(".wikipedia.org"), url
                local = yarl.URL.build(scheme="http", host="127.0.0.1", port=port,
                                       path="/" + url.host.split(".")[0] + url.path)
                return await super()._request(method, local, **kwargs)

    return Redirecting


def _serve(monkeypatch, run_search):
    seen = []

    async def handler(request):
        seen.append({
            "lang": request.match_info["lang"],
            "ua": request.headers.get("User-Agent"),
            "srsearch": request.query.get("srsearch"),
        })
        return web.json_response({"query": {"search": [
            {"title": "Krajowy System Cyberbezpieczeństwa", "snippet": "<b>KSC</b> ustawa"},
        ]}})

    async def scenario():
        app = web.Application()
        app.router.add_get("/{lang}/w/api.php", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            return await run_search(_redirecting_session(port))
        finally:
            await runner.cleanup()

    monkeypatch.setattr(sb.WikipediaBackend, "_cache", {})
    monkeypatch.setattr(sb, "_HEALTH", {})
    monkeypatch.setattr(sb, "_VENDOR_SPACING_SECONDS", 0, raising=False)
    return asyncio.run(scenario()), seen


def test_shared_session_without_default_headers_sends_behive_user_agent(monkeypatch):
    async def run(session_cls):
        engine = sb.SearchEngine.__new__(sb.SearchEngine)
        engine.backends = [sb.WikipediaBackend()]
        # Same shape as SearchEngine's shared session: no default headers at all.
        async with session_cls() as shared:
            return await engine.search("ustawa o krajowym systemie cyberbezpieczeństwa", 5, None, shared, lang="pl")

    results, seen = _serve(monkeypatch, run)
    assert len(seen) == 1
    assert seen[0]["lang"] == "pl"
    assert seen[0]["ua"] == EXPECTED_UA, seen[0]["ua"]
    assert results and results[0]["source"] == "wikipedia"
    assert results[0]["url"].startswith("https://pl.wikipedia.org/wiki/")


def test_shared_session_with_foreign_default_agent_is_overridden_per_request(monkeypatch):
    async def run(session_cls):
        async with session_cls(headers={"User-Agent": "python-requests/2.0"}) as shared:
            return await sb.WikipediaBackend()._search("NIS2 directive", 3, shared, lang="en")

    results, seen = _serve(monkeypatch, run)
    assert [s["ua"] for s in seen] == [EXPECTED_UA]
    assert results[0]["url"].startswith("https://en.wikipedia.org/wiki/")


def test_own_session_path_still_sends_user_agent(monkeypatch):
    async def run(session_cls):
        monkeypatch.setattr(sb.aiohttp, "ClientSession", session_cls)
        return await sb.WikipediaBackend()._search("NIS2 directive", 3, None, lang="en")

    results, seen = _serve(monkeypatch, run)
    assert [s["ua"] for s in seen] == [EXPECTED_UA]
    assert len(results) == 1


@pytest.mark.parametrize("lang,expected", [("pl", "pl"), ("EN-us", "en"), ("../x", "en"), (None, "en")])
def test_language_is_sanitised_before_building_the_host(monkeypatch, lang, expected):
    async def run(session_cls):
        async with session_cls() as shared:
            return await sb.WikipediaBackend()._search(f"query {expected} {lang}", 3, shared, lang=lang)

    _, seen = _serve(monkeypatch, run)
    assert [(s["lang"], s["ua"]) for s in seen] == [(expected, EXPECTED_UA)]
