"""Tests for behive.engine.bedrock_compat module."""
import pytest
from unittest.mock import patch, MagicMock
import behive.engine.bedrock_compat as bedrock


class TestBedrockCompat:
    def test_import(self):
        """Module should import (optional dependency)."""
        assert bedrock is not None

    def test_has_client_class(self):
        """Should have BedrockCompatClient class."""
        assert hasattr(bedrock, 'BedrockCompatClient')
        assert hasattr(bedrock, 'get_bedrock_compat_client')
        assert hasattr(bedrock, 'patch_boto3_bedrock')
        assert hasattr(bedrock, 'unpatch_boto3')
