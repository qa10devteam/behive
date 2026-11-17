<p align="center">
  <img src="demo.gif" alt="BeHive — Deep Research Engine" width="720" />
</p>

<h1 align="center">🐝 BeHive</h1>

<p align="center">
  <strong>Open-source research engine that extracts structured knowledge from any topic.</strong><br />
  Feed it a question. Get back scored claims, entity graphs, and a synthesized report — not paragraphs of slop.
</p>

<p align="center">
  <a href="https://pypi.org/project/behive"><img src="https://img.shields.io/badge/pip_install-behive-FFB300?style=flat-square&logo=pypi&logoColor=white" /></a>
  <a href="https://www.npmjs.com/package/n8n-nodes-behive"><img src="https://img.shields.io/badge/n8n-community_node-FF6D5A?style=flat-square&logo=n8n&logoColor=white" /></a>
  <img src="https://img.shields.io/badge/MCP-Live-00C853?style=flat-square" />
  <img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" />
  <img src="https://img.shields.io/badge/version-0.6.0-black?style=flat-square" />
</p>

---

## The Problem

You ask an AI to research something. It hallucinates confidently or gives you a 2000-word essay that says nothing specific. No sources. No confidence scores. No way to verify.

**BeHive fixes this.** It goes out, searches real sources, extracts verifiable claims with confidence scores, and builds a knowledge graph — all through your existing AI (Claude, ChatGPT, Gemini).

---

## ⚡ Use with Claude Desktop (10 seconds)

**No install needed.** Add this to your Claude Desktop config and you're done:

**File:** `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows)

```json
{
  "mcpServers": {
    "behive": {
      "url": "https://api.behive.site/mcp",
      "transport": "streamable-http"
    }
  }
}
```

Restart Claude Desktop. Ask: *"Research the EU AI Act enforcement timeline"*

That's it. Claude calls BeHive automatically.

<details>
<summary>💡 What happens under the hood</summary>

```
You ask Claude → Claude calls BeHive MCP tool → BeHive scouts 70+ APIs
→ fetches 200+ URLs via stealth drones → extracts claims with scores
→ deduplicates → returns structured report to Claude
```

**Cost:** Free. The hosted MCP server is provided by the BeHive team.  
**Privacy:** Queries are processed and discarded. No data retention.
</details>

---

## ⚡ Use with ChatGPT (Custom GPT)

1. Go to [chat.openai.com/gpts/editor](https://chat.openai.com/gpts/editor)
2. Create a new GPT
3. Under **Actions** → click **Import from URL** → paste:
   ```
   https://api.behive.site/openapi.json
   ```
4. Name it "Deep Researcher" and save

Now ask your GPT: *"Research quantum computing breakthroughs 2026"*

---

## ⚡ Use with Cursor / Windsurf / Claude Code

Add to your MCP config (`.cursor/mcp.json`, `~/.claude/settings.json`, etc.):

```json
{
  "mcpServers": {
    "behive": {
      "url": "https://api.behive.site/mcp",
      "transport": "streamable-http"
    }
  }
}
```

---

## ⚡ Use with Hermes Agent / OpenClaw

```bash
# Hermes — already has a built-in skill
hermes skill install behive-research

# OpenClaw — copy from integrations/
cp integrations/openclaw/SKILL.md ~/.openclaw/skills/behive.md
```

---

## 🛠 Self-Host (full control)

Want to run BeHive on your own machine? Three options:

### Option A: Zero-setup (DuckDB, no PostgreSQL needed)

```bash
pip install behive
export ANTHROPIC_API_KEY=***  # or OPENAI_API_KEY

