"""Case 8 on the real boundary: private PostgreSQL, real HarvesterBee.run().

A blacklisted eur-lex.europa.eu domain: the CELEX row is harvested through CELLAR (fetcher patched
at the L1 boundary, no network) and gets a hive_content row whose id comes from the sequence;
a plain HTML row of the same blacklisted domain is still groomed; the blacklist row itself is not
touched by the exception; an empty legal fetch leaves consecutive_bad unchanged.
"""
import asyncio
import logging

import pytest

from test_repair1022_support import live_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.pg_harness

LOGGER = "hive2.harvest"
M = "dispatch-pg-mission"

CELEX_URL = "https://eur-lex.europa.eu/legal-content/PL/TXT/PDF/?uri=CELEX:32022L2555"
PLAIN_URL = "https://eur-lex.europa.eu/homepage.html"
LEX_URL = "https://sip.lex.pl/#/document/470296514"
OTHER_URL = "https://other.example.test/page"
CELEX_TITLE = "Dyrektywa (UE) 2022/2555 (dyrektywa NIS 2)"
ACT_TEXT = " ".join(
    f"Artykuł {i}. Podmioty kluczowe i ważne wdrażają środki zarządzania ryzykiem w cyberbezpieczeństwie, "
    f"zgodnie z dyrektywą 2022/2555, w terminie {i + 10} dni od dnia zgłoszenia incydentu."
    for i in range(1, 25)
)
SEQ_START = 700


def _prepare(live_db, monkeypatch, domains):
    """domains: {domain: (blacklisted, consecutive_bad, harvest_count)} rows in hive_domain_reputation."""
    from behive.engine import domain_reputation, harvest

    monkeypatch.setattr(domain_reputation, "_global_rep", None)
    domain_reputation.get_reputation()  # creates hive_domain_reputation in the private schema
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute(f"CREATE SEQUENCE hive_content_seq START {SEQ_START}")
        for dom, (black, bad, count) in domains.items():
            cur.execute(
                "INSERT INTO hive_domain_reputation (domain, blacklisted, blacklist_reason, consecutive_bad, "
                "harvest_count, avg_quality) VALUES (%s,%s,%s,%s,%s,0.77)",
                (dom, black, f"consecutive_bad={bad} (≥3)" if black else "", bad, count),
            )
    monkeypatch.setattr(domain_reputation, "_global_rep", None)  # fresh cache, as a new run would have
    monkeypatch.setattr(harvest, "_get_drone_strategy", lambda mission, dom: {"strategy": "DIRECT"})
    harvest.clear_url_cache()


def _sources(live_db, rows):
    with live_db.conn, live_db.conn.cursor() as cur:
        for i, (url, rel, total) in enumerate(rows, start=1):
            cur.execute(
                "INSERT INTO hive_sources (id, mission_id, url, source_type, score_total, score_relevance, status) "
                "VALUES (%s,%s,%s,'web',%s,%s,'scouted')",
                (i, M, url, total, rel),
            )


def _rep(live_db, domain):
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute(
            "SELECT blacklisted, blacklist_reason, consecutive_bad, harvest_count, avg_quality "
            "FROM hive_domain_reputation WHERE domain = %s", (domain,))
        return cur.fetchone()


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


def _fetchers(monkeypatch, fetch_celex):
    from behive.engine import legal_sources as ls

    calls = []

    def celex(celex, langs=("POL", "ENG")):
        calls.append(celex)
        return fetch_celex(ls, celex)

    monkeypatch.setattr(ls, "fetch_celex_text", celex)
    monkeypatch.setattr(ls, "fetch_eli_act", lambda *a: pytest.fail("ELI fetcher called"))
    monkeypatch.setattr(ls, "fetch_sejm_print", lambda *a: pytest.fail("print fetcher called"))
    return calls


def _no_web(monkeypatch, harvest, seen):
    """A legal row must never reach a generic extractor; a plain row must never get here at all."""
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


