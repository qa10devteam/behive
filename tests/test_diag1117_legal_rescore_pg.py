"""Case 6 of the NIS2 legal-sources design: rescore before the harvest filter.

Private PostgreSQL (CLAIM_TEST_PG_DSN), real HarvesterBee._load_pending_sources. The
fetchers are patched at their boundary (behive.engine.legal_sources.fetch_*): no network.

A CELEX row with a URL-like title enters the queue after rescore; a /processes/209 row
(no identifier, off-topic JSON) does not; an operator_seed row is untouched and still
loaded; ids never change; a failed, slow or too-late fetch leaves the row as it was and is
logged at INFO with a reason.
"""
import logging
import time

import pytest

from test_repair1022_support import live_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.pg_harness

LOGGER = "hive2.harvest"
M = "rescore-mission"
TOPIC = (
    "ustawa o krajowym systemie cyberbezpieczeństwa po NIS2, firmy produkcyjne jako "
    "podmioty kluczowe lub ważne, obowiązki, terminy, kary"
)
PL_TITLE = (
    "Dyrektywa Parlamentu Europejskiego i Rady (UE) 2022/2555 z dnia 14 grudnia 2022 r. "
    "w sprawie środków na rzecz wysokiego wspólnego poziomu cyberbezpieczeństwa na "
    "terytorium Unii, zmieniająca rozporządzenie (UE) nr 910/2014 i dyrektywę (UE) 2018/1972 "
    "oraz uchylająca dyrektywę (UE) 2016/1148 (dyrektywa NIS 2)"
)
EN_TITLE = (
    "Directive (EU) 2022/2555 of the European Parliament and of the Council of 14 December 2022 "
    "on measures for a high common level of cybersecurity across the Union"
)
KPK_TITLE = (
    "Ustawa z dnia 11 czerwca 2026 r. o zmianie ustawy - Kodeks postępowania karnego oraz "
    "ustawy - Kodeks karny skarbowy"
)
PRINT_TITLE = (
    "Sprawozdanie Komisji Nadzwyczajnej do spraw zmian w kodyfikacjach o poselskich "
    "projektach ustaw o zmianie ustawy - Kodeks karny."
)

CELEX_EN = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32022L2555"
CELEX_PL = "https://eur-lex.europa.eu/legal-content/PL/TXT/PDF/?uri=CELEX:32022L2555"
CELEX_OTHER = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32016L1148"
PROCESS = "https://api.sejm.gov.pl/sejm/term10/processes/209"
ELI_901 = "https://api.sejm.gov.pl/eli/acts/DU/2026/901"
PRINT_2608 = "https://api.sejm.gov.pl/sejm/term10/prints/2608/2608.pdf"
SEED = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32024R2847"
FOREIGN = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32022L2555&from=EN"
DONE = "https://eur-lex.europa.eu/legal-content/DE/TXT/?uri=CELEX:32022L2555"

KPK_SNIPPET = "o zmianie ustawy - Kodeks postępowania karnego oraz ustawy - Kodeks karny skarbowy"
HP = lambda url: url.split("://", 1)[1].split("?", 1)[0]  # noqa: E731  host+path title as scout stored it

# id, mission, url, title, snippet, source_type, total, relevance, status
ROWS = [
    (1, M, CELEX_EN, HP(CELEX_EN), "", "web", 14, 0, "scouted"),
    (2, M, CELEX_PL, "Publications Office", "", "pdf", 14, 0, "scouted"),
    (3, M, CELEX_OTHER, HP(CELEX_OTHER), "", "web", 14, 0, "scouted"),
    (4, M, PROCESS, HP(PROCESS), '{"ELI": "DU/2024/1228", "title": "o zmianie ustawy"}', "web", 14, 0, "scouted"),
    (5, M, ELI_901, HP(ELI_901), KPK_SNIPPET, "web", 28, 6, "scouted"),   # production shape
    (6, M, PRINT_2608, HP(PRINT_2608), "", "pdf", 34, 6, "scouted"),     # production shape
    (7, M, SEED, SEED, "", "operator_seed", 0, 0, "pending"),
    (8, "other-mission", FOREIGN, HP(FOREIGN), "", "web", 14, 0, "scouted"),
    (9, M, DONE, HP(DONE), "", "web", 14, 0, "done"),
]


