"""BeHive API Server — FastAPI app for research missions."""

import os
import time
import json
import hashlib
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional

# Install legacy import shims
try:
    from behive.compat.shims import install_shims, install_ops_shim
    install_shims()
    install_ops_shim()
except ImportError:
    pass


# ─── DB connection ────────────────────────────────────────────────────────────

def get_db_url():
    return os.environ.get(
        "DATABASE_URL",
        os.environ.get("BEHIVE_DB_URL", "postgresql://localhost:5432/behive")
    )


def get_db():
    import psycopg2
    return psycopg2.connect(get_db_url())


# ─── Security: Rate Limiter ──────────────────────────────────────────────────

class RateLimiter:
    """Simple in-memory rate limiter (per-IP, sliding window)."""
    
    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self.requests: dict[str, list[float]] = defaultdict(list)
    
    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window_start = now - self.window
        # Clean old entries
        self.requests[key] = [t for t in self.requests[key] if t > window_start]
        if len(self.requests[key]) >= self.max_requests:
            return False
        self.requests[key].append(now)
        return True


# Global rate limiters
_rate_limiter = RateLimiter(max_requests=60, window_seconds=60)  # 60 req/min general
_research_limiter = RateLimiter(max_requests=5, window_seconds=60)  # 5 research/min


# ─── Security: API Key Auth ──────────────────────────────────────────────────

def get_api_key() -> Optional[str]:
    """Get configured API key (if any). None = open access."""
    return os.environ.get("BEHIVE_API_KEY")


async def verify_auth(request: Request):
    """Verify API key if BEHIVE_API_KEY is set."""
    api_key = get_api_key()
    if not api_key:
        return  # No key configured = open access (self-hosted default)
    
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if token == api_key:
            return
    
    # Also check X-API-Key header
    x_key = request.headers.get("X-API-Key", "")
    if x_key == api_key:
        return
    
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ─── App ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🐝 BeHive API starting...")
    yield
    print("🐝 BeHive API shutdown.")