behive research "NVIDIA GPU market 2026"
```

Data is stored locally in `~/.behive/research.duckdb`. No database setup needed.

### Option B: Docker Compose (recommended for teams)

```bash
git clone https://github.com/qa10devteam/behive && cd behive
echo "ANTHROPIC_API_KEY=***" > .env
docker compose up -d
# API → http://localhost:8091
# MCP → http://localhost:8090/mcp
```

### Option C: Full production (PostgreSQL + GPU)

```bash
pip install "behive[all]"
export BEHIVE_DB_URL=postgresql://user:pass@localhost:5432/behive
export ANTHROPIC_API_KEY=***
behive serve
```

---

## 🧠 Model Configuration

BeHive works with any LLM. You choose what runs each pipeline stage:

| Stage | What it does | Recommended model |
|-------|-------------|-------------------|
| **scout** | Query generation, source planning | Claude Haiku 4.5 / GPT-4.1-mini / local |
| **harvest** | Content triage, relevance filtering | Claude Haiku 4.5 / GPT-4.1-mini / local |
| **process** | Claim extraction, entity recognition | Claude Sonnet 4.6 / GPT-4.5 / Gemini 2.5 Flash |
| **synth** | Report synthesis, deduplication | Claude Opus 4.6 / GPT-4.5 / Gemini 2.5 Pro |
| **falsifier** | Cross-verification, bias detection | Claude Sonnet 5 / o4-mini |

### Quick config

```bash
behive config --preset budget     # Haiku everywhere (~$0.30/mission)
behive config --preset balanced   # Haiku scout + Sonnet synth (~$1.50)
behive config --preset deep       # Opus synth + Falsifier (~$4.00)
```

### Model presets (2026)

| Preset | Model String |
|--------|-------------|
| `claude-haiku` | anthropic/claude-haiku-4-5-20251001 |
| `claude-sonnet` | anthropic/claude-sonnet-4-6 |
| `claude-opus` | anthropic/claude-opus-4-6-v1 |
| `claude-sonnet-5` | anthropic/claude-sonnet-5 |
| `gpt-4.1-mini` | openai/gpt-4.1-mini |
| `gpt-4.5` | openai/gpt-4.5-preview |
| `o4-mini` | openai/o4-mini |
| `gemini-flash` | google/gemini-2.5-flash |
| `gemini-pro` | google/gemini-2.5-pro |
| `llama-4` | ollama/llama4-scout |
| `local` | openai/local-model (any OpenAI-compatible endpoint) |

### Environment variables

```bash
export BEHIVE_MODEL=anthropic/claude-sonnet-4-6          # one model for everything
export BEHIVE_MODEL_SCOUT=ollama/llama4-scout            # per-stage override
export BEHIVE_MODEL_SYNTH=anthropic/claude-opus-4-6-v1   # per-stage override
```

Priority: `BEHIVE_MODEL_{STAGE}` > `BEHIVE_MODEL` > config.yaml > defaults

Or pass any [litellm-compatible](https://docs.litellm.ai/docs/providers) model string directly.

The CLI reads `~/.behive_runtime.env` for the current account. Set
`BEHIVE_ENV_FILE` to use a different explicit file. Existing environment values
win over that file. BeHive does not import credentials from Hermes auth files.

For a source checkout, graph support has its own dependency extra:
`pip install -e '.[graph]'`. Harvesting and processing extras are separate.
These local metadata changes have not been published to PyPI.

API providers requiring keys are skipped when configuration is missing; these
notices are diagnostics, not research evidence. LEX also requires its contracted
`LEX_API_BASE` endpoint: BeHive does not query the SIP login portal as an API.
BYOK Discord notifications are off by default. To enable them, explicitly set
`BEHIVE_BYOK_NOTIFICATIONS=1` and supply the bot credentials and destination in
the environment; no Hermes credential file is read.

---

## Verification and scoring

No benchmark table is published here without a reproducible run receipt.
Claim counts, runtime and scores vary with the topic, sources, configured model,
provider availability and pipeline options. Quality scores are internal ranking
signals, not calibrated probabilities or a guarantee of factual correctness.

The repository contains regression tests for provider configuration, source
admission, entity handling and deterministic synthesis. PostgreSQL persistence,
public-provider checks and browser tests must be reported separately from
controlled-provider unit tests. Never run database-writing tests against a shared
research database: use a disposable cluster and retain the raw results.

---

## Drone Arsenal

BeHive's API registry contains **83 provider entries**. Registered does not mean
configured or available: some providers require credentials or contracts, and
public endpoints can fail or return no relevant results.

| Category | Examples | Method |
|----------|----------|--------|
| Web search | DuckDuckGo, Brave, SearXNG | Multi-backend fallthrough |
| Academic | arXiv, Semantic Scholar, PubMed | Direct API |
| News | Google News RSS, NewsAPI, GDELT | Real-time feeds |
| Government | EU Open Data, USPTO, SEC EDGAR | Registry APIs |
| Code | GitHub, GitLab, npm, PyPI | Package ecosystems |
| Financial | Yahoo Finance, FRED, ECB | Market data |
| Social | Reddit, HN (via Algolia), X/Twitter | Community signals |

**Stealth layer:** 8-layer drone architecture bypasses Cloudflare, DataDome, Akamai, PerimeterX. Rotating fingerprints, browser TLS profiles, residential proxy support.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      BeHive Pipeline                          │
└─────────────────────────────────────────────────────────────┘
                              │
    ┌───────────┬─────────────┼─────────────┬───────────┐
    ▼           ▼             ▼             ▼           ▼
┌────────┐ ┌────────┐ ┌──────────┐ ┌────────┐ ┌────────┐
│ SCOUT  │ │HARVEST │ │ PROCESS  │ │ SYNTH  │ │ GRAPH  │
│        │ │        │ │          │ │        │ │        │
│ Queen  │ │Parallel│ │ Dual-    │ │ Report │ │ Neo4j  │
│ plans  │ │ fetch  │ │ model    │ │ synth  │ │ build  │
│ 200+   │ │ drones │ │ extract  │ │ dedupe │ │ entity │
│ tasks  │ │ 70 APIs│ │ + score  │ │ FUIR   │ │ link   │
└────────┘ └────────┘ └──────────┘ └────────┘ └────────┘
```

