"""Claim processing — extraction, deduplication, quality scoring, and DB persistence."""

from typing import Any
#!/usr/bin/env python3
"""
HIVE 2.0 — hive2_process.py
BPMN Operations Pipeline: Routes documents through the intelligence operations registry.

Architecture:
  Document → RelevanceFilter → OperationRouter → BeeWorker(s) → DuckDB

Usage:
  python3 hive2_process.py <mission_id> [--relevance-threshold 0.4] [--max-ops 10] [--debug]

Phase A (parallel, per-document):
  1. RelevanceFilter  — Bedrock haiku relevance scoring (skip score < threshold)
  2. OperationRouter  — heuristic routing to BPMN operations
  3. BeeWorker(s)     — parallel LLM calls per selected operation

Phase B (sequential, global):
  4. EntityDedup      — rapidfuzz entity deduplication
  5. FactValidation   — cross-domain confidence scoring
  6. GapDrone         — Bedrock research gap generation
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from statistics import mean, stdev
from typing import Any, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Required dependencies
# ---------------------------------------------------------------------------
try:
    pass  # duckdb removed — PostgreSQL is primary
except ImportError:
    duckdb = None  # Not fatal — Postgres is primary

# PostgreSQL backend (preferred — no lock issues)
try:
    import hive2_db
    _pg_available = (hive2_db.DB_BACKEND == "postgres")
except ImportError:
    _pg_available = False

try:
    import boto3
    _boto3_available = True
except ImportError:
    _boto3_available = False
    boto3 = None

# ---------------------------------------------------------------------------
# HIVE 2.0 Operations Registry
# ---------------------------------------------------------------------------
try:
    from hive2_ops import (
        BeeOperation,
        Tier,
        Specialty,
        get_operation,
        get_tier_ops,
    )
    from hive2_ops.tier1_recon import TIER1_OPERATIONS
    from hive2_ops.tier2_harvest import TIER2_OPERATIONS
    from hive2_ops.tier3_analysis import TIER3_OPERATIONS
    from hive2_ops.tier4_validation import TIER4_OPERATIONS
    from hive2_ops.tier5_synthesis import TIER5_OPERATIONS
    from hive2_ops.tier6_warriors import TIER6_OPERATIONS
    from hive2_ops.tier7_queen import TIER7_OPERATIONS
    # HIVE 3.0 — Biomimetic Intelligence Tiers
    from hive2_ops.tier8_neural import TIER8_OPERATIONS
    from hive2_ops.tier9_immune import TIER9_OPERATIONS
    from hive2_ops.tier10_mycelium import TIER10_OPERATIONS
    from hive2_ops.tier11_evolution import TIER11_OPERATIONS
    from hive2_ops.tier12_quantum import TIER12_OPERATIONS
    from hive2_ops.tier13_stigmergy import TIER13_OPERATIONS
    from hive2_ops.tier14_resonance import TIER14_OPERATIONS
    _ops_available = True
    ALL_OPERATIONS: list[BeeOperation] = (
        TIER1_OPERATIONS + TIER2_OPERATIONS + TIER3_OPERATIONS +
        TIER4_OPERATIONS + TIER5_OPERATIONS + TIER6_OPERATIONS +
        TIER7_OPERATIONS +
        # HIVE 3.0 Biomimetic tiers
        TIER8_OPERATIONS + TIER9_OPERATIONS + TIER10_OPERATIONS +
        TIER11_OPERATIONS + TIER12_OPERATIONS + TIER13_OPERATIONS +
        TIER14_OPERATIONS
    )
    _OPS_BY_ID: dict[str, BeeOperation] = {op.id: op for op in ALL_OPERATIONS}
except ImportError as _ops_err:
    _ops_available = False
    ALL_OPERATIONS = []
    _OPS_BY_ID = {}
    BeeOperation = None
    Tier = None

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    from rapidfuzz import fuzz as rfuzz
    _rapidfuzz_available = True
except ImportError:
    _rapidfuzz_available = False
    rfuzz = None

try:
    import spacy
    _spacy_available = True
except ImportError:
    _spacy_available = False
    spacy = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = ""


def _db_connect_retry(path: str = DB_PATH, read_only: bool = False, max_retries: int = 5):
    """Connect to database with retry. Uses PostgreSQL when HIVE_DB_BACKEND=postgres."""
    if _pg_available:
        return hive2_db.connect_retry(path, read_only=read_only, max_retries=max_retries)
    import time as _t
    for attempt in range(max_retries):
        try:
            con = _hive_db.connect(read_only=read_only)
            return con
        except Exception as e:
            if 'IO Error' in str(e) or 'lock' in str(e).lower() or 'busy' in str(e).lower():
                wait = 2 ** attempt * 2
                log.warning(f"DuckDB connect retry {attempt+1}/{max_retries}: {e}, waiting {wait}s")
                _t.sleep(wait)
            else:
                raise
    return _hive_db.connect(read_only=read_only)


# Bedrock models — global sonnet primary, opus fallback
MODEL_FAST_PRIMARY  = "us.anthropic.claude-haiku-4-5-20251001-v1:0"   # Haiku US — eu-central-1 wypalony
MODEL_FAST_FALLBACK = "us.anthropic.claude-haiku-4-5-20251001-v1:0"   # Haiku US
MODEL_DEEP          = "us.anthropic.claude-haiku-4-5-20251001-v1:0"   # Haiku US
BEDROCK_REGION      = "us-east-1"

# Concurrency limits
BATCH_SIZE         = 20   # docs processed concurrently
MAX_BEDROCK_CALLS  = 4   # global semaphore cap — niższe = mniej throttlingu

# Dedup threshold (rapidfuzz)
DEDUP_THRESHOLD = 85

# Regex patterns (fallback NER)
_RE_PERSON   = re.compile(r"\b([A-Z][a-z]+ (?:[A-Z][a-z]+ )?[A-Z][a-z]+)\b")
_RE_ORG      = re.compile(r"\b([A-Z][A-Za-z&\s]{2,30}(?:Inc\.|Corp\.|Ltd\.|LLC|GmbH|SA|AG|plc|Group))\b")
_RE_MONEY    = re.compile(r"(\$|€|£|USD|EUR|GBP)\s*(\d[\d,\.]*\s*(?:million|billion|mln|bn)?)", re.IGNORECASE)
_RE_PERCENT  = re.compile(r"\b(\d+(?:\.\d+)?)\s*(%|percent)\b", re.IGNORECASE)
_RE_PERCENT_MILLION = re.compile(
    r"(\d+[.,]?\d*)\s*(%|percent|million|billion|mln|mld|tys\.?|thousand|k\b|M\b|bn\b)",
    re.IGNORECASE,
)
_RE_FORMATTED_NUMBER = re.compile(r"\b(\d{1,3}(?:[.,]\d{3})+)\b")
_RE_USER_METRIC = re.compile(
    r"\b(\d+)\s*(users|subscribers|downloads|installs|DAU|MAU)\b", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hive2.process")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception as e:
        log.debug(f"Suppressed in process.py: {e}")
        return ""


def _detect_doc_type(title: str, domain: str, text: str) -> str:
    """Fast heuristic doc-type classification. Returns one of: financial, legal,
    social_media, scientific, corporate_disclosure, news, general."""
    title_lower  = (title or "").lower()
    domain_lower = (domain or "").lower()
    text_sample  = (text or "")[:500].lower()
    combined     = title_lower + " " + domain_lower + " " + text_sample

    if any(k in combined for k in ["balance sheet", "income statement", "revenue", "earnings",
                                    "10-k", "annual report", "quarterly", "ebitda", "cash flow"]):
        return "financial"
    if any(k in combined for k in ["legal", "court", "regulation", "statute", "judgment",
                                    "contract", "clause", "plaintiff", "defendant", "law review"]):
        return "legal"
    if any(k in domain_lower for k in ["twitter", "x.com", "facebook", "instagram",
                                        "tiktok", "reddit", "telegram", "tweet"]):
        return "social_media"
    if any(k in combined for k in ["abstract", "methodology", "doi:", "arxiv",
                                    "journal", "hypothesis", "conclusion", "citation"]):
        return "scientific"
    if any(k in combined for k in ["disclosure", "sec filing", "8-k", "proxy statement",
                                    "investor relation", "corporate governance"]):
        return "corporate_disclosure"
    if any(k in domain_lower for k in ["reuters", "bloomberg", "bbc", "cnn", "nytimes",
                                        "guardian", "apnews", "news", "herald", "times"]):
        return "news"
    if any(k in combined for k in ["says", "reports", "announced", "according to",
                                    "breaking", "exclusive"]):
        return "news"
    return "general"


# ---------------------------------------------------------------------------
# SGLang client — replaces Bedrock, zero throttling, 30x concurrency
# ---------------------------------------------------------------------------

import urllib.request as _urllib_request

SGLANG_URL        = os.environ.get("BEHIVE_SGLANG_URL", "http://localhost:8002/v1/chat/completions")
SGLANG_MODEL      = "Qwen2.5-7B-Instruct"
MAX_LLM_CALLS     = 40   # SGLang continuous batching — 40 parallel bez problemu

class SGLangClient:
    """HTTP client for SGLang OpenAI-compatible API. Thread-safe, stateless."""

    def invoke(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 1024,
    ) -> str:
        """Invoke a single bee operation on content."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model":      SGLANG_MODEL,
            "messages":   messages,
            "max_tokens": min(max_tokens, 4096),
            "temperature": 0.1,
        }
        body = json.dumps(payload).encode()
        req = _urllib_request.Request(
            SGLANG_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(3):
            try:
                with _urllib_request.urlopen(req, timeout=120) as resp:
                    out = json.loads(resp.read())
                content = out["choices"][0]["message"]["content"].strip()
                # Strip markdown fences if model added them
                if content.startswith("```"):
                    lines = content.split("\n")
                    content = "\n".join(
                        lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
                    ).strip()
                return content
            except Exception as e:
                log.debug(f"Exception in process.py: {e}")
                import time as _time
                if attempt < 2:
                    _time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"SGLang invoke failed after 3 attempts: {e}") from e
        raise RuntimeError("SGLang invoke: unreachable")

    def is_available(self) -> bool:
        """Quick health check — cached, re-checked every 30s."""
        import time as _t
        now = _t.time()
        if now - self._last_check < 30:
            return self._available
        try:
            url = SGLANG_URL.replace("/v1/chat/completions", "/health")
            with _urllib_request.urlopen(url, timeout=2) as r:
                self._available = r.status == 200
        except Exception as e:
            log.debug(f"Suppressed in process.py: {e}")
            self._available = False
        self._last_check = _t.time()
        return self._available

    def __init__(self):
        self._available = False
        self._last_check = 0.0


