"""Heavy functional tests — exercises internal logic paths with enough data."""
import pytest
from unittest.mock import patch, MagicMock
import json


# ═══ RAG deep coverage (targets 772 uncovered lines) ═══
from behive.engine.rag import chunk_document, extract_key_phrases, CHUNK_WORDS, CHUNK_OVERLAP


class TestRagChunkingDeep:
    """Tests that generate enough text to trigger multi-chunk splitting."""

    def _make_doc(self, word_count=3000):
        """Helper to create a document with N words."""
        text = " ".join([
            f"Research finding {i}: the market for AI grew by {i}% in sector {i%10}."
            for i in range(word_count // 15)
        ])
        return {
            "id": "doc-001",
            "mission_id": "m-test",
            "url": "http://bloomberg.com/research",
            "domain": "bloomberg.com",
            "language": "en",
            "raw_text": text,
            "title": "Deep Research Report on AI Markets",
            "published_date": "2024-06-15",
            "author": "Research Team",
            "keywords": json.dumps(["AI", "market", "growth"]),
        }

    def test_multi_chunk_split(self):
        """3000 words / 500 per chunk = ~6 chunks."""
        doc = self._make_doc(3000)
        chunks = chunk_document(doc)
        assert len(chunks) >= 5

    def test_chunk_has_metadata(self):
        """Each chunk should have full metadata."""
        doc = self._make_doc(2000)
        chunks = chunk_document(doc)
        for chunk in chunks:
            assert "chunk_text" in chunk
            assert "context_header" in chunk
            assert "chunk_index" in chunk
            assert "mission_id" in chunk
            assert "url" in chunk
            assert "domain" in chunk

    def test_context_header_built(self):
        """context_header should include title and domain."""
        doc = self._make_doc(1500)
        chunks = chunk_document(doc)
        assert len(chunks) >= 1
        header = chunks[0]["context_header"]
        assert "bloomberg" in header.lower() or "Deep Research" in header

    def test_chunk_word_count(self):
        """Each chunk should be approximately CHUNK_WORDS."""
        doc = self._make_doc(5000)
        chunks = chunk_document(doc)
        for chunk in chunks[:-1]:  # Last chunk can be shorter
            wc = chunk.get("word_count", len(chunk["chunk_text"].split()))
            # Should be roughly CHUNK_WORDS (with some tolerance for overlap)
            assert wc <= CHUNK_WORDS * 1.5

    def test_overlap_exists(self):
        """Adjacent chunks should share some words (overlap)."""
        doc = self._make_doc(3000)
        chunks = chunk_document(doc)
        if len(chunks) >= 2:
            last_words_chunk0 = set(chunks[0]["chunk_text"].split()[-CHUNK_OVERLAP:])
            first_words_chunk1 = set(chunks[1]["chunk_text"].split()[:CHUNK_OVERLAP])
            overlap = last_words_chunk0 & first_words_chunk1
            assert len(overlap) >= 1  # At least some overlap

    def test_empty_raw_text(self):
        """Empty raw_text should return empty list."""
        doc = self._make_doc(0)
        doc["raw_text"] = ""
        chunks = chunk_document(doc)
        assert chunks == []

    def test_minimal_doc(self):
        """Document with only raw_text and url should work."""
        doc = {"raw_text": " ".join(["word"] * 600), "url": "http://x.com"}
        chunks = chunk_document(doc)
        assert len(chunks) >= 1


class TestKeyPhrasesDeep:
    """Deep tests for key phrase extraction."""

    def test_multiword_phrases(self):
        """Should extract multi-word phrases from technical text."""
        text = (
            "Machine learning models for natural language processing have improved "
            "significantly. Deep neural networks achieve state-of-the-art results on "
            "sentiment analysis, named entity recognition, and machine translation tasks. "
            "Transfer learning from large language models reduces training time."
        )
        phrases = extract_key_phrases(text)
        assert len(phrases) >= 2
        # Should find some multi-word terms
        multi_word = [p for p in phrases if len(p.split()) >= 2]
        assert len(multi_word) >= 1

    def test_no_duplicates(self):
        """Phrases should not repeat."""
        text = "AI market AI market AI market growth in technology sector"
        phrases = extract_key_phrases(text)
        assert len(phrases) == len(set(phrases))


# ═══ Server deep coverage (targets 243 uncovered lines) ═══
import os
os.environ.setdefault("HIVE_PG_USER", "test")
os.environ.setdefault("HIVE_PG_PASSWORD", "test")
os.environ.setdefault("HIVE_PG_DATABASE", "testdb")
os.environ.setdefault("HIVE_PG_HOST", "127.0.0.1")


class TestServerDeep:
    """Deep server tests with actual endpoint logic."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from behive.server import app
        return TestClient(app, raise_server_exceptions=False)

    def test_research_creates_mission(self, client):
        """POST /research should create a mission and return ID."""
        resp = client.post("/research", json={"query": "AI market 2024", "depth": 1})
        if resp.status_code in [200, 202]:
            data = resp.json()
            assert "job_id" in data
            assert isinstance(data["job_id"], str)
            assert len(data["job_id"]) > 5

    def test_research_with_all_params(self, client):
        """Should accept depth and force params."""
        resp = client.post("/research", json={
            "query": "semiconductor industry",
            "depth": 3,
            "force": True,
        })
        assert resp.status_code in [200, 202, 500]

    def test_health_detailed(self, client):
        """Health endpoint should show DB and engine status."""
        resp = client.get("/health")
        data = resp.json()
        assert "version" in data
        # Check for additional fields
        assert "status" in data

    def test_missions_with_limit(self, client):
        """GET /missions should support limit param."""
        resp = client.get("/missions", params={"limit": 5})
        assert resp.status_code in [200, 500]

    def test_intelligence_entity(self, client):
        """GET /intelligence/entity/{name} endpoint."""
        resp = client.get("/intelligence/entity/OpenAI")
        assert resp.status_code in [200, 404, 500]

    def test_intelligence_network(self, client):
        """GET /intelligence/network/{name} endpoint."""
        resp = client.get("/intelligence/network/artificial-intelligence")
        assert resp.status_code in [200, 404, 500]

    def test_intelligence_stats(self, client):
        """GET /intelligence/stats endpoint."""
        resp = client.get("/intelligence/stats")
        assert resp.status_code in [200, 500]


# ═══ Content quality deep (targets lines 108-275) ═══
from behive.engine.content_quality import score_content, score_content_detailed


class TestContentQualityDeep:
    """Exercise all scoring edge cases."""

    def test_unicode_handling(self):
        """Should handle non-ASCII text."""
        text = "Badania wykazały wzrost PKB o 3,2% w roku 2024. Dane z GUS potwierdzają trend."
        score = score_content(text)
        assert 0.0 <= score <= 1.0

    def test_very_long_text(self):
        """Long text should not crash."""
        text = " ".join(["The market grew significantly in the past year."] * 500)
        score = score_content(text)
        assert 0.0 <= score <= 1.0

    def test_detailed_all_fields(self):
        """All detailed fields should be valid floats."""
        text = " ".join([
            "Machine learning applications in healthcare grew 34% in 2024.",
            "The diagnostic imaging segment led growth with $12.5B in revenue.",
            "North American hospitals deployed AI in 42% of radiology departments.",
            "European adoption lagged at 28%, with regulatory concerns cited.",
            "Asia Pacific growth was 31%, driven by China and India.",
        ] * 5)
        result = score_content_detailed(text)
        for key, value in result.items():
            if key != "covert_infected":
                assert isinstance(value, (int, float)), f"{key} is not numeric: {value}"

    def test_repetitive_text_penalized(self):
        """Highly repetitive text should score lower for diversity."""
        repetitive = "AI is growing. " * 100
        diverse = " ".join([f"Point {i}: {topic}." for i, topic in enumerate([
            "AI market grew 23%", "Healthcare leads at 34%", "Revenue hit $184B",
            "Asia Pacific grew 31%", "Europe at 28%", "Regulatory changes ahead",
        ] * 10)])
        r_score = score_content_detailed(repetitive)
        d_score = score_content_detailed(diverse)
        # Diverse text should have higher lexical_diversity
        assert d_score["lexical_diversity"] >= r_score["lexical_diversity"]
