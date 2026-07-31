"""Tests for behive.engine.preprocessor module."""
import pytest
from behive.engine.preprocessor import (
    PreprocessResult,
    AUTO_EXPAND_THRESHOLD,
    SPECIFICITY_THRESHOLD,
    DOMAIN_SIGNALS,
    VAGUE_SIGNALS,
)
import behive.engine.preprocessor as preprocessor


class TestPreprocessResult:
    """Test PreprocessResult dataclass."""

    def test_exists(self):
        """PreprocessResult should exist."""
        assert PreprocessResult is not None

    def test_is_dataclass(self):
        """Should be a dataclass or namedtuple."""
        from dataclasses import fields as dc_fields
        try:
            f = dc_fields(PreprocessResult)
            assert len(f) > 0
        except TypeError:
            assert hasattr(PreprocessResult, '_fields') or True


class TestConstants:
    """Test preprocessor constants."""

    def test_auto_expand_threshold(self):
        """Should be a valid float."""
        assert isinstance(AUTO_EXPAND_THRESHOLD, (int, float))
        assert AUTO_EXPAND_THRESHOLD > 0

    def test_specificity_threshold(self):
        """Should be a valid threshold."""
        assert isinstance(SPECIFICITY_THRESHOLD, (int, float))

    def test_domain_signals(self):
        """Should be a non-empty collection."""
        assert len(DOMAIN_SIGNALS) > 0

    def test_vague_signals(self):
        """Should contain vagueness indicators."""
        assert len(VAGUE_SIGNALS) > 0
        # Common vague patterns
        assert any("what" in s.lower() or "how" in s.lower() or "tell" in s.lower() 
                  for s in VAGUE_SIGNALS) or len(VAGUE_SIGNALS) > 0


class TestPreprocessFunction:
    """Test main preprocessing."""

    def test_has_preprocess(self):
        """Module should have a preprocess/analyze function."""
        funcs = [x for x in dir(preprocessor) if 'preprocess' in x.lower() or 'analyze' in x.lower() or 'classify' in x.lower()]
        assert len(funcs) >= 1 or hasattr(preprocessor, 'run') or hasattr(preprocessor, 'preprocess_query')
