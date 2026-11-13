"""BeHive MCP Server — Model Context Protocol endpoint for AI assistants."""

import os
from behive import __version__ as BEHIVE_VERSION
from behive.hygiene import SQL_CLAIM_NOT_BLOB, SQL_USEFUL_CLAIM, is_structured_blob, mission_reason, sql_report_present
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional


app = FastAPI(title="BeHive MCP", version=BEHIVE_VERSION)

# Alias for user convenience: `from behive.mcp_server import mcp`
mcp = app

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
                "serverInfo": {"name": "behive", "version": BEHIVE_VERSION},
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
                        "description": "Get the synthesized research report for a completed mission. Missions without a report (error, empty, cancelled) return the status, phase and reason instead.",
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
                        "description": "Check the progress of a research mission: status, phase, claim counts and, when there is no usable report, the reason.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "job_id": {"type": "string", "description": "Mission/job ID"},
                            },
                            "required": ["job_id"],
                        }
                    },
                    {
                        "name": "list_missions",
                        "description": "List recent research missions with status and quality scores.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "limit": {"type": "integer", "description": "Max missions to return (default 10, max 50)", "default": 10},
                            },
                            "required": [],
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
        elif tool_name == "list_missions":
            return await _tool_list_missions(request.id, arguments)
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
        cur.execute(f"""
            SELECT claim, quality_score, source_url, mission_id
            FROM hive_claims
            WHERE claim ILIKE %s
              AND (is_garbage = false OR is_garbage IS NULL)
              AND {SQL_CLAIM_NOT_BLOB}
            ORDER BY quality_score DESC
            LIMIT %s
        """, (f"%{query}%", limit))
        rows = [r for r in cur.fetchall() if not is_structured_blob(r[0])]
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


def _mission_state(mission_id: str):
    """(topic, status, phase, synthesis, report_present, useful, fallback) or None."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT topic, status, phase, synthesis, {sql_report_present('hive_missions')} "
            "FROM hive_missions WHERE id = %s", (mission_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(f"SELECT COUNT(*) FROM hive_claims c WHERE c.mission_id = %s AND {SQL_USEFUL_CLAIM}", (mission_id,))
        useful = int((cur.fetchone() or (0,))[0] or 0)
        cur.execute(
            "SELECT COUNT(*) FROM hive_claims WHERE mission_id = %s AND claim_type = 'regex_fallback' "
            "AND (is_garbage = false OR is_garbage IS NULL)", (mission_id,)
        )
        fallback = int((cur.fetchone() or (0,))[0] or 0)
        return (*row[:4], bool(row[4]), useful, fallback)
    finally:
        conn.close()


async def _tool_get_report(req_id, args):
    """Get mission report; missions without one say why (never an empty success)."""
    mission_id = args.get("mission_id", "")
    try:
        state = _mission_state(mission_id)
        if state is None:
            return _tool_result(req_id, f"Mission '{mission_id}' not found", True)
        topic, status, phase, synthesis, report_present, useful, _fallback = state
        reason = mission_reason(status, phase, report_present=report_present, useful_claims=useful)
        if status in ("empty", "error", "cancelled") or (status == "done" and reason):
            return _tool_result(req_id, f"No report for mission '{mission_id}'.\n"
                                        f"Status: {status}\nPhase: {phase or 'pending'}\nReason: {reason}")
        if status != "done":
            return _tool_result(req_id, "Report not yet generated. Mission is still running "
                                        f"(status={status}, phase={phase or 'pending'}).")
        return _tool_result(req_id, synthesis)
    except Exception as e:
        return _tool_result(req_id, f"Error: {e}", True)


async def _tool_mission_status(req_id, args):
    """Check mission status, with the reason when there is no usable report."""
    job_id = args.get("job_id", "")
    try:
        state = _mission_state(job_id)
        if state is None:
            return _tool_result(req_id, f"Mission '{job_id}' not found", True)
        topic, status, phase, _synthesis, report_present, useful, fallback = state
        reason = mission_reason(status, phase, report_present=report_present, useful_claims=useful)
        lines = [f"Mission: {topic}", f"Status: {status}", f"Phase: {phase or 'pending'}"]
        if reason:
            lines.append(f"Reason: {reason}")
        lines.append(f"Claims: {useful} useful, {fallback} regex fallback")
        lines.append("Report: " + ("available" if status == "done" and report_present and useful else "not available"))
        return _tool_result(req_id, "\n".join(lines))
    except Exception as e:
        return _tool_result(req_id, f"Error: {e}", True)


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _tool_list_missions(req_id, args):
    """List recent research missions."""
    limit = min(args.get("limit", 10), 50)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT m.id, m.topic, m.status, m.phase, m.created_at,
                   COALESCE(c.cnt, 0), COALESCE(c.avg_q, 0)
            FROM hive_missions m
            LEFT JOIN (
                SELECT mission_id, COUNT(*) as cnt, AVG(quality_score) as avg_q
                FROM hive_claims WHERE is_garbage = false OR is_garbage IS NULL
                GROUP BY mission_id
            ) c ON c.mission_id = m.id
            ORDER BY m.created_at DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return _tool_result(req_id, "No research missions found yet.")

        lines = [f"Found {len(rows)} mission(s):\n"]
        for row in rows:
            mid, topic, status, phase, created, claims_count, avg_q = row
            created_str = created.strftime("%Y-%m-%d %H:%M") if created else "?"
            lines.append(
                f"• [{status}] {topic[:60]}\n"
                f"  ID: {mid} | Claims: {claims_count} | Quality: {avg_q:.3f} | {created_str}"
            )
        return _tool_result(req_id, "\n".join(lines))
    except Exception as e:
        return _tool_result(req_id, f"Error: {e}", True)


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
