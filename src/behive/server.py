"""BeHive API Server — FastAPI app for research missions."""

import os
import re
import time
import json
import hashlib
import asyncio
from collections import defaultdict, Counter
from contextlib import asynccontextmanager
from typing import Optional, AsyncGenerator

from fastapi import FastAPI, HTTPException, Request, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# Install legacy import shims
try:
    from behive.compat.shims import install_shims, install_ops_shim
    install_shims()
    install_ops_shim()
except ImportError:
    pass


# ─── DB connection ────────────────────────────────────────────────────────────

def get_db_url():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    if os.environ.get("BEHIVE_DB_URL"):
        return os.environ["BEHIVE_DB_URL"]
    # Construct from individual vars
    user = os.environ.get("HIVE_PG_USER", "behive")
    password = os.environ.get("HIVE_PG_PASSWORD", "behive")
    host = os.environ.get("HIVE_PG_HOST", "localhost")
    port = os.environ.get("HIVE_PG_PORT", "5432")
    db = os.environ.get("HIVE_PG_DATABASE", "behive")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def get_db():
    import psycopg2
    return psycopg2.connect(get_db_url())


# ─── Auth ─────────────────────────────────────────────────────────────────────

def verify_auth(request: Request):
    """Check API key if BEHIVE_API_KEY is set."""
    api_key = os.environ.get("BEHIVE_API_KEY")
    if not api_key:
        return  # No auth required
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {api_key}":
        return
    # Also check X-API-Key header
    if request.headers.get("X-API-Key") == api_key:
        return
    raise HTTPException(401, "Invalid or missing API key")


# ─── Rate Limiting ────────────────────────────────────────────────────────────

class TokenBucket:
    def __init__(self, rate: int, per: int = 60):
        self.rate = rate
        self.per = per
        self.buckets: dict[str, list] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        bucket = self.buckets[key]
        # Remove old entries
        self.buckets[key] = [t for t in bucket if now - t < self.per]
        if len(self.buckets[key]) >= self.rate:
            return False
        self.buckets[key].append(now)
        return True


_rate_limiter = TokenBucket(rate=60, per=60)
_research_limiter = TokenBucket(rate=5, per=60)


# ─── App ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🐝 BeHive API starting...")
    yield
    print("🐝 BeHive API shutdown.")


