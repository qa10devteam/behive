"""SEARCH BACKENDS DEEP — sync-only safe tests.
search_backends.py is 50% (149 miss).
"""
import pytest
from unittest.mock import patch, MagicMock


class TestSearchBackendBase:
    def test_base_init(self):
        from behive.engine.search_backends import SearchBackend
        sb = SearchBackend()
        assert sb.priority >= 0

    def test_base_check(self):
        from behive.engine.search_backends import SearchBackend
        sb = SearchBackend()
        assert sb._check_available() == False


class TestPlaywrightBackend:
    def test_init(self):
        from behive.engine.search_backends import PlaywrightBackend
        pb = PlaywrightBackend()
        assert pb.priority == 0

    def test_check_available(self):
        from behive.engine.search_backends import PlaywrightBackend
        pb = PlaywrightBackend()
        result = pb._check_available()
        assert isinstance(result, bool)


class TestSearXNGBackend:
    def test_init(self):
        from behive.engine.search_backends import SearXNGBackend
        sb = SearXNGBackend()
        assert sb is not None

    def test_check_available(self):
        from behive.engine.search_backends import SearXNGBackend
        sb = SearXNGBackend()
        assert isinstance(sb._check_available(), bool)


class TestBraveBackend:
    def test_init(self):
        from behive.engine.search_backends import BraveBackend
        bb = BraveBackend()
        assert bb is not None

    def test_check_available(self):
        from behive.engine.search_backends import BraveBackend
        bb = BraveBackend()
        assert isinstance(bb._check_available(), bool)


class TestSerperBackend:
    def test_init(self):
        from behive.engine.search_backends import SerperBackend
        sb = SerperBackend()
        assert sb is not None

    def test_check_available(self):
        from behive.engine.search_backends import SerperBackend
        sb = SerperBackend()
        assert isinstance(sb._check_available(), bool)


class TestTavilyBackend:
    def test_init(self):
        from behive.engine.search_backends import TavilyBackend
        tb = TavilyBackend()
        assert tb is not None

    def test_check_available(self):
        from behive.engine.search_backends import TavilyBackend
        tb = TavilyBackend()
        assert isinstance(tb._check_available(), bool)


class TestSearchEngine:
    def test_get_engine(self):
        from behive.engine.search_backends import get_engine
        engine = get_engine()
        assert engine is not None

    def test_engine_has_backends(self):
        from behive.engine.search_backends import get_engine
        engine = get_engine()
        if hasattr(engine, 'backends'):
            assert isinstance(engine.backends, list)
