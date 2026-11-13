"""Web discovery through supported public search APIs, without SERP scraping.

Priority: Exa (REST when keyed, otherwise MCP), Parallel, Keenable, keyed
Brave/Serper/Tavily, configured SearXNG, optional DDGS, then Wikipedia fallback.
SearchEngine aggregates up to ten unique URLs per provider. Per-vendor health
and request serialization are shared across engine instances in this process;
refusals open a ten-minute circuit instead of retrying or changing identity.

Usage:
    results = await SearchEngine().search("fuel telemetry suppliers", 20, lang="pl")
    # Each result: {url, title, snippet, source=<backend name>}
"""

import os
import re
import logging
import asyncio
import json
import uuid
import threading
from contextlib import asynccontextmanager
from time import monotonic as _now
from weakref import WeakKeyDictionary
import aiohttp
from typing import Optional
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)


class _VendorHealth:
    def __init__(self):
        self.open_until = 0.0
        self.finished_at = 0.0
        self.slot = threading.Lock()
        self.loop_locks = WeakKeyDictionary()

    @asynccontextmanager
    async def serial(self):
        # asyncio locks serialize a loop; the non-blocking process slot also
        # covers callers using different loops/threads without blocking either.
        loop = asyncio.get_running_loop()
        lock = self.loop_locks.setdefault(loop, asyncio.Lock())
        async with lock:
            while not self.slot.acquire(blocking=False):
                await asyncio.sleep(0.025)
            try:
                yield
            finally:
                self.slot.release()


_HEALTH: dict[str, _VendorHealth] = {}
_CIRCUIT_SECONDS = 600
_VENDOR_SPACING_SECONDS = 0.3


def _breaker_reason(exc: Exception) -> str:
    status = getattr(exc, "status", 0)
    if status in (401, 402, 403, 429) or isinstance(status, int) and status >= 500:
        return f"HTTP {status}"
    text = str(exc).lower()
    named_text = f"{type(exc).__name__.lower()} {text}"
    if "captcha" in named_text:
        return "CAPTCHA"
    if any(marker in named_text for marker in ("rate limit", "rate-limit", "ratelimit", "too many requests", "quota exceeded", "slow down")):
        return "rate limit"
    if re.match(r"\s*(?:http(?:\s+status)?|status(?:\s+code)?)\s*[:=]?\s*(?:40[123]|429|5\d\d)\b", text):
        return "vendor refusal"
    if isinstance(exc, (aiohttp.ClientError, TimeoutError)):
        return "transport unavailable"
    return ""


class SearchBackend:
    """Base class for search backends."""
    name: str = "base"
    priority: int = 99
    
    def __init__(self):
        self.available = self._check_available()
    
    def _check_available(self) -> bool:
        return False
    
    @property
    def healthy(self) -> bool:
        return _now() >= _HEALTH.setdefault(self.name, _VendorHealth()).open_until

    async def _run(self, operation) -> list[dict]:
        state = _HEALTH.setdefault(self.name, _VendorHealth())
        if not self.healthy:
            log.debug("Search [%s] skip: circuit open", self.name)
            return []
        async with state.serial():
            if not self.healthy:
                log.debug("Search [%s] skip: circuit opened while queued", self.name)
                return []
            delay = state.finished_at + _VENDOR_SPACING_SECONDS - _now()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await operation()
            except Exception as exc:
                reason = _breaker_reason(exc)
                if reason:
                    state.open_until = _now() + _CIRCUIT_SECONDS
                    log.info("Search health: vendor=%s circuit=open cooldown=%ss reason=%s", self.name, _CIRCUIT_SECONDS, reason)
                log.debug("Search [%s] failed=%s reason=%s", self.name, type(exc).__name__, reason or "invalid response")
                return []
            finally:
                state.finished_at = _now()

    async def search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None, *, lang: Optional[str] = None) -> list[dict]:
        if not self.available or max_results <= 0 or not (query or "").strip():
            return []
        return await self._run(lambda: self._search(query, max_results, session, lang=lang))

    async def _search(self, query: str, max_results: int = 30,
                      session: Optional[aiohttp.ClientSession] = None, *, lang: Optional[str] = None) -> list[dict]:
        raise NotImplementedError


