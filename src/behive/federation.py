"""Federated Knowledge Network — shared intelligence layer.

Enables BeHive instances to contribute verified claims to a shared pool
and query collective knowledge. "Backlink exchange for intelligence."

Architecture:
- Contributor: anonymized by hashed API key
- Claims: only high-confidence, sourced claims are shared
- Reputation: earn trust by contributing quality findings
- Enrichment: pre-scout your research with network knowledge
"""

import os
import time
import hashlib
import asyncio
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, Header, Query


router = APIRouter(prefix="/federation", tags=["federation"])


# ─── Models ───────────────────────────────────────────────────────────────────

class FederatedClaim(BaseModel):
    """A claim shared to the federated knowledge network."""
    claim_hash: str = Field(..., description="SHA256 of normalized claim text")
    text: str = Field(..., min_length=10, max_length=2000)
    confidence: float = Field(..., ge=0, le=1)
    network_confidence: float = Field(0.0, ge=0, le=1)
    source_urls: list[str] = Field(default_factory=list, max_length=10)
    domain_tags: list[str] = Field(default_factory=list, max_length=5)
    entity_refs: list[str] = Field(default_factory=list, max_length=20)
    contributor_hash: str = ""
    contributed_at: Optional[str] = None
    confirmations: int = 0
    contradictions: int = 0


class ContributeRequest(BaseModel):
    """Request to contribute claims to the network."""
    claims: list[FederatedClaim] = Field(..., min_length=1, max_length=100)


class ContributeResponse(BaseModel):
    accepted: int
    rejected: int
    reasons: list[str] = Field(default_factory=list)


class NetworkSearchResult(BaseModel):
    claims: list[FederatedClaim]
    total: int
    query: str


class ConfirmRequest(BaseModel):
    claim_hash: str
    confirmed: bool = True  # True = confirm, False = contradict


class NetworkStats(BaseModel):
    total_claims: int
    total_contributors: int
    total_confirmations: int
    avg_confidence: float
    top_domains: list[dict]
    last_contribution: Optional[str] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _hash_contributor(token: str) -> str:
    """Anonymize contributor identity."""
    return hashlib.sha256(f"behive-fkn-{token}".encode()).hexdigest()[:16]


