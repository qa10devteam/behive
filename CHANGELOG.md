# Changelog

All notable changes to BeHive are documented here.

## [0.7.0] — 2026-11-17

### Added
- Mission status is honest: `done` requires a report and at least one useful (non-`regex_fallback`) claim; API and MCP show the reason for `empty` and `error`
- Every model call in the process phase is capped by the phase deadline (`BEHIVE_PHASE_DEADLINE`); the process phase ends by itself before the deadline
- Test-suite safety: `tests/conftest.py` refuses to start without `BEHIVE_TEST_DB=1` and a non-production target
- Seeded mission fixtures (`seeded_mission`) so the main suite no longer depends on production data
- Blocking ruff checks (F821, F811, F601) in CI

### Fixed
- Scout sequence `hive_sources_id_seq` is never dropped; it is created only when missing and never rewound
- DDL guard: `information_schema` is checked before `ALTER TABLE ... ADD COLUMN`
- Wikipedia requests carry the BeHive User-Agent on every request, including shared sessions
- `queen_feedback._db_connect` recursion (hung the test run)
- Dead `prescout` INSERT referencing removed modules
- API Scout takes `hive_content` ids from `hive_content_seq`, as harvest does (no more `hive_content_pkey` collisions)
- CI test step sets the consent env (`BEHIVE_TEST_DB=1`) required by the conftest guard

### Changed
- Process-phase model calls go through `llm.complete` only; SGLang is used only when a local server answers
- Removed dead `run_full_coverage.sh` (referenced nonexistent paths)

## [0.6.0] — 2026-08-13

### Added
- **Entity Anchoring** — Queen task generation enforces 60%+ entity-specific queries (eliminates topic drift)
- **direct_url scout method** — fetch known API endpoints directly (KRS, VAT Biała Lista, company sites)
- **Context Providers subsystem** — pluggable context injection for domain-specific knowledge
- **AI Brand Visibility Checker** — free tool endpoint for brand SERP analysis
- **Federated Knowledge Network** — cross-instance knowledge sharing protocol
- **llms.txt** — AI discoverability metadata

### Fixed
- Graph engine single-instance PID lock prevents zombie process accumulation
- PostgreSQL schema compatibility (DuckDB syntax migration)

### Security
- All hardcoded database credentials removed from test files
- Production password rotated; credentials now read from environment variables only
- Git history scrubbed of leaked test credentials (BFG)

### Changed
- Synth module streamlined (491-line rewrite, faster report generation)
- API scout sources expanded (+152 entries, 70+ categories)

## [0.1.0] — 2026-07-28

### Added
- Initial public release
- V4 pipeline: dual-model extraction (Haiku bulk + Sonnet enrichment)
- Quality scoring system (5 dimensions, 0.55 gate, 0.82+ avg achieved)
- REST API with 27 endpoints + SSE streaming
- MCP server (Streamable HTTP, port 8090)
- Knowledge graph integration (Neo4j)
- 70+ API sources across 37 categories
- 8-layer stealth drone architecture
- Docker Compose self-hosted setup
- Agent integrations (Hermes, OpenClaw, Claude Desktop, Cursor, n8n)
- n8n community node (`n8n-nodes-behive` on npm)
- PyPI package (`pip install behive`)
- CLI with animated research visualization

### Benchmarks (v0.1.0)
| Topic | Claims | Quality | Duration |
|-------|--------|---------|----------|
| NVIDIA GPU market | 290 | 0.797 | 8 min |
| OpenAI GPT-5 | 574 | 0.789 | 12 min |
| EU AI Act | 267 | 0.759 | 6 min |
| Meta Llama 4 | 568 | 0.821 | 11 min |
