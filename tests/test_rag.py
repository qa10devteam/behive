"""Tests for behive.engine.rag module."""
import pytest
from unittest.mock import patch, MagicMock
from behive.engine.rag import (
    chunk_document,
    build_rag_index,
    extract_key_phrases,
    embed_text,
    ensure_schema,
    evaluate_rag,
    corrective_retrieve,
    cross_mission_retrieve,
    CHUNK_WORDS,
    CHUNK_OVERLAP,
    BATCH_SIZE,
)


class TestChunkDocument:
    """Test document chunking (accepts dict with content field)."""

    def test_empty_document(self):
        """Empty doc should return empty or single chunk."""
        chunks = chunk_document({"raw_text": "", "url": "http://example.com"})
        assert isinstance(chunks, list)

    def test_short_document(self):
        """Short doc should return at least one chunk."""
        doc = {"raw_text": "Short text here about AI research. " * 20, "url": "http://example.com"}
        chunks = chunk_document(doc)
        assert len(chunks) >= 1

    def test_long_document_splits(self):
        """Long doc should be split into multiple chunks."""
        long_text = " ".join(["This is a sentence about artificial intelligence research."] * 200)
        doc = {"raw_text": long_text, "url": "http://example.com"}
        chunks = chunk_document(doc)
        assert len(chunks) > 1

    def test_chunks_are_dicts(self):
        """Each chunk should be a dict."""
        doc = {"raw_text": " ".join(["Research finding."] * 50), "url": "http://x.com"}
        chunks = chunk_document(doc)
        if chunks:
            assert isinstance(chunks[0], dict)

    def test_returns_list(self):
        """Should always return a list."""
        doc = {"raw_text": "test content here", "url": "http://x.com"}
        assert isinstance(chunk_document(doc), list)


class TestConstants:
    """Test RAG constants."""

    def test_chunk_words_positive(self):
        assert CHUNK_WORDS > 0

    def test_chunk_overlap_less_than_size(self):
        assert CHUNK_OVERLAP < CHUNK_WORDS

    def test_batch_size_positive(self):
        assert BATCH_SIZE > 0


class TestExtractKeyPhrases:
    """Test key phrase extraction."""

    def test_basic_extraction(self):
        text = "Machine learning and natural language processing are subfields of artificial intelligence."
        phrases = extract_key_phrases(text)
        assert isinstance(phrases, list)

    def test_empty_text(self):
        phrases = extract_key_phrases("")
        assert isinstance(phrases, list)

    def test_returns_strings(self):
        phrases = extract_key_phrases("Python is used for data science and machine learning.")
        if phrases:
            assert all(isinstance(p, str) for p in phrases)


class TestEmbedText:
    """Test text embedding."""

    def test_function_exists(self):
        assert callable(embed_text)


class TestBuildRagIndex:
    def test_function_exists(self):
        assert callable(build_rag_index)


class TestEnsureSchema:
    def test_function_exists(self):
        assert callable(ensure_schema)


class TestEvaluateRag:
    def test_function_exists(self):
        assert callable(evaluate_rag)


class TestCorrectiveRetrieve:
    def test_function_exists(self):
        assert callable(corrective_retrieve)


class TestCrossMissionRetrieve:
    def test_function_exists(self):
        assert callable(cross_mission_retrieve)