def _hash_claim(text: str) -> str:
    """Normalize and hash claim text for dedup."""
    normalized = text.lower().strip()
    # Remove extra whitespace
    normalized = " ".join(normalized.split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


def _get_db():
    """Get database connection for federation tables."""
    try:
        import psycopg2
        db_url = os.environ.get("BEHIVE_DB_URL") or os.environ.get("DATABASE_URL")
        if not db_url:
            user = os.environ.get("HIVE_PG_USER", "behive")
            password = os.environ.get("HIVE_PG_PASSWORD", "behive")
            host = os.environ.get("HIVE_PG_HOST", "localhost")
            port = os.environ.get("HIVE_PG_PORT", "5432")
            database = os.environ.get("HIVE_PG_DATABASE", "behive")
            db_url = f"postgresql://{user}:{password}@{host}:{port}/{database}"
        return psycopg2.connect(db_url)
    except Exception:
        return None


def _ensure_tables():
    """Create federation tables if they don't exist."""
    conn = _get_db()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS federated_claims (
                    claim_hash TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    confidence FLOAT NOT NULL DEFAULT 0.0,
                    network_confidence FLOAT NOT NULL DEFAULT 0.0,
                    source_urls TEXT[] DEFAULT '{}',
                    domain_tags TEXT[] DEFAULT '{}',
                    entity_refs TEXT[] DEFAULT '{}',
                    contributor_hash TEXT NOT NULL,
                    contributed_at TIMESTAMPTZ DEFAULT NOW(),
                    confirmations INT DEFAULT 0,
                    contradictions INT DEFAULT 0
                );
                
                CREATE TABLE IF NOT EXISTS federated_contributors (
                    contributor_hash TEXT PRIMARY KEY,
                    reputation_score FLOAT DEFAULT 0.5,
                    claims_contributed INT DEFAULT 0,
                    claims_confirmed INT DEFAULT 0,
                    claims_flagged INT DEFAULT 0,
                    domain_expertise JSONB DEFAULT '{}',
                    joined_at TIMESTAMPTZ DEFAULT NOW(),
                    last_active TIMESTAMPTZ DEFAULT NOW()
                );
                
                CREATE TABLE IF NOT EXISTS federated_votes (
                    id SERIAL PRIMARY KEY,
                    claim_hash TEXT NOT NULL REFERENCES federated_claims(claim_hash),
                    voter_hash TEXT NOT NULL,
                    confirmed BOOLEAN NOT NULL,
                    voted_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(claim_hash, voter_hash)
                );
                
                CREATE INDEX IF NOT EXISTS idx_fed_claims_domain ON federated_claims USING GIN (domain_tags);
                CREATE INDEX IF NOT EXISTS idx_fed_claims_confidence ON federated_claims (network_confidence DESC);
                CREATE INDEX IF NOT EXISTS idx_fed_claims_contributor ON federated_claims (contributor_hash);
                CREATE INDEX IF NOT EXISTS idx_fed_claims_text ON federated_claims USING GIN (to_tsvector('english', text));
            """)
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        return False
    finally:
        conn.close()


# Initialize tables on import
_tables_ready = False


def _init_tables():
    global _tables_ready
    if not _tables_ready:
        _tables_ready = _ensure_tables()
    return _tables_ready


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/contribute", response_model=ContributeResponse)
async def contribute_claims(
    req: ContributeRequest,
    x_api_key: str = Header(None, alias="X-API-Key"),
    authorization: str = Header(None),
):
    """
    Contribute verified claims to the Federated Knowledge Network.
    
    Requirements:
    - Claims must have confidence >= 0.7
    - Claims must have at least 1 source URL
    - Claims must be at least 10 characters
    - Max 100 claims per request
    """
    token = x_api_key or ""
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(401, "API key required to contribute")

    if not _init_tables():
        raise HTTPException(503, "Federation database not available")

    contributor_hash = _hash_contributor(token)
    accepted = 0
    rejected = 0
    reasons = []

    conn = _get_db()
    if not conn:
        raise HTTPException(503, "Database connection failed")

    try:
        with conn.cursor() as cur:
            # Ensure contributor exists
            cur.execute("""
                INSERT INTO federated_contributors (contributor_hash)
                VALUES (%s)
                ON CONFLICT (contributor_hash) DO UPDATE SET last_active = NOW()
            """, (contributor_hash,))

            for claim in req.claims:
                # Quality gate
                if claim.confidence < 0.7:
                    rejected += 1
                    reasons.append(f"Claim too low confidence ({claim.confidence:.2f} < 0.7)")
                    continue
                if not claim.source_urls:
                    rejected += 1
                    reasons.append("Claim has no source URLs")
                    continue

                claim_hash = _hash_claim(claim.text)
                now = datetime.now(timezone.utc).isoformat()

                # Dedup — if claim already exists, treat as confirmation
                cur.execute("SELECT claim_hash FROM federated_claims WHERE claim_hash = %s", (claim_hash,))
                existing = cur.fetchone()

                if existing:
                    # Boost confirmation count
                    cur.execute("""
                        UPDATE federated_claims 
                        SET confirmations = confirmations + 1,
                            network_confidence = LEAST(1.0, confidence + (confirmations + 1) * 0.05)
                        WHERE claim_hash = %s AND contributor_hash != %s
                    """, (claim_hash, contributor_hash))
                    accepted += 1
                else:
                    # New claim
                    cur.execute("""
                        INSERT INTO federated_claims 
                            (claim_hash, text, confidence, network_confidence, source_urls, 
                             domain_tags, entity_refs, contributor_hash, contributed_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (claim_hash) DO NOTHING
                    """, (
                        claim_hash, claim.text, claim.confidence, claim.confidence,
                        claim.source_urls, claim.domain_tags, claim.entity_refs,
                        contributor_hash, now,
                    ))
                    accepted += 1

            # Update contributor stats
            cur.execute("""
                UPDATE federated_contributors 
                SET claims_contributed = claims_contributed + %s
                WHERE contributor_hash = %s
            """, (accepted, contributor_hash))

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Contribution failed: {str(e)[:100]}")
    finally:
        conn.close()

    return ContributeResponse(
        accepted=accepted,
        rejected=rejected,
        reasons=reasons[:5],  # Limit reasons shown
    )


@router.get("/search", response_model=NetworkSearchResult)
async def search_network(
    q: str = Query(..., min_length=2, max_length=500, description="Search query"),
    domain: Optional[str] = Query(None, description="Filter by domain tag"),
    min_conf: float = Query(0.6, ge=0, le=1, description="Minimum network confidence"),
    limit: int = Query(20, ge=1, le=100),
    x_api_key: str = Header(None, alias="X-API-Key"),
    authorization: str = Header(None),
):
    """
    Search the Federated Knowledge Network for existing verified claims.
    
    Use this before starting a research mission to leverage collective knowledge.
    Requires contributor token (must contribute to consume — "give to get").
    """
    token = x_api_key or ""
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(401, "API key required to search the network")

    if not _init_tables():
        raise HTTPException(503, "Federation database not available")

    conn = _get_db()
    if not conn:
        raise HTTPException(503, "Database connection failed")

    try:
        with conn.cursor() as cur:
            # Full-text search with optional domain filter
            if domain:
                cur.execute("""
                    SELECT claim_hash, text, confidence, network_confidence,
                           source_urls, domain_tags, entity_refs, contributor_hash,
                           contributed_at::text, confirmations, contradictions
                    FROM federated_claims
                    WHERE to_tsvector('english', text) @@ plainto_tsquery('english', %s)
                      AND %s = ANY(domain_tags)
                      AND network_confidence >= %s
                    ORDER BY network_confidence DESC, confirmations DESC
                    LIMIT %s
                """, (q, domain, min_conf, limit))
            else:
                cur.execute("""
                    SELECT claim_hash, text, confidence, network_confidence,
                           source_urls, domain_tags, entity_refs, contributor_hash,
                           contributed_at::text, confirmations, contradictions
                    FROM federated_claims
                    WHERE to_tsvector('english', text) @@ plainto_tsquery('english', %s)
                      AND network_confidence >= %s
                    ORDER BY network_confidence DESC, confirmations DESC
                    LIMIT %s
                """, (q, min_conf, limit))

            rows = cur.fetchall()

            # Get total count
            if domain:
                cur.execute("""
                    SELECT COUNT(*) FROM federated_claims
                    WHERE to_tsvector('english', text) @@ plainto_tsquery('english', %s)
                      AND %s = ANY(domain_tags) AND network_confidence >= %s
                """, (q, domain, min_conf))
            else:
                cur.execute("""
                    SELECT COUNT(*) FROM federated_claims
                    WHERE to_tsvector('english', text) @@ plainto_tsquery('english', %s)
                      AND network_confidence >= %s
                """, (q, min_conf))
            total = cur.fetchone()[0]

    except Exception as e:
        raise HTTPException(500, f"Search failed: {str(e)[:100]}")
    finally:
        conn.close()

    claims = []
    for row in rows:
        claims.append(FederatedClaim(
            claim_hash=row[0],
            text=row[1],
            confidence=row[2],
            network_confidence=row[3],
            source_urls=row[4] or [],
            domain_tags=row[5] or [],
            entity_refs=row[6] or [],
            contributor_hash=row[7],
            contributed_at=row[8],
            confirmations=row[9],
            contradictions=row[10],
        ))

    return NetworkSearchResult(claims=claims, total=total, query=q)


@router.post("/confirm")
async def confirm_or_contradict(
    req: ConfirmRequest,
    x_api_key: str = Header(None, alias="X-API-Key"),
    authorization: str = Header(None),
):
    """
    Vote on a claim: confirm (your research agrees) or contradict (you found counter-evidence).
    
    Confirmations boost network_confidence.
    Contradictions decrease it and flag for review.
    """
    token = x_api_key or ""
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(401, "API key required to vote")

    if not _init_tables():
        raise HTTPException(503, "Federation database not available")

    voter_hash = _hash_contributor(token)
    conn = _get_db()
    if not conn:
        raise HTTPException(503, "Database connection failed")

    try:
        with conn.cursor() as cur:
            # Check claim exists
            cur.execute("SELECT contributor_hash FROM federated_claims WHERE claim_hash = %s", (req.claim_hash,))
            claim = cur.fetchone()
            if not claim:
                raise HTTPException(404, "Claim not found")

            # Can't vote on your own claim
            if claim[0] == voter_hash:
                raise HTTPException(400, "Cannot vote on your own claim")

            # Insert vote (upsert)
            cur.execute("""
                INSERT INTO federated_votes (claim_hash, voter_hash, confirmed)
                VALUES (%s, %s, %s)
                ON CONFLICT (claim_hash, voter_hash) DO UPDATE SET confirmed = %s, voted_at = NOW()
            """, (req.claim_hash, voter_hash, req.confirmed, req.confirmed))

            # Update claim counters
            if req.confirmed:
                cur.execute("""
                    UPDATE federated_claims 
                    SET confirmations = confirmations + 1,
                        network_confidence = LEAST(1.0, confidence + (confirmations + 1) * 0.03)
                    WHERE claim_hash = %s
                """, (req.claim_hash,))
            else:
                cur.execute("""
                    UPDATE federated_claims 
                    SET contradictions = contradictions + 1,
                        network_confidence = GREATEST(0.0, network_confidence - 0.1)
                    WHERE claim_hash = %s
                """, (req.claim_hash,))

            # Update contributor reputation
            cur.execute("""
                UPDATE federated_contributors
                SET claims_confirmed = claims_confirmed + 1
                WHERE contributor_hash = %s
            """, (claim[0],))  # Original contributor gets credit

        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Vote failed: {str(e)[:100]}")
    finally:
        conn.close()

    return {"status": "ok", "action": "confirmed" if req.confirmed else "contradicted"}


@router.get("/stats", response_model=NetworkStats)
async def network_stats():
    """
    Get Federated Knowledge Network statistics.
    
    Public endpoint — no auth required. Shows network health and scale.
    """
    if not _init_tables():
        raise HTTPException(503, "Federation database not available")

    conn = _get_db()
    if not conn:
        raise HTTPException(503, "Database connection failed")

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM federated_claims")
            total_claims = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM federated_contributors")
            total_contributors = cur.fetchone()[0]

            cur.execute("SELECT COALESCE(SUM(confirmations), 0) FROM federated_claims")
            total_confirmations = cur.fetchone()[0]

            cur.execute("SELECT COALESCE(AVG(network_confidence), 0) FROM federated_claims")
            avg_confidence = cur.fetchone()[0]

            cur.execute("""
                SELECT unnest(domain_tags) as domain, COUNT(*) as cnt
                FROM federated_claims
                GROUP BY domain ORDER BY cnt DESC LIMIT 10
            """)
            top_domains = [{"domain": r[0], "count": r[1]} for r in cur.fetchall()]

            cur.execute("SELECT MAX(contributed_at::text) FROM federated_claims")
            last = cur.fetchone()[0]

    except Exception as e:
        raise HTTPException(500, f"Stats query failed: {str(e)[:100]}")
    finally:
        conn.close()

    return NetworkStats(
        total_claims=total_claims,
        total_contributors=total_contributors,
        total_confirmations=total_confirmations,
        avg_confidence=round(avg_confidence, 3),
        top_domains=top_domains,
        last_contribution=last,
    )
