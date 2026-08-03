"""conftest.py — shared fixtures for test_80_* integration tests.

Strategy: SINGLETON PgConnection + mocked LLM/HTTP.
PgConnection wraps psycopg2 with ? → %s conversion (required by engine modules).
ONE connection for entire test session → no connection exhaustion.
"""
import pytest
import psycopg2
import os
import json
from unittest.mock import patch, MagicMock

# ═══════════════════════════════════════════════════════════════════════════════
# SINGLETON PgConnection — ONE connection for ALL tests
# ═══════════════════════════════════════════════════════════════════════════════

PG_DSN = "host=localhost port=5432 dbname=hive user=hive_app password=bH9_xK2m_pR7v_2026"

# Module-level singleton — created once, reused everywhere
_SHARED_PGCONN = None
_SHARED_RAW_CONN = None


def _get_shared_pgconn():
    """Get or create the singleton PgConnection.
    Resets pool on first call (important for pytest-xdist forked workers).
    """
    global _SHARED_PGCONN
    if _SHARED_PGCONN is None:
        # Reset the pool for this worker (may be forked)
        import behive.engine.db as _db_mod
        _db_mod._pool = None
        from behive.engine.db import PgConnection
        _SHARED_PGCONN = PgConnection(read_only=False)
    return _SHARED_PGCONN


def _get_shared_raw_conn():
    """Get or create a raw psycopg2 connection (for fixtures that need cursor())."""
    global _SHARED_RAW_CONN
    if _SHARED_RAW_CONN is None or _SHARED_RAW_CONN.closed:
        _SHARED_RAW_CONN = psycopg2.connect(PG_DSN)
        _SHARED_RAW_CONN.autocommit = True
    return _SHARED_RAW_CONN


@pytest.fixture
def pg_conn():
    """Raw psycopg2 connection (for tests that need .cursor() directly)."""
    return _get_shared_raw_conn()


@pytest.fixture
def real_mission_id():
    """Return a real mission_id that has claims+sources."""
    conn = _get_shared_raw_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT m.id FROM hive_missions m 
        JOIN hive_claims c ON c.mission_id = m.id 
        GROUP BY m.id 
        HAVING COUNT(c.id) > 10 
        ORDER BY m.created_at DESC 
        LIMIT 1
    """)
    row = cur.fetchone()
    cur.close()
    return row[0] if row else "hive_test_1234"


# ═══════════════════════════════════════════════════════════════════════════════
# Global LLM mock — intercepts all LLM calls across the entire engine
# ═══════════════════════════════════════════════════════════════════════════════

_MOCK_LLM_RESPONSE = json.dumps([
    {"id": 1, "query": "AI semiconductor market size 2024",
     "source": "ddg_text", "purpose": "Quantify total addressable market",
     "expected_output": "Market size figures and growth rate"},
    {"id": 2, "query": "NVIDIA GPU market share AI chips",
     "source": "ddg_news", "purpose": "Identify market leader position",
     "expected_output": "Market share percentages"},
    {"id": 3, "query": "AMD Intel AI chip competition analysis",
     "source": "google_rss", "purpose": "Map competitive landscape",
     "expected_output": "Competitor positioning data"},
])

# Alternative responses for different stages
_MOCK_AXES_RESPONSE = json.dumps([
    "AI chip market sizing and growth trajectory 2023-2025",
    "NVIDIA vs AMD vs Intel competitive positioning in AI accelerators",
    "Emerging AI chip startups and alternative architectures",
    "Enterprise AI infrastructure spending and deployment patterns",
    "Geopolitical impacts on semiconductor supply chains for AI",
])

_call_count = 0

def _smart_llm_mock(prompt="", stage="process", system="", max_tokens=4096,
                   temperature=0.3, json_mode=False, **kw):
    """Smart LLM mock that returns appropriate JSON for different stages."""
    global _call_count
    _call_count += 1
    
    # SYNTH stage needs long text (multi_role_synthesis checks len > 500)
    if stage == "synth":
        return """# Research Report: AI Semiconductor Market Analysis 2024

## Executive Summary
The AI semiconductor market reached $196 billion in 2024, growing 23% year-over-year. NVIDIA dominates with approximately 80% market share in AI accelerators.

## Key Findings
1. NVIDIA H100/H200 GPUs remain dominant in data center AI training
2. AMD MI300X gaining traction in inference workloads
3. Enterprise AI spending accelerated significantly in H2 2024
4. Supply constraints easing but still present for cutting-edge nodes

## Market Dynamics
The total addressable market for AI semiconductors is projected to reach $340 billion by 2027. Key drivers include GenAI training demand, inference scaling requirements, and emerging edge AI applications.

