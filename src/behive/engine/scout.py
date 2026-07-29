"""Scout module — query generation, source discovery, RSS/DDG/API routing, and URL scoring."""

import logging
#!/usr/bin/env python3
"""
HIVE 2.0 — Scout Phase (Queen-Planned Swarm Architecture)
Executes 200+ parallel scouts, each finding 30-40 URLs from a single source+query.


log = logging.getLogger(__name__)
Usage:
    MODE 1 (queen-planned):
        python3 hive2_scout.py <mission_id> --plan <plan_json_file>

    MODE 2 (standalone / testing):
        python3 hive2_scout.py <mission_id> <topic>
"""

import sys
import asyncio
import json
import re
import time
import urllib.request
from datetime import datetime
from urllib.parse import urlparse, quote_plus
from typing import Optional

import hive2_db as _hive_db
import feedparser

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

DB_PATH = ""  # Legacy — PostgreSQL is primary


def _duckdb_connect_retry(path: str = DB_PATH, read_only: bool = False, max_retries: int = 5):
    """Connect via hive2_db (PostgreSQL)."""
    return _hive_db.connect(read_only=read_only)

AUTHORITY_DOMAINS: dict[str, int] = {
    "statista.com":     15,
    "datareportal.com": 15,
    "gsma.com":         15,
    "itu.int":          15,
    "worldbank.org":    15,
    "imf.org":          15,
    "reuters.com":      12,
    "bloomberg.com":    12,
    "ft.com":           12,
    "gov.pl":           10,
    "europa.eu":        10,
    "un.org":           10,
    "arxiv.org":        10,
    "tuik.gov.tr":      15,
    "btk.gov.tr":       13,
    "sabah.com.tr":      8,
    "geostat.ge":       15,
    "civil.ge":         10,
    "pwc.com":          11,
    "mckinsey.com":     11,
    "deloitte.com":     11,
    "gartner.com":      12,
    "idc.com":          12,
    "emarketer.com":    12,
    "forrester.com":    12,
}

