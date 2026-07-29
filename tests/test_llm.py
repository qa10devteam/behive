"""
Unit tests for LLM routing (behive.engine.llm).

Tests that the BYOK model resolution works correctly:
- Per-stage overrides
- Global BEHIVE_MODEL
- Auto-detection from API keys
- Error on no credentials
- Retry logic
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.mark.unit
class TestModelResolution:
    """Test _get_configured_model() priority chain."""

    def test_stage_override_wins(self, clean_env):
        """BEHIVE_MODEL_SYNTH takes priority over BEHIVE_MODEL."""
        os.environ["BEHIVE_MODEL"] = "openai/gpt-4o"
        os.environ["BEHIVE_MODEL_SYNTH"] = "anthropic/claude-sonnet-4-20250514"
        
        from behive.engine.llm import _get_configured_model
        assert _get_configured_model("synth") == "anthropic/claude-sonnet-4-20250514"
        assert _get_configured_model("process") == "openai/gpt-4o"

    def test_global_model(self, clean_env):
        """BEHIVE_MODEL applies to all stages."""
        os.environ["BEHIVE_MODEL"] = "groq/llama-3.3-70b-versatile"
        
        from behive.engine.llm import _get_configured_model
        assert _get_configured_model("scout") == "groq/llama-3.3-70b-versatile"
        assert _get_configured_model("process") == "groq/llama-3.3-70b-versatile"
        assert _get_configured_model("synth") == "groq/llama-3.3-70b-versatile"

    def test_autodetect_anthropic(self, clean_env):
        """Auto-detects Anthropic from ANTHROPIC_API_KEY."""
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test123"
        
        from behive.engine.llm import _get_configured_model
        model = _get_configured_model("process")
        assert "anthropic" in model

    def test_autodetect_openai(self, clean_env):
        """Auto-detects OpenAI from OPENAI_API_KEY (no config file override)."""
        os.environ["OPENAI_API_KEY"] = "sk-test123"
        
        with patch("behive.engine.llm._has_aws_credentials", return_value=False):
            with patch("behive.config.load_config", return_value={}):
                from importlib import reload
                import behive.engine.llm as llm_mod
                # Force re-evaluation
                model = llm_mod._get_configured_model("process")
                assert "openai" in model

    def test_autodetect_groq(self, clean_env):
        """Auto-detects Groq from GROQ_API_KEY (no config file override)."""
        os.environ["GROQ_API_KEY"] = "gsk_test123"
        
        with patch("behive.engine.llm._has_aws_credentials", return_value=False):
            with patch("behive.config.load_config", return_value={}):
                from behive.engine.llm import _get_configured_model
                model = _get_configured_model("process")
                assert "groq" in model

    def test_local_llm_url(self, clean_env):
        """BEHIVE_LLM_URL triggers local model path (no config file override)."""
        os.environ["BEHIVE_LLM_URL"] = "http://localhost:11434/v1"
        
        with patch("behive.engine.llm._has_aws_credentials", return_value=False):
            with patch("behive.config.load_config", return_value={}):
                from behive.engine.llm import _get_configured_model
                model = _get_configured_model("process")
                assert "local" in model

    def test_no_credentials_returns_empty(self, clean_env):
        """No credentials configured → empty string (not a crash)."""
        with patch("behive.engine.llm._has_aws_credentials", return_value=False):
            with patch("behive.config.load_config", return_value={}):
                from behive.engine.llm import _get_configured_model
                model = _get_configured_model("process")
                assert model == ""

    def test_no_credentials_complete_raises(self, clean_env):
        """complete() raises RuntimeError with setup instructions when no keys."""
        with patch("behive.engine.llm._has_aws_credentials", return_value=False):
            with patch("behive.config.load_config", return_value={}):
                from behive.engine.llm import complete
                with pytest.raises(RuntimeError) as exc_info:
                    complete("test prompt", stage="process")
                error_msg = str(exc_info.value)
                assert "ANTHROPIC_API_KEY" in error_msg or "No model" in error_msg or "BYOK" in error_msg


@pytest.mark.unit
class TestRetryLogic:
    """Test that LLM calls retry on transient errors."""

    def test_retry_on_rate_limit(self, clean_env):
        """Rate limit errors trigger retry (up to 3 attempts)."""
        os.environ["OPENAI_API_KEY"] = "sk-test"
        
        import litellm
        rate_limit_error = Exception("Rate limit exceeded")
        
        call_count = [0]
        def mock_completion(**kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise rate_limit_error
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = "success"
            return mock_resp
        
        with patch("litellm.completion", side_effect=mock_completion):
            with patch("time.sleep"):  # Don't actually sleep
                from behive.engine.llm import complete
                result = complete("test", stage="process")
                assert result == "success"
                assert call_count[0] == 3

    def test_no_retry_on_auth_error(self, clean_env):
        """Auth errors propagate immediately (no retry, no swallow)."""
        os.environ["OPENAI_API_KEY"] = "sk-test"
        
        # Simulate litellm AuthenticationError
        class AuthError(Exception):
            pass
        auth_error = AuthError("AuthenticationError: invalid_api_key: The API key is invalid")
        
        with patch("litellm.completion", side_effect=auth_error):
            from behive.engine.llm import complete
            with pytest.raises(Exception) as exc_info:
                complete("test", stage="process")
            assert "invalid_api_key" in str(exc_info.value)


@pytest.mark.unit 
class TestCompleteFunction:
    """Test complete() behavior."""

    def test_complete_returns_string(self, clean_env, mock_litellm):
        """complete() returns the response content as string."""
        os.environ["OPENAI_API_KEY"] = "sk-test"
        mock_litellm.return_value.choices[0].message.content = "Hello world"
        
        from behive.engine.llm import complete
        result = complete("Say hello", stage="process")
        assert result == "Hello world"

    def test_complete_passes_system_prompt(self, clean_env, mock_litellm):
        """System prompt is included in messages."""
        os.environ["OPENAI_API_KEY"] = "sk-test"
        mock_litellm.return_value.choices[0].message.content = "ok"
        
        from behive.engine.llm import complete
        complete("extract facts", stage="process", system="You are a fact extractor")
        
        call_kwargs = mock_litellm.call_args[1]
        messages = call_kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert "fact extractor" in messages[0]["content"]

    def test_complete_json_mode(self, clean_env, mock_litellm):
        """json_mode=True sets response_format."""
        os.environ["OPENAI_API_KEY"] = "sk-test"
        mock_litellm.return_value.choices[0].message.content = '{"key": "value"}'
        
        from behive.engine.llm import complete
        complete("return json", stage="process", json_mode=True)
        
        call_kwargs = mock_litellm.call_args[1]
        assert call_kwargs["response_format"] == {"type": "json_object"}
