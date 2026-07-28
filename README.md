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
  <a href="https://api.yu-na.io/docs"><img src="https://img.shields.io/badge/API-Live-4FC3F7?style=for-the-badge&logo=fastapi&logoColor=white" /></a>
  <a href="#mcp-integration"><img src="https://img.shields.io/badge/MCP-Native-AB47BC?style=for-the-badge" /></a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#use-with-claude--chatgpt--gemini">Use with AI Assistants</a> •
  <a href="#drone-arsenal">Drone Arsenal</a> •
  <a href="#benchmarks">Benchmarks</a> •
  <a href="#architecture">Architecture</a> •
  <a href="#api-reference">API</a> •
  <a href="#self-hosting">Self-Hosting</a> •
  <a href="#integrations">Integrations</a>
</p>

---

## The Problem

You ask Claude to research a topic. It gives you a confident-sounding summary based on training data that's months old. No sources. No structure. No way to verify.

You ask Perplexity. Better — it cites sources. But the output is still unstructured text. You can't query it, cross-reference it, or build on it.

**BeHive is different.** It produces **machine-readable intelligence**: typed claims with confidence scores, entity relationship graphs, and structured JSON you can pipe into any downstream system.

```
Your AI assistant → BeHive → Verified, structured, scored knowledge
                              ├── 363 claims (avg quality 0.824)
                              ├── 42 entities with relationships
                              └── Synthesized report with citations
```

---

## Quick Start

```bash
pip install behive

# Configure your LLM (pick one)
export BEHIVE_LLM=bedrock        # AWS Bedrock Claude (recommended)
export BEHIVE_LLM=openai         # OpenAI GPT-4o
export BEHIVE_LLM=local          # Self-hosted via SGLang/vLLM

# Run research from CLI
behive research "NVIDIA Blackwell GPU production 2026" --scale 30

# Or from Python
python -c "
from behive import research
import asyncio

result = asyncio.run(research('NVIDIA Blackwell GPU production 2026'))
print(f'{result.claims_count} claims, avg quality {result.avg_quality:.3f}')
for claim in result.top_claims(5):
    print(f'  [{claim.score:.2f}] {claim.text}')
"
```

---

## Use with Claude / ChatGPT / Gemini

BeHive turns any AI assistant into a **verified research machine**. Three integration paths:

### 🟣 Claude (via MCP — zero-code)

Add to your Claude Desktop `config.json` or Cursor settings:

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

Now Claude can call BeHive natively:

> **You:** "Research the EU Carbon Border Adjustment Mechanism — what are the compliance deadlines and industry impacts?"
>
> **Claude** *(calls `research_topic`)* **:** "I've launched a deep research mission. BeHive found 312 claims across 156 sources. Here are the key findings, scored by confidence:
> - [0.94] CBAM transitional phase runs Jan 2024–Dec 2025; full enforcement begins Jan 2026
> - [0.91] Importers must purchase CBAM certificates matching embedded CO₂ at EU ETS price
> - [0.88] Steel, cement, aluminium, fertilizers, electricity, and hydrogen are covered sectors
> ..."

Claude's responses shift from "based on my training data" to **"verified against 156 live sources with per-claim confidence scores."**

### 🟢 ChatGPT (via Custom GPT / Function Calling)

Create a Custom GPT with this action:

```yaml
openapi: 3.0.0
info:
  title: BeHive Research
  version: 1.0.0
servers:
  - url: https://your-server.com/api/v1
paths:
  /research:
    post:
      operationId: startResearch
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                query:
                  type: string
                depth:
                  type: integer
                  default: 3
      responses:
        '200':
          description: Mission started
  /research/{job_id}/report:
    get:
      operationId: getReport
      parameters:
        - name: job_id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: Research report
```

Or call from the OpenAI API with function calling:

```python
import openai
import requests

# Start BeHive research
mission = requests.post("http://localhost:8091/research", json={
    "query": "Quantum computing error correction breakthroughs 2026",
    "depth": 3
}).json()

# Wait for completion, then feed to GPT-4o
report = requests.get(f"http://localhost:8091/research/{mission['job_id']}/report").json()

response = openai.chat.completions.create(
    model="gpt-4o",
    messages=[
        {"role": "system", "content": "You are an analyst. Use the research data below to answer questions. Cite claim IDs."},
        {"role": "user", "content": f"Research data:\n{report['synthesis']}\n\nQuestion: What's the most promising approach to fault-tolerant quantum computing?"}
    ]
)
```

### 🔵 Gemini (via API or Vertex AI)

```python
import google.generativeai as genai
import requests

# BeHive produces the research
claims = requests.get("http://localhost:8091/search", params={
    "query": "autonomous vehicles regulations 2026",
    "limit": 50
}).json()

# Gemini synthesizes with verified data
model = genai.GenerativeModel("gemini-2.0-flash")
response = model.generate_content(
    f"Based on these verified research claims (each with a confidence score), "
    f"write a briefing on autonomous vehicle regulation trends:\n\n"
    f"{claims['results']}"
)
```

### Why this matters

| Without BeHive | With BeHive |
|---|---|
| "Based on my training data..." | "Based on 234 live sources, scored 0.79 avg..." |
| Hallucination risk | Every claim traced to source URL |
| Stale knowledge (months old) | Real-time web research |
| Unstructured text blob | Typed claims, entities, relationships |
| One-shot, forgotten | Cumulative knowledge graph across sessions |

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

### What they bypass

| Defense | How |
|---------|-----|
| Cloudflare | Detected → escalate to nodriver/patchright (JS challenge solved) |
| DataDome | TLS fingerprint rotation (primp/curl_cffi) |
| Akamai Bot Manager | CDP-based headless + real browser UA pool |
| Rate limits | Automatic backoff + UA rotation + parallel diversification |
| Paywalls | Jina relay proxy + archive.org cache |
| Turnstile CAPTCHA | patchright stealth Playwright |
| 403/429 blocks | Smart retry with escalation, never hammer the same layer |

### Parallel fetch architecture

```
                    ┌─── HEAD sweep (974+ URLs, async semaphore) ───┐
                    │                                                │
                    ▼                                                ▼
          ┌─────────────────┐                            ┌────────────────┐
          │  Resource Router │                            │  Domain Recon  │
          │  (8 resource     │                            │  (tier scoring │
          │   types detected)│                            │   reputation)  │
          └────────┬────────┘                            └───────┬────────┘
                   │                                              │
        ┌──────────┼──────────┬──────────┐                       │
        ▼          ▼          ▼          ▼                       ▼
   api_bee    pdf_drone   std_drone  heavy_drone         domain_score
   (70 APIs)  (VLM parse) (Layer 1-8) (patchright)       (0.0 - 1.0)
```

**Routing decisions per resource type:**
- `api_endpoint` → Direct API bee (structured JSON, no parsing needed)
- `pdf` → PDF drone (Vision LLM extraction)
- `static_html` → Standard drone (Layer 1-4 usually sufficient)
- `spa` → Heavy drone (Layer 5-6, needs JS execution)
- `paywall` → Jina relay or archive fallback
- `rss_feed` → RSS bee (structured, fast)
- `database_portal` → Dedicated connector (custom scraping logic)

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
| Crypto | 2 | CoinGecko, CoinMarketCap |
| Patents | 1 | Google Patents (via SerpAPI) |
| Medical | 1 | PubMed/NCBI |
| ... | 25+ | Trade, geopolitics, environment, demographics, ... |

**Total: 70 APIs, 125 endpoints** — each checked per-mission based on topic relevance.

---

## Benchmarks

Real results. No cherry-picking. Scale 30 (standard depth).

**Hardware:** EC2 g6.24xlarge — 4× NVIDIA L4 (92 GB VRAM), 96 vCPU, 384 GB RAM  
**Models:** Bedrock Claude Haiku (bulk extraction) + Sonnet (enrichment), SGLang/Qwen on local GPUs