FRESHNESS_PATTERNS = [
    (r"\b202[4-9]\b", 10),
    (r"\b202[0-3]\b",  7),
    (r"\b201[5-9]\b",  4),
    (r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+202[4-9]", 10),
]

DEPTH_SIGNALS = [
    r"\bpdf\b", r"\.pdf", r"\breport\b", r"\bstudy\b", r"\banalysis\b",
    r"\bstatistic", r"\bdata\b", r"\bsurvey\b", r"\bwhitepaper\b",
    r"\bresearch\b", r"\bfinding", r"\bresult", r"\bmarket",
]


# ──────────────────────────────────────────────────────────────────────────────
# SwarmScout — main class
# ──────────────────────────────────────────────────────────────────────────────

class SwarmScout:
    """
    Executes a Queen-planned list of scout tasks concurrently with asyncio.gather.
    Each task targets one specific source+query and returns 30-40 URLs.
    """

    def __init__(self, mission_id: str, topic: str = ""):
        self.mission_id = mission_id
        self.topic = topic
        self.db = _duckdb_connect_retry(DB_PATH)
        # Semaphores — maximised for swarm multiplication
        self._ddg_sem   = asyncio.Semaphore(32)   # 32 parallel DDG calls
        self._http_sem  = asyncio.Semaphore(24)   # 24 parallel HTTP fetches
        # Dedicated thread pool for blocking I/O (DDG, feedparser, urllib)
        self._executor  = __import__('concurrent.futures', fromlist=['ThreadPoolExecutor']).ThreadPoolExecutor(max_workers=32)
        self._seen_urls: set[str] = set()
        self._lock = asyncio.Lock()
        self._total_saved = 0
        self._stats: dict[str, int] = {}
        self._init_sequence()

    # ── Init ──────────────────────────────────────────────────────────────────

    def _init_sequence(self):
        """Create/reset hive_sources_id_seq above current max."""
        try:
            max_id = self.db.execute(
                "SELECT COALESCE(MAX(id), 0) FROM hive_sources"
            ).fetchone()[0]
            self.db.execute("DROP SEQUENCE IF EXISTS hive_sources_id_seq")
            self.db.execute(
                f"CREATE SEQUENCE hive_sources_id_seq START {max_id + 500}"
            )
        except Exception as e:
            log.debug(f"  [init_seq] warning: {e}")

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _score_source(self, url: str, title: str, snippet: str,
                      source_type: str) -> dict:
        combined = f"{title} {snippet} {url}".lower()
        topic_words = set(re.findall(r"\w+", self.topic.lower())) - {
            "and", "or", "the", "of", "in", "for", "a", "an"
        }

        # Relevance
        matches = sum(1 for w in topic_words if w in combined)
        score_relevance = min(25, int(matches / max(len(topic_words), 1) * 25))

        # Freshness — default 8 (neutral, nie kara za brak roku w snippecie)
        score_freshness = 0
        for pattern, pts in FRESHNESS_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE):
                score_freshness = max(score_freshness, pts)
        if score_freshness == 0:
            score_freshness = 8  # neutral baseline (previously 3 — punished undated pages)

        # Authority — elevated default values (page exists = worth checking)
        domain = urlparse(url).netloc.lower().lstrip("www.")
        score_authority = 0
        for auth_domain, pts in AUTHORITY_DOMAINS.items():
            if auth_domain in domain:
                score_authority = pts
                break
        if score_authority == 0:
            if any(x in domain for x in [".gov", ".int", ".edu"]):
                score_authority = 12  # was 6 — instytucje publiczne wysoko
            elif any(x in domain for x in [".org"]):
                score_authority = 9   # organizacje
            elif any(x in domain for x in ["news", "press", "media", "journal", "report"]):
                score_authority = 7   # was 4 — media
            elif any(x in domain for x in [".co.", ".com", ".io", ".ai"]):
                score_authority = 7   # was 2 — domeny komercyjne = wartościowe źródła
            else:
                score_authority = 6   # was 2 — pozostałe (not karz za nieznane domeny)

        # Depth
        depth_hits = sum(
            1 for pat in DEPTH_SIGNALS if re.search(pat, combined, re.IGNORECASE)
        )
        score_depth = min(15, depth_hits * 3)

        # Source-type bonus
        type_bonus = {
            "academic": 10, "report": 8, "pdf": 8, "primary": 7,
            "news": 5, "rss": 5, "gnews": 5, "wayback": 6,
            "reddit": 3, "social": 3, "ddg": 4, "perspective": 5,
            "document": 8,
        }.get(source_type.lower(), 3)

        total = score_relevance + score_freshness + score_authority + score_depth + type_bonus
        return {
            "score_total":     min(100, total),
            "score_relevance": score_relevance,
            "score_freshness": score_freshness,
            "score_authority": score_authority,
            "score_depth":     score_depth,
        }

    # ── DB persistence ────────────────────────────────────────────────────────

    def _save_source(self, url: str, title: str, snippet: str,
                     source_type: str, scout_method: str) -> bool:
        """Insert a source. Returns True if newly saved, False if duplicate/empty."""
        url = (url or "").strip()
        if not url:
            return False
        # Fast duplicate check without lock (set is thread-safe for reads)
        if url in self._seen_urls:
            return False

        # ── SUPERSCOUT BLACKLIST — zero garbage, tylko premium sources ──────────
        _BLACKLISTED_DOMAINS = {
            # Encyclopedic noise
            "wikipedia.org", "wiktionary.org", "wikimedia.org", "wikisource.org",
            "britannica.com", "encyclopedia.com", "wikihow.com",
            # Office / productivity apps
            "outlook.com", "office.com", "microsoft.com",
            # Social noise
            "pinterest.com", "pinterest.design", "tumblr.com", "reddit.com",
            "quora.com",   # opinie zamiast danych
            "facebook.com", "instagram.com", "twitter.com", "x.com",
            # E-commerce / spam
            "amazon.com", "ebay.com", "etsy.com", "aliexpress.com",
            "allegro.pl",
            # QR / generator tools
            "the-qrcode-generator.com", "qr-code-generator.com",
            # Misc garbage scouted w poprzednich misjach
            "nibiru.net", "shz.de", "iso.org.tr",
            # Gry / rozrywka
            "roblox.com", "twitch.tv", "youtube.com", "tiktok.com",
            "spotify.com", "netflix.com", "imdb.com",
            # Aggregators without content
            "feedburner.com", "bit.ly", "tinyurl.com", "linktr.ee",
            # Job boards (nie B2B research)
            "indeed.com", "glassdoor.com", "pracuj.pl", "linkedin.com/jobs",
        }
        from urllib.parse import urlparse as _up
        _domain = _up(url).netloc.lower().lstrip("www.")
        if any(_domain == bd or _domain.endswith("." + bd) for bd in _BLACKLISTED_DOMAINS):
            return False

        # ── SUPERSCOUT SCORE GATE — minimum quality ───────────────────────────
        # Preliminary score based on URL+title+snippet alone
        _pre_score = 0
        _combined_pre = f"{url} {title or ''} {snippet or ''}".lower()
        # Bonus za znane premium domeny
        _PREMIUM_DOMAINS = {
            "statista.com", "datareportal.com", "gsma.com", "itu.int",
            "worldbank.org", "imf.org", "reuters.com", "bloomberg.com",
            "ft.com", "mckinsey.com", "gartner.com", "forrester.com",
            "emarketer.com", "idc.com", "pwc.com", "deloitte.com",
            "hbr.org", "techcrunch.com", "wired.com", "venturebeat.com",
            "businessinsider.com", "forbes.com", "wsj.com",
            # PL premium
            "gov.pl", "stat.gov.pl", "nask.pl", "computerworld.pl",
            "spidersweb.pl", "antyweb.pl", "telepolis.pl",
        }
        if any(_domain == pd or _domain.endswith("." + pd) for pd in _PREMIUM_DOMAINS):
            _pre_score += 10
        # Freshness bonus w URL/tytule
        import re as _re
        if _re.search(r"202[4-6]", _combined_pre):
            _pre_score += 5
        # Bonus for research keywords
        for kw in ["report", "study", "data", "statistics", "survey", "research",
                   "market", "analysis", "trend", "benchmark", "raport"]:
            if kw in _combined_pre:
                _pre_score += 2
                break
        # Filter weak sources at scout stage (threshold = 5)
        if _pre_score < 5 and source_type not in ("rss_gov", "rss_industry", "gov_stat"):
            # Sources without any quality signals → skip
            if not any(x in _combined_pre for x in ["saas", "tiktok", "social media", "marketing",
                                                      "b2b", "consulting", "poland", "polska",
                                                      "turkey", "georgia", "linkedin", "roi"]):
                return False

        self._seen_urls.add(url)

        domain = urlparse(url).netloc.lower().lstrip("www.")
        scores = self._score_source(url, title or "", snippet or "", source_type)

        try:
            self.db.execute(
                """
                INSERT INTO hive_sources
                    (id, mission_id, url, domain, title, snippet, source_type,
                     score_total, score_relevance, score_freshness,
                     score_authority, score_depth, status, harvest_method)
                VALUES (nextval('hive_sources_id_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scouted', ?)
                ON CONFLICT DO NOTHING
                """,
                [
                    self.mission_id, url, domain,
                    (title or "")[:512], (snippet or "")[:1024],
                    source_type,
                    scores["score_total"], scores["score_relevance"],
                    scores["score_freshness"], scores["score_authority"],
                    scores["score_depth"],
                    scout_method,
                ],
            )
            self._stats[source_type] = self._stats.get(source_type, 0) + 1
            return True
        except Exception as e:
            log.debug(f"  [save_source] error for {url[:80]}: {e}")
            return False

    # ── Scout method: DDG text ────────────────────────────────────────────────

    async def _ddg_text(self, query: str, region: str = "en-us",
                        max_results: int = 40) -> list[dict]:
        async with self._ddg_sem:
            try:
                try:
                    from ddgs import DDGS
                except ImportError:
                    from duckduckgo_search import DDGS
                results = await asyncio.get_event_loop().run_in_executor(
                    self._executor,
                    lambda: DDGS().text(query, region=region, max_results=max_results),
                )
                return [
                    {"url": r.get("href", ""), "title": r.get("title", ""),
                     "snippet": r.get("body", "")}
                    for r in (results or [])
                ]
            except Exception as e:
                log.debug(f"  [ddg_text] '{query[:60]}': {e}")
                return []

    # ── Scout method: DDG news ────────────────────────────────────────────────

    async def _ddg_news(self, query: str, region: str = "en-us",
                        max_results: int = 40) -> list[dict]:
        async with self._ddg_sem:
            try:
                try:
                    from ddgs import DDGS
                except ImportError:
                    from duckduckgo_search import DDGS
                results = await asyncio.get_event_loop().run_in_executor(
                    self._executor,
                    lambda: DDGS().news(query, region=region, max_results=max_results),
                )
                return [
                    {"url": r.get("url", ""), "title": r.get("title", ""),
                     "snippet": r.get("body", "") or r.get("excerpt", "")}
                    for r in (results or [])
                ]
            except Exception as e:
                log.debug(f"  [ddg_news] '{query[:60]}': {e}")
                return []

    # ── Scout method: DDG site: ───────────────────────────────────────────────

    async def _ddg_site(self, query: str, site: str,
                        max_results: int = 35) -> list[dict]:
        full_query = f"site:{site} {query}" if site else query
        return await self._ddg_text(full_query, max_results=max_results)

    # ── Scout method: DDG filetype:pdf ───────────────────────────────────────

    async def _ddg_filetype(self, query: str, max_results: int = 30) -> list[dict]:
        return await self._ddg_text(f"{query} filetype:pdf", max_results=max_results)

    # ── Scout method: DDG reddit ──────────────────────────────────────────────

    async def _ddg_reddit(self, query: str, max_results: int = 30) -> list[dict]:
        return await self._ddg_text(f"site:reddit.com {query}", max_results=max_results)

    # ── Scout method: DDG academic ────────────────────────────────────────────

    async def _ddg_academic(self, query: str, max_results: int = 25) -> list[dict]:
        full_q = f"site:arxiv.org OR site:scholar.google.com {query}"
        return await self._ddg_text(full_q, max_results=max_results)

    # ── Scout method: Google News RSS ────────────────────────────────────────

    async def _google_rss(self, query: str, lang: str = "en",
                          max_items: int = 40) -> list[dict]:
        async with self._http_sem:
            try:
                country = {
                    "en": "US", "pl": "PL", "tr": "TR", "ka": "GE",
                    "de": "DE", "fr": "FR", "es": "ES",
                }.get(lang, "US")
                encoded = quote_plus(query)
                rss_url = (
                    f"https://news.google.com/rss/search"
                    f"?q={encoded}&hl={lang}&gl={country}&ceid={country}:{lang}"
                )
                loop = asyncio.get_event_loop()
                feed = await loop.run_in_executor(
                    None, lambda: feedparser.parse(rss_url)
                )
                results = []
                for entry in (feed.entries or [])[:max_items]:
                    url = entry.get("link", "")
                    if url:
                        results.append({
                            "url":     url,
                            "title":   entry.get("title", ""),
                            "snippet": entry.get("summary", "")[:512],
                        })
                return results
            except Exception as e:
                log.debug(f"  [google_rss] '{query[:60]}' lang={lang}: {e}")
                return []

    # ── Scout method: Wayback CDX ─────────────────────────────────────────────

    async def _wayback_cdx(self, site: str, query: str,
                           max_results: int = 30) -> list[dict]:
        async with self._http_sem:
            try:
                topic_slug = query.lower().replace(" ", "-")
                cdx_url = (
                    f"http://web.archive.org/cdx/search/cdx"
                    f"?url={site}/*{topic_slug}*"
                    f"&output=json&limit={max_results}"
                    f"&fl=original,timestamp,statuscode"
                    f"&filter=statuscode:200&collapse=urlkey"
                )
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(
                    None,
                    lambda: urllib.request.urlopen(cdx_url, timeout=10).read().decode(),
                )
                rows = json.loads(data)
                results = []
                for row in rows[1:]:  # skip header
                    orig_url = row[0]
                    ts = row[1]
                    results.append({
                        "url":     orig_url,
                        "title":   f"{site} archive ({ts[:8]})",
                        "snippet": "",
                    })
                return results
            except Exception as e:
                log.debug(f"  [wayback_cdx] {site}: {e}")
                return []

    # ── Scout method: Jina search ─────────────────────────────────────────────

    async def _jina_search(self, query: str) -> list[dict]:
        """GET r.jina.ai wrapped Google search — extracts links."""
        async with self._http_sem:
            try:
                from curl_cffi import requests as cffi_requests
                encoded = quote_plus(query)
                url = f"https://r.jina.ai/https://www.google.com/search?q={encoded}"
                loop = asyncio.get_event_loop()
                resp = await loop.run_in_executor(
                    None,
                    lambda: cffi_requests.get(
                        url,
                        headers={"Accept": "application/json"},
                        timeout=10,
                        impersonate="chrome120",
                    ),
                )
                # Extract URLs from markdown-style links [title](url)
                text = resp.text or ""
                hits = re.findall(r"\[([^\]]+)\]\((https?://[^\)]+)\)", text)
                results = []
                for title, link in hits[:40]:
                    if "google.com" not in link and "jina.ai" not in link:
                        results.append({"url": link, "title": title, "snippet": ""})
                return results
            except Exception as e:
                log.debug(f"  [jina_search] '{query[:60]}': {e}")
                return []

    # ── Scout method: curl site-search ───────────────────────────────────────

    async def _curl_sitesearch(self, query: str, site: str) -> list[dict]:
        """curl_cffi GET to a site-specific search."""
        async with self._http_sem:
            try:
                from curl_cffi import requests as cffi_requests
                encoded = quote_plus(f"site:{site} {query}")
                search_url = f"https://www.google.com/search?q={encoded}&num=40"
                loop = asyncio.get_event_loop()
                resp = await loop.run_in_executor(
                    None,
                    lambda: cffi_requests.get(
                        search_url,
                        timeout=10,
                        impersonate="chrome120",
                    ),
                )
                # Extract /url?q= redirects
                hits = re.findall(r'/url\?q=(https?://[^&"]+)', resp.text or "")
                results = []
                seen = set()
                for raw in hits:
                    link = urllib.parse.unquote(raw)
                    if link not in seen and site in link:
                        seen.add(link)
                        results.append({"url": link, "title": "", "snippet": ""})
                return results[:40]
            except Exception as e:
                log.debug(f"  [curl_sitesearch] '{query[:60]}' site={site}: {e}")
                return []

    # ── Task dispatcher ───────────────────────────────────────────────────────

    async def _run_task(self, task: dict) -> int:
        """Execute one scout task. Fan-out: fires multiple engines in parallel per query."""
        method      = task.get("method", "ddg_text")
        query       = task.get("query", self.topic)
        region      = task.get("region", "en-us")
        site        = task.get("site", "")
        source_type = task.get("source", "ddg")
        lang        = task.get("language", task.get("lang", "en"))

        try:
            # ── Fan-out: always fire DDG text + DDG news simultaneously ──────
            # Plus the explicitly requested method if it differs from the above.
            fan_coros = []
            fan_methods = set()

            # Primary (explicitly requested)
            primary_map = {
                "ddg_text":       lambda: self._ddg_text(query, region),
                "ddg_news":       lambda: self._ddg_news(query, region),
                "ddg_site":       lambda: self._ddg_site(query, site),
                "ddg_filetype":   lambda: self._ddg_filetype(query),
                "ddg_reddit":     lambda: self._ddg_reddit(query),
                "ddg_academic":   lambda: self._ddg_academic(query),
                "google_rss":     lambda: self._google_rss(query, lang),
                "wayback_cdx":    lambda: self._wayback_cdx(site, query),
                "jina_search":    lambda: self._jina_search(query),
                "curl_sitesearch":lambda: self._curl_sitesearch(query, site),
            }
            if method in primary_map:
                fan_coros.append(primary_map[method]())
                fan_methods.add(method)

            # Automatic fan-out companions (always bolt these on)
            if "ddg_text" not in fan_methods:
                fan_coros.append(self._ddg_text(query, region))
            if "ddg_news" not in fan_methods:
                fan_coros.append(self._ddg_news(query, region))
            if "google_rss" not in fan_methods and lang in ("en", "pl", "tr"):
                fan_coros.append(self._google_rss(query, lang))

            results_nested = await asyncio.gather(*fan_coros, return_exceptions=True)

            saved = 0
            for result_list in results_nested:
                if isinstance(result_list, Exception) or not result_list:
                    continue
                for r in result_list:
                    url     = r.get("url") or r.get("href", "")
                    title   = r.get("title", "")
                    snippet = r.get("snippet", "") or r.get("body", "")
                    if self._save_source(url, title, snippet, source_type, method):
                        saved += 1

            async with self._lock:
                self._total_saved += saved

            return saved

        except Exception as e:
            log.debug(f"  [task_err] method={method} query={query[:50]}: {e}")
            return 0

    # ── execute_plan: main entry for queen-planned mode ───────────────────────

    async def execute_plan(self, plan: list[dict]) -> int:
        """Run all plan tasks concurrently. Returns total URLs saved."""
        # Pre-load existing URLs
        try:
            existing = self.db.execute(
                "SELECT url FROM hive_sources WHERE mission_id=?",
                [self.mission_id]
            ).fetchall()
            self._seen_urls = {r[0] for r in existing}
        except Exception:
            self._seen_urls = set()

        log.debug(f"\n{'='*62}")
        log.debug(f"  🐝 HIVE 2.0 SWARM SCOUT — {self.mission_id[:16]}")
        log.debug(f"  📋 Executing {len(plan)} scout tasks in parallel")
        log.debug(f"  🔄 Pre-loaded {len(self._seen_urls)} existing URLs")
        log.debug(f"  🕐 Started: {datetime.utcnow().strftime('%H:%M:%S UTC')}")
        log.debug(f"{'='*62}\n")

        t0 = time.time()
        coroutines = [self._run_task(task) for task in plan]
        await asyncio.gather(*coroutines, return_exceptions=True)
        elapsed = time.time() - t0

        # Print breakdown
        try:
            breakdown = self.db.execute(
                "SELECT source_type, COUNT(*) FROM hive_sources "
                "WHERE mission_id=? GROUP BY source_type ORDER BY 2 DESC",
                [self.mission_id]
            ).fetchall()
        except Exception:
            breakdown = list(self._stats.items())

        log.debug(f"\n{'='*62}")
        log.debug(f"  ✅ SWARM COMPLETE in {elapsed:.1f}s")
        log.debug(f"  📊 {self._total_saved} URLs saved this run")
        log.debug(f"{'─'*62}")
        for src, cnt in breakdown:
            bar = "█" * min(40, cnt // 2)
            log.debug(f"     {src:<22} {cnt:>5}  {bar}")
        log.debug(f"{'='*62}\n")

        # Update mission
        try:
            self.db.execute(
                "UPDATE hive_missions SET sources_found=?, status='harvest' WHERE id=?",
                [self._total_saved, self.mission_id],
            )
        except Exception as e:
            log.debug(f"Suppressed in scout.py: {e}")
            try:
                self.db.execute(
                    "UPDATE hive_missions SET sources_found=?, phase='harvest' WHERE id=?",
                    [self._total_saved, self.mission_id],
                )
            except Exception as e:
                log.debug(f"  [db] mission update failed: {e}")

        self.db.close()
        return self._total_saved


# ──────────────────────────────────────────────────────────────────────────────
# Standalone plan generator (50 tasks for testing without Queen)
# ──────────────────────────────────────────────────────────────────────────────

def generate_standalone_plan(topic: str) -> list[dict]:
    """
    Generate a 50-task basic plan when no Queen plan is provided.
    Covers DDG text/news in multiple regions, RSS, filetype, academic, reddit,
    plus site-specific searches.
    """
    t = topic
    _cy = datetime.utcnow().year          # current year (2026, 2027, …)
    _py = _cy - 1                          # previous year
    plan = [
        # ── Core DDG text (multiple regions) ──────────────────────────────
        {"query": t,                                  "region": "pl-pl",  "method": "ddg_text",     "source": "ddg"},
        {"query": f"{t} statistics {_py} {_cy}",     "region": "en-us",  "method": "ddg_text",     "source": "ddg"},
        {"query": f"{t} raport dane",                 "region": "pl-pl",  "method": "ddg_text",     "source": "ddg"},
        {"query": f"{t} market overview",             "region": "en-us",  "method": "ddg_text",     "source": "ddg"},
        {"query": f"{t} data analysis",               "region": "en-us",  "method": "ddg_text",     "source": "ddg"},
        {"query": f"{t} trends {_py}",                "region": "en-us",  "method": "ddg_text",     "source": "ddg"},
        {"query": f"{t} key findings",                "region": "en-us",  "method": "ddg_text",     "source": "ddg"},
        {"query": t,                                  "region": "uk-en",  "method": "ddg_text",     "source": "ddg"},
        {"query": f"{t} market share",                "region": "en-us",  "method": "ddg_text",     "source": "ddg"},
        {"query": f"{t} growth rate",                 "region": "en-us",  "method": "ddg_text",     "source": "ddg"},
        # ── DDG news ──────────────────────────────────────────────────────
        {"query": t,                                  "region": "en-us",  "method": "ddg_news",     "source": "news"},
        {"query": t,                                  "region": "pl-pl",  "method": "ddg_news",     "source": "news"},
        {"query": f"{t} {_py}",                       "region": "en-us",  "method": "ddg_news",     "source": "news"},
        {"query": f"{t} {_cy}",                       "region": "en-us",  "method": "ddg_news",     "source": "news"},
        {"query": f"{t} report",                      "region": "en-us",  "method": "ddg_news",     "source": "news"},
        # ── Google RSS ────────────────────────────────────────────────────
        {"query": t,                                  "lang": "en",       "method": "google_rss",   "source": "rss"},
        {"query": t,                                  "lang": "pl",       "method": "google_rss",   "source": "rss"},
        {"query": f"{t} statistics",                  "lang": "en",       "method": "google_rss",   "source": "rss"},
        # ── filetype:pdf ──────────────────────────────────────────────────
        {"query": f"{t} market report",               "method": "ddg_filetype", "source": "document"},
        {"query": f"{t} statistics annual",           "method": "ddg_filetype", "source": "document"},
        {"query": f"{t} whitepaper",                  "method": "ddg_filetype", "source": "document"},
        # ── Academic ──────────────────────────────────────────────────────
        {"query": f"{t} research paper",              "method": "ddg_academic", "source": "academic"},
        {"query": f"{t} analysis study",              "method": "ddg_academic", "source": "academic"},
        # ── Reddit ────────────────────────────────────────────────────────
        {"query": t,                                  "method": "ddg_reddit",   "source": "reddit"},
        {"query": f"{t} discussion",                  "method": "ddg_reddit",   "source": "reddit"},
        # ── Site-specific DDG ─────────────────────────────────────────────
        {"query": t, "site": "statista.com",          "method": "ddg_site",     "source": "primary"},
        {"query": t, "site": "datareportal.com",      "method": "ddg_site",     "source": "primary"},
        {"query": t, "site": "gsma.com",              "method": "ddg_site",     "source": "primary"},
        {"query": t, "site": "itu.int",               "method": "ddg_site",     "source": "primary"},
        {"query": t, "site": "worldbank.org",         "method": "ddg_site",     "source": "primary"},
        {"query": t, "site": "imf.org",               "method": "ddg_site",     "source": "primary"},
        {"query": t, "site": "europa.eu",             "method": "ddg_site",     "source": "primary"},
        {"query": t, "site": "un.org",                "method": "ddg_site",     "source": "primary"},
        {"query": t, "site": "oecd.org",              "method": "ddg_site",     "source": "report"},
        {"query": t, "site": "mckinsey.com",          "method": "ddg_site",     "source": "report"},
        {"query": t, "site": "deloitte.com",          "method": "ddg_site",     "source": "report"},
        {"query": t, "site": "pwc.com",               "method": "ddg_site",     "source": "report"},
        {"query": t, "site": "gartner.com",           "method": "ddg_site",     "source": "report"},
        {"query": t, "site": "forrester.com",         "method": "ddg_site",     "source": "report"},
        {"query": t, "site": "idc.com",               "method": "ddg_site",     "source": "report"},
        {"query": t, "site": "arxiv.org",             "method": "ddg_site",     "source": "academic"},
        {"query": t, "site": "researchgate.net",      "method": "ddg_site",     "source": "academic"},
        # ── Wayback CDX for top authority domains ─────────────────────────
        {"query": t, "site": "datareportal.com",      "method": "wayback_cdx",  "source": "wayback"},
        {"query": t, "site": "statista.com",          "method": "wayback_cdx",  "source": "wayback"},
        {"query": t, "site": "gsma.com",              "method": "wayback_cdx",  "source": "wayback"},
        # ── Broader queries ───────────────────────────────────────────────
        {"query": f"{t} overview",                    "region": "en-us",  "method": "ddg_text",     "source": "ddg"},
        {"query": f"{t} industry analysis",           "region": "en-us",  "method": "ddg_text",     "source": "ddg"},
        {"query": f"{t} global market",               "region": "en-us",  "method": "ddg_text",     "source": "ddg"},
        {"query": f"{t} forecast",                    "region": "en-us",  "method": "ddg_text",     "source": "ddg"},
        {"query": f"{t} benchmark comparison",        "region": "en-us",  "method": "ddg_text",     "source": "ddg"},
    ]
    return plan


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point."""
    args = sys.argv[1:]

    if len(args) < 2:
        log.debug("Usage:")
        log.debug("  python3 hive2_scout.py <mission_id> --plan <plan_json_file>")
        log.debug("  python3 hive2_scout.py <mission_id> <topic>")
        sys.exit(1)

    mission_id = args[0]
    remaining  = args[1:]

    # ── MODE 1: queen-planned ────────────────────────────────────────────────
    if "--plan" in remaining:
        idx = remaining.index("--plan")
        try:
            plan_file = remaining[idx + 1]
        except IndexError:
            log.debug("Error: --plan requires a path to a JSON file")
            sys.exit(1)

        with open(plan_file, "r") as f:
            plan = json.load(f)

        if not isinstance(plan, list):
            log.debug("Error: plan JSON must be a list of scout task objects")
            sys.exit(1)

        # topic: try to infer from plan or leave blank
        topic = plan[0].get("query", "") if plan else ""
        log.info(f"[hive2_scout] MODE=queen-planned  mission={mission_id}  tasks={len(plan)}")

    # ── MODE 2: standalone ───────────────────────────────────────────────────
    else:
        topic_parts = [a for a in remaining if not a.startswith("--")]
        topic = " ".join(topic_parts)
        if not topic:
            log.debug("Error: topic required in standalone mode")
            sys.exit(1)
        plan = generate_standalone_plan(topic)
        log.info(f"[hive2_scout] MODE=standalone  mission={mission_id}  topic={topic!r}  tasks={len(plan)}")

    scout = SwarmScout(mission_id, topic)
    total = asyncio.run(scout.execute_plan(plan))
    log.debug(f"\n✓ Scout done — {total} URLs discovered for mission {mission_id}")


if __name__ == "__main__":
    main()