# Bedrock fallback client (used when SGLang is unavailable)
import boto3 as _boto3
import json as _json_b

class _BedrockFallbackClient:
    def __init__(self):
        self._client = None
    def _get(self):
        if self._client is None:
            self._client = _boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
        return self._client
    def invoke(self, prompt: str, system_prompt: str = None, model: str = None, max_tokens: int = 512) -> str:
        """Invoke a single bee operation on content."""
        m = model or MODEL_FAST_PRIMARY
        sp = system_prompt or "You are a precise data extraction AI. Return only valid JSON."
        body = _json_b.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": sp,
            "messages": [{"role": "user", "content": prompt}]
        })
        resp = self._get().invoke_model(body=body, modelId=m, contentType="application/json", accept="application/json")
        out = _json_b.loads(resp["body"].read())
        return out["content"][0]["text"].strip()

_bedrock_client = _BedrockFallbackClient()
_sglang = SGLangClient()
_llm_semaphore: Optional[asyncio.Semaphore] = None
_executor = ThreadPoolExecutor(max_workers=MAX_LLM_CALLS)


async def _bedrock_invoke_async(
    prompt: str,
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: int = 1024,
) -> str:
    """LLM invoke — SGLang backend, OpenAI-compatible, zero throttling.
    Nazwa zachowana dla kompatybilności wstecznej z resztą kodu.
    """
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(MAX_LLM_CALLS)

    async with _llm_semaphore:
        loop = asyncio.get_event_loop()
        # Try SGLang first (bee model, zero throttling), fall back to Bedrock Haiku
        try:
            if _sglang.is_available():
                return await loop.run_in_executor(
                    _executor,
                    lambda: _sglang.invoke(prompt, system_prompt, model, max_tokens),
                )
        except Exception as e:
            log.debug(f"Suppressed: {e}")
        # Bedrock fallback
        _m = model or MODEL_FAST_PRIMARY
        return await loop.run_in_executor(
            _executor,
            lambda: _bedrock_client.invoke(prompt, system_prompt=system_prompt or "You are a precise data extraction AI. Return only valid JSON.", model=_m, max_tokens=max_tokens),
        )


def _extract_json_from_text(text: str) -> Any:
    """Try to parse JSON from LLM response. Returns None on failure."""
    # Try direct parse
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        log.debug(f"Parse error: {e}")
    # Try fenced code block
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            log.debug(f"Parse error: {e}")
    # Try first JSON array
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            return json.loads(m.group())
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            log.debug(f"Parse error: {e}")
    # Try first JSON object
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group())
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            log.debug(f"Parse error: {e}")
    return None


# ---------------------------------------------------------------------------
# RelevanceFilter
# ---------------------------------------------------------------------------

class RelevanceFilter:
    """
    Scores each document against the mission topic using local SGLang/Qwen (free).
    Bedrock Haiku as fallback. Returns 0.0–1.0; skips docs below threshold.
    
    v2 (2026-07-27): Stricter scoring with keyword overlap pre-filter + longer excerpts.
    """

    def __init__(self, topic: str, threshold: float = 0.4, debug: bool = False):
        self.topic     = topic
        self.threshold = threshold
        self.debug     = debug
        # Pre-compute topic keywords for fast pre-filter
        self._topic_keywords = self._extract_keywords(topic)

    @staticmethod
    def _extract_keywords(text: str) -> set[str]:
        """Extract meaningful keywords (3+ chars, lowercased, no stopwords). Includes years."""
        stopwords = {'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can',
                     'had', 'her', 'was', 'one', 'our', 'out', 'has', 'have', 'from',
                     'they', 'been', 'said', 'each', 'which', 'their', 'will', 'other',
                     'about', 'many', 'then', 'them', 'some', 'what', 'would', 'make',
                     'like', 'into', 'than', 'its', 'over', 'such', 'after', 'with',
                     'this', 'that', 'how', 'does', 'comparison', 'between', 'versus'}
        words = re.findall(r'[a-zA-Z0-9]{3,}', text.lower())
        return {w for w in words if w not in stopwords}

    def _keyword_overlap(self, doc: dict) -> float:
        """Fast keyword overlap score (0.0-1.0). Used as pre-filter."""
        if not self._topic_keywords:
            return 0.5
        text = f"{doc.get('title', '')} {(doc.get('content') or doc.get('text') or doc.get('raw_text') or '')[:2000]}".lower()
        # Skip pre-filter for very short docs (let LLM decide)
        if len(text) < 100:
            return 0.5
        doc_words = set(re.findall(r'[a-zA-Z0-9]{3,}', text))
        overlap = len(self._topic_keywords & doc_words)
        return min(1.0, overlap / max(1, len(self._topic_keywords) * 0.4))

    async def score(self, doc_id: str, title: str, text_sample: str) -> float:
        """Returns relevance score 0.0–1.0. Uses local SGLang/Qwen (free), Bedrock fallback."""
        if not _sglang.is_available() and not _boto3_available:
            return 1.0  # no-op fallback: process everything

        prompt = (
            f"Rate the relevance of this document to the research topic: \"{self.topic}\"\n\n"
            f"Document title: {title or 'untitled'}\n"
            f"Document excerpt: {text_sample[:800]}\n\n"
            "Respond with ONLY a JSON object: {\"score\": <float 0.0-1.0>, \"reason\": \"<one sentence>\"}\n"
            "Scoring guide:\n"
            "- 0.8-1.0: Directly about the topic, contains key data (revenue, dates, names)\n"
            "- 0.5-0.8: Contains relevant information but not the primary focus\n"
            "- 0.2-0.5: Tangentially related, mentions topic keywords in passing\n"
            "- 0.0-0.2: Completely unrelated, no connection to the topic\n"
            "IMPORTANT: First-party data (company's own reports, press releases with numbers) about the topic entity IS highly relevant even if promotional."
        )

        try:
            # SGLang primary (local Qwen, free)
            text = await asyncio.to_thread(_sglang.invoke, prompt, None, None, 100)
            data = _extract_json_from_text(text)
            if data and isinstance(data, dict):
                score = float(data.get("score", 0.5))
                score = max(0.0, min(1.0, score))
                return score
            # try plain float
            m = re.search(r"\b(0\.\d+|1\.0|0)\b", text)
            if m:
                return max(0.0, min(1.0, float(m.group(1))))
        except Exception as e:
            if self.debug:
                log.debug(f"  relevance score failed for doc {doc_id}: {e}")
            return 0.6  # conservative fallback: process on error

        return 0.5

    async def score_batch(self, docs: list[dict]) -> list[float]:
        """Score up to 20 docs in a single LLM call (SGLang/Qwen primary, Bedrock fallback)."""
        if not docs:
            return [1.0] * len(docs)
        if not _sglang.is_available() and not _boto3_available:
            return [1.0] * len(docs)
        if self.threshold <= 0.0:
            return [1.0] * len(docs)  # --no-filter bypass

        items = "\n".join(
            f"{i+1}. [{(doc.get('title') or 'untitled')[:80]}] {((doc.get('content') or doc.get('text') or doc.get('raw_text') or ''))[:400]}"
            for i, doc in enumerate(docs)
        )
        prompt = (
            f"Research topic: \"{self.topic}\"\n\n"
            f"Rate each document's relevance (0.0=unrelated, 1.0=highly relevant).\n"
            f"Be STRICT: 0.3 or below for tangential mentions. 0.5+ only if the document DIRECTLY addresses the topic.\n\n"
            f"{items}\n\n"
            f"Respond with ONLY a JSON array of {len(docs)} floats, e.g. [0.9, 0.1, 0.7, ...]"
        )
        try:
            # SGLang primary (local Qwen, free + fast)
            text = await asyncio.to_thread(_sglang.invoke, prompt, None, None, 200)
            m = re.search(r'\[[\d.,\s]+\]', text)
            if m:
                scores = json.loads(m.group(0))
                if len(scores) == len(docs):
                    return [max(0.0, min(1.0, float(s))) for s in scores]
        except Exception as e:
            log.debug(f"Suppressed in process.py: {e}")
            # Bedrock fallback
            try:
                text = await _bedrock_invoke_async(prompt, max_tokens=200)
                m = re.search(r'\[[\d.,\s]+\]', text)
                if m:
                    scores = json.loads(m.group(0))
                    if len(scores) == len(docs):
                        return [max(0.0, min(1.0, float(s))) for s in scores]
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                log.debug(f"Parse error: {e}")
        return [0.4] * len(docs)  # fallback — conservative, below default threshold

    async def filter(self, docs: list[dict]) -> list[dict]:
        """Score all docs and return those that pass threshold.
        
        v2: Two-stage filter:
          Stage 0: Pass through pre-scored API docs (already validated)
          Stage 1: Fast keyword overlap pre-filter (drops obvious garbage, no LLM call)
          Stage 2: LLM scoring on survivors (batch 20/call, concurrent)
        """
        # ── Stage 0: Pass through pre-scored docs (from API Scout) ──
        pre_scored = [d for d in docs if d.get('_from_api') and d.get('relevance_score', 0) > 0]
        remaining = [d for d in docs if not d.get('_from_api')]
        
        # ── Stage 1: Keyword pre-filter (free, instant) ──
        pre_passed = []
        pre_skipped = 0
        KEYWORD_THRESHOLD = 0.10  # Very low — just catches total garbage
        KEYWORD_AUTO_PASS = 0.25  # Keyword match = auto-pass (SGLang LLM too aggressive)
        auto_passed = []
        for doc in remaining:
            kw_score = self._keyword_overlap(doc)
            doc["_keyword_score"] = kw_score
            if kw_score >= KEYWORD_AUTO_PASS:
                # Strong keyword overlap — bypass LLM scoring (first-party data, press releases)
                doc["_relevance_score"] = kw_score
                auto_passed.append(doc)
            elif kw_score >= KEYWORD_THRESHOLD:
                pre_passed.append(doc)
            else:
                doc["_relevance_score"] = kw_score * 0.3  # store low score
                pre_skipped += 1
        
        if auto_passed:
            log.info(f"  Keyword auto-pass: {len(auto_passed)} docs (kw>={KEYWORD_AUTO_PASS})")
        if pre_skipped > 0:
            log.info(f"  Keyword pre-filter: {pre_skipped} docs dropped (0 topic keywords), {len(pre_passed)} remain")

        # ── Stage 2: LLM relevance scoring (SGLang, batched) ──
        BATCH = 20
        chunks = [pre_passed[i:i+BATCH] for i in range(0, len(pre_passed), BATCH)]

        # Fire all score_batch calls concurrently
        chunk_scores = await asyncio.gather(
            *[self.score_batch(chunk) for chunk in chunks],
            return_exceptions=True,
        )

        all_scores: list[float] = []
        for scores in chunk_scores:
            if isinstance(scores, Exception):
                all_scores.extend([0.4] * BATCH)  # Conservative: don't auto-pass on error
            else:
                all_scores.extend(scores)

        passed = []
        skipped = pre_skipped
        
        # ── FALLBACK: If LLM gives 0 on >80% docs, it's broken — use keyword scores ──
        zero_count = sum(1 for s in all_scores if not isinstance(s, Exception) and float(s) < 0.05)
        llm_broken = len(all_scores) > 5 and zero_count / max(1, len(all_scores)) > 0.80
        if llm_broken:
            log.warning(f"  ⚠️ LLM relevance broken ({zero_count}/{len(all_scores)} scored 0.0) — falling back to keyword scoring")
        
        for doc, score in zip(pre_passed, all_scores):
            if isinstance(score, Exception):
                score = 0.4  # below threshold on error → skip by default
            score = float(score)
            kw_score = doc.get("_keyword_score", 0.5)
            
            if llm_broken:
                # LLM is useless — use keyword score as primary
                final_score = kw_score
            else:
                # Blend with keyword score for robustness (80% LLM, 20% keyword)
                final_score = score * 0.8 + kw_score * 0.2
            
            doc["_relevance_score"] = final_score
            doc_id = doc.get("source_id", "?")
            if final_score >= self.threshold:
                arrow = "→ process"
                passed.append(doc)
            else:
                arrow = "→ skip"
                skipped += 1
            if self.debug:
                log.info(f"  doc {doc_id}: score {final_score:.2f} (llm={score:.2f}, kw={kw_score:.2f}) {arrow}")

        log.info(
            f"RelevanceFilter: {len(passed)} pass / {skipped} skip "
            f"(threshold={self.threshold})"
            + (f" + {len(pre_scored)} API docs" if pre_scored else "")
            + (f" + {len(auto_passed)} keyword-autopass" if auto_passed else "")
        )
        return pre_scored + auto_passed + passed


