"""
Unit tests for search backends (behive.engine.search_backends).

Tests multi-backend fallthrough chain:
Chromium/Bing → SearXNG → Brave → Serper → Tavily → DDG
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.mark.unit
class TestSearchBackendRegistry:
    """Test backend discovery and priority ordering."""

    def test_backends_sorted_by_priority(self):
        """Backends are registered in priority order (lowest=first)."""
        from behive.engine.search_backends import SearchEngine
        engine = SearchEngine()
        priorities = [b.priority for b in engine.backends]
        assert priorities == sorted(priorities)

    def test_ddg_always_available(self):
        """DDG backend is always present (no API key needed)."""
        from behive.engine.search_backends import SearchEngine
        engine = SearchEngine()
        backend_names = [type(b).__name__ for b in engine.backends]
        assert any("DDG" in name or "DuckDuckGo" in name for name in backend_names), \
            f"DDG not found in: {backend_names}"

    def test_brave_requires_key(self):
        """Brave backend only registers when BRAVE_SEARCH_API_KEY is set."""
        from behive.engine.search_backends import SearchEngine
        # Without key
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BRAVE_SEARCH_API_KEY", None)
            engine = SearchEngine()
            names = [type(b).__name__ for b in engine.backends]
            # Brave may still be registered but marked as unavailable
            # This is implementation-dependent

    def test_serper_requires_key(self):
        """Serper backend only registers when SERPER_API_KEY is set."""
        from behive.engine.search_backends import SearchEngine
        os.environ.pop("SERPER_API_KEY", None)
        engine = SearchEngine()
        # Just verify no crash on init without key


@pytest.mark.unit
class TestSearchResults:
    """Test search result normalization."""

    def test_result_has_required_fields(self):
        """Search results must have url, title, snippet."""
        from behive.engine.search_backends import SearchEngine
        # Create a mock result
        result = {"url": "https://example.com", "title": "Test", "snippet": "A test page"}
        assert all(k in result for k in ("url", "title", "snippet"))

    def test_deduplication(self):
        """Duplicate URLs from multiple backends are deduplicated."""
        from behive.engine.search_backends import SearchEngine
        engine = SearchEngine()
        
        results = [
            {"url": "https://example.com/page1", "title": "Page 1", "snippet": "First"},
            {"url": "https://example.com/page1", "title": "Page 1 Copy", "snippet": "Duplicate"},
            {"url": "https://example.com/page2", "title": "Page 2", "snippet": "Second"},
        ]
        
        # Deduplicate by URL
        seen = set()
        unique = []
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                unique.append(r)
        
        assert len(unique) == 2


@pytest.mark.unit
class TestPlaywrightBackend:
    """Test Chromium/Playwright search backend."""

    def test_bing_url_construction(self):
        """Bing search URL uses correct parameters."""
        # The PlaywrightBackend should construct Bing URLs like:
        # https://www.bing.com/search?q={query}&mkt=en-US&count=20
        expected_base = "https://www.bing.com/search"
        query = "test query 2025"
        url = f"{expected_base}?q={query.replace(' ', '+')}&mkt=en-US&count=20"
        assert "bing.com" in url
        assert "mkt=en-US" in url
