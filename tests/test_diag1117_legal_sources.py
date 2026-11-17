"""Offline adapters for CELLAR, Sejm ELI and Sejm prints.

Recordings in tests/fixtures/legal/ are status, headers, size and a heading
or metadata body. They are not acts. No request may go to eur-lex.europa.eu.
"""
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from behive.engine import cellar
from behive.engine.legal_sources import (
    LegalFetch,
    celex_from_url,
    eli_from_url,
    fetch_celex_text,
    fetch_eli_act,
    fetch_sejm_print,
    legal_fetch_host,
    sejm_print_from_url,
    url_like_title,
)

FIX = Path(__file__).parent / "fixtures" / "legal"
DOC1 = "https://publications.europa.eu/resource/cellar/9b84d482-85bd-11ed-9887-01aa75ed71a1.0018.03/DOC_1"
BARE = "https://publications.europa.eu/resource/celex/32022L2555"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class _Resp:
    """Stand-in for one urllib response. byte size is len(body), not Content-Length."""

    def __init__(self, status: int, headers: dict, body: bytes, url: str):
        self.status = status
        self.code = status
        self.headers = headers
        self._body = body
        self._url = url

    def read(self, n=-1):
        if n is None or n < 0 or n >= len(self._body):
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:n], self._body[n:]
        return data

    def geturl(self):
        return self._url

    def close(self):
        return None


def _pdf(text: str) -> bytes:
    """Tiny valid PDF. The text is a fixture marker, not an act."""
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET\n".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


@pytest.fixture
def transport(monkeypatch):
    """Spy on every HTTP call the adapter and cellar.py can make."""
    seen = []

    def urlopen_bomb(req, *args, **kwargs):
        url = getattr(req, "full_url", None) or str(req)
        seen.append(url)
        raise AssertionError(f"urlopen leak: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen_bomb)
    monkeypatch.setattr(cellar, "celex_work_uri", lambda celex: "")
    return seen


def _assert_no_eurlex(urls):
    bad = [u for u in urls if "eur-lex.europa.eu" in u]
    assert bad == []


def test_recordings_match_the_live_readings_table():
    bare = _load("celex_32022L2555_bare.json")
    bare_body = (FIX / "celex_32022L2555_bare.body").read_bytes()
    assert bare["status"] == 400
    assert bare["byte_size"] == 205
    assert len(bare_body) == 205
    assert b"Invalid content type CONTENT_STREAM" in bare_body
    assert b"without language" in bare_body
    assert bare["content_type"] is None

    pol = _load("celex_32022L2555_pol_303.json")
    assert pol["status"] == 303
    assert pol["content_type"] == "application/xhtml+xml;charset=UTF-8"
    assert pol["location"] == "http://" + DOC1[len("https://"):]
    assert pol["location"].startswith("http://")

    doc = _load("celex_32022L2555_doc1.json")
    assert doc["status"] == 200
    assert doc["byte_size"] == 732726
    assert doc["content_length"] is None
    assert "xhtml" in doc["content_type"]
    assert "2022/2555" in doc["excerpt"]
    assert len(doc["excerpt"]) < 500

    eli = _load("eli_DU_2026_901.json")
    eli_body = json.loads((FIX / "eli_DU_2026_901.body").read_text(encoding="utf-8"))
    assert eli["status"] == 200
    assert eli["byte_size"] == 1153
    assert len((FIX / "eli_DU_2026_901.body").read_bytes()) == 1153
    assert eli_body["textHTML"] is False
    assert eli_body["textPDF"] is True
    assert eli_body["status"] == "obowiązujący"
    assert eli_body["title"].startswith("Ustawa z dnia 11 czerwca 2026")

    printed = _load("print_term10_2608.json")
    printed_body = json.loads((FIX / "print_term10_2608.body").read_text(encoding="utf-8"))
    assert printed["status"] == 200
    assert printed["byte_size"] == 329
    assert len((FIX / "print_term10_2608.body").read_bytes()) == 329
    assert printed_body["processPrint"] == ["1613"]
    assert printed_body["attachments"] == ["2608.pdf", "V2_242-123.NK.docx"]
    assert printed_body["title"].startswith("Sprawozdanie Komisji")


def test_celex_303_then_200_measures_body_not_content_length(transport, monkeypatch):
    doc = _load("celex_32022L2555_doc1.json")
    pol = _load("celex_32022L2555_pol_303.json")
    shell = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<html xmlns=\"http://www.w3.org/1999/xhtml\"><head>"
        f"<title>{doc['excerpt']}</title></head><body><p>{doc['excerpt']}</p>"
        "</body></html>"
    ).encode("utf-8")
    assert len(shell) < doc["byte_size"]
    body = shell + b" " * (doc["byte_size"] - len(shell))
    assert len(body) == 732726
    calls = []
    wrapped = {"n": 0}
    real = cellar.fetch_celex_html

    def fetch_celex(celex, langs=("POL", "ENG", "ENU"), max_bytes=250_000):
        wrapped["n"] += 1
        wrapped["langs"] = langs
        wrapped["max_bytes"] = max_bytes
        return real(celex, langs=langs, max_bytes=max_bytes)

    def open_once(req, timeout):
        url = req.full_url
        calls.append(url)
        if url.endswith("/32022L2555.POL"):
            return _Resp(
                303,
                {
                    "Location": pol["location"],
                    "Content-Type": pol["content_type"],
                    "Content-Length": "999999",
                },
                b"",
                url,
            )
        if url == "https://" + DOC1[len("https://"):] or url == DOC1:
            return _Resp(
                200,
                {"Content-Type": doc["content_type"], "Content-Length": "1"},
                body,
                url,
            )
        raise AssertionError(f"unexpected publication URL: {url}")

    monkeypatch.setattr(cellar, "fetch_celex_html", fetch_celex)
    monkeypatch.setattr(cellar, "_open_once", open_once)

    result = fetch_celex_text("32022L2555")

    assert wrapped["n"] == 1
    assert wrapped["max_bytes"] >= 732726
    assert result == LegalFetch(
        ok=True,
        source="cellar",
        identifier="32022L2555",
        title=result.title,
        fetch_url=DOC1,
        content_type=doc["content_type"],
        http_status=200,
        byte_size=732726,
        text=result.text,
        reason="",
    )
    assert result.byte_size == len(body)
    assert result.byte_size != 1
    assert "xhtml" in result.content_type
    assert "2022/2555" in result.text
    assert "dyrektywa" in result.text
    assert "cyberbezpiecze" in result.text
    assert result.fetch_url.startswith("https://")
    assert BARE not in calls
    assert not any(u.startswith("http://") for u in calls)
    assert any(u.endswith("/32022L2555.POL") for u in calls)
    assert DOC1 in calls
    _assert_no_eurlex(calls + transport)


