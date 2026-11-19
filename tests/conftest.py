"""conftest.py — shared fixtures for test_80_* integration tests.

Strategy: SINGLETON PgConnection + mocked LLM/HTTP.
PgConnection wraps psycopg2 with ? → %s conversion (required by engine modules).
ONE connection for entire test session → no connection exhaustion.
"""
import pytest
import psycopg2
import os
import json
from unittest.mock import MagicMock


# ═══════════════════════════════════════════════════════════════════════════════
# Test selection — private PG harness tests and live tests are not part of the
# default (main suite) run. They are skipped with a reason, never silently dropped.
# ═══════════════════════════════════════════════════════════════════════════════

def pytest_collection_modifyitems(config, items):
    has_pg_harness = bool(os.environ.get("CLAIM_TEST_PG_DSN"))
    run_live = os.environ.get("BEHIVE_RUN_LIVE") == "1"
    skip_pg = pytest.mark.skip(
        reason="pg_harness: needs the private PostgreSQL harness (CLAIM_TEST_PG_DSN, run_suite.sh pg)")
    skip_live = pytest.mark.skip(reason="live: needs internet, Qdrant or an LLM key; set BEHIVE_RUN_LIVE=1")
    for item in items:
        if not has_pg_harness and item.get_closest_marker("pg_harness") is not None:
            item.add_marker(skip_pg)
        if not run_live and item.get_closest_marker("live") is not None:
            item.add_marker(skip_live)

# ═══════════════════════════════════════════════════════════════════════════════
# SINGLETON PgConnection — ONE connection for ALL tests
# ═══════════════════════════════════════════════════════════════════════════════

_DEFAULT_PG_DSN = ("host=localhost port=5432 dbname=hive user=hive_app password="
                   + os.environ.get('HIVE_PG_PASSWORD', 'test'))
# Target of every connection this conftest opens (raw fixtures, engine pool, env for
# code that reads BEHIVE_DB_URL). Override with BEHIVE_TEST_PG_DSN.
PG_DSN = os.environ.get("BEHIVE_TEST_PG_DSN") or _DEFAULT_PG_DSN


# ═══════════════════════════════════════════════════════════════════════════════
# Safety guard — the fixtures below write (PgConnection(read_only=False)) and some
# tests TRUNCATE. On the BeHive host localhost:5432/hive IS the production database.
# ═══════════════════════════════════════════════════════════════════════════════

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _network_isolated() -> bool:
    """True inside a network namespace whose only interface is loopback.

    /proc/self/net/dev is per network namespace (unlike /sys/class/net, which shows
    the namespace that mounted sysfs).
    """
    try:
        with open("/proc/self/net/dev", encoding="utf-8") as handle:
            names = {line.split(":", 1)[0].strip() for line in handle.readlines()[2:] if ":" in line}
    except OSError:
        return False
    return names <= {"lo"}


def _describe_dsn(dsn: str) -> str:
    from psycopg2.extensions import parse_dsn
    params = parse_dsn(dsn)
    return "host={} port={} dbname={}".format(params.get("host", "?"), params.get("port", "5432"),
                                               params.get("dbname", "?"))


def _test_db_refusal(dsn: str, *, consent: str | None, isolated: bool) -> str | None:
    """Why a session must not use ``dsn`` (None when allowed).

    Requires explicit consent (BEHIVE_TEST_DB=1). A database named 'hive' is the
    production name: it is accepted only over loopback TCP inside an isolated network
    namespace (the private copy of the harness), never over a Unix socket (socket
    paths are shared across network namespaces) or a remote host.
    """
    from psycopg2.extensions import parse_dsn
    if consent != "1":
        return "BEHIVE_TEST_DB=1 is not set (explicit consent that the target is a disposable test database)"
    params = parse_dsn(dsn)
    if params.get("dbname") != "hive":
        return None
    host = params.get("host", "")
    if host.startswith("/") or host == "":
        return "database 'hive' over a Unix socket can be the production database"
    if host not in _LOOPBACK_HOSTS:
        return "database 'hive' on a non-loopback host can be a production database"
    if not isolated:
        return "localhost:5432/hive outside an isolated network namespace is the production database"
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Direct psycopg2.connect calls (R-26)
#
# About 26 test files call psycopg2.connect(host="localhost", port=5432, dbname="hive", ...)
# themselves, bypassing PG_DSN and the guard above. On the BeHive host that address is the
# production database. pytest_configure wraps psycopg2.connect once for the whole session:
# the legacy default target is redirected to BEHIVE_TEST_PG_DSN when it is set, and refused
# outside an isolated network namespace otherwise. Other targets are left alone.
# ═══════════════════════════════════════════════════════════════════════════════

