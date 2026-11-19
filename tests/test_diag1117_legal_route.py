"""PreScout route_resource: legal acts (CELEX/ELI/print) and LEX as a BYOK gap.

Offline. The EUR-Lex HEAD is the recorded live shape from 2026-11-17: HTTP 202,
text/html, Content-Length 0, no Location (a WAF bot page, not the PDF).
"""
from behive.engine.prescout import classify_domain, route_resource

CELEX_PDF = "https://eur-lex.europa.eu/legal-content/PL/TXT/PDF/?uri=CELEX:32022L2555"
HEAD_202 = {"http_status": 202, "content_type": "text/html", "content_length": 0}


def _route(url: str, head: dict):
    return route_resource(url, head, classify_domain(url))


def test_eurlex_celex_202_routes_to_cellar():
    rtype, decision, flags = _route(CELEX_PDF, HEAD_202)
    assert (rtype, decision) == ("legal_act", "cellar")
    assert "celex:32022L2555" in flags
    assert "thin_content" not in flags


def test_eurlex_celex_202_english_html_url_routes_to_cellar():
    url = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32022L2555"
    rtype, decision, flags = _route(url, HEAD_202)
    assert (rtype, decision) == ("legal_act", "cellar")
    assert "celex:32022L2555" in flags


def test_eurlex_202_without_celex_stays_thin_skip():
    url = "https://eur-lex.europa.eu/search.html?qid=1&text=nis2"
    assert _route(url, HEAD_202) == ("thin_content", "skip", ["thin_content"])


def test_same_202_on_foreign_host_stays_thin_skip():
    url = "https://example.org/legal-content/PL/TXT/PDF/?uri=CELEX:32022L2555"
    assert _route(url, HEAD_202) == ("thin_content", "skip", ["thin_content"])


def test_lookalike_host_does_not_get_cellar():
    url = "https://eur-lex.europa.eu.evil.example/x?uri=CELEX:32022L2555"
    assert _route(url, HEAD_202) == ("thin_content", "skip", ["thin_content"])


def test_eurlex_celex_non_202_is_unchanged():
    ok_pdf = {"http_status": 200, "content_type": "application/pdf", "content_length": 900_000}
    assert _route(CELEX_PDF, ok_pdf) == ("pdf", "pdf_drone", [])
    assert _route(CELEX_PDF, {"http_status": 404, "content_type": "text/html", "content_length": 10}) == (
        "thin_content", "skip", ["http_404"],
    )


def test_lex_sip_is_paywall_byok_gap():
    url = "https://sip.lex.pl/akty-prawne/dzu-dziennik-ustaw/obowiazki-podmiotow-kluczowych-roz-3"
    assert classify_domain(url)["paywall"] is True
    head = {"http_status": 200, "content_type": "text/html", "content_length": 44101}
    rtype, decision, flags = _route(url, head)
    assert (rtype, decision) == ("paywall", "bee_master_needed")
    assert "byok_gap" in flags
    # BeeMaster auto mode skips this flag; it must not be approved as a normal page
    assert "paywall_known" in flags


def test_lex_sip_failed_head_is_still_byok_gap():
    url = "https://sip.lex.pl/akty-prawne/x/470296514"
    rtype, decision, flags = _route(url, {"http_status": None, "head_error": "timeout"})
    assert (rtype, decision) == ("paywall", "bee_master_needed")
    assert "byok_gap" in flags


def test_lex_lookalike_host_is_not_byok_gap():
    url = "https://sip.lex.pl.example.com/x"
    assert "byok_gap" not in _route(url, {"http_status": 200, "content_type": "text/html", "content_length": 50_000})[2]


def test_eli_gov_pl_pdf_keeps_pdf_drone():
    url = "https://eli.gov.pl/api/acts/DU/2026/20/text.pdf"
    head = {"http_status": 200, "content_type": "application/pdf", "content_length": 900_000}
    assert _route(url, head) == ("pdf", "pdf_drone", [])


def test_sejm_print_pdf_keeps_pdf_drone():
    url = "https://api.sejm.gov.pl/sejm/term10/prints/2608/2608.pdf"
    head = {"http_status": 200, "content_type": "application/pdf", "content_length": 250_000}
    assert _route(url, head) == ("pdf", "pdf_drone", [])


def test_sejm_eli_json_keeps_api_bee():
    url = "https://api.sejm.gov.pl/eli/acts/DU/2026/901"
    head = {"http_status": 200, "content_type": "application/json", "content_length": 1153}
    assert _route(url, head) == ("api_endpoint", "api_bee", ["api_available"])