app = FastAPI(
    title="BeHive Research Engine",
    description="Deep research with structured knowledge extraction",
    version="0.3.2",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("BEHIVE_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── SSE Event Bus ────────────────────────────────────────────────────────────

_mission_events: dict[str, list[dict]] = defaultdict(list)
_mission_subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)


def _emit_event(mission_id: str, event: str, data: dict):
    """Push SSE event to store and notify subscribers."""
    entry = {"event": event, "data": data, "ts": time.time()}
    events = _mission_events[mission_id]
    events.append(entry)
    if len(events) > 500:
        _mission_events[mission_id] = events[-500:]
    for q in _mission_subscribers.get(mission_id, []):
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            pass


# ─── Security Middleware ──────────────────────────────────────────────────────

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
    if not _rate_limiter.is_allowed(client_ip):
        return JSONResponse(status_code=429, content={"detail": "Too many requests. Max 60/min."}, headers={"Retry-After": "60"})
    if request.url.path == "/research" and request.method == "POST":
        if not _research_limiter.is_allowed(client_ip):
            return JSONResponse(status_code=429, content={"detail": "Research rate limit: max 5/min."}, headers={"Retry-After": "60"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ─── Models ───────────────────────────────────────────────────────────────────

class ResearchRequest(BaseModel):
    query: str = Field(default="", description="Research topic or question")
    topic: str = Field(default="", description="Alias for query (backward compat)")
    depth: int = Field(default=3, ge=1, le=5, description="1=quick, 3=standard, 5=deep")
    scale: int = Field(default=200, ge=10, le=500, description="Max sources to scout")
    force: bool = Field(True, description="Skip dedup check")
    
    @property
    def effective_query(self) -> str:
        return self.query or self.topic


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.3.2"
    missions_total: int = 0
    claims_total: int = 0
    avg_quality: float = 0.0
    db: str = ""


# ─── Entity Extraction (lightweight NLP) ─────────────────────────────────────

_ENTITY_PATTERN = re.compile(
    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+(?:Inc|Corp|Ltd|LLC|AG|SA|GmbH|Co|plc)\.?)?)\b'
    r'|'
    r'\b([A-Z]{2,}(?:\s+[A-Z]{2,})*)\b'  # ALL CAPS (acronyms like NVIDIA, EU, AI)
    r'|'
    r'\$[\d,.]+\s*(?:billion|million|trillion|B|M|T)\b'  # Money amounts
)

_STOP_ENTITIES = {
    "The", "This", "That", "These", "Those", "However", "Although",
    "According", "Furthermore", "Additionally", "Meanwhile", "Moreover",
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
}


def extract_entities(text: str) -> list[str]:
    """Extract entity names from claim text.
    
    Primary: uses hive_entities table (LLM-extracted, typed).
    Fallback: improved regex for when DB lookup isn't available.
    """
    entities = set()
    for match in _ENTITY_PATTERN.finditer(text):
        entity = match.group(0).strip()
        if entity and len(entity) > 1 and entity not in _STOP_ENTITIES:
            # Filter out garbage: pure uppercase common words
            if entity.isupper() and len(entity) <= 3 and entity not in {
                "AI", "EU", "US", "UK", "UN", "AWS", "GCP", "IBM", "MIT", "GDP",
                "CEO", "CTO", "API", "LLM", "GPU", "CPU", "RAM", "SQL", "IoT",
            }:
                continue
            entities.add(entity)
    return sorted(entities)


def _get_typed_entities_for_claim(cur, claim_text: str, mission_id: str) -> list[dict]:
    """Get LLM-extracted typed entities from hive_entities table."""
    cur.execute("""
        SELECT DISTINCT entity_type, value 
        FROM hive_entities 
        WHERE mission_id = %s 
          AND (value ILIKE %s OR %s ILIKE '%%' || value || '%%')
        LIMIT 50
    """, (mission_id, f"%{claim_text[:100]}%", claim_text[:200]))
    return [{"type": r[0], "name": r[1]} for r in cur.fetchall()]


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

# ─── 1. Health ────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM hive_missions")
        missions = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE is_garbage = false")
        row = cur.fetchone()
        claims, avg_q = row[0], row[1] or 0
        conn.close()
        return HealthResponse(
            missions_total=missions,
            claims_total=claims,
            avg_quality=round(float(avg_q), 4),
            db=get_db_url().split("@")[-1] if "@" in get_db_url() else "connected",
        )
    except Exception as e:
        return HealthResponse(status="degraded", db=str(e)[:200])


# ─── 2. Start Research ────────────────────────────────────────────────────────

@app.post("/research", dependencies=[Depends(verify_auth)])
async def start_research(req: ResearchRequest):
    """Start a new research mission. Returns job_id for polling."""
    if not req.effective_query:
        raise HTTPException(status_code=422, detail="Either 'query' or 'topic' must be provided")
    ts = str(int(time.time()))
    h = hashlib.md5(f"{req.effective_query}{ts}".encode()).hexdigest()[:6]
    mission_id = f"hive_{ts}_{h}"

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO hive_missions (id, topic, status, phase, config, created_at)
            VALUES (%s, %s, 'queued', 'queued', %s::jsonb, NOW())
            ON CONFLICT (id) DO NOTHING
        """, (mission_id, req.effective_query, json.dumps({"depth": req.depth, "scale": req.scale})))
        conn.commit()
        conn.close()
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")

    asyncio.create_task(_run_pipeline(mission_id, req.effective_query, req.depth))

    return {
        "job_id": mission_id,
        "status": "running",
        "topic": req.effective_query,
        "depth": req.depth,
        "message": f"Research started. Poll GET /research/{mission_id}/status or stream GET /research/{mission_id}/events",
    }


# ─── 3. Research Status ──────────────────────────────────────────────────────

@app.get("/research/{mission_id}/status")
async def research_status(mission_id: str):
    """Get mission status and progress."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT status, phase, topic FROM hive_missions WHERE id = %s", (mission_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"Mission {mission_id} not found")
        status, phase, topic = row
        # Get current claims count
        cur.execute(
            "SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE mission_id = %s AND (is_garbage = false OR is_garbage IS NULL)",
            (mission_id,)
        )
        crow = cur.fetchone()
        conn.close()
        return {
            "mission_id": mission_id,
            "status": status,
            "phase": phase,
            "topic": topic,
            "claims_so_far": crow[0] or 0,
            "avg_quality": round(float(crow[1] or 0), 4),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 4. SSE Events Stream ────────────────────────────────────────────────────

@app.get("/research/{mission_id}/events")
async def research_events(mission_id: str):
    """Server-Sent Events stream for a research mission."""

    async def event_generator() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        _mission_subscribers[mission_id].append(queue)
        try:
            # Send any existing events first (replay)
            for entry in _mission_events.get(mission_id, []):
                yield f"event: {entry['event']}\ndata: {json.dumps(entry['data'])}\n\n"

            # Stream new events
            while True:
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"event: {entry['event']}\ndata: {json.dumps(entry['data'])}\n\n"
                    if entry["event"] in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield f": keepalive\n\n"
        finally:
            _mission_subscribers[mission_id].remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ─── 5. Get Full Research Results ─────────────────────────────────────────────

@app.get("/research/{mission_id}")
async def get_research(mission_id: str):
    """Get completed research results with claims."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT topic, status, phase, synthesis FROM hive_missions WHERE id = %s", (mission_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"Mission {mission_id} not found")
        topic, status, phase, report = row
        cur.execute("""
            SELECT claim, confidence, quality_score, source_url, claim_type
            FROM hive_claims
            WHERE mission_id = %s AND (is_garbage = false OR is_garbage IS NULL)
            ORDER BY quality_score DESC LIMIT 500
        """, (mission_id,))
        claims = [
            {"text": r[0], "confidence": r[1], "quality_score": r[2], "source_url": r[3], "type": r[4]}
            for r in cur.fetchall()
        ]
        conn.close()
        avg_q = sum(c["quality_score"] or 0 for c in claims) / max(1, len(claims))
        return {
            "mission_id": mission_id,
            "topic": topic,
            "status": status,
            "phase": phase,
            "report": report or "",
            "claims": claims,
            "metrics": {"total_claims": len(claims), "avg_quality": round(avg_q, 4)},
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 6. Get Report Only ──────────────────────────────────────────────────────

@app.get("/research/{mission_id}/report")
async def get_report(mission_id: str):
    """Get synthesized report for a completed mission (no claims array)."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT topic, status, synthesis FROM hive_missions WHERE id = %s", (mission_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"Mission {mission_id} not found")
        topic, status, synthesis = row
        cur.execute(
            "SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE mission_id = %s AND (is_garbage = false OR is_garbage IS NULL)",
            (mission_id,)
        )
        crow = cur.fetchone()
        conn.close()
        if status != "done":
            raise HTTPException(202, f"Mission still in progress (status={status})")
        return {
            "mission_id": mission_id,
            "topic": topic,
            "synthesis": synthesis or "",
            "claims_count": crow[0] or 0,
            "avg_quality": round(float(crow[1] or 0), 4),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 7. Search Claims (primary path) ─────────────────────────────────────────

@app.get("/claims/search")
async def search_claims(q: str, limit: int = Query(20, ge=1, le=100)):
    """Full-text search across all mission claims."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT claim, quality_score, source_url, mission_id, claim_type, confidence
            FROM hive_claims
            WHERE claim ILIKE %s AND (is_garbage = false OR is_garbage IS NULL)
            ORDER BY quality_score DESC
            LIMIT %s
        """, (f"%{q}%", limit))
        results = [
            {"text": r[0], "quality_score": r[1], "source_url": r[2], "mission_id": r[3], "type": r[4], "confidence": r[5]}
            for r in cur.fetchall()
        ]
        conn.close()
        return {"query": q, "results": results, "total": len(results)}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 8. Search alias ─────────────────────────────────────────────────────────

@app.get("/search")
async def search_alias(query: str = Query(..., alias="query"), limit: int = Query(20, ge=1, le=100)):
    """Alias for /claims/search."""
    return await search_claims(q=query, limit=limit)


# ─── 9. List Missions ────────────────────────────────────────────────────────

@app.get("/missions")
async def list_missions(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    """List all research missions, newest first."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT m.id, m.topic, m.status, m.phase, m.created_at,
                   COALESCE(c.cnt, 0) as claims_count,
                   COALESCE(c.avg_q, 0) as avg_quality
            FROM hive_missions m
            LEFT JOIN (
                SELECT mission_id, COUNT(*) as cnt, AVG(quality_score) as avg_q
                FROM hive_claims WHERE is_garbage = false OR is_garbage IS NULL
                GROUP BY mission_id
            ) c ON c.mission_id = m.id
            ORDER BY m.created_at DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        missions = [
            {
                "id": r[0], "topic": r[1], "status": r[2], "phase": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
                "claims_count": r[5], "avg_quality": round(float(r[6] or 0), 4),
            }
            for r in cur.fetchall()
        ]
        cur.execute("SELECT COUNT(*) FROM hive_missions")
        total = cur.fetchone()[0]
        conn.close()
        return {"missions": missions, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 10. Intelligence: Entity ────────────────────────────────────────────────

@app.get("/intelligence/entity/{name}")
async def intelligence_entity(name: str, limit: int = Query(50, ge=1, le=200)):
    """Get intelligence about a specific entity — from knowledge graph or claims.
    
    Uses LLM-extracted typed entities from hive_entities (72K+ entries),
    with Neo4j graph as enrichment layer.
    """
    # Try Neo4j first (richest data)
    try:
        from behive.knowledge_graph import entity_lookup
        neo4j_result = entity_lookup(name, limit=limit)
        if neo4j_result:
            return neo4j_result
    except (ImportError, Exception):
        pass

    # Primary: hive_entities table (LLM-extracted, typed entities)
    try:
        conn = get_db()
        cur = conn.cursor()
        
        # Get entity occurrences with types
        cur.execute("""
            SELECT e.entity_type, e.value, e.context, e.confidence, e.mission_id
            FROM hive_entities e
            WHERE e.value ILIKE %s
            ORDER BY e.confidence DESC
            LIMIT %s
        """, (f"%{name}%", limit))
        entity_hits = cur.fetchall()
        
        # Get claims mentioning this entity
        cur.execute("""
            SELECT claim, quality_score, source_url, mission_id, claim_type, confidence
            FROM hive_claims
            WHERE claim ILIKE %s AND (is_garbage = false OR is_garbage IS NULL)
            ORDER BY quality_score DESC
            LIMIT %s
        """, (f"%{name}%", limit))
        claims = [
            {"text": r[0], "quality_score": r[1], "source_url": r[2], 
             "mission_id": r[3], "type": r[4], "confidence": r[5]}
            for r in cur.fetchall()
        ]
        
        # Get co-occurring entities from hive_entities (same missions)
        mission_ids = list(set(r[4] for r in entity_hits))[:20]
        co_entities: Counter = Counter()
        if mission_ids:
            cur.execute("""
                SELECT value, COUNT(*) as cnt
                FROM hive_entities
                WHERE mission_id = ANY(%s)
                  AND value NOT ILIKE %s
                  AND entity_type NOT IN ('entitie', 'url', 'date')
                  AND LENGTH(value) > 2
                GROUP BY value
                ORDER BY cnt DESC
                LIMIT 30
            """, (mission_ids, f"%{name}%"))
            for row in cur.fetchall():
                co_entities[row[0]] = row[1]
        
        # Determine entity type from our data
        entity_types: Counter = Counter()
        for hit in entity_hits:
            if hit[0] and hit[0] != 'entitie':
                entity_types[hit[0]] += 1
        primary_type = entity_types.most_common(1)[0][0] if entity_types else "unknown"
        
        conn.close()

        top_related = [{"entity": e, "co_occurrences": c, "type": "co_occurrence"} 
                       for e, c in co_entities.most_common(20)]
        avg_q = sum(c["quality_score"] or 0 for c in claims) / max(1, len(claims))

        return {
            "entity": name,
            "entity_type": primary_type,
            "mentions": len(claims),
            "entity_records": len(entity_hits),
            "avg_quality": round(avg_q, 4),
            "related_entities": top_related,
            "claims": claims[:30],
            "source": "hive_entities+claims",
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 11. Intelligence: Network ───────────────────────────────────────────────

@app.get("/intelligence/network/{name}")
async def intelligence_network(name: str, depth: int = Query(2, ge=1, le=3)):
    """Entity relationship network — from knowledge graph or hive_entities co-occurrence.
    
    Uses LLM-extracted typed entities (72K+ records) to build relationship networks.
    Edges represent co-occurrence within the same mission (semantically related).
    """
    # Try Neo4j first
    try:
        from behive.knowledge_graph import entity_network
        neo4j_result = entity_network(name, depth=depth)
        if neo4j_result:
            return neo4j_result
    except (ImportError, Exception):
        pass

    # Primary: hive_entities co-occurrence within missions
    try:
        conn = get_db()
        cur = conn.cursor()

        nodes = {}  # name -> {type, weight}
        edges = []
        nodes[name] = {"type": "center", "weight": 0}

        # Find missions containing this entity
        cur.execute("""
            SELECT DISTINCT mission_id FROM hive_entities
            WHERE value ILIKE %s
            LIMIT 30
        """, (f"%{name}%",))
        mission_ids = [r[0] for r in cur.fetchall()]
        nodes[name]["weight"] = len(mission_ids)

        if not mission_ids:
            conn.close()
            return {"center": name, "depth": depth, "nodes": [{"name": name, "type": "unknown", "weight": 0}], 
                    "edges": [], "node_count": 1, "edge_count": 0}

        # Hop 1: entities co-occurring in same missions (typed!)
        cur.execute("""
            SELECT value, entity_type, COUNT(DISTINCT mission_id) as cnt
            FROM hive_entities
            WHERE mission_id = ANY(%s)
              AND value NOT ILIKE %s
              AND entity_type NOT IN ('entitie', 'url', 'date')
              AND LENGTH(value) > 2
            GROUP BY value, entity_type
            ORDER BY cnt DESC
            LIMIT 20
        """, (mission_ids, f"%{name}%"))
        hop1_results = cur.fetchall()
        
        for entity_val, entity_type, weight in hop1_results:
            nodes[entity_val] = {"type": entity_type or "unknown", "weight": weight}
            edges.append({"source": name, "target": entity_val, "weight": weight, "relation": "co_occurs_in_research"})

        # Hop 2 (if depth >= 2): for top hop1 entities, find THEIR co-occurring entities
        if depth >= 2:
            for entity_val, entity_type, _ in hop1_results[:8]:
                cur.execute("""
                    SELECT DISTINCT mission_id FROM hive_entities
                    WHERE value ILIKE %s LIMIT 15
                """, (f"%{entity_val}%",))
                hop1_missions = [r[0] for r in cur.fetchall()]
                
                if hop1_missions:
                    cur.execute("""
                        SELECT value, entity_type, COUNT(DISTINCT mission_id) as cnt
                        FROM hive_entities
                        WHERE mission_id = ANY(%s)
                          AND value NOT ILIKE %s AND value NOT ILIKE %s
                          AND entity_type NOT IN ('entitie', 'url', 'date')
                          AND LENGTH(value) > 2
                        GROUP BY value, entity_type
                        ORDER BY cnt DESC
                        LIMIT 5
                    """, (hop1_missions, f"%{entity_val}%", f"%{name}%"))
                    for h2_val, h2_type, h2_weight in cur.fetchall():
                        if h2_val not in nodes:
                            nodes[h2_val] = {"type": h2_type or "unknown", "weight": h2_weight}
                        edges.append({"source": entity_val, "target": h2_val, "weight": h2_weight, "relation": "co_occurs_in_research"})

        conn.close()
        
        node_list = [{"name": n, "type": info["type"], "weight": info["weight"]} for n, info in nodes.items()]
        
        return {
            "center": name,
            "depth": depth,
            "nodes": node_list,
            "edges": edges,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "source": "hive_entities",
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 12. Intelligence: Stats ─────────────────────────────────────────────────

@app.get("/intelligence/stats")
async def intelligence_stats():
    """System-wide intelligence statistics including knowledge graph."""
    result = {}
    
    # Neo4j graph stats
    try:
        from behive.knowledge_graph import graph_stats
        gs = graph_stats()
        if gs:
            result["knowledge_graph"] = gs
    except ImportError:
        pass

    try:
        conn = get_db()
        cur = conn.cursor()

        # Missions
        cur.execute("SELECT COUNT(*) FROM hive_missions")
        total_missions = cur.fetchone()[0]
        cur.execute("SELECT status, COUNT(*) FROM hive_missions GROUP BY status")
        missions_by_status = {r[0]: r[1] for r in cur.fetchall()}

        # Claims
        cur.execute("SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE is_garbage = false OR is_garbage IS NULL")
        row = cur.fetchone()
        total_claims, avg_quality = row[0], float(row[1] or 0)

        cur.execute("""
            SELECT claim_type, COUNT(*) FROM hive_claims
            WHERE is_garbage = false OR is_garbage IS NULL
            GROUP BY claim_type ORDER BY COUNT(*) DESC LIMIT 20
        """)
        claims_by_type = {r[0] or "unknown": r[1] for r in cur.fetchall()}

        # Top entities (sample recent claims, extract entities)
        cur.execute("""
            SELECT claim FROM hive_claims
            WHERE is_garbage = false OR is_garbage IS NULL
            ORDER BY quality_score DESC LIMIT 500
        """)
        entity_counter: Counter = Counter()
        for (claim_text,) in cur.fetchall():
            for e in extract_entities(claim_text):
                entity_counter[e] += 1

        top_entities = [{"entity": e, "mentions": c} for e, c in entity_counter.most_common(30)]

        # Quality distribution
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE quality_score >= 0.90) as excellent,
                COUNT(*) FILTER (WHERE quality_score >= 0.82 AND quality_score < 0.90) as great,
                COUNT(*) FILTER (WHERE quality_score >= 0.75 AND quality_score < 0.82) as good,
                COUNT(*) FILTER (WHERE quality_score >= 0.55 AND quality_score < 0.75) as acceptable,
                COUNT(*) FILTER (WHERE quality_score < 0.55) as rejected
            FROM hive_claims WHERE is_garbage = false OR is_garbage IS NULL
        """)
        qrow = cur.fetchone()
        quality_distribution = {
            "excellent_090+": qrow[0], "great_082+": qrow[1],
            "good_075+": qrow[2], "acceptable_055+": qrow[3], "rejected": qrow[4],
        }

        conn.close()
        result.update({
            "total_missions": total_missions,
            "total_claims": total_claims,
            "avg_quality": round(avg_quality, 4),
            "missions_by_status": missions_by_status,
            "claims_by_type": claims_by_type,
            "quality_distribution": quality_distribution,
            "top_entities": top_entities,
        })
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_pipeline(mission_id: str, topic: str, depth: int):
    """Run research pipeline as subprocess, stream progress via SSE events."""
    import sys

    try:
        _emit_event(mission_id, "start", {"topic": topic, "status": "scout"})

        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE hive_missions SET status = 'running', phase = 'scout' WHERE id = %s", (mission_id,))
        conn.commit()
        conn.close()

        _emit_event(mission_id, "phase", {"phase": "scout", "event": "started"})

        # Run orchestrator as subprocess with line-buffered output for SSE
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "behive.engine",
            "run", topic,
            "--mission-id", mission_id,
            "--depth", str(depth),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "BEHIVE_MISSION_ID": mission_id, "PYTHONUNBUFFERED": "1"},
        )

        # Parse subprocess output for phase transitions and progress
        current_phase = "scout"
        async for line in proc.stdout:
            text = line.decode(errors="replace").strip()
            # Detect phase transitions
            if "Scout done" in text or "scout.*done" in text.lower():
                current_phase = "harvest"
                _emit_event(mission_id, "phase", {"phase": "harvest", "event": "started"})
                _update_phase(mission_id, "harvest")
            elif "Harvest" in text and "done" in text.lower():
                current_phase = "process"
                _emit_event(mission_id, "phase", {"phase": "process", "event": "started"})
                _update_phase(mission_id, "process")
            elif "Process" in text and "done" in text.lower():
                current_phase = "synth"
                _emit_event(mission_id, "phase", {"phase": "synth", "event": "started"})
                _update_phase(mission_id, "synth")
            # Detect claims progress
            elif "claims" in text.lower() and any(c.isdigit() for c in text):
                nums = re.findall(r'\d+', text)
                if nums:
                    _emit_event(mission_id, "claims", {"count": int(nums[0]), "phase": current_phase})

        await proc.wait()

        # Gather final results
        conn = get_db()
        cur = conn.cursor()
        if proc.returncode == 0:
            cur.execute("UPDATE hive_missions SET status = 'done', phase = 'done' WHERE id = %s", (mission_id,))
            cur.execute(
                "SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE mission_id = %s AND (is_garbage = false OR is_garbage IS NULL)",
                (mission_id,)
            )
            row = cur.fetchone()
            _emit_event(mission_id, "done", {"total_claims": row[0] or 0, "avg_quality": round(float(row[1] or 0), 4)})
        else:
            cur.execute("UPDATE hive_missions SET status = 'error', phase = 'error' WHERE id = %s", (mission_id,))
            _emit_event(mission_id, "error", {"message": f"Pipeline exited with code {proc.returncode}"})
        conn.commit()
        conn.close()

    except Exception as e:
        _emit_event(mission_id, "error", {"message": str(e)})
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("UPDATE hive_missions SET status = 'error', phase = 'error' WHERE id = %s", (mission_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass


def _update_phase(mission_id: str, phase: str):
    """Update mission phase in DB."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE hive_missions SET phase = %s WHERE id = %s", (phase, mission_id))
        conn.commit()
        conn.close()
    except Exception:
        pass
