"""Cases 7, 8 and 9 of the NIS2 legal-sources design: harvest dispatch for legal acts.

Offline. The transport is patched at the L1 boundary (cellar.fetch_celex_html and
legal_sources._http_get / _pdf_text, or the fetch_* functions themselves) and a spy proves that no
request ever goes to eur-lex.europa.eu, that nothing falls back to the web extractor for a legal
URL, that an empty legal fetch never reaches the domain reputation, and that LEX is neither
fetched nor logged into.
"""
import asyncio
import json
import logging
import time

import pytest

LOGGER = "hive2.harvest"
M = "dispatch-mission"

CELEX_URL = "https://eur-lex.europa.eu/legal-content/PL/TXT/PDF/?uri=CELEX:32022L2555"
CELEX_EN_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32022L2555"
ELI_URL = "https://api.sejm.gov.pl/eli/acts/DU/2026/901"
PRINT_URL = "https://api.sejm.gov.pl/sejm/term10/prints/2608/2608.pdf"
PROCESS_URL = "https://api.sejm.gov.pl/sejm/term10/processes/209"
LEX_URL = "https://sip.lex.pl/#/document/470296514"
PLAIN_URL = "https://www.example.test/news/page.html"

CELEX_TITLE = "Dyrektywa Parlamentu Europejskiego i Rady (UE) 2022/2555 (dyrektywa NIS 2)"
ELI_TITLE = "Ustawa z dnia 11 czerwca 2026 r. o zmianie ustawy - Kodeks postępowania karnego"
ACT_TEXT = " ".join(
    f"Artykuł {i}. Podmioty kluczowe i ważne wdrażają środki zarządzania ryzykiem w cyberbezpieczeństwie, "
    f"zgodnie z dyrektywą 2022/2555, w terminie {i + 10} dni od dnia zgłoszenia incydentu."
    for i in range(1, 25)
)
ACT_HTML = f"<html><head><title>{CELEX_TITLE}</title></head><body><p>{ACT_TEXT}</p></body></html>"


def _info(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]


def _is_eurlex(url) -> bool:
    return "eur-lex.europa.eu" in str(url)


class World:
    """Offline stand-ins: no socket is ever opened; every attempt is recorded."""

    def __init__(self, monkeypatch):
        import urllib.request

        from behive.engine import domain_reputation, harvest

        self.harvest = harvest
        self.requests = []        # every URL anything tried to fetch
        self.web_calls = []       # URLs sent to the generic web / pdf / jina / crawl4ai extractors
        self.rep_updates = []     # (url, quality, success) given to domain_reputation.update

        monkeypatch.setattr(harvest, "_get_drone_strategy", lambda mission, dom: {"strategy": "DIRECT"})
        harvest.clear_url_cache()

        def urlopen(req, *a, **k):
            self.requests.append(getattr(req, "full_url", str(req)))
            raise AssertionError(f"network request in an offline test: {self.requests[-1]}")

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)

        world = self

        class _Requests:
            @staticmethod
            def get(url, *a, **k):
                world.requests.append(url)
                raise AssertionError(f"requests.get in an offline test: {url}")

        monkeypatch.setattr(harvest, "_get_requests", lambda: _Requests)

        def spy_async(name):
            async def _spy(self_, url):
                world.web_calls.append((name, url))
                return {"method": name, "text": "", "word_count": 0, "success": False}
            return _spy

        def spy_sync(name):
            def _spy(self_, url):
                world.web_calls.append((name, url))
                return {"method": name, "text": "", "word_count": 0, "success": False}
            return _spy

        for name in ("_harvest_web", "_harvest_jina", "_harvest_crawl4ai", "_harvest_newspaper"):
            monkeypatch.setattr(harvest.HarvesterBee, name, spy_async(name))
        for name in ("_harvest_pdf", "_harvest_document"):
            monkeypatch.setattr(harvest.HarvesterBee, name, spy_sync(name))

        class _Rep:
            def update(self_, url, quality, success=True):
                world.rep_updates.append((url, quality, success))

            def should_skip(self_, url):
                return False

            def flush(self_):
                pass

        rep = _Rep()
        monkeypatch.setattr(domain_reputation, "get_reputation", lambda: rep)

    def bee(self, hints=None, prescout_skip=()):
        bee = object.__new__(self.harvest.HarvesterBee)
        bee.mission_id = M
        bee.results = []
        bee._method_counts = {}
        bee._skip_counts = {}
        bee._routing_hints = dict(hints or {})
        bee._prescout_skip = set(prescout_skip)
        return bee

    def one(self, url, bee=None, **bee_kw):
        bee = bee or self.bee(**bee_kw)
        return asyncio.run(bee._harvest_one({"source_id": 1, "url": url, "source_type": "web"})), bee

    def eurlex_requests(self):
        return [u for u in self.requests if _is_eurlex(u)] + [u for _, u in self.web_calls if _is_eurlex(u)]


