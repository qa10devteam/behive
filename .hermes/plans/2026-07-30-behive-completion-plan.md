# BeHive Completion Plan — "Ship It Right"

**Created:** 2026-07-30
**Goal:** Transform BeHive from "promising prototype with 100 stars" → "production research engine people trust"
**Scope:** Everything needed for a user to `pip install behive`, run a mission, get results.
**Standard:** gstack-level test coverage, superpowers-level methodology discipline.

---

## Current Reality (Brutal Honesty)

| Metric | Now | Target |
|--------|-----|--------|
| Success rate | 74-80% | 90%+ |
| Fresh install works | ❌ crashes | ✅ pip install → serve → research |
| Test coverage | 6.5% (1.2K/19K) | 40%+ (8K+/19K) |
| Local↔Package sync | 0% (34 local files, 0 in pkg) | 100% (single source) |
| Docs | 0 pages | 10+ pages (quickstart, API, config, deploy) |
| Docker "just works" | untested | ✅ `docker compose up` → working system |
| behive.dev | DNS dead | live docs site |
| PyPI __version__ | 0.3.0 in code | matches pyproject.toml |
| MCP server | not running | ✅ auto-starts with serve |
| Stuck missions cleanup | 140 zombie missions | auto-cleanup cron |

---

## Architecture: P0 — CRITICAL PATH (ship-blocking)

### Phase 1: Foundation — Make Fresh Install Work (15 tasks)

**Problem:** `pip install behive && behive serve` crashes because FastAPI is in `[api]` optional deps, but the main command expects it.

1. Move `fastapi`, `uvicorn`, `sse-starlette` from `[api]` to core dependencies
2. Fix `__init__.py` version → read from `importlib.metadata` (single source of truth)
3. Add `behive serve` graceful error when no DB configured (not crash)
4. Add `behive db init` that creates tables from bundled `init-db.sql` against user's PostgreSQL
5. Fix `behive serve` to also start MCP endpoint (single process, dual port)
6. Add `behive config` interactive wizard (asks for DB URL, LLM key, validates)
7. Test: fresh venv → `pip install behive` → `behive --help` → all commands listed
8. Test: `pip install behive` → `behive serve` (no DB) → clear error message with setup instructions
9. Test: `pip install behive` → configure DB → `behive serve` → health returns 200
10. Test: `behive research "test query" --depth 1` end-to-end (depth 1 = fast)
11. Add `behive doctor` command — checks DB, LLM, Playwright, reports status
12. Ensure `behive[all]` actually installs everything needed for full functionality
13. Pin minimum Python version (3.10+) and test
14. Add py.typed marker for IDE support
15. Version sync: pyproject.toml = server.py = cli.py = __init__.py (use importlib.metadata)

### Phase 2: Engine Reliability — 90% Success Rate (25 tasks)

**Problem:** 56 timeouts, 54 stuck scouts, 30 NULL phase. 20% failures.

16. Audit ALL stuck missions — categorize failure reasons (search failures, LLM timeouts, Playwright crashes)
17. Implement scout retry with exponential backoff (current: single attempt)
18. Add graceful Playwright fallback when browser crashes (catch EPIPE, retry without browser)
19. Implement mission timeout watchdog — kills stuck missions after configurable deadline
20. Fix NULL phase missions — add DB constraint `phase NOT NULL DEFAULT 'created'`
21. Add health check heartbeat per mission (last_heartbeat_at column, stale = stuck)
22. Implement concurrency limiter — max 3 simultaneous missions (prevent resource exhaustion)
23. Fix harvest phase failures — graceful skip when individual URL fails
24. Fix synth phase timeout — add streaming synthesis with partial progress saves
25. Add circuit breaker for LLM calls — track failures, back off on repeated errors
26. Implement search backend rotation — if Chromium fails, try SearXNG → Brave → Serper
27. Add mission progress tracking — percent complete, phase timing, estimated completion
28. Fix falsifier combinatorial explosion (depth 5 + many claims → infinite batches)
29. Add automatic stuck mission cleanup cron (every 15 min, mark old scouts as error)
30. Implement graceful degradation — partial results better than total failure
31. Add per-phase retry — failed harvest retries URLs, failed process retries claims
32. Track timing per phase — identify bottlenecks (add phase_started_at, phase_duration columns)
33. Implement quality-gate before synth — skip synthesis if <3 quality claims
34. Fix "0 sources found" edge case — clear error message, suggest different query
35. Add depth auto-selection — simple queries use depth 1-2, complex use 3-5
36. Benchmark: run 20 missions in sequence, measure success rate, log failures
37. Fix DDG rate limiting — add per-source rate limits, respect 429 backoff
38. Implement source deduplication — same URL harvested once per mission, not N times
39. Add mission cancellation endpoint — POST /research/{id}/cancel
40. Ensure all errors write to hive_missions.error_message (currently some are NULL)

