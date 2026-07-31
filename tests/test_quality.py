"""Tests for behive.engine.quality module."""
import pytest
from behive.engine.quality import (
    score_content,
    score_content_detailed,
    quality_label,
    is_covert_infected,
    COVERT_THRESHOLD,
)


class TestQualityModule:
    """Test quality module (re-exports from content_quality)."""

    def test_score_content_empty(self):
        score = score_content("")
        assert score <= 0.1

    def test_score_content_real(self):
        text = "Global semiconductor revenue hit $580B in 2024 (Gartner)."
        score = score_content(text)
        assert 0.0 <= score <= 1.0

    def test_detailed_returns_dict(self):
        result = score_content_detailed("AI market analysis from McKinsey.")
        assert isinstance(result, dict)

    def test_quality_label_types(self):
        for val in [0.0, 0.25, 0.5, 0.75, 1.0]:
            label = quality_label(val)
            assert isinstance(label, str)

    def test_covert_check(self):
        assert isinstance(is_covert_infected("Normal text."), bool)

    def test_threshold_valid(self):
        assert 0.0 <= COVERT_THRESHOLD <= 1.0
