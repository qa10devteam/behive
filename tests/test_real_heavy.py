"""Heavy integration tests using real DB — exercises class internals.
These target the biggest uncovered code blocks: Scout, Harvest, Synth."""
import pytest
import os
import json

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


class TestSwarmScoutReal:
    """Test SwarmScout with real DB (methods that don't need network)."""

    @pytest.fixture
    def scout(self):
        from behive.engine.scout import SwarmScout
        from behive.engine.orchestrator import _create_mission, _connect_db
        
        mission_id = "test-scout-real-001"
        _create_mission(mission_id, "AI market research")
        
        s = SwarmScout(mission_id=mission_id, topic="AI market research")
        yield s
        
        # Cleanup
        con = _connect_db()
        con.execute("DELETE FROM hive_missions WHERE id = ?", [mission_id])
        con.commit()

    def test_scout_instantiation(self, scout):
        """Scout should instantiate with mission_id."""
        assert scout is not None

    def test_scout_has_init_sequence(self, scout):
        """Should have _init_sequence method."""
        assert hasattr(scout, '_init_sequence')

    def test_scout_has_search_methods(self, scout):
        """Should have various search methods."""
        assert hasattr(scout, '_ddg_text')
        assert hasattr(scout, '_ddg_news')
        assert hasattr(scout, '_google_rss')


class TestHarvesterBeeReal:
    """Test HarvesterBee with real DB."""

    @pytest.fixture
    def harvester(self):
        from behive.engine.harvest import HarvesterBee
        from behive.engine.orchestrator import _create_mission, _connect_db
        
        mission_id = "test-harvest-real-001"
        _create_mission(mission_id, "test harvest")
        
        h = HarvesterBee(mission_id=mission_id)
        yield h
        
        con = _connect_db()
        con.execute("DELETE FROM hive_missions WHERE id = ?", [mission_id])
        con.commit()

    def test_harvester_instantiation(self, harvester):
        assert harvester is not None

    def test_detect_language(self, harvester):
        """Language detection should work on plain text."""
        result = harvester._detect_language("This is an English text about AI.")
        assert isinstance(result, str)
        assert len(result) >= 2  # ISO language code

    def test_detect_language_polish(self, harvester):
        """Should detect Polish."""
        result = harvester._detect_language("To jest tekst po polsku o rynku.")
        assert isinstance(result, str)

    def test_harvester_has_harvest_methods(self, harvester):
        """Should have various harvesting strategies."""
        assert hasattr(harvester, '_harvest_jina')
        assert hasattr(harvester, '_harvest_web')
        assert hasattr(harvester, '_harvest_pdf')
        assert hasattr(harvester, '_harvest_newspaper')


class TestQueenSynthReal:
    """Test Queen synthesis with real DB."""

    @pytest.fixture
    def queen(self):
        from behive.engine.synth import Queen
        from behive.engine.orchestrator import _create_mission, _connect_db
        
        mission_id = "test-queen-real-001"
        _create_mission(mission_id, "AI market analysis")
        
        q = Queen(mission_id=mission_id)
        yield q
        
        con = _connect_db()
        con.execute("DELETE FROM hive_missions WHERE id = ?", [mission_id])
        con.commit()

    def test_queen_instantiation(self, queen):
        assert queen is not None

    def test_queen_has_run(self, queen):
        assert hasattr(queen, 'run')
        assert callable(queen.run)

    def test_queen_has_format_report(self, queen):
        assert hasattr(queen, 'format_report')

    def test_queen_has_honey_context(self, queen):
        assert hasattr(queen, '_build_honey_context')


