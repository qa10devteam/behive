"""
E2E Pipeline tests — full research mission lifecycle.

Launches a real mission, waits for completion, validates output quality.
Slow (5-15 min per test). Requires: running server + LLM + search backends.

Run: pytest tests/test_e2e_pipeline.py -v --timeout=900
"""
import os
import sys
import time
import json
import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytestmark = pytest.mark.e2e

TIMEOUT_SECONDS = 600  # 10 min max per mission


def _server_available(url: str) -> bool:
    try:
        return requests.get(f"{url}/health", timeout=3).status_code == 200
    except Exception:
        return False


def _wait_for_mission(url: str, job_id: str, timeout: int = TIMEOUT_SECONDS) -> dict:
    """Poll mission until done/error or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{url}/research/{job_id}/status")
        if r.status_code != 200:
            time.sleep(5)
            continue
        data = r.json()
        status = data.get("status", "")
        if status in ("done", "error", "cancelled"):
            return data
        time.sleep(10)
    return {"status": "timeout", "elapsed": time.time() - start}


@pytest.fixture(scope="module", autouse=True)
def skip_if_no_server(behive_server_url):
    if not _server_available(behive_server_url):
        pytest.skip("BeHive server not running")


class TestFullPipeline:
    """Complete research mission: start → scout → harvest → process → synth → done."""

    def test_simple_topic_completes(self, behive_server_url):
        """A straightforward topic completes successfully."""
        r = requests.post(f"{behive_server_url}/research", json={
            "query": "Python 3.12 new features overview",
            "depth": 1,
        })
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        
        result = _wait_for_mission(behive_server_url, job_id)
        assert result["status"] == "done", f"Mission failed: {result}"

    def test_mission_produces_claims(self, behive_server_url, pg_cursor):
        """Completed mission has claims in the database."""
        r = requests.post(f"{behive_server_url}/research", json={
            "query": "PostgreSQL 17 new features performance",
            "depth": 2,
        })
        job_id = r.json()["job_id"]
        
        result = _wait_for_mission(behive_server_url, job_id)
        assert result["status"] == "done", f"Mission failed: {result}"
        
        # Verify claims exist
        pg_cursor.execute(
            "SELECT COUNT(*) FROM hive_claims WHERE mission_id = %s", (job_id,)
        )
        count = pg_cursor.fetchone()[0]
        assert count > 0, f"Mission {job_id} produced 0 claims"

    def test_mission_quality_above_threshold(self, behive_server_url, pg_cursor):
        """Claims quality should average ≥ 0.65."""
        r = requests.post(f"{behive_server_url}/research", json={
            "query": "React Server Components architecture benefits 2025",
            "depth": 2,
        })
        job_id = r.json()["job_id"]
        
        result = _wait_for_mission(behive_server_url, job_id)
        if result["status"] != "done":
            pytest.skip(f"Mission didn't complete: {result['status']}")
        
        pg_cursor.execute("""
            SELECT AVG(quality_score) FROM hive_claims 
            WHERE mission_id = %s AND quality_score > 0
        """, (job_id,))
        avg_q = pg_cursor.fetchone()[0]
        
        if avg_q is not None:
            assert avg_q >= 0.65, f"Quality {avg_q:.3f} below threshold 0.65"

    def test_mission_has_sources(self, behive_server_url, pg_cursor):
        """Scout phase discovers sources."""
        r = requests.post(f"{behive_server_url}/research", json={
            "query": "Docker container security best practices",
            "depth": 1,
        })
        job_id = r.json()["job_id"]
        
        result = _wait_for_mission(behive_server_url, job_id)
        assert result["status"] == "done", f"Mission failed: {result}"
        
        pg_cursor.execute(
            "SELECT COUNT(*) FROM hive_sources WHERE mission_id = %s", (job_id,)
        )
        count = pg_cursor.fetchone()[0]
        assert count >= 5, f"Only {count} sources found (expected ≥5)"

    def test_mission_timeout_respected(self, behive_server_url):
        """Mission doesn't hang forever — completes or errors within timeout."""
        r = requests.post(f"{behive_server_url}/research", json={
            "query": "Obscure niche topic that might fail gracefully XYZ123",
            "depth": 1,
        })
        job_id = r.json()["job_id"]
        
        start = time.time()
        result = _wait_for_mission(behive_server_url, job_id, timeout=TIMEOUT_SECONDS)
        elapsed = time.time() - start
        
        # Must complete (done or error) — NOT timeout
        assert result["status"] != "timeout", \
            f"Mission hung for {elapsed:.0f}s without resolving"


class TestMissionResilience:
    """Tests that the pipeline handles edge cases gracefully."""

    def test_unicode_topic(self, behive_server_url):
        """Unicode/non-ASCII topic doesn't crash."""
        r = requests.post(f"{behive_server_url}/research", json={
            "query": "人工智能 AI 发展趋势 2025",
            "depth": 1,
        })
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        
        result = _wait_for_mission(behive_server_url, job_id)
        # May succeed or error, but must NOT hang
        assert result["status"] in ("done", "error")

    def test_very_long_topic(self, behive_server_url):
        """Very long topic string doesn't crash."""
        long_topic = "artificial intelligence " * 50  # ~1200 chars
        r = requests.post(f"{behive_server_url}/research", json={
            "query": long_topic[:500],
            "depth": 1,
        })
        assert r.status_code == 200

    def test_concurrent_missions(self, behive_server_url):
        """Multiple missions can run concurrently without deadlock."""
        jobs = []
        for topic in ["Rust async runtime", "Go generics performance"]:
            r = requests.post(f"{behive_server_url}/research", json={
                "query": topic, "depth": 1
            })
            assert r.status_code == 200
            jobs.append(r.json()["job_id"])
            time.sleep(2)
        
        # Both should resolve (not deadlock)
        for job_id in jobs:
            result = _wait_for_mission(behive_server_url, job_id)
            assert result["status"] in ("done", "error"), \
                f"Mission {job_id} deadlocked: {result}"
