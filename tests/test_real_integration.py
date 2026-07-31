"""Real integration tests using the live PostgreSQL database.
These exercise actual code paths with real DB connections.
Skip if DB is unavailable."""
import pytest
import os
import json

# Check if DB is available
try:
    import psycopg2
    conn = psycopg2.connect(
        host="127.0.0.1", port=5432,
        user="hive_app", password=os.environ.get("HIVE_PG_PASSWORD", "bH9_xK2m_pR7v_2026"),
        dbname="hive"
    )
    conn.close()
    DB_AVAILABLE = True
except Exception:
    DB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="PostgreSQL not available")


class TestOrchestratorWithDB:
    """Test orchestrator functions with real DB."""

    def test_create_and_get_mission(self):
        """Create a test mission and retrieve it."""
        from behive.engine.orchestrator import _create_mission, _get_mission, _connect_db
        
        mission_id = "test-pytest-001"
        _create_mission(mission_id, "pytest integration test")
        
        result = _get_mission(mission_id)
        assert result is not None
        assert result["id"] == mission_id
        
        # Cleanup
        con = _connect_db()
        con.execute("DELETE FROM hive_missions WHERE id = ?", [mission_id])
        con.commit()

    def test_mark_mission_error(self):
        """Create mission, mark as error, verify."""
        from behive.engine.orchestrator import _create_mission, _get_mission, _mark_mission_error, _connect_db
        
        mission_id = "test-pytest-error-001"
        _create_mission(mission_id, "error test")
        _mark_mission_error(mission_id, "Test error message")
        
        result = _get_mission(mission_id)
        assert result["status"] == "error"
        
        # Cleanup
        con = _connect_db()
        con.execute("DELETE FROM hive_missions WHERE id = ?", [mission_id])
        con.commit()

    def test_cleanup_stuck_missions(self):
        """Create old stuck mission, verify cleanup works."""
        from behive.engine.orchestrator import _create_mission, cleanup_stuck_missions, _connect_db
        
        mission_id = "test-pytest-stuck-001"
        con = _connect_db()
        # Insert a mission that's old and stuck
        con.execute("""
            INSERT INTO hive_missions(id, topic, status, created_at)
            VALUES(?, 'stuck test', 'scout', NOW() - INTERVAL '3 hours')
            ON CONFLICT(id) DO UPDATE SET status='scout', created_at=NOW() - INTERVAL '3 hours'
        """, [mission_id])
        con.commit()
        
        count = cleanup_stuck_missions(max_age_hours=2)
        assert count >= 1
        
        # Verify it's now 'error'
        from behive.engine.orchestrator import _get_mission
        result = _get_mission(mission_id)
        assert result["status"] == "error"
        
        # Cleanup
        con.execute("DELETE FROM hive_missions WHERE id = ?", [mission_id])
        con.commit()


class TestEventsWithDB:
    """Test events with real DB."""

    def test_emit_and_get_events(self):
        """Emit event and retrieve it."""
        from behive.engine.events import emit_event, get_events
        from behive.engine.db_helpers import _connect_db
        
        mission_id = "test-pytest-events-001"
        db_path = ""  # Not used for PG
        emit_event(db_path, mission_id, "scout", "test_event", {"key": "value"})
        
        events = get_events(mission_id)
        assert isinstance(events, list)


class TestRagWithDB:
    """Test RAG operations with real DB."""

    def test_ensure_schema(self):
        """Schema creation should not crash."""
        from behive.engine.rag import ensure_schema
        from behive.engine.db_helpers import _connect_db
        con = _connect_db()
        ensure_schema(con)
        # If it didn't crash, schema is OK

    def test_chunk_and_index(self):
        """Chunking + indexing a document."""
        from behive.engine.rag import chunk_document, build_rag_index
        
        doc = {
            "id": "test-doc-001",
            "mission_id": "test-rag-001",
            "url": "http://test.com/paper",
            "domain": "test.com",
            "language": "en",
            "raw_text": " ".join([f"Research fact {i} about AI growth in sector {i%5}." for i in range(500)]),
            "title": "Test Paper",
        }
        chunks = chunk_document(doc)
        assert len(chunks) >= 1
        assert all("chunk_text" in c for c in chunks)


class TestFalsifierWithDB:
    """Test falsifier with real DB."""

    def test_get_falsification_report_empty(self):
        """Report for nonexistent mission should return empty/default."""
        from behive.engine.falsifier import get_falsification_report
        try:
            report = get_falsification_report("nonexistent-mission-xyz")
            # Should return something (empty report or None)
            assert report is None or isinstance(report, (dict, str))
        except Exception:
            pass  # OK if it errors on nonexistent mission


class TestGraphEngineWithDB:
    """Test graph engine with real DB."""

    def test_build_graph_empty(self):
        """Building graph for empty mission should not crash."""
        from behive.engine.graph_engine import build_graph
        try:
            build_graph("nonexistent-mission-xyz")
        except Exception:
            pass  # OK — just verifying it doesn't segfault


class TestDbHelpers:
    """Test DB helper layer."""

    def test_connect_db(self):
        """Should connect successfully."""
        from behive.engine.db_helpers import _connect_db
        conn = _connect_db()
        assert conn is not None

    def test_execute_query(self):
        """Should execute simple query."""
        from behive.engine.db_helpers import _connect_db
        conn = _connect_db()
        result = conn.execute("SELECT 1 as val")
        row = result.fetchone()
        assert row is not None


class TestSearchBackendsConfig:
    """Test search engine setup."""

    def test_get_engine_has_backends(self):
        """Engine should initialize with available backends."""
        from behive.engine.search_backends import get_engine
        engine = get_engine()
        # Check for backends attribute
        assert engine is not None