class BackendError(RuntimeError):
    """A provider transport, protocol, or tool failure (never a page result)."""

    def __init__(self, reason: str, status: int = 0):
        super().__init__(reason)
        self.status = status


_PARALLEL_SESSION_ID = uuid.uuid4().hex  # Anonymous, per-process rate correlation.


def _parse_mcp_body(body: str) -> str:
    # SSE uses CR/LF, not splitlines(): U+0085/U+2028 can occur in page text.
    candidates = [body.strip()] if body.lstrip().startswith("{") else []
    candidates.extend(line[5:].strip() for line in re.split(r"\r\n|\r|\n", body) if line.startswith("data:"))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("error"):
            error = data["error"]
            raise BackendError(str(error.get("message", error) if isinstance(error, dict) else error))
        if "result" not in data:
            continue
        result = data.get("result") or {}
        texts = [str(c["text"]) for c in result.get("content", []) if isinstance(c, dict) and c.get("text")]
        text = "\n".join(texts)
        if result.get("isError"):
            raise BackendError(text or "MCP tool error")
        if text:
            return text
    reason = _breaker_reason(BackendError(body[:2000]))
    raise BackendError(reason or "MCP response contained no text content")


def _parse_exa_text(text: str, limit: int) -> list[dict]:
    rows = []
    labels = ("Title:", "URL:", "Highlights:", "Published:", "Author:")
    for block in re.split(r"\r?\n---\r?\n", text):
        title = url = ""
        highlights = []
        in_highlights = False
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("Title:"):
                title = line[6:].strip()
            elif line.startswith("URL:"):
                url = line[4:].strip()
            if line.startswith(labels):
                in_highlights = line.startswith("Highlights:")
                if in_highlights and line[11:].strip():
                    highlights.append(line[11:].strip())
            elif in_highlights and line:
                highlights.append(line)
        if url:
            rows.append({"url": url, "title": title, "snippet": " ".join(highlights), "source": "exa"})
        if len(rows) >= limit:
            break
    return rows


class HTTPBackend(SearchBackend):
    """Minimal aiohttp transport shared by the public search providers."""

    async def _request(self, method, url, session=None, **kwargs):
        own = session is None
        if own:
            session = aiohttp.ClientSession(headers={"User-Agent": "behive"})
        try:
            async with session.request(method, url, timeout=aiohttp.ClientTimeout(total=30), **kwargs) as response:
                body = await response.text(encoding="utf-8", errors="replace")
                if response.status >= 400:
                    raise BackendError(f"HTTP {response.status}", response.status)
                try:
                    envelope = json.loads(body)
                except ValueError:
                    envelope = None  # MCP SSE is parsed by _parse_mcp_body.
                if isinstance(envelope, dict) and envelope.get("error"):
                    error = envelope["error"]
                    raise BackendError(str(error.get("message", error) if isinstance(error, dict) else error))
                return body
        finally:
            if own:
                await session.close()

    async def _mcp_call(self, url, tool, arguments, session):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        body = await self._request("POST", url, session, json=payload, headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": "behive",
        })
        return _parse_mcp_body(body)


class ExaBackend(HTTPBackend):
    name = "exa"
    priority = -30

    def _check_available(self):
        self.api_key = os.environ.get("EXA_API_KEY", "")
        return True

    async def _search(self, query, max_results=30, session=None, *, lang=None):
        if self.api_key:
            data = json.loads(await self._request("POST", "https://api.exa.ai/search", session,
                headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
                json={"query": query, "numResults": max(1, max_results), "contents": {"highlights": True}}))
            return [{"url": r.get("url", ""), "title": r.get("title", ""), "snippet": " ".join(r.get("highlights") or []) or r.get("text", ""), "source": self.name}
                    for r in (data.get("results") or [])[:max_results]]
        text = await self._mcp_call("https://mcp.exa.ai/mcp", "web_search_exa", {"query": query, "numResults": max(1, max_results)}, session)
        rows = _parse_exa_text(text, max_results)
        reason = _breaker_reason(BackendError(text)) if not rows else ""
        if reason:
            raise BackendError(reason)
        return rows


