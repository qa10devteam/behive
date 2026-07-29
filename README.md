<p align="center">
  <img src="demo.gif" alt="BeHive — Deep Research Engine" width="720" />
</p>

<h1 align="center">🐝 BeHive</h1>

<p align="center">
  <strong>Open-source research engine that extracts structured knowledge from any topic.</strong><br />
  Feed it a question. Get back scored claims, entity graphs, and a synthesized report — not paragraphs of slop.
</p>

<p align="center">
  <a href="https://pypi.org/project/behive"><img src="https://img.shields.io/badge/pip_install-behive-FFB300?style=for-the-badge&logo=python&logoColor=white" /></a>
  <a href="https://www.npmjs.com/package/n8n-nodes-behive"><img src="https://img.shields.io/badge/n8n-community_node-FF6D5A?style=for-the-badge&logo=n8n&logoColor=white" /></a>
  <a href="#self-hosting"><img src="https://img.shields.io/badge/Docker-compose_up-2496ED?style=for-the-badge&logo=docker&logoColor=white" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge" /></a>
  <a href="#mcp-integration"><img src="https://img.shields.io/badge/MCP-Native-AB47BC?style=for-the-badge" /></a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#use-with-claude--chatgpt--gemini">Use with AI Assistants</a> •
  <a href="#drone-arsenal">Drone Arsenal</a> •
  <a href="#architecture">Architecture</a> •
  <a href="#api-reference">API</a> •
  <a href="#self-hosting">Self-Hosting</a>
</p>

---

## The Problem

You ask Claude to research a topic. It gives you a confident-sounding summary based on training data that's months old. No sources. No structure. No way to verify.

You ask Perplexity. Better — it cites sources. But the output is still unstructured text. You can't query it, cross-reference it, or build on it.

**BeHive is different.** It produces **machine-readable intelligence**: typed claims with confidence scores, and a synthesized report — not paragraphs of slop.

```
Your AI assistant → BeHive → Verified, structured, scored knowledge
                              ├── 290 claims (avg quality 0.797)
                              ├── Synthesized report with citations
                              └── Searchable knowledge base
```

---

## Quick Start

### Docker (recommended — includes PostgreSQL)

```bash
git clone https://github.com/qa10devteam/behive.git && cd behive
cp .env.example .env        # add your LLM API key
docker compose up -d        # API at http://localhost:8091, MCP at http://localhost:8090
```

### pip install

```bash
pip install behive

# Set your LLM API key (BeHive uses YOUR subscription — no cost from us)
export ANTHROPIC_API_KEY=your-key  # or OPENAI_API_KEY, or AWS creds for Bedrock

# PostgreSQL required (create DB first, then init schema via docker/init-db.sql)
export BEHIVE_DB_URL=postgresql://user:pass@localhost:5432/behive

# Start both API + MCP server
behive serve
```

> **⚠️ GPU/CUDA note:** Core `pip install behive` does NOT pull PyTorch or CUDA. GPU deps are only in the optional `behive[qdrant]` extra (for vector embeddings). If you install it on a CPU-only machine:
> `pip install torch --index-url https://download.pytorch.org/whl/cpu` first, then `pip install "behive[qdrant]"`.

**Optional extras:**
```bash
pip install "behive[all]"       # stealth + harvest + process + science + mcp + api
pip install "behive[stealth]"   # curl_cffi, primp, nodriver, patchright
pip install "behive[harvest]"   # trafilatura, newspaper4k, PyMuPDF, crawl4ai
pip install "behive[mcp,api]"   # MCP server + REST API
```

After starting, BeHive runs:
- **REST API** → `http://localhost:8091` 
- **MCP Server** → `http://localhost:8090/mcp`
- **Swagger docs** → `http://localhost:8091/docs`

---

## 🧠 Model Routing — Use Cheap Models to Collect, Smart Models to Analyze

BeHive doesn't force you into one model. You choose what runs each pipeline stage:

| Stage | Role | Recommended |
|-------|------|-------------|
| **scout** | Query generation, source discovery | Haiku / GPT-4o-mini / local |
| **harvest** | Relevance filtering, content triage | Haiku / GPT-4o-mini / local |
| **process** | Claim extraction, quality scoring | Haiku or Sonnet |
| **synth** | Report synthesis, deduplication | Sonnet / Opus / GPT-4o |