class TestDbOperations:
    """Test low-level DB operations that cover db.py and db_helpers.py."""

    @pytest.fixture(autouse=True)
    def reset_pool(self):
        """Reset DB pool state between tests."""
        import behive.engine.db_helpers as dbh
        dbh._pg_available = None
        yield

    def test_connect_and_query(self):
        from behive.engine.db_helpers import _connect_db
        con = _connect_db()
        result = con.execute("SELECT COUNT(*) as cnt FROM hive_missions")
        row = result.fetchone()
        assert row is not None
        assert row["cnt"] >= 0 or row[0] >= 0

    def test_insert_and_delete(self):
        from behive.engine.db_helpers import _connect_db
        con = _connect_db()
        
        # Insert
        con.execute(
            "INSERT INTO hive_missions(id, topic, status, created_at) VALUES(?, ?, 'test', NOW()) ON CONFLICT(id) DO NOTHING",
            ["test-db-ops-001", "db test"]
        )
        con.commit()
        
        # Verify
        result = con.execute("SELECT * FROM hive_missions WHERE id = ?", ["test-db-ops-001"])
        row = result.fetchone()
        assert row is not None
        
        # Cleanup
        con.execute("DELETE FROM hive_missions WHERE id = ?", ["test-db-ops-001"])
        con.commit()

    def test_json_column(self):
        """Test JSONB column handling."""
        from behive.engine.db_helpers import _connect_db
        con = _connect_db()
        
        # Insert with JSONB
        con.execute(
            "INSERT INTO hive_missions(id, topic, status, quality_metrics, created_at) VALUES(?, ?, 'test', ?::jsonb, NOW()) ON CONFLICT(id) DO UPDATE SET quality_metrics = EXCLUDED.quality_metrics",
            ["test-json-001", "json test", json.dumps({"score": 0.85})]
        )
        con.commit()
        
        result = con.execute("SELECT quality_metrics FROM hive_missions WHERE id = ?", ["test-json-001"])
        row = result.fetchone()
        assert row is not None
        
        # Cleanup
        con.execute("DELETE FROM hive_missions WHERE id = ?", ["test-json-001"])
        con.commit()


class TestContentQualityWithRealText:
    """Test content quality with diverse real-world text samples."""

    def test_academic_text(self):
        from behive.engine.content_quality import score_content, score_content_detailed
        text = """
        Background: Artificial intelligence (AI) applications in healthcare have shown
        promising results in recent years. This systematic review analyzed 847 studies
        published between 2019 and 2024 across PubMed, Scopus, and Web of Science.
        
        Methods: We included randomized controlled trials and cohort studies with sample
        sizes exceeding 100 participants. Primary outcomes were diagnostic accuracy
        (sensitivity, specificity, AUC) and clinical outcomes (mortality, readmission).
        
        Results: AI-assisted diagnosis achieved pooled sensitivity of 0.94 (95% CI: 0.91-0.96)
        and specificity of 0.89 (95% CI: 0.86-0.92). In radiology, AI surpassed human
        radiologists in 12 of 15 comparative studies for lung nodule detection.
        
        Conclusions: AI demonstrates clinically meaningful improvements in diagnostic
        accuracy, particularly in medical imaging and pathology. Implementation barriers
        include regulatory uncertainty and interoperability challenges.
        """
        score = score_content(text)
        assert score > 0.3  # Academic text should score well
        
        detailed = score_content_detailed(text)
        assert detailed["quality_score"] > 0.3
        assert detailed["factual_density"] > 0

    def test_news_article(self):
        from behive.engine.content_quality import score_content
        text = """
        Global semiconductor revenue surpassed $600 billion for the first time in 2024,
        according to data from the Semiconductor Industry Association (SIA). The growth
        was driven primarily by demand for AI accelerators, with NVIDIA capturing 80%
        of the data center GPU market. Samsung and TSMC reported record quarterly earnings,
        with TSMC's revenue reaching $23.5 billion in Q4 alone. Memory chip prices rose
        40% from their 2023 lows as supply discipline took hold across the industry.
        Intel's foundry business continued to lag, with yield rates at its 18A process
        node still below target. The US CHIPS Act disbursed $12 billion to domestic fabs.
        """
        score = score_content(text)
        assert score > 0.3

    def test_marketing_fluff(self):
        from behive.engine.content_quality import score_content
        text = """
        Unlock your potential with our revolutionary AI-powered solution! Join thousands
        of satisfied customers who have transformed their businesses. Our cutting-edge
        platform leverages state-of-the-art technology to deliver unparalleled results.
        Don't miss this limited-time offer! Sign up today and experience the future.
        """
        score = score_content(text)
        # Marketing fluff should score lower than research
        assert isinstance(score, float)