class ParallelBackend(HTTPBackend):
    name = "parallel"
    priority = -20

    def _check_available(self):
        self.api_key = os.environ.get("PARALLEL_API_KEY", "")
        return True

    async def _search(self, query, max_results=30, session=None, *, lang=None):
        if self.api_key:
            # Current official REST contract: /v1/search has no max_results field.
            text = await self._request("POST", "https://api.parallel.ai/v1/search", session,
                headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
                json={"objective": query, "search_queries": [query], "mode": "fast"})
        else:
            text = await self._mcp_call("https://search.parallel.ai/mcp", "web_search", {
                "objective": query, "search_queries": [query], "session_id": _PARALLEL_SESSION_ID,
            }, session)
        data = json.loads(text)
        if data.get("error"):
            raise BackendError(str(data["error"]))
        return [{"url": r.get("url", ""), "title": r.get("title", ""), "snippet": " ".join(r.get("excerpts") or []), "source": self.name}
                for r in (data.get("results") or [])[:max_results]]


class KeenableBackend(HTTPBackend):
    name = "keenable"
    priority = -10

    def _check_available(self):
        self.api_key = os.environ.get("KEENABLE_API_KEY", "")
        return True

    async def _search(self, query, max_results=30, session=None, *, lang=None):
        headers = {"X-Keenable-Title": "behive", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        path = "/v1/search" if self.api_key else "/v1/search/public"
        data = json.loads(await self._request("POST", f"https://api.keenable.ai{path}", session,
            headers=headers, json={"query": query, "max_results": max(1, max_results)}))
        return [{"url": r.get("url", ""), "title": r.get("title", ""), "snippet": r.get("snippet") or r.get("description") or "", "source": self.name}
                for r in (data.get("results") or [])[:max_results]]


class PlaywrightBackend(SearchBackend):
    """Chromium/Playwright — scrapes Google/Bing directly. FREE, unlimited.
    
    Available when playwright is installed (included in Docker image).
    No API keys needed. Rotates between Google and Bing to avoid rate limits.
    Set BEHIVE_BROWSER_SEARCH=0 to disable.
    """
    name = "chromium"
    priority = 0  # Highest priority
    
    def _check_available(self) -> bool:
        if os.environ.get("BEHIVE_BROWSER_SEARCH", "1") == "0":
            return False
        try:
            import playwright  # noqa: F401
            return True
        except ImportError:
            return False
    
    async def _search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None, *, lang: Optional[str] = None) -> list[dict]:
        """Search Google/Bing via headless Chromium."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return []
        
        results = []
        
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
                )
                try:
                    context = await browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        locale="en-US",
                        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
                    )
                    page = await context.new_page()
                    page.set_default_timeout(15000)
                    
                    # Try Google first, fallback to Bing
                    try:
                        results = await self._scrape_google(page, query, max_results)
                    except Exception as e:
                        log.debug(f"Google scrape failed: {e}, trying Bing")
                        try:
                            results = await self._scrape_bing(page, query, max_results)
                        except Exception as e2:
                            log.debug(f"Bing scrape also failed: {e2}")
                    
                finally:
                    await browser.close()
        except Exception as e:
            log.warning(f"Playwright/Chromium crashed: {e} — falling through to next backend")
            return []
        
        return results[:max_results]
    
    async def _scrape_google(self, page, query: str, max_results: int) -> list[dict]:
        """Scrape Google search results."""
        url = f"https://www.google.com/search?q={quote_plus(query)}&num={min(max_results, 50)}&hl=en"
        await page.goto(url, wait_until="domcontentloaded")
        
        # Wait for results
        await page.wait_for_selector("div#search", timeout=10000)
        
        results = await page.evaluate("""() => {
            const results = [];
            // Standard search results
            const items = document.querySelectorAll('div.g, div[data-sokoban-container]');
            items.forEach(item => {
                const link = item.querySelector('a[href^="http"]');
                const title = item.querySelector('h3');
                const snippet = item.querySelector('[data-sncf], .VwiC3b, [style*="-webkit-line-clamp"]');
                if (link && title) {
                    results.push({
                        url: link.href,
                        title: title.textContent.trim(),
                        snippet: snippet ? snippet.textContent.trim() : '',
                        source: 'google_chromium'
                    });
                }
            });
            return results;
        }""")
        
        if not results:
            raise ValueError("No results extracted from Google")
        
        return results
    
    async def _scrape_bing(self, page, query: str, max_results: int) -> list[dict]:
        """Scrape Bing search results."""
        url = f"https://www.bing.com/search?q={quote_plus(query)}&count={min(max_results, 50)}&mkt=en-US&setlang=en"
        
        # Bing may redirect (language/consent) — wait for final load
        await page.goto(url, wait_until="networkidle", timeout=20000)
        
        # Wait for results container (may appear after redirect)
        try:
            await page.wait_for_selector("#b_results li.b_algo", timeout=10000)
        except Exception:
            # Try without b_algo class (different layout)
            await page.wait_for_selector("#b_results li", timeout=5000)
        
        results = await page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('#b_results > li.b_algo').forEach(item => {
                const linkEl = item.querySelector('h2 a');
                const snippet = item.querySelector('.b_caption p');
                if (!linkEl) return;
                
                let url = linkEl.href;
                // Bing wraps URLs in /ck/a tracking redirect — extract real URL from cite
                if (url.includes('/ck/a') || url.includes('bing.com')) {
                    const cite = item.querySelector('cite');
                    if (cite) {
                        let citeUrl = cite.textContent.trim()
                            .replace(/\\s*›\\s*/g, '/')
                            .replace(/\\.\\.\\.$/, '');
                        if (!citeUrl.startsWith('http')) citeUrl = 'https://' + citeUrl;
                        url = citeUrl;
                    }
                }
                
                results.push({
                    url: url,
                    title: linkEl.textContent.trim(),
                    snippet: snippet ? snippet.textContent.trim() : '',
                    source: 'bing_chromium'
                });
            });
            return results;
        }""")
        
        if not results:
            raise ValueError("No results extracted from Bing")
        
        return results