def test_bare_work_400_is_a_reason_and_is_not_requested(transport, monkeypatch):
    err = (FIX / "celex_32022L2555_bare.body").read_bytes()
    calls = []

    def open_once(req, timeout):
        url = req.full_url
        calls.append(url)
        return _Resp(400, {"Content-Type": "text/plain", "Content-Length": "999"}, err, url)

    monkeypatch.setattr(cellar, "_open_once", open_once)

    result = fetch_celex_text("32022L2555")

    assert result.ok is False
    assert result.source == "cellar"
    assert result.identifier == "32022L2555"
    assert result.text == ""
    assert result.http_status == 400
    assert result.byte_size == 205
    assert result.byte_size != 999
    assert result.reason.startswith("http_400")
    assert "Invalid content type CONTENT_STREAM" in result.reason
    assert "without language" in result.reason
    assert BARE not in calls
    assert calls
    assert all(u.endswith(".POL") or u.endswith(".ENG") for u in calls)
    _assert_no_eurlex(calls + transport)


def test_fetch_celex_text_does_not_raise_on_transport_failure(transport, monkeypatch):
    def open_once(req, timeout):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(cellar, "_open_once", open_once)
    result = fetch_celex_text("32022L2555")
    assert result.ok is False
    assert result.reason
    assert result.text == ""
    _assert_no_eurlex(transport)