# ---------------------------------------------------------------------------
# OperationRouter
# ---------------------------------------------------------------------------

class OperationRouter:
    """
    Heuristic routing: maps document type to BPMN operation IDs.
    Returns list of BeeOperation objects to run.
    Includes HIVE 3.0 biomimetic tiers T8-T14 routing.
    """

    # Operations run on every document regardless of type.
    # BPMN-CI1 FIRST — always produces claims (hive_claims rows).
    # BPMN-047/048/049 produce entities/relations/facts.
    UNIVERSAL_OPS = ["BPMN-CI1", "BPMN-047", "BPMN-048", "BPMN-049"]

    # Type → additional op IDs (T1-T7 legacy routing)
    TYPE_ROUTING: dict[str, list[str]] = {
        "financial":            ["BPMN-041"],
        "legal":                ["BPMN-042"],
        "social_media":         ["BPMN-044"],
        "scientific":           ["BPMN-043"],
        "corporate_disclosure": ["BPMN-008", "BPMN-068"],
        "news":                 ["BPMN-046", "BPMN-058"],
        "general":              [],
    }

    # ── HIVE 3.0 Biomimetic Tier Routing ────────────────────────────────────
    # T8 Neural — pattern learning & synapse weighting (all doc types)
    BIOMIMETIC_UNIVERSAL_OPS = [
        "BPMN-242",  # T8: Hebbian Learning Synapse Weighter
        "BPMN-322",  # T9: Pattern Recognition Receptor Scanner
        "BPMN-402",  # T10: Hyphal Network Topology Mapper
        "BPMN-482",  # T11: Selection Pressure Mapper
        "BPMN-562",  # T12: Superposition Hypothesis Initializer
        "BPMN-642",  # T13: Pheromone Trail Mapper
        "BPMN-722",  # T14: Schumann Resonance Scanner
    ]

    # Type → biomimetic ops for specialized analysis
    BIOMIMETIC_TYPE_ROUTING: dict[str, list[str]] = {
        "financial": [
            "BPMN-483",  # T11: Fitness Landscape Analyzer (market fitness)
            "BPMN-563",  # T12: Superposition Amplitude Weighting (multi-scenario)
            "BPMN-643",  # T13: Stigmergic Path Optimizer (capital flow)
        ],
        "legal": [
            "BPMN-327",  # T9: Molecular Mimicry Detector (legal pattern mimicry)
            "BPMN-487",  # T11: Extinction Risk Assessor (legal risk)
            "BPMN-647",  # T13: Colony Intelligence Aggregator (precedent clustering)
        ],
        "social_media": [
            "BPMN-247",  # T8: Gap Junction Coupling Detector (information spreading)
            "BPMN-323",  # T9: Toll-Like Receptor Profiler (threat pattern)
            "BPMN-727",  # T14: Coupling Coefficient Calculator (viral coupling)
        ],
        "scientific": [
            "BPMN-243",  # T8: Long-Term Potentiation Amplifier (knowledge reinforcement)
            "BPMN-407",  # T10: Oscillatory Wave Propagator (idea diffusion)
            "BPMN-567",  # T12: Superposition Competitive Intelligence Probe
        ],
        "corporate_disclosure": [
            "BPMN-403",  # T10: Anastomosis Junction Detector (corporate network junctions)
            "BPMN-483",  # T11: Fitness Landscape Analyzer
            "BPMN-643",  # T13: Stigmergic Path Optimizer
        ],
        "news": [
            "BPMN-322",  # T9: Pattern Recognition Receptor Scanner (narrative patterns)
            "BPMN-247",  # T8: Gap Junction Coupling Detector (story spread)
            "BPMN-723",  # T14: Resonance Cavity Identifier (echo chambers)
        ],
        "general": [
            "BPMN-242",  # T8: Hebbian Learning Synapse Weighter
            "BPMN-402",  # T10: Hyphal Network Topology Mapper
        ],
    }

    def __init__(self, max_ops: int = 5, debug: bool = False,
                 enable_biomimetic: bool = False):
        """
        Args:
            enable_biomimetic: T8-T14 cognitive bees. DISABLED by default (2026-07-27).
                Reason: data audit showed 75% noise (meta-commentary), 50% compute cost,
                0% quality improvement. Cognition feedback loop dead (0/146 mutations accepted,
                empty fitness/tuning/priors tables). Re-enable only after ground-truth feedback
                loop is operational.
        """
        self.max_ops           = max_ops
        self.debug             = debug
        self.enable_biomimetic = enable_biomimetic
        self._op_fitness = self._load_feedback_fitness()

    def _load_feedback_fitness(self) -> dict:
        """Load operation fitness from user feedback. Returns {op_name: useful_ratio}."""
        try:
            import psycopg2
            from behive.engine.db import get_pg_connection
            pg = get_pg_connection()
            cur = pg.cursor()
            cur.execute("""
                SELECT source_operation, 
                       COUNT(*) as total,
                       COUNT(*) FILTER (WHERE rating = 1) as useful,
                       COUNT(*) FILTER (WHERE rating = -1) as useless
                FROM hive_claim_feedback
                WHERE source_operation IS NOT NULL
                GROUP BY source_operation
                HAVING COUNT(*) >= 5
            """)
            fitness = {}
            for op, total, useful, useless in cur.fetchall():
                fitness[op] = useful / max(1, total)
            cur.close(); pg.close()
            if fitness:
                log.info(f"  📊 Feedback fitness loaded: {len(fitness)} ops rated")
            return fitness
        except Exception as e:
            log.debug(f"Suppressed in process.py: {e}")
            return {}

    def route(self, doc: dict) -> list["BeeOperation"]:
        """Return ordered list of BeeOperation instances to apply to this document.

        Routing order:
          1. Type-specific T1-T7 ops
          2. Universal T1-T7 ops (BPMN-047/048/049)
          3. Type-specific T8-T14 biomimetic ops  [if enable_biomimetic]
          4. Universal T8-T14 biomimetic ops       [if enable_biomimetic]
        """
        if not _ops_available:
            return []

        title  = doc.get("title", "") or ""
        domain = doc.get("domain", "") or _domain(doc.get("url", ""))
        text   = doc.get("raw_text", "") or ""

        doc_type = _detect_doc_type(title, domain, text)
        doc["_doc_type"] = doc_type

        # ── Legacy T1-T7 routing ───────────────────────────────────────────
        type_ops   = self.TYPE_ROUTING.get(doc_type, [])
        all_op_ids = type_ops + [o for o in self.UNIVERSAL_OPS if o not in type_ops]

        # ── HIVE 3.0 Biomimetic T8-T14 routing ────────────────────────────
        if self.enable_biomimetic:
            bio_type_ops      = self.BIOMIMETIC_TYPE_ROUTING.get(doc_type, [])
            bio_universal_ops = [o for o in self.BIOMIMETIC_UNIVERSAL_OPS
                                 if o not in bio_type_ops]
            all_op_ids = all_op_ids + bio_type_ops + bio_universal_ops

        # Deduplicate preserving order, then cap
        all_op_ids = list(dict.fromkeys(all_op_ids))

        # ── Feedback-driven filtering: suppress ops with low user ratings ──
        if self._op_fitness:
            ops_before = len(all_op_ids)
            filtered = []
            suppressed = []
            for op_id in all_op_ids:
                op = _OPS_BY_ID.get(op_id)
                if op and op.name in self._op_fitness:
                    ratio = self._op_fitness[op.name]
                    if ratio < 0.3:  # Less than 30% useful → suppress
                        suppressed.append(op.name)
                        continue
                filtered.append(op_id)
            all_op_ids = filtered
            if suppressed and self.debug:
                log.debug(f"  📊 Suppressed low-fitness ops: {suppressed}")

        all_op_ids = all_op_ids[: self.max_ops]

        ops = []
        for op_id in all_op_ids:
            op = _OPS_BY_ID.get(op_id)
            if op:
                ops.append(op)
            elif self.debug:
                log.debug(f"  operation {op_id} not found in registry")

        if self.debug:
            legacy  = [o.id for o in ops if o.tier and o.tier.value <= 7]
            biomim  = [o.id for o in ops if o.tier and o.tier.value >= 8]
            log.debug(f"  [{doc_type}] routing → legacy={legacy} biomimetic={biomim}")

        return ops


# ---------------------------------------------------------------------------
# BeeWorker
# ---------------------------------------------------------------------------