class SearXNGBackend(HTTPBackend):
    """Configured SearXNG instance; engine failures are not successful searches."""
    name = "searxng"
    priority = 5
    _ACADEMIC = re.compile(r"\b(?:arxiv|pubmed|google scholar|academic|scholarly|research papers?|scientific papers?|peer-reviewed|badania naukowe|publikacje naukowe|artykuły naukowe)\b", re.I)

    def _check_available(self) -> bool:
        self.url = os.environ.get("SEARXNG_URL", "").rstrip("/")
        return bool(self.url)

    async def _search(self, query, max_results=30, session=None, *, lang=None):
        academic = bool(self._ACADEMIC.search(query))
        params = {"q": query, "format": "json", "pageno": 1, "categories": "science" if academic else "general"}
        if lang:
            params["language"] = lang
        data = json.loads(await self._request("GET", f"{self.url}/search", session, params=params))
        unresponsive = data.get("unresponsive_engines") or []
        failed = {str(e[0]).lower() for e in unresponsive if isinstance(e, (list, tuple)) and e}
        rows = data.get("results") or []
        # A 200 with no live engine output and refusal diagnostics is an outage,
        # not an empty-but-healthy search; don't fan it out again for every task.
        contributing = {str(engine).lower() for row in rows for engine in (row.get("engines") or [row.get("engine")]) if engine}
        if unresponsive and (not rows or contributing and contributing <= failed):
            raise BackendError("all SearXNG engines unresponsive", 503)
        if unresponsive:
            log.debug("Search [searxng] degraded: unresponsive_engines=%s", ",".join(sorted(failed)))
        if not academic:
            rows = [row for row in rows if row.get("category") in (None, "general") and not any(
                e in {"arxiv", "pubmed", "google scholar", "crossref", "wikipedia", "wikidata"}
                for e in (row.get("engines") or [row.get("engine")])
            )]
        return [{"url": r.get("url", ""), "title": r.get("title", ""), "snippet": r.get("content", ""), "source": self.name}
                for r in rows[:max_results]]


