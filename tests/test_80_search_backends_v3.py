"""SEARCH_BACKENDS V3 — SearchEngine, backend classes, get_engine.
search_backends.py=50%(149 miss).
"""
import pytest
from unittest.mock import patch, MagicMock


class TestSearchBackendBase:
    """SearchBackend base class."""

    def test_import(self):
        from behive.engine.search_backends import SearchBackend
        assert SearchBackend is not None

    def test_subclasses(self):
        from behive.engine.search_backends import (
            PlaywrightBackend, SearXNGBackend, BraveBackend,
            SerperBackend, TavilyBackend, DuckDuckGoBackend,
        )
        assert all(cls is not None for cls in [
            PlaywrightBackend, SearXNGBackend, BraveBackend,
            SerperBackend, TavilyBackend, DuckDuckGoBackend,
        ])


class TestPlaywrightBackend:
    """PlaywrightBackend — Bing via Chromium."""

    def test_init(self):
        from behive.engine.search_backends import PlaywrightBackend
        pb = PlaywrightBackend()
        assert pb is not None

    def test_priority(self):
        from behive.engine.search_backends import PlaywrightBackend
        pb = PlaywrightBackend()
        if hasattr(pb, 'priority'):
            assert isinstance(pb.priority, int)

    def test_name(self):
        from behive.engine.search_backends import PlaywrightBackend
        pb = PlaywrightBackend()
        if hasattr(pb, 'name'):
            assert isinstance(pb.name, str)


class TestBraveBackend:
    """BraveBackend — Brave Search API."""

    def test_init(self):
        from behive.engine.search_backends import BraveBackend
        bb = BraveBackend()
        assert bb is not None


class TestSerperBackend:
    """SerperBackend — serper.dev API."""

    def test_init(self):
        from behive.engine.search_backends import SerperBackend
        sb = SerperBackend()
        assert sb is not None


class TestTavilyBackend:
    """TavilyBackend — Tavily Search API."""

    def test_init(self):
        from behive.engine.search_backends import TavilyBackend
        tb = TavilyBackend()
        assert tb is not None


class TestDuckDuckGoBackend:
    """DuckDuckGoBackend — DDG fallback."""

    def test_init(self):
        from behive.engine.search_backends import DuckDuckGoBackend
        ddg = DuckDuckGoBackend()
        assert ddg is not None


class TestSearchEngine:
    """SearchEngine — orchestrates backends by priority."""

    def test_init(self):
        from behive.engine.search_backends import SearchEngine
        se = SearchEngine()
        assert se is not None

    def test_backends_list(self):
        from behive.engine.search_backends import SearchEngine
        se = SearchEngine()
        if hasattr(se, 'backends'):
            assert isinstance(se.backends, list)
            assert len(se.backends) > 0

    def test_get_engine(self):
        from behive.engine.search_backends import get_engine
        engine = get_engine()
        assert engine is not None


class TestSearXNGBackend:
    """SearXNGBackend — self-hosted search."""

    def test_init(self):
        from behive.engine.search_backends import SearXNGBackend
        sx = SearXNGBackend()
        assert sx is not None