| Topic | Claims | Avg Quality | Duration | Sources |
|:------|-------:|:-----------:|---------:|--------:|
| NVIDIA GPU market 2026 | 290 | **0.797** | 8 min | 234 |
| OpenAI GPT-5 capabilities | 574 | **0.789** | 12 min | 174 |
| EU AI Act enforcement | 267 | **0.759** | 6 min | 130 |
| Perplexity AI business model | 267 | **0.759** | 7 min | 150 |
| Meta Llama 4 architecture | 568 | **0.821** | 11 min | 198 |

**Quality score meaning:**
- `0.90+` — Exceptional: specific numbers, dates, sources, fully verifiable
- `0.82+` — Excellent: multi-dimensional, publication-ready
- `0.75+` — Good: useful intelligence with some specifics
- `0.65+` — Acceptable: general facts, entered into DB
- `<0.55` — Rejected: too vague, not stored

> **Honest scoring, no tricks.** No sigmoid rescaling, no artificial inflation. The score is a weighted average of specificity, information density, uniqueness, verifiability, and structure.

---

## Architecture

```
                         ┌──────────────────────────────────┐
                         │         BeHive Pipeline           │
                         └──────────────────────────────────┘
                                        │
        ┌───────────┬───────────┬───────┴───────┬───────────┬───────────┐
        ▼           ▼           ▼               ▼           ▼           ▼
   ┌─────────┐ ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌─────────┐ ┌────────┐
   │  SCOUT  │ │ HARVEST │ │ PROCESS  │ │   V4     │ │  SYNTH  │ │ GRAPH  │
   │         │ │         │ │          │ │          │ │         │ │        │
   │ Queen   │ │ Parallel│ │ BeeHive  │ │ Haiku    │ │ Claude  │ │ Neo4j  │
   │ plans   │ │ HTTP    │ │ fast     │ │ extract  │ │ report  │ │ entity │
   │ 5 axes  │ │ 1000+   │ │ extract  │ │ + Sonnet │ │ + cite  │ │ fuse   │
   │ × N     │ │ URLs    │ │ + score  │ │ enrich   │ │         │ │        │
   └─────────┘ └─────────┘ └──────────┘ └──────────┘ └─────────┘ └────────┘
       │              │            │            │            │          │
       │              │            ▼            │            │          │
       │              │    ┌──────────────┐     │            │          │
       │              │    │ Quality Gate │     │            │          │
       │              │    │  conf ≥ 0.55 │     │            │          │
       │              │    │  dedup 0.60  │     │            │          │
       │              │    └──────────────┘     │            │          │
       │              │            │            │            │          │
       └──────────────┴────────────┴────────────┴────────────┴──────────┘
                                        │
                              ┌─────────┴─────────┐
                              │   PostgreSQL       │
                              │   Claims + KG      │
                              │   25K+ records     │
                              └───────────────────┘
```

**What makes it different from GPT-Researcher:**

1. **Dual-model extraction** — Fast model (Haiku) for bulk extraction, powerful model (Sonnet) for enriching thin claims. Not just "summarize this page."
2. **Quality scoring** — Every claim gets a 0.0-1.0 score. Below threshold = rejected. No filler.
3. **Knowledge graph** — Entities and relationships persist across missions. Research compounds.
4. **70+ API sources** — Not just web search. SEC filings, arXiv, patent databases, government APIs.
5. **Deduplication** — Jaccard 0.60 threshold prevents the same fact from different sources inflating counts.

---

## API Reference

BeHive exposes a REST API (port 8091) and MCP server (port 8090).

### Start Research

```bash
curl -X POST http://localhost:8091/research \
  -H "Content-Type: application/json" \
  -d '{
    "query": "SpaceX Starship launch cadence 2026",
    "depth": 3,
    "scale": 30
  }'
# → {"job_id": "hive_1785227949_815112", "status": "started"}
```

### Stream Progress (SSE)

```bash
curl -N http://localhost:8091/research/hive_1785227949_815112/events
```

```
event: start
data: {"topic": "SpaceX Starship...", "status": "scout"}

event: phase
data: {"phase": "process", "event": "started"}

event: claims
data: {"count": 142, "avg_quality": 0.791, "above_082": 23, "new_since_last": 18}

event: done
data: {"total_claims": 363, "avg_quality": 0.824, "sources": 64}
```

