---
name: behive-research
description: "Deep research missions with structured claim extraction, quality scoring, and knowledge graphs. Connects to BeHive API to research any topic at scale."
version: 1.0.0
author: qa10devteam
---

# BeHive Deep Research

Run multi-source research missions that extract structured, scored claims from any topic. Returns verified intelligence — not text summaries.

## Setup

BeHive must be running. Install and start:

```bash
pip install behive
cp .env.example .env  # add your LLM API key
docker compose up -d
# OR: behive api start
```

Verify: `curl http://localhost:8091/health`

## Operations

### 1. Start Research Mission

When the user asks to research a topic, deeply investigate something, or gather intelligence:

```bash
curl -s -X POST http://localhost:8091/research \
  -H "Content-Type: application/json" \
  -d '{"topic": "<USER_TOPIC>", "scale": 30, "depth": 3}'
```

**Scale guide:**
- `15` = quick scout (2-3 min, ~50 claims)
- `30` = standard (5-10 min, ~200-500 claims)
- `100` = deep (15-30 min, ~800+ claims)
- `300` = exhaustive (45-90 min, ~2000+ claims)

Save the returned `mission_id` or `job_id`.

### 2. Check Progress

Poll every 30 seconds:

```bash
curl -s http://localhost:8091/research/<MISSION_ID>
```

Phases: `scout` → `harvest` → `process` → `synth` → `done`

Or stream real-time via SSE:
```bash
curl -N http://localhost:8091/research/<MISSION_ID>/events
```

### 3. Get Report

When status is `done`:

```bash
curl -s http://localhost:8091/research/<MISSION_ID>/report
```

Returns the full synthesized report with citations and quality metrics.

### 4. Search Knowledge

Search across all past missions:

```bash
curl -s "http://localhost:8091/search?q=<QUERY>&limit=20"
```

### 5. Knowledge Graph

Query entities and relationships:

```bash
curl -s "http://localhost:8091/graph/entities?limit=50"
curl -s "http://localhost:8091/graph/entity/<NAME>/relationships"
```

### 6. List Past Missions

```bash
curl -s http://localhost:8091/missions
```

## Output Format

Claims are structured JSON with:
- `claim` — the extracted fact
- `quality_score` — 0.0 to 1.0 (only ≥0.55 enter the database)
- `source_url` — origin URL
- `confidence` — model confidence
- `claim_type` — fact, statistic, quote, prediction, etc.
- `evidence` — supporting context

## Quality Interpretation

| Score | Meaning |
|-------|---------|
| 0.85+ | Excellent — specific, multi-dimensional, verified |
| 0.75–0.84 | Good — solid with evidence |
| 0.65–0.74 | Acceptable — valid but may lack specifics |
| < 0.65 | Marginal or rejected |

## Guardrails

- Do NOT use `web_extract` or browser tools for localhost URLs — use `exec` with curl
- Missions are async — always poll until `done` before fetching report
- Scale 30 is the default — only increase if user explicitly asks for deeper research
- If health check fails, inform user that BeHive is not running
- Report mission duration and claim count to user after completion

## Example Workflow

User: "Research the AI chip market — NVIDIA vs AMD vs custom silicon"

1. Start: `curl -X POST .../research -d '{"topic": "AI chip market NVIDIA AMD custom silicon 2025-2026", "scale": 30}'`
2. Poll until done (typically 8-12 min at scale 30)
3. Fetch report, summarize key findings
4. Offer to search specific claims or explore the knowledge graph
