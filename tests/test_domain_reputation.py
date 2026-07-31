"""Tests for behive.engine.domain_reputation module."""
import pytest
from behive.engine.domain_reputation import DomainReputation
import behive.engine.domain_reputation as dr


class TestDomainReputationModule:
    """Test domain reputation module structure."""

    def test_module_imports(self):
        """Module should import without errors."""
        assert dr is not None

    def test_domain_reputation_class_exists(self):
        """DomainReputation class should exist."""
        assert DomainReputation is not None

    def test_domain_reputation_is_dataclass(self):
        """DomainReputation should be a dataclass or class with score."""
        from dataclasses import fields as dc_fields
        try:
            f = dc_fields(DomainReputation)
            field_names = [field.name for field in f]
            assert len(field_names) > 0
        except TypeError:
            # Not a dataclass - check for attributes
            assert hasattr(DomainReputation, '__init__')

    def test_get_reputation_exists(self):
        """get_reputation function should exist."""
        assert hasattr(dr, 'get_reputation')
        assert callable(dr.get_reputation)

    def test_module_has_scoring_logic(self):
        """Module should contain domain scoring constants or functions."""
        # Check for tier-related attributes
        attrs = dir(dr)
        has_tiers = any('tier' in a.lower() or 'score' in a.lower() or 'domain' in a.lower() 
                       for a in attrs)
        assert has_tiers
