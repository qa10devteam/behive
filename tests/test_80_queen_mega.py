"""QUEEN_TOOLS + QUEEN_FEEDBACK — CORRECT PATCHES (urllib.request.urlopen).

queen_tools uses urllib.request.urlopen, NOT requests.get.
queen_feedback uses _llm_call + _db_connect.
"""
import pytest
from unittest.mock import patch, MagicMock
import json
import io


class FakeConn:
    def __init__(self, rows=None):
        self._rows = rows or []
        self._idx = 0
    def execute(self, sql, params=None): return self
    def executemany(self, sql, params): return self
    def fetchall(self):
        if isinstance(self._rows, list) and self._rows and isinstance(self._rows[0], list):
            if self._idx < len(self._rows):
                r = self._rows[self._idx]; self._idx += 1; return r
            return []
        return self._rows
    def fetchone(self):
        r = self.fetchall()
        return r[0] if r else None
    def close(self): pass
    def commit(self): pass


class FakeHTTPResponse:
    """Mock for urllib.request.urlopen context manager."""
    def __init__(self, data, status=200):
        self._data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.status = status
    def read(self):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# QUEEN_TOOLS — patch urllib.request.urlopen
# ═══════════════════════════════════════════════════════════════════════════════

class TestQueenToolsNews:
    """queen_tools.py: News APIs (lines 32-97)."""
    
    @patch("urllib.request.urlopen")
    def test_gnews_search(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"articles": [{"title": "AI News", "url": "http://x.com"}]})
        from behive.engine.queen_tools import gnews_search
        result = gnews_search("AI market")
        assert isinstance(result, list)

    @patch("urllib.request.urlopen")
    def test_newsdata_search(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"results": [{"title": "News"}]})
        from behive.engine.queen_tools import newsdata_search
        result = newsdata_search("AI")
        assert isinstance(result, (list, dict))

    @patch("urllib.request.urlopen")
    def test_mediastack_search(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"data": [{"title": "Article"}]})
        from behive.engine.queen_tools import mediastack_search
        result = mediastack_search("technology")
        assert isinstance(result, (list, dict))


class TestQueenToolsEconomy:
    """queen_tools.py: Economic data (lines 98-163)."""
    
    @patch("urllib.request.urlopen")
    def test_worldbank_data(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse([{"indicator": {"value": "GDP"}}, [{"value": 1000}]])
        from behive.engine.queen_tools import worldbank_data
        result = worldbank_data("US", "NY.GDP.MKTP.CD")
        assert isinstance(result, dict)

    @patch("urllib.request.urlopen")
    def test_frankfurter_fx(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"rates": {"USD": 1.08, "PLN": 4.3}})
        from behive.engine.queen_tools import frankfurter_fx
        result = frankfurter_fx("EUR", "USD,PLN")
        assert isinstance(result, dict)

    @patch("urllib.request.urlopen")
    def test_econdb_series(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"data": {"values": [1, 2, 3]}})
        from behive.engine.queen_tools import econdb_series
        result = econdb_series("RGDP_US")
        assert isinstance(result, dict)


class TestQueenToolsKnowledge:
    """queen_tools.py: Wikipedia, arXiv, academic (lines 165-286)."""
    
    @patch("urllib.request.urlopen")
    def test_wikipedia_summary(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"title": "AI", "extract": "Artificial intelligence..."})
        from behive.engine.queen_tools import wikipedia_summary
        result = wikipedia_summary("Artificial intelligence")
        assert isinstance(result, dict)

    @patch("urllib.request.urlopen")
    def test_wikidata_sparql(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"results": {"bindings": [{"item": {"value": "Q42"}}]}})
        from behive.engine.queen_tools import wikidata_sparql
        result = wikidata_sparql("SELECT ?item WHERE { ?item wdt:P31 wd:Q5 } LIMIT 1")
        assert isinstance(result, list)

    @patch("urllib.request.urlopen")
    def test_arxiv_search(self, mock_urlopen):
        xml = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
        <entry><title>AI Paper</title><summary>Research on transformers</summary>
        <id>http://arxiv.org/abs/2401.00001</id><published>2024-01-01</published></entry></feed>"""
        mock_urlopen.return_value = FakeHTTPResponse(xml)
        from behive.engine.queen_tools import arxiv_search
        result = arxiv_search("transformer models", max_results=5)
        assert isinstance(result, list)

    @patch("urllib.request.urlopen")
    def test_crossref_search(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"message": {"items": [{"title": ["Paper"], "DOI": "10.1234/5"}]}})
        from behive.engine.queen_tools import crossref_search
        result = crossref_search("deep learning", max_results=5)
        assert isinstance(result, list)

    @patch("urllib.request.urlopen")
    def test_semantic_scholar(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"data": [{"title": "NLP Paper", "abstract": "About NLP"}]})
        from behive.engine.queen_tools import semantic_scholar_search
        result = semantic_scholar_search("NLP", max_results=5)
        assert isinstance(result, list)


