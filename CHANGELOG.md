# Changelog

All notable changes to BeHive are documented here.

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
