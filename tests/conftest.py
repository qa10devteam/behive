"""
BeHive Test Suite — fixtures and configuration.

Markers:
  - @pytest.mark.unit: pure logic, no I/O, fast
  - @pytest.mark.integration: requires PostgreSQL
  - @pytest.mark.e2e: full pipeline (slow, requires LLM + search)
"""
import os
import sys
import pytest
import psycopg2
from unittest.mock import patch, MagicMock

# Ensure src/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ─── Markers ──────────────────────────────────────────────────────────────────

def pytest_configure(config):
    config.addinivalue_line("markers", "unit: pure unit tests, no I/O")
    config.addinivalue_line("markers", "integration: requires PostgreSQL")
    config.addinivalue_line("markers", "e2e: full pipeline end-to-end")
    config.addinivalue_line("markers", "regression: regression tests for past bugs")


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def pg_conn():
    """PostgreSQL connection for integration tests."""
    dsn = os.environ.get("BEHIVE_TEST_DSN", "dbname=hive user=hive_app password=hive_2026_prod host=127.0.0.1")
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        yield conn
        conn.close()
    except psycopg2.OperationalError:
        pytest.skip("PostgreSQL not available")


@pytest.fixture
def pg_cursor(pg_conn):
    """Cursor that rolls back after each test."""
    cur = pg_conn.cursor()
    yield cur
    pg_conn.rollback()


@pytest.fixture
def mock_litellm():
    """Mock litellm.completion for unit tests."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"test": "response"}'
    
    with patch("litellm.completion", return_value=mock_response) as mock:
        yield mock


@pytest.fixture
def clean_env():
    """Clean environment — no LLM keys, no BEHIVE_ vars."""
    env_vars = [
        "BEHIVE_MODEL", "BEHIVE_MODEL_SCOUT", "BEHIVE_MODEL_PROCESS",
        "BEHIVE_MODEL_SYNTH", "BEHIVE_LLM_URL", "BEHIVE_LOCAL_LLM_URL",
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY",
        "MISTRAL_API_KEY", "DEEPSEEK_API_KEY", "TOGETHER_API_KEY",
        "AWS_ACCESS_KEY_ID", "AWS_PROFILE",
    ]
    old = {k: os.environ.pop(k, None) for k in env_vars}
    yield
    for k, v in old.items():
        if v is not None:
            os.environ[k] = v


@pytest.fixture(scope="session")
def behive_server_url():
    """URL of running BeHive server for E2E tests."""
    url = os.environ.get("BEHIVE_TEST_URL", "http://127.0.0.1:8091")
    return url