class BraveBackend(HTTPBackend):
    """Brave Search — fast, 2000 free req/month."""
    name = "brave"
    priority = 2
    
    def _check_available(self) -> bool:
        self.api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
        return bool(self.api_key)
    
    async def _search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None, *, lang: Optional[str] = None) -> list[dict]:
        if not self.api_key:
            return []
        
        own_session = session is None
        if own_session:
            session = aiohttp.ClientSession()
        
        try:
            headers = {
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": self.api_key,
            }
            params = {"q": query, "count": min(max_results, 20)}
            
            async with session.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    raise BackendError(f"HTTP {resp.status}", resp.status)
                data = await resp.json(content_type=None)
                results = []
                for r in data.get("web", {}).get("results", []):
                    results.append({
                        "url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "snippet": r.get("description", ""),
                        "source": "brave",
                    })
                return results[:max_results]
        finally:
            if own_session:
                await session.close()


    async def _news(self, query, max_results, session=None, *, lang=None):
        params = {"q": query, "count": min(max_results, 20)}
        if lang:
            params["search_lang"] = lang.split("-")[0]
        data = json.loads(await self._request("GET", "https://api.search.brave.com/res/v1/news/search", session,
            headers={"Accept": "application/json", "X-Subscription-Token": self.api_key}, params=params))
        return [{"url": r.get("url", ""), "title": r.get("title", ""), "snippet": r.get("description", ""), "source": "brave_news"}
                for r in (data.get("results") or [])[:max_results]]


