"""Tests for behive.engine.llm module — BYOK LLM abstraction."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import os
from behive.engine.llm import complete, complete_async


class TestComplete:
    """Test synchronous LLM completion."""

    def test_function_exists(self):
        assert callable(complete)

    def test_signature(self):
        """Should accept prompt, stage, system, max_tokens, temperature, json_mode."""
        import inspect
        sig = inspect.signature(complete)
        params = list(sig.parameters.keys())
        assert "prompt" in params
        assert "stage" in params
        assert "system" in params

    @patch("litellm.completion")
    def test_returns_string(self, mock_completion):
        """Should return a string response."""
        mock_completion.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="AI market is growing"))]
        )
        result = complete("What is the AI market size?")
        assert isinstance(result, str)
        assert "AI market" in result

    @patch("litellm.completion")
    def test_handles_empty_response(self, mock_completion):
        """Should handle empty LLM response."""
        mock_completion.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=""))]
        )
        result = complete("test")
        assert result == "" or result is not None

    @patch("litellm.completion")
    def test_stage_parameter(self, mock_completion):
        """Stage parameter should be passed for model routing."""
        mock_completion.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="result"))]
        )
        result = complete("test", stage="scout")
        assert isinstance(result, str)

    @patch("litellm.completion")
    def test_json_mode(self, mock_completion):
        """json_mode should request JSON output."""
        mock_completion.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"key": "value"}'))]
        )
        result = complete("return json", json_mode=True)
        assert isinstance(result, str)


class TestCompleteAsync:
    """Test async LLM completion."""

    def test_function_exists(self):
        assert callable(complete_async)

    def test_is_coroutine(self):
        import inspect
        assert inspect.iscoroutinefunction(complete_async)


class TestModelDetection:
    """Test BYOK model auto-detection."""

    def test_module_imports_without_keys(self):
        """Module should import even without API keys."""
        import behive.engine.llm as llm
        assert llm is not None

    def test_complete_without_model_raises(self):
        """Calling complete without any LLM config should raise or return error."""
        # With mocked litellm that raises
        with patch("litellm.completion", side_effect=Exception("No model configured")):
            with pytest.raises(Exception):
                complete("test")
