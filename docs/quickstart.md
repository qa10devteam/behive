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

# Check status
curl http://localhost:8091/research/{job_id}/status

# Get results
curl http://localhost:8091/research/{job_id}/report
```

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
