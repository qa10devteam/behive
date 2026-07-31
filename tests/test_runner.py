"""Tests for behive.engine.runner module."""
import pytest
from unittest.mock import patch, MagicMock
import behive.engine.runner as runner


class TestRunnerImport:
    def test_import(self):
        assert runner is not None

    def test_has_run_function(self):
        """Should have a main run/execute function."""
        attrs = dir(runner)
        assert any('run' in a.lower() for a in attrs)