def _seed(conn):
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO hive_missions (id, topic) VALUES (%s, %s)", (M, TOPIC))
        for r in ROWS:
            cur.execute(
                "INSERT INTO hive_sources (id, mission_id, url, title, snippet, source_type, "
                "score_total, score_relevance, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                r,
            )


def _row(conn, sid):
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, url, title, snippet, source_type, score_total, score_relevance, "
            "score_freshness, score_authority, score_depth, status FROM hive_sources WHERE id = %s",
            (sid,),
        )
        return cur.fetchone()


def _count(conn):
    with conn, conn.cursor() as cur:
        cur.execute("SELECT count(*), max(id) FROM hive_sources")
        return cur.fetchone()


def _info(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]


class Fetchers:
    """Patches the L1 fetchers at their boundary and records every call."""

    def __init__(self, monkeypatch):
        from behive.engine import legal_sources as ls

        self.celex, self.eli, self.sprint = [], [], []

        def celex(celex, langs=("POL", "ENG")):
            self.celex.append(celex)
            if celex == "32022L2555":
                return ls.LegalFetch(True, "cellar", celex, PL_TITLE, "https://publications.europa.eu/x/DOC_1",
                                     "application/xhtml+xml", 200, 732726, "<html/>", "")
            return ls.LegalFetch(False, "cellar", celex, "", "", "", 400, 205, "",
                                 "http_400: Invalid content type CONTENT_STREAM for WORK without language")

        def eli(publisher, year, pos):
            self.eli.append((publisher, year, pos))
            return ls.LegalFetch(False, "eli", f"{publisher}/{year}/{pos}", KPK_TITLE, "", "application/json",
                                 200, 1153, "", "no_pdf")

        def sprint(term, number):
            self.sprint.append((term, number))
            return ls.LegalFetch(False, "sejm_print", f"{term}/{number}", PRINT_TITLE, "", "application/json",
                                 200, 329, "", "no_pdf")

        monkeypatch.setattr(ls, "fetch_celex_text", celex)
        monkeypatch.setattr(ls, "fetch_eli_act", eli)
        monkeypatch.setattr(ls, "fetch_sejm_print", sprint)


def _load(harvest):
    bee = harvest.HarvesterBee(M)
    try:
        return bee._load_pending_sources()
    finally:
        bee.con.close()


def test_celex_row_enters_queue_after_rescore_and_processes_row_does_not(live_db, monkeypatch, caplog):
    from behive.engine import harvest
    from behive.engine.legal_sources import rescore_legal_source

    _seed(live_db.conn)
    before = {r[0]: _row(live_db.conn, r[0]) for r in ROWS}
    count_before = _count(live_db.conn)
    fetch = Fetchers(monkeypatch)
    caplog.set_level(logging.INFO, logger=LOGGER)

    sources = _load(harvest)
    loaded = {s["source_id"] for s in sources}

    # many EUR-Lex URLs of one CELEX: one fetch for that CELEX (cached for the mission)
    assert sorted(fetch.celex) == ["32016L1148", "32022L2555"]
    assert fetch.eli == [("DU", 2026, 901)]
    assert fetch.sprint == [(10, "2608")]

    # the CELEX rows pass 5/15 and are queued; ids and row count did not change
    for sid in (1, 2):
        row = _row(live_db.conn, sid)
        assert row[6] >= 5 and row[5] >= 15, row
        assert sid in loaded
    assert _count(live_db.conn) == count_before
    assert [r[0] for r in (_row(live_db.conn, s[0]) for s in ROWS)] == [r[0] for r in ROWS]

    # title: replaced only when the old title was URL-like; the English title stays
    assert _row(live_db.conn, 1)[2] == PL_TITLE
    assert _row(live_db.conn, 2)[2] == "Publications Office"  # not URL-like: kept
    assert _row(live_db.conn, 5)[2] == KPK_TITLE
    assert _row(live_db.conn, 6)[2] == PRINT_TITLE

    # /processes/209 has no identifier, no fetch, nothing changed, not queued
    assert _row(live_db.conn, 4) == before[4]
    assert 4 not in loaded

    # 901 and 2608: scores are exactly the scorer's answer for the metadata title; nothing is
    # forced, the cap holds, and a row is queued only if it passes 5/15 on its own
    for sid, url, meta in ((5, ELI_901, KPK_TITLE), (6, PRINT_2608, PRINT_TITLE)):
        row = _row(live_db.conn, sid)
        want = rescore_legal_source(TOPIC, url, before[sid][2], before[sid][3], meta, source_type=before[sid][4])
        if sid == 5:
            # a rescore never lowers: the scorer's answer for the title is below 28/6, the row keeps 28/6
            assert (want["score_total"], want["score_relevance"]) < (before[sid][5], before[sid][6]), want
            assert (row[5], row[6]) == (28, 6), row
        else:
            assert (row[5], row[6]) == (want["score_total"], want["score_relevance"])
        assert row[6] <= before[sid][6], row
        if row[6] < 5:
            assert row[5] <= 14, row
        assert (sid in loaded) == (row[6] >= 5 and row[5] >= 15)

    # the failed CELEX (HTTP 400) is left as it was and logged at INFO with the reason
    assert _row(live_db.conn, 3) == before[3]
    assert 3 not in loaded
    lines = [m for m in _info(caplog) if m.startswith("legal_rescore") and CELEX_OTHER in m]
    assert len(lines) == 1 and f"mission={M}" in lines[0] and "reason=http_400" in lines[0], lines

    # operator_seed: not rescored, not fetched, still loaded; other mission and done rows untouched
    assert _row(live_db.conn, 7) == before[7]
    assert 7 in loaded
    assert _row(live_db.conn, 8) == before[8]
    assert _row(live_db.conn, 9) == before[9]
    assert 8 not in loaded and 9 not in loaded


