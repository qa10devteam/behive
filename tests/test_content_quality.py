"""Tests for behive.engine.content_quality module."""
import pytest
from behive.engine.content_quality import (
    score_content,
    score_content_detailed,
    quality_label,
    is_covert_infected,
    COVERT_THRESHOLD,
)


class TestScoreContent:
    """Test content quality scoring."""

    def test_empty_string(self):
        """Empty content should score 0 or very low."""
        score = score_content("")
        assert score <= 0.15

    def test_short_text(self):
        """Short text should score low."""
        score = score_content("Hello")
        assert score < 0.5

    def test_returns_float(self):
        """Score should always be a float."""
        assert isinstance(score_content("hello world"), (int, float))

    def test_bounded_0_1(self):
        """Score should be between 0 and 1."""
        for text in ["", "x", "a" * 5000]:
            score = score_content(text)
            assert 0.0 <= score <= 1.0

    def test_longer_content_scores(self):
        """Longer well-structured content should get a score."""
        text = " ".join(["The market grew significantly."] * 30)
        score = score_content(text)
        assert 0.0 <= score <= 1.0


class TestScoreContentDetailed:
    """Test detailed scoring."""

    def test_returns_dict(self):
        """Detailed scoring should return dict."""
        result = score_content_detailed("Some factual text about AI research.")
        assert isinstance(result, dict)

    def test_has_quality_score_key(self):
        """Should include quality_score key."""
        result = score_content_detailed("Market grew 15% in 2024.")
        assert "quality_score" in result

    def test_has_component_scores(self):
        """Should break down into component scores."""
        result = score_content_detailed("Testing content quality scoring module.")
        assert "sentence_coherence" in result
        assert "lexical_diversity" in result
        assert "factual_density" in result

    def test_covert_infected_field(self):
        """Should include covert_infected boolean."""
        result = score_content_detailed("Test text.")
        assert "covert_infected" in result
        assert isinstance(result["covert_infected"], bool)


class TestQualityLabel:
    """Test quality labeling (returns UPPERCASE labels)."""

    def test_high_score_label(self):
        """High scores should get EXCELLENT."""
        label = quality_label(0.9)
        assert label == "EXCELLENT"

    def test_medium_score_label(self):
        """Medium scores should get FAIR or GOOD."""
        label = quality_label(0.5)
        assert label in ["FAIR", "GOOD", "OK"]

    def test_low_score_label(self):
        """Low scores should get POOR or INFECTED."""
        label = quality_label(0.1)
        assert label in ["POOR", "INFECTED", "LOW", "BAD"]

    def test_returns_string(self):
        """Should always return a string."""
        for val in [0.0, 0.25, 0.5, 0.75, 1.0]:
            assert isinstance(quality_label(val), str)


class TestCovertInfection:
    """Test covert content detection."""

    def test_returns_bool(self):
        """Should return boolean."""
        assert isinstance(is_covert_infected("test"), bool)

    def test_threshold_constant(self):
        """COVERT_THRESHOLD should be a valid float."""
        assert isinstance(COVERT_THRESHOLD, (int, float))
        assert 0.0 <= COVERT_THRESHOLD <= 1.0
