"""BeHive API Server — FastAPI app for research missions."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional


# ─── DB connection ────────────────────────────────────────────────────────────

def get_db_url():
    return os.environ.get(
        "DATABASE_URL",
        os.environ.get("BEHIVE_DB_URL", "postgresql://behive_user:REDACTED@localhost:5432/hive")
    )


def get_db():
    import psycopg2
    return psycopg2.connect(get_db_url())


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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.post("/research")
async def start_research(req: ResearchRequest):
    """Start a new research mission. Returns job_id for polling."""
    import hashlib
    import time

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
            INSERT INTO hive_missions (id, topic, status, depth, created_at)
            VALUES (%s, %s, 'queued', %s, NOW())
            ON CONFLICT (id) DO NOTHING
        """, (mission_id, req.query, req.depth))
        conn.commit()
        conn.close()
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")

    return {
        "mission_id": mission_id,
        "status": "queued",
        "topic": req.query,
        "depth": req.depth,
        "message": f"Research queued. Poll GET /research/{mission_id}/status for progress.",
    }


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