def test_blacklisted_eurlex_celex_row_is_harvested_via_cellar_plain_row_is_groomed(live_db, monkeypatch, caplog):
    from behive.engine import harvest

    _prepare(live_db, monkeypatch, {"eur-lex.europa.eu": (True, 3, 8)})
    before = _rep(live_db, "eur-lex.europa.eu")
    _sources(live_db, [(CELEX_URL, 12, 34), (PLAIN_URL, 6, 30), (OTHER_URL, 6, 30)])
    calls = _fetchers(monkeypatch, lambda ls, celex: ls.LegalFetch(
        True, "cellar", celex, CELEX_TITLE, "https://publications.europa.eu/x/DOC_1",
        "application/xhtml+xml", 200, 732726, "<html><body><p>" + ACT_TEXT + "</p></body></html>", ""))
    seen = []
    _no_web(monkeypatch, harvest, seen)
    caplog.set_level(logging.INFO, logger=LOGGER)

    bee = _run(harvest)
    info = _info(caplog)

    # the act is fetched through CELLAR and saved with an id from hive_content_seq (not MAX(id)+1 = 1)
    assert calls == ["32022L2555"]
    rows = _content(live_db)
    celex_rows = [r for r in rows if r[1] == CELEX_URL]
    assert len(celex_rows) == 1
    assert celex_rows[0][0] == SEQ_START
    assert celex_rows[0][2] == "cellar"
    assert celex_rows[0][3] >= 50
    assert _status(live_db)[CELEX_URL] == "done"
    assert f"Harvesting: {CELEX_URL}" in info

    # the plain page of the same blacklisted domain is still removed and logged as today
    assert f"Harvesting: {PLAIN_URL}" not in info
    assert PLAIN_URL not in seen
    assert any(m.startswith(f"skip mission={M} reason=domain_blacklist url={PLAIN_URL} ") for m in info)
    assert _status(live_db)[PLAIN_URL] == "scouted"
    assert bee._skip_counts.get("domain_blacklist") == 1
    # no CELEX line is logged as a blacklist skip
    assert not any("reason=domain_blacklist" in m and CELEX_URL in m for m in info)

    # nothing is un-blacklisted by the exception; no other URL of that domain slipped through
    after = _rep(live_db, "eur-lex.europa.eu")
    assert after[0] is True and after[1] == before[1]
    assert [r[1] for r in rows] == [CELEX_URL]
    assert set(seen) == {OTHER_URL}   # the unrelated domain takes its old path (web, then fallbacks)


def test_empty_legal_fetch_leaves_consecutive_bad_unchanged(live_db, monkeypatch, caplog):
    from behive.engine import harvest

    _prepare(live_db, monkeypatch, {"eur-lex.europa.eu": (False, 2, 8)})
    before = _rep(live_db, "eur-lex.europa.eu")
    _sources(live_db, [(CELEX_URL, 12, 34)])
    _fetchers(monkeypatch, lambda ls, celex: ls.LegalFetch(
        True, "cellar", celex, "", "https://publications.europa.eu/x", "text/html", 202, 0,
        "<html><body></body></html>", ""))
    seen = []
    _no_web(monkeypatch, harvest, seen)
    caplog.set_level(logging.INFO, logger=LOGGER)

    bee = _run(harvest)

    assert any(m.startswith(f"skip mission={M} reason=legal_fetch_empty url={CELEX_URL} ") for m in _info(caplog))
    assert bee._skip_counts == {"legal_fetch_empty": 1}
    assert seen == []
    assert _content(live_db) == []
    after = _rep(live_db, "eur-lex.europa.eu")
    assert after == before, (before, after)          # consecutive_bad 2, not blacklisted, count 8
    assert _status(live_db)[CELEX_URL] == "failed"


def test_blacklisted_domain_stays_blacklisted_after_an_empty_legal_fetch(live_db, monkeypatch):
    from behive.engine import harvest

    _prepare(live_db, monkeypatch, {"eur-lex.europa.eu": (True, 3, 8)})
    before = _rep(live_db, "eur-lex.europa.eu")
    _sources(live_db, [(CELEX_URL, 12, 34)])
    _fetchers(monkeypatch, lambda ls, celex: ls.LegalFetch(
        False, "cellar", celex, "", "", "", 202, 0, "", "http_202"))
    _no_web(monkeypatch, harvest, [])
    _run(harvest)
    assert _rep(live_db, "eur-lex.europa.eu") == before


def test_blacklisted_lex_is_removed_as_a_byok_gap_and_never_fetched(live_db, monkeypatch, caplog):
    from behive.engine import harvest

    _prepare(live_db, monkeypatch, {"sip.lex.pl": (True, 3, 6), "eur-lex.europa.eu": (False, 0, 8)})
    _sources(live_db, [(LEX_URL, 18, 37), (CELEX_URL, 12, 34)])
    _fetchers(monkeypatch, lambda ls, celex: ls.LegalFetch(
        True, "cellar", celex, CELEX_TITLE, "https://publications.europa.eu/x/DOC_1",
        "application/xhtml+xml", 200, 1000, "<p>" + ACT_TEXT + "</p>", ""))
    seen = []
    _no_web(monkeypatch, harvest, seen)
    caplog.set_level(logging.INFO, logger=LOGGER)

    bee = _run(harvest)
    info = _info(caplog)

    lex_lines = [m for m in info if m.startswith("skip ") and LEX_URL in m]
    assert lex_lines == [f"skip mission={M} reason=byok_gap url={LEX_URL} detail=lex_login_required"]
    assert f"Harvesting: {LEX_URL}" not in info
    assert seen == []
    assert bee._skip_counts.get("byok_gap") == 1
    assert bee._skip_counts.get("domain_blacklist", 0) == 0
    assert [r[1] for r in _content(live_db)] == [CELEX_URL]
