# Contributing to BeHive

Thank you for your interest in contributing to BeHive! This document provides guidelines for development.

## Development Setup

```bash
# Clone
git clone https://github.com/qa10devteam/behive
cd behive

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install in development mode with all extras
pip install -e ".[all,dev]"

# Start PostgreSQL (Docker or local)
docker run -d --name behive-pg -p 5432:5432 \
  -e POSTGRES_USER=behive -e POSTGRES_PASSWORD=behive -e POSTGRES_DB=behive \
  postgres:16

# Initialize database
export BEHIVE_DB_URL=postgresql://behive:behive@localhost:5432/behive
behive db init

# Verify
behive doctor
```

## Running Tests

```bash
# All unit tests (fast, no network)
pytest tests/ -q --ignore=tests/test_e2e_pipeline.py

# Specific test file
pytest tests/test_llm.py -v

# With coverage
pytest tests/ --cov=behive --cov-report=html
```

## Code Style

- We use [ruff](https://github.com/astral-sh/ruff) for linting
- Run: `ruff check src/`
- Max line length: not enforced (use judgment)
- Type hints on public APIs, optional on internals
- Docstrings on all public functions

## Making Changes

1. **Fork & branch**: `git checkout -b feat/my-feature`
2. **Write tests first** if touching core pipeline logic
3. **Run tests**: `pytest tests/ -q`
4. **Lint**: `ruff check src/`
5. **No secrets**: `grep -r 'password\|secret\|sk-ant-' src/` must return only env-var references
6. **Commit**: Professional messages (this is a public repo with 100+ stars)
7. **PR**: Describe what and why, link related issues

## Architecture

```
src/behive/
├── __init__.py          # Public API (research, BeHiveClient)
├── cli.py              # CLI commands (serve, research, doctor, etc.)
├── server.py           # FastAPI REST API
├── mcp_server.py       # MCP protocol server
├── client.py           # Programmatic client
├── models.py           # Pydantic models (ResearchResult, Claim, Entity)
├── config.py           # Model routing configuration
├── knowledge_graph.py  # Neo4j integration
├── engine/             # Core pipeline modules
│   ├── orchestrator.py # Main pipeline orchestration
│   ├── scout.py        # Source discovery
│   ├── harvest.py      # Content fetching
│   ├── process.py      # Claim extraction
│   ├── synth.py        # Report synthesis
│   ├── falsifier.py    # Cross-validation
│   ├── llm.py          # BYOK LLM routing (litellm)
│   ├── search_backends.py  # Multi-backend search
│   └── ...
└── compat/             # Legacy import shims
```

## BYOK Principle

BeHive is Bring Your Own Key. **NEVER** hardcode:
- Model names (use `BEHIVE_MODEL` env var → litellm auto-detect)
- Provider-specific code (use `llm.py` abstraction)
- Infrastructure paths (use env vars or `os.path.expanduser`)

Test: "Does this work for someone who just ran `pip install behive` and has an OpenAI key?"

## PR Checklist

- [ ] Tests pass: `pytest tests/ -q`
- [ ] No secrets: `grep -r 'password\|secret\|sk-' src/` clean
- [ ] No hardcoded paths: `grep -r '/home/' src/` clean
- [ ] Lint passes: `ruff check src/`
- [ ] Version bump if releasing (pyproject.toml only — rest auto-detects)
