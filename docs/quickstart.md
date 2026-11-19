# BeHive Quickstart Guide

Get a research mission running in 5 minutes.

## Prerequisites

- Python 3.10+
- PostgreSQL 14+ (running and accessible)
- An LLM API key (OpenAI, Anthropic, Groq, Mistral, or any litellm-supported provider)

## Installation

```bash
pip install behive
```

For full functionality (web scraping, stealth crawling, advanced NLP):
```bash
pip install "behive[all]"
```

## Setup

### 1. Create Database

```bash
createdb behive
```

### 2. Configure Environment

```bash
# Required: LLM provider (pick one)
export OPENAI_API_KEY=sk-...
# OR
export ANTHROPIC_API_KEY=sk-ant-...
# OR
export GROQ_API_KEY=gsk_...

# Required: Database
export BEHIVE_DB_URL=postgresql://user:password@localhost:5432/behive

# Optional: Custom model (any litellm-compatible string)
export BEHIVE_MODEL=anthropic/claude-sonnet-4-20250514
```

### 3. Initialize Schema

```bash
behive db init
```

### 4. Verify Setup

```bash
behive doctor
```

Expected output:
```
🐝 BeHive Doctor v0.4.0

  ✓ Python       3.11.x
  ✓ Database     connected (40 tables, 0 missions)
  ✓ LLM          configured (OpenAI)
  ○ Browser      not installed (optional)
  ○ Neo4j        not configured (optional)
  ○ Qdrant       not configured (optional)

  All checks passed. BeHive is ready.
```

## Run Your First Research

### CLI Mode

```bash
# Quick research (depth 1 = fast, 1-3 minutes)
behive research "NVIDIA GPU market share 2025" --depth 1

# Standard research (depth 3 = balanced, 5-15 minutes)
behive research "Kubernetes vs Docker Swarm comparison" --depth 3

# Deep research (depth 5 = thorough, 15-45 minutes)
behive research "EU AI Act impact on startups" --depth 5
```

### API Mode

Start the server:
```bash
behive serve
```

Then make requests:
```bash
# Start a research mission
curl -X POST http://localhost:8091/research \
  -H "Content-Type: application/json" \
  -d '{"query": "React vs Vue 2025", "depth": 3}'

# Check status (status, phase, reason, report_available, ...)
curl http://localhost:8091/research/{job_id}/status

# Get results (HTTP 409 with the reason if the mission is empty or failed)
curl http://localhost:8091/research/{job_id}/report
```

Reasoning effort is optional: add `"reasoning_effort"` (whole mission) and/or
`"synthesis_effort"` (report synthesis only) to the `POST /research` body, each `low`,
`medium` or `high` (anything else returns HTTP 422). Without them nothing changes and the
model bridge keeps its default, `low`; with only `reasoning_effort` set, the synthesis uses
it too. Example: `-d '{"query": "React vs Vue 2025", "reasoning_effort": "medium", "synthesis_effort": "high"}'`.
The MCP tool `research_topic` takes the same two fields.

### Mission status

A mission ends in one of three statuses:

- `done`: the mission has a report and at least one useful claim.
- `empty`: the pipeline finished but produced no qualifying claims, so there is no report.
- `error`: the pipeline failed or timed out and produced no report.

A mission that is cancelled reports `cancelled` with the reason "Mission was cancelled."
The terminal statuses are therefore `done`, `empty`, `error` and `cancelled`.
While a mission runs its status is one of `queued`, `running`, `planning`, `scout`,
`harvest`, `process` and `synth`: it starts as `queued`, becomes `running`, and while the
pipeline runs `status` carries the current phase name. Clients should treat any status
other than the four terminal ones as still running. `phase` moves through
`queued`, `scout`, `harvest`, `process` and `synth`. When a mission ends as `empty` or
`error`, `phase` holds the stage or cause where it ended (one of the keys below) and
`reason` explains it.

`GET /research/{job_id}/status` returns these fields:

- `status`: terminal `done`, `empty`, `error` or `cancelled`; while running `queued`, `running`, `planning`, `scout`, `harvest`, `process` or `synth`.
- `phase`: the current phase, or the end-state key for `empty` and `error`.
- `reason`: why there is no usable report; `null` for running missions and for a real `done`.
- `report_available`: `true` only when the status is `done` and a report exists with at least one useful claim.
- `useful_claims`: claims that count toward `done`.
- `fallback_claims`: claims from the regex fallback. They are raw material, not a product, and never make a mission `done`.

Example for an empty mission:

```json
{"status": "empty", "phase": "empty_claims", "report_available": false,
 "reason": "Sources were collected but no qualifying claims were produced."}
```

`GET /research/{job_id}/report` returns the report with HTTP 200 only for a `done`
mission. For `empty`, `error` and `cancelled` it returns HTTP 409. `detail` and `reason`
carry the reason, and the body also holds `status`, `phase`, `report_available: false`,
`useful_claims` and `fallback_claims`:

