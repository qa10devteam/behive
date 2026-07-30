"""
Integration tests for BeHive server API.

Requires: running BeHive server (BEHIVE_TEST_URL env, default http://127.0.0.1:8091)
          PostgreSQL (BEHIVE_TEST_DSN or default hive_app credentials)
"""
import os
import sys
import time
import json
import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Skip entire module if server not running
pytestmark = pytest.mark.integration


def _server_available(url: str) -> bool:
    try:
        r = requests.get(f"{url}/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def skip_if_no_server(behive_server_url):
    if not _server_available(behive_server_url):
        pytest.skip("BeHive server not running")


class TestHealthEndpoint:
    """GET /health — server status."""

    def test_health_returns_200(self, behive_server_url):
        r = requests.get(f"{behive_server_url}/health")
        assert r.status_code == 200

    def test_health_has_version(self, behive_server_url):
        data = requests.get(f"{behive_server_url}/health").json()
        assert "version" in data
        assert data["version"] == "0.3.2"

    def test_health_has_metrics(self, behive_server_url):
        data = requests.get(f"{behive_server_url}/health").json()
        assert "missions_total" in data
        assert "claims_total" in data
        assert "avg_quality" in data
        assert isinstance(data["missions_total"], int)
        assert isinstance(data["avg_quality"], (int, float))


class TestResearchEndpoint:
    """POST /research — start a mission."""

    def test_research_accepts_query(self, behive_server_url):
        r = requests.post(f"{behive_server_url}/research", json={
            "query": "Python testing frameworks comparison 2025",
            "depth": 1,
        })
        if r.status_code == 429:
            pytest.skip("Rate limited — too many recent missions")
        assert r.status_code == 200
        data = r.json()
        assert "job_id" in data
        assert data["job_id"].startswith("hive_")

    def test_research_accepts_topic_alias(self, behive_server_url):
        """Backward compat: 'topic' field works same as 'query'."""
        r = requests.post(f"{behive_server_url}/research", json={
            "topic": "BeHive test topic backward compat",
            "depth": 1,
        })
        if r.status_code == 429:
            pytest.skip("Rate limited — too many recent missions")
        assert r.status_code == 200
        data = r.json()
        assert "job_id" in data

    def test_research_rejects_empty(self, behive_server_url):
        """No query or topic → 422."""
        r = requests.post(f"{behive_server_url}/research", json={"depth": 1})
        if r.status_code == 429:
            pytest.skip("Rate limited")
        assert r.status_code == 422

    def test_research_depth_validation(self, behive_server_url):
        """Depth must be 1-5."""
        r = requests.post(f"{behive_server_url}/research", json={
            "query": "test", "depth": 10
        })
        if r.status_code == 429:
            pytest.skip("Rate limited")
        assert r.status_code == 422


class TestStatusEndpoint:
    """GET /research/{id}/status — mission status."""

    def test_status_valid_mission(self, behive_server_url, pg_cursor):
        """Returns status for existing mission."""
        pg_cursor.execute(
            "SELECT id FROM hive_missions ORDER BY created_at DESC LIMIT 1"
        )
        row = pg_cursor.fetchone()
        if not row:
            pytest.skip("No missions in DB")
        
        mission_id = row[0]
        r = requests.get(f"{behive_server_url}/research/{mission_id}/status")
        assert r.status_code == 200
        data = r.json()
        assert "status" in data
        assert data["status"] in ("running", "done", "error", "cancelled")

    def test_status_nonexistent_returns_404(self, behive_server_url):
        """Non-existent mission → 404."""
        r = requests.get(f"{behive_server_url}/research/nonexistent_abc123/status")
        assert r.status_code == 404


class TestClaimsSearch:
    """GET /claims/search — search extracted knowledge."""

    def test_claims_search_returns_results(self, behive_server_url):
        """Search for common term returns claims."""
        r = requests.get(f"{behive_server_url}/claims/search", params={"q": "AI"})
        assert r.status_code == 200
        data = r.json()
        assert "claims" in data or "results" in data

    def test_claims_search_empty_query(self, behive_server_url):
        """Empty search still returns 200 (no crash)."""
        r = requests.get(f"{behive_server_url}/claims/search", params={"q": ""})
        # May return 200 with empty results or 422 — both are acceptable
        assert r.status_code in (200, 422)


class TestReportEndpoint:
    """GET /research/{id}/report — synthesized report."""

    def test_report_for_done_mission(self, behive_server_url, pg_cursor):
        """Done missions have a report."""
        pg_cursor.execute(
            "SELECT id FROM hive_missions WHERE status='done' AND synthesis IS NOT NULL LIMIT 1"
        )
        row = pg_cursor.fetchone()
        if not row:
            pytest.skip("No completed missions with synthesis")
        
        mission_id = row[0]
        r = requests.get(f"{behive_server_url}/research/{mission_id}/report")
        assert r.status_code == 200
        data = r.json()
        assert "report" in data or "synthesis" in data or "content" in data
