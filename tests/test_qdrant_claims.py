import pytest

from behive.engine.qdrant_claims import collection_stats, semantic_search

pytestmark = pytest.mark.live  # needs the production Qdrant collection


def test_qdrant_collection_live():
    st = collection_stats()
    assert st.get("ok") is True
    assert st.get("points", 0) >= 1000
    assert st.get("vector_size") == 384


def test_semantic_search_mercosur_ethanol():
    r = semantic_search("EU Mercosur tariff quota ethanol", top_k=3)
    assert r.get("ok") is True
    assert r.get("count", 0) >= 1
    top = (r.get("hits") or [{}])[0]
    assert top.get("score", 0) >= 0.5
    text = (top.get("claim") or "").lower()
    assert "ethanol" in text or "mercosur" in text or "quota" in text
