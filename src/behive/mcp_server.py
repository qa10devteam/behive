"""BeHive MCP Server — Model Context Protocol endpoint for AI assistants."""

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional


app = FastAPI(title="BeHive MCP", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    import psycopg2
    url = os.environ.get("DATABASE_URL", os.environ.get(
        "BEHIVE_DB_URL", "postgresql://behive_user:***@localhost:5432/hive"
    ))
    return psycopg2.connect(url)


class MCPRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[int] = None
    method: str
    params: Optional[dict] = None


@app.post("/mcp")
async def mcp_endpoint(request: MCPRequest):
    """MCP Streamable HTTP — handles initialize, tools/list, tools/call."""

    if request.method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request.id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "behive", "version": "0.2.0"},
            }
        }

    elif request.method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request.id,
            "result": {
                "tools": [
                    {
                        "name": "research_topic",
                        "description": "Start a deep research mission on any topic. Returns scored claims, entities, and a synthesized report.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Research question or topic"},
                                "depth": {"type": "integer", "description": "1-5 (1=quick, 3=standard, 5=deep)", "default": 3},
                            },
                            "required": ["query"],
                        }
                    },
                    {
                        "name": "search_knowledge",
                        "description": "Search previously extracted claims and facts across all research missions.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Search terms"},
                                "limit": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                            },
                            "required": ["query"],
                        }
                    },
                    {
                        "name": "get_report",
                        "description": "Get the synthesized research report for a completed mission.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "mission_id": {"type": "string", "description": "Mission ID from research_topic"},
                            },
                            "required": ["mission_id"],
                        }
                    },
                    {
                        "name": "mission_status",
                        "description": "Check the progress of a running research mission.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "job_id": {"type": "string", "description": "Mission/job ID"},
                            },
                            "required": ["job_id"],
                        }
                    },
                ]
            }
        }

    elif request.method == "tools/call":
        tool_name = request.params.get("name") if request.params else None
        arguments = request.params.get("arguments", {}) if request.params else {}

        if tool_name == "research_topic":
            return await _tool_research(request.id, arguments)
        elif tool_name == "search_knowledge":
            return await _tool_search(request.id, arguments)
        elif tool_name == "get_report":
            return await _tool_get_report(request.id, arguments)
        elif tool_name == "mission_status":
            return await _tool_mission_status(request.id, arguments)
        else:
            return _error(request.id, -32601, f"Unknown tool: {tool_name}")

    return _error(request.id, -32601, f"Unknown method: {request.method}")


# ─── Tool implementations ────────────────────────────────────────────────────

async def _tool_research(req_id, args):
    """Start a research mission."""
    query = args.get("query", "")
    depth = args.get("depth", 3)

    if not query:
        return _tool_result(req_id, "Error: query is required", True)

    import httpx
    api_port = os.environ.get("BEHIVE_API_PORT", "8091")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{api_port}/research",
                json={"query": query, "depth": depth, "force": True},
                timeout=30,
            )
            data = resp.json()
            return _tool_result(req_id, str(data))
    except Exception as e:
        return _tool_result(req_id, f"Error starting research: {e}", True)


async def _tool_search(req_id, args):
    """Search claims."""
    query = args.get("query", "")
    limit = args.get("limit", 20)

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT claim, quality_score, source_url, mission_id
            FROM hive_claims
            WHERE claim ILIKE %s AND (is_garbage = false OR is_garbage IS NULL)
            ORDER BY quality_score DESC
            LIMIT %s
        """, (f"%{query}%", limit))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return _tool_result(req_id, f"No claims found matching '{query}'")

        lines = [f"Found {len(rows)} claims matching '{query}':\n"]
        for r in rows:
            score = r[1] or 0
            lines.append(f"[{score:.2f}] {r[0][:200]}")
            if r[2]:
                lines.append(f"       Source: {r[2]}")
        return _tool_result(req_id, "\n".join(lines))
    except Exception as e:
        return _tool_result(req_id, f"Search error: {e}", True)


async def _tool_get_report(req_id, args):
    """Get mission report."""
    mission_id = args.get("mission_id", "")
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT synthesis FROM hive_missions WHERE id = %s", (mission_id,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return _tool_result(req_id, f"Mission '{mission_id}' not found", True)
        return _tool_result(req_id, row[0] or "Report not yet generated. Mission may still be running.")
    except Exception as e:
        return _tool_result(req_id, f"Error: {e}", True)


async def _tool_mission_status(req_id, args):
    """Check mission status."""
    job_id = args.get("job_id", "")
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT status, phase, topic FROM hive_missions WHERE id = %s", (job_id,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return _tool_result(req_id, f"Mission '{job_id}' not found", True)
        return _tool_result(req_id, f"Mission: {row[2]}\nStatus: {row[0]}\nPhase: {row[1] or 'pending'}")
    except Exception as e:
        return _tool_result(req_id, f"Error: {e}", True)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _tool_result(req_id, text, is_error=False):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        }
    }


def _error(req_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message}
    }
