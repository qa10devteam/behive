"""L7-FIX on the real boundary: private PostgreSQL, real HarvesterBee.run() (5 workers).

One act, one hive_content row per run: the three EUR-Lex URLs of Directive 2022/2555 give one row
and two duplicate skips; the fetcher is patched at the L1 boundary and called once. Same for an
ELI act and a Sejm print. Fixture text is a marker sentence, not an act.
"""
import asyncio
import logging
import time

import pytest

from test_repair1022_support import live_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.pg_harness

LOGGER = "hive2.harvest"
M = "diag1119-pg-mission"
SEQ_START = 900

NIS2 = [
    "https://eur-lex.europa.eu/legal-content/PL/TXT?uri=CELEX:32022L2555",
    "https://eur-lex.europa.eu/legal-content/PL/TXT/PDF?uri=CELEX:32022L2555",
    "https://eur-lex.europa.eu/eli/dir/2022/2555/oj/pol",
]
ELI = ["https://api.sejm.gov.pl/eli/acts/DU/2026/901", "https://eli.gov.pl/api/acts/DU/2026/901"]
PRINT = [
    "https://api.sejm.gov.pl/sejm/term10/prints/2608/2608.pdf",
    "https://api.sejm.gov.pl/sejm/term10/prints/2608",
]
ACT_TEXT = " ".join(
    f"Artykuł {i}. Podmioty kluczowe i ważne wdrażają środki zarządzania ryzykiem w cyberbezpieczeństwie, "
    f"zgodnie z przepisami, w terminie {i + 10} dni od dnia zgłoszenia incydentu."
    for i in range(1, 25)
)


def _prepare(live_db, monkeypatch):
    from behive.engine import domain_reputation, harvest

    monkeypatch.setattr(domain_reputation, "_global_rep", None)
    domain_reputation.get_reputation()
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute(f"CREATE SEQUENCE hive_content_seq START {SEQ_START}")
    monkeypatch.setattr(domain_reputation, "_global_rep", None)
    monkeypatch.setattr(harvest, "_get_drone_strategy", lambda mission, dom: {"strategy": "DIRECT"})
    harvest.clear_url_cache()


def _sources(live_db, urls):
    with live_db.conn, live_db.conn.cursor() as cur:
        for i, url in enumerate(urls, start=1):
            cur.execute(
                "INSERT INTO hive_sources (id, mission_id, url, source_type, score_total, score_relevance, status) "
                "VALUES (%s,%s,%s,'web',34,12,'scouted')", (i, M, url))


def _content(live_db):
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute("SELECT id, url, harvest_method, word_count FROM hive_content WHERE mission_id = %s ORDER BY id",
                    (M,))
        return cur.fetchall()


def _status(live_db):
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute("SELECT url, status FROM hive_sources WHERE mission_id = %s ORDER BY id", (M,))
        return dict(cur.fetchall())


def _info(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]


def _no_web(monkeypatch, harvest, seen):
    async def web(self, url):
        seen.append(url)
        return {"method": "web_curl", "text": "", "word_count": 0, "success": False}

    for name in ("_harvest_web", "_harvest_jina", "_harvest_crawl4ai"):
        monkeypatch.setattr(harvest.HarvesterBee, name, web)
    monkeypatch.setattr(harvest.HarvesterBee, "_harvest_pdf", lambda self, url: seen.append(url) or {})


def _run(harvest):
    bee = harvest.HarvesterBee(M)
    try:
        asyncio.run(bee.run())
    finally:
        harvest.clear_url_cache()
    return bee


def _slow(result):
    """Fetcher body: slow enough that every worker has dispatched before the first one finishes."""
    time.sleep(0.4)
    return result


def _check(live_db, caplog, bee, urls, route, ident):
    info = _info(caplog)
    rows = _content(live_db)
    assert len(rows) == 1, rows                              # one act, one content row
    assert rows[0][0] == SEQ_START and rows[0][2] == route and rows[0][1] in urls
    skips = [m for m in info if m.startswith(f"skip mission={M} reason=cache_skip ")]
    dupes = sorted(u for u in urls if u != rows[0][1])
    assert len(skips) == len(dupes) == len(urls) - 1
    for url in dupes:
        assert f"skip mission={M} reason=cache_skip url={url} detail=duplicate_act:{route}:{ident}" in info
        assert f"Harvesting: {url}" in info                  # reached the dispatch, never fetched
    assert bee._skip_counts == {"cache_skip": len(urls) - 1}
    # the duplicate rows end exactly where an existing cache_skip leaves them
    status = _status(live_db)
    assert status[rows[0][1]] == "done"
    assert all(status[u] == "failed" for u in dupes)


