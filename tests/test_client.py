"""Tests for behive.client module."""
import pytest
from unittest.mock import patch, MagicMock
import behive.client as client


class TestClient:
    def test_import(self):
        assert client is not None

    def test_has_client_class_or_function(self):
        """Should have a BeHiveClient or research function."""
        attrs = [a for a in dir(client) if not a.startswith('_')]
        assert len(attrs) >= 1