### Phase 3: Code Quality — Single Source of Truth (20 tasks)

**Problem:** 34 local scripts (27K LOC) diverged from package (19K LOC). Running server uses old paths.

41. Audit local↔package divergence — which local files have NEWER code than package?
42. Create sync script — diff each local file with package counterpart
43. Port missing modules: hive2_falsifier.py, hive2_rag.py, hive2_synth.py improvements
44. Port missing modules: hive2_queen_tools.py, hive2_scout_targeted.py
45. Port missing modules: hive2_intel_summary.py, hive2_master_intelligence.py
46. Remove old ~/hive2_*.py files after confirmed port (archive branch first)
47. Run ALL engine modules through import test: `python3 -c "from behive.engine.X import *"`
48. Add type hints to all public functions (at least parameter types)
49. Replace all `print()` with `logging.getLogger(__name__)` calls
50. Remove all TODO/FIXME/HACK comments that reference internal infrastructure
51. Scan for hardcoded paths (/home/ubuntu, localhost specific ports)
52. Add `__all__` exports to every engine module
53. Ensure every module has a docstring explaining its role in the pipeline
54. Run ruff/flake8 — fix all errors, add to CI
55. Create engine architecture diagram (Mermaid in README)
56. Write CONTRIBUTING.md with dev setup instructions
57. Update server.py to use packaged engine (not sys.path hacks)
58. Update launcher to be the CLI command only (no custom launch scripts)
59. Add integration test: `pip install -e . && behive research "test" --depth 1`
60. All imports must be relative within package (`from .scout import ...` not absolute)

### Phase 4: Test Suite — From 6.5% to 40%+ (30 tasks)

**Standard:** gstack has 28% test-to-code ratio. We target 40% minimum.

61. Unit tests: `test_llm.py` — mock litellm, test BYOK detection, model routing, retry
62. Unit tests: `test_search_backends.py` — mock each backend, test fallthrough chain
63. Unit tests: `test_scout.py` — mock search results, test URL extraction, dedup
64. Unit tests: `test_harvest.py` — mock HTTP responses, test content extraction
65. Unit tests: `test_process.py` — mock LLM, test claim extraction, scoring
66. Unit tests: `test_synth.py` — mock LLM, test report generation
67. Unit tests: `test_orchestrator.py` — mock all phases, test state machine
68. Unit tests: `test_db_helpers.py` — test connection, migration, query helpers
69. Unit tests: `test_quality.py` — test score calculation, threshold filtering
70. Unit tests: `test_preprocessor.py` — test query analysis, specificity scoring
71. Integration tests: `test_api.py` — FastAPI TestClient, all 12 endpoints
72. Integration tests: `test_cli.py` — subprocess calls to each CLI command
73. Integration tests: `test_mcp.py` — MCP protocol compliance
74. Integration tests: `test_mission_lifecycle.py` — create → scout → harvest → process → synth
75. Integration tests: `test_docker.py` — docker compose up, health check, research, down
76. E2E test: `test_e2e_fresh_install.py` — in tmpdir venv, pip install, configure, run
77. Performance tests: `test_concurrent.py` — 3 simultaneous missions don't deadlock
78. Performance tests: `test_memory.py` — memory usage stays bounded during missions
79. Regression tests: for each bug fixed (DOUBLE PRECISION, connect_retry, etc.)
80. Add pytest fixtures: mock DB, mock LLM responses, mock HTTP for search
81. Add test data: sample search results, sample HTML pages, sample LLM responses
82. CI: GitHub Actions workflow — lint + test on push (Python 3.10, 3.11, 3.12)
83. CI: Separate workflow for integration tests (needs PostgreSQL service)
84. Test coverage reporting — generate HTML report, upload to codecov
85. Property-based tests: hypothesis for claim parsing, URL extraction
86. Snapshot tests: golden file comparison for report output format
87. Test the test fixtures themselves (ensure mocks are representative)
88. Add `conftest.py` with reusable DB setup/teardown
89. Benchmark test: measure median mission time, alert if regression
90. Fuzz test: random queries, ensure no unhandled exceptions

