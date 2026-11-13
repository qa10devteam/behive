import pytest

from behive.engine.cellar import curia_search, case_to_celex


@pytest.mark.live  # needs internet (CURIA / CELLAR)
def test_curia_still_maps_signature():
    assert case_to_celex("C-128/21") == "62021CJ0128"
    doc = curia_search("C-128/21")
    assert doc is not None
    assert doc["api_name"] == "CURIA CJEU"
    text = doc.get("raw_text") or ""
    assert "62021CJ0128" in text or "C-128/21" in text or "101" in text


@pytest.mark.live  # needs internet (CURIA / CELLAR)
def test_curia_phrase_without_signature():
    doc = curia_search("CURIA Article 101 TFEU")
    assert doc is not None
    hits = (doc.get("data") or {}).get("hits") or []
    assert hits, "expected CELEX 6… hits from title search"
    assert all(str(h.get("celex") or "").startswith("6") for h in hits)


def test_curia_portal_only_is_none():
    assert curia_search("CURIA InfoCuria TSUE") is None