_REAL_PG_CONNECT = psycopg2.connect

# Keys that choose WHERE a connection goes; everything else (connect_timeout,
# application_name, options, ...) is carried over to the redirected connection.
_TARGET_KEYS = {"host", "hostaddr", "port", "dbname", "database", "user", "password", "service",
                "passfile", "sslmode", "sslrootcert", "sslcert", "sslkey", "sslcrl",
                "krbsrvname", "gsslib", "target_session_attrs", "channel_binding"}
_CONNECT_ONLY_KWARGS = ("dsn", "connection_factory", "cursor_factory")


class ProductionDatabaseRefused(RuntimeError):
    """A test tried to open the legacy default database (production 'hive' on this host)."""


def _connect_params(args: tuple, kwargs: dict) -> dict | None:
    """Effective libpq parameters of a psycopg2.connect call; None when they cannot be parsed
    (the real connect then raises its own error)."""
    from psycopg2.extensions import make_dsn, parse_dsn
    dsn = args[0] if args else kwargs.get("dsn")
    extra = {k: v for k, v in kwargs.items() if k not in _CONNECT_ONLY_KWARGS}
    try:
        return parse_dsn(make_dsn(dsn, **extra))
    except psycopg2.Error:
        return None


def _target_of(params: dict) -> tuple[str, str, str]:
    """(host, port, dbname) with the libpq environment defaults applied."""
    host = params.get("host") or params.get("hostaddr") or os.environ.get("PGHOST", "")
    port = str(params.get("port") or os.environ.get("PGPORT") or "5432")
    dbname = (params.get("dbname") or os.environ.get("PGDATABASE") or params.get("user")
              or os.environ.get("PGUSER") or "")
    return host, port, dbname


def _is_legacy_default(params: dict) -> bool:
    """dbname 'hive' on a loopback host, over a Unix socket, or with no host at all."""
    host, _port, dbname = _target_of(params)
    return dbname == "hive" and (host == "" or host in _LOOPBACK_HOSTS or host.startswith("/"))


def _guarded_pg_connect(*args, **kwargs):
    params = _connect_params(args, kwargs)
    if params is None:
        return _REAL_PG_CONNECT(*args, **kwargs)
    override = os.environ.get("BEHIVE_TEST_PG_DSN")
    if override and _target_of(params) == _target_of(_connect_params((override,), {}) or {}):
        return _REAL_PG_CONNECT(*args, **kwargs)  # already the guarded target
    if not _is_legacy_default(params):
        return _REAL_PG_CONNECT(*args, **kwargs)
    if override:
        from psycopg2.extensions import make_dsn
        carried = {k: v for k, v in params.items() if k not in _TARGET_KEYS}
        passthrough = {k: v for k, v in kwargs.items() if k in ("connection_factory", "cursor_factory")}
        return _REAL_PG_CONNECT(make_dsn(override, **carried), *args[1:], **passthrough)
    if _network_isolated():
        return _REAL_PG_CONNECT(*args, **kwargs)  # the harness: localhost:5432/hive is the private copy
    raise ProductionDatabaseRefused(
        "tests/conftest.py refuses a direct psycopg2.connect to the legacy default ({}): outside an "
        "isolated network namespace that is the production database. Set BEHIVE_TEST_PG_DSN to a "
        "disposable database, or run the isolated harness.".format(
            "host={} port={} dbname={}".format(*_target_of(params))))


def _install_pg_connect_guard() -> None:
    if psycopg2.connect is not _guarded_pg_connect:
        psycopg2.connect = _guarded_pg_connect


def pytest_configure(config):
    reason = _test_db_refusal(PG_DSN, consent=os.environ.get("BEHIVE_TEST_DB"), isolated=_network_isolated())
    if reason:
        pytest.exit(
            "Refusing to start: tests/conftest.py connects read-write to " + _describe_dsn(PG_DSN)
            + " and some tests TRUNCATE tables; " + reason + ". Run the suite in the isolated harness "
            "(private PostgreSQL copy, own network namespace, BEHIVE_TEST_DB=1) or point BEHIVE_TEST_PG_DSN "
            "at a disposable database and set BEHIVE_TEST_DB=1.",
            returncode=4,
        )
    _install_pg_connect_guard()
    # One target for the whole session: the engine pool uses the same database.
    import behive.engine.db as _db_mod
    _db_mod.DSN = PG_DSN
    _db_mod._pool = None

# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline children left behind by a test (R-36)
#
# The API's _run_pipeline starts `python -m behive.engine run ...` as a real subprocess and
# cmd_run begins with cleanup_stuck_missions(max_age_hours=1). A child that outlives its test
# marks the old missions of a LATER test 'error' while that test runs. After every test, a
# still-running direct child of the pytest process that runs behive.engine (any module), the
# orchestrator or a hive2_ script is terminated (killed after a short wait) and recorded;
# pytest_terminal_summary prints the record (expected: empty).
# ═══════════════════════════════════════════════════════════════════════════════

import re
import signal
import time

_PIPELINE_CHILD = re.compile(r"behive[./]engine(?![\w])|orchestrator|hive2_")
_LEAKED_CHILDREN: list[tuple[str, str]] = []  # (test node id, command line)


def _running_children(parent: int) -> list[tuple[int, str]]:
    """(pid, command line) of the live (not zombie) direct children of ``parent`` (Linux /proc)."""
    found = []
    try:
        pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
                state, ppid = handle.read().rsplit(")", 1)[1].split()[:2]
            if int(ppid) != parent or state in ("Z", "X"):
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                cmdline = handle.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except (OSError, ValueError, IndexError):
            continue  # ended meanwhile
        found.append((pid, cmdline))
    return found


