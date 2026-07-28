<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python" />
  <img src="https://img.shields.io/badge/License-MIT-green" />
  <img src="https://img.shields.io/badge/MCP-Native-purple" />
  <img src="https://img.shields.io/badge/Claims-25K+-orange" />
</p>

# 🐝 BeHive — Research Engine That Builds Knowledge Graphs

<p align="center">
  <img src="demo.gif" alt="BeHive demo — 20s terminal session" width="700" />
</p>

BeHive is an open-source deep research engine that produces **structured, verifiable intelligence** from any topic. Unlike traditional research tools that output text summaries, BeHive extracts **typed claims with quality scores**, builds **entity relationship graphs**, and delivers **cross-mission knowledge fusion**.

```python
from behive import research

result = await research("NVIDIA GPU market 2025", depth=3)

# Structured claims with quality scores
for claim in result.excellent_claims:
    print(f"[{claim.quality_score:.2f}] {claim.text}")
# [0.95] NVIDIA reported $35.1B Q3 FY2025 revenue, up 94% YoY
# [0.91] Data Center segment: $30.8B revenue, +112% YoY
# [0.87] Gross margin 74.6%, raised FY2025 guidance to $128B

# Entity intelligence
print(result.entities[:5])
# NVIDIA (org), Blackwell (product), Jensen Huang (person), $35.1B (amount)
```

## Why BeHive?

| Feature | GPT-Researcher | Perplexity | Tavily | **BeHive** |
|---------|---------------|------------|--------|------------|
| Structured claims | ❌ text only | ❌ text only | ❌ text only | ✅ typed + scored |
| Quality scoring | ❌ | ❌ | ❌ | ✅ 0.0-1.0 per claim |
| Knowledge graph | ❌ | ❌ | ❌ | ✅ Neo4j entities |
| Cross-mission fusion | ❌ | ❌ | ❌ | ✅ cumulative |
| MCP native | ❌ | ❌ | ❌ | ✅ first-class |
| Self-hosted | ⚠️ API key | ❌ cloud only | ⚠️ API key | ✅ fully local |
| API integration | ❌ | ❌ | ⚠️ search only | ✅ 70+ API sources |

## Quick Start

```bash
pip install behive

# Set your LLM provider (Bedrock, OpenAI, or local)
export AWS_PROFILE=default  # for Bedrock
# or
export OPENAI_API_KEY=sk-...

# Run research
behive research "Tesla Optimus robot timeline 2025" --depth 3
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    BeHive Pipeline                    │
├─────────┬──────────┬───────────┬──────────┬─────────┤
│  SCOUT  │ HARVEST  │  PROCESS  │  SYNTH   │  GRAPH  │
│         │          │           │          │         │
│ Queen   │ Parallel │ V4 Haiku  │ Bedrock  │ Neo4j   │
│ plans   │ fetch    │ extraction│ Claude   │ entity  │
│ 5 axes  │ 1100     │ + Sonnet  │ synthesis│ network │
│ × N     │ sources  │ enrichment│ report   │ + KG    │
└─────────┴──────────┴───────────┴──────────┴─────────┘
```

**Pipeline stages:**
1. **Scout** — Queen decomposes topic into 5 research axes, generates search queries
2. **Harvest** — Parallel HTTP fetch of 1000+ sources (HEAD sweep → full content)
3. **Process** — Dual-model extraction:
   - BeeHive (SGLang/Qwen) for fast entity/relation extraction
   - V4 Haiku for precision claim extraction with enrichment
   - Sonnet for upgrading thin claims below 0.78 quality
4. **Synth** — Bedrock Claude synthesizes structured report
5. **Graph** — Neo4j ingestion for cross-mission intelligence

## Quality System

BeHive scores every claim on 5 dimensions:

- **Specificity** (25%) — numbers, dates, proper names, percentages
- **Information Density** (25%) — data points per word
- **Uniqueness** (15%) — not trivially available elsewhere  
- **Verifiability** (15%) — can be fact-checked against source
- **Structure** (20%) — well-formed, atomic, self-contained

**Quality gate**: Claims below 0.55 never enter the database.  
**Enrichment bonus**: Claims with 3+ data dimensions get +0.05-0.08.  
**Current avg**: 0.79 on new missions (honest, no sigmoid tricks).

## MCP Integration

BeHive is MCP-native — connect it to any AI agent:

```json
{
  "mcpServers": {
    "behive": {
      "url": "http://localhost:8090/mcp",
      "transport": "streamable-http"
    }
  }
}
```

Tools exposed:
- `research_topic` — Start a research mission
- `mission_status` — Check progress
- `get_report` — Get synthesized report
- `search_knowledge` — Query extracted claims

## API

```bash
# Start research
curl -X POST http://localhost:8091/research \
  -H "Content-Type: application/json" \
  -d '{"query": "EU AI Act enforcement", "depth": 3}'

# Stream progress (SSE)
curl http://localhost:8091/research/{mission_id}/events

# Get results
curl http://localhost:8091/research/{mission_id}/report

# Query knowledge graph
curl http://localhost:8091/intelligence/entity/NVIDIA
curl http://localhost:8091/intelligence/network/OpenAI?depth=2
```

## Configuration

```bash
# PostgreSQL (required)
export BEHIVE_DB_URL="postgresql://behive_user:password@localhost:5432/hive"

# LLM (pick one)
export AWS_PROFILE=default          # Bedrock (recommended)
export OPENAI_API_KEY=sk-...        # OpenAI
export BEHIVE_LLM_URL=http://localhost:8002  # Local (SGLang/vLLM)

# Optional
export BEHIVE_NEO4J_URI=bolt://localhost:7687
export BEHIVE_QDRANT_URL=http://localhost:6333
```

## Benchmarks

| Topic | Claims | Avg Quality | Duration | Sources |
|-------|--------|-------------|----------|---------|
| NVIDIA GPU market 2025 | 290 | 0.797 | 8min | 234 |
| OpenAI GPT-5 capabilities | 574 | 0.789 | 12min | 174 |
| EU AI Act enforcement | 267 | 0.759 | 6min | 130 |
| Perplexity AI | 267 | 0.759 | 7min | 150 |

*Scale 30 (standard depth). Run on EC2 g5.12xlarge with Bedrock Claude.*

## License

MIT — use it, fork it, ship it.

---

Built by [QA10](https://qa10.io) · [API Docs](https://api.yu-na.io/docs) · [MCP Server](http://localhost:8090/mcp)
