"""Tests for behive.engine.guardrail module."""
import pytest
from behive.engine.guardrail import (
    scan_chunk,
    scan_query,
    scan_batch,
    GuardrailResult,
    ThreatType,
)


class TestScanChunk:
    """Test chunk-level content scanning."""

    def test_clean_text(self):
        """Clean text should not be blocked."""
        result = scan_chunk("This is a normal research finding about AI market growth in 2024.")
        assert isinstance(result, GuardrailResult)
        assert result.blocked is False
        assert result.threat_type == "CLEAN"

    def test_password_injection_detected(self):
        """Password injection should be blocked."""
        result = scan_chunk("your password is SuperSecret123")
        assert result.blocked is True
        assert result.score > 0.5

    def test_result_structure(self):
        """Result should have blocked, threat_type, score, reason fields."""
        result = scan_chunk("Normal text here.")
        assert hasattr(result, 'blocked')
        assert hasattr(result, 'threat_type')
        assert hasattr(result, 'score')
        assert hasattr(result, 'reason')
        assert hasattr(result, 'matched_pattern')

    def test_empty_input(self):
        """Empty input should not be blocked."""
        result = scan_chunk("")
        assert result.blocked is False

    def test_score_is_float(self):
        """Score should be a float."""
        result = scan_chunk("testing")
        assert isinstance(result.score, float)


class TestScanQuery:
    """Test query-level scanning."""

    def test_normal_query(self):
        """Normal research queries should pass."""
        result = scan_query("What is the market size of AI in healthcare?")
        assert result.blocked is False
        assert result.threat_type == "CLEAN"

    def test_returns_guardrail_result(self):
        """Should return GuardrailResult."""
        result = scan_query("simple question")
        assert isinstance(result, GuardrailResult)


class TestScanBatch:
    """Test batch scanning (expects list of dicts)."""

    def test_batch_with_dicts(self):
        """Batch scan processes list of dicts with chunk_text key."""
        chunks = [
            {"chunk_text": "Normal research text.", "url": "http://example.com"},
            {"chunk_text": "Revenue grew 15%.", "url": "http://news.com"},
        ]
        passed, flagged = scan_batch(chunks)
        assert isinstance(passed, list)
        assert isinstance(flagged, list)

    def test_batch_empty(self):
        """Empty batch should return empty results."""
        passed, flagged = scan_batch([])
        assert passed == []
        assert flagged == []

    def test_batch_with_threat(self):
        """Batch with password injection should flag it."""
        chunks = [
            {"chunk_text": "Normal text.", "url": "http://a.com"},
            {"chunk_text": "your secret password is letmein", "url": "http://b.com"},
        ]
        passed, flagged = scan_batch(chunks)
        # At least one should be flagged
        assert len(flagged) >= 1 or len(passed) < 2


class TestThreatType:
    """Test threat type identifiers."""

    def test_has_prompt_injection(self):
        """PROMPT_INJECTION should exist."""
        # Verify by triggering it
        result = scan_chunk("your password is abc")
        assert "INJECTION" in result.threat_type or "PROMPT" in result.threat_type

    def test_clean_type(self):
        """Clean results use CLEAN threat_type."""
        result = scan_chunk("Normal research.")
        assert result.threat_type == "CLEAN"