def _is_gone(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")
    except (OSError, IndexError):
        return True


def _reap_leaked_children(test_id: str, grace: float = 3.0) -> None:
    """SIGTERM, then after ``grace`` seconds SIGKILL, every pipeline child still running."""
    victims = [(pid, cmd) for pid, cmd in _running_children(os.getpid()) if _PIPELINE_CHILD.search(cmd)]
    if not victims:
        return
    for pid, cmd in victims:
        _LEAKED_CHILDREN.append((test_id, cmd))
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    pending = [pid for pid, _cmd in victims]
    while pending and time.monotonic() < deadline:
        pending = [pid for pid in pending if not _is_gone(pid)]
        if pending:
            time.sleep(0.05)
    for pid in pending:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@pytest.fixture
def no_real_pipeline(monkeypatch):
    """POST /research starts `python -m behive.engine run ...` through server._run_pipeline.
    Tests of the endpoint that do not follow the pipeline replace it with a no-op coroutine:
    a child outliving its test would run cleanup_stuck_missions on the shared database."""
    import behive.server

    async def _no_pipeline(*args, **kwargs):
        return None

    monkeypatch.setattr(behive.server, "_run_pipeline", _no_pipeline)


@pytest.fixture(autouse=True)
def reap_pipeline_children(request):
    yield
    _reap_leaked_children(request.node.nodeid)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _LEAKED_CHILDREN:
        return
    terminalreporter.section("behive pipeline children left running by tests (terminated)")
    for test_id, cmd in _LEAKED_CHILDREN:
        terminalreporter.write_line(f"{test_id}: {cmd}")


# Module-level singleton — created once, reused everywhere
_SHARED_PGCONN = None
_SHARED_RAW_CONN = None


def _get_shared_pgconn():
    """Get or create the singleton PgConnection.
    Resets pool on first call (important for pytest-xdist forked workers); a
    singleton closed by engine code is replaced from the existing pool.
    """
    global _SHARED_PGCONN
    stale = (_SHARED_PGCONN is not None and (
        getattr(_SHARED_PGCONN, "_conn", None) is None
        or getattr(getattr(_SHARED_PGCONN, "_pool", None), "closed", False)))
    if _SHARED_PGCONN is None or stale:
        import behive.engine.db as _db_mod
        if _SHARED_PGCONN is None:
            # Reset the pool for this worker (may be forked)
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
def pg_cursor():
    """Cursor on its own connection to PG_DSN; rows work by index and by column name
    (DictCursor). Uncommitted work is rolled back."""
    import psycopg2.extras
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    try:
        yield cur
    finally:
        conn.rollback()
        cur.close()
        conn.close()


@pytest.fixture(scope="module")
def behive_server_url():
    """URL of an already running BeHive server; opt-in only (never the production
    default 127.0.0.1:8091): set BEHIVE_TEST_URL."""
    url = os.environ.get("BEHIVE_TEST_URL")
    if not url:
        pytest.skip("BEHIVE_TEST_URL not set: needs a running BeHive server")
    return url.rstrip("/")


@pytest.fixture
def seeded_mission():
    """A self-contained mission in the test database (11 claims, 3 entities, 3 facts),
    removed afterwards. For tests that must not depend on production data being present
    (schema-only copies, CI). Ids: MAX(id)+1, as the tables have no id default."""
    import uuid
    mid = "hive_test_seed_" + uuid.uuid4().hex[:12]
    cur = _get_shared_raw_conn().cursor()
    try:
        cur.execute("INSERT INTO hive_missions (id, topic, status, phase, created_at) "
                    "VALUES (%s, %s, 'done', 'done', NOW())", (mid, "Installed solar capacity in Poland 2024"))
        for i in range(11):
            cur.execute("INSERT INTO hive_claims (id, mission_id, claim, source_url, claim_type, confidence, "
                        "quality_score, is_garbage) VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM hive_claims), "
                        "%s, %s, %s, 'fact', 0.8, 0.8, false)",
                        (mid, f"Installed solar capacity in region {i} reached {100 + i} MW in 2024.",
                         f"https://example.test/solar/{i}"))
        for etype, value in (("ORG", "Polskie Sieci Elektroenergetyczne"), ("LOC", "Wielkopolska"),
                             ("METRIC", "installed solar capacity")):
            cur.execute("INSERT INTO hive_entities (id, mission_id, entity_type, value, context, confidence) "
                        "VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM hive_entities), %s, %s, %s, %s, 0.9)",
                        (mid, etype, value, f"{value} appears in the 2024 report on solar capacity in Poland."))
        for i in range(3):
            cur.execute("INSERT INTO hive_facts (id, mission_id, value, unit, context, confidence) "
                        "VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM hive_facts), %s, %s, 'MW', %s, '0.8')",
                        (mid, 100.0 + i, f"Region {i} reached {100 + i} MW of installed solar capacity in 2024."))
        yield mid
    finally:
        for table in ("hive_facts", "hive_entities", "hive_claims"):
            cur.execute(f"DELETE FROM {table} WHERE mission_id = %s", (mid,))
        cur.execute("DELETE FROM hive_missions WHERE id = %s", (mid,))
        cur.close()


@pytest.fixture
def real_mission_id(request):
    """A real mission with claims; a seeded one when the database has none (schema-only copy)."""
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
    return row[0] if row else request.getfixturevalue("seeded_mission")


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

_RELEVANCE_SYSTEM_PREFIX = "You judge document relevance"       # RelevanceFilter._CONTENT_SYSTEM
_EXTRACTION_SYSTEM_PREFIX = "You select source-backed research claims"  # source_extraction


def _relevance_batch_reply(prompt: str) -> str:
    """A valid RelevanceFilter.score_batch answer: one grade per supplied index."""
    try:
        payload = json.loads(prompt.split("\n", 1)[1])
        indexes = [int(doc["index"]) for doc in payload["documents"]]
    except (IndexError, KeyError, TypeError, ValueError):
        return "[]"
    return json.dumps([{"index": i, "grade": 1, "reason": "test double: relevant"} for i in indexes])


def _smart_llm_mock(prompt="", stage="process", system="", max_tokens=4096,
                   temperature=0.3, json_mode=False, **kw):
    """LLM double whose answer depends on the kind of call (relevance batch,
    source extraction, planning, synthesis, generic extraction)."""
    global _call_count
    _call_count += 1

    if isinstance(system, str) and system.startswith(_RELEVANCE_SYSTEM_PREFIX):
        return _relevance_batch_reply(prompt if isinstance(prompt, str) else "")
    if isinstance(system, str) and system.startswith(_EXTRACTION_SYSTEM_PREFIX):
        return "[]"  # valid: no source-backed claims
    
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


def _patches_llm_provider(request) -> bool:
    """True when the test itself patches litellm.completion/acompletion (@patch)."""
    fn = getattr(request.node, "function", None)
    for patching in getattr(fn, "patchings", None) or []:
        if getattr(patching, "attribute", None) not in ("completion", "acompletion"):
            continue
        try:
            target = patching.getter()
        except Exception:
            continue
        if getattr(target, "__name__", "").split(".")[0] == "litellm":
            return True
    return False


@pytest.fixture(autouse=True)
def mock_llm_globally(monkeypatch, request):
    """Auto-mock all LLM calls so tests never hit real APIs.

    A test that patches the provider boundary itself (litellm.completion) exercises
    the real llm.complete() against its own double, with a test-only model name so
    the BYOK key check passes; no request leaves the process.
    """
    monkeypatch.setattr("behive.engine.llm.embed", lambda texts, **kw: [_MOCK_EMBED] if isinstance(texts, str) else [_MOCK_EMBED for _ in texts], raising=False)
    if _patches_llm_provider(request):
        if not os.environ.get("BEHIVE_MODEL"):
            monkeypatch.setenv("BEHIVE_MODEL", "openai/behive-test-double")
        return
    monkeypatch.setattr("behive.engine.llm.complete", _smart_llm_mock, raising=False)
    # The falsifier's ConflictResolver probes a local OpenAI-compatible proxy and
    # retries with sleeps; tests use its documented "no LLM client" path.
    monkeypatch.setattr("behive.engine.falsifier.ConflictResolver._init_client",
                        lambda self: None, raising=False)


@pytest.fixture(autouse=True)
def reset_rate_limiters():
    """server.py keeps module-level TokenBuckets (60/min, research 5/min): a test
    must not inherit another test's 429."""
    import sys
    server = sys.modules.get("behive.server")
    for name in ("_rate_limiter", "_research_limiter"):
        bucket = getattr(server, name, None) if server is not None else None
        if bucket is not None and hasattr(bucket, "buckets"):
            bucket.buckets.clear()
    yield


@pytest.fixture(autouse=True)
def mock_subprocess(monkeypatch, request):
    """Prevent any subprocess spawning (scout/harvest phase scripts).
    Skip for tests that need real subprocess (mark with @pytest.mark.real_subprocess).
    """
    if "real_subprocess" in request.keywords:
        return

    def fake_run(*a, **kw):
        # Same output type as subprocess.run: str with text=/universal_newlines=/encoding=.
        as_text = bool(kw.get("text") or kw.get("universal_newlines") or kw.get("encoding"))
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "OK" if as_text else b"OK"
        fake_result.stderr = "" if as_text else b""
        return fake_result
    monkeypatch.setattr("subprocess.run", fake_run, raising=False)


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
def pin_engine_database():
    """Each test starts with behive.engine.db bound to PG_DSN and an open pool: some
    tests reload the module (DSN recomputed from env) or close the shared pool."""
    import behive.engine.db as _db_mod
    if _db_mod.DSN != PG_DSN:
        _db_mod.DSN = PG_DSN
        _db_mod._pool = None
    elif _db_mod._pool is not None and _db_mod._pool.closed:
        _db_mod._pool = None
    yield


@pytest.fixture
def small_mission_id():
    """A real mission with 3-10 harvested documents (bounded pipeline runs on a full
    data copy); the historical fixed id on an empty database."""
    cur = _get_shared_raw_conn().cursor()
    try:
        cur.execute("SELECT mission_id FROM hive_content GROUP BY mission_id "
                    "HAVING COUNT(*) BETWEEN 3 AND 10 ORDER BY mission_id DESC LIMIT 1")
        row = cur.fetchone()
    finally:
        cur.close()
    return row[0] if row else "hive_1785496253_3b165f"


@pytest.fixture(autouse=True)
def mock_pg_connect_retry(monkeypatch, pin_engine_database):
    """Return SINGLETON PgConnection for all engine modules.
    
    KEY FIX: PgConnection converts ? → %s (psycopg2 compatibility).
    Without this, modules using ? placeholders silently fail in try/except blocks
    → 0 coverage on the lines after the SQL call.
    
    Singleton = 1 DB connection for entire session (no exhaustion).
    """
    _get_shared_pgconn()

    def _return_pgconn(*args, **kwargs):
        # Engine code closes the connections it opens (events.emit_event,
        # db_helpers callers): hand out a live singleton every time instead of a
        # closed one (closed PgConnection -> "'NoneType' object has no attribute 'cursor'").
        return _get_shared_pgconn()
    
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
    """Point code that reads the environment at the same database as PG_DSN."""
    from urllib.parse import quote
    from psycopg2.extensions import parse_dsn
    params = parse_dsn(PG_DSN)
    host, port = params.get("host", "localhost"), params.get("port", "5432")
    user, password, dbname = params.get("user", "hive_app"), params.get("password", ""), params.get("dbname", "hive")
    monkeypatch.setenv("HIVE_PG_HOST", host)
    monkeypatch.setenv("HIVE_PG_PORT", str(port))
    monkeypatch.setenv("HIVE_PG_DATABASE", dbname)
    monkeypatch.setenv("HIVE_PG_USER", user)
    monkeypatch.setenv("HIVE_PG_PASSWORD", password)
    monkeypatch.setenv("BEHIVE_DB_URL", f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{dbname}")
    # Disable external calls
    monkeypatch.setenv("BEHIVE_MAX_CONCURRENT", "1")
    monkeypatch.setenv("BEHIVE_PHASE_TIMEOUT", "5")