def test_eli_without_html_falls_back_to_pdf_not_text_html(transport, monkeypatch):
    meta = (FIX / "eli_DU_2026_901.body").read_bytes()
    pdf = _pdf("SYNTHETIC-ELI-BODY")
    calls = []

    def http_get(url, headers=None, max_bytes=8_000_000, timeout=20.0):
        calls.append((url, dict(headers or {})))
        if url == "https://api.sejm.gov.pl/eli/acts/DU/2026/901":
            assert "application/json" in (headers or {}).get("Accept", "")
            return {
                "status": 200,
                "url": url,
                "content_type": "application/json",
                "body": meta,
                "byte_size": len(meta),
                "truncated": False,
            }
        if url == "https://api.sejm.gov.pl/eli/acts/DU/2026/901/text.pdf":
            return {
                "status": 200,
                "url": url,
                "content_type": "application/pdf",
                "body": pdf,
                "byte_size": len(pdf),
                "truncated": False,
                "content_length": "1",
            }
        raise AssertionError(f"unexpected ELI URL: {url}")

    monkeypatch.setattr("behive.engine.legal_sources._http_get", http_get)
    result = fetch_eli_act("DU", 2026, 901)

    urls = [u for u, _ in calls]
    assert not any(u.endswith("/text.html") for u in urls)
    assert any(u.endswith("/text.pdf") for u in urls)
    assert result.ok is True
    assert result.source == "eli"
    assert result.identifier == "DU/2026/901"
    assert result.title == (
        "Ustawa z dnia 11 czerwca 2026 r. o zmianie ustawy - "
        "Kodeks postępowania karnego oraz ustawy - Kodeks karny skarbowy"
    )
    assert result.title != "D20260901.pdf"
    assert "pdf" in result.content_type
    assert "html" not in result.content_type.lower()
    assert result.fetch_url.endswith("/text.pdf")
    assert result.byte_size == len(pdf)
    assert result.byte_size != 1
    assert "SYNTHETIC-ELI-BODY" in result.text
    assert result.reason == ""
    _assert_no_eurlex(urls + transport)


def test_print_2608_title_comes_from_json_not_the_filename(transport, monkeypatch):
    meta = (FIX / "print_term10_2608.body").read_bytes()
    recorded = json.loads(meta.decode("utf-8"))
    pdf = _pdf("SYNTHETIC-PRINT-BODY")
    calls = []

    def http_get(url, headers=None, max_bytes=8_000_000, timeout=20.0):
        calls.append((url, dict(headers or {})))
        if url == "https://api.sejm.gov.pl/sejm/term10/prints/2608":
            assert "application/json" in (headers or {}).get("Accept", "")
            return {
                "status": 200,
                "url": url,
                "content_type": "application/json",
                "body": meta,
                "byte_size": len(meta),
                "truncated": False,
            }
        if url == "https://api.sejm.gov.pl/sejm/term10/prints/2608/2608.pdf":
            return {
                "status": 200,
                "url": url,
                "content_type": "application/pdf",
                "body": pdf,
                "byte_size": len(pdf),
                "truncated": False,
            }
        raise AssertionError(f"unexpected print URL: {url}")

    monkeypatch.setattr("behive.engine.legal_sources._http_get", http_get)
    result = fetch_sejm_print(10, "2608")

    assert result.ok is True
    assert result.source == "sejm_print"
    assert result.title == recorded["title"]
    assert result.title != "2608.pdf"
    assert "2608.pdf" not in result.title
    assert "Kodeks karny" in result.title
    assert "NIS" not in result.title
    assert not hasattr(result, "score_relevance")
    assert "pdf" in result.content_type
    assert result.fetch_url.endswith("/2608.pdf")
    assert "SYNTHETIC-PRINT-BODY" in result.text
    assert result.text != recorded["title"]
    _assert_no_eurlex([u for u, _ in calls] + transport)


