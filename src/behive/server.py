"""BeHive API Server — FastAPI app for research missions."""

import os
import re
import time
import json
import hashlib
import asyncio
import logging

log = logging.getLogger(__name__)
from collections import defaultdict, Counter
from contextlib import asynccontextmanager
from typing import Optional, AsyncGenerator, Literal

from fastapi import FastAPI, HTTPException, Request, Depends, Query
from behive import __version__ as BEHIVE_VERSION
from behive.hygiene import (
    MIN_CLAIMS_FOR_DONE, MISSION_REASONS, PROCESS_FAILURE_PHASES, SQL_CLAIM_NOT_BLOB, SQL_USEFUL_CLAIM,
    classify_completion, has_salvageable_claims, is_structured_blob, mission_reason, sql_done_product,
    sql_report_present,
)

# ─── Concurrency control ──────────────────────────────────────────────────────
_MAX_CONCURRENT_MISSIONS = int(os.environ.get("BEHIVE_MAX_CONCURRENT", "3"))
_mission_semaphore: asyncio.Semaphore = None  # initialized in lifespan

def _get_semaphore() -> asyncio.Semaphore:
    global _mission_semaphore
    if _mission_semaphore is None:
        _mission_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_MISSIONS)
    return _mission_semaphore
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


def get_db_url_display():
    """Return DB URL with password masked — for logs and health endpoint."""
    url = get_db_url()
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", url)


def get_db():
    """Connect to PostgreSQL. Returns connection or raises with helpful message."""
    try:
        import psycopg2
    except ImportError:
        raise RuntimeError(
            "psycopg2 not installed. Run: pip install 'behive[postgres]' or pip install psycopg2-binary"
        )
    url = get_db_url()
    try:
        return psycopg2.connect(url)
    except psycopg2.OperationalError as e:
        err_msg = str(e)
        raise RuntimeError(
            f"Cannot connect to PostgreSQL. {err_msg.strip()}\n\n"
            "Quick setup:\n"
            "  1. Install PostgreSQL: sudo apt install postgresql\n"
            "  2. Create database: behive init-db\n"
            "  3. Or set DATABASE_URL environment variable\n"
            f"\n  Tried: {get_db_url_display()}"
        ) from e
    except Exception as e:
        raise RuntimeError(f"Database error: {e}") from e


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
    """Application lifespan — check DB connectivity on startup."""
    print("🐝 BeHive API starting...")
    # Validate DB connection at startup (non-fatal — API still serves /health as degraded)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        conn.close()
        print("✅ PostgreSQL connected")
    except RuntimeError as e:
        print(f"⚠️  Database not available: {e}")
        print("   The API will start but /research will fail until DB is configured.")
        print("   Run: behive init-db")
    except Exception as e:
        print(f"⚠️  Database check failed: {e}")
    yield
    print("🐝 BeHive API shutdown.")