---

## Architecture: P1 — PRODUCT QUALITY (week 2-3)

### Phase 5: Documentation — From Zero to Useful (15 tasks)

91. Create `docs/` directory with mkdocs or docusaurus config
92. Write `docs/quickstart.md` — pip install → first research in 5 minutes
93. Write `docs/configuration.md` — all env vars, config options, LLM setup
94. Write `docs/api-reference.md` — all REST endpoints with curl examples
95. Write `docs/mcp-guide.md` — how to use with Claude, Hermes, other MCP clients
96. Write `docs/deployment.md` — Docker Compose, systemd, cloud deployment
97. Write `docs/architecture.md` — how the pipeline works, phase diagram
98. Write `docs/troubleshooting.md` — common errors and fixes
99. Write `docs/contributing.md` — dev setup, running tests, PR guidelines
100. Write `docs/changelog.md` — real changelog from git history
101. Add API docs auto-generation from FastAPI (Swagger UI at /docs)
102. Create GitHub Wiki pages mirroring docs/ for discoverability
103. Add inline code examples in docstrings (for IDE tooltips)
104. Create animated terminal GIF for README (asciinema)
105. Fix behive.dev domain (point to docs or GitHub Pages)

### Phase 6: Docker & Deployment — "Just Works" (12 tasks)

106. Test `docker compose up` from scratch — fix any issues
107. Add `.env.example` with all required vars documented
108. Add health check in docker-compose for behive service
109. Add depends_on with health checks (postgres ready before behive starts)
110. Test `docker compose --profile full up` (with Neo4j, Qdrant)
111. Add docker-compose.override.yml example for development
112. Test ARM64 compatibility (Apple Silicon users)
113. Add Dockerfile multi-stage build (smaller final image)
114. Create Helm chart basics for K8s deployment
115. Add systemd service file example in docs
116. Test compose on fresh machine (no pre-existing volumes)
117. Add backup/restore instructions for PostgreSQL data

### Phase 7: Developer Experience (12 tasks)

118. Add `behive shell` — interactive Python REPL with client pre-configured
119. Add `behive missions --format json` for scripting
120. Add `behive claims search "query"` CLI command
121. Add progress bar during `behive research` CLI runs
122. Add `--verbose` flag for debugging (shows each phase, timing)
123. Add webhook/callback support (notify URL when mission completes)
124. Add structured logging (JSON format option for log aggregation)
125. Add OpenTelemetry traces (optional, for observability)
126. Create VS Code extension that shows BeHive status
127. Add `behive benchmark` command — runs test suite of queries, reports metrics
128. Add `behive export` — export claims/entities to CSV/JSON
129. Add `behive import` — import from other research tools

---

## Architecture: P2 — COMPETITIVE EDGE (week 3-4)

### Phase 8: Intelligence Features — Beyond "Just Research" (15 tasks)