### Quick Setup

```bash
# Apply a preset
behive config --preset balanced   # Haiku collects, Sonnet synthesizes (~$1.50/mission)
behive config --preset budget     # Haiku everywhere (~$0.30/mission)
behive config --preset quality    # Sonnet everywhere (~$4.00/mission)
behive config --preset local      # Your own LLM server ($0.00/mission)

# Interactive
behive config --quick    # Pick one model for everything
behive config --full     # Choose model per stage

# Fine-grained
behive config --stage synth --model claude-opus
behive config --stage scout --model ollama/deepseek-r1

# Check current config
behive config --show
```

### Environment Variable Override (Docker/CI)

```bash
export BEHIVE_MODEL_SCOUT=ollama/llama3.1
export BEHIVE_MODEL_SYNTH=anthropic/claude-sonnet-4-20250514
behive serve
```

Priority: `BEHIVE_MODEL_{STAGE}` > `BEHIVE_MODEL` > config.yaml > defaults

### Available Model Presets

| Preset | Model String |
|--------|-------------|
| `claude-haiku` | anthropic/claude-haiku-4-5-20251001 |
| `claude-sonnet` | anthropic/claude-sonnet-4-20250514 |
| `claude-opus` | anthropic/claude-opus-4-20250514 |
| `gpt-4o-mini` | openai/gpt-4o-mini |
| `gpt-4o` | openai/gpt-4o |
| `gpt-4.1` | openai/gpt-4.1 |
| `gemini-flash` | google/gemini-2.5-flash |
| `gemini-pro` | google/gemini-2.5-pro |
| `bedrock-haiku` | bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0 |
| `bedrock-sonnet` | bedrock/us.anthropic.claude-sonnet-4-6-v1@us-east-1 |
| `local` | openai/local-model (any OpenAI-compatible server) |
| `ollama` | ollama/llama3.1 |