@pytest.fixture
def world(monkeypatch):
    w = World(monkeypatch)
    yield w
    w.harvest.clear_url_cache()


# ── route_legal ──────────────────────────────────────────────────────────────

def test_route_legal_names_the_fetcher_or_none():
    from behive.engine.harvest import route_legal

    assert route_legal(CELEX_URL) == "cellar"
    assert route_legal(CELEX_EN_URL) == "cellar"
    assert route_legal("https://publications.europa.eu/resource/celex/32022L2555") == "cellar"
    assert route_legal(ELI_URL) == "eli"
    assert route_legal("https://eli.gov.pl/api/acts/DU/2026/20") == "eli"
    assert route_legal(PRINT_URL) == "sejm_print"
    assert route_legal("https://api.sejm.gov.pl/sejm/term10/prints/2608") == "sejm_print"
    # not an act: no identifier, so no route, even on a legal host
    assert route_legal(PROCESS_URL) is None
    assert route_legal("https://eur-lex.europa.eu/search.html?text=nis2") is None
    assert route_legal("https://eur-lex.europa.eu/homepage.html") is None
    assert route_legal(LEX_URL) is None
    assert route_legal(PLAIN_URL) is None
    assert route_legal("") is None


def test_route_legal_api_json_only_with_the_api_bee_hint():
    from behive.engine.harvest import route_legal

    assert route_legal(PROCESS_URL) is None
    assert route_legal(PROCESS_URL, "api_bee") == "api_json"
    assert route_legal(PROCESS_URL, "standard_drone") is None
    # an act keeps its legal route whatever the hint says
    assert route_legal(CELEX_URL, "api_bee") == "cellar"


# ── case 7: CELEX via CELLAR, never eur-lex.europa.eu ────────────────────────

def test_celex_url_is_fetched_through_cellar_not_eurlex(world, monkeypatch):
    from behive.engine import cellar

    calls = []

    def fetch_celex_html(celex, langs=("POL", "ENG"), max_bytes=0):
        calls.append((celex, tuple(langs)))
        return "https://publications.europa.eu/resource/cellar/9b84d482.0018.03/DOC_1", ACT_HTML

    monkeypatch.setattr(cellar, "fetch_celex_html", fetch_celex_html)
    monkeypatch.setattr(cellar, "_last_http", {
        "status": 200, "url": "https://publications.europa.eu/resource/cellar/9b84d482.0018.03/DOC_1",
        "content_type": "application/xhtml+xml;charset=UTF-8", "byte_size": 732726, "error": "",
    }, raising=False)

    result, bee = world.one(CELEX_URL)

    assert calls == [("32022L2555", ("POL", "ENG"))]
    assert result["method"] == "cellar"
    assert result["success"] is True
    assert result["url"] == CELEX_URL
    assert result["title"] == CELEX_TITLE
    assert result["word_count"] >= 50 and result["word_count"] == len(result["text"].split())
    assert "2022/2555" in result["text"] and "<p>" not in result["text"]   # text, not markup
    # nothing went to EUR-Lex, and the generic web path was never taken
    assert world.eurlex_requests() == []
    assert world.requests == []
    assert world.web_calls == []
    assert bee._method_counts == {"cellar": 1}
    assert [(u, s) for u, _, s in world.rep_updates] == [(CELEX_URL, True)]


def test_cellar_prescout_decision_is_not_a_prescout_skip(world, monkeypatch):
    """L2 routes an EUR-Lex 202 + CELEX to legal_act/cellar: the hint is 'cellar', not 'skip'."""
    from behive.engine import legal_sources as ls

    monkeypatch.setattr(ls, "fetch_celex_text", lambda celex, langs=("POL", "ENG"): ls.LegalFetch(
        True, "cellar", celex, CELEX_TITLE, "https://publications.europa.eu/x/DOC_1",
        "application/xhtml+xml", 200, 732726, "<p>" + ACT_TEXT + "</p>", ""))
    result, bee = world.one(CELEX_EN_URL, hints={CELEX_EN_URL: "cellar"})
    assert result["method"] == "cellar" and result["success"] is True
    assert bee._skip_counts == {}