def test_three_urls_of_the_same_directive_give_one_content_row(live_db, monkeypatch, caplog):
    from behive.engine import harvest, legal_sources as ls

    _prepare(live_db, monkeypatch)
    _sources(live_db, NIS2)
    calls = []

    def celex(celex, langs=("POL", "ENG")):
        calls.append(celex)
        return _slow(ls.LegalFetch(True, "cellar", celex, "Dyrektywa (UE) 2022/2555",
                                   "https://publications.europa.eu/x/DOC_1", "application/xhtml+xml", 200, 732726,
                                   "<html><body><p>" + ACT_TEXT + "</p></body></html>", ""))

    monkeypatch.setattr(ls, "fetch_celex_text", celex)
    monkeypatch.setattr(ls, "fetch_eli_act", lambda *a: pytest.fail("ELI fetcher called"))
    monkeypatch.setattr(ls, "fetch_sejm_print", lambda *a: pytest.fail("print fetcher called"))
    seen = []
    _no_web(monkeypatch, harvest, seen)
    caplog.set_level(logging.INFO, logger=LOGGER)

    bee = _run(harvest)

    assert calls == ["32022L2555"]                           # the fetcher is called once
    assert seen == []
    _check(live_db, caplog, bee, NIS2, "cellar", "32022L2555")


def test_two_urls_of_the_same_eli_act_give_one_content_row(live_db, monkeypatch, caplog):
    from behive.engine import harvest, legal_sources as ls

    _prepare(live_db, monkeypatch)
    _sources(live_db, ELI)
    calls = []

    def eli(publisher, year, pos):
        calls.append((publisher, year, pos))
        return _slow(ls.LegalFetch(True, "eli", f"{publisher}/{year}/{pos}", "Ustawa z dnia 11 czerwca 2026 r.",
                                   "https://api.sejm.gov.pl/eli/acts/DU/2026/901/text.pdf", "application/pdf", 200,
                                   1000, ACT_TEXT, ""))

    monkeypatch.setattr(ls, "fetch_eli_act", eli)
    monkeypatch.setattr(ls, "fetch_celex_text", lambda *a, **k: pytest.fail("CELEX fetcher called"))
    monkeypatch.setattr(ls, "fetch_sejm_print", lambda *a: pytest.fail("print fetcher called"))
    _no_web(monkeypatch, harvest, [])
    caplog.set_level(logging.INFO, logger=LOGGER)

    bee = _run(harvest)

    assert calls == [("DU", 2026, 901)]
    _check(live_db, caplog, bee, ELI, "eli", "DU/2026/901")


def test_two_urls_of_the_same_sejm_print_give_one_content_row(live_db, monkeypatch, caplog):
    from behive.engine import harvest, legal_sources as ls

    _prepare(live_db, monkeypatch)
    _sources(live_db, PRINT)
    calls = []

    def sejm_print(term, number):
        calls.append((term, number))
        return _slow(ls.LegalFetch(True, "sejm_print", f"{term}/{number}", "Sprawozdanie Komisji",
                                   "https://api.sejm.gov.pl/sejm/term10/prints/2608/2608.pdf", "application/pdf", 200,
                                   1000, ACT_TEXT, ""))

    monkeypatch.setattr(ls, "fetch_sejm_print", sejm_print)
    monkeypatch.setattr(ls, "fetch_celex_text", lambda *a, **k: pytest.fail("CELEX fetcher called"))
    monkeypatch.setattr(ls, "fetch_eli_act", lambda *a: pytest.fail("ELI fetcher called"))
    _no_web(monkeypatch, harvest, [])
    caplog.set_level(logging.INFO, logger=LOGGER)

    bee = _run(harvest)

    assert calls == [(10, "2608")]
    _check(live_db, caplog, bee, PRINT, "sejm_print", "10/2608")


def test_different_acts_are_all_harvested(live_db, monkeypatch, caplog):
    """The key is (route, identifier): a different act is not a duplicate."""
    from behive.engine import harvest, legal_sources as ls

    _prepare(live_db, monkeypatch)
    other = "https://eur-lex.europa.eu/legal-content/PL/TXT?uri=CELEX:32016R0679"
    _sources(live_db, [NIS2[0], other])
    calls = []

    def celex(celex, langs=("POL", "ENG")):
        calls.append(celex)
        return ls.LegalFetch(True, "cellar", celex, "Akt", "https://publications.europa.eu/x", "text/html", 200,
                             1000, "<html><body><p>" + ACT_TEXT + "</p></body></html>", "")

    monkeypatch.setattr(ls, "fetch_celex_text", celex)
    _no_web(monkeypatch, harvest, [])
    caplog.set_level(logging.INFO, logger=LOGGER)

    bee = _run(harvest)

    assert sorted(calls) == ["32016R0679", "32022L2555"]
    assert len(_content(live_db)) == 2
    assert bee._skip_counts.get("cache_skip", 0) == 0
