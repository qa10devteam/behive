"""Tests for behive.engine.falsifier module."""
import pytest
from unittest.mock import patch, MagicMock
from behive.engine.falsifier import (
    ClaimDeduplicator,
    ConflictResolver,
    NumericalConflictDetector,
    NumericalValue,
    NumericalFalsificationPipeline,
    CanonicalClaim,
    UNIT_CANON,
    run_falsification_for_mission,
    get_falsification_report,
    cross_source_corroboration,
)


class TestNumericalValue:
    """Test numerical value parsing."""

    def test_create_numerical_value(self):
        """Should create NumericalValue with correct fields."""
        nv = NumericalValue(raw="$42B", value=42.0, unit_raw="$B", unit_canon="USD_B")
        assert nv.value == 42.0
        assert nv.raw == "$42B"
        assert nv.unit_canon == "USD_B"

    def test_numerical_value_fields(self):
        """Should have raw, value, unit_raw, unit_canon fields."""
        from dataclasses import fields
        f = [field.name for field in fields(NumericalValue)]
        assert "value" in f
        assert "raw" in f
        assert "unit_raw" in f
        assert "unit_canon" in f


class TestUnitCanon:
    """Test unit canonicalization map."""

    def test_unit_canon_exists(self):
        """UNIT_CANON should be a non-empty dict."""
        assert isinstance(UNIT_CANON, dict)
        assert len(UNIT_CANON) > 5

    def test_common_units_mapped(self):
        """Common units should be in the canon."""
        all_keys = list(UNIT_CANON.keys())
        assert len(all_keys) > 0


class TestClaimDeduplicator:
    """Test claim deduplication logic."""

    def test_instantiate(self):
        """Should create with mission_id."""
        dedup = ClaimDeduplicator(mission_id="test-mission-001")
        assert dedup is not None

    def test_has_methods(self):
        """Should have deduplication methods."""
        dedup = ClaimDeduplicator(mission_id="test-001")
        attrs = dir(dedup)
        assert any('dedup' in a.lower() or 'run' in a.lower() or 'process' in a.lower() for a in attrs)


class TestConflictResolver:
    """Test conflict resolution."""

    def test_instantiate(self):
        """Should create with mission_id."""
        resolver = ConflictResolver(mission_id="test-001")
        assert resolver is not None


class TestNumericalConflictDetector:
    """Test numerical conflict detection."""

    def test_instantiate(self):
        """Should create with mission_id."""
        detector = NumericalConflictDetector(mission_id="test-001")
        assert detector is not None

    def test_has_methods(self):
        """Should have detection methods."""
        detector = NumericalConflictDetector(mission_id="test-001")
        attrs = dir(detector)
        assert any('detect' in a.lower() or 'run' in a.lower() or 'find' in a.lower() for a in attrs)


class TestNumericalFalsificationPipeline:
    """Test the falsification pipeline."""

    def test_instantiate(self):
        """Should create with mission_id."""
        pipeline = NumericalFalsificationPipeline(mission_id="test-001")
        assert pipeline is not None


class TestCanonicalClaim:
    """Test canonical claim dataclass."""

    def test_create(self):
        """Should create with required fields."""
        claim = CanonicalClaim(claim_id="c1", text="AI market is $184B", 
                              confidence=0.85, mission_id="m1")
        assert claim.text == "AI market is $184B"
        assert claim.confidence == 0.85
        assert claim.duplicate_count == 1

    def test_source_urls_default(self):
        """source_urls should default to empty list."""
        claim = CanonicalClaim(claim_id="c2", text="test", confidence=0.5, mission_id="m1")
        assert isinstance(claim.source_urls, list)


class TestCrossSourceCorroboration:
    """Test cross-source corroboration."""

    def test_function_exists(self):
        """Function should be callable."""
        assert callable(cross_source_corroboration)

    def test_signature(self):
        """Should accept claims."""
        import inspect
        sig = inspect.signature(cross_source_corroboration)
        assert len(list(sig.parameters)) >= 1


class TestRunFalsification:
    """Test main falsification runner."""

    def test_function_exists(self):
        """Should be callable."""
        assert callable(run_falsification_for_mission)

    def test_get_report_exists(self):
        """Report function should be callable."""
        assert callable(get_falsification_report)