def test_dispatch_runs_before_the_extension_branch(world, monkeypatch):
    """.../PDF/?uri=CELEX:… and …/prints/2608/2608.pdf must not reach _harvest_pdf."""
    from behive.engine import legal_sources as ls

    monkeypatch.setattr(ls, "fetch_sejm_print", lambda term, number: ls.LegalFetch(
        True, "sejm_print", f"{term}/{number}", "Sprawozdanie Komisji", "https://api.sejm.gov.pl/x/2608.pdf",
        "application/pdf", 200, 4096, ACT_TEXT, ""))
    result, _ = world.one(PRINT_URL)
    assert result["method"] == "sejm_print" and result["success"] is True
    assert result["title"] == "Sprawozdanie Komisji"
    assert ls.sejm_print_from_url(PRINT_URL) == (10, "2608")
    assert world.web_calls == [] and world.requests == []


# ── case 8: ELI act, textHTML=false → PDF ────────────────────────────────────

def test_eli_act_without_html_falls_back_to_pdf(world, monkeypatch):
    from behive.engine import legal_sources as ls

    meta = {"title": ELI_TITLE, "textHTML": False, "textPDF": True, "status": "obowiązujący"}
    seen = []

    def http_get(url, headers=None, max_bytes=ls.PDF_MAX_BYTES, timeout=20.0):
        seen.append(url)
        if url.endswith("/DU/2026/901"):
            body = json.dumps(meta).encode()
            return {"status": 200, "url": url, "content_type": "application/json", "body": body,
                    "byte_size": len(body), "truncated": False}
        if url.endswith("/DU/2026/901/text.pdf"):
            body = b"%PDF-1.4 recorded"
            return {"status": 200, "url": url, "content_type": "application/pdf", "body": body,
                    "byte_size": len(body), "truncated": False}
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr(ls, "_http_get", http_get)
    monkeypatch.setattr(ls, "_pdf_text", lambda pdf_bytes: ACT_TEXT)

    result, bee = world.one(ELI_URL)

    assert seen == [
        "https://api.sejm.gov.pl/eli/acts/DU/2026/901",
        "https://api.sejm.gov.pl/eli/acts/DU/2026/901/text.pdf",
    ]
    assert result["method"] == "eli"
    assert result["success"] is True
    assert result["title"] == ELI_TITLE
    assert result["word_count"] >= 50
    assert world.eurlex_requests() == [] and world.web_calls == []


# ── case 9: LEX is a BYOK gap, never fetched, never logged into ──────────────

def test_lex_logs_byok_gap_once_and_does_not_fetch(world, monkeypatch, caplog):
    from behive.engine import legal_sources as ls

    for name in ("fetch_celex_text", "fetch_eli_act", "fetch_sejm_print"):
        monkeypatch.setattr(ls, name, lambda *a, **k: pytest.fail("legal fetcher called for LEX"))
    caplog.set_level(logging.INFO, logger=LOGGER)

    result, bee = world.one(LEX_URL)

    lines = [m for m in _info(caplog) if m.startswith("skip ")]
    assert lines == [f"skip mission={M} reason=byok_gap url={LEX_URL} detail=lex_login_required"]
    assert bee._skip_counts == {"byok_gap": 1}
    assert result["success"] is False and result["method"] == "byok_gap"
    assert result["text"] == "" and result["word_count"] == 0
    # no GET, no POST (login), no extractor, no fallback chain, no reputation row
    assert world.requests == [] and world.web_calls == []
    assert world.rep_updates == []


# ── empty legal fetch: logged, never a bad result for the domain ─────────────

@pytest.mark.parametrize("fetch", [
    # the WAF / CELLAR refusal: nothing came back
    lambda ls, celex: ls.LegalFetch(False, "cellar", celex, "", "", "", 400, 205, "",
                                    "http_400: Invalid content type CONTENT_STREAM for WORK without language"),
    # a "successful" reply that holds no words (bot page shell)
    lambda ls, celex: ls.LegalFetch(True, "cellar", celex, "", "https://publications.europa.eu/x", "text/html",
                                    202, 0, "<html><body></body></html>", ""),
])
def test_empty_legal_fetch_is_logged_and_leaves_reputation_alone(world, monkeypatch, caplog, fetch):
    from behive.engine import legal_sources as ls

    monkeypatch.setattr(ls, "fetch_celex_text", lambda celex, langs=("POL", "ENG"): fetch(ls, celex))
    caplog.set_level(logging.INFO, logger=LOGGER)

    result, bee = world.one(CELEX_URL)

    lines = [m for m in _info(caplog) if m.startswith("skip ")]
    assert len(lines) == 1
    assert lines[0].startswith(f"skip mission={M} reason=legal_fetch_empty url={CELEX_URL} detail=")
    assert bee._skip_counts == {"legal_fetch_empty": 1}
    assert result["success"] is False and result["word_count"] == 0
    assert result["method"] == "cellar"
    assert world.rep_updates == []            # consecutive_bad is never pushed by a WAF reply
    assert world.web_calls == []              # no jina / crawl4ai / web retry through a third party
    assert world.eurlex_requests() == []


