"""Tests for behive.mcp_server module."""
import pytest
from unittest.mock import patch, MagicMock
import behive.mcp_server as mcp


class TestMCPServer:
    def test_import(self):
        assert mcp is not None

    def test_has_tools(self):
        """MCP server should define tool functions."""
        attrs = [a for a in dir(mcp) if not a.startswith('_')]
        assert len(attrs) >= 3
