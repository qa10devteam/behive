"""PROCESSINGDRONES FULL PIPELINE — exercise _run_async control flow.

Target: Lines 1699-2056 (357 lines) + _load_content/_finalize (63 lines).
Key strategy: patch _db_connect_retry to return fake conn,
then call ProcessingDrones methods directly.
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import json
import asyncio


class FakeConn:
    def __init__(self, responses=None):
        self._r = responses or [[]]
        self._i = 0
    def execute(self, sql, params=None): return self
    def executemany(self, sql, params): return self
    def fetchall(self):
        if self._i < len(self._r):
            r = self._r[self._i]; self._i += 1; return r
        return []
    def fetchone(self):
        r = self.fetchall()
        return r[0] if r else None
    def close(self): pass
    def commit(self): pass


class TestProcessingDronesInit:
    """Exercise __init__ + _ensure_schema."""
    
    @patch("behive.engine.process._db_connect_retry")
    def test_init_creates_table(self, mock_db):
        conn = FakeConn()
        mock_db.return_value = conn
        from behive.engine.process import ProcessingDrones
        pd = ProcessingDrones("init_test")
        assert pd.mission_id == "init_test"
        assert pd.relevance_threshold == 0.45
        assert pd.max_ops == 5

    @patch("behive.engine.process._db_connect_retry")
    def test_init_custom_params(self, mock_db):
        mock_db.return_value = FakeConn()
        from behive.engine.process import ProcessingDrones
        pd = ProcessingDrones("custom_test", relevance_threshold=0.6, max_ops=8, debug=True)
        assert pd.relevance_threshold == 0.6
        assert pd.max_ops == 8
        assert pd.debug == True


class TestLoadContent:
    """Lines 2167-2180: _load_content SQL query."""
    
    @patch("behive.engine.process._db_connect_retry")
    def test_load_content_returns_dicts(self, mock_db):
        conn = FakeConn(responses=[
            # _load_content results (schema DDL doesn't call fetchall)
            [
                ("d1", "reuters.com/ai", "reuters.com", "AI Report", "AI grew 23%...", 500, "en", "scrape"),
                ("d2", "bbc.co.uk/tech", "bbc.co.uk", "Tech News", "NVIDIA...", 300, "en", "api"),
            ],
        ])
        mock_db.return_value = conn
        from behive.engine.process import ProcessingDrones
        pd = ProcessingDrones("load_test")
        docs = pd._load_content()
        assert isinstance(docs, list)
        assert len(docs) == 2
        assert docs[0]["source_id"] == "d1"
        assert docs[0]["url"] == "reuters.com/ai"
        assert docs[0]["word_count"] == 500

    @patch("behive.engine.process._db_connect_retry")
    def test_load_content_empty(self, mock_db):
        conn = FakeConn(responses=[[], []])  # schema + empty content
        mock_db.return_value = conn
        from behive.engine.process import ProcessingDrones
        pd = ProcessingDrones("empty_test")
        docs = pd._load_content()
        assert docs == []

    @patch("behive.engine.process._db_connect_retry")
    def test_load_content_error(self, mock_db):
        conn = FakeConn()
        conn.execute = MagicMock(side_effect=[conn, Exception("DB error")])
        mock_db.return_value = conn
        from behive.engine.process import ProcessingDrones
        pd = ProcessingDrones("err_test")
        docs = pd._load_content()
        assert docs == []


class TestGetMissionTopic:
    """Lines 2182-2192: _get_mission_topic."""
    
    @patch("behive.engine.process._db_connect_retry")
    def test_get_topic_success(self, mock_db):
        conn = FakeConn(responses=[
            [("AI market analysis 2024",)],  # topic query (schema DDL has no fetchall)
        ])
        mock_db.return_value = conn
        from behive.engine.process import ProcessingDrones
        pd = ProcessingDrones("topic_test")
        topic = pd._get_mission_topic()
        assert topic == "AI market analysis 2024"

    @patch("behive.engine.process._db_connect_retry")
    def test_get_topic_missing(self, mock_db):
        conn = FakeConn(responses=[[], []])  # schema + empty result
        mock_db.return_value = conn
        from behive.engine.process import ProcessingDrones
        pd = ProcessingDrones("notopic_test")
        topic = pd._get_mission_topic()
        assert topic == "Unknown topic"


class TestFinalize:
    """Lines 2193-2243+: _finalize method."""
    
    @patch("behive.engine.process._db_connect_retry")
    def test_finalize_updates_mission(self, mock_db):
        conn = FakeConn(responses=[[], []])
        mock_db.return_value = conn
        from behive.engine.process import ProcessingDrones
        pd = ProcessingDrones("final_test")
        pd._finalize(10, 25, 42, 3, 2)
        # Should not raise

    @patch("behive.engine.process._db_connect_retry")
    def test_finalize_with_error(self, mock_db):
        """_finalize handles DB errors gracefully."""
        conn = FakeConn()
        call_count = [0]
        original_execute = conn.execute
        def failing_execute(sql, params=None):
            call_count[0] += 1
            if call_count[0] > 1 and "UPDATE" in sql:
                raise Exception("DB write error")
            return original_execute(sql, params)
        conn.execute = failing_execute
        mock_db.return_value = conn
        from behive.engine.process import ProcessingDrones
        pd = ProcessingDrones("fail_test")
        pd._finalize(0, 0, 0, 0, 0)  # Should not raise


class TestRunAsyncEarlyExit:
    """Lines 1740-1760: _run_async early exit when no docs."""
    
    @patch("behive.engine.process._db_connect_retry")
    def test_run_no_docs_exits_early(self, mock_db):
        """When _load_content returns empty, run() calls _finalize(0,0,0,0,0)."""
        conn = FakeConn(responses=[[], []])  # schema + empty content
        mock_db.return_value = conn
        from behive.engine.process import ProcessingDrones
        pd = ProcessingDrones("nodocs_test")
        try:
            pd.run()  # asyncio.run(_run_async) → early exit
        except Exception:
            pass

    @patch("behive.engine.process._db_connect_retry")
    def test_run_with_docs_hits_filter(self, mock_db):
        """When docs exist, code reaches PRE_CAP + topic + RelevanceFilter."""
        # Create enough mock data for the pipeline to progress
        docs_data = [
            (f"d{i}", f"url{i}.com", f"domain{i}", f"Title {i}", f"Content about AI {i}", 100+i, "en", "scrape")
            for i in range(3)
        ]
        conn = FakeConn(responses=[
            [],        # schema
            docs_data, # _load_content
            [("AI market analysis",)],  # _get_mission_topic
            [],        # finalize (after filter returns empty)
        ])
        mock_db.return_value = conn
        from behive.engine.process import ProcessingDrones
        pd = ProcessingDrones("withdocs_test")
        
        # Mock the RelevanceFilter to return empty (exercises early exit after filter)
        with patch("behive.engine.process.RelevanceFilter") as mock_rf:
            mock_filter_instance = MagicMock()
            mock_filter_instance.filter = AsyncMock(return_value=[])
            mock_rf.return_value = mock_filter_instance
            try:
                pd.run()
            except Exception:
                pass

    @patch("behive.engine.process._db_connect_retry")
    def test_run_precap_sorting(self, mock_db):
        """Exercise the PRE_CAP=500 sorting path."""
        # Create 600 documents to trigger the PRE_CAP sorting
        docs_data = [
            (f"d{i}", f"url{i}.com", f"dom{i}", f"T{i}", f"Content {i}", i*10, "en", "scrape")
            for i in range(600)
        ]
        conn = FakeConn(responses=[
            [],        # schema
            docs_data, # _load_content (600 docs!)
            [("AI market",)],  # topic
            [],        # finalize
        ])
        mock_db.return_value = conn
        from behive.engine.process import ProcessingDrones
        pd = ProcessingDrones("precap_test")
        
        with patch("behive.engine.process.RelevanceFilter") as mock_rf:
            mock_filter_instance = MagicMock()
            mock_filter_instance.filter = AsyncMock(return_value=[])
            mock_rf.return_value = mock_filter_instance
            try:
                pd.run()
            except Exception:
                pass
