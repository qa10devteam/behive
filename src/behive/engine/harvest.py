#!/usr/bin/env python3
"""
HIVE 2.0 — HarvesterBee
Fetches raw content from hive_sources and writes to hive_content.

Usage: python3 hive2_harvest.py <mission_id> [--workers N]
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# ALL optional/heavy imports are now LAZY — loaded on first use only.
# This keeps module-level import time < 0.05 s.
#
# Lazily loaded:
#   pg, requests, fitz (pymupdf),
#   curl_cffi, httpx, newspaper, trafilatura, langdetect, markitdown, crawl4ai
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = ""  # Legacy, unused — Postgres is primary

NEWS_DOMAINS = {
    "reuters.com", "bloomberg.com", "bbc.com", "ft.com", "wsj.com",
    "nytimes.com", "theguardian.com", "cnbc.com", "techcrunch.com",
    "gazeta.pl", "wyborcza.pl", "onet.pl", "wp.pl",
    "hurriyet.com.tr", "sabah.com.tr", "milliyet.com.tr",
    "bianet.org", "civil.ge", "agenda.ge", "interpressnews.ge",
}

DEFAULT_WORKERS = 5
JINA_BASE = "https://r.jina.ai/"
REQUEST_TIMEOUT = 30
# One legal-metadata fetch (CELEX / ELI / print) before the relevance filter; also capped by
# the phase deadline (llm.deadline_call_timeout).
LEGAL_RESCORE_FETCH_TIMEOUT_S = 30.0
# One legal-act fetch in _harvest_one (CELLAR / ELI / print; a 700 KB act body is one call);
# also capped by the phase deadline.
LEGAL_FETCH_TIMEOUT_S = 60.0
# Routes of route_legal() that fetch an act through the L1 fetchers.
_LEGAL_ROUTES = ("cellar", "eli", "sejm_print")
# LEX (Wolters Kluwer SIP) is login-only: BeHive never fetches it and never logs in.
_LEX_HOST = "sip.lex.pl"
# Fine-grained timeout for new aiohttp/httpx calls: connect=3s, read=8s
_CONNECT_TIMEOUT = 3
_READ_TIMEOUT = 8

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hive2.harvest")


class _VisibleHTMLParser(HTMLParser):
    """Dependency-free text fallback; never treat executable/hidden bodies as prose."""

    _HIDDEN = frozenset({'script', 'style', 'noscript', 'svg', 'template'})
    _BREAKS = frozenset({
        'address', 'article', 'aside', 'blockquote', 'br', 'dd', 'div', 'dl',
        'dt', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr',
        'li', 'main', 'nav', 'ol', 'p', 'pre', 'section', 'table', 'td', 'th',
        'title', 'tr', 'ul',
    })

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._HIDDEN:
            self.hidden.append(tag)
        if tag in self._BREAKS or tag in self._HIDDEN:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in self.hidden:
            # Also unwind malformed, unclosed nested hidden elements.
            del self.hidden[len(self.hidden) - 1 - self.hidden[::-1].index(tag):]
        if tag in self._BREAKS or tag in self._HIDDEN:
            self.parts.append(' ')

    def handle_startendtag(self, tag, attrs):
        if tag in self._BREAKS:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    """Discard comments/hidden bodies, decode entities once, normalize whitespace."""
    parser = _VisibleHTMLParser()
    parser.feed(html)
    parser.close()
    return ' '.join(''.join(parser.parts).split())


def _web_text_error(text: str) -> str | None:
    """Reject empty shells and short challenge/denial pages, not articles about them."""
    words = re.findall(r'[^\W_]+', text, flags=re.UNICODE)
    if len(words) <= 120 and re.search(
        r'just a moment|attention required|access denied|enable javascript',
        text, flags=re.IGNORECASE,
    ):
        return 'blocked_web_page'
    if len(words) < 3:
        return 'insufficient_web_text'
    return None


# ---------------------------------------------------------------------------
# In-memory URL cache (skip re-fetching same URL within a mission run)
# ---------------------------------------------------------------------------
_url_cache: set = set()
_URL_CACHE_MAX = 500


def _is_url_cached(url: str) -> bool:
    return url in _url_cache


def _cache_url(url: str) -> None:
    if len(_url_cache) < _URL_CACHE_MAX:
        _url_cache.add(url)


def clear_url_cache() -> None:
    """Clear the in-memory URL cache (e.g. between missions)."""
    _url_cache.clear()


# ---------------------------------------------------------------------------
# Skip log — one INFO line per skipped source, one summary per mission
# ---------------------------------------------------------------------------
# Fixed reason codes; never free text. byok_gap (LEX, no login) and legal_fetch_empty
# (CELLAR / ELI / print returned nothing) are emitted by the legal dispatch.
SKIP_REASONS = (
    "score_filter",
    "domain_blacklist",
    "prescout_skip",
    "drone_skip",
    "cache_skip",
    "byok_gap",
    "legal_fetch_empty",
)
# Codes always shown in skip_summary (stable line shape); the rest only when counted.
_SUMMARY_REASONS = SKIP_REASONS[:5]


def note_skip(mission_id: str, url: str, reason: str, detail: str = "",
              counts: "dict[str, int] | None" = None) -> None:
    """Log one INFO line for a skipped source and bump counts[reason]."""
    if reason not in SKIP_REASONS:
        raise ValueError(f"unknown skip reason: {reason!r}")
    detail = " ".join(str(detail).split())
    log.info("skip mission=%s reason=%s url=%s detail=%s", mission_id, reason, url, detail)
    if counts is not None:
        counts[reason] = counts.get(reason, 0) + 1


def skip_summary(mission_id: str, counts: "dict[str, int]", loaded: int, groomed: int) -> None:
    """Log one INFO summary line per mission; skipped is the sum of the per-reason counts."""
    reasons = list(_SUMMARY_REASONS) + [r for r in SKIP_REASONS[5:] if counts.get(r)]
    parts = " ".join(f"{r}={counts.get(r, 0)}" for r in reasons)
    log.info(
        "skip_summary mission=%s loaded=%s groomed=%s skipped=%s %s",
        mission_id, loaded, groomed, sum(counts.values()), parts,
    )


# ---------------------------------------------------------------------------
# Lazy import helpers — cached after first successful import
# ---------------------------------------------------------------------------

# -- curl_cffi --
_curl_cffi_cls = None
_curl_cffi_checked = False


def _get_curl_cffi_session_cls():
    global _curl_cffi_cls, _curl_cffi_checked
    if not _curl_cffi_checked:
        try:
            from curl_cffi.requests import AsyncSession as _CurlAsyncSession
            _curl_cffi_cls = _CurlAsyncSession
        except ImportError:
            pass
        _curl_cffi_checked = True
    return _curl_cffi_cls


# -- httpx --
_httpx_mod = None
_httpx_checked = False


def _get_httpx():
    global _httpx_mod, _httpx_checked
    if not _httpx_checked:
        try:
            import httpx as _httpx
            _httpx_mod = _httpx
        except ImportError:
            pass
        _httpx_checked = True
    return _httpx_mod


# -- trafilatura --
_trafilatura_mod = None
_trafilatura_checked = False


def _get_trafilatura():
    global _trafilatura_mod, _trafilatura_checked
    if not _trafilatura_checked:
        try:
            import trafilatura as _t
            _trafilatura_mod = _t
        except ImportError:
            pass
        _trafilatura_checked = True
    return _trafilatura_mod


# -- newspaper --
_newspaper_mod = None
_newspaper_checked = False


def _get_newspaper():
    global _newspaper_mod, _newspaper_checked
    if not _newspaper_checked:
        try:
            import newspaper as _n
            _newspaper_mod = _n
        except ImportError:
            pass
        _newspaper_checked = True
    return _newspaper_mod


# -- crawl4ai --
_crawl4ai_cls = None
_crawl4ai_cache_mode = None
_crawl4ai_checked = False


def _get_crawl4ai():
    global _crawl4ai_cls, _crawl4ai_cache_mode, _crawl4ai_checked
    if not _crawl4ai_checked:
        try:
            from crawl4ai import AsyncWebCrawler as _AWC, CacheMode as _CM
            _crawl4ai_cls = _AWC
            _crawl4ai_cache_mode = _CM
        except ImportError:
            pass
        _crawl4ai_checked = True
    return _crawl4ai_cls, _crawl4ai_cache_mode


# -- langdetect --
_langdetect_fn = None
_langdetect_checked = False


def _get_langdetect():
    global _langdetect_fn, _langdetect_checked
    if not _langdetect_checked:
        try:
            from langdetect import detect as _detect
            _langdetect_fn = _detect
        except ImportError:
            pass
        _langdetect_checked = True
    return _langdetect_fn


# -- markitdown --
_markitdown_cls = None
_markitdown_checked = False


def _get_markitdown():
    global _markitdown_cls, _markitdown_checked
    if not _markitdown_checked:
        try:
            from markitdown import MarkItDown as _MD
            _markitdown_cls = _MD
        except ImportError:
            pass
        _markitdown_checked = True
    return _markitdown_cls


# -- database --
from behive.engine.db import connect as _db_connect


def _get_db_connection(read_only: bool = False):
    """Get database connection via unified hive2_db layer (PostgreSQL)."""
    return _db_connect(read_only=read_only)


# -- requests --
_requests_mod = None
_requests_checked = False


def _get_requests():
    global _requests_mod, _requests_checked
    if not _requests_checked:
        try:
            import requests as _req
            _requests_mod = _req
        except ImportError:
            pass
        _requests_checked = True
    return _requests_mod


# -- fitz (pymupdf) --
_fitz_mod = None
_fitz_checked = False


def _get_fitz():
    global _fitz_mod, _fitz_checked
    if not _fitz_checked:
        try:
            import fitz as _fitz
            _fitz_mod = _fitz
        except ImportError:
            pass
        _fitz_checked = True
    return _fitz_mod


# ---------------------------------------------------------------------------
# aiohttp connection pool  (Fix #2)
# ---------------------------------------------------------------------------
_aiohttp_session = None


async def get_session() -> object:
    """Return the shared aiohttp.ClientSession, creating it if needed."""
    global _aiohttp_session
    if _aiohttp_session is None or _aiohttp_session.closed:
        import aiohttp
        timeout = aiohttp.ClientTimeout(
            connect=_CONNECT_TIMEOUT,
            total=_READ_TIMEOUT,
        )
        connector = aiohttp.TCPConnector(limit=20, limit_per_host=5)
        _aiohttp_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={"User-Agent": USER_AGENT},
        )
    return _aiohttp_session


async def close_session() -> None:
    """Close and discard the shared aiohttp.ClientSession."""
    global _aiohttp_session
    if _aiohttp_session is not None and not _aiohttp_session.closed:
        await _aiohttp_session.close()
    _aiohttp_session = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host.lstrip("www.")
    except Exception as e:
        log.debug(f"Suppressed in harvest.py: {e}")
        return ""


def _ext(url: str) -> str:
    try:
        path = urlparse(url).path.lower()
        _, ext = os.path.splitext(path)
        return ext
    except Exception as e:
        log.debug(f"Suppressed in harvest.py: {e}")
        return ""


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def route_legal(url: str, routing_hint: str | None = None) -> str | None:
    """Which L1 fetcher takes this URL: 'cellar' (CELEX), 'eli' (ELI act), 'sejm_print'.

    Only URLs on a CELLAR / ELI / Sejm host qualify (legal_fetch_host), so a CELEX-looking
    query on a foreign domain gets no route. 'api_json' only with the PreScout hint 'api_bee'
    and only when the URL is not an act. None otherwise. Pure: never touches the network.
    """
    if not url:
        return None
    from behive.engine import legal_sources as ls

    if ls.legal_fetch_host(url):
        if ls.celex_from_url(url):
            return "cellar"
        if ls.eli_from_url(url):
            return "eli"
        if ls.sejm_print_from_url(url):
            return "sejm_print"
    if routing_hint == "api_bee":
        return "api_json"
    return None


def _is_lex_url(url: str) -> bool:
    try:
        return (urlparse(url or "").hostname or "").lower() == _LEX_HOST
    except Exception:
        return False


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Drone strategy helper — reads drone map from DB
# ---------------------------------------------------------------------------

_drone_strategy_cache: dict[str, dict] = {}  # (mission_id, domain) → strategy

def _get_drone_strategy(mission_id: str, domain: str) -> dict:
    """Zwraca strategię trutni dla domeny. Cache in-memory."""
    cache_key = f"{mission_id}:{domain}"
    if cache_key in _drone_strategy_cache:
        return _drone_strategy_cache[cache_key]
    try:
        from behive.engine.drones import get_strategy
        result = get_strategy(mission_id, domain)
    except Exception as e:
        log.debug(f"Suppressed in harvest.py: {e}")
        result = {"strategy": "DIRECT", "working_ua": USER_AGENT,
                  "delay_ms": 0, "via_jina": False, "via_wayback": False}
    _drone_strategy_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# HarvesterBee
# ---------------------------------------------------------------------------

class HarvesterBee:
    """Content harvester — fetches, extracts, and scores web content."""
    def __init__(self, mission_id: str, workers: int = DEFAULT_WORKERS):
        self.mission_id = mission_id
        self.workers = workers
        # READ-ONLY connection — held for the entire run, never blocks writers
        self.con = _get_db_connection(read_only=True)
        self.results: list[dict] = []
        self._method_counts: dict[str, int] = {}
        self._skip_counts: dict[str, int] = {}
        self._loaded_count = 0
        # ── PreScout routing hints (optional) ────────────────────────────────
        # Loaded from /tmp/hive_routing_<mission_id>.json if PreScout ran.
        # Maps url → routing_final (e.g. "skip", "api_bee", "pdf_drone", ...)
        self._routing_hints: dict[str, str] = {}
        self._prescout_skip: set[str]       = set()
        _routing_path = f'/tmp/hive_routing_{mission_id}.json'
        if os.path.exists(_routing_path):
            try:
                with open(_routing_path) as _f:
                    _rmap = json.load(_f)
                for _r in _rmap:
                    _url = _r.get("url", "")
                    _rt  = _r.get("routing_final", "standard_drone")
                    self._routing_hints[_url] = _rt
                    if _rt == "skip":
                        self._prescout_skip.add(_url)
                log.info(
                    f"PreScout routing hints loaded: {len(self._routing_hints)} URLs "
                    f"({len(self._prescout_skip)} pre-skipped)"
                )
            except Exception as _e:
                log.warning(f"PreScout routing hints load failed: {_e}")

    def _write_con(self):
        """Context manager: short-lived WRITE connection. Open → write → close immediately."""
        import contextlib
        @contextlib.contextmanager
        def _ctx():
            wcon = _get_db_connection(read_only=False)
            try:
                yield wcon
            finally:
                try:
                    wcon.close()
                except Exception as e:
                    log.debug(f"Suppressed: {e}")
        return _ctx()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> dict:
        """Execute the main pipeline loop."""
        log.info(f"HarvesterBee starting — mission={self.mission_id}, workers={self.workers}")

        sources = self._load_pending_sources()
        if not sources:
            log.warning("No pending sources found for this mission.")
            self._finalize(0)
            skip_summary(self.mission_id, self.__dict__.setdefault("_skip_counts", {}), 0, 0)
            return
        self._loaded_count = len(sources)
        self._log_score_filter_skips(sources)

        # Show how many were filtered
        total_pending = self.con.execute(
            "SELECT COUNT(*) FROM hive_sources WHERE mission_id=? AND status IN ('pending','scouted')",
            [self.mission_id]
        ).fetchone()[0]
        log.info(f"Loaded {len(sources)} source(s) after relevance filter (total scouted: {total_pending}, filtered out: {total_pending - len(sources)})")

        # ── REC-01: Quorum check (waggle dance gate) ───────────────────────────
        try:
            from behive.engine.bionic_quorum import QuorumChecker
            qc = QuorumChecker(self.mission_id)
            quorum = qc.check()
            log.debug(quorum.summary())
            if quorum.needs_expansion:
                log.debug(f"  🐝 Quorum: {quorum.harvestable_count} < {25} — range expansion triggered")
                log.debug(f"  🐝 Expansion queries: {quorum.expansion_queries}")
                # Expansion queries are logged — hive2.py pipeline handles re-scout
                # Here we just proceed with what we have (non-blocking)
        except Exception as _qe:
            log.debug(f"Quorum check error (non-critical): {_qe}")

        # ── REC-02: Pre-filter via domain reputation (grooming) ───────────────
        try:
            from behive.engine.domain_reputation import get_reputation
            _rep = get_reputation()
            before = len(sources)
            kept, groomed_out = [], []
            for s in sources:
                _u = s.get("url", "")
                if not _rep.should_skip(_u):
                    kept.append(s)
                elif route_legal(_u) in _LEGAL_ROUTES:
                    # Narrow exception, not an amnesty: an act reachable via CELLAR / ELI /
                    # print is fetched without touching the blacklisted host's page. The
                    # domain stays blacklisted; every other URL of it is still groomed.
                    kept.append(s)
                else:
                    groomed_out.append(s)
            sources = kept
            for s in groomed_out:
                _u = s.get("url", "")
                if _is_lex_url(_u):
                    # LEX is login-only: a BYOK gap, not a "bad domain".
                    self._note_skip(_u, "byok_gap", "lex_login_required")
                else:
                    self._note_skip(_u, "domain_blacklist", self._blacklist_reason(_rep, _u))
            if len(sources) < before:
                log.info(f"🐝 Grooming: {before - len(sources)} sources removed by domain reputation")
        except Exception as _re:
            log.debug(f"Domain reputation filter error (non-critical): {_re}")

        queue: asyncio.Queue = asyncio.Queue()
        for src in sources:
            await queue.put(src)

        # ── REC-03: Adaptive Worker Reallocation (forager bee analog) ─────────
        # Forager bees reallocate to the richest flowers.
        # TODO: implement adaptive worker count — start with N workers, scale up if
        # queue many, scale down if many consecutive errors.
        adaptive_workers = min(self.workers, len(sources))
        if len(sources) > 100:
            adaptive_workers = min(adaptive_workers + 2, 12)  # scale up for dużych misji
        elif len(sources) < 20:
            adaptive_workers = max(adaptive_workers - 1, 2)   # scale down for małych

        worker_tasks = [
            asyncio.create_task(self._worker(queue, i))
            for i in range(adaptive_workers)
        ]

        # Checkpoint every 50 docs — write to DB continuously (resilience to SIGKILL)
        async def _checkpoint_loop():
            while True:
                await asyncio.sleep(30)
                done = [r for r in self.results if r not in self._checkpointed]
                if done:
                    self._flush_checkpoint(done)

        self._checkpointed: set = set()
        ckpt_task = asyncio.create_task(_checkpoint_loop())

        await queue.join()
        for t in worker_tasks:
            t.cancel()
        ckpt_task.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        await close_session()

        self._save_results()
        self._finalize(len(sources))
        self._print_summary(len(sources))

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    async def _worker(self, queue: asyncio.Queue, worker_id: int):
        while True:
            try:
                source = await queue.get()
                try:
                    result = await self._harvest_one(source)
                    self.results.append(result)
                except Exception as exc:
                    log.error(f"Worker {worker_id}: unhandled error on {source.get('url','?')}: {exc}")
                    self.results.append({
                        "source_id": source["source_id"],
                        "url": source.get("url", ""),
                        "text": "",
                        "word_count": 0,
                        "method": "failed",
                        "error": str(exc),
                        "success": False,
                    })
                finally:
                    queue.task_done()
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Route harvester
    # ------------------------------------------------------------------

    async def _harvest_one(self, source: dict) -> dict:
        url = source.get("url", "")
        log.info(f"Harvesting: {url}")

        base = {
            "source_id": source["source_id"],
            "url": url,
            "domain": _domain(url),
            "text": "",
            "word_count": 0,
            "method": "unknown",
            "success": False,
            "language": "unknown",
            "quality_score": 0.0,
            "error": None,
        }

        # Fix #4: skip re-fetching already-seen URLs within this run
        if _is_url_cached(url):
            log.debug(f"  URL cache hit — skipping re-fetch: {url}")
            self._note_skip(url, "cache_skip", "duplicate_url")
            base["method"] = "cache_skip"
            base["error"] = "duplicate url in mission run"
            return base

        ext = _ext(url)
        dom = _domain(url)

        # ── DRONE STRATEGY — reads drone strategy for this domain ──────────
        drone_strategy = _get_drone_strategy(self.mission_id, dom)
        strategy   = drone_strategy.get("strategy", "DIRECT")
        via_jina   = drone_strategy.get("via_jina", False)
        via_wayback = drone_strategy.get("via_wayback", False)
        drone_ua   = drone_strategy.get("working_ua", USER_AGENT)
        cffi_target = drone_strategy.get("cffi_target")
        delay_ms   = drone_strategy.get("delay_ms", 0)

        # Blokada zidentyfikowana przez trutnie — skip od razu, nie marnuj czasu
        if strategy == "SKIP":
            log.debug(f"  Drone: SKIP {dom} (wall not bypassed)")
            self._note_skip(url, "drone_skip", drone_strategy.get("block_type", "unknown"))
            base["method"] = "drone_skip"
            base["error"]  = f"drone_blocked: {drone_strategy.get('block_type', 'unknown')}"
            return base

        # ── PreScout routing hint — skip if BeeMaster rejected ────────────
        # (a 'cellar' hint is a decision to fetch via CELLAR, never a skip)
        if (url in self._prescout_skip and source.get("source_type") != "operator_seed"
                and getattr(self, "_routing_hints", {}).get(url) != "cellar"):
            log.debug(f"  PreScout: SKIP {url} (approved=False by BeeMaster)")
            self._note_skip(url, "prescout_skip", "routing_final=skip")
            base["method"] = "prescout_skip"
            base["error"]  = "skipped_by_prescout"
            return base

        # ── LEX (sip.lex.pl): login-only. No fetch, no login, no fallback chain ──
        if _is_lex_url(url):
            self._note_skip(url, "byok_gap", "lex_login_required")
            base["method"] = "byok_gap"
            base["error"]  = "byok_gap: lex_login_required"
            return base

        # ── Legal sources: act via CELLAR / ELI / Sejm print, before the extension branch ──
        legal_route = route_legal(url, getattr(self, "_routing_hints", {}).get(url))

        # One act, one content row per run: an act (route, identifier) already taken by an
        # earlier URL is neither fetched nor stored again. Claimed before any await, so the
        # concurrent workers cannot both take the same act.
        if legal_route in _LEGAL_ROUTES:
            act_key = self._legal_act_key(url, legal_route)
            acts = self.__dict__.setdefault("_legal_acts", {})
            if act_key is not None and act_key in acts:
                self._note_skip(url, "cache_skip", f"duplicate_act:{act_key[0]}:{act_key[1]}")
                base["method"] = "cache_skip"
                base["error"] = "duplicate act in mission run"
                return base
            if act_key is not None:
                acts[act_key] = url
        else:
            act_key = None

        # Required delays (rate-limiting — drones measured threshold)
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)


        try:
            legal_result = None
            if legal_route in _LEGAL_ROUTES:
                legal_result = await self._harvest_legal(url, legal_route)
            elif legal_route == "api_json":
                legal_result = await asyncio.get_event_loop().run_in_executor(
                    None, self._harvest_json, url
                )
                if not legal_result.get("success"):
                    legal_result = None  # not JSON after all: the old path decides

            if legal_result is not None:
                base.update(legal_result)
            elif ext == ".pdf":
                result = await asyncio.get_event_loop().run_in_executor(
                    None, self._harvest_pdf, url
                )
                base.update(result)
            elif ext in (".docx", ".xlsx"):
                result = await asyncio.get_event_loop().run_in_executor(
                    None, self._harvest_document, url
                )
                base.update(result)
            elif dom in NEWS_DOMAINS:
                result = await self._harvest_newspaper(url)
                base.update(result)
            else:
                result = await self._harvest_web(url)
                base.update(result)

            # Fallback chain (never for a legal result: a third-party proxy must not refetch an act)
            if legal_result is None and base["word_count"] < 150:
                log.debug(f"  word_count={base['word_count']} < 150 → trying Jina")
                jina_result = await self._harvest_jina(url)
                if jina_result["word_count"] > base["word_count"]:
                    base.update(jina_result)

            if legal_result is None and base["word_count"] < 100:
                log.debug(f"  word_count={base['word_count']} < 100 → trying crawl4ai")
                c4a_result = await self._harvest_crawl4ai(url)
                if c4a_result["word_count"] > base["word_count"]:
                    base.update(c4a_result)

        except Exception as exc:
            log.warning(f"  harvest_one failed for {url}: {exc}")
            base["error"] = str(exc)
            base["success"] = False

        # Phase deadline: the act was not started. Work is deferred — not a failed harvest,
        # no reputation entry, not cached.
        if base.get("deferred"):
            if act_key is not None:
                # not started: a later URL of the same act may still take the claim
                self.__dict__.get("_legal_acts", {}).pop(act_key, None)
            return base

        # Empty legal fetch (WAF page, 0 words, CELLAR refusal): logged, and never a bad
        # result for the domain, so a WAF reply cannot push consecutive_bad to a blacklist.
        if legal_route in _LEGAL_ROUTES and not base["text"]:
            self._note_skip(url, "legal_fetch_empty", base.get("error") or "empty")
            base["success"] = False
            base["word_count"] = 0
            _cache_url(url)
            m = base["method"]
            self._method_counts[m] = self._method_counts.get(m, 0) + 1
            return base

        # Post-process
        if base["text"]:
            base["word_count"] = len(base["text"].split())
            base["language"] = self._detect_language(base["text"])
            base["quality_score"] = self._score_quality(base["text"], url)
            base["success"] = base["word_count"] >= 50

            # ── REC-04: Covert Degradation Detection (DWV analog) ──────────
            # Replaces naive _score_quality with multi-signal bionic scorer
            try:
                from behive.engine.content_quality import score_content_detailed, quality_label
                cq = score_content_detailed(base["text"])
                base["quality_score"] = cq["quality_score"]
                base["success"] = base["word_count"] >= 50 and not cq["covert_infected"]
                if cq["covert_infected"]:
                    log.debug(
                        f"  🐝 DWV: covert infection detected {url[:60]} "
                        f"q={cq['quality_score']:.2f} ({quality_label(cq['quality_score'])})"
                    )
            except ImportError:
                pass  # fallback to existing _score_quality above

        # ── REC-02: Domain Reputation update (grooming behavior) ─────────────
        try:
            from behive.engine.domain_reputation import get_reputation
            get_reputation().update(url, base.get("quality_score", 0.0), base.get("success", False))
        except Exception as e:
            log.debug(f"Suppressed: {e}")

        # Record URL as seen
        _cache_url(url)

        # Track method stats
        m = base["method"]
        self._method_counts[m] = self._method_counts.get(m, 0) + 1

        return base

    # ------------------------------------------------------------------
    # Harvest methods
    # ------------------------------------------------------------------
    def _harvest_pdf(self, url: str) -> dict:
        result = {"method": "pdf", "text": "", "word_count": 0, "success": False}
        fitz = _get_fitz()  # lazy import
        if fitz is None:
            result["error"] = "pymupdf not installed"
            return result
        requests = _get_requests()  # lazy import
        if requests is None:
            result["error"] = "requests not installed"
            return result
        try:
            resp = requests.get(
                url, timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                stream=True
            )
            resp.raise_for_status()
            pdf_bytes = resp.content
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            pages = []
            for page in doc:
                pages.append(page.get_text())
            text = "\n".join(pages).strip()
            result["text"] = text
            result["word_count"] = len(text.split())
            result["success"] = True
        except Exception as exc:
            log.debug(f"Exception in harvest.py: {exc}")
            result["error"] = str(exc)
        return result

    @staticmethod
    def _legal_act_key(url: str, route: str):
        """(route, identifier) of the act a legal URL points at, or None. Identifier formats
        match the L1 fetchers: CELEX, 'PUB/YEAR/POS', 'TERM/NUMBER'."""
        from behive.engine import legal_sources as ls

        if route == "cellar":
            celex = ls.celex_from_url(url)
            return (route, celex) if celex else None
        if route == "eli":
            eli = ls.eli_from_url(url)
            return (route, f"{eli[0]}/{eli[1]}/{eli[2]}") if eli else None
        if route == "sejm_print":
            printed = ls.sejm_print_from_url(url)
            return (route, f"{printed[0]}/{printed[1]}") if printed else None
        return None

    def _legal_fetch_sync(self, url: str, route: str):
        """Blocking: the L1 fetcher for this route, then HTML → plain text. Returns (LegalFetch, text)."""
        from behive.engine import legal_sources as ls

        if route == "cellar":
            got = ls.fetch_celex_text(ls.celex_from_url(url))
        elif route == "eli":
            publisher, year, pos = ls.eli_from_url(url)  # type: ignore[misc]
            got = ls.fetch_eli_act(publisher, year, pos)
        else:
            term, number = ls.sejm_print_from_url(url)  # type: ignore[misc]
            got = ls.fetch_sejm_print(term, number)
        text = got.text or ""
        if got.ok and text:
            ctype = (got.content_type or "").lower()
            if "html" in ctype or "xml" in ctype or text.lstrip().startswith("<"):
                text = _html_to_text(text)
            else:
                text = text.strip()
        return got, text

    async def _harvest_legal(self, url: str, route: str) -> dict:
        """Fetch an act through CELLAR / ELI / a Sejm print (never eur-lex.europa.eu).

        Runs the blocking L1 fetcher in an executor, capped by LEGAL_FETCH_TIMEOUT_S and the
        phase deadline. A phase deadline defers the work (deferred=True); any other failure
        or an empty text is a result with text == "" and the reason in error.
        """
        from behive.engine.llm import PhaseDeadlineExceeded, deadline_call_timeout

        result = {"method": route, "text": "", "word_count": 0, "success": False, "title": ""}
        try:
            timeout = deadline_call_timeout(LEGAL_FETCH_TIMEOUT_S, "legal_harvest")
        except PhaseDeadlineExceeded:
            result["error"] = "phase_deadline"
            result["deferred"] = True
            return result
        loop = asyncio.get_running_loop()
        try:
            got, text = await asyncio.wait_for(
                loop.run_in_executor(None, self._legal_fetch_sync, url, route), timeout
            )
        except asyncio.TimeoutError:
            result["error"] = "legal_fetch: timeout"
            return result
        except Exception as exc:
            result["error"] = f"legal_fetch: {type(exc).__name__}"
            return result
        result["title"] = got.title or ""
        if got.ok:
            err = _web_text_error(text) if text else "empty_text"
            if err is None:
                result["text"] = text
                result["word_count"] = len(text.split())
                result["success"] = True
            else:
                result["error"] = f"legal_fetch: {err}"
        else:
            result["error"] = f"legal_fetch: {got.reason or 'empty'}"
        return result

    def _harvest_json(self, url: str) -> dict:
        """api_bee endpoint: GET with Accept: application/json, body kept as text (no HTML extractor)."""
        result = {"method": "api_json", "text": "", "word_count": 0, "success": False}
        requests = _get_requests()  # lazy import
        if requests is None:
            result["error"] = "requests not installed"
            return result
        try:
            resp = requests.get(
                url, timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            resp.raise_for_status()
            if "json" not in (resp.headers.get("Content-Type") or "").lower():
                result["error"] = "not_json"
                return result
            text = (resp.text or "").strip()
            result["text"] = text
            result["word_count"] = len(text.split())
            result["success"] = bool(text)
        except Exception as exc:
            log.debug(f"Exception in harvest.py: {exc}")
            result["error"] = str(exc)
        return result

    def _harvest_document(self, url: str) -> dict:
        result = {"method": "document", "text": "", "word_count": 0, "success": False}
        MarkItDown = _get_markitdown()  # lazy import
        if MarkItDown is None:
            result["error"] = "markitdown not installed — skipping"
            return result
        requests = _get_requests()  # lazy import
        if requests is None:
            result["error"] = "requests not installed"
            return result
        try:
            resp = requests.get(
                url, timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT}
            )
            resp.raise_for_status()
            import tempfile
            ext = _ext(url)
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
                tf.write(resp.content)
                tmp_path = tf.name
            md = MarkItDown()
            res = md.convert(tmp_path)
            text = res.text_content if hasattr(res, "text_content") else str(res)
            os.unlink(tmp_path)
            result["text"] = text.strip()
            result["word_count"] = len(result["text"].split())
            result["success"] = True
        except Exception as exc:
            log.debug(f"Exception in harvest.py: {exc}")
            result["error"] = str(exc)
        return result

    async def _harvest_web(self, url: str) -> dict:
        result = {"method": "web_curl", "text": "", "word_count": 0, "success": False}
        html = ""

        # Try curl_cffi first (lazy import)
        CurlAsyncSession = _get_curl_cffi_session_cls()
        if CurlAsyncSession is not None:
            try:
                async with CurlAsyncSession(impersonate="chrome131") as session:
                    resp = await session.get(
                        url,
                        timeout=_READ_TIMEOUT,
                    )
                    html = resp.text
                result["method"] = "web_curl"
            except Exception as exc:
                log.debug(f"  curl_cffi failed: {exc}")
                html = ""

        # Fallback to httpx (lazy import)
        httpx = _get_httpx()
        if not html and httpx is not None:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=True
                ) as client:
                    resp = await client.get(url)
                    html = resp.text
                result["method"] = "web_httpx"
            except Exception as exc:
                log.debug(f"  httpx failed: {exc}")

        # Fallback to aiohttp session pool
        if not html:
            try:
                session = await get_session()
                async with session.get(url) as resp:
                    html = await resp.text()
                result["method"] = "web_aiohttp"
            except Exception as exc:
                log.debug(f"  aiohttp failed: {exc}")

        # Last resort: requests (sync in executor)
        if not html and _get_requests() is not None:
            try:
                def _sync_get():
                    r = _get_requests().get(
                        url, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                        headers={"User-Agent": USER_AGENT},
                        allow_redirects=True
                    )
                    r.raise_for_status()
                    return r.text
                html = await asyncio.get_event_loop().run_in_executor(None, _sync_get)
                result["method"] = "web_requests"
            except Exception as exc:
                log.debug(f"  requests fallback failed: {exc}")

        if html:
            trafilatura = _get_trafilatura()  # lazy import
            if trafilatura is not None:
                extracted = trafilatura.extract(
                    html,
                    include_comments=False,
                    include_tables=True,
                    no_fallback=False,
                )
                text = extracted or ""
            else:
                text = _html_to_text(html)
            result["text"] = text
            result["word_count"] = len(text.split())
            error = _web_text_error(text)
            result["success"] = error is None
            if error:
                result["error"] = error

        return result

    async def _harvest_jina(self, url: str) -> dict:
        result = {"method": "jina", "text": "", "word_count": 0, "success": False}
        jina_url = JINA_BASE + url
        try:
            httpx = _get_httpx()  # lazy import
            # Retry logic: 3 attempts with exponential backoff for 429
            raw = None
            for _attempt in range(3):
                try:
                    if httpx is not None:
                        async with httpx.AsyncClient(
                            timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
                            headers={
                                "User-Agent": USER_AGENT,
                                "Accept": "text/plain",
                            },
                            follow_redirects=True
                        ) as client:
                            resp = await client.get(jina_url)
                            if resp.status_code == 429:
                                wait = 2 ** (_attempt + 1)  # 2s, 4s, 8s
                                log.warning(f"Jina 429 — backoff {wait}s for {url[:60]}")
                                await asyncio.sleep(wait)
                                continue
                            raw = resp.text
                    else:
                        session = await get_session()
                        async with session.get(
                            jina_url,
                            headers={"Accept": "text/plain"},
                        ) as resp:
                            if resp.status == 429:
                                wait = 2 ** (_attempt + 1)
                                log.warning(f"Jina 429 — backoff {wait}s for {url[:60]}")
                                await asyncio.sleep(wait)
                                continue
                            raw = await resp.text()
                    break  # success
                except Exception as _e:
                    log.debug(f"Exception in harvest.py: {_e}")
                    if _attempt == 2:
                        raise
                    await asyncio.sleep(2 ** _attempt)
            if raw is None:
                result["error"] = "jina 429 exhausted retries"
                return result

            # Strip common Jina boilerplate headers
            lines = raw.splitlines()
            clean_lines = []
            skip_patterns = re.compile(
                r"^(Title:|URL Source:|Markdown Content:|Published Time:|=+|-+)\s*",
                re.IGNORECASE
            )
            for line in lines:
                if skip_patterns.match(line.strip()):
                    continue
                clean_lines.append(line)
            text = "\n".join(clean_lines).strip()
            result["text"] = text
            result["word_count"] = len(text.split())
            result["success"] = bool(text)
        except Exception as exc:
            log.debug(f"Exception in harvest.py: {exc}")
            result["error"] = str(exc)
        return result

    async def _harvest_crawl4ai(self, url: str) -> dict:
        result = {"method": "crawl4ai", "text": "", "word_count": 0, "success": False}
        AsyncWebCrawler, CacheMode = _get_crawl4ai()  # lazy import
        if AsyncWebCrawler is None:
            result["error"] = "crawl4ai not installed"
            return result
        try:
            async with AsyncWebCrawler() as crawler:
                crawl_result = await crawler.arun(
                    url=url,
                    cache_mode=CacheMode.BYPASS,
                )
            text = ""
            if crawl_result and crawl_result.success:
                if hasattr(crawl_result, "fit_markdown") and crawl_result.fit_markdown:
                    text = crawl_result.fit_markdown
                elif hasattr(crawl_result, "markdown") and crawl_result.markdown:
                    text = crawl_result.markdown
                elif hasattr(crawl_result, "cleaned_html"):
                    text = re.sub(r"<[^>]+>", " ", crawl_result.cleaned_html or "")
                    text = re.sub(r"\s+", " ", text).strip()
            result["text"] = text
            result["word_count"] = len(text.split())
            result["success"] = bool(text)
        except Exception as exc:
            log.debug(f"Exception in harvest.py: {exc}")
            result["error"] = str(exc)
        return result

    async def _harvest_newspaper(self, url: str) -> dict:
        result = {"method": "newspaper", "text": "", "word_count": 0, "success": False}
        newspaper = _get_newspaper()  # lazy import
        if newspaper is None:
            # Fallback to web
            return await self._harvest_web(url)
        try:
            def _parse():
                article = newspaper.Article(url)
                article.download()
                article.parse()
                try:
                    article.nlp()
                except Exception as e:
                    log.debug(f"Suppressed: {e}")
                parts = []
                if article.title:
                    parts.append(article.title)
                if article.text:
                    parts.append(article.text)
                return "\n\n".join(parts)

            text = await asyncio.get_event_loop().run_in_executor(None, _parse)
            result["text"] = text.strip()
            result["word_count"] = len(result["text"].split())
            result["success"] = bool(result["text"])
        except Exception as exc:
            log.debug(f"  newspaper failed: {exc}")
            # Fallback to web
            fallback = await self._harvest_web(url)
            fallback["method"] = "newspaper_web_fallback"
            return fallback
        return result

    # ------------------------------------------------------------------
    # Scoring / language
    # ------------------------------------------------------------------

    def _score_quality(self, text: str, url: str) -> float:
        if not text:
            return 0.0
        words = text.split()
        word_count = len(words)

        # word_count contribution (capped at 1.0 at 500 words)
        wc_score = min(word_count / 500.0, 1.0) * 0.4

        # has numbers
        has_numbers = 0.2 if re.search(r"\d+", text) else 0.0

        # has dates
        has_dates = 0.2 if re.search(
            r"\b(20\d{2}|19\d{2}|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",
            text, re.IGNORECASE
        ) else 0.0

        # not boilerplate: penalise short repeated lines
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        unique_ratio = len(set(lines)) / max(len(lines), 1)
        not_boilerplate = 0.2 if unique_ratio > 0.7 else 0.0

        return round(wc_score + has_numbers + has_dates + not_boilerplate, 4)

    def _detect_language(self, text: str) -> str:
        if not text or len(text.split()) < 10:
            return "unknown"
        langdetect = _get_langdetect()  # lazy import
        if langdetect is not None:
            try:
                return langdetect(text[:2000])
            except Exception as e:
                log.debug(f"Suppressed: {e}")
        # Fallback: basic heuristic
        sample = text[:500].lower()
        if any(w in sample for w in ["the ", "and ", "is ", "are ", "was "]):
            return "en"
        if any(w in sample for w in ["und ", "die ", "der ", "ist ", "ein "]):
            return "de"
        if any(w in sample for w in [" le ", " la ", " les ", " est ", " une "]):
            return "fr"
        if any(w in sample for w in [" i ", " w ", " z ", " na ", " się "]):
            return "pl"
        return "unknown"

    # ------------------------------------------------------------------
    # DB persistence
    # ------------------------------------------------------------------

    def _note_skip(self, url: str, reason: str, detail: str = "") -> None:
        # setdefault: tests build HarvesterBee via __new__ without __init__
        counts = self.__dict__.setdefault("_skip_counts", {})
        note_skip(self.mission_id, url, reason, detail, counts)

    @staticmethod
    def _blacklist_reason(rep, url: str) -> str:
        """Blacklist reason of the URL's domain (read from the reputation cache); '' if unknown."""
        try:
            from behive.engine.domain_reputation import _extract_domain
            rec = rep._load(_extract_domain(url))
            return (rec or {}).get("blacklist_reason", "") or ""
        except Exception as e:
            log.debug(f"Suppressed: {e}")
            return ""

    def _log_score_filter_skips(self, sources: list[dict]) -> None:
        """INFO skip line for every pending/scouted row of this mission that
        _load_pending_sources did not load. Read-only; never changes what is harvested.
        Operator seeds are always loaded, so they never appear here."""
        try:
            loaded_ids = {s.get("source_id") for s in sources}
            rows = self.con.execute("""
                SELECT id, url, score_relevance, score_total
                FROM hive_sources
                WHERE mission_id = ?
                  AND status IN ('pending', 'scouted')
                  AND source_type IS DISTINCT FROM 'operator_seed'
                ORDER BY id
            """, [self.mission_id]).fetchall()
            for sid, url, rel, total in rows:
                if sid not in loaded_ids:
                    self._note_skip(url or "", "score_filter", f"relevance={rel} total={total}")
        except Exception as exc:
            log.debug(f"score_filter skip log failed (non-critical): {exc}")

    def _legal_meta_fetch(self, kind: str, ident) -> tuple[str, str]:
        """(metadata title, reason) for one CELEX / ELI act / print, from the L1 fetchers.

        One call is bounded by LEGAL_RESCORE_FETCH_TIMEOUT_S and by the phase deadline;
        PhaseDeadlineExceeded propagates (nothing was started). A failure is (\"\", reason).
        """
        import concurrent.futures as _cf
        from behive.engine import legal_sources as _ls
        from behive.engine.llm import deadline_call_timeout

        timeout = deadline_call_timeout(LEGAL_RESCORE_FETCH_TIMEOUT_S, "legal_rescore")
        if kind == "celex":
            fn, args = _ls.fetch_celex_text, (ident,)
        elif kind == "eli":
            fn, args = _ls.fetch_eli_act, tuple(ident)
        else:
            fn, args = _ls.fetch_sejm_print, tuple(ident)
        pool = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="legal-rescore")
        try:
            got = pool.submit(fn, *args).result(timeout=timeout)
        except _cf.TimeoutError:
            return "", "timeout"
        except Exception as exc:
            return "", f"fetch_error: {type(exc).__name__}"
        finally:
            pool.shutdown(wait=False)
        title = " ".join(str(getattr(got, "title", "") or "").split())
        if title:
            return title, ""
        return "", str(getattr(got, "reason", "") or "no_title")

    def _rescore_legal_sources(self) -> None:
        """Score pending/scouted rows with a URL-like title or a CELEX/ELI/print id by their
        metadata title, before the 5/15 filter. Only the score columns (and the title, when
        the old one is URL-like) of the existing row id change; the threshold, the cap and
        operator_seed rows are not touched. A failed, slow or too-late fetch leaves the row
        and is logged at INFO with a reason. One CELEX is fetched once per run."""
        from behive.engine import legal_sources as ls
        from behive.engine.llm import PhaseDeadlineExceeded

        mission = self.mission_id
        row = self.con.execute("SELECT topic FROM hive_missions WHERE id = ?", [mission]).fetchone()
        topic = (row[0] if row else "") or ""
        rows = self.con.execute("""
            SELECT id, url, title, snippet, source_type, score_total, score_relevance,
                   score_freshness, score_authority, score_depth
            FROM hive_sources
            WHERE mission_id = ?
              AND status IN ('pending', 'scouted')
              AND source_type IS DISTINCT FROM 'operator_seed'
            ORDER BY id
        """, [mission]).fetchall()
        candidates = []
        for r in rows:
            url = r[1] or ""
            if ls.celex_from_url(url):
                key = ("celex", ls.celex_from_url(url))
            elif ls.eli_from_url(url):
                key = ("eli", ls.eli_from_url(url))
            elif ls.sejm_print_from_url(url):
                key = ("print", ls.sejm_print_from_url(url))
            else:
                key = None
            if key is not None or ls.url_like_title(r[2] or "", url):
                candidates.append((r, key))
        if not candidates:
            return

        def _skip(url: str, reason: str) -> None:
            code, _, detail = reason.partition(":")
            log.info("legal_rescore skip mission=%s url=%s reason=%s detail=%s",
                     mission, url, code.strip(), detail.strip())

        cache: dict = {}
        deadline_hit = False
        rescored = unchanged = skipped = 0
        updates = []
        for (sid, url, title, snippet, stype, total, rel, fresh, auth, depth), key in candidates:
            if key is None:
                skipped += 1
                _skip(url, "no_identifier")
                continue
            if not topic:
                skipped += 1
                _skip(url, "no_topic")
                continue
            if key not in cache:
                if deadline_hit:
                    skipped += 1
                    _skip(url, "phase_deadline")
                    continue
                try:
                    cache[key] = self._legal_meta_fetch(*key)
                except PhaseDeadlineExceeded:
                    deadline_hit = True
                    skipped += 1
                    _skip(url, "phase_deadline")
                    continue
            meta_title, reason = cache[key]
            if not meta_title:
                skipped += 1
                _skip(url, reason or "no_title")
                continue
            new = ls.rescore_legal_source(topic, url, title or "", snippet or "", meta_title,
                                          source_type=stype or "web")
            new_title = meta_title[:512] if ls.url_like_title(title or "", url) else (title or "")
            old = (title or "", total, rel, fresh, auth, depth)
            if total is not None and new["score_total"] < total:
                # Never lower a row: appending the official title can dilute relevance
                # (12/34 -> 5/27). The old scores stay; a URL-like title may still be replaced.
                kept = True
                want = (new_title, total, rel, fresh, auth, depth)
            else:
                kept = False
                want = (new_title, new["score_total"], new["score_relevance"],
                        new["score_freshness"], new["score_authority"], new["score_depth"])
            if kept:
                unchanged += 1
                if want != old:
                    updates.append((sid, want, False))
                continue
            if want == old:
                unchanged += 1
                continue
            updates.append((sid, want, True))
        if updates:
            with self._write_con() as wcon:
                for sid, w, scored in updates:
                    wcon.execute(
                        "UPDATE hive_sources SET title = ?, score_total = ?, score_relevance = ?, "
                        "score_freshness = ?, score_authority = ?, score_depth = ? "
                        "WHERE id = ? AND mission_id = ? AND status IN ('pending', 'scouted')",
                        [*w, sid, mission])
                    if scored:
                        rescored += 1
        log.info("legal_rescore mission=%s candidates=%d rescored=%d unchanged=%d skipped=%d fetches=%d",
                 mission, len(candidates), rescored, unchanged, skipped, len(cache))

    def _rescore_before_filter(self) -> None:
        """Never lets the rescore step stop the load: on any error the rows stay as scouted."""
        try:
            self._rescore_legal_sources()
        except Exception as exc:
            log.info("legal_rescore mission=%s failed reason=%s", self.mission_id, type(exc).__name__)
            log.debug(f"legal_rescore failed (non-critical): {exc}")

    def _load_pending_sources(self) -> list[dict]:
        try:
            self._rescore_before_filter()
            # MAX_HARVEST = 300 top-scoring sources per mission
            # Filtr: score_relevance >= 5 (entity anchoring: sources without entity name penalized by -5)
            #        score_total >= 15 (eliminuje losowy szum)
            # Operator seeds are additive: they never replace automatic sources
            # nor suppress the relaxed fallback below.
            seeds = self.con.execute("""
                SELECT id, url, source_type, score_total, score_relevance
                FROM hive_sources
                WHERE mission_id = ?
                  AND status IN ('pending', 'scouted')
                  AND source_type = 'operator_seed'
                ORDER BY id
            """, [self.mission_id]).fetchall()
            rows = self.con.execute("""
                SELECT id, url, source_type, score_total, score_relevance
                FROM hive_sources
                WHERE mission_id = ?
                  AND status IN ('pending', 'scouted')
                  AND source_type IS DISTINCT FROM 'operator_seed'
                  AND score_relevance >= 5
                  AND score_total >= 15
                ORDER BY score_total DESC NULLS LAST
                LIMIT 300
            """, [self.mission_id]).fetchall()
            if not rows:
                # Fallback — relaxed thresholds if nothing passes
                log.warning("No sources passed relevance filter — falling back to top-100 by score_total")
                rows = self.con.execute("""
                    SELECT id, url, source_type, score_total, score_relevance
                    FROM hive_sources
                    WHERE mission_id = ?
                      AND status IN ('pending', 'scouted')
                      AND source_type IS DISTINCT FROM 'operator_seed'
                    ORDER BY score_total DESC NULLS LAST
                    LIMIT 100
                """, [self.mission_id]).fetchall()
            rows = list(seeds) + list(rows)
            cols = ["source_id", "url", "source_type", "score_total", "score_relevance"]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            log.error(f"Failed to load pending sources: {exc}")
            return []

    def _flush_checkpoint(self, results: list):
        """Checkpoint — zapisz zebrane wyniki do DB bez czekania na koniec."""
        saved = 0
        with self._write_con() as wcon:
            for r in results:
                if r.get("success") and r.get("text"):
                    try:
                        wcon.execute("""
                            INSERT INTO hive_content
                                (id, mission_id, url, domain, raw_text, word_count,
                                 language, quality_score, harvest_method, harvested_at)
                            VALUES (nextval('hive_content_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, [
                            self.mission_id, r["url"], r.get("domain", ""),
                            r["text"], r["word_count"], r.get("language", "unknown"),
                            r.get("quality_score", 0.0), r.get("method", "unknown"),
                            _now_utc(),
                        ])
                        wcon.execute(
                            "UPDATE hive_sources SET status = 'done' WHERE id = ?",
                            [r["source_id"]]
                        )
                        saved += 1
                    except (Exception,) as e:  # DB operation
                        log.debug(f"DB error: {e}")
                self._checkpointed.add(id(r))
        if saved:
            log.info(f"  Checkpoint: +{saved} docs zapisanych do DB")

    def _save_results(self):
        saved = 0
        failed = 0
        # Close read-only connection before write
        try:
            self.con.close()
        except Exception as e:
            log.debug(f"Suppressed: {e}")
        # Chunked write — 50 results per short-lived connection (avoids holding lock for full batch)
        CHUNK = 50
        for offset in range(0, len(self.results), CHUNK):
            batch = self.results[offset:offset + CHUNK]
            with self._write_con() as wcon:
                for r in batch:
                    if r.get("success") and r.get("text"):
                        try:
                            wcon.execute("""
                                INSERT INTO hive_content
                                    (id, mission_id, url, domain, raw_text, word_count,
                                     language, quality_score, harvest_method, harvested_at)
                                VALUES (nextval('hive_content_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, [
                                self.mission_id,
                                r["url"],
                                r.get("domain", ""),
                                r["text"],
                                r["word_count"],
                                r.get("language", "unknown"),
                                r.get("quality_score", 0.0),
                                r.get("method", "unknown"),
                                _now_utc(),
                            ])
                            wcon.execute("""
                                UPDATE hive_sources
                                SET status = 'done'
                                WHERE id = ?
                            """, [r["source_id"]])
                            saved += 1
                        except Exception as exc:
                            log.error(f"DB save error for {r.get('url','?')}: {exc}")
                            failed += 1
                    else:
                        if r.get("deferred"):
                            continue  # phase deadline: the row stays as it was, to be harvested later
                        try:
                            wcon.execute("""
                                UPDATE hive_sources
                                SET status = 'failed'
                                WHERE id = ?
                            """, [r["source_id"]])
                        except (Exception,) as e:  # DB operation
                            log.debug(f"DB error: {e}")
                        failed += 1
        log.info(f"Saved {saved} content rows, {failed} failed")

        # ── REC-07: Domain Reputation flush (grooming behavior) ──────────────
        # After saving results, update domain reputation in cross-mission memory.
        try:
            from behive.engine.domain_reputation import get_reputation
            _rep = get_reputation()
            _rep.flush()
            log.debug(f"🐝 Grooming: domain reputation flushed ({len(self.results)} entries)")
        except Exception as _dre:
            log.debug(f"Domain reputation flush failed (non-critical): {_dre}")

    def _finalize(self, total_sources: int):
        total_words  = sum(r.get("word_count", 0) for r in self.results)
        harvested    = sum(1 for r in self.results if r.get("success"))
        # ── REC-08: Quality metrics w _finalize ──────────────────────────────
        avg_quality  = (
            sum(r.get("quality_score", 0.0) for r in self.results if r.get("success"))
            / max(harvested, 1)
        )
        unique_domains = len({r.get("domain", "") for r in self.results if r.get("success")})
        method_dist    = {}
        for r in self.results:
            m = r.get("method", "unknown")
            method_dist[m] = method_dist.get(m, 0) + 1
        import json as _json
        try:
            with self._write_con() as wcon:
                wcon.execute("""
                    UPDATE hive_missions
                    SET status = 'process',
                        sources_harvested = ?,
                        total_words = ?,
                        harvest_done_at = ?
                    WHERE id = ?
                """, [harvested, total_words, _now_utc(), self.mission_id])
                # ── REC-08: Persist quality metrics to quality_metrics JSON col ──
                try:
                    _qm = _json.dumps({
                        "avg_quality":     round(avg_quality, 3),
                        "unique_domains":  unique_domains,
                        "method_dist":     method_dist,
                        "success_rate":    round(harvested / max(total_sources, 1), 3),
                        "total_words":     total_words,
                    })
                    wcon.execute(
                        "UPDATE hive_missions SET quality_metrics = ? WHERE id = ?",
                        [_qm, self.mission_id]
                    )
                except Exception:
                    pass  # quality_metrics col may not exist in older DBs
        except Exception as exc:
            log.warning(f"Could not update hive_missions: {exc}")

    def _print_summary(self, total_sources: int):
        total = len(self.results)
        success = sum(1 for r in self.results if r.get("success"))
        total_words = sum(r.get("word_count", 0) for r in self.results)
        rate = (success / total * 100) if total else 0

        log.debug("\n" + "=" * 60)
        log.debug(f"  HIVE 2.0 HARVEST SUMMARY — Mission: {self.mission_id}")
        log.debug("=" * 60)
        log.debug(f"  Sources loaded   : {total_sources}")
        log.debug(f"  Total harvested  : {success}/{total}")
        log.debug(f"  Total words      : {total_words:,}")
        log.debug(f"  Success rate     : {rate:.1f}%")
        log.debug("")
        log.debug("  By method:")
        for method, count in sorted(self._method_counts.items()):
            log.debug(f"    {method:<30} {count}")
        log.debug("=" * 60 + "\n")
        counts = self.__dict__.setdefault("_skip_counts", {})
        skip_summary(
            self.mission_id, counts, getattr(self, "_loaded_count", total_sources),
            counts.get("domain_blacklist", 0),
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="HIVE 2.0 HarvesterBee — fetch content from hive_sources"
    )
    parser.add_argument("mission_id", help="Mission UUID to harvest")
    parser.add_argument(
        "--workers", "-w",
        type=int, default=DEFAULT_WORKERS,
        help=f"Number of async workers (default: {DEFAULT_WORKERS})"
    )
    args = parser.parse_args()

    bee = HarvesterBee(mission_id=args.mission_id, workers=args.workers)
    asyncio.run(bee.run())


if __name__ == "__main__":
    main()