app = FastAPI(
    title="BeHive Research Engine",
    description="Deep research with structured knowledge extraction",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("BEHIVE_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Rate limiting + security headers."""
    # Get client IP
    client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
    
    # Rate limit check
    if not _rate_limiter.is_allowed(client_ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Max 60/min."},
            headers={"Retry-After": "60"},
        )
    
    # Stricter rate limit for research endpoints
    if request.url.path == "/research" and request.method == "POST":
        if not _research_limiter.is_allowed(client_ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Research rate limit: max 5/min."},
                headers={"Retry-After": "60"},
            )
    
    response = await call_next(request)
    
    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    
    return response


# ─── Models ───────────────────────────────────────────────────────────────────

class ResearchRequest(BaseModel):
    query: str = Field(..., description="Research topic or question")
    depth: int = Field(3, ge=1, le=5, description="1=quick, 3=standard, 5=deep")
    scale: Optional[int] = Field(None, ge=10, le=500, description="Task count override")
    force: bool = Field(True, description="Skip dedup check")


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.2.0"
    missions_total: int = 0
    claims_total: int = 0
    avg_quality: float = 0.0
    db: str = ""


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM hive_missions")
        missions = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*), AVG(quality_score) FROM hive_claims WHERE is_garbage = false")
        row = cur.fetchone()
        claims, avg_q = row[0], row[1] or 0
        conn.close()
        return HealthResponse(
            missions_total=missions,
            claims_total=claims,
            avg_quality=round(avg_q, 4),
            db=get_db_url().split("@")[-1] if "@" in get_db_url() else get_db_url(),
        )
    except Exception as e:
        return HealthResponse(status="degraded", db=str(e))


@app.post("/research", dependencies=[Depends(verify_auth)])
async def start_research(req: ResearchRequest):
    """Start a new research mission. Returns job_id for polling."""
    import hashlib
    import time
    from asyncio import get_event_loop

    # Generate mission ID
    slug = req.query[:40].lower().replace(" ", "_")
    ts = str(int(time.time()))
    h = hashlib.md5(f"{req.query}{ts}".encode()).hexdigest()[:6]
    mission_id = f"hive_{ts}_{h}"

    # Insert mission record
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO hive_missions (id, topic, status, phase, config, created_at)
            VALUES (%s, %s, 'queued', 'queued', %s::jsonb, NOW())
            ON CONFLICT (id) DO NOTHING
        """, (mission_id, req.query, json.dumps({"depth": req.depth, "scale": req.scale})))
        conn.commit()
        conn.close()
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")

    # Launch pipeline in background
    import asyncio
    asyncio.create_task(_run_pipeline(mission_id, req.query, req.depth))

    return {
        "mission_id": mission_id,
        "status": "running",
        "topic": req.query,
        "depth": req.depth,
        "message": f"Research started. Poll GET /research/{mission_id}/status for progress.",
    }


async def _run_pipeline(mission_id: str, topic: str, depth: int):
    """Run the full research pipeline in background."""
    import subprocess
    import sys

    try:
        # Update status to running
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE hive_missions SET status = 'running', phase = 'scout' WHERE id = %s",
            (mission_id,)
        )
        conn.commit()
        conn.close()

        # Run orchestrator as subprocess (isolates heavy pipeline)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "behive.engine",
            "run", topic,
            "--mission-id", mission_id,
            "--depth", str(depth),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "BEHIVE_MISSION_ID": mission_id},
        )

        stdout, stderr = await proc.communicate()

        # Update final status
        conn = get_db()
        cur = conn.cursor()
        if proc.returncode == 0:
            cur.execute(
                "UPDATE hive_missions SET status = 'done', phase = 'done' WHERE id = %s",
                (mission_id,)
            )
        else:
            error_msg = stderr.decode()[-500:] if stderr else "Unknown error"
            cur.execute(
                "UPDATE hive_missions SET status = 'error', phase = 'error' WHERE id = %s",
                (mission_id,)
            )
        conn.commit()
        conn.close()

    except Exception as e:
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "UPDATE hive_missions SET status = 'error', phase = 'error' WHERE id = %s",
                (mission_id,)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass


@app.get("/research/{mission_id}/status")
async def research_status(mission_id: str):
    """Get mission status."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT status, phase FROM hive_missions WHERE id = %s",
            (mission_id,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            raise HTTPException(404, f"Mission {mission_id} not found")
        return {"mission_id": mission_id, "status": row[0], "phase": row[1]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/research/{mission_id}")
async def get_research(mission_id: str):
    """Get completed research results."""
    try:
        conn = get_db()
        cur = conn.cursor()

        # Mission info
        cur.execute(
            "SELECT topic, status, synthesis FROM hive_missions WHERE id = %s",
            (mission_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"Mission {mission_id} not found")

        topic, status, report = row

        # Claims
        cur.execute("""
            SELECT claim, confidence, quality_score, source_url, claim_type
            FROM hive_claims
            WHERE mission_id = %s AND (is_garbage = false OR is_garbage IS NULL)
            ORDER BY quality_score DESC
            LIMIT 500
        """, (mission_id,))
        claims = [
            {
                "text": r[0], "confidence": r[1], "quality_score": r[2],
                "source_url": r[3], "type": r[4]
            }
            for r in cur.fetchall()
        ]

        conn.close()

        avg_q = sum(c["quality_score"] or 0 for c in claims) / max(1, len(claims))

        return {
            "mission_id": mission_id,
            "topic": topic,
            "status": status,
            "report": report or "",
            "claims": claims,
            "metrics": {
                "total_claims": len(claims),
                "avg_quality": round(avg_q, 4),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/claims/search")
async def search_claims(q: str, limit: int = 20):
    """Search claims across all missions."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT claim, quality_score, source_url, mission_id
            FROM hive_claims
            WHERE claim ILIKE %s AND (is_garbage = false OR is_garbage IS NULL)
            ORDER BY quality_score DESC
            LIMIT %s
        """, (f"%{q}%", limit))
        results = [
            {"text": r[0], "quality_score": r[1], "source_url": r[2], "mission_id": r[3]}
            for r in cur.fetchall()
        ]
        conn.close()
        return {"query": q, "results": results, "total": len(results)}
    except Exception as e:
        raise HTTPException(500, str(e))