def test_fetcher_exception_is_an_empty_fetch_not_a_bad_result(world, monkeypatch, caplog):
    from behive.engine import legal_sources as ls

    def boom(celex, langs=("POL", "ENG")):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(ls, "fetch_celex_text", boom)
    caplog.set_level(logging.INFO, logger=LOGGER)
    result, bee = world.one(CELEX_URL)
    assert result["success"] is False
    assert bee._skip_counts == {"legal_fetch_empty": 1}
    assert world.rep_updates == []


# ── phase deadline: work is deferred, not failed ─────────────────────────────

def test_phase_deadline_defers_the_legal_fetch(world, monkeypatch):
    from behive.engine import legal_sources as ls

    monkeypatch.setenv("BEHIVE_PHASE_DEADLINE", str(time.time() + 3.0))  # inside the margin
    monkeypatch.setattr(ls, "fetch_celex_text", lambda *a, **k: pytest.fail("fetch started past the deadline"))
    result, bee = world.one(CELEX_URL)
    assert result["success"] is False
    assert result.get("deferred") is True
    assert result["error"] == "phase_deadline"
    assert world.rep_updates == []
    assert bee._skip_counts == {}


def test_slow_legal_fetch_is_cut(world, monkeypatch):
    from behive.engine import legal_sources as ls

    monkeypatch.setattr(world.harvest, "LEGAL_FETCH_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ls, "fetch_celex_text", lambda celex, langs=("POL", "ENG"): time.sleep(2.0))
    started = time.monotonic()
    result, bee = world.one(CELEX_URL)
    assert time.monotonic() - started < 1.5
    assert result["success"] is False
    assert bee._skip_counts == {"legal_fetch_empty": 1}
    assert world.rep_updates == []


# ── what must not change ─────────────────────────────────────────────────────

def test_plain_html_still_takes_the_web_path_and_counts_for_reputation(world):
    result, bee = world.one(PLAIN_URL)
    assert [name for name, _ in world.web_calls][0] == "_harvest_web"
    assert result["success"] is False                 # the spy returned nothing
    assert [(u, s) for u, _, s in world.rep_updates] == [(PLAIN_URL, False)]
    assert bee._skip_counts == {}


def test_non_act_on_a_legal_host_takes_the_old_path(world):
    result, _ = world.one(PROCESS_URL)
    assert [name for name, _ in world.web_calls][0] == "_harvest_web"


def test_api_bee_json_is_stored_as_text_not_through_the_html_extractor(world, monkeypatch):
    body = json.dumps({"number": 209, "title": "projekt ustawy o krajowym systemie cyberbezpieczeństwa",
                       "stages": [{"name": f"etap {i}", "date": f"2026-01-{i:02d}"} for i in range(1, 29)]})

    class _Resp:
        status_code = 200
        text = body
        headers = {"Content-Type": "application/json"}

        @staticmethod
        def raise_for_status():
            return None

    seen = []

    class _Requests:
        @staticmethod
        def get(url, **kw):
            seen.append((url, dict(kw.get("headers") or {})))
            return _Resp()

    monkeypatch.setattr(world.harvest, "_get_requests", lambda: _Requests)
    result, _ = world.one(PROCESS_URL, hints={PROCESS_URL: "api_bee"})
    assert seen and seen[0][0] == PROCESS_URL and seen[0][1].get("Accept") == "application/json"
    assert result["method"] == "api_json"
    assert json.loads(result["text"])["number"] == 209
    assert world.web_calls == []


def test_prescout_skip_still_wins_for_a_plain_url(world, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    result, bee = world.one(PLAIN_URL, prescout_skip={PLAIN_URL})
    assert result["method"] == "prescout_skip"
    assert bee._skip_counts == {"prescout_skip": 1}
    assert world.web_calls == []