130. Implement `/intelligence/entity/{name}` fully — co-occurrence graph
131. Implement `/intelligence/network/{name}` — entity relationship map
132. Implement `/intelligence/stats` — knowledge base statistics
133. Add cross-mission knowledge linking — entity mentions across missions
134. Add temporal tracking — how entities evolve over time
135. Add contradiction detection — flag claims that contradict each other
136. Add claim provenance chain — which source → which claim → which conclusion
137. Implement mission comparison — diff two research results
138. Add "research alerts" — notify when new info contradicts existing claims
139. Add custom LLM prompts per pipeline stage (user can override synth template)
140. Add multi-language support (query in any language, results in requested language)
141. Add citation format export (BibTeX, APA, Chicago)
142. Add research templates (competitive analysis, market research, due diligence)
143. Implement follow-up research — "dig deeper on claim X"
144. Add collaborative missions — multiple users contribute sources

### Phase 9: Performance & Scale (10 tasks)

145. Add Redis caching for search results (avoid re-fetching)
146. Implement async pipeline (all phases run as async tasks, not subprocess)
147. Add connection pooling for PostgreSQL (asyncpg or psycopg pool)
148. Optimize claim storage — batch inserts instead of one-by-one
149. Add query result caching — same query within 24h returns cached
150. Implement incremental research — add new sources to existing mission
151. Add rate limiting for API (prevent abuse)
152. Benchmark and optimize synth phase (largest bottleneck)
153. Add database migrations (Alembic) for schema evolution
154. Implement sharding strategy for 1M+ claims

### Phase 10: Community & Ecosystem (10 tasks)

155. Create issue templates (bug report, feature request, research template request)
156. Create PR template (with checklist, inspired by superpowers 94% rejection approach)
157. Add SECURITY.md (responsible disclosure policy)
158. Add CODE_OF_CONDUCT.md
159. Create GitHub Discussions (Q&A, Show & Tell)
160. Add "Powered by BeHive" badge for users
161. Create example integrations (n8n, Hermes, Claude, LangChain)
162. Add plugin system — custom search backends, custom processors
163. Create skill for Hermes agent (already exists but needs update)
164. Community showcase — featured research reports

---

## Execution Strategy

### Week 1: Phases 1-2 (Foundation + Reliability)
- **Day 1-2:** Fresh install fix (tasks 1-15)
- **Day 3-5:** Reliability sprint (tasks 16-40)
- **Deliverable:** `pip install behive && behive serve` works. 90% success rate.

### Week 2: Phases 3-4 (Code Quality + Tests)
- **Day 6-8:** Sync local→package, cleanup (tasks 41-60)
- **Day 9-12:** Test suite build (tasks 61-90)
- **Deliverable:** Single source of truth. 40%+ test coverage. CI green.

### Week 3: Phases 5-7 (Docs + Docker + DX)
- **Day 13-15:** Documentation (tasks 91-105)
- **Day 16-17:** Docker (tasks 106-117)
- **Day 18-19:** DX improvements (tasks 118-129)
- **Deliverable:** Complete docs site. Docker works. Good CLI.

### Week 4: Phases 8-10 (Intelligence + Scale + Community)
- **Day 20-22:** Intelligence features (tasks 130-144)
- **Day 23-25:** Performance (tasks 145-154)
- **Day 26-28:** Community (tasks 155-164)
- **Deliverable:** Competitive product. Scalable. Community-ready.

---

## Success Criteria (Definition of Done)

1. ✅ `pip install behive && behive serve` works on fresh machine with only PostgreSQL + LLM key
2. ✅ `behive research "topic"` completes successfully 90%+ of the time
3. ✅ `docker compose up` → full system running in <2 minutes
4. ✅ README claims match reality 100%
5. ✅ Test suite: 40%+ coverage, CI green
6. ✅ Docs: someone new can set up and use BeHive without reading source code
7. ✅ No secrets in git history
8. ✅ No hardcoded infrastructure in product code
9. ✅ behive.dev resolves to documentation
10. ✅ Community PRs responded to within 24h

---

## Anti-Patterns to Avoid (Lessons from the Past)

- ❌ Marketing before product works
- ❌ Claiming features in README that don't exist
- ❌ Pushing code without running tests
- ❌ Leaving secrets in git
- ❌ Ignoring community PRs
- ❌ Local scripts diverging from package
- ❌ "It works on my EC2" = shipped
- ❌ Sigmoid rescaling to inflate quality metrics
- ❌ Fixing one bug at a time when systemic audit is needed
