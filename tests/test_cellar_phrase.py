import pytest

from behive.engine.cellar import _bif_arg, _safe_kw, search_cellar


def test_bif_arg_word_and_phrase():
    assert _bif_arg(["gdpr"]) == "\"'gdpr'\""
    assert _bif_arg(["ochrona", "danych"]) == "'\"ochrona danych\"'"


def test_safe_kw_splits_eurlex_hyphen():
    assert _safe_kw("EUR-Lex General Data Protection Regulation") == "general data protection regulation"


@pytest.mark.live  # needs internet (EUR-Lex CELLAR SPARQL)
def test_search_cellar_celex_first():
    hits = search_cellar("32016R0679")
    assert hits
    assert any((h.get("celex") or "").startswith("32016R0679") for h in hits)


@pytest.mark.live  # needs internet (EUR-Lex CELLAR SPARQL)
def test_search_cellar_phrase_finds_gdpr_act():
    hits = search_cellar("General Data Protection Regulation")
    celexes = [h.get("celex") for h in hits]
    assert "32016R0679" in celexes, celexes[:8]


@pytest.mark.live  # needs internet (EUR-Lex CELLAR SPARQL)
def test_search_cellar_pl_phrase_nonzero():
    hits = search_cellar("ochrona danych osobowych")
    assert len(hits) >= 1
    assert all(h.get("celex") for h in hits)