Or pass any [litellm-compatible](https://docs.litellm.ai/docs/providers) model string directly.

---

## 🔌 Setup with Claude Desktop (30 seconds)

> **You bring your Claude subscription. BeHive adds research superpowers. No extra cost from us.**

**Step 1:** Install and start BeHive:
```bash
pip install "behive[mcp,api]"
export ANTHROPIC_API_KEY=your-key
export BEHIVE_DB_URL=postgresql://user:pass@localhost:5432/behive
behive serve
```

**Step 2:** Open Claude Desktop → Settings → Developer → Edit Config → paste:
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

**Step 3:** Restart Claude Desktop. Done. Now ask:
> "Research the EU AI Act enforcement timeline and penalties"

Claude will call BeHive automatically, fetch sources, and return scored claims instead of guessing from training data.

<details>
<summary><strong>What happens under the hood</strong></summary>

```
You ask Claude a question
    ↓
Claude calls BeHive MCP tool "research_topic"
    ↓
BeHive scouts 70+ APIs, fetches 1000+ URLs via stealth drones
    ↓
Your LLM key extracts claims (Claude Haiku = ~$0.50 per mission)
    ↓
BeHive scores, deduplicates, stores in PostgreSQL
    ↓
Returns structured report to Claude
    ↓
Claude presents findings with confidence scores and source links
```

**Cost: ~$0.30–$2.00 per research mission** (your Anthropic/OpenAI tokens).
BeHive itself: **free forever** (MIT license).
</details>

---

## 🔌 Setup with Cursor / Windsurf / Any MCP Client

Any editor or tool supporting MCP works identically to Claude Desktop:

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

**Available MCP tools:**
| Tool | Description |
|------|-------------|
| `research_topic` | Start a deep research mission (returns job_id) |
| `mission_status` | Poll running mission progress |
| `get_report` | Get synthesized report for completed mission |
| `search_knowledge` | Search all previously extracted claims |

---

## Drone Arsenal

BeHive doesn't just search the web. It deploys **stealth drones** — multi-layered fetch agents that break through anti-bot defenses, paywalls, and rate limits.

### 8-Layer Evasion Stack

Every URL goes through an **escalation cascade**. If Layer 1 gets blocked, Layer 2 fires. All the way to Layer 8.

```
Layer 1 │ DIRECT          — aiohttp + full Chrome 131 headers
Layer 2 │ UA ROTATION     — 10 browser fingerprints (Chrome/Firefox/Safari/Edge)
Layer 3 │ curl_cffi       — TLS impersonation (JA3/JA4 fingerprint matching)
Layer 4 │ primp           — Rust-native TLS, newer fingerprints than curl_cffi
Layer 5 │ nodriver        — Headless Chrome via CDP, passes Cloudflare Bot Management
Layer 6 │ patchright      — Stealth Playwright (no Runtime.enable/Console.enable leak)
Layer 7 │ Jina relay      — r.jina.ai proxy (paywall + captcha bypass)
Layer 8 │ Archives        — Wayback Machine + archive.org fallback
```

### 70+ API Sources

Scout bees don't just Google. They query **specialized APIs** across 37 categories:

| Category | APIs | Examples |
|----------|------|----------|
| Academic | 5 | arXiv, Semantic Scholar, CrossRef, OpenAlex, CORE |
| Financial | 6 | SEC EDGAR, Yahoo Finance, FRED, ECB, World Bank |
| Government | 5 | TED (EU procurement), SAM.gov, UK FTS, BZP (Poland), GUS |
| Security | 6 | CVE/NVD, Shodan, VirusTotal, AbuseIPDB |
| Development | 8 | GitHub, npm, PyPI, crates.io, Docker Hub, Homebrew |
| ML/AI | 5 | HuggingFace, Papers With Code, Replicate, Ollama |
| News | 4 | NewsAPI, GNews, TheNewsAPI, Mediastack |
| ... | 25+ | Trade, geopolitics, environment, demographics, ... |

**Total: 70 APIs, 125 endpoints** — each checked per-mission based on topic relevance.

---

## Architecture

```
                         ┌──────────────────────────────────┐
                         │         BeHive Pipeline           │
                         └──────────────────────────────────┘
                                        │
        ┌───────────┬───────────┬───────┴───────┬───────────┐
        ▼           ▼           ▼               ▼           ▼
   ┌─────────┐ ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌─────────┐
   │  SCOUT  │ │ HARVEST │ │ PROCESS  │ │  ENRICH  │ │  SYNTH  │
   │         │ │         │ │          │ │          │ │         │
   │ Queen   │ │ Parallel│ │ Haiku    │ │ Sonnet   │ │ Claude  │
   │ plans   │ │ HTTP    │ │ extract  │ │ enrich   │ │ report  │
   │ 5 axes  │ │ 1000+   │ │ + score  │ │ thin     │ │ + cite  │
   │ × N     │ │ URLs    │ │          │ │ claims   │ │         │
   └─────────┘ └─────────┘ └──────────┘ └──────────┘ └─────────┘
       │              │            │            │            │
       │              │            ▼            │            │
       │              │    ┌──────────────┐     │            │
       │              │    │ Quality Gate │     │            │
       │              │    │  conf ≥ 0.55 │     │            │
       │              │    │  dedup 0.60  │     │            │
       │              │    └──────────────┘     │            │
       │              │            │            │            │
       └──────────────┴────────────┴────────────┴────────────┘
                                        │
                              ┌─────────┴─────────┐
                              │   PostgreSQL       │
                              │   Claims + Missions│
                              └───────────────────┘
```

**What makes it different from GPT-Researcher:**

1. **Dual-model extraction** — Fast model (Haiku) for bulk extraction, powerful model (Sonnet) for enriching thin claims. Not just "summarize this page."
2. **Quality scoring** — Every claim gets a 0.0-1.0 score. Below threshold = rejected. No filler.
3. **70+ API sources** — Not just web search. SEC filings, arXiv, patent databases, government APIs.
4. **Deduplication** — Jaccard 0.60 threshold prevents the same fact from different sources inflating counts.

---

## API Reference

BeHive exposes a REST API (port 8091) and MCP server (port 8090).

### Start Research

```bash
curl -X POST http://localhost:8091/research \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_BEHIVE_API_KEY" \
  -d '{
    "query": "SpaceX Starship launch cadence 2026",
    "depth": 3,
    "scale": 30
  }'
# → {"job_id": "hive_1785227949_815112", "status": "started"}
```

### Check Status

```bash
curl http://localhost:8091/research/hive_1785227949_815112/status
# → {"phase": "process", "progress": 0.65, "claims_so_far": 142}
```

### Get Results

```bash
curl http://localhost:8091/research/hive_1785227949_815112
# → {"status": "done", "topic": "...", "report": "## ...", "claims_count": 290, ...}
```

### Search Knowledge

```bash
curl "http://localhost:8091/claims/search?q=NVIDIA+revenue&limit=20"
```

### All Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/research` | Start new mission |
| `GET` | `/research/{id}/status` | Check progress |
| `GET` | `/research/{id}` | Get full results + report |
| `GET` | `/claims/search` | Search claims (param: `q`, `limit`) |

Full Swagger docs: `http://localhost:8091/docs`

---

## Self-Hosting

### Docker (recommended)

```bash
git clone https://github.com/qa10devteam/behive.git
cd behive
cp .env.example .env     # add your LLM API key
docker compose up -d     # API ready at localhost:8091
```

With Neo4j + Qdrant (knowledge graph + vector search):
```bash
docker compose --profile full up -d
```

### Minimum Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| RAM | 4 GB | 16 GB |
| CPU | 2 cores | 8+ cores |
| Storage | 10 GB | 50 GB |
| GPU | Not required | Optional (local LLM) |
| PostgreSQL | 14+ | 16 |
| LLM | Any OpenAI-compatible | Claude Haiku + Sonnet |

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | For Anthropic models (recommended) |
| `OPENAI_API_KEY` | — | For OpenAI models |
| `AWS_ACCESS_KEY_ID` | — | For Bedrock |
| `BEHIVE_DB_URL` | `postgresql://localhost/hive` | PostgreSQL connection |
| `BEHIVE_LOCAL_LLM_URL` | — | Local LLM endpoint (Ollama/vLLM) |
| `BEHIVE_API_KEY` | — | Auth for REST API (optional localhost, required public) |
| `BEHIVE_SCALE` | `30` | Default research scale (30-300) |
| `BEHIVE_QUALITY_GATE` | `0.55` | Minimum claim quality to store |

---

## Comparison

| | BeHive | GPT-Researcher | Tavily | Perplexity | STORM |
|---|:---:|:---:|:---:|:---:|:---:|
| Output format | Structured JSON | Markdown text | JSON snippets | Text | Wiki article |
| Per-claim scoring | ✅ 0.0-1.0 | ❌ | ❌ | ❌ | ❌ |
| Cross-session memory | ✅ Cumulative | ❌ | ❌ | ❌ | ❌ |
| MCP native | ✅ | ❌ | ❌ | ❌ | ❌ |
| API sources (70+) | ✅ | ❌ Web only | ⚠️ Search | ⚠️ Search | ❌ Web only |
| Self-hosted | ✅ Full | ⚠️ Needs API keys | ❌ Cloud | ❌ Cloud | ✅ |
| Quality deduplication | ✅ Jaccard 0.60 | ❌ | ❌ | ❌ | ❌ |
| Pricing | **Free (MIT)** | Free (MIT) | $0.01/search | $20/mo+ | Free (MIT) |

---

## Roadmap

- [x] V4 pipeline (Haiku + Sonnet extraction)
- [x] Quality scoring (avg 0.79+ achieved)
- [x] REST API (5 endpoints)
- [x] MCP Server (Streamable HTTP, 4 tools)
- [x] 70+ API sources
- [x] `pip install behive` ([PyPI](https://pypi.org/project/behive/))
- [x] Docker Compose one-liner
- [x] n8n community node ([npm](https://www.npmjs.com/package/n8n-nodes-behive))
- [x] Model routing CLI (`behive config`)
- [ ] SSE streaming (real-time progress events)
- [ ] Knowledge graph (Neo4j entity persistence)
- [ ] Web UI dashboard
- [ ] Multi-tenant API keys
- [ ] Webhook callbacks
- [ ] Scheduled recurring research
- [ ] PDF export

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code style, and PR guidelines.

```bash
git clone https://github.com/qa10devteam/behive.git
cd behive
pip install -e ".[all]"
pytest
```

---

## License

MIT — use it, fork it, ship it, sell it.

---

<p align="center">
  <sub>Built by <a href="https://qa10.io">QA10</a> · Structured knowledge, not text soup.</sub>
</p>