class SerperBackend(SearchBackend):
    """Serper.dev — Google SERP API, 2500 free credits."""
    name = "serper"
    priority = 3
    
    def _check_available(self) -> bool:
        self.api_key = os.environ.get("SERPER_API_KEY", "")
        return bool(self.api_key)
    
    async def _search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None, *, lang: Optional[str] = None) -> list[dict]:
        if not self.api_key:
            return []
        
        own_session = session is None
        if own_session:
            session = aiohttp.ClientSession()
        
        try:
            headers = {
                "X-API-KEY": self.api_key,
                "Content-Type": "application/json",
            }
            payload = {"q": query, "num": min(max_results, 30)}
            
            async with session.post(
                "https://google.serper.dev/search",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    raise BackendError(f"HTTP {resp.status}", resp.status)
                data = await resp.json(content_type=None)
                results = []
                for r in data.get("organic", []):
                    results.append({
                        "url": r.get("link", ""),
                        "title": r.get("title", ""),
                        "snippet": r.get("snippet", ""),
                        "source": "serper",
                    })
                return results[:max_results]
        finally:
            if own_session:
                await session.close()


class TavilyBackend(SearchBackend):
    """Tavily — research-optimized search, 1000 free/month."""
    name = "tavily"
    priority = 4
    
    def _check_available(self) -> bool:
        self.api_key = os.environ.get("TAVILY_API_KEY", "")
        return bool(self.api_key)
    
    async def _search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None, *, lang: Optional[str] = None) -> list[dict]:
        if not self.api_key:
            return []
        
        own_session = session is None
        if own_session:
            session = aiohttp.ClientSession()
        
        try:
            payload = {
                "api_key": self.api_key,
                "query": query,
                "max_results": min(max_results, 20),
                "search_depth": "advanced",
                "include_answer": False,
            }
            
            async with session.post(
                "https://api.tavily.com/search",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    raise BackendError(f"HTTP {resp.status}", resp.status)
                data = await resp.json(content_type=None)
                results = []
                for r in data.get("results", []):
                    results.append({
                        "url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "snippet": r.get("content", "")[:300],
                        "source": "tavily",
                    })
                return results[:max_results]
        finally:
            if own_session:
                await session.close()


class WikipediaBackend(SearchBackend):
    """MediaWiki search — works when Google/DDG/SearXNG are CAPTCHA'd."""
    name = "wikipedia"
    priority = 90
    # Wikimedia refuses (403) requests without a descriptive agent. Sent on every
    # request: SearchEngine's shared session carries no default headers.
    USER_AGENT = "BeHive/0.6 (https://api.behive.site; wikipedia-fallback)"
    _sem = asyncio.Semaphore(2)
    _cache: dict = {}
    _JUNK = re.compile(
        r"\bsite:\S+|\bfiletype:\S+|\bOR\b|\b(news|raport|dane|whitepaper|"
        r"overview|discussion|filetype|pdf)\b",
        re.I,
    )

    @classmethod
    def _norm(cls, query: str) -> str:
        q = cls._JUNK.sub(" ", query or "")
        q = " ".join(q.split())
        return q or (query or "").strip()

    def _check_available(self) -> bool:
        return True

    async def _search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None, *, lang: Optional[str] = None) -> list[dict]:
        language = (lang or "en").lower().split("-")[0]
        if not re.fullmatch(r"[a-z]{2,3}", language):
            language = "en"
        normalized = WikipediaBackend._norm(query).lower()
        key = (language, normalized, min(max_results, 10))
        if not normalized:
            return []
        if key in WikipediaBackend._cache:
            return WikipediaBackend._cache[key][:max_results]
        own = session is None
        if own:
            session = aiohttp.ClientSession(headers={"User-Agent": WikipediaBackend.USER_AGENT})
        try:
            async with WikipediaBackend._sem:
                params = {
                    "action": "query",
                    "list": "search",
                    "srsearch": WikipediaBackend._norm(query),
                    "srlimit": str(min(max(max_results, 1), 10)),
                    "format": "json",
                    "utf8": "1",
                }
                async with session.get(
                    f"https://{language}.wikipedia.org/w/api.php",
                    params=params,
                    headers={"User-Agent": WikipediaBackend.USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        raise BackendError(f"HTTP {resp.status}", resp.status)
                    data = await resp.json(content_type=None)
            out = []
            for hit in (data.get("query") or {}).get("search") or []:
                title = hit.get("title") or ""
                if not title:
                    continue
                slug = title.replace(" ", "_")
                snippet = hit.get("snippet") or ""
                snippet = re.sub(r"<[^>]+>", "", snippet)
                out.append({
                    "url": f"https://{language}.wikipedia.org/wiki/{quote_plus(slug)}",
                    "title": title,
                    "snippet": snippet,
                    "source": "wikipedia",
                })
            WikipediaBackend._cache[key] = out
            return out[:max_results]
        finally:
            if own:
                await session.close()


class DuckDuckGoBackend(SearchBackend):
    """DuckDuckGo — always available fallback. Slow, rate-limited."""
    name = "duckduckgo"
    priority = 10  # Lowest priority — fallback only
    
    def _check_available(self) -> bool:
        return True  # Always available (no API key needed)
    
    async def _search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None, *, lang: Optional[str] = None) -> list[dict]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync_search, query, max_results)
    
    def _sync_search(self, query: str, max_results: int) -> list[dict]:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "url": r.get("href", r.get("link", "")),
                    "title": r.get("title", ""),
                    "snippet": r.get("body", r.get("snippet", "")),
                    "source": "duckduckgo",
                })
        return results[:max_results]


# ─── Search Engine (auto-selects best backend) ────────────────────────────────

def normalize_url(url: str) -> str:
    """Stable HTTP URL identity; keep meaningful query parameters, drop trackers."""
    try:
        parts = urlsplit((url or "").strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return ""
        host = parts.hostname.lower().removeprefix("www.")
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")))
        return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), query, ""))
    except (TypeError, ValueError):
        return ""


ALL_BACKENDS = [
    ExaBackend,
    ParallelBackend,
    KeenableBackend,
    SearXNGBackend,
    BraveBackend,
    SerperBackend,
    TavilyBackend,
    WikipediaBackend,
    DuckDuckGoBackend,
]


