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

# Set your LLM API key (you use YOUR OWN subscription — BeHive costs nothing)
export ANTHROPIC_API_KEY=your-key  # or OPENAI_API_KEY, or AWS creds for Bedrock

# PostgreSQL required for storage (or use Docker below)
export BEHIVE_DB_URL=postgresql://user:***@localhost:5432/behive

# Start the server
behive serve
```

**Fastest path — Docker Compose (PostgreSQL included):**
```bash
git clone https://github.com/qa10devteam/behive && cd behive
echo "ANTHROPIC_API_KEY=your-key" > .env
docker compose up -d
# API at http://localhost:8091
```

**Full install** (stealth drones, content extraction, NLP processing):
```bash
pip install "behive[all]"
```

> **⚠️ GPU/CUDA note:** `behive[all]` does NOT include GPU dependencies. If you need vector embeddings (Qdrant), install separately:
> `pip install "behive[qdrant]"` — this pulls PyTorch + sentence-transformers (~4GB with CUDA).
> For CPU-only machines, install torch CPU-only first: `pip install torch --index-url https://download.pytorch.org/whl/cpu`

Or pick what you need:
```bash
pip install "behive[stealth]"   # curl_cffi, primp, nodriver, patchright
pip install "behive[harvest]"   # trafilatura, newspaper4k, PyMuPDF, crawl4ai
pip install "behive[process]"   # rapidfuzz, spacy, litellm, tiktoken
pip install "behive[mcp,api]"   # MCP server + REST API
```

That's it. BeHive is now running:
- **API** → `http://localhost:8091` (REST endpoints)
- **MCP** → `http://localhost:8090/mcp` (for AI assistants)
- **Docs** → `http://localhost:8091/docs` (Swagger UI)

---

## 🧠 Model Routing — Use Cheap Models to Collect, Smart Models to Analyze

BeHive doesn't force you into one model. You choose what runs each pipeline stage:

| Stage | Role | Recommended |
|-------|------|-------------|
| **scout** | Query generation, source discovery | Haiku / GPT-4o-mini / local |
| **harvest** | Relevance filtering, content triage | Haiku / GPT-4o-mini / local |
| **process** | Claim extraction, quality scoring | Haiku or Sonnet |
| **synth** | Report synthesis, deduplication | Sonnet / Opus / GPT-4o |

### Quick Setup (one command)

```bash
# Apply a preset
behive config --preset balanced   # Haiku collects, Sonnet synthesizes (~$1.50/mission)
behive config --preset budget     # Haiku everywhere (~$0.30/mission)
behive config --preset quality    # Sonnet everywhere (~$4.00/mission)
behive config --preset local      # Your own LLM server ($0.00/mission)
```

### Interactive Setup

```bash
behive config --quick    # Pick one model for everything
behive config --full     # Choose model per stage (interactive)
```

### Fine-grained Control

```bash
# Set a single stage
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
| `bedrock-haiku` | bedrock/us.anthropic.claude-haiku-4-5-... |
| `bedrock-sonnet` | bedrock/us.anthropic.claude-sonnet-4-6-... |
| `local` | openai/local-model (any OpenAI-compatible server) |
| `ollama` | ollama/llama3.1 |

Or pass any [litellm-compatible](https://docs.litellm.ai/docs/providers) model string directly.

---

## 🔌 Setup with Claude Desktop (30 seconds)

> **You bring your Claude subscription. BeHive adds research superpowers. No extra cost from us.**

**Step 1:** Install and start BeHive:
```bash
pip install behive
export ANTHROPIC_API_KEY=*** # your own key
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

Claude will call BeHive automatically, fetch 200+ sources, and return scored claims instead of guessing from training data.

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
BeHive scores, deduplicates, builds knowledge graph
    ↓
Returns structured report to Claude
    ↓
Claude presents findings with confidence scores and source links
```

**Cost: ~$0.30–$2.00 per research mission** (your Anthropic/OpenAI tokens).
BeHive itself: **free forever** (MIT license).
</details>

---

## 🔌 Setup with ChatGPT (Custom GPT)

**Step 1:** Start BeHive on a server with a public URL (or use tunneling):
```bash
pip install behive
export OPENAI_API_KEY=*** # your own key
behive serve --host 0.0.0.0

# Expose with a tunnel (for testing):
# npx cloudflared tunnel --url http://localhost:8091
```

**Step 2:** Create a Custom GPT at [chat.openai.com/gpts/editor](https://chat.openai.com/gpts/editor):

- **Name:** "Deep Researcher (BeHive)"
- **Instructions:** "You are a research analyst. Use the BeHive actions to research topics. Always cite claim confidence scores."
- **Actions → Import URL:** paste your server URL + `/openapi.json`

Or manually add this schema:

```yaml
openapi: 3.1.0
info:
  title: BeHive Research API
  version: 0.3.0
servers:
  - url: https://*** paths:
  /research:
    post:
      operationId: startResearch
      summary: Start a deep research mission
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [query]
              properties:
                query:
                  type: string
                  description: Research topic or question
                depth:
                  type: integer
                  default: 3
                  description: 1=quick, 3=standard, 5=deep
      responses:
        '200':
          description: Mission started successfully
  /research/{mission_id}:
    get:
      operationId: getResearchResults
      summary: Get completed research with scored claims
      parameters:
        - name: mission_id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: Research results with claims and report
  /claims/search:
    get:
      operationId: searchKnowledge
      summary: Search across all previously researched knowledge
      parameters:
        - name: q
          in: query
          required: true
          schema:
            type: string
        - name: limit
          in: query
          schema:
            type: integer
            default: 20
      responses:
        '200':
          description: Matching claims with scores
```

**Step 3:** Use your Custom GPT. Ask: "Research quantum computing breakthroughs 2026"

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

## 🔌 Setup with Hermes Agent / OpenClaw

**Hermes Agent** (automatic — skill already published):
```bash
# BeHive skill auto-loads when you ask Hermes to research anything
# Just ensure behive serve is running on the same machine
behive serve
```

**OpenClaw:**
```bash
# Install from integrations directory
cp integrations/openclaw/SKILL.md ~/.openclaw/skills/behive-research.md
```

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
behive serve              # Starts both REST API (:8091) + MCP (:8090)
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

### Search Backends (priority order)

BeHive tries search backends in priority order and falls through on failure:

| Priority | Backend | Env Variable | Free Tier |
|:---:|---------|-------------|-----------|
| 1 | SearXNG (self-hosted) | `SEARXNG_URL=http://localhost:8080` | Unlimited |
| 2 | Brave Search | `BRAVE_SEARCH_API_KEY=***` | 2,000 req/month |
| 3 | Serper.dev (Google) | `SERPER_API_KEY=***` | 2,500 credits |
| 4 | Tavily | `TAVILY_API_KEY=***` | 1,000 req/month |
| 5 | DuckDuckGo | *(always available)* | Unlimited (slow) |

No env vars set? DDG is the default. Add any key above to instantly upgrade search quality.

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
- [x] REST API (12 endpoints)
- [x] MCP Server (Streamable HTTP)
- [x] SSE streaming (real-time progress)
- [x] Knowledge graph (Neo4j)
- [x] 70+ API sources (37 free APIs confirmed working, 26 need BYOK keys)
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
