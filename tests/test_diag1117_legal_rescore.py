"""rescore_legal_source: case 5 of the NIS2 legal-sources design (offline).

A stored host+path title (or an English manifestation of a CELEX act) scores 0 for a
Polish topic. The metadata title from CELLAR/ELI/print JSON goes through the EXISTING
scout scorer. The 5/15 threshold and the cap (relevance < 5 -> total <= 14) stay.
Metadata titles are the ones recorded in the design (section 5); no act text is used.
"""
from pathlib import Path

TOPIC = (
    "ustawa o krajowym systemie cyberbezpieczeństwa po NIS2, firmy produkcyjne jako "
    "podmioty kluczowe lub ważne, obowiązki, terminy, kary"
)
EURLEX_EN = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32022L2555"
EURLEX_EN_HOST_PATH = "eur-lex.europa.eu/legal-content/EN/TXT/"
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
ELI_901 = "https://api.sejm.gov.pl/eli/acts/DU/2026/901"
KPK_TITLE = (
    "Ustawa z dnia 11 czerwca 2026 r. o zmianie ustawy - Kodeks postępowania karnego oraz "
    "ustawy - Kodeks karny skarbowy"
)
PRINT_2608 = "https://api.sejm.gov.pl/sejm/term10/prints/2608/2608.pdf"
PRINT_TITLE = (
    "Sprawozdanie Komisji Nadzwyczajnej do spraw zmian w kodyfikacjach o poselskich "
    "projektach ustaw o zmianie ustawy - Kodeks karny."
)
SCORE_KEYS = {"score_total", "score_relevance", "score_freshness", "score_authority", "score_depth"}


def _scout_score(url, title, snippet, source_type="web"):
    """The existing scout scorer, called the way rescore must call it."""
    from behive.engine.scout import SwarmScout

    scout = SwarmScout.__new__(SwarmScout)
    scout.topic = TOPIC
    return scout._score_source(url, title, snippet, source_type)


def test_host_path_title_with_polish_meta_title_passes_relevance_5():
    from behive.engine.legal_sources import rescore_legal_source

    before = rescore_legal_source(TOPIC, EURLEX_EN, EURLEX_EN_HOST_PATH, "", "")
    after = rescore_legal_source(TOPIC, EURLEX_EN, EURLEX_EN_HOST_PATH, "", PL_TITLE)
    assert SCORE_KEYS <= set(after)
    assert before["score_relevance"] < 5 and before["score_total"] <= 14
    assert after["score_relevance"] >= 5
    assert after["score_total"] >= 15
    assert after["score_total"] > before["score_total"]


def test_empty_meta_title_returns_the_old_scores_unchanged():
    from behive.engine.legal_sources import rescore_legal_source

    got = rescore_legal_source(TOPIC, EURLEX_EN, EURLEX_EN_HOST_PATH, "", "", meta_text="ignored")
    assert got == _scout_score(EURLEX_EN, EURLEX_EN_HOST_PATH, "")
    assert got["score_relevance"] < 5 and got["score_total"] <= 14
    # a title that is NOT url-like is scored as stored when there is no metadata
    got = rescore_legal_source(TOPIC, EURLEX_EN, EN_TITLE, "", "")
    assert got == _scout_score(EURLEX_EN, EN_TITLE, "")


def test_english_manifestation_of_the_same_celex_is_lifted_by_the_polish_title():
    from behive.engine.legal_sources import rescore_legal_source

    old = rescore_legal_source(TOPIC, EURLEX_EN, EN_TITLE, "", "")
    assert old["score_relevance"] == 0 and old["score_total"] <= 14
    new = rescore_legal_source(TOPIC, EURLEX_EN, EN_TITLE, "", PL_TITLE)
    assert new["score_relevance"] >= 5 and new["score_total"] >= 15


def test_url_like_title_is_replaced_by_meta_title_not_appended(monkeypatch):
    """The stored host+path never reaches the scorer as the title; meta_title+meta_text do."""
    from behive.engine import legal_sources
    from behive.engine.scout import SwarmScout

    calls = []
    real = SwarmScout._score_source

    def spy(self, url, title, snippet, source_type):
        calls.append((self.topic, url, title, snippet, source_type))
        return real(self, url, title, snippet, source_type)

    monkeypatch.setattr(SwarmScout, "_score_source", spy)
    legal_sources.rescore_legal_source(TOPIC, EURLEX_EN, EURLEX_EN_HOST_PATH, "snip", PL_TITLE, "first page text")
    assert len(calls) == 1
    topic, url, title, snippet, _ = calls[0]
    assert topic == TOPIC and url == EURLEX_EN and snippet == "snip"
    assert PL_TITLE in title and "first page text" in title
    assert EURLEX_EN_HOST_PATH not in title


def test_criminal_code_titles_are_scored_by_the_scorer_and_not_lifted():
    """901 and 2608 get their real titles. Nothing is forced: the result is exactly what the
    scout scorer says for (url, meta title, snippet), relevance is not above the stored
    production relevance (6 for both), and the cap holds below 5. The scorer matches the topic
    word "ustawa" in any statute title, so these titles do reach relevance 5; that is the
    scorer's answer, not something rescore adds or removes."""
    from behive.engine.legal_sources import rescore_legal_source

    for url, host_path, meta, snippet, old_rel in (
        (ELI_901, "api.sejm.gov.pl/eli/acts/DU/2026/901", KPK_TITLE,
         "o zmianie ustawy - Kodeks postępowania karnego oraz ustawy - Kodeks karny skarbowy", 6),
        (PRINT_2608, "api.sejm.gov.pl/sejm/term10/prints/2608/2608.pdf", PRINT_TITLE, "", 6),
    ):
        got = rescore_legal_source(TOPIC, url, host_path, snippet, meta)
        assert got == _scout_score(url, meta, snippet), url
        assert got["score_relevance"] <= old_rel, (url, got)
        if got["score_relevance"] < 5:
            assert got["score_total"] <= 14, (url, got)


def test_cap_stays_for_any_title_below_relevance_5():
    from behive.engine.legal_sources import rescore_legal_source

    got = rescore_legal_source(TOPIC, ELI_901, "api.sejm.gov.pl/eli/acts/DU/2026/901", "", "Rozporządzenie o kodeksie drogowym")
    assert got["score_relevance"] < 5
    assert got["score_total"] <= 14


def test_threshold_and_cap_are_untouched_in_the_scorer_source():
    """Guard for A3: the scorer keeps its cap line; rescore does not carry its own copy of the scorer."""
    root = Path(__file__).resolve().parents[1] / "src" / "behive" / "engine"
    scout = (root / "scout.py").read_text(encoding="utf-8")
    assert "if score_relevance < 5:\n            total = min(14, total)" in scout
    legal = (root / "legal_sources.py").read_text(encoding="utf-8")
    assert "min(14" not in legal and "FRESHNESS_PATTERNS" not in legal
    harvest = (root / "harvest.py").read_text(encoding="utf-8")
    assert "score_relevance >= 5" in harvest and "score_total >= 15" in harvest

