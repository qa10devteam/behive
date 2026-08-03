"""BeHive client — programmatic research interface."""

import asyncio
import os
import time
from typing import Optional

from behive.models import ResearchResult, Claim, Entity


class BeHiveClient:
    """
    Client for running research missions.
    
    Supports three backends:
    1. Local (direct DB access) — for self-hosted installations
    2. API (remote BeHive server) — for cloud/team usage
    3. MCP (Model Context Protocol) — for AI agent integration
    
    Usage:
        client = BeHiveClient()  # auto-detects backend
        result = await client.research("topic", depth=3)
    """
    
    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        db_url: Optional[str] = None,
    ):
        self.api_url = api_url or os.environ.get("BEHIVE_API_URL")
        self.api_key = api_key or os.environ.get("BEHIVE_API_KEY")
        self.db_url = db_url or os.environ.get("BEHIVE_DB_URL")
        self._backend = self._detect_backend()
    
    def _detect_backend(self) -> str:
        if self.api_url:
            return "api"
        # Try PostgreSQL
        try:
            import psycopg2
            url = self.db_url or os.environ.get("DATABASE_URL") or \
                  f"postgresql://{os.environ.get('HIVE_PG_USER', 'behive')}:" \
                  f"{os.environ.get('HIVE_PG_PASSWORD', 'behive')}@" \
                  f"{os.environ.get('HIVE_PG_HOST', 'localhost')}:" \
                  f"{os.environ.get('HIVE_PG_PORT', '5432')}/" \
                  f"{os.environ.get('HIVE_PG_DATABASE', 'behive')}"
            psycopg2.connect(url).close()
            self.db_url = url
            return "local"
        except Exception:
            pass
        # Try DuckDB (zero-setup fallback)
        try:
            from behive.engine.db_duckdb import is_available, get_db_path
            if is_available():
                os.environ["BEHIVE_BACKEND"] = "duckdb"
                return "local"
        except ImportError:
            pass
        # No backend available — give clear error
        if not self.api_url:
            raise RuntimeError(
                "No backend configured. Options:\n"
                "  • behive research 'topic' --no-db  (uses DuckDB, zero setup)\n"
                "  • BEHIVE_API_URL=http://localhost:8091  (connect to running server)\n"
                "  • DATABASE_URL=postgresql://...          (direct PostgreSQL)\n\n"
                "Quick start:\n"
                "  pip install behive && behive research 'your topic' --no-db"
            )
        return "api"
    
    async def research(
        self,
        topic: str,
        depth: int = 3,
        scale: Optional[int] = None,
        wait: bool = True,
        timeout: int = 600,
    ) -> ResearchResult:
        """
        Run a research mission.
        
        Args:
            topic: Research question or topic
            depth: 1-5 (1=quick scout, 3=standard, 5=deep with falsification)
            scale: Override task count (30-300). Auto-calculated from depth if None.
            wait: Block until done (True) or return immediately (False)
            timeout: Max seconds to wait (default 600s = 10min)
        
        Returns:
            ResearchResult with claims, entities, and synthesized report
        """
        if self._backend == "local":
            return await self._research_local(topic, depth, scale, wait, timeout)
        else:
            return await self._research_api(topic, depth, scale, wait, timeout)
    
    async def _research_local(
        self, topic: str, depth: int, scale: Optional[int], 
        wait: bool, timeout: int
    ) -> ResearchResult:
        """Run research using local pipeline (direct DB access)."""
        import sys
        sys.path.insert(0, os.environ.get("BEHIVE_PATH", os.path.dirname(__file__)))
        
        from hive2 import cmd_run
        
        # Map depth to scale
        scale_map = {1: 30, 2: 70, 3: 150, 4: 200, 5: 300}
        actual_scale = scale or scale_map.get(depth, 150)
        
        start = time.time()
        
        # Run pipeline
        try:
            mission_id = await asyncio.to_thread(
                cmd_run, topic, think=False, deep=False, force=True, scale=actual_scale
            )
        except Exception as e:
            return ResearchResult(
                topic=topic,
                mission_id="",
                status="error",
                error=str(e),
            )
        
        duration = time.time() - start
        
        # Load results from DB
        return await self._load_results(topic, mission_id, duration)
    
    async def _research_api(
        self, topic: str, depth: int, scale: Optional[int],
        wait: bool, timeout: int
    ) -> ResearchResult:
        """Run research via remote API."""
        import httpx
        
        url = f"{self.api_url}/research"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json={
                "query": topic,
                "depth": depth,
                "scale": scale,
            }, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
            mission_id = data.get("mission_id", "")
            
            if wait and data.get("status") != "done":
                # Poll until done
                start = time.time()
                while time.time() - start < timeout:
                    await asyncio.sleep(5)
                    status_resp = await client.get(
                        f"{self.api_url}/research/{mission_id}/status",
                        headers=headers,
                    )
                    status_data = status_resp.json()
                    if status_data.get("status") in ("done", "error"):
                        break
                
                # Fetch final results
                result_resp = await client.get(
                    f"{self.api_url}/research/{mission_id}",
                    headers=headers,
                )
                data = result_resp.json()
            
            return self._parse_api_result(topic, data)
    
    async def _load_results(self, topic: str, mission_id: str, duration: float) -> ResearchResult:
        """Load results from PostgreSQL."""
        import psycopg2
        
        conn = psycopg2.connect(self.db_url)
        cur = conn.cursor()
        
        # Load claims
        cur.execute("""
            SELECT claim, confidence, quality_score, source_url, claim_type
            FROM hive_claims 
            WHERE mission_id = %s AND (is_garbage = false OR is_garbage IS NULL)
            ORDER BY quality_score DESC
        """, (mission_id,))
        
        claims = [
            Claim(
                text=row[0],
                confidence=row[1] or 0.7,
                quality_score=row[2] or 0.0,
                source_url=row[3] or "",
                claim_type=row[4] or "intelligence",
            )
            for row in cur.fetchall()
        ]
        
        # Load entities
        cur.execute("""
            SELECT name, type, confidence 
            FROM hive_entities 
            WHERE mission_id = %s
            ORDER BY confidence DESC
            LIMIT 200
        """, (mission_id,))
        
        entities = [
            Entity(name=row[0], entity_type=row[1], confidence=row[2] or 0.8)
            for row in cur.fetchall()
        ]
        
        # Load report (from missions table if exists)
        report = ""
        try:
            cur.execute("""
                SELECT report FROM hive_missions WHERE mission_id = %s
            """, (mission_id,))
            row = cur.fetchone()
            if row and row[0]:
                report = row[0]
        except Exception:
            conn.rollback()
        
        conn.close()
        
        avg_q = sum(c.quality_score for c in claims) / max(1, len(claims))
        
        return ResearchResult(
            topic=topic,
            mission_id=mission_id,
            claims=claims,
            entities=entities,
            report=report,
            sources_processed=0,
            duration_seconds=duration,
            avg_quality=avg_q,
            status="done",
        )
    
    def _parse_api_result(self, topic: str, data: dict) -> ResearchResult:
        """Parse API response into ResearchResult."""
        claims = [
            Claim(
                text=c["text"],
                confidence=c.get("confidence", 0.7),
                quality_score=c.get("quality_score", 0.0),
                source_url=c.get("source_url", ""),
                claim_type=c.get("type", "intelligence"),
            )
            for c in data.get("claims", [])
        ]
        
        entities = [
            Entity(name=e["name"], entity_type=e.get("type", "unknown"))
            for e in data.get("entities", [])
        ]
        
        return ResearchResult(
            topic=topic,
            mission_id=data.get("mission_id", ""),
            claims=claims,
            entities=entities,
            report=data.get("report", ""),
            sources_processed=data.get("metrics", {}).get("sources_processed", 0),
            duration_seconds=data.get("metrics", {}).get("duration_seconds", 0),
            avg_quality=data.get("metrics", {}).get("avg_quality", 0),
            status=data.get("status", "done"),
            error=data.get("error"),
        )


# ─── Convenience function ────────────────────────────────────────────────────

async def research(topic: str, depth: int = 3, **kwargs) -> ResearchResult:
    """
    Quick research function — auto-detects backend.
    
    Usage:
        from behive import research
        result = await research("NVIDIA GPU market 2025")
        for claim in result.excellent_claims:
            print(claim)
    """
    client = BeHiveClient()
    return await client.research(topic, depth=depth, **kwargs)
