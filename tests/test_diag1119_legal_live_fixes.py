"""L7-FIX, offline: CELEX suffix in parentheses, CELLAR request for such an id, rescore never lowers.

Recordings in tests/fixtures/legal/ (status, headers, size; no act text) come from the live
measurement of 2026-11-19. No request may go to eur-lex.europa.eu.
"""
import contextlib
import json
import urllib.request
from pathlib import Path

import pytest

from behive.engine import cellar
from behive.engine import legal_sources as ls
from behive.engine.legal_sources import celex_from_url, fetch_celex_text

FIX = Path(__file__).parent / "fixtures" / "legal"
SUFFIXED = "52023XC0914(01)"
EURLEX_SUFFIXED = "https://eur-lex.europa.eu/legal-content/PL/TXT/HTML?uri=CELEX%3A52023XC0914%2801%29"
CELLAR_RAW = "https://publications.europa.eu/resource/celex/52023XC0914(01).POL"
TOPIC = (
    "ustawa o krajowym systemie cyberbezpieczeństwa po NIS2, firmy produkcyjne jako "
    "podmioty kluczowe lub ważne, obowiązki, terminy, kary"
)
PL_TITLE = (
    "Dyrektywa Parlamentu Europejskiego i Rady (UE) 2022/2555 z dnia 14 grudnia 2022 r. "
    "w sprawie środków na rzecz wysokiego wspólnego poziomu cyberbezpieczeństwa na "
    "terytorium Unii (dyrektywa NIS 2)"
)


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# ── 1a. identifier with a parenthesised suffix ────────────────────────────────────────────

@pytest.mark.parametrize("url, expected", [
    (EURLEX_SUFFIXED, SUFFIXED),
    ("https://eur-lex.europa.eu/legal-content/EN/TXT/HTML?uri=CELEX%3A52023XC0914%2801%29", SUFFIXED),
    ("https://eur-lex.europa.eu/legal-content/PL/TXT/HTML/?uri=CELEX:52023XC0914(01)", SUFFIXED),
    ("https://eur-lex.europa.eu/legal-content/PL/TXT/HTML/?uri=CELEX%3A52023xc0914%2801%29&from=PL", SUFFIXED),
    ("https://publications.europa.eu/resource/celex/52023XC0914(01)", SUFFIXED),
    ("https://publications.europa.eu/resource/celex/52023XC0914%2801%29.POL", SUFFIXED),
    ("https://eur-lex.europa.eu/legal-content/PL/TXT/?uri=CELEX:32016R0679R(02)", "32016R0679R(02)"),
    ("https://eur-lex.europa.eu/legal-content/PL/TXT/?uri=CELEX:52023XC0914(01)R(01)", "52023XC0914(01)R(01)"),
])
def test_celex_from_url_keeps_the_parenthesised_suffix(url, expected):
    assert celex_from_url(url) == expected


@pytest.mark.parametrize("url, expected", [
    ("https://eur-lex.europa.eu/legal-content/PL/TXT?uri=CELEX:32022L2555", "32022L2555"),
    ("https://eur-lex.europa.eu/legal-content/PL/TXT/PDF?uri=CELEX%3A32022L2555", "32022L2555"),
    ("https://eur-lex.europa.eu/eli/dir/2022/2555/oj/pol", "32022L2555"),
    ("https://eur-lex.europa.eu/legal-content/EN/TXT/HTML?qid=1744621941964&uri=CELEX%3A02022L2555", ""),
    ("https://eur-lex.europa.eu/legal-content/PL/TXT/HTML?uri=OJ%3AL%3A2022%3A333%3AFULL", ""),
])
def test_celex_from_url_without_a_suffix_is_unchanged(url, expected):
    assert celex_from_url(url) == expected


# ── 1b. the CELLAR request for a suffixed id ──────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body, url, ctype=""):
        self.status = status
        self.code = status
        self.headers = {"Content-Type": ctype} if ctype else {}
        self._body = body
        self._url = url

    def read(self, n=-1):
        data, self._body = self._body, b""
        return data

    def geturl(self):
        return self._url

    def close(self):
        return None


