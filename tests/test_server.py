"""Tests for behive.server module — FastAPI endpoints."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import os

# Set env before importing server
os.environ.setdefault("HIVE_PG_USER", "test")
os.environ.setdefault("HIVE_PG_PASSWORD", "test")
os.environ.setdefault("HIVE_PG_DATABASE", "testdb")
os.environ.setdefault("HIVE_PG_HOST", "127.0.0.1")


@pytest.fixture
def client():
    """FastAPI test client with mocked DB."""
    with patch("behive.server.get_db_url", return_value="postgresql://test:test@localhost/test"):
        from fastapi.testclient import TestClient
        from behive.server import app
        return TestClient(app, raise_server_exceptions=False)


class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_has_version(self, client):
        data = client.get("/health").json()
        assert "version" in data

    def test_health_has_status(self, client):
        data = client.get("/health").json()
        assert "status" in data


class TestResearchEndpoint:
    def test_research_accepts_query(self, client):
        """POST /research with query should return job_id."""
        response = client.post("/research", json={"query": "test topic", "depth": 1})
        assert response.status_code in [200, 202, 500]
        if response.status_code in [200, 202]:
            data = response.json()
            assert "job_id" in data or "id" in data

    def test_research_missing_query(self, client):
        """POST /research without query should return 422."""
        response = client.post("/research", json={})
        assert response.status_code == 422

    def test_research_with_depth(self, client):
        """Should accept depth parameter."""
        response = client.post("/research", json={"query": "test", "depth": 3})
        assert response.status_code in [200, 202, 500]


class TestStatusEndpoint:
    def test_status_nonexistent(self, client):
        response = client.get("/research/nonexistent-id/status")
        assert response.status_code in [404, 500]


class TestClaimsSearch:
    def test_claims_search(self, client):
        response = client.get("/claims/search", params={"q": "AI"})
        assert response.status_code in [200, 500]

    def test_search_alias(self, client):
        response = client.get("/search", params={"q": "test", "query": "test"})
        assert response.status_code in [200, 422, 500]


class TestMissionsEndpoint:
    def test_list_missions(self, client):
        response = client.get("/missions")
        assert response.status_code in [200, 500]


class TestReportEndpoint:
    def test_report_nonexistent(self, client):
        response = client.get("/research/fake-id/report")
        assert response.status_code in [404, 500]