### Get Report

```bash
curl http://localhost:8091/research/hive_1785227949_815112/report
# → {"synthesis": "## SpaceX Starship...", "claims_count": 363, ...}
```

### Search Knowledge

```bash
# Full-text search across all missions
curl "http://localhost:8091/search?query=NVIDIA+revenue&limit=20"

# Entity intelligence
curl http://localhost:8091/intelligence/entity/NVIDIA

# Network graph (2-hop neighborhood)  
curl "http://localhost:8091/intelligence/network/OpenAI?depth=2"
```

### All Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/research` | Start new mission |
| `GET` | `/research/{id}/status` | Check progress |
| `GET` | `/research/{id}/events` | SSE stream |
| `GET` | `/research/{id}/report` | Get synthesis |
| `GET` | `/search` | Query claims |
| `GET` | `/intelligence/entity/{name}` | Entity details |
| `GET` | `/intelligence/network/{name}` | Relationship graph |
| `GET` | `/intelligence/stats` | System statistics |

Full Swagger docs: `http://localhost:8091/docs`

---

## MCP Integration

BeHive implements the [Model Context Protocol](https://modelcontextprotocol.io) — the emerging standard for AI tool connectivity.

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

**Compatible with:**
- Claude Desktop / Claude Code
- Cursor IDE
- Windsurf
- n8n (via MCP node)
- Any MCP-compatible client

**Tools exposed:**

| Tool | Description |
|------|-------------|
| `research_topic` | Start deep research on any topic |
| `mission_status` | Poll progress (phase, quality, claims) |
| `get_report` | Get the synthesized markdown report |
| `search_knowledge` | Query claims across all missions |
| `list_missions` | See completed research history |

---

## Self-Hosting

### Docker (recommended)

```bash
git clone https://github.com/qa10devteam/behive.git
cd behive
cp .env.example .env     # add your LLM API key
docker compose up -d     # API ready at localhost:8091
```

Full stack with knowledge graph + vector search:
```bash
docker compose --profile full up -d
```

### Manual Setup

```bash
pip install behive[all]

# PostgreSQL
createdb hive
behive db init

# Configure
export BEHIVE_DB_URL="postgresql://user:pass@localhost:5432/hive"
export BEHIVE_LLM=bedrock  # or openai, local

# Start services
behive api start      # REST API on :8091
behive mcp start      # MCP server on :8090
```

### Minimum Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| RAM | 4 GB | 16 GB |
| CPU | 2 cores | 8+ cores |
| Storage | 10 GB | 50 GB |
| GPU | Not required | 4× L4 (local LLM) |
| PostgreSQL | 14+ | 16 (pgvector) |
| LLM | Any OpenAI-compatible | Bedrock Claude (Haiku + Sonnet) |

---

## How It Works (for humans)

1. **You give it a topic.** "NVIDIA GPU market 2026"

2. **Scout bees plan the research.** The Queen decomposes it into 5 axes (market share, financials, products, competition, supply chain). Generates 12-14 search queries per axis. Checks 70+ APIs.

3. **Harvest bees collect sources.** Parallel HTTP fetches ~1000 URLs. HEAD sweep first (fast), then full content extraction on promising ones. Typically lands 60-90 usable documents.

4. **Worker bees extract claims.** This is where BeHive shines:
   - Every document gets parsed into atomic, verifiable claims
   - Each claim scored on 5 dimensions (specificity, density, uniqueness, verifiability, structure)
   - Claims below 0.55 quality → rejected
   - Thin claims (missing dates/numbers) → enriched by Sonnet
   - Duplicate claims (Jaccard >0.60) → merged

5. **The Queen synthesizes.** Claude weaves the verified claims into a structured report with inline citations. No hallucination — every statement maps to a scored claim.

6. **Knowledge graph grows.** Entities (companies, people, products, amounts) and their relationships are stored in Neo4j. Next research mission on a related topic starts with existing context.

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `BEHIVE_DB_URL` | `postgresql://localhost/hive` | PostgreSQL connection |
| `BEHIVE_LLM` | `bedrock` | LLM provider: `bedrock`, `openai`, `local` |
| `BEHIVE_LLM_URL` | — | Local LLM endpoint (for `local` mode) |
| `BEHIVE_NEO4J_URI` | `bolt://localhost:7687` | Neo4j (optional) |
| `BEHIVE_QDRANT_URL` | `http://localhost:6333` | Qdrant (optional) |
| `BEHIVE_SCALE` | `30` | Default research scale (30-300) |
| `BEHIVE_QUALITY_GATE` | `0.55` | Minimum claim quality to store |
| `AWS_PROFILE` | `default` | For Bedrock authentication |
| `OPENAI_API_KEY` | — | For OpenAI mode |

---

## Comparison

| | BeHive | GPT-Researcher | Tavily | Perplexity | STORM |
|---|:---:|:---:|:---:|:---:|:---:|
| Output format | Structured JSON | Markdown text | JSON snippets | Text | Wiki article |
| Per-claim scoring | ✅ 0.0-1.0 | ❌ | ❌ | ❌ | ❌ |
| Knowledge graph | ✅ Neo4j | ❌ | ❌ | ❌ | ❌ |
| Cross-session memory | ✅ Cumulative | ❌ | ❌ | ❌ | ❌ |
| MCP native | ✅ | ❌ | ❌ | ❌ | ❌ |
| API sources (70+) | ✅ | ❌ Web only | ⚠️ Search | ⚠️ Search | ❌ Web only |
| Self-hosted | ✅ Full | ⚠️ Needs API keys | ❌ Cloud | ❌ Cloud | ✅ |
| Quality deduplication | ✅ Jaccard 0.60 | ❌ | ❌ | ❌ | ❌ |
| SSE streaming | ✅ Real-time | ❌ | ❌ | ❌ | ❌ |
| Pricing | **Free (MIT)** | Free (MIT) | $0.01/search | $20/mo+ | Free (MIT) |

---

## Roadmap

- [x] V4 pipeline (Haiku + Sonnet extraction)
- [x] Quality scoring (avg 0.82+ achieved)
- [x] REST API (27 endpoints)
- [x] MCP Server (Streamable HTTP)
- [x] SSE streaming (real-time progress)
- [x] Knowledge graph (Neo4j)
- [x] 70+ API sources
- [x] `pip install behive` ([PyPI](https://pypi.org/project/behive/))
- [x] Docker Compose one-liner
- [x] n8n community node ([npm](https://www.npmjs.com/package/n8n-nodes-behive))
- [x] Agent skills (Hermes, OpenClaw, Claude Desktop)
- [ ] Web UI dashboard
- [ ] Multi-tenant API keys
- [ ] Webhook callbacks
- [ ] Scheduled recurring research
- [ ] PDF export with charts

---

## Integrations

BeHive works with every major AI agent platform:

| Platform | Method | Install |
|----------|--------|---------|
| **Claude Desktop** | MCP (zero-code) | Add URL to `claude_desktop_config.json` |
| **Cursor / Windsurf** | MCP | Add MCP server in settings |
| **Hermes Agent** | MCP + Skill | `cp integrations/hermes ~/.hermes/skills/research/behive-research` |
| **OpenClaw** | Skill | `cp integrations/openclaw ~/.openclaw/workspace/skills/behive-research` |
| **n8n** | Community Node | Install `n8n-nodes-behive` in Settings → Community Nodes |
| **ChatGPT** | Custom GPT / API | OpenAPI spec in README above |
| **Any MCP client** | Streamable HTTP | URL: `http://localhost:8090/mcp` |

See [`integrations/`](integrations/) for detailed setup guides.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code style, and PR guidelines.

```bash
git clone https://github.com/qa10devteam/behive.git
cd behive
pip install -e ".[all,dev]"
pytest
```

---

## License

MIT — use it, fork it, ship it, sell it.

---

<p align="center">
  <sub>Built by <a href="https://qa10.io">QA10</a> · Structured knowledge, not text soup.</sub>
</p>