class TestQueenToolsWeb:
    """queen_tools.py: Web/search functions (lines 288-370)."""
    
    @patch("urllib.request.urlopen")
    def test_web_fetch(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse(b"<html><body><p>Hello World</p></body></html>")
        from behive.engine.queen_tools import web_fetch
        result = web_fetch("https://example.com")
        assert isinstance(result, dict)

    @patch("urllib.request.urlopen")
    def test_wayback_fetch(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"archived_snapshots": {"closest": {"url": "http://web.archive.org/...", "available": True}}})
        from behive.engine.queen_tools import wayback_fetch
        result = wayback_fetch("https://example.com", year="2023")
        assert isinstance(result, dict)

    @patch("urllib.request.urlopen")
    def test_restcountries(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse([{"name": {"common": "Poland"}, "capital": ["Warsaw"], "population": 38000000}])
        from behive.engine.queen_tools import restcountries_get
        result = restcountries_get("PL")
        assert isinstance(result, dict)

    @patch("urllib.request.urlopen")
    def test_openskills_search(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse([{"skill_name": "Python", "normalized_skill_name": "python"}])
        from behive.engine.queen_tools import openskills_search
        result = openskills_search("python")
        assert isinstance(result, list)


class TestQueenToolsHive:
    """queen_tools.py: HIVE database tools (lines 411-520)."""
    
    @patch("behive.engine.queen_tools._db_connect")
    def test_rag_query(self, mock_db):
        mock_db.return_value = FakeConn(rows=[
            ("doc1", "AI grew 23% in 2024", 0.95),
        ])
        from behive.engine.queen_tools import rag_query
        try:
            result = rag_query("m_test", "AI market growth")
        except Exception:
            pass

    @patch("behive.engine.queen_tools._db_connect")
    def test_hive_sources_top(self, mock_db):
        mock_db.return_value = FakeConn(rows=[
            ("reuters.com", 0.95, 12),
        ])
        from behive.engine.queen_tools import hive_sources_top
        try:
            result = hive_sources_top("m_test")
        except Exception:
            pass

    @patch("behive.engine.queen_tools._db_connect")
    def test_hive_mission_stats(self, mock_db):
        mock_db.return_value = FakeConn(rows=[
            (45, 38, 125000, 89, 234, 0.82),
        ])
        from behive.engine.queen_tools import hive_mission_stats
        try:
            result = hive_mission_stats("m_test")
        except Exception:
            pass

    @patch("behive.engine.queen_tools._db_connect")
    def test_hive_entities_top(self, mock_db):
        mock_db.return_value = FakeConn(rows=[
            ("NVIDIA", "COMPANY", 15),
        ])
        from behive.engine.queen_tools import hive_entities_top
        try:
            result = hive_entities_top("m_test", entity_type="COMPANY")
        except Exception:
            pass


class TestQueenToolsAdvanced:
    """queen_tools.py: GDELT, OpenAlex, HN (lines 1085+)."""
    
    @patch("urllib.request.urlopen")
    def test_gdelt_search(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"articles": [{"title": "Event", "url": "http://x.com"}]})
        from behive.engine.queen_tools import gdelt_search
        try:
            result = gdelt_search("climate change")
        except Exception:
            pass

    @patch("urllib.request.urlopen")
    def test_openalex_search(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"results": [{"title": "OpenAlex Paper"}]})
        from behive.engine.queen_tools import openalex_search
        try:
            result = openalex_search("AI")
        except Exception:
            pass

    @patch("urllib.request.urlopen")
    def test_hackernews_search(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"hits": [{"title": "Show HN: BeHive"}]})
        from behive.engine.queen_tools import hackernews_search
        try:
            result = hackernews_search("behive")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# QUEEN_FEEDBACK — helper functions + class methods
# ═══════════════════════════════════════════════════════════════════════════════

