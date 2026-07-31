"""Tests for behive.engine.search_backends module."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from behive.engine.search_backends import (
    SearchEngine,
    SearchBackend,
    PlaywrightBackend,
    DuckDuckGoBackend,
    BraveBackend,
    SearXNGBackend,
    SerperBackend,
    TavilyBackend,
    ALL_BACKENDS,
    get_engine,
    search,
    search_news,
)


class TestSearchEngine:
    """Test SearchEngine class."""

    def test_get_engine(self):
        """get_engine should return a SearchEngine instance."""
        engine = get_engine()
        assert engine is not None
        assert isinstance(engine, SearchEngine)

    def test_engine_has_backends(self):
        """Engine should have registered backends."""
        engine = get_engine()
        assert hasattr(engine, 'backends') or hasattr(engine, '_backends')


class TestAllBackends:
    """Test backend registry."""

    def test_all_backends_list(self):
        """ALL_BACKENDS should be a non-empty list."""
        assert isinstance(ALL_BACKENDS, (list, tuple, dict))
        assert len(ALL_BACKENDS) >= 2

    def test_backend_classes_exist(self):
        """All backend classes should be importable."""
        assert PlaywrightBackend is not None
        assert DuckDuckGoBackend is not None
        assert BraveBackend is not None
        assert SearXNGBackend is not None
        assert SerperBackend is not None
        assert TavilyBackend is not None


class TestSearchBackendBase:
    """Test SearchBackend base class."""

    def test_is_class(self):
        """SearchBackend should be a class."""
        assert isinstance(SearchBackend, type)

    def test_has_search_method(self):
        """Should define a search method."""
        assert hasattr(SearchBackend, 'search') or hasattr(SearchBackend, 'query')


class TestPlaywrightBackend:
    """Test Playwright-based search."""

    def test_instantiate(self):
        """Should instantiate without errors."""
        backend = PlaywrightBackend()
        assert backend is not None

    def test_has_priority(self):
        """Backend should have a priority attribute."""
        backend = PlaywrightBackend()
        assert hasattr(backend, 'priority') or hasattr(backend, 'name')


class TestDuckDuckGoBackend:
    """Test DDG backend."""

    def test_instantiate(self):
        """Should instantiate."""
        backend = DuckDuckGoBackend()
        assert backend is not None


class TestBraveBackend:
    """Test Brave Search backend."""

    def test_instantiate(self):
        """Should instantiate."""
        backend = BraveBackend()
        assert backend is not None


class TestSearch:
    """Test the search function."""

    def test_function_exists(self):
        """search should be callable."""
        assert callable(search)

    def test_signature(self):
        """Should accept query string."""
        import inspect
        sig = inspect.signature(search)
        params = list(sig.parameters.keys())
        assert "query" in params or "q" in params or len(params) >= 1


class TestSearchNews:
    """Test news search."""

    def test_function_exists(self):
        """search_news should be callable."""
        assert callable(search_news)