def _shell(heading):
    """A synthetic XHTML shell (heading only, no act text), over the 400-char floor."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
        f"<title>{heading}</title></head><body><h1>{heading}</h1><p>" + "x " * 300 + "</p></body></html>"
    ).encode("utf-8")


def test_cellar_gets_the_suffixed_id_with_unencoded_parentheses(monkeypatch):
    rec_pol = _load("celex_52023XC0914_01_pol_raw_404.json")
    rec_enc = _load("celex_52023XC0914_01_pol_encoded_404.json")
    work = _load("sparql_52023XC0914.json")["bindings"][0]["work"]
    assert rec_pol["status"] == 404 and rec_enc["status"] == 404
    assert "%28" in rec_enc["url"] and "%28" not in rec_pol["url"]
    pol_body = (FIX / "celex_52023XC0914_01_pol_raw_404.body").read_bytes()
    enc_body = (FIX / "celex_52023XC0914_01_pol_encoded_404.body").read_bytes()

    seen, work_asked = [], []

    def urlopen_bomb(req, *a, **k):
        seen.append(getattr(req, "full_url", str(req)))
        raise AssertionError(f"urlopen leak: {seen[-1]}")

    def open_once(req, timeout):
        url = req.full_url
        seen.append(url)
        if url == rec_pol["url"]:
            return _Resp(404, pol_body, url)
        if url == rec_enc["url"]:               # CELLAR takes percent-encoded parentheses literally
            return _Resp(404, enc_body, url)
        if url.endswith(".ENG") and "(01)" in url:
            return _Resp(404, pol_body.replace(b".POL", b".ENG"), url)
        if url == work.replace("http://", "https://", 1):   # CELLAR Location is http://, fetched over https
            return _Resp(200, _shell("Komunikat Komisji"), url, "application/xhtml+xml;charset=UTF-8")
        raise AssertionError(f"unexpected publication URL: {url}")

    def work_uri(celex):
        work_asked.append(celex)
        return work if celex == SUFFIXED else ""

    monkeypatch.setattr(urllib.request, "urlopen", urlopen_bomb)
    monkeypatch.setattr(cellar, "_open_once", open_once)
    monkeypatch.setattr(cellar, "celex_work_uri", work_uri)

    got = fetch_celex_text(celex_from_url(EURLEX_SUFFIXED))

    assert seen[0] == CELLAR_RAW                      # id with its suffix, parentheses unencoded
    assert not any("%28" in u or "%29" in u for u in seen)
    assert not any("eur-lex.europa.eu" in u for u in seen)
    assert work_asked == [SUFFIXED]                   # the work is looked up by the full id
    assert got.ok and got.identifier == SUFFIXED and got.title == "Komunikat Komisji"


def test_cellar_failure_for_a_suffixed_id_names_the_full_id(monkeypatch):
    pol_body = (FIX / "celex_52023XC0914_01_pol_raw_404.body").read_bytes()
    urls = []

    def open_once(req, timeout):
        urls.append(req.full_url)
        return _Resp(404, pol_body, req.full_url)

    monkeypatch.setattr(cellar, "_open_once", open_once)
    monkeypatch.setattr(cellar, "celex_work_uri", lambda celex: "")
    got = fetch_celex_text(celex_from_url(EURLEX_SUFFIXED), langs=("POL",))
    assert urls == [CELLAR_RAW]
    assert not got.ok and got.identifier == SUFFIXED
    assert got.reason.startswith("http_404") and "52023XC0914(01)" in got.reason


# ── 3. the rescore never lowers a row ─────────────────────────────────────────────────────

URL_PDF = "https://eur-lex.europa.eu/legal-content/PL/TXT/PDF?uri=CELEX%3A32022L2555"
HOST_PATH = "eur-lex.europa.eu/legal-content/PL/TXT/PDF"


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _ReadCon:
    def __init__(self, source_rows):
        self.source_rows = source_rows

    def execute(self, sql, params=None):
        if "FROM hive_missions" in sql:
            return _Cur([(TOPIC,)])
        return _Cur(self.source_rows)


class _WriteCon:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append((sql, list(params or [])))


def _bee(source_rows, sink, monkeypatch, meta_title=PL_TITLE):
    from behive.engine import harvest

    bee = harvest.HarvesterBee.__new__(harvest.HarvesterBee)
    bee.mission_id = "m-rescore"
    bee.con = _ReadCon(source_rows)

    @contextlib.contextmanager
    def write_con():
        yield _WriteCon(sink)

    bee._write_con = write_con
    monkeypatch.setattr(bee, "_legal_meta_fetch", lambda kind, ident: (meta_title, ""))
    return bee


def _row(sid, url, title, total, rel, fresh=7, auth=9, depth=3):
    return (sid, url, title, "", "web", total, rel, fresh, auth, depth)


def _info_line(caplog, prefix):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


def test_rescore_keeps_12_34_when_the_new_score_would_be_5_27(monkeypatch, caplog):
    import logging

    sink = []
    bee = _bee([_row(1, URL_PDF, "Dyrektywa NIS2 podmioty", 34, 12)], sink, monkeypatch)
    monkeypatch.setattr(ls, "rescore_legal_source", lambda *a, **k: {
        "score_total": 27, "score_relevance": 5, "score_freshness": 7, "score_authority": 9, "score_depth": 3})
    caplog.set_level(logging.INFO)
    bee._rescore_legal_sources()
    assert sink == []                                  # nothing written: 12/34 stays
    line = _info_line(caplog, "legal_rescore mission=")[-1]
    assert "rescored=0" in line and "unchanged=1" in line


def test_rescore_with_a_real_scorer_drop_keeps_the_old_scores(monkeypatch, caplog):
    """The existing scorer itself lowers this row (freshness diluted by the appended title)."""
    import logging

    title = "NIS2 ustawa o krajowym systemie cyberbezpieczeństwa podmioty kluczowe"
    old = ls.rescore_legal_source(TOPIC, URL_PDF, title, "", "")
    new = ls.rescore_legal_source(TOPIC, URL_PDF, title, "", PL_TITLE)
    assert new["score_total"] < old["score_total"]     # premise: the real scorer lowers it
    sink = []
    bee = _bee([_row(1, URL_PDF, title, old["score_total"], old["score_relevance"],
                     old["score_freshness"], old["score_authority"], old["score_depth"])], sink, monkeypatch)
    caplog.set_level(logging.INFO)
    bee._rescore_legal_sources()
    assert sink == []
    assert "unchanged=1" in _info_line(caplog, "legal_rescore mission=")[-1]


def test_rescore_still_raises_a_row_and_replaces_a_url_like_title(monkeypatch, caplog):
    import logging

    sink = []
    bee = _bee([_row(1, URL_PDF, HOST_PATH, 14, 0, 8)], sink, monkeypatch)
    caplog.set_level(logging.INFO)
    bee._rescore_legal_sources()
    assert len(sink) == 1
    params = sink[0][1]
    assert params[0] == PL_TITLE[:512] and params[1] > 14 and params[2] >= 5
    line = _info_line(caplog, "legal_rescore mission=")[-1]
    assert "rescored=1" in line and "unchanged=0" in line


def test_rescore_lower_score_keeps_old_scores_but_may_replace_a_url_like_title(monkeypatch, caplog):
    import logging

    sink = []
    bee = _bee([_row(1, URL_PDF, HOST_PATH, 34, 12, 7, 9, 3)], sink, monkeypatch)
    monkeypatch.setattr(ls, "rescore_legal_source", lambda *a, **k: {
        "score_total": 27, "score_relevance": 5, "score_freshness": 7, "score_authority": 9, "score_depth": 3})
    caplog.set_level(logging.INFO)
    bee._rescore_legal_sources()
    assert len(sink) == 1
    assert sink[0][1][:6] == [PL_TITLE[:512], 34, 12, 7, 9, 3]     # new title, old scores
    line = _info_line(caplog, "legal_rescore mission=")[-1]
    assert "rescored=0" in line and "unchanged=1" in line
