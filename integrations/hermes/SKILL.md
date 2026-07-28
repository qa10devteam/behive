---
name: behive-research
description: "Use when the user asks to research a topic deeply, gather intelligence, or build a knowledge base. Connects to BeHive API (self-hosted or cloud) to run multi-source research missions with structured claim extraction, quality scoring, and knowledge graph construction."
version: 1.0.0
author: QA10
license: MIT
metadata:
  hermes:
    tags: [research, behive, deep-research, knowledge-graph, claims, intelligence]
    related_skills: [hive-intelligence-system, arxiv]
---

# BeHive Deep Research

## Overview

BeHive is a deep research engine that extracts **structured, scored claims** from any topic. Unlike simple web search, it:

1. Decomposes topics into research axes
2. Fetches 1000+ sources via 8-layer stealth drones (70+ APIs)
3. Extracts typed claims with per-claim quality scores (0.0–1.0)
4. Builds entity relationship graphs (Neo4j)
5. Produces synthesized reports with inline citations

This skill connects Hermes Agent to a running BeHive instance.

## When to Use

- User says "research X deeply", "gather intelligence on Y", "find everything about Z"
- User needs verified facts with sources, not LLM-generated summaries
- User wants claims they can cite, filter by quality, or query later
- User asks to build a knowledge base on a topic over time
- User says "run a mission on X", "scale 100", "deep dive"

## Don't Use For

- Simple factual lookups (use web_search)
- Single-page content extraction (use web_extract)
- Real-time news (BeHive takes 5-15 min per mission)

## Prerequisites

BeHive API running at `http://localhost:8091` or configured endpoint.

Verify:
```bash
curl -s http://localhost:8091/health | jq .
```

If using MCP (recommended), configure in `~/.hermes/config.yaml`:
```yaml
mcp_servers:
  behive:
    url: http://localhost:8090/mcp
    transport: streamable-http
```

## Quick Research (MCP)

If BeHive MCP is configured, use MCP tools directly:

```
mcp_behive_research_topic(request={"query": "NVIDIA GPU market 2025-2026", "depth": 3, "force": true})
```

Then poll:
```
mcp_behive_mission_status(job_id="<returned_id>")
```

Get report:
```
mcp_behive_get_report(job_id="<id>", format="markdown")
```

Search past knowledge:
```
mcp_behive_search_knowledge(query="NVIDIA revenue", limit=20)
```

## Research via REST API (terminal)

### Start a Mission

```bash
curl -s -X POST http://localhost:8091/research \
  -H "Content-Type: application/json" \
  -d '{"topic": "EU AI Act enforcement mechanisms 2026", "scale": 30, "depth": 3}' | jq .
```

Scale guide:
- `15` — quick scout (2-3 min, ~50 claims)
- `30` — standard (5-10 min, ~200-500 claims)
- `100` — deep (15-30 min, ~800+ claims)
- `300` — exhaustive (45-90 min, ~2000+ claims)

### Poll Status

```bash
curl -s http://localhost:8091/research/<mission_id> | jq .status,.phase,.progress
```

Phases: `scout` → `harvest` → `process` → `synth` → `done`

### Get Report

```bash
curl -s http://localhost:8091/research/<mission_id>/report | jq .
```

### Search Claims

```bash
curl -s "http://localhost:8091/search?q=NVIDIA+revenue&limit=20" | jq .
```

### List Missions

```bash
curl -s http://localhost:8091/missions | jq '.[] | {id, topic, status, avg_quality, total_claims}'
```

### SSE Streaming (real-time progress)

```bash
curl -N http://localhost:8091/research/<mission_id>/events
```

Returns Server-Sent Events with phase transitions, progress %, and claim counts.

## Quality Interpretation

| Score Range | Meaning | Action |
|-------------|---------|--------|
| 0.85–1.00 | Excellent — specific, verified, multi-dimensional | Trust directly |
| 0.75–0.84 | Good — solid claim with evidence | Use with confidence |
| 0.65–0.74 | Acceptable — valid but may lack specifics | Verify key details |
| 0.55–0.64 | Marginal — passed quality gate barely | Cross-reference |
| < 0.55 | Rejected — never enters database | N/A |

## Knowledge Graph Queries

```bash
# Get entities
curl -s "http://localhost:8091/graph/entities?limit=50" | jq .

# Get relationships for an entity
curl -s "http://localhost:8091/graph/entity/NVIDIA/relationships" | jq .

# Network stats
curl -s http://localhost:8091/intelligence/stats | jq .
```

## Workflow: Research → Report → Deliver

1. Start mission with appropriate scale
2. Monitor via SSE or polling (every 30s)
3. On completion, fetch report
4. Format key findings for user
5. Offer to search specific claims or explore the knowledge graph

## Common Pitfalls

1. **Using web_extract on localhost** — Hermes blocks private IPs via web_extract. Always use `terminal()` + `curl` for BeHive API calls.
2. **Scale too high for simple topics** — Scale 30 is sufficient for most queries. Scale 300 takes 45+ minutes and may hit rate limits.
3. **Not waiting for completion** — Missions are async. Always poll or stream until `status=done`.
4. **Confusing mission_id formats** — IDs look like `hive_1785227949_815112`. Copy exactly from the start response.
5. **Expecting real-time results** — Even scale 15 takes 2-3 minutes. Set user expectations.

## Benchmarks (real, honest)

| Topic | Claims | Avg Quality | Duration | Sources |
|-------|--------|-------------|----------|---------|
| NVIDIA GPU market 2025 | 290 | 0.797 | 8 min | 234 |
| OpenAI GPT-5 capabilities | 574 | 0.789 | 12 min | 174 |
| EU AI Act enforcement | 267 | 0.759 | 6 min | 130 |
| Meta Llama 4 | 568 | 0.821 | 11 min | 198 |

Hardware: EC2 g6.24xlarge, 4× NVIDIA L4, Bedrock Claude Haiku + Sonnet.

## Verification Checklist

- [ ] `curl http://localhost:8091/health` returns 200
- [ ] MCP configured in config.yaml (if using MCP path)
- [ ] Mission started and job_id captured
- [ ] Status polled until `done` or `error`
- [ ] Report delivered to user in readable format