def test_rescore_is_idempotent_and_uses_the_existing_row_id(live_db, monkeypatch):
    from behive.engine import harvest

    _seed(live_db.conn)
    Fetchers(monkeypatch)
    _load(harvest)
    first = [_row(live_db.conn, r[0]) for r in ROWS]
    count_first = _count(live_db.conn)
    _load(harvest)
    assert [_row(live_db.conn, r[0]) for r in ROWS] == first
    assert _count(live_db.conn) == count_first


def test_slow_fetch_is_cut_and_leaves_the_row(live_db, monkeypatch, caplog):
    from behive.engine import harvest
    from behive.engine import legal_sources as ls

    _seed(live_db.conn)
    before = _row(live_db.conn, 1)
    monkeypatch.setattr(harvest, "LEGAL_RESCORE_FETCH_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ls, "fetch_celex_text", lambda celex, langs=("POL", "ENG"): time.sleep(2.0))
    monkeypatch.setattr(ls, "fetch_eli_act", lambda *a: time.sleep(2.0))
    monkeypatch.setattr(ls, "fetch_sejm_print", lambda *a: time.sleep(2.0))
    caplog.set_level(logging.INFO, logger=LOGGER)
    started = time.monotonic()
    _load(harvest)
    assert time.monotonic() - started < 3.0
    assert _row(live_db.conn, 1) == before
    lines = [m for m in _info(caplog) if m.startswith("legal_rescore") and CELEX_EN in m]
    assert len(lines) == 1 and "reason=timeout" in lines[0], lines


def test_phase_deadline_stops_the_fetches_and_leaves_every_row(live_db, monkeypatch, caplog):
    from behive.engine import harvest
    from behive.engine import legal_sources as ls

    _seed(live_db.conn)
    before = {r[0]: _row(live_db.conn, r[0]) for r in ROWS}
    monkeypatch.setenv("BEHIVE_PHASE_DEADLINE", str(time.time() + 3.0))  # inside the margin: no time to start
    for name in ("fetch_celex_text", "fetch_eli_act", "fetch_sejm_print"):
        monkeypatch.setattr(ls, name, lambda *a, **k: pytest.fail("fetch started past the phase deadline"))
    caplog.set_level(logging.INFO, logger=LOGGER)
    sources = _load(harvest)
    assert {r[0]: _row(live_db.conn, r[0]) for r in ROWS} == before
    assert any(m.startswith("legal_rescore") and "reason=phase_deadline" in m for m in _info(caplog))
    # the operator seed is still loaded
    assert 7 in {s["source_id"] for s in sources}