class BeeWorker:
    """
    Executes a single BeeOperation against a document.
    Substitutes template variables, calls Bedrock, parses output, saves to DB.
    """

    def __init__(self, topic: str, debug: bool = False):
        self.topic = topic
        self.debug = debug

    def _build_prompt(self, op: "BeeOperation", doc: dict) -> str:
        """Substitute template variables into the operation's prompt template."""
        text    = (doc.get("raw_text", "") or "")[:6000]  # cap to ~6k chars
        title   = doc.get("title", "") or ""
        domain  = doc.get("domain", "") or _domain(doc.get("url", ""))
        topic   = self.topic

        # Build topic-aware template if op has no custom one
        if op.llm_prompt_template:
            template = op.llm_prompt_template
        else:
            # Detect competitive intelligence context
            comp_keywords = ["konkurencja", "competitor", "competitive", "competition",
                             "rival", "rynek", "market", "landscape", "vs ", "versus",
                             "alternative", "alternative to"]
            is_competitive = any(k in topic.lower() for k in comp_keywords)

            if is_competitive:
                template = (
                    f"You are a competitive intelligence analyst extracting facts for: {topic}\n\n"
                    "Extract EVERY actionable fact from this document. Even if the document is not directly about the topic, "
                    "extract anything useful: company names, prices, tech stacks, clients, reviews, market data.\n\n"
                    "Rules:\n"
                    "- NEVER write 'Document contains no data' — always extract something\n"
                    "- If document mentions ANY company: extract it with what they do\n"
                    "- If document mentions ANY price/cost: extract it with context\n"
                    "- If document mentions ANY client/customer: extract it\n"
                    "- If document is a job posting: extract company + required tools + salary if present\n"
                    "- Minimum 1 claim per document, maximum 10\n\n"
                    "Return JSON array ONLY:\n"
                    '[{"claim": "exact fact from document", "company": "company name or empty", '
                    '"type": "pricing|customer|market|feature|review|job|technology|general", '
                    '"confidence": 0.5-1.0, "source_context": "brief quote from document"}]\n\n'
                    "Document text:\n{text}"
                )
            else:
                template = (
                    f"Extract structured intelligence from the following text about {topic}. "
                    "Return results as JSON."
                )

        # Substitute known variables
        prompt = template
        for var, val in [
            ("{text}", text), ("{topic}", topic),
            ("{target}", domain or title), ("{title}", title),
            ("{domain}", domain), ("{url}", doc.get("url", "")),
        ]:
            prompt = prompt.replace(var, val)

        # Append text as user content if template has no {text} placeholder
        if "{text}" not in template:
            prompt = (
                f"Mission topic: {topic}\n\n"
                f"{prompt}\n\n"
                f"=== DOCUMENT ===\nTitle: {title}\nSource: {domain}\n\n{text}\n=== END ===\n\n"
                f"IMPORTANT: Return ONLY a valid JSON object or array. No markdown fences, no prose outside JSON. Start your response with {{ or [."
            )
        else:
            prompt = (
                f"Mission topic: {topic}\n\n"
                f"{prompt}\n\n"
                f"IMPORTANT: Return ONLY a valid JSON object or array. No markdown fences, no prose outside JSON. Start your response with {{ or [."
            )

        return prompt

    async def execute(
        self,
        op: "BeeOperation",
        doc: dict,
        con: Any,
        mission_id: str,
    ) -> dict:
        """Run operation, save results. Returns result summary dict."""
        start_ms = int(time.monotonic() * 1000)
        doc_id   = str(doc.get("source_id", ""))
        url      = doc.get("url", "")
        score    = doc.get("_relevance_score", 1.0)

        prompt = self._build_prompt(op, doc)

        # Dynamic max_tokens: competitive intel needs more tokens for JSON array output
        input_chars = len(prompt)
        if input_chars > 4000:
            dyn_max_tokens = 1500
        elif input_chars > 2000:
            dyn_max_tokens = 1200
        else:
            dyn_max_tokens = 900   # always enough for 5-10 claims JSON array

        try:
            raw_text = await _bedrock_invoke_async(prompt, max_tokens=dyn_max_tokens)
        except Exception as exc:
            log.warning(f"  BeeWorker [{op.id}] doc={doc_id}: Bedrock error: {exc}")
            return {"op_id": op.id, "success": False, "error": str(exc)}

        elapsed_ms = int(time.monotonic() * 1000) - start_ms

        # Parse response
        parsed = _extract_json_from_text(raw_text)
        output_json = json.dumps(parsed) if parsed else raw_text[:2000]

        # Confidence from operation threshold
        confidence = float(op.confidence_threshold) if parsed else 0.3

        # Save to hive_bee_results
        result_id = _new_id()
        try:
            con.execute("""
                INSERT INTO hive_bee_results
                    (id, mission_id, doc_id, operation_id, operation_name,
                     tier, relevance_score, output_json, confidence, processing_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, [
                result_id, mission_id, doc_id, op.id, op.name,
                op.tier.value if op.tier else 0,
                round(float(score), 4),
                output_json, confidence, elapsed_ms,
            ])
        except Exception as exc:
            log.debug(f"  hive_bee_results save error: {exc}")

        # Dispatch to entity/fact savers based on operation type
        entities_saved = 0
        facts_saved    = 0
        if parsed:
            entities_saved = self._save_entities(parsed, op, doc, con, mission_id)
            facts_saved    = self._save_facts(parsed, op, doc, con, mission_id)

        # Save competitive claims directly to hive_claims (crucial for synthesis)
        claims_saved = self._save_claims(parsed, op, doc, con, mission_id)

        if self.debug:
            log.debug(
                f"  [{op.id}] doc={doc_id} {elapsed_ms}ms "
                f"entities={entities_saved} facts={facts_saved} claims={claims_saved}"
            )

        return {
            "op_id":          op.id,
            "success":        True,
            "entities_saved": entities_saved,
            "facts_saved":    facts_saved,
            "claims_saved":   claims_saved,
            "elapsed_ms":     elapsed_ms,
        }

    def _save_claims(
        self,
        parsed: Any,
        op: "BeeOperation",
        doc: dict,
        con: Any,
        mission_id: str,
    ) -> int:
        """Save competitive intelligence claims (companies, prices, customers) to hive_claims."""
        url   = doc.get("url", "")
        saved = 0
        rows  = []

        def _harvest(data):
            if isinstance(data, list):
                for item in data:
                    _harvest(item)
            elif isinstance(data, dict):
                claim_text = str(
                    data.get("claim") or data.get("finding") or data.get("fact") or ""
                ).strip()
                # If claim_text looks like JSON/dict (starts with {), try to extract narrative or readable form
                if claim_text.startswith("{") or claim_text.startswith("{'"):
                    narrative = data.get("narrative") or data.get("description") or data.get("summary") or ""
                    entity = data.get("entity") or data.get("name") or ""
                    if narrative:
                        claim_text = f"{entity}: {narrative}".strip(": ") if entity else str(narrative)
                    else:
                        return  # Skip raw JSON blobs
                if not claim_text or len(claim_text) < 15:
                    return
                # Skip meta-commentary — extractor sometimes writes "Document has no data"
                skip_phrases = [
                    "document contains no", "document is a generic", "no quantitative",
                    "does not contain", "no data on", "document does not", "UNCONFIRMED:",
                    "document has no", "no specific data", "no information",
                ]
                if any(p.lower() in claim_text.lower() for p in skip_phrases):
                    return
                # ─── QUALITY GATE: reject low-quality claims before DB insert ───
                try:
                    from hive3_bees import SpecializedBee
                    quality = SpecializedBee._score_claim_quality(claim_text)
                    if quality < 0.65:  # Quality gate — only useful claims enter DB
                        return  # Garbage/mediocre — don't pollute the DB
                except ImportError:
                    pass  # hive3_bees not available — skip quality gate
                claim_type = str(
                    data.get("type") or data.get("claim_type") or "intelligence"
                ).lower()
                company    = str(data.get("company") or data.get("entity") or "")[:200]
                evidence   = str(
                    data.get("source_context") or data.get("evidence") or data.get("context") or ""
                )[:500]
                conf       = float(data.get("confidence", 0.7))
                # Boost confidence when company name is present (more specific = more reliable)
                if company and company not in ("", "Unknown", "N/A"):
                    conf = min(1.0, conf + 0.1)
                rows.append([
                    mission_id, claim_text[:1000], evidence, url,
                    claim_type, round(conf, 4),
                ])

        _harvest(parsed)

        if rows:
            try:
                con.executemany("""
                    INSERT INTO hive_claims
                        (id, mission_id, claim, evidence, source_url,
                         claim_type, confidence, extracted_at)
                    VALUES (nextval('hive_facts_seq'), ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, rows)
                saved = len(rows)
            except Exception as exc:
                log.debug(f"  _save_claims batch error: {exc}")
                for row in rows:
                    try:
                        con.execute("""
                            INSERT INTO hive_claims
                                (id, mission_id, claim, evidence, source_url,
                                 claim_type, confidence, extracted_at)
                            VALUES (nextval('hive_facts_seq'), ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """, row)
                        saved += 1
                    except Exception as e:
                        log.debug(f"Suppressed: {e}")
        return saved

    def _save_entities(
        self,
        parsed: Any,
        op: "BeeOperation",
        doc: dict,
        con: Any,
        mission_id: str,
    ) -> int:
        """Extract entity-type information from parsed LLM output and save to hive_entities."""
        url    = doc.get("url", "")
        saved  = 0
        items: list[tuple[str, str, str, float]] = []  # (entity_type, value, context, confidence)

        def _extract_entities_from(data):
            if isinstance(data, list):
                for item in data:
                    _extract_entities_from(item)
            elif isinstance(data, dict):
                # Explicit entity fields
                if "entity_type" in data or "type" in data:
                    etype   = str(data.get("entity_type") or data.get("type", "UNKNOWN"))
                    value   = str(data.get("value") or data.get("text") or data.get("name") or data.get("canonical", ""))
                    context = str(data.get("context") or data.get("description") or "")[:500]
                    conf    = float(data.get("confidence", 0.7))
                    if value and len(value) >= 2:
                        items.append((etype.upper(), value[:500], context, conf))
                # Handle 'company' field from competitive intelligence claims
                # Schema: {claim, company, type, confidence, source_context}
                company_name = str(data.get("company") or "").strip()
                if company_name and company_name not in ("Unknown", "N/A", "unknown", "") and len(company_name) >= 2:
                    claim_ctx = str(data.get("claim") or data.get("source_context") or "")[:300]
                    conf = float(data.get("confidence", 0.7))
                    items.append(("ORG", company_name[:500], claim_ctx, conf))
                # Check for nested entity lists
                for key in ("entities", "persons", "organizations", "people", "orgs",
                            "named_entities", "items", "results"):
                    if key in data:
                        _extract_entities_from(data[key])
                # NER-specific keys from BPMN-047
                for key in ("persons", "person_names"):
                    if key in data:
                        for p in (data[key] if isinstance(data[key], list) else [data[key]]):
                            v = str(p.get("name") or p.get("canonical_form") or p.get("canonical") or p) if isinstance(p, dict) else str(p)
                            org = str(p.get("organization", "")) if isinstance(p, dict) else ""
                            role = str(p.get("role", "")) if isinstance(p, dict) else ""
                            ctx = f"{role} @ {org}".strip(" @") if (role or org) else ""
                            if v and len(v) >= 2:
                                items.append(("PERSON", v[:500], ctx[:300], 0.75))
                for key in ("organizations", "orgs", "org_names"):
                    if key in data:
                        for o in (data[key] if isinstance(data[key], list) else [data[key]]):
                            v = str(o.get("name") or o.get("canonical_form") or o.get("canonical") or o) if isinstance(o, dict) else str(o)
                            ctx = str(o.get("description") or o.get("type") or "") if isinstance(o, dict) else ""
                            if v and len(v) >= 2:
                                items.append(("ORG", v[:500], ctx[:300], 0.75))

        _extract_entities_from(parsed)

        # Batch insert with executemany for throughput
        rows_to_insert = [
            [mission_id, url, etype, value, context, round(conf, 4), _now_utc()]
            for etype, value, context, conf in items[:100]
        ]
        if rows_to_insert:
            try:
                con.executemany("""
                    INSERT INTO hive_entities
                        (id, mission_id, source_url, entity_type, value, context,
                         confidence, extracted_at)
                    VALUES (nextval('hive_entities_seq'), ?, ?, ?, ?, ?, ?, ?)
                """, rows_to_insert)
                saved = len(rows_to_insert)
            except Exception as exc:
                log.debug(f"  entity batch insert error: {exc}")
                # Fall back to row-by-row on batch failure
                for row in rows_to_insert:
                    try:
                        con.execute("""
                            INSERT INTO hive_entities
                                (id, mission_id, source_url, entity_type, value, context,
                                 confidence, extracted_at)
                            VALUES (nextval('hive_entities_seq'), ?, ?, ?, ?, ?, ?, ?)
                        """, row)
                        saved += 1
                    except Exception as exc2:
                        log.debug(f"  entity insert error: {exc2}")

        return saved

    def _save_facts(
        self,
        parsed: Any,
        op: "BeeOperation",
        doc: dict,
        con: Any,
        mission_id: str,
    ) -> int:
        """Extract numeric facts from parsed LLM output and save to hive_facts."""
        url   = doc.get("url", "")
        saved = 0
        items: list[tuple[float, str, str, float]] = []  # (value, unit, context, confidence)

        def _extract_facts_from(data):
            if isinstance(data, list):
                for item in data:
                    _extract_facts_from(item)
            elif isinstance(data, dict):
                # Explicit fact fields — handle both "value" and "raw_value" (BPMN-049)
                raw_val_key = "value" if "value" in data else ("raw_value" if "raw_value" in data else None)
                if raw_val_key and any(k in data for k in ("unit", "metric", "measure", "metric_name")):
                    try:
                        val  = float(str(data[raw_val_key]).replace(",", "").replace(" ", "").replace("%",""))
                        unit = str(data.get("unit") or data.get("metric") or data.get("measure") or data.get("metric_name") or "")[:100]
                        ctx  = str(data.get("context") or data.get("description") or data.get("what") or data.get("entity") or "")[:500]
                        conf = float(data.get("confidence", 0.6))
                        items.append((val, unit, ctx, conf))
                    except (ValueError, TypeError):
                        pass
                # Check nested fact lists
                for key in ("facts", "figures", "metrics", "numbers", "statistics",
                            "quantitative_data", "data_points", "kpis"):
                    if key in data:
                        _extract_facts_from(data[key])

        _extract_facts_from(parsed)

        # Batch insert with executemany for throughput
        rows_to_insert = []
        for value, unit, context, conf in items[:50]:
            confidence_label = "HIGH" if conf >= 0.8 else ("MEDIUM" if conf >= 0.6 else "LOW")
            rows_to_insert.append([
                mission_id, value, unit, context, 1,
                json.dumps([url]), json.dumps([_domain(url)]),
                confidence_label, False, _now_utc(),
            ])
        if rows_to_insert:
            try:
                con.executemany("""
                    INSERT INTO hive_facts
                        (id, mission_id, value, unit, context, source_count,
                         sources, domains, confidence, is_outlier, created_at)
                    VALUES (nextval('hive_facts_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, rows_to_insert)
                saved = len(rows_to_insert)
            except Exception as exc:
                log.debug(f"  facts batch insert error: {exc}")
                # Fall back to row-by-row on batch failure
                for row in rows_to_insert:
                    try:
                        con.execute("""
                            INSERT INTO hive_facts
                                (id, mission_id, value, unit, context, source_count,
                                 sources, domains, confidence, is_outlier, created_at)
                            VALUES (nextval('hive_facts_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, row)
                        saved += 1
                    except Exception as exc2:
                        log.debug(f"  fact insert error: {exc2}")

        return saved


# ---------------------------------------------------------------------------
# Fallback regex-based entity/fact extraction (when ops unavailable)
# ---------------------------------------------------------------------------

def _regex_extract_entities(text: str, url: str) -> list[dict]:
    entities = []
    sample   = text[:50_000]

    def _add(raw: str, etype: str):
        raw = raw.strip()
        if 2 <= len(raw) <= 200:
            entities.append({
                "entity_type": etype,
                "value":       raw,
                "context":     "",
                "confidence":  0.5,
                "url":         url,
            })

    for m in _RE_PERSON.finditer(sample):
        _add(m.group(1), "PERSON")
    for m in _RE_ORG.finditer(sample):
        _add(m.group(1), "ORG")
    for m in _RE_MONEY.finditer(sample):
        _add(f"{m.group(1)}{m.group(2)}", "MONEY")
    for m in _RE_PERCENT.finditer(sample):
        _add(f"{m.group(1)}{m.group(2)}", "PERCENT")

    # Deduplicate
    seen: set[tuple] = set()
    deduped = []
    for e in entities:
        k = (e["value"], e["entity_type"])
        if k not in seen:
            seen.add(k)
            deduped.append(e)
    return deduped


def _regex_extract_facts(text: str, url: str) -> list[dict]:
    facts  = []
    sample = text[:200_000]

    def _add(value_str: str, unit: str, context: str = ""):
        value_str = value_str.replace(",", "").replace(" ", "")
        try:
            value = float(value_str.replace(",", "."))
            facts.append({
                "value":      value,
                "unit":       unit.lower().strip(),
                "context":    context[:300],
                "confidence": 0.3,
                "url":        url,
            })
        except ValueError:
            pass

    for m in _RE_PERCENT_MILLION.finditer(sample):
        ctx_s = max(0, m.start() - 80)
        ctx_e = min(len(sample), m.end() + 80)
        _add(m.group(1), m.group(2), sample[ctx_s:ctx_e])

    for m in _RE_FORMATTED_NUMBER.finditer(sample):
        ctx_s = max(0, m.start() - 80)
        ctx_e = min(len(sample), m.end() + 80)
        _add(m.group(1), "count", sample[ctx_s:ctx_e])

    for m in _RE_USER_METRIC.finditer(sample):
        ctx_s = max(0, m.start() - 80)
        ctx_e = min(len(sample), m.end() + 80)
        _add(m.group(1), m.group(2), sample[ctx_s:ctx_e])

    seen: set[tuple] = set()
    deduped = []
    for f in facts:
        k = (f["value"], f["unit"])
        if k not in seen:
            seen.add(k)
            deduped.append(f)
    return deduped


# ---------------------------------------------------------------------------
# Phase B — Dedup + Validation + Gap drones
# ---------------------------------------------------------------------------

class DeduplicationDrone:
    """Merges near-duplicate entities using rapidfuzz."""

    def run(self, con: Any, mission_id: str) -> int:
        """Execute the main pipeline loop."""
        if not _rapidfuzz_available:
            log.warning("DeduplicationDrone: rapidfuzz not available, skipping")
            return 0

        try:
            rows = con.execute("""
                SELECT id, value, entity_type
                FROM hive_entities
                WHERE mission_id = ?
            """, [mission_id]).fetchall()
        except Exception as exc:
            log.error(f"DeduplicationDrone load failed: {exc}")
            return 0

        if not rows:
            return 0

        by_type: dict[str, list[tuple]] = defaultdict(list)
        for eid, value, etype in rows:
            by_type[etype].append((eid, value or ""))

        merges = 0
        for etype, items in by_type.items():
            canonical_map: dict[str, list] = {}
            assigned: set = set()

            for eid, val in items:
                if eid in assigned:
                    continue
                cluster = [eid]
                for eid2, val2 in items:
                    if eid2 == eid or eid2 in assigned:
                        continue
                    if rfuzz.token_sort_ratio(val.lower(), val2.lower()) >= DEDUP_THRESHOLD:
                        cluster.append(eid2)
                        assigned.add(eid2)
                assigned.add(eid)
                canonical_map[val] = cluster

            for canonical, cluster_ids in canonical_map.items():
                if len(cluster_ids) > 1:
                    merges += 1
                    try:
                        placeholders = ",".join("?" for _ in cluster_ids)
                        con.execute(
                            f"UPDATE hive_entities SET value = ? WHERE id IN ({placeholders})",
                            [canonical] + cluster_ids,
                        )
                    except Exception as exc:
                        log.debug(f"  dedup update failed: {exc}")

        log.info(f"DeduplicationDrone: {merges} merge clusters")
        return merges


class FactValidationDrone:
    """Cross-domain confidence scoring for numeric facts."""

    def run(self, con: Any, mission_id: str) -> int:
        """Execute the main pipeline loop."""
        try:
            rows = con.execute("""
                SELECT id, value, unit, sources
                FROM hive_facts
                WHERE mission_id = ?
            """, [mission_id]).fetchall()
        except Exception as exc:
            log.error(f"FactValidationDrone load failed: {exc}")
            return 0

        if not rows:
            return 0

        # Group by (value, unit) to count distinct domains
        vud: dict[tuple, set] = defaultdict(set)
        unit_values: dict[str, list] = defaultdict(list)

        for _, value, unit, sources_json in rows:
            if value is None:
                continue
            try:
                srcs = json.loads(sources_json) if sources_json else []
            except Exception as e:
                log.debug(f"Suppressed in process.py: {e}")
                srcs = []
            for src in srcs:
                vud[(round(float(value), 4), unit)].add(_domain(str(src)))
            unit_values[unit].append(float(value))

        updated = 0
        for fact_id, value, unit, _ in rows:
            if value is None:
                continue
            key          = (round(float(value), 4), unit)
            domain_count = len(vud.get(key, set()))

            confidence = (
                "CONFIRMED" if domain_count >= 5 else
                "HIGH"      if domain_count >= 3 else
                "MEDIUM"    if domain_count >= 2 else
                "LOW"
            )

            is_outlier = False
            vals = unit_values.get(unit, [])
            if len(vals) >= 3:
                try:
                    m = mean(vals)
                    s = stdev(vals)
                    if s > 0 and abs(float(value) - m) > 2 * s:
                        is_outlier = True
                except Exception as e:
                    log.debug(f"Suppressed: {e}")

            try:
                con.execute("""
                    UPDATE hive_facts
                    SET confidence = ?, is_outlier = ?
                    WHERE id = ?
                """, [confidence, is_outlier, fact_id])
                updated += 1
            except Exception as exc:
                log.debug(f"  fact update failed: {exc}")

        log.info(f"FactValidationDrone: {updated} facts updated")
        return updated


class ClaimCrossValidationDrone:
    """
    Cross-domain confidence upgrade dla hive_claims.

    Jeśli ta sama treść claimu (fuzzy match) pojawia się w ≥2 niezależnych domenach
    → confidence upgrade +0.15 (max 0.95), dodaje flagę 'cross_validated'.

    Niska nakładowość: działa na już zebranych claims bez nowych fetchów.
    """

    SIMILARITY_THRESHOLD = 0.72   # rapidfuzz token_set_ratio / 100
    MIN_DOMAINS          = 2
    CONFIDENCE_BOOST     = 0.15
    CONFIDENCE_MAX       = 0.95

    def run(self, con: Any, mission_id: str) -> int:
        # Dodaj evidence_flags if not istnieje
        """Execute the main pipeline loop."""
        try:
            con.execute("ALTER TABLE hive_claims ADD COLUMN IF NOT EXISTS evidence_flags VARCHAR")
        except Exception as e:
            log.debug(f"Suppressed in process.py: {e}")
            pass  # może not wspierać IF NOT EXISTS — ignoruj

        try:
            rows = con.execute("""
                SELECT id, claim, source_url, confidence
                FROM hive_claims
                WHERE mission_id = ?
                  AND (evidence_flags IS NULL OR evidence_flags NOT LIKE '%cross_validated%')
                ORDER BY confidence DESC
            """, [mission_id]).fetchall()
        except Exception as exc:
            log.error(f"ClaimCrossValidationDrone load failed: {exc}")
            return 0

        if len(rows) < 2:
            return 0

        try:
            from rapidfuzz import fuzz as _fuzz
        except ImportError:
            log.warning("ClaimCrossValidationDrone: rapidfuzz not available, skipping")
            return 0

        # Grupuj claims po domenie
        def _domain_of(url: str) -> str:
            try:
                from urllib.parse import urlparse
                return urlparse(str(url)).netloc.lower().lstrip("www.")
            except Exception as e:
                log.debug(f"Suppressed in process.py: {e}")
                return str(url)[:40]

        # Buduj indeks: claim_id → (claim_text, domain, confidence)
        claims = [(r[0], r[1] or "", _domain_of(r[2] or ""), float(r[3] or 0.5)) for r in rows]
        n = len(claims)

        # Cluster similar claims (O(n²) — acceptable for <500 claims)
        upgraded = 0
        processed_ids: set = set()

        for i in range(n):
            cid_i, text_i, dom_i, conf_i = claims[i]
            if cid_i in processed_ids:
                continue

            cluster_ids  = [cid_i]
            cluster_doms = {dom_i}

            for j in range(i + 1, n):
                cid_j, text_j, dom_j, conf_j = claims[j]
                if cid_j in processed_ids:
                    continue
                # Fuzzy match — token set ratio (handles different word order)
                sim = _fuzz.token_set_ratio(text_i, text_j) / 100.0
                if sim >= self.SIMILARITY_THRESHOLD:
                    cluster_ids.append(cid_j)
                    cluster_doms.add(dom_j)

            if len(cluster_doms) >= self.MIN_DOMAINS:
                # Upgrade wszystkich w klastrze
                new_conf = min(self.CONFIDENCE_MAX, conf_i + self.CONFIDENCE_BOOST)
                for cid in cluster_ids:
                    try:
                        con.execute("""
                            UPDATE hive_claims
                            SET confidence = ?,
                                evidence_flags = COALESCE(evidence_flags || ',', '') || ?
                            WHERE id = ?
                        """, [new_conf, f"cross_validated:{len(cluster_doms)}domains", cid])
                        upgraded += 1
                        processed_ids.add(cid)
                    except Exception as exc:
                        log.debug(f"  claim cross-val update failed: {exc}")

        log.info(f"ClaimCrossValidationDrone: {upgraded} claims upgraded across {len(processed_ids)} ids")
        return upgraded


class GapDrone:
    """Generates research gap analysis using Bedrock."""

    def run(self, con: Any, mission_id: str, topic: str) -> list[dict]:
        """Execute the main pipeline loop."""
        if not _boto3_available:
            log.warning("GapDrone: boto3 not available, skipping")
            return []

        try:
            entity_rows = con.execute("""
                SELECT value, entity_type, COUNT(*) AS cnt
                FROM hive_entities
                WHERE mission_id = ?
                GROUP BY value, entity_type
                ORDER BY cnt DESC LIMIT 20
            """, [mission_id]).fetchall()
        except Exception as e:
            log.debug(f"Suppressed in process.py: {e}")
            entity_rows = []

        try:
            fact_rows = con.execute("""
                SELECT claim, claim_type, confidence
                FROM hive_claims
                WHERE mission_id = ? AND confidence >= 0.7
                ORDER BY confidence DESC
                LIMIT 10
            """, [mission_id]).fetchall()
        except Exception as e:
            log.debug(f"Suppressed in process.py: {e}")
            fact_rows = []

        top_entities    = ", ".join(f"{r[0]} ({r[1]})" for r in entity_rows) or "none"
        confirmed_facts = "; ".join(f"{r[0][:80]} [{r[1]}]" for r in fact_rows) or "none"

        prompt = (
            f"You are an intelligence analyst. Research topic: {topic}\n\n"
            f"Top entities: {top_entities}\n"
            f"Confirmed facts: {confirmed_facts}\n\n"
            "Identify 5 critical research gaps — questions the sources DON'T answer. "
            "Respond as JSON array: [{\"gap_number\":1,\"gap_title\":\"...\","
            "\"gap_description\":\"...\",\"priority\":\"HIGH|MEDIUM|LOW\"}]"
        )

        try:
            text      = _bedrock_client.invoke(prompt, max_tokens=1024)
            gaps_raw  = _extract_json_from_text(text) or []
        except Exception as exc:
            log.warning(f"GapDrone Bedrock call failed: {exc}")
            gaps_raw = [
                {"gap_number": i, "gap_title": f"Research Gap {i}",
                 "gap_description": "Insufficient data.", "priority": "MEDIUM"}
                for i in range(1, 6)
            ]

        gaps   = []
        saved  = 0
        # Batch insert gaps with executemany for throughput
        gap_rows = []
        for g in gaps_raw[:5]:
            gap_rows.append([
                mission_id,
                f"{g.get('gap_title','Gap')}: {g.get('gap_description','')}",
                "research",
                {"HIGH": 8, "MEDIUM": 5, "LOW": 3}.get(g.get("priority","MEDIUM"), 5),
            ])
            gaps.append(g)
        if gap_rows:
            try:
                con.executemany("""
                    INSERT INTO hive_gaps (mission_id, gap_query, gap_type, priority)
                    VALUES (?, ?, ?, ?)
                """, gap_rows)
                saved = len(gap_rows)
            except Exception as exc:
                log.debug(f"  gap batch save error: {exc}")
                # Fall back to row-by-row
                saved = 0
                gaps = []
                for g, row in zip(gaps_raw[:5], gap_rows):
                    try:
                        con.execute("""
                            INSERT INTO hive_gaps (mission_id, gap_query, gap_type, priority)
                            VALUES (?, ?, ?, ?)
                        """, row)
                        saved += 1
                        gaps.append(g)
                    except Exception as exc2:
                        log.debug(f"  gap save error: {exc2}")

        log.info(f"GapDrone: {saved} gaps saved")
        return gaps


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

class ProcessingDrones:
    """
    Orchestrates the full per-document BPMN pipeline + global phase-B drones.
    Backward compatible entry point called by hive2.py orchestrator.
    """

    def __init__(self, mission_id: str, relevance_threshold: float = 0.45,
                 max_ops: int = 5, debug: bool = False):
        self.mission_id          = mission_id
        self.relevance_threshold = relevance_threshold
        self.max_ops             = max_ops
        self.debug               = debug
        self.con                 = _db_connect_retry(DB_PATH)  # Routes to PostgreSQL via hive2_db
        self._ensure_schema()

    def _ensure_schema(self):
        """Create hive_bee_results table if it doesn't exist."""
        try:
            self.con.execute("""
                CREATE TABLE IF NOT EXISTS hive_bee_results (
                    id            VARCHAR PRIMARY KEY,
                    mission_id    VARCHAR,
                    doc_id        VARCHAR,
                    operation_id  VARCHAR,
                    operation_name VARCHAR,
                    tier          INTEGER,
                    relevance_score FLOAT,
                    output_json   VARCHAR,
                    confidence    FLOAT,
                    processing_ms INTEGER,
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        except Exception as exc:
            log.warning(f"Schema ensure error: {exc}")

    def run(self) -> dict:
        """Synchronous entry point — wraps the async pipeline."""
        asyncio.run(self._run_async())

    async def _run_async(self):
        log.info(f"ProcessingDrones starting — mission={self.mission_id}")
        if not _ops_available:
            log.warning("hive2_ops not available — falling back to regex extraction only")

        # Eagerly probe Bedrock model ONCE before parallel ops begin
        if _boto3_available:
            try:
                _bedrock._confirm_fast_model()
            except NameError:
                pass  # _bedrock not initialized in this context — skip probe

        # Load documents
        docs = self._load_content()
        if not docs:
            log.warning("No content documents found for this mission.")
            self._finalize(0, 0, 0, 0, 0)
            return

        # Pre-cap: if > 500 docs, take top 500 by word_count (quality proxy)
        # Saves Bedrock TPM in RelevanceFilter
        PRE_CAP = 500
        if len(docs) > PRE_CAP:
            docs.sort(key=lambda d: d.get('word_count', 0), reverse=True)
            log.info(f"  Pre-cap: {len(docs)} docs → top {PRE_CAP} by word_count")
            docs = docs[:PRE_CAP]

        topic = self._get_mission_topic()
        log.info(f"Topic: '{topic}' | {len(docs)} docs to process")

        # -----------------------------------------------
        # Phase A0 — API Scout: structured data sources
        # -----------------------------------------------
        try:
            from hive_api_scout import scout_apis
            api_docs = await scout_apis(topic, max_results=10, timeout_per_api=12)
            if api_docs:
                # API docs get a relevance boost (structured = high signal)
                for d in api_docs:
                    d['relevance_score'] = 0.9  # pre-scored high
                    d['_from_api'] = True
                log.info(f"Phase A0: API Scout returned {len(api_docs)} structured docs")
                docs = api_docs + docs  # Prepend — they'll survive relevance filter
        except Exception as e:
            log.debug(f"Phase A0: API Scout skipped ({e})")

        # -----------------------------------------------
        # Phase A1 — Relevance filtering
        # -----------------------------------------------
        log.info(f"Phase A1: RelevanceFilter (threshold={self.relevance_threshold})")
        rel_filter  = RelevanceFilter(topic, self.relevance_threshold, self.debug)
        scored_docs = await rel_filter.filter(docs)

        if not scored_docs:
            log.warning("All documents filtered out by relevance. Raising threshold? Exiting.")
            self._finalize(0, 0, 0, 0, 0)
            return

        # Cap at 300 docs (top relevance) to keep process time < 90 min
        MAX_PROCESS_DOCS = 80  # 80 top-scored × 10 ops = 800 tasks → ~400s process
        if len(scored_docs) > MAX_PROCESS_DOCS:
            scored_docs.sort(key=lambda d: d.get('relevance_score', 0), reverse=True)
            log.info(f"  Capping {len(scored_docs)} docs → top {MAX_PROCESS_DOCS} by relevance score")
            scored_docs = scored_docs[:MAX_PROCESS_DOCS]

        # -----------------------------------------------
        # Phase A2 — BPMN routing + BeeWorker execution
        # -----------------------------------------------
        log.info(f"Phase A2: BPMN routing + BeeWorker execution on {len(scored_docs)} docs")
        # Gen3 EpistemicOperationRouter — CredibilityBee gate + FalsificationBee
        try:
            import importlib.util as _g3ilu
            _g3spec = _g3ilu.spec_from_file_location("hive3_bees_gen3", "hive3_bees_gen3.py")
            _g3mod  = _g3ilu.module_from_spec(_g3spec)
            _g3spec.loader.exec_module(_g3mod)
            router = _g3mod.EpistemicOperationRouter(max_ops=self.max_ops, debug=self.debug)
            log.info("🔬 EpistemicOperationRouter (Gen3) aktywny — FalsificationBee + CredibilityBee")
        except Exception as _g3e:
            router = OperationRouter(max_ops=self.max_ops, debug=self.debug)
            log.debug(f"Gen3 router fallback do OperationRouter: {_g3e}")
        worker    = BeeWorker(topic=topic, debug=self.debug)
        
        # HIVE 3.0: Specialized Bee Hive (replaces generic BeeWorker when available)
        # Set HIVE_USE_BATCH=1 env var to force legacy path → BeeOpCollector (Opcja C)
        _force_batch = os.environ.get("HIVE_USE_BATCH", "0") == "1"
        try:
            from hive3_bees import BeeHive
            hive = BeeHive(topic=topic, mission_id=self.mission_id, debug=self.debug)
            use_hive3 = not _force_batch
            # Register Gen2 bees (Temporal, Linguistic, Forensic, Geopolitical, Financial, etc.)
            try:
                from hive3_bees_gen2 import register_gen2_bees
                total_bee_types = register_gen2_bees()
                log.info(f"  🧬 HIVE 3.0 BeeHive activated — {total_bee_types} bee types online (Gen1+Gen2)")
            except ImportError:
                log.info("  🧬 HIVE 3.0 BeeHive activated — Gen1 bees online")
        except ImportError:
            hive = None
            use_hive3 = False

        total_entities = 0
        total_facts    = 0
        total_bee_ops  = 0

        # ── HIVE 3.0 path: use BeeHive (tiered cascade, sequential per-doc) ──
        if use_hive3:
            log.info(f"Phase A2 [HIVE 3.0]: processing {len(scored_docs)} docs via BeeHive")
            _total_docs = len(scored_docs)
            _last_pct = 0
            _docs_done = 0
            for batch_start in range(0, _total_docs, BATCH_SIZE):
                batch = scored_docs[batch_start: batch_start + BATCH_SIZE]

                log.info(
                    f"  batch {batch_start // BATCH_SIZE + 1}: "
                    f"docs {batch_start+1}–{batch_start+len(batch)}"
                )
                batch_tasks = []
                for doc in batch:
                    ops = router.route(doc)
                    if ops:
                        batch_tasks.append(hive.process_doc(doc, ops, self.con))
                    else:
                        batch_tasks.append(self._fallback_extract(doc))

                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                for res in batch_results:
                    if isinstance(res, Exception):
                        log.warning(f"  batch task error: {res}")
                    elif isinstance(res, dict):
                        total_entities += res.get("entities", 0)
                        total_facts    += res.get("facts", 0)
                        total_bee_ops  += res.get("ops_run", 0)

                # ── Checkpoint co 5% ──
                _docs_done += len(batch)
                _cur_pct = int(100 * _docs_done / _total_docs) if _total_docs else 100
                if _cur_pct >= _last_pct + 5 or _docs_done == len(batch):
                    log.debug(f"  ⚙️  Process: {_cur_pct}% ({_docs_done}/{_total_docs} docs, {total_facts} claims, {total_entities} entities)")
                    _last_pct = _cur_pct
                    # Emit progress event for SSE
                    try:
                        from hive2_events import emit_event
                        emit_event(None, mission_id, 'process', 'progress', data={
                            'pct': _cur_pct,
                            'docs_done': _docs_done,
                            'docs_total': _total_docs,
                            'claims': total_facts,
                            'entities': total_entities,
                        })
                    except Exception as e:
                        log.debug(f"Suppressed: {e}")

        # ── Legacy path: BeeOpCollector — SGLang array batch (Opcja C) ──
        else:
            log.info(f"Phase A2 [Batch]: {len(scored_docs)} docs → SGLang array batch")
            try:
                from hive3_batch_client import SGLangBatchClient
                from hive3_bee_collector import BeeOpCollector

                batch_client = SGLangBatchClient()
                collector    = BeeOpCollector()

                cstats = await collector.process_docs_batch(
                    docs=scored_docs,
                    router=router,
                    worker=worker,
                    con=self.con,
                    mission_id=self.mission_id,
                    batch_client=batch_client,
                    max_batch_size=200,
                )
                total_entities = cstats.entities_saved
                total_facts    = cstats.facts_saved
                total_bee_ops  = cstats.ops_success
                log.info(f"Phase A2 [Batch] done: {cstats.summary()}")

            except Exception as e:
                log.warning(f"  BeeOpCollector failed ({e}) — fallback do sequential gather")
                # Fallback: original sequential loop
                for batch_start in range(0, len(scored_docs), BATCH_SIZE):
                    batch = scored_docs[batch_start: batch_start + BATCH_SIZE]
                    log.info(
                        f"  [fallback] batch {batch_start // BATCH_SIZE + 1}: "
                        f"docs {batch_start+1}–{batch_start+len(batch)}"
                    )
                    batch_tasks = []
                    for doc in batch:
                        ops = router.route(doc)
                        if ops:
                            batch_tasks.append(
                                self._process_doc_with_ops(doc, ops, worker, topic)
                            )
                        else:
                            batch_tasks.append(self._fallback_extract(doc))

                    batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                    for res in batch_results:
                        if isinstance(res, Exception):
                            log.warning(f"  batch task error: {res}")
                        elif isinstance(res, dict):
                            total_entities += res.get("entities", 0)
                            total_facts    += res.get("facts", 0)
                            total_bee_ops  += res.get("ops_run", 0)

        log.info(
            f"Phase A done: {total_entities} entities, {total_facts} facts, "
            f"{total_bee_ops} operation executions"
        )
        
        # -----------------------------------------------
        # Phase A3 — V4 Bedrock Extraction (dedicated claim extraction)
        # -----------------------------------------------
        v4_claims = 0
        try:
            from hive4_extraction import HiveExtractor
            extractor = HiveExtractor()
            
            # Only extract from docs that haven't produced claims yet
            # (BeeHive may have already extracted some via ScoutBee/ValidatorBee)
            existing_claims = 0
            try:
                self.con.execute(
                    "SELECT count(*) FROM hive_claims WHERE mission_id = ?",
                    [self.mission_id]
                )
                existing_claims = self.con.fetchone()[0]
            except (OSError, Exception) as e:  # network/HTTP
                log.debug(f"HTTP error: {e}")
            
            # Always run V4 extraction — produces consistently higher quality claims
            if existing_claims < 800:  # Always run V4 — its claims are higher quality
                # Take top 60 docs by relevance for dedicated extraction
                extract_docs = scored_docs[:60]
                for d in extract_docs:
                    d["mission_id"] = self.mission_id
                
                log.info(f"Phase A3 [V4 Extraction]: {len(extract_docs)} docs → Bedrock Haiku")
                results = await extractor.extract_batch(extract_docs, enrich=True)
                
                # Save to hive_claims
                try:
                    from hive3_bees import SpecializedBee
                    _has_bee = True
                except ImportError:
                    _has_bee = False
                for r in results:
                    try:
                        quality = SpecializedBee._score_claim_quality(r.claim) if _has_bee else 0.7
                        if quality >= 0.65:
                            self.con.execute("""
                                INSERT INTO hive_claims
                                    (id, mission_id, claim, source_url, claim_type, 
                                     confidence, quality_score, extracted_at)
                                VALUES (nextval('hive_facts_seq'), ?, ?, ?, 'v4_extraction',
                                        ?, ?, CURRENT_TIMESTAMP)
                            """, [self.mission_id, r.claim, r.source_url, 
                                  round(r.confidence, 4), round(quality, 4)])
                            v4_claims += 1
                    except Exception as exc:
                        log.debug(f"V4 claim insert error: {exc}")
                
                log.info(f"Phase A3 done: {v4_claims} high-quality claims (Bedrock Haiku + Sonnet enrichment)")
                log.info(f"  Extractor stats: {extractor.stats}")
                # Emit V4 extraction event for SSE
                try:
                    from hive2_events import emit_event
                    emit_event(None, mission_id, 'process', 'v4_extraction', data={
                        'claims': v4_claims,
                        'model': 'bedrock-haiku+sonnet',
                    })
                except Exception as e:
                    log.debug(f"Suppressed: {e}")
            else:
                log.info(f"Phase A3 skipped — BeeHive already produced {existing_claims} claims")
        except Exception as e:
            log.warning(f"Phase A3 [V4 Extraction] error: {e}")
        
        total_facts += v4_claims
        
        # HIVE 3.0: Log specialized bee stats
        if use_hive3 and hive:
            stats = hive.get_hive_stats()
            for tier, info in stats.get("bee_details", {}).items():
                log.info(f"  {info['emoji']} {info['name']}: {info['ops']} ops, "
                         f"{info['entities']} entities, memory={info['memory_size']}")
            # Inject collective memory into Queen context
            collective = hive.get_collective_memory()
            if collective:
                try:
                    from hive3_cascade import _BEE_COLLECTIVE_MEMORY
                except ImportError:
                    pass
                # Store for Queen to access
                self._collective_bee_memory = collective

        # -----------------------------------------------
        # Phase B — Dedup + Validation + Gap analysis
        # -----------------------------------------------
        log.info("Phase B: DeduplicationDrone")
        clusters = DeduplicationDrone().run(self.con, self.mission_id)

        log.info("Phase B: FactValidationDrone")
        validated = FactValidationDrone().run(self.con, self.mission_id)

        log.info("Phase B: ClaimCrossValidationDrone")
        cross_validated = ClaimCrossValidationDrone().run(self.con, self.mission_id)

        log.info("Phase B: GapDrone")
        gaps = GapDrone().run(self.con, self.mission_id, topic)

        # -----------------------------------------------
        # Finalize
        # -----------------------------------------------
        self._finalize(
            sources_processed=len(scored_docs),
            total_entities=total_entities,
            total_facts=total_facts,
            clusters=clusters,
            gaps=len(gaps),
        )
        self._print_summary(
            docs_input=len(docs),
            docs_processed=len(scored_docs),
            total_entities=total_entities,
            total_facts=total_facts,
            clusters=clusters,
            gaps=len(gaps),
            bee_ops=total_bee_ops,
        )

    async def _process_doc_with_ops(
        self,
        doc: dict,
        ops: list,
        worker: BeeWorker,
        topic: str,
    ) -> dict:
        """Run all selected ops for a doc in parallel; return entity/fact totals."""
        tasks = [
            worker.execute(op, doc, self.con, self.mission_id)
            for op in ops
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        entities = facts = ops_run = 0
        for r in results:
            if isinstance(r, Exception):
                log.debug(f"  op error: {r}")
            elif isinstance(r, dict) and r.get("success"):
                entities += r.get("entities_saved", 0)
                facts    += r.get("facts_saved", 0)
                ops_run  += 1

        return {"entities": entities, "facts": facts, "ops_run": ops_run}

    async def _fallback_extract(self, doc: dict) -> dict:
        """Regex-only fallback when ops registry is unavailable."""
        url  = doc.get("url", "")
        text = doc.get("raw_text", "") or ""

        entities = _regex_extract_entities(text, url)
        facts    = _regex_extract_facts(text, url)

        ent_saved = 0
        # Batch insert entities with executemany for throughput
        ent_rows = [
            [self.mission_id, url,
             ent["entity_type"], ent["value"], ent["context"],
             round(ent["confidence"], 4), _now_utc()]
            for ent in entities
        ]
        if ent_rows:
            try:
                self.con.executemany("""
                    INSERT INTO hive_entities
                        (id, mission_id, source_url, entity_type, value, context,
                         confidence, extracted_at)
                    VALUES (nextval('hive_entities_seq'), ?, ?, ?, ?, ?, ?, ?)
                """, ent_rows)
                ent_saved = len(ent_rows)
            except Exception as exc:
                log.debug(f"  fallback entity batch save error: {exc}")
                for row in ent_rows:
                    try:
                        self.con.execute("""
                            INSERT INTO hive_entities
                                (id, mission_id, source_url, entity_type, value, context,
                                 confidence, extracted_at)
                            VALUES (nextval('hive_entities_seq'), ?, ?, ?, ?, ?, ?, ?)
                        """, row)
                        ent_saved += 1
                    except Exception as exc2:
                        log.debug(f"  fallback entity save error: {exc2}")

        fact_saved = 0
        # Batch insert facts with executemany for throughput
        fact_rows = [
            [self.mission_id, f["value"], f["unit"], f["context"], 1,
             json.dumps([url]), json.dumps([_domain(url)]),
             "LOW", False, _now_utc()]
            for f in facts
        ]
        if fact_rows:
            try:
                self.con.executemany("""
                    INSERT INTO hive_facts
                        (id, mission_id, value, unit, context, source_count,
                         sources, domains, confidence, is_outlier, created_at)
                    VALUES (nextval('hive_facts_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, fact_rows)
                fact_saved = len(fact_rows)
            except Exception as exc:
                log.debug(f"  fallback fact batch save error: {exc}")
                for row in fact_rows:
                    try:
                        self.con.execute("""
                            INSERT INTO hive_facts
                                (id, mission_id, value, unit, context, source_count,
                                 sources, domains, confidence, is_outlier, created_at)
                            VALUES (nextval('hive_facts_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, row)
                        fact_saved += 1
                    except Exception as exc2:
                        log.debug(f"  fallback fact save error: {exc2}")

        return {"entities": ent_saved, "facts": fact_saved, "ops_run": 0}

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _load_content(self) -> list[dict]:
        try:
            rows = self.con.execute("""
                SELECT id, url, domain, title, raw_text, word_count, language, harvest_method
                FROM hive_content
                WHERE mission_id = ?
                ORDER BY quality_score DESC NULLS LAST
            """, [self.mission_id]).fetchall()
            cols = ["source_id", "url", "domain", "title", "raw_text",
                    "word_count", "language", "harvest_method"]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            log.error(f"Failed to load content: {exc}")
            return []

    def _get_mission_topic(self) -> str:
        try:
            row = self.con.execute(
                "SELECT topic FROM hive_missions WHERE id = ?",
                [self.mission_id]
            ).fetchone()
            return row[0] if row else "Unknown topic"
        except Exception as e:
            log.debug(f"Suppressed in process.py: {e}")
            return "Unknown topic"

    def _finalize(
        self,
        sources_processed: int,
        total_entities: int,
        total_facts: int,
        clusters: int,
        gaps: int,
    ):
        try:
            self.con.execute("""
                UPDATE hive_missions
                SET status = 'synth',
                    sources_processed = ?,
                    total_entities = ?,
                    total_facts = ?,
                    process_done_at = ?
                WHERE id = ?
            """, [
                sources_processed, total_entities, total_facts,
                _now_utc(), self.mission_id,
            ])
        except Exception as exc:
            log.warning(f"Could not update hive_missions: {exc}")

        # ── Knowledge Graph ingest (Neo4j) ────────────────────────────────────
        try:
            from hive4_knowledge_graph import ingest_from_db
            kg_result = ingest_from_db(self.mission_id)
            log.info(f"KG ingest: +{kg_result.get('entities_created', 0)} entities, "
                     f"+{kg_result.get('relationships_created', 0)} relationships")
        except Exception as e:
            log.debug(f"KG ingest skipped: {e}")

        # ── Topic Clustering hook ─────────────────────────────────────────────
        try:
            import importlib.util as _clu
            _cspec = _clu.spec_from_file_location("hive2_cluster", "hive2_cluster.py")
            _cmod  = _clu.module_from_spec(_cspec)
            _cspec.loader.exec_module(_cmod)
            topic_str = None
            try:
                row = self.con.execute(
                    "SELECT topic FROM hive_missions WHERE id = ?", [self.mission_id]
                ).fetchone()
                topic_str = row[0] if row else None
                self.con.close()
            except (OSError, Exception) as e:  # network/HTTP
                log.debug(f"HTTP error: {e}")
            if topic_str:
                cr = _cmod.post_process_hook(self.mission_id, topic_str)
                log.info(
                    f"🍯 ClusterPipeline: {cr.get('clusters', 0)} klastrów "
                    f"dla misji {self.mission_id}"
                )
        except Exception as _ce:
            log.debug(f"ClusterPipeline hook pominięty: {_ce}")
    def _print_summary(
        self,
        docs_input: int,
        docs_processed: int,
        total_entities: int,
        total_facts: int,
        clusters: int,
        gaps: int,
        bee_ops: int,
    ):
        filtered = docs_input - docs_processed
        log.debug("\n" + "=" * 65)
        log.debug(f"  HIVE 2.0 BPMN PROCESS SUMMARY — Mission: {self.mission_id}")
        log.debug("=" * 65)
        log.debug(f"  Documents input     : {docs_input}")
        log.debug(f"  Docs filtered out   : {filtered}  (relevance < {self.relevance_threshold})")
        # Explicitly close DuckDB to release write lock before exit
        try:
            self.con.close()
        except Exception as e:
            log.debug(f"Suppressed: {e}")

        log.debug(f"  Documents processed : {docs_processed}")
        log.debug(f"  BPMN op executions  : {bee_ops}")
        log.debug(f"  Entities extracted  : {total_entities}")
        log.debug(f"  Facts extracted     : {total_facts}")
        log.debug(f"  Dedup clusters      : {clusters}")
        log.debug(f"  Research gaps       : {gaps}")
        log.debug("=" * 65 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="HIVE 3.0 BPMN ProcessingDrones — transform raw content into intelligence"
    )
    parser.add_argument("mission_id", help="Mission ID to process")
    parser.add_argument(
        "--relevance-threshold", type=float, default=0.25,
        help="Minimum relevance score (0.0–1.0) to process a document (default: 0.25)"
    )
    parser.add_argument(
        "--max-ops", type=int, default=5,
        help="Maximum BPMN operations per document (default: 5)"
    )
    parser.add_argument(
        "--deep", action="store_true",
        help="Deep cascade mode: activate all biomimetic tiers (max_ops=50)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--no-filter", action="store_true",
        help="Skip RelevanceFilter (process all docs, saves Bedrock TPM)"
    )
    args = parser.parse_args()

    # Deep mode overrides max_ops to utilize full biomimetic cascade
    if args.deep:
        args.max_ops = 5    # depth=1: 5 ops×doc — BPMN-CI1+047+048+049+800, ~200s process

    if args.debug:
        # Only raise our own logger to DEBUG; leave boto3/botocore at WARNING
        log.setLevel(logging.DEBUG)
        logging.getLogger("hive2").setLevel(logging.DEBUG)
        # Suppress boto3/botocore HTTP-level noise
        logging.getLogger("boto3").setLevel(logging.WARNING)
        logging.getLogger("botocore").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

    drones = ProcessingDrones(
        mission_id=args.mission_id,
        relevance_threshold=0.0 if getattr(args, 'no_filter', False) else args.relevance_threshold,
        max_ops=args.max_ops,
        debug=args.debug,
    )
    drones.run()


if __name__ == "__main__":
    main()
