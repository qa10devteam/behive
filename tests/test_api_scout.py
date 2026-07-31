"""Tests for behive.engine.api_scout module."""
import pytest
from unittest.mock import patch, MagicMock
import behive.engine.api_scout as api_scout


class TestApiScout:
    def test_import(self):
        assert api_scout is not None

    def test_has_scout_functions(self):
        fns = [a for a in dir(api_scout) if not a.startswith('_') and callable(getattr(api_scout, a, None))]
        assert len(fns) >= 1