app = FastAPI(
    title="BeHive Research Engine",
    description="Deep research with structured knowledge extraction",
    version=BEHIVE_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("BEHIVE_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Free Tools Routers ──────────────────────────────────────────────────────
try:
    from behive.brand_check import router as brand_check_router
    from behive.brand_check import BrandCheckRequest, check_brand_visibility
    app.include_router(brand_check_router)

    @app.post("/brand-check")
    async def brand_check_alias(req: BrandCheckRequest):
        return await check_brand_visibility(req)
except ImportError:
    pass

# ─── Federated Knowledge Network ─────────────────────────────────────────────
try:
    from behive.federation import router as federation_router
    app.include_router(federation_router)
except ImportError:
    pass


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
    scale: int = Field(default=30, ge=10, le=500, description="Max sources to scout")
    force: bool = Field(True, description="Skip dedup check")
    reasoning_effort: Optional[Literal["low", "medium", "high"]] = Field(
        default=None, description="Optional reasoning effort for the whole mission (default: the bridge's, low)")
    synthesis_effort: Optional[Literal["low", "medium", "high"]] = Field(
        default=None, description="Optional reasoning effort for the report synthesis only")

    @property
    def effective_query(self) -> str:
        return self.query or self.topic


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = BEHIVE_VERSION
    missions_total: int = 0
    missions_engine: int = 0
    missions_empty: int = 0
    missions_error: int = 0
    missions_error_empty: int = 0
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
    """Regex Title-Case tokens from a string. Does NOT read hive_entities."""
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
        cur.execute(
            """SELECT COUNT(*) FROM hive_missions m
               WHERE status='done' AND EXISTS (
                 SELECT 1 FROM hive_claims c WHERE c.mission_id=m.id
                   AND (is_garbage=false OR is_garbage IS NULL))"""
        )
        missions_engine = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM hive_missions WHERE status='empty'")
        missions_empty = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM hive_missions WHERE status='error'")
        missions_error = cur.fetchone()[0]
        cur.execute(
            """SELECT COUNT(*) FROM hive_missions m
               WHERE status IN ('empty','error')
                 AND NOT EXISTS (SELECT 1 FROM hive_content x WHERE x.mission_id=m.id)
                 AND NOT EXISTS (SELECT 1 FROM hive_claims c WHERE c.mission_id=m.id)"""
        )
        missions_error_empty = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE is_garbage = false")
        row = cur.fetchone()
        claims, avg_q = row[0], row[1] or 0
        conn.close()
        return HealthResponse(
            missions_total=missions,
            missions_engine=missions_engine,
            missions_empty=missions_empty,
            missions_error=missions_error,
            missions_error_empty=missions_error_empty,
            claims_total=claims,
            avg_quality=round(float(avg_q), 4),
            db=get_db_url().split("@")[-1] if "@" in get_db_url() else "connected",
        )
    except Exception as e:
        return HealthResponse(status="degraded", db=str(e)[:200])


@app.get("/byok")
async def byok_status():
    """Which vendor keys are missing and how to obtain them. Never returns secret values."""
    from behive.engine.byok import status_list
    return {"byok": status_list()}


# ─── 2. Start Research ────────────────────────────────────────────────────────

@app.post("/research", dependencies=[Depends(verify_auth)])
async def start_research(req: ResearchRequest):
    """Start a new research mission. Returns job_id for polling."""
    if not req.effective_query:
        raise HTTPException(status_code=422, detail="Either 'query' or 'topic' must be provided")
    ts = str(int(time.time()))
    h = hashlib.md5(f"{req.effective_query}{ts}".encode()).hexdigest()[:6]
    mission_id = f"hive_{ts}_{h}"

    # Only what the caller chose is stored; without the fields the config is depth and scale, as before.
    config: dict = {"depth": req.depth, "scale": req.scale}
    if req.reasoning_effort:
        config["reasoning_effort"] = req.reasoning_effort
    if req.synthesis_effort:
        config["synthesis_effort"] = req.synthesis_effort

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO hive_missions (id, topic, status, phase, config, created_at)
            VALUES (%s, %s, 'queued', 'queued', %s::jsonb, NOW())
            ON CONFLICT (id) DO NOTHING
        """, (mission_id, req.effective_query, json.dumps(config)))
        conn.commit()
        conn.close()
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")

    asyncio.create_task(_run_pipeline(mission_id, req.effective_query, req.depth, req.scale))

    return {
        "job_id": mission_id,
        "status": "running",
        "topic": req.effective_query,
        "depth": req.depth,
        "message": f"Research started. Poll GET /research/{mission_id}/status or stream GET /research/{mission_id}/events",
    }


def _claim_split(cur, mission_id: str) -> tuple[int, int]:
    """(useful claims, regex_fallback claims) of a mission; see hygiene.SQL_USEFUL_CLAIM."""
    cur.execute(f"SELECT COUNT(*) FROM hive_claims c WHERE c.mission_id = %s AND {SQL_USEFUL_CLAIM}", (mission_id,))
    useful = int((cur.fetchone() or (0,))[0] or 0)
    cur.execute(
        "SELECT COUNT(*) FROM hive_claims WHERE mission_id = %s AND claim_type = 'regex_fallback' "
        "AND (is_garbage = false OR is_garbage IS NULL)",
        (mission_id,),
    )
    fallback = int((cur.fetchone() or (0,))[0] or 0)
    return useful, fallback


# ─── 3. Research Status ──────────────────────────────────────────────────────

@app.get("/research/{mission_id}/status")
async def research_status(mission_id: str):
    """Get mission status and progress."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            f"""SELECT status, phase, topic, sources_found, sources_harvested, sources_processed,
                      harvest_done_at, process_done_at, {sql_report_present('hive_missions')}
               FROM hive_missions WHERE id = %s""",
            (mission_id,),
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"Mission {mission_id} not found")
        status, phase, topic, n_found, n_harv, n_proc, harvest_done, process_done, report_present = row
        cur.execute(
            "SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE mission_id = %s AND (is_garbage = false OR is_garbage IS NULL)",
            (mission_id,)
        )
        crow = cur.fetchone() or (0, 0)
        useful, fallback = _claim_split(cur, mission_id)
        cur.execute("SELECT COUNT(*) FROM hive_content WHERE mission_id = %s", (mission_id,))
        n_content = int((cur.fetchone() or (0,))[0] or 0)
        cur.execute("SELECT COUNT(*) FROM hive_facts WHERE mission_id = %s", (mission_id,))
        n_facts = int((cur.fetchone() or (0,))[0] or 0)
        conn.close()
        derived = phase
        if process_done and phase in ("harvest", "scout", "process", "queued"):
            derived = "synth"
        elif harvest_done and phase in ("harvest", "scout", "queued"):
            derived = "process"
        return {
            "mission_id": mission_id,
            "status": status,
            "phase": derived,
            "topic": topic,
            "reason": mission_reason(status, phase, report_present=bool(report_present), useful_claims=useful),
            "report_available": status == "done" and bool(report_present) and useful > 0,
            "claims_so_far": crow[0] or 0,
            "useful_claims": useful,
            "fallback_claims": fallback,
            "avg_quality": round(float(crow[1] or 0), 4),
            "sources_found": int(n_found or 0),
            "sources_harvested": int(n_harv or 0),
            "sources_processed": int(n_proc or 0),
            "n_content": n_content,
            "n_facts": n_facts,
            "harvest_done_at": harvest_done.isoformat() if harvest_done else None,
            "process_done_at": process_done.isoformat() if process_done else None,
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
        cur.execute(
            f"SELECT topic, status, phase, synthesis, {sql_report_present('hive_missions')} FROM hive_missions WHERE id = %s",
            (mission_id,),
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"Mission {mission_id} not found")
        topic, status, phase, report, report_present = row
        useful, _fallback = _claim_split(cur, mission_id)
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
            "reason": mission_reason(status, phase, report_present=bool(report_present), useful_claims=useful),
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
        cur.execute(
            f"SELECT topic, status, synthesis, phase, {sql_report_present('hive_missions')} FROM hive_missions WHERE id = %s",
            (mission_id,),
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"Mission {mission_id} not found")
        topic, status, synthesis, phase, report_present = row
        cur.execute(
            "SELECT COUNT(*), COALESCE(AVG(quality_score), 0) FROM hive_claims WHERE mission_id = %s AND (is_garbage = false OR is_garbage IS NULL)",
            (mission_id,)
        )
        crow = cur.fetchone()
        useful, fallback = _claim_split(cur, mission_id)
        conn.close()
        if status in {"empty", "error", "cancelled"}:
            # Never publish persisted exception strings or provider responses.
            safe_phase = phase if phase in MISSION_REASONS else status
            reason = mission_reason(status, safe_phase)
            return JSONResponse(status_code=409, content={
                "detail": reason, "mission_id": mission_id, "topic": topic,
                "status": status, "phase": safe_phase, "reason": reason,
                "report_available": False,
                "synthesis": "", "claims_count": crow[0] or 0,
                "useful_claims": useful, "fallback_claims": fallback,
                "avg_quality": round(float(crow[1] or 0), 4),
            })
        if status != "done":
            raise HTTPException(202, f"Mission still in progress (status={status})")
        # Missions finished before the 2026-11-13 rules can be 'done' without a
        # report or with regex material only: say so instead of an empty success.
        return {
            "mission_id": mission_id,
            "topic": topic,
            "synthesis": synthesis or "",
            "report_available": bool(report_present) and useful > 0,
            "reason": mission_reason(status, phase, report_present=bool(report_present), useful_claims=useful),
            "claims_count": crow[0] or 0,
            "useful_claims": useful,
            "fallback_claims": fallback,
            "avg_quality": round(float(crow[1] or 0), 4),
        }
    except HTTPException:
        raise
    except Exception as e:
        log.warning("Mission %s report unavailable (%s)", mission_id, type(e).__name__)
        raise HTTPException(500, "Report unavailable") from None


# ─── 7. Search Claims (primary path) ─────────────────────────────────────────

@app.get("/claims/search")
async def search_claims(q: str, limit: int = Query(20, ge=1, le=100)):
    """Full-text search across all mission claims."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT claim, quality_score, source_url, mission_id, claim_type, confidence
            FROM hive_claims
            WHERE claim ILIKE %s
              AND (is_garbage = false OR is_garbage IS NULL)
              AND {SQL_CLAIM_NOT_BLOB}
            ORDER BY quality_score DESC
            LIMIT %s
        """, (f"%{q}%", limit))
        results = [
            {"text": r[0], "quality_score": r[1], "source_url": r[2], "mission_id": r[3], "type": r[4], "confidence": r[5]}
            for r in cur.fetchall()
            if not is_structured_blob(r[0])
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
    """Attested facts (≥2 domains, same number) first. Else hive_triples co-occurrence."""
    try:
        from behive.engine.fact_synth import attested_network
        att = attested_network(name)
        if att.get("attested"):
            att["depth"] = depth
            return att
    except Exception:
        pass

    try:
        from behive.engine.triples import triples_for_entity
        stored = triples_for_entity(name, limit=40)
        if stored:
            nodes = {name: {"type": "center", "weight": 0}}
            edges = []
            for t in stored:
                other = t["object"] if name.lower() in (t["subject"] or "").lower() else t["subject"]
                if other == name:
                    other = t["object"] if t["object"] != name else t["subject"]
                nodes[other] = {"type": "entity", "weight": t["source_count"]}
                nodes[name]["weight"] += t["source_count"]
                edges.append({
                    "source": t["subject"],
                    "target": t["object"],
                    "weight": t["source_count"],
                    "relation": "co_occurs_in_research",
                })
            node_list = [{"name": n, "type": info["type"], "weight": info["weight"]} for n, info in nodes.items()]
            return {
                "center": name,
                "depth": depth,
                "nodes": node_list,
                "edges": edges,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "source": "hive_triples",
                "predicate": "co_occurs_in_research",
                "knowledge_graph": False,
            }
    except Exception:
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
            "knowledge_graph": False,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ─── 12. Intelligence: Stats ─────────────────────────────────────────────────

@app.get("/intelligence/stats")
async def intelligence_stats():
    """System-wide stats. Not a knowledge graph."""
    result = {"knowledge_graph": False}

    # Neo4j is not the product. Do not attach a knowledge_graph blob.

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

        cur.execute("SELECT COUNT(*) FROM hive_triples")
        triples_total = int(cur.fetchone()[0] or 0)

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
            "top_entities_method": "regex Title-Case on 500 claims, not hive_entities",
            "hive_triples": triples_total,
            "knowledge_graph": False,
            "graph_kind": "qdrant_index + cooccurrence triples + attested facts",
        })
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/intelligence/graph")
async def intelligence_graph():
    """Qdrant collection + NetworkX graph.json from payloads. Not a knowledge graph."""
    from behive.engine.qdrant_claims import collection_stats
    st = collection_stats()
    st["knowledge_graph"] = False
    st["kind"] = "qdrant_index_plus_payload_graph"
    st["graph_json"] = False
    st["graph_engine"] = "networkx in venv; graph.json from Qdrant payloads"
    try:
        from behive.engine.graph_engine import GRAPH_FILE, META_FILE
        st["graph_json"] = GRAPH_FILE.exists()
        st["graph_path"] = str(GRAPH_FILE)
        if META_FILE.exists():
            nxm = json.loads(META_FILE.read_text())
            tops = nxm.get("top_entities_by_degree") or nxm.get("top_entities_by_pagerank") or []
            honest = []
            for t in tops:
                honest.append({
                    "entity": t.get("entity"),
                    "degree_norm": t.get("degree_norm", t.get("pagerank")),
                })
            nxm["top_entities_by_degree"] = honest
            nxm.pop("top_entities_by_pagerank", None)
            nxm["centrality"] = "degree/n (not PageRank; scipy absent)"
            nxm["rebuild"] = "Qdrant payload copy; not live HDBSCAN"
            nxm["knowledge_graph"] = False
            st["networkx"] = nxm
    except Exception as e:
        st["graph_error"] = str(e)[:200]
    return st


_G_CACHE = None


def _cached_graph():
    global _G_CACHE
    if _G_CACHE is None:
        from behive.engine.graph_engine import load_graph
        _G_CACHE = load_graph()
    return _G_CACHE


def _slim_node(n: dict) -> dict:
    out = {
        "node_id": n.get("node_id") or n.get("canonical_name"),
        "node_type": n.get("node_type"),
        "canonical_name": n.get("canonical_name"),
        "text": (n.get("text") or "")[:240],
        "mission_id": n.get("mission_id"),
        "confidence": n.get("confidence"),
        "community_c0": n.get("community_c0"),
        "community_id": n.get("community_id"),
    }
    return {k: v for k, v in out.items() if v is not None and v != ""}


@app.get("/intelligence/graph/query")
async def intelligence_graph_query(term: str = Query(..., min_length=2), top_n: int = Query(12, ge=1, le=40)):
    from behive.engine.graph_engine import query_graph
    G = _cached_graph()
    hits = await asyncio.to_thread(query_graph, term, G, top_n, True)
    return {
        "term": term,
        "count": len(hits),
        "method": "substring scan of graph.json nodes (not GraphRAG)",
        "knowledge_graph": False,
        "hits": [_slim_node(h) for h in hits],
    }


@app.get("/intelligence/graph/communities")
async def intelligence_graph_communities(level: str = Query("C0"), top_n: int = Query(15, ge=1, le=50)):
    from behive.engine.graph_engine import get_communities
    lvl = "C0" if level.upper() == "C0" else "C1"
    G = _cached_graph()
    rows = await asyncio.to_thread(get_communities, G, top_n, lvl)
    claim_n = 0
    try:
        from behive.engine.graph_engine import META_FILE
        if META_FILE.exists():
            claim_n = int((json.loads(META_FILE.read_text()) or {}).get("claim_nodes") or 0)
    except Exception:
        pass
    largest = int((rows[0] or {}).get("node_count") or 0) if rows else 0
    share = (largest / claim_n) if claim_n else 0.0
    return {
        "level": lvl,
        "count": len(rows),
        "communities": rows,
        "clustering": "copied Qdrant community_c0/c1 labels, not live HDBSCAN",
        "claim_nodes": claim_n,
        "largest_share": round(share, 4),
        "skewed": share >= 0.5,
        "knowledge_graph": False,
    }


@app.get("/intelligence/graph/path")
async def intelligence_graph_path(a: str = Query(...), b: str = Query(...)):
    from behive.engine.graph_engine import shortest_path
    G = _cached_graph()
    path = await asyncio.to_thread(shortest_path, a, b, G)
    return {
        "a": a,
        "b": b,
        "hops": max(0, len(path) - 1),
        "path": [_slim_node(p) for p in path],
        "method": "shortest_path on graph.json co-occurrence (not causal)",
        "knowledge_graph": False,
    }


@app.get("/intelligence/semantic")
async def intelligence_semantic(query: str = Query(..., min_length=3), top_k: int = Query(8, ge=1, le=30)):
    """Semantic search over existing Qdrant hive_intelligence (95k MiniLM vectors)."""
    from behive.engine.qdrant_claims import semantic_search
    result = await asyncio.to_thread(semantic_search, query, top_k)
    if not result.get("ok"):
        raise HTTPException(503, result.get("error") or "semantic search failed")
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_pipeline(mission_id: str, topic: str, depth: int, scale: int = 30):
    """Run research pipeline as subprocess, stream progress via SSE events."""
    import sys
    
    # Concurrency limiter — max N missions at once
    sem = _get_semaphore()
    if sem.locked():
        _emit_event(mission_id, "queued", {"position": _MAX_CONCURRENT_MISSIONS, "message": "Waiting for slot..."})
        _update_phase(mission_id, "queued")
    
    async with sem:
        await _run_pipeline_inner(mission_id, topic, depth, scale)


async def _run_pipeline_inner(mission_id: str, topic: str, depth: int, scale: int = 30):
    """Actual pipeline execution (called within semaphore)."""
    import sys

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE hive_missions SET status = 'running', phase = 'scout' "
            "WHERE id = %s AND status NOT IN ('done', 'cancelled', 'empty', 'error') RETURNING id",
            (mission_id,),
        )
        started = cur.fetchone()
        conn.commit()
        conn.close()
        conn = None
        if not started:
            return
        _emit_event(mission_id, "start", {"topic": topic, "status": "scout"})
        _emit_event(mission_id, "phase", {"phase": "scout", "event": "started"})

        # Run orchestrator as subprocess with line-buffered output for SSE
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "behive.engine",
            "run", topic,
            "--mission-id", mission_id,
            "--depth", str(depth),
            "--scale", str(scale),
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

        # Recovery may supply evidence, but a failed recovery is not empty scout.
        salvage_error = None
        try:
            from behive.engine.claims_fallback import salvage_claims_from_content
            await asyncio.to_thread(salvage_claims_from_content, mission_id)
        except Exception as exc:
            salvage_error = type(exc).__name__
            log.warning("Mission %s claims recovery failed (%s)", mission_id, salvage_error)
        if proc.returncode != 0:
            log.warning("Mission %s pipeline failed (phase=%s, exit=%s)",
                        mission_id, current_phase, proc.returncode)

        # Gather final results. Exit 0 is not done: done needs a non-blank report
        # AND a useful claim (regex_fallback salvage never decides done), and a
        # worker's process-phase failure stays error whatever material exists.
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            f"SELECT status, phase, {sql_report_present('hive_missions')} FROM hive_missions WHERE id = %s",
            (mission_id,),
        )
        worker_status, worker_phase, report_present = cur.fetchone() or (None, None, False)
        cur.execute("SELECT COUNT(*) FROM hive_sources WHERE mission_id = %s", (mission_id,))
        n_src = int((cur.fetchone() or (0,))[0] or 0)
        cur.execute("SELECT COUNT(*) FROM hive_content WHERE mission_id = %s", (mission_id,))
        n_content = int((cur.fetchone() or (0,))[0] or 0)
        cur.execute(
            f"SELECT COUNT(*), COALESCE(AVG(c.quality_score), 0) FROM hive_claims c WHERE c.mission_id = %s AND {SQL_USEFUL_CLAIM}",
            (mission_id,),
        )
        row_cl = cur.fetchone() or (0, 0)
        n_cl = int(row_cl[0] or 0)
        avg_q = row_cl[1] or 0
        outcome_status, outcome_phase = classify_completion(
            useful_claims=n_cl, report_present=bool(report_present), sources=n_src, content=n_content)
        if worker_status == "error" and worker_phase in PROCESS_FAILURE_PHASES:
            new_status, new_phase = "error", worker_phase
        elif outcome_status == "done" and (has_salvageable_claims(n_cl) or proc.returncode == 0):
            new_status, new_phase = "done", "done"
        elif outcome_status == "error":
            new_status, new_phase = outcome_status, outcome_phase
        # A worker's explicit clean-empty transition is terminal, not a crash
        # (cmd_run exits 1 for empty). Other nonzero exits are errors.
        elif salvage_error is not None or worker_status == "error" or (proc.returncode != 0 and worker_status != "empty"):
            new_status = "error"
            new_phase = "claims_fallback_failed" if salvage_error else "pipeline_failed"
        else:
            new_status, new_phase = outcome_status, outcome_phase
        if new_status == "done":
            recovered = proc.returncode != 0 or salvage_error or worker_status == "error"
            event_data = {
                "total_claims": n_cl, "avg_quality": round(float(avg_q or 0), 4),
                "salvaged": proc.returncode != 0, "completion_basis": "report_and_useful_claims",
                "recovery": "source_product" if recovered else None,
            }
        else:
            reason = MISSION_REASONS.get(new_phase) or mission_reason(new_status, new_phase)
            event_data = {"message": reason, "reason": reason, "phase": new_phase,
                          "sources": n_src, "claims": n_cl, "exit_code": proc.returncode}
            if salvage_error:
                event_data["error_type"] = salvage_error
        # This invocation claimed a nonterminal mission before spawning. A legacy
        # synthesis writer may set provisional done: confirm a real product here
        # or correct a false done. A real done product (report + useful claim) is
        # never downgraded by a later worker error. Calls that start after
        # completion are rejected by the initial claim.
        done_product = sql_done_product("hive_missions")
        cur.execute(
            f"""UPDATE hive_missions SET status = %s, phase = %s WHERE id = %s
                AND status <> 'cancelled'
                AND (status <> 'done' OR %s = 'done' OR NOT {done_product})
                AND (%s <> 'done' OR ({done_product} AND (SELECT COUNT(*) FROM hive_claims c
                    WHERE c.mission_id = hive_missions.id AND {SQL_USEFUL_CLAIM}) >= %s))
                AND (%s <> 'empty' OR NOT EXISTS (SELECT 1 FROM hive_claims c
                    WHERE c.mission_id = hive_missions.id AND {SQL_USEFUL_CLAIM}))
                RETURNING id""",
            (new_status, new_phase, mission_id, new_status, new_status,
             1 if proc.returncode == 0 else MIN_CLAIMS_FOR_DONE, new_status),
        )
        transitioned = cur.fetchone()
        conn.commit()
        conn.close()
        conn = None
        if not transitioned:
            return
        _emit_event(mission_id, new_status, event_data)

        if new_status == "done":
            try:
                await asyncio.to_thread(_bridge_done_mission, mission_id)
            except Exception as e:
                _emit_event(mission_id, "bridge_error", {"message": "Source-product bridge failed", "error_type": type(e).__name__})

    except Exception as e:
        log.warning("Mission %s pipeline exception (%s)", mission_id, type(e).__name__)
        try:
            # Discard any failed finalization before attempting an error transition.
            if conn is not None:
                conn.rollback()
                conn.close()
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "UPDATE hive_missions SET status = 'error', phase = 'pipeline_exception' WHERE id = %s "
                "AND status NOT IN ('done', 'cancelled', 'empty', 'error') RETURNING id",
                (mission_id,),
            )
            transitioned = cur.fetchone()
            conn.commit()
            conn.close()
            if transitioned:
                _emit_event(mission_id, "error", {"message": "Pipeline execution raised an exception",
                                                  "error_type": type(e).__name__})
        except Exception:
            pass


def _bridge_done_mission(mission_id: str) -> None:
    """Push mission claims to SR. Must not raise into the API loop."""
    review_file = os.environ.get("BEHIVE_SR_MANUAL_REVIEW_IDS_FILE")
    if review_file is not None:
        try:
            with open(review_file, encoding="utf-8") as handle:
                held_ids = json.load(handle)
            if not isinstance(held_ids, list) or not all(isinstance(item, str) for item in held_ids):
                raise ValueError("Invalid review ID list")
        except Exception:
            log.error("Automatic SR bridge skipped: review gate unavailable")
            return
        if mission_id in held_ids:
            log.info("Automatic SR bridge held for manual review")
            return
    from behive.sr_bridge import bridge_mission
    bridge_mission(mission_id)


def _update_phase(mission_id: str, phase: str):
    """Update mission phase in DB."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE hive_missions SET phase = %s WHERE id = %s AND status NOT IN ('done', 'cancelled', 'empty', 'error')", (phase, mission_id))
        conn.commit()
        conn.close()
    except Exception:
        pass