@pytest.mark.parametrize(
    "url,celex",
    [
        ("https://eur-lex.europa.eu/legal-content/PL/TXT/PDF/?uri=CELEX:32022L2555", "32022L2555"),
        ("https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32022L2555", "32022L2555"),
        ("https://eur-lex.europa.eu/legal-content/PL/TXT/?uri=CELEX%3A32022L2555", "32022L2555"),
        ("http://data.europa.eu/eli/dir/2022/2555/oj", "32022L2555"),
        ("https://eur-lex.europa.eu/eli/dir/2022/2555", "32022L2555"),
        ("https://publications.europa.eu/resource/celex/32022L2555", "32022L2555"),
        ("https://publications.europa.eu/resource/celex/32022L2555.POL", "32022L2555"),
        ("https://publications.europa.eu/resource/celex/32022l2555.ENG", "32022L2555"),
        ("https://eur-lex.europa.eu/search.html", ""),
        ("https://eur-lex.europa.eu/", ""),
        ("https://example.test/no-identifier", ""),
        ("", ""),
    ],
)
def test_celex_from_url_shapes(url, celex):
    assert celex_from_url(url) == celex


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://api.sejm.gov.pl/eli/acts/DU/2026/901", ("DU", 2026, 901)),
        ("https://api.sejm.gov.pl/eli/acts/DU/2026/901/", ("DU", 2026, 901)),
        ("https://eli.gov.pl/api/acts/DU/2026/901", ("DU", 2026, 901)),
        ("https://eli.gov.pl/api/acts/du/2026/20", ("DU", 2026, 20)),
        ("https://api.sejm.gov.pl/sejm/term10/processes/209", None),
        ("https://api.sejm.gov.pl/sejm/term10/prints/2608", None),
        ("https://api.sejm.gov.pl/sejm/term10/prints/2608/2608.pdf", None),
        ("https://example.test/eli/acts/DU/2026/901", None),
        ("", None),
    ],
)
def test_eli_from_url_shapes(url, expected):
    assert eli_from_url(url) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://api.sejm.gov.pl/sejm/term10/prints/2608", (10, "2608")),
        ("https://api.sejm.gov.pl/sejm/term10/prints/2608/2608.pdf", (10, "2608")),
        ("https://api.sejm.gov.pl/sejm/term10/prints/2608.pdf", (10, "2608")),
        ("https://api.sejm.gov.pl/sejm/term10/processes/209", None),
        ("https://api.sejm.gov.pl/eli/acts/DU/2026/901", None),
        ("", None),
    ],
)
def test_sejm_print_from_url_shapes(url, expected):
    assert sejm_print_from_url(url) == expected


@pytest.mark.parametrize(
    "title,url,expected",
    [
        ("", "https://api.sejm.gov.pl/sejm/term10/processes/209", True),
        (
            "https://api.sejm.gov.pl/sejm/term10/processes/209",
            "https://api.sejm.gov.pl/sejm/term10/processes/209",
            True,
        ),
        (
            "api.sejm.gov.pl/sejm/term10/processes/209",
            "https://api.sejm.gov.pl/sejm/term10/processes/209",
            True,
        ),
        ("api.sejm.gov.pl/foo", "https://api.sejm.gov.pl/other", True),
        ("Publications Office", "https://eur-lex.europa.eu/search.html", False),
        (
            "Ustawa z dnia 11 czerwca 2026 r. o zmianie ustawy - Kodeks karny",
            "https://api.sejm.gov.pl/eli/acts/DU/2026/901",
            False,
        ),
    ],
)
def test_url_like_title(title, url, expected):
    assert url_like_title(title, url) is expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://eur-lex.europa.eu/legal-content/PL/TXT/?uri=CELEX:32022L2555", True),
        ("https://publications.europa.eu/resource/celex/32022L2555.POL", True),
        ("https://api.sejm.gov.pl/eli/acts/DU/2026/901", True),
        ("https://eli.gov.pl/api/acts/DU/2026/20", True),
        ("https://sip.lex.pl/orzeczenia/roz-3", False),
        ("https://news.google.com/rss", False),
        ("https://www.sejm.gov.pl/", False),
        ("", False),
    ],
)
def test_legal_fetch_host(url, expected):
    assert legal_fetch_host(url) is expected


def test_http_location_is_rewritten_before_it_is_followed():
    loc = "http://publications.europa.eu/resource/cellar/9b84d482-85bd-11ed-9887-01aa75ed71a1.0018.03/DOC_1"
    assert cellar._https_location(loc) == "https://" + loc[len("http://"):]
    assert cellar._https_location("https://publications.europa.eu/resource/celex/32022L2555.POL").startswith("https://")