**Key design:** The Queen plans all research tasks BEFORE any scout leaves. This means parallel execution, no redundant searches, and smart gap detection.

---

## API Reference

### REST API (port 8091)

```bash
# Start a research mission
curl -X POST http://localhost:8091/research \
  -H "Content-Type: application/json" \
  -d '{"topic": "quantum computing 2026", "depth": 3}'

# Check status (status, phase, reason, report_available, useful_claims, fallback_claims)
curl http://localhost:8091/research/{mission_id}/status

# Get report (HTTP 409 with the reason if the mission is empty or failed)
curl http://localhost:8091/research/{mission_id}/report

# Search knowledge base
curl "http://localhost:8091/search?q=quantum+supremacy&limit=20"
```

### MCP Tools

| Tool | Description |
|------|-------------|
| `research_topic` | Start deep research on any topic |
| `mission_status` | Check progress; prints status, phase, reason and claim counts |
| `get_report` | Retrieve the final synthesized report; says why when there is none |
| `search_knowledge` | Query across all previous research |
| `list_missions` | List recent research missions |

### Mission status

A mission ends in one of three statuses:

- `done`: the mission has a report and at least one useful claim.
- `empty`: the pipeline finished but produced no qualifying claims, so there is no report.
- `error`: the pipeline failed or timed out and produced no report.

A mission that is cancelled reports `cancelled` with the reason "Mission was cancelled."
While a mission runs its status is `queued` or `running`, and `phase` moves through
`queued`, `scout`, `harvest`, `process` and `synth`. When a mission ends as `empty` or
`error`, `phase` holds the stage or cause where it ended (one of the keys below) and
`reason` explains it.

`GET /research/{mission_id}/status` returns these fields:

- `status`: `queued`, `running`, `done`, `empty`, `error` or `cancelled`.
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

`GET /research/{mission_id}/report` returns the report with HTTP 200 only for a `done`
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

### Python SDK

```python
from behive import BeHiveClient

client = BeHiveClient()  # auto-detects local server

# Start research
mission = client.research("EU AI Act penalties", depth=3)

# Wait and get results
report = mission.wait()
print(f"Found {len(report.claims)} claims, avg quality: {report.quality:.2f}")
```

---

## Comparison

| Feature | BeHive | GPT-Researcher | Tavily | Perplexity |
|---------|--------|---------------|--------|------------|
| **Structured claims** | ✅ JSON with scores | ❌ Text only | ❌ Snippets | ❌ Text |
| **Knowledge graph** | ✅ Neo4j entities | ❌ | ❌ | ❌ |
| **Self-hosted** | ✅ MIT | ✅ | ❌ SaaS only | ❌ SaaS only |
| **MCP native** | ✅ | ❌ | ❌ | ❌ |
| **Quality scoring** | ✅ 5-dimension | ❌ | ❌ | ❌ |
| **Multi-source** | 70+ APIs | ~5 | 1 (Tavily) | Internal |
| **Cost** | Free + your LLM | Free + your LLM | $50-500/mo | $20/mo |
| **Falsifier** | ✅ Cross-verify | ❌ | ❌ | ❌ |

---

## Integrations

| Platform | Method | Difficulty |
|----------|--------|-----------|
| **Claude Desktop** | MCP (add URL) | 10 seconds |
| **ChatGPT** | Custom GPT (import URL) | 30 seconds |
| **Cursor / Windsurf** | MCP config | 10 seconds |
| **Claude Code** | MCP config | 10 seconds |
| **Hermes Agent** | Built-in skill | 1 command |
| **OpenClaw** | Skill file | 1 file copy |
| **n8n** | Community node | Install in UI |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). PRs welcome.

```bash
git clone https://github.com/qa10devteam/behive
cd behive
pip install -e ".[dev]"
pytest
```

---

## License

MIT — use it however you want. Star ⭐ if it helps.

---

<p align="center">
  Built by <a href="https://qa10.io">QA10</a> · <a href="https://pypi.org/project/behive">PyPI</a> · <a href="https://github.com/qa10devteam/behive/issues">Issues</a>
</p>