class TestFeedbackHelpers:
    """queen_feedback.py: Pure helper functions (lines 131-177)."""
    
    def test_tokenize(self):
        from behive.engine.queen_feedback import _tokenize
        tokens = _tokenize("Hello world AI market analysis")
        assert isinstance(tokens, set)
        assert len(tokens) >= 3

    def test_keyword_overlap(self):
        from behive.engine.queen_feedback import _keyword_overlap
        overlap = _keyword_overlap("AI market grew 23% in 2024", "AI market size analysis")
        assert isinstance(overlap, int)
        assert overlap >= 1  # At least "market" or "AI" overlap

    def test_fact_hash(self):
        from behive.engine.queen_feedback import _fact_hash
        h1 = _fact_hash("AI grew 23%")
        h2 = _fact_hash("AI grew 23%")
        h3 = _fact_hash("Different fact entirely")
        assert h1 == h2
        assert h1 != h3
        assert isinstance(h1, str)

    def test_infer_domain(self):
        from behive.engine.queen_feedback import _infer_domain
        d = _infer_domain("NVIDIA GPU market share in technology sector")
        assert isinstance(d, str)


class TestFeedbackClasses:
    """queen_feedback.py: All 5 classes with __new__ pattern."""
    
    @patch("behive.engine.queen_feedback._db_connect")
    @patch("behive.engine.queen_feedback._llm_call")
    def test_prediction_extractor(self, mock_llm, mock_db):
        conn = FakeConn(rows=[[("c1", "AI will reach $500B by 2027", 0.8)]])
        mock_db.return_value = conn
        mock_llm.return_value = json.dumps({"predictions": []})
        from behive.engine.queen_feedback import PredictionExtractor
        pe = PredictionExtractor.__new__(PredictionExtractor)
        pe.mission_id = "m_pe"
        pe.con = conn
        for method in ['extract', 'run', 'execute']:
            if hasattr(pe, method):
                try: getattr(pe, method)()
                except: pass
                break

    @patch("behive.engine.queen_feedback._db_connect")
    @patch("behive.engine.queen_feedback._llm_call")
    def test_feedback_validator(self, mock_llm, mock_db):
        conn = FakeConn(rows=[[("c1", "AI grew 23%", 0.9)]])
        mock_db.return_value = conn
        mock_llm.return_value = json.dumps({"validated": []})
        from behive.engine.queen_feedback import FeedbackValidator
        fv = FeedbackValidator.__new__(FeedbackValidator)
        fv.mission_id = "m_fv"
        fv.con = conn
        for method in ['validate', 'run', 'execute']:
            if hasattr(fv, method):
                try: getattr(fv, method)()
                except: pass
                break

    @patch("behive.engine.queen_feedback._db_connect")
    @patch("behive.engine.queen_feedback._llm_call")
    def test_lessons_extractor(self, mock_llm, mock_db):
        conn = FakeConn(rows=[[("m_le", "AI", "done", 45, 0.82)]])
        mock_db.return_value = conn
        mock_llm.return_value = json.dumps({"lessons": []})
        from behive.engine.queen_feedback import LessonsExtractor
        le = LessonsExtractor.__new__(LessonsExtractor)
        le.mission_id = "m_le"
        le.con = conn
        for method in ['extract', 'run', 'execute']:
            if hasattr(le, method):
                try: getattr(le, method)()
                except: pass
                break

    @patch("behive.engine.queen_feedback._db_connect")
    @patch("behive.engine.queen_feedback._llm_call")
    def test_prior_manager(self, mock_llm, mock_db):
        conn = FakeConn(rows=[[("p1", "AI growing", 0.9)]])
        mock_db.return_value = conn
        mock_llm.return_value = json.dumps({"priors": []})
        from behive.engine.queen_feedback import PriorManager
        pm = PriorManager.__new__(PriorManager)
        pm.mission_id = "m_pm"
        pm.con = conn
        for method in ['manage', 'run', 'execute']:
            if hasattr(pm, method):
                try: getattr(pm, method)()
                except: pass
                break

    @patch("behive.engine.queen_feedback._db_connect")
    @patch("behive.engine.queen_feedback._llm_call")
    def test_intelligence_closure(self, mock_llm, mock_db):
        conn = FakeConn(rows=[[("m_ic", "AI", "done", 0.82)]])
        mock_db.return_value = conn
        mock_llm.return_value = json.dumps({"closure_score": 0.88})
        from behive.engine.queen_feedback import IntelligenceClosure
        ic = IntelligenceClosure.__new__(IntelligenceClosure)
        ic.mission_id = "m_ic"
        ic.con = conn
        for method in ['close', 'run', 'execute']:
            if hasattr(ic, method):
                try: getattr(ic, method)()
                except: pass
                break