## Competitive Landscape
- NVIDIA: 80% share, H100/H200/B100 product line
- AMD: 15% share, MI300X competitive in inference
- Intel: 5% share, Gaudi 3 entering market
- Startups: Cerebras, Groq, SambaNova targeting specialized workloads

## Conclusion
Strong growth trajectory with concentration risk in NVIDIA dominance. [UNVERIFIED] Market could reach $500B by 2030.

## Reviewer Notes
1. Market share figures from Mercury Research (single source)
2. Growth projections vary significantly across analysts
3. Geopolitical risks (China restrictions) not fully quantified
"""
    
    # If prompt asks for axes/decomposition
    prompt_lower = prompt.lower() if isinstance(prompt, str) else ""
    if "decompos" in prompt_lower or "axes" in prompt_lower or "research directions" in prompt_lower:
        return _MOCK_AXES_RESPONSE
    # If it's asking for tasks/queries
    if "query" in prompt_lower or "search" in prompt_lower or "task" in prompt_lower:
        return _MOCK_LLM_RESPONSE
    # Default — return a JSON object
    return json.dumps({
        "claims": [
            {"claim": "AI market grew 23% in 2024", "confidence": 0.9, "evidence": "Reuters report"},
            {"claim": "NVIDIA holds 80% GPU share", "confidence": 0.85, "evidence": "WSJ analysis"}
        ],
        "entities": [{"name": "NVIDIA", "type": "company"}],
        "summary": "The AI market experienced significant growth in 2024."
    })

_MOCK_EMBED = [0.1] * 512


@pytest.fixture(autouse=True)
def mock_llm_globally(monkeypatch):
    """Auto-mock all LLM calls so tests never hit real APIs."""

    # Patch at multiple levels to catch all paths
    monkeypatch.setattr("behive.engine.llm.complete", _smart_llm_mock, raising=False)
    monkeypatch.setattr("behive.engine.llm.embed", lambda texts, **kw: [_MOCK_EMBED] if isinstance(texts, str) else [_MOCK_EMBED for _ in texts], raising=False)


@pytest.fixture(autouse=True)
def mock_subprocess(monkeypatch, request):
    """Prevent any subprocess spawning (scout/harvest phase scripts).
    Skip for tests that need real subprocess (mark with @pytest.mark.real_subprocess).
    """
    if "real_subprocess" in request.keywords:
        return
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = b"OK"
    fake_result.stderr = b""
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_result, raising=False)


# ═══════════════════════════════════════════════════════════════════════════════
# CORE FIX: Singleton PgConnection for ALL _pg_connect_retry calls
# ═══════════════════════════════════════════════════════════════════════════════

_ALL_ENGINE_MODULES = [
    "scout", "prescout", "harvest", "process", "events",
    "orchestrator", "synth", "falsifier", "graph_engine",
    "domain_reputation", "queen_fact_mem", "intel_summary",
    "master_intelligence", "rag", "bionic_quorum", "queen_calibrator",
    "queen_feedback", "queen_primer", "queen_memory", "content_quality",
    "drones",
]

@pytest.fixture(autouse=True)
def mock_pg_connect_retry(monkeypatch):
    """Return SINGLETON PgConnection for all engine modules.
    
    KEY FIX: PgConnection converts ? → %s (psycopg2 compatibility).
    Without this, modules using ? placeholders silently fail in try/except blocks
    → 0 coverage on the lines after the SQL call.
    
    Singleton = 1 DB connection for entire session (no exhaustion).
    """
    pgconn = _get_shared_pgconn()
    
    def _return_pgconn(*args, **kwargs):
        return pgconn
    
    # Patch in ALL modules
    for mod in _ALL_ENGINE_MODULES:
        for func_name in ["_pg_connect_retry", "_connect", "_pg_connect", "_get_pg",
                         "_db_connect", "connect"]:
            try:
                monkeypatch.setattr(f"behive.engine.{mod}.{func_name}", _return_pgconn, raising=False)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# Env setup — point engine at real DB
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def set_pg_env(monkeypatch):
    """Ensure engine finds the DB."""
    monkeypatch.setenv("HIVE_PG_HOST", "localhost")
    monkeypatch.setenv("HIVE_PG_PORT", "5432")
    monkeypatch.setenv("HIVE_PG_DATABASE", "hive")
    monkeypatch.setenv("HIVE_PG_USER", "hive_app")
    monkeypatch.setenv("HIVE_PG_PASSWORD", "bH9_xK2m_pR7v_2026")
    monkeypatch.setenv("BEHIVE_DB_URL", "postgresql://hive_app:bH9_xK2m_pR7v_2026@localhost:5432/hive")
    # Disable external calls
    monkeypatch.setenv("BEHIVE_MAX_CONCURRENT", "1")
    monkeypatch.setenv("BEHIVE_PHASE_TIMEOUT", "5")
