# Agent Integrations

BeHive ships with ready-made skills for popular AI agent frameworks.

## Hermes Agent

Copy the skill to your Hermes installation:

```bash
cp -r integrations/hermes ~/.hermes/skills/research/behive-research
```

Or install from the running agent:
```
> Load skill behive-research
```

The skill enables deep research via MCP or REST API. Hermes will automatically use BeHive when you ask it to research a topic.

**MCP Setup** (recommended — zero-code integration):

Add to `~/.hermes/config.yaml`:
```yaml
mcp_servers:
  behive:
    url: http://localhost:8090/mcp
    transport: streamable-http
```

## OpenClaw

Install the skill:

```bash
cp -r integrations/openclaw ~/.openclaw/workspace/skills/behive-research
```

Or via ClawHub (when published):
```bash
clawhub install behive-research
```

Then ask your OpenClaw agent:
> "Research the AI chip market deeply"

The agent will call BeHive's API, poll for completion, and deliver the structured report.

## Claude Desktop (MCP)

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

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

Restart Claude Desktop. BeHive tools appear automatically.

## Cursor / Windsurf / Any MCP Client

Add the MCP server URL `http://localhost:8090/mcp` (Streamable HTTP transport) in your editor's MCP settings.

## n8n

Install the community node:
```bash
# In n8n Settings → Community Nodes → Install
n8n-nodes-behive
```

Or via npm:
```bash
cd ~/.n8n
npm install n8n-nodes-behive
```

Provides **BeHive** node (research, search, missions) and **BeHive Trigger** (poll for completed missions).
