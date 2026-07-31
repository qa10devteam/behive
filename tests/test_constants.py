"""Tests for behive.engine.constants module."""
import pytest
import behive.engine.constants as constants


class TestConstants:
    """Verify all constants are defined and have expected types."""

    def test_stage_names_defined(self):
        """Stage names should be strings."""
        assert hasattr(constants, 'STAGE_SCOUT')
        assert hasattr(constants, 'STAGE_HARVEST')
        assert hasattr(constants, 'STAGE_PROCESS')
        assert hasattr(constants, 'STAGE_SYNTH')
        assert isinstance(constants.STAGE_SCOUT, str)

    def test_table_names_defined(self):
        """Table constants should be defined."""
        assert hasattr(constants, 'TABLE_MISSIONS')
        assert hasattr(constants, 'TABLE_CLAIMS')
        assert hasattr(constants, 'TABLE_SOURCES')
        assert hasattr(constants, 'TABLE_CONTENT')
        assert hasattr(constants, 'TABLE_ENTITIES')

    def test_quality_thresholds(self):
        """Quality thresholds should be floats between 0 and 1."""
        assert 0.0 <= constants.DEFAULT_QUALITY_THRESHOLD <= 1.0
        assert 0.0 <= constants.DEFAULT_CONFIDENCE_THRESHOLD <= 1.0

    def test_source_types(self):
        """Source type constants should exist."""
        assert hasattr(constants, 'SOURCE_API')
        assert hasattr(constants, 'SOURCE_DIRECT')

    def test_max_tokens(self):
        """Token limits should be positive integers."""
        assert constants.MAX_TOKENS_HAIKU > 0
        assert constants.MAX_TOKENS_SONNET > 0
        assert constants.MAX_TOKENS_SONNET >= constants.MAX_TOKENS_HAIKU

    def test_all_stages_list(self):
        """ALL_STAGES should contain all stage constants."""
        assert isinstance(constants.ALL_STAGES, (list, tuple))
        assert len(constants.ALL_STAGES) >= 4

    def test_bedrock_constants(self):
        """Bedrock constants should be strings."""
        assert isinstance(constants.BEDROCK_SERVICE, str)
        assert isinstance(constants.BEDROCK_ANTHROPIC_VERSION, str)

    def test_module_importable(self):
        """Module should import cleanly."""
        import importlib
        mod = importlib.import_module('behive.engine.constants')
        assert mod is not None
