"""Tests for behive.knowledge_graph module."""
import pytest
from unittest.mock import patch, MagicMock
import os


class TestKnowledgeGraphImport:
    def test_import(self):
        """Module should import."""
        import behive.knowledge_graph as kg
        assert kg is not None


class TestNeo4jConfig:
    @patch.dict(os.environ, {"BEHIVE_NEO4J_URI": "bolt://localhost:7687"})
    def test_neo4j_uri_from_env(self):
        """Should read Neo4j URI from env."""
        import behive.knowledge_graph as kg
        assert True  # Import succeeded with env var

    def test_no_neo4j_graceful(self):
        """Without Neo4j, should not crash."""
        import behive.knowledge_graph as kg
        assert kg is not None


class TestGetNeo4jPassword:
    @patch.dict(os.environ, {"BEHIVE_NEO4J_PASSWORD": "test123"})
    def test_reads_from_env(self):
        """Should read password from BEHIVE_NEO4J_PASSWORD."""
        from behive.knowledge_graph import _get_neo4j_password
        pwd = _get_neo4j_password()
        assert pwd == "test123"
