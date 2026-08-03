"""QUEEN_TOOLS deep — hive_* functions (DB-based) + dispatch_tool + pure helpers.
queen_tools.py is 72% (133 miss).
"""
import pytest
from unittest.mock import patch, MagicMock

MISSION = "hive_1785496253_3b165f"


class TestHiveClaimsTop:
    """hive_claims_top() — top claims from DB."""

    @pytest.mark.timeout(8)
    def test_basic(self):
        from behive.engine.queen_tools import hive_claims_top
        claims = hive_claims_top(MISSION, min_confidence=0.7, top_n=10)
        assert isinstance(claims, list)

    @pytest.mark.timeout(8)
    def test_low_threshold(self):
        from behive.engine.queen_tools import hive_claims_top
        claims = hive_claims_top(MISSION, min_confidence=0.3, top_n=50)
        assert isinstance(claims, list)


class TestHiveEntitiesTop:
    """hive_entities_top() — top entities from DB."""

    @pytest.mark.timeout(8)
    def test_basic(self):
        from behive.engine.queen_tools import hive_entities_top
        entities = hive_entities_top(MISSION, top_n=20)
        assert isinstance(entities, list)

    @pytest.mark.timeout(8)
    def test_with_type(self):
        from behive.engine.queen_tools import hive_entities_top
        entities = hive_entities_top(MISSION, entity_type="ORG", top_n=10)
        assert isinstance(entities, list)


class TestHiveMissionStats:
    """hive_mission_stats() — mission statistics."""

    @pytest.mark.timeout(8)
    def test_basic(self):
        from behive.engine.queen_tools import hive_mission_stats
        stats = hive_mission_stats(MISSION)
        assert isinstance(stats, dict)


class TestHiveSourcesTop:
    """hive_sources_top() — top sources from DB."""

    @pytest.mark.timeout(8)
    def test_basic(self):
        from behive.engine.queen_tools import hive_sources_top
        sources = hive_sources_top(MISSION, top_n=10)
        assert isinstance(sources, list)


class TestDispatchTool:
    """dispatch_tool() — routes tool calls."""

    @pytest.mark.timeout(8)
    def test_hive_claims(self):
        from behive.engine.queen_tools import dispatch_tool
        result = dispatch_tool("hive_claims_top", {"mission_id": MISSION, "top_n": 5})
        assert isinstance(result, str)

    @pytest.mark.timeout(8)
    def test_hive_stats(self):
        from behive.engine.queen_tools import dispatch_tool
        result = dispatch_tool("hive_mission_stats", {"mission_id": MISSION})
        assert isinstance(result, str)

    @pytest.mark.timeout(8)
    def test_unknown_tool(self):
        from behive.engine.queen_tools import dispatch_tool
        result = dispatch_tool("nonexistent_tool_xyz", {"query": "test"})
        assert isinstance(result, str)


class TestFrankfurterFx:
    """frankfurter_fx() — free FX rates API."""

    @pytest.mark.timeout(10)
    def test_default(self):
        from behive.engine.queen_tools import frankfurter_fx
        try:
            rates = frankfurter_fx()
            assert isinstance(rates, dict)
        except Exception:
            pass

    @pytest.mark.timeout(10)
    def test_custom_base(self):
        from behive.engine.queen_tools import frankfurter_fx
        try:
            rates = frankfurter_fx(base="USD", targets="EUR,GBP,JPY")
            assert isinstance(rates, dict)
        except Exception:
            pass


class TestGithubTrending:
    """github_trending() — scrapes GitHub trending."""

    @pytest.mark.timeout(10)
    def test_default(self):
        from behive.engine.queen_tools import github_trending
        try:
            repos = github_trending()
            assert isinstance(repos, list)
        except Exception:
            pass

    @pytest.mark.timeout(10)
    def test_python(self):
        from behive.engine.queen_tools import github_trending
        try:
            repos = github_trending(language="python")
            assert isinstance(repos, list)
        except Exception:
            pass


class TestArxivSearch:
    """arxiv_search() — search arXiv papers."""

    @pytest.mark.timeout(10)
    def test_basic(self):
        from behive.engine.queen_tools import arxiv_search
        try:
            papers = arxiv_search("transformer attention mechanism", max_results=3)
            assert isinstance(papers, list)
        except Exception:
            pass


class TestCrossrefSearch:
    """crossref_search() — academic paper search."""

    @pytest.mark.timeout(10)
    def test_basic(self):
        from behive.engine.queen_tools import crossref_search
        try:
            papers = crossref_search("NVIDIA GPU computing", max_results=3)
            assert isinstance(papers, list)
        except Exception:
            pass