class SearchEngine:
    """Aggregate provider-ranked web hits; never retry a refused provider.

    Exa/Parallel/Keenable public tiers need no credentials. Setting EXA_API_KEY,
    PARALLEL_API_KEY or KEENABLE_API_KEY selects that vendor's official REST
    endpoint, with no anonymous fallback for a refused key. BRAVE_SEARCH_API_KEY,
    SERPER_API_KEY, TAVILY_API_KEY and SEARXNG_URL retain their existing roles.
    The legacy Playwright helper is deliberately not in ALL_BACKENDS.
    """
    
    def __init__(self):
        self.backends: list[SearchBackend] = []
        for cls in ALL_BACKENDS:
            backend = cls()
            if backend.available:
                self.backends.append(backend)
        self.backends.sort(key=lambda b: b.priority)
        
        available_names = [b.name for b in self.backends]
        log.info(f"SearchEngine initialized: {available_names}")
    
    @property
    def primary_backend(self) -> str:
        """Name of the highest-priority available backend."""
        return self.backends[0].name if self.backends else "none"
    
    @property
    def available_backends(self) -> list[str]:
        return [b.name for b in self.backends]
    
    async def search(self, query: str, max_results: int = 30,
                     preferred_backend: Optional[str] = None,
                     session: Optional[aiohttp.ClientSession] = None, *, lang: Optional[str] = None) -> list[dict]:
        """Aggregate healthy web providers, preserving their ranking and query text.

        A provider contributes at most ten URLs. ``preferred_backend`` changes
        order, not failover semantics; the first four arguments remain positional.
        ``lang`` is passed separately only where the provider supports it.
        """
        if not (query or "").strip() or max_results <= 0:
            return []
        backends = list(self.backends)
        if preferred_backend:
            backends.sort(key=lambda b: b.name != preferred_backend)
        collected: list[dict] = []
        seen: set[str] = set()
        for backend in backends:
            if len(collected) >= max_results:
                break
            if backend.name == "wikipedia" and collected:
                log.debug("Search [wikipedia] skip: web providers succeeded")
                continue
            limit = min(10, max_results - len(collected))
            try:
                kwargs = {"lang": lang} if lang is not None else {}
                results = await backend.search(query, limit, session, **kwargs)
                added = 0
                for row in results:
                    url = normalize_url(row.get("url", ""))
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    collected.append({"url": url, "title": row.get("title") or "", "snippet": row.get("snippet") or "", "source": backend.name})
                    added += 1
                    if added >= limit:
                        break
                log.debug("Search [%s] query=%r received=%d unique=%d total=%d", backend.name, query[:80], len(results), added, len(collected))
            except Exception as exc:
                log.debug("Search [%s] query=%r failed=%s", backend.name, query[:80], type(exc).__name__)
        if not collected:
            log.warning("All search backends failed for: %r", query[:60])
        return collected
    
    async def search_news(self, query: str, max_results: int = 20,
                          session: Optional[aiohttp.ClientSession] = None, *, lang: Optional[str] = None) -> list[dict]:
        """News endpoints share each vendor's web-search limiter and breaker."""
        if max_results <= 0 or not (query or "").strip():
            return []
        for backend in self.backends:
            if isinstance(backend, BraveBackend) and backend.available:
                results = await backend._run(lambda: backend._news(query, max_results, session, lang=lang))
                if results:
                    return results[:max_results]
        for backend in self.backends:
            if isinstance(backend, DuckDuckGoBackend) and backend.available:
                loop = asyncio.get_running_loop()
                results = await backend._run(lambda: loop.run_in_executor(None, self._ddg_news_sync, query, max_results))
                if results:
                    return results[:max_results]
        return await self.search(f"{query} news", max_results, session=session, lang=lang)
    
    @staticmethod
    def _ddg_news_sync(query: str, max_results: int) -> list[dict]:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        
        results = []
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "url": r.get("url", r.get("link", "")),
                    "title": r.get("title", ""),
                    "snippet": r.get("body", ""),
                    "source": "duckduckgo_news",
                })
        return results


# ─── Convenience function ─────────────────────────────────────────────────────

_engine: Optional[SearchEngine] = None

def get_engine() -> SearchEngine:
    """Get or create the global SearchEngine instance."""
    global _engine
    if _engine is None:
        _engine = SearchEngine()
    return _engine

async def search(query: str, max_results: int = 30, **kwargs) -> list[dict]:
    """Convenience: search with auto-selected backend."""
    engine = get_engine()
    return await engine.search(query, max_results, **kwargs)

async def search_news(query: str, max_results: int = 20, **kwargs) -> list[dict]:
    """Convenience: news search."""
    engine = get_engine()
    return await engine.search_news(query, max_results, **kwargs)