```json
{"detail": "Qualifying claims were found but no report was produced.",
 "status": "error", "phase": "synthesis_failed", "report_available": false}
```

For a mission that is still running the endpoint does not return a report either.

The MCP tool `mission_status` prints the same facts as text:

```
Mission: <topic>
Status: error
Phase: synthesis_failed
Reason: Qualifying claims were found but no report was produced.
Claims: 497 useful, 0 regex fallback
Report: not available
```

The `Reason:` line appears only when there is one. The MCP tool `get_report` refuses the same
way instead of returning an empty report:

```
No report for mission '<mission_id>'.
Status: empty
Phase: empty_claims
Reason: Sources were collected but no qualifying claims were produced.
```

Reasons, by `phase` key:

```
  empty_scout             No sources or qualifying claims were produced.
  empty_claims            Sources were collected but no qualifying claims were produced.
  process_timeout         The processing phase exceeded its time limit; no report was produced.
  process_error           The processing phase failed; no report was produced.
  synthesis_failed        Qualifying claims were found but no report was produced.
  pipeline_failed         Pipeline execution failed without a qualifying product.
  claims_fallback_failed  Claims recovery failed without a qualifying product.
  pipeline_exception      Pipeline execution raised an exception.
  timeout                 Pipeline execution timed out.
  cancelled               Mission was cancelled.
```

Missions recorded as `done` before the status rules (2026-11-13) that have no report, or no
claims beyond the regex fallback, report this reason: "Marked done without a report or without
claims beyond regex fallback (recorded before the 2026-11-13 status rules); it has no usable result."

### Python API

```python
from behive import research

result = await research("NVIDIA GPU market 2025", depth=3)
print(f"Found {len(result.claims)} claims")
print(f"Average quality: {result.avg_quality:.2f}")
print(result.report)
```

### MCP Integration (Claude, Hermes, etc.)

BeHive exposes an MCP server on port 8090:

```json
{
  "mcpServers": {
    "behive": {
      "url": "http://localhost:8090/mcp"
    }
  }
}
```

Available tools: `research_topic`, `mission_status`, `get_report`, `search_knowledge`, `list_missions`.

## Configuration

### Model Routing

Route different pipeline stages to different models:

```bash
# Use fast model for scouting, powerful for synthesis
behive config --stage scout --model groq/llama-3.3-70b-versatile
behive config --stage synth --model anthropic/claude-sonnet-4-20250514

# Or apply a preset
behive config --preset balanced
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BEHIVE_DB_URL` | PostgreSQL connection string | `postgresql://behive:behive@localhost:5432/behive` |
| `BEHIVE_MODEL` | Default LLM model | Auto-detect from API key |
| `BEHIVE_MAX_CONCURRENT` | Max simultaneous missions | `3` |
| `BEHIVE_PHASE_TIMEOUT` | Per-phase timeout (seconds) | `600` |
| `BEHIVE_SCOUT_TIMEOUT` | Scout phase timeout | `300` |
| `BEHIVE_FALSIFIER_TIMEOUT` | Falsifier timeout | `1200` |
| `BEHIVE_HOME` | Directory for local scripts | `~` |
| `BEHIVE_REPORTS_DIR` | PDF report output directory | `~/hive-reports` |

### Optional Services

| Service | Variable | Purpose |
|---------|----------|---------|
| Neo4j | `BEHIVE_NEO4J_URI` | Knowledge graph storage |
| Qdrant | `BEHIVE_QDRANT_URL` | Vector similarity search |
| Playwright | `pip install playwright && playwright install chromium` | Web search (recommended) |

## Docker Compose

For a full self-contained setup:

```bash
git clone https://github.com/qa10devteam/behive
cd behive
cp .env.example .env  # Edit with your API key
docker compose up -d
```

This starts PostgreSQL, BeHive server, and optional Neo4j/Qdrant.

## Troubleshooting

### "No LLM API key found"
Set at least one of: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, or AWS credentials.

### "Cannot connect to database"
1. Ensure PostgreSQL is running: `pg_isready`
2. Check connection string: `psql $BEHIVE_DB_URL`
3. Initialize schema: `behive db init`

### "Scout found 0 sources"
Install Playwright for web search: `pip install playwright && playwright install chromium`

### Mission stuck or timing out
- Check logs: `behive missions --limit 5`
- Increase timeout: `export BEHIVE_PHASE_TIMEOUT=900`
- For depth-5 missions, falsifier may need more time: `export BEHIVE_FALSIFIER_TIMEOUT=2400`

## Next Steps

- [API Reference](api-reference.md)
- [Configuration Guide](configuration.md)
- [Deployment Guide](deployment.md)
- [Architecture Overview](architecture.md)
