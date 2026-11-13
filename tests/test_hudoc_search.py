import io
import json
import urllib.error
from email.message import Message

import pytest

from behive.engine import cellar
from behive.engine.cellar import _hudoc_kw, hudoc_search


class _Response(io.BytesIO):
    def __init__(self, payload, status):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.status = status


def _json_response(monkeypatch, payload, status=200):
    response = _Response(payload, status)
    monkeypatch.setattr(cellar.urllib.request, "urlopen", lambda *a, **kw: response)
    monkeypatch.setattr(cellar, "fetch_hudoc_html", lambda *a, **kw: ("", ""))


# One row from the browser's actual privacy API response (1075 hits).
# This is boundary-test data, not a fallback or evidence of live Python success.
_PRIVACY_ROW = {"columns": {
    "itemid": "001-175121",
    "docname": "CASE OF SATAKUNNAN MARKKINAPÖRSSI OY AND SATAMEDIA OY v. FINLAND",
    "appno": "931/13", "respondent": "FIN", "languageisocode": "ENG",
    "kpdate": "2017-06-27T00:00:00", "doctypebranch": "GRANDCHAMBER",
    "importance": "1", "ecli": "ECLI:CE:ECHR:2017:0627JUD000093113",
}}


def test_hudoc_search_adapts_observed_public_response(monkeypatch):
    _json_response(monkeypatch, {"resultcount": 1075, "results": [_PRIVACY_ROW]})
    doc = hudoc_search("privacy", length=3)
    assert doc is not None
    assert doc["data"] == {"count": 1, "total": 1075, "itemid": "001-175121"}
    assert _PRIVACY_ROW["columns"]["docname"] in doc["raw_text"]


def test_hudoc_search_denies_non_success_status(monkeypatch):
    _json_response(monkeypatch, {"resultcount": 1075, "results": [_PRIVACY_ROW]}, status=403)
    assert hudoc_search("privacy") is None


@pytest.mark.parametrize("total", [None, "1075", -1, True, {}, 0])
def test_hudoc_search_denies_invalid_total(monkeypatch, total):
    _json_response(monkeypatch, {"resultcount": total, "results": [_PRIVACY_ROW]})
    assert hudoc_search("privacy") is None


@pytest.mark.parametrize("body", [b"<title>Just a moment...</title>", b"not JSON"])
def test_hudoc_search_denies_non_json(monkeypatch, body):
    _json_response(monkeypatch, body)
    assert hudoc_search("privacy") is None


def test_hudoc_search_preserves_cloudflare_denial(monkeypatch):
    def denied(request, **kwargs):
        headers = Message()
        headers["Cf-Mitigated"] = "challenge"
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", headers, None)

    monkeypatch.setattr(cellar.urllib.request, "urlopen", denied)
    assert hudoc_search("privacy") is None



@pytest.mark.parametrize("payload", [
    None,
    [],
    {"resultcount": 1, "results": "not rows"},
    {"resultcount": 1, "results": [None]},
    {"resultcount": 1, "results": [{"columns": "not columns"}]},
    {"resultcount": 1, "results": [{"columns": {}}]},
    {"resultcount": 1, "results": [{"columns": {"itemid": "001-175121"}}]},
])
def test_hudoc_search_denies_invalid_remote_schema(monkeypatch, payload):
    _json_response(monkeypatch, payload)
    assert hudoc_search("privacy") is None


def test_hudoc_kw_drops_portal_tokens():
    assert _hudoc_kw("HUDOC ECHR privacy") == "privacy"
    assert _hudoc_kw("echr") == ""


@pytest.mark.live  # needs internet (HUDOC)
def test_hudoc_search_live_privacy():
    doc = hudoc_search("privacy", length=3)
    assert doc is not None
    assert doc["api_name"] == "HUDOC ECHR"
    assert (doc.get("data") or {}).get("total", 0) >= 1
    assert "CASE OF" in (doc.get("raw_text") or "") or "itemid=" in (doc.get("raw_text") or "")
