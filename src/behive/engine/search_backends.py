"""Multi-backend web search for BeHive Scout.

Priority order (first available wins):
1. Chromium/Playwright (Google/Bing scraping — FREE, unlimited, built into Docker)
2. SearXNG (self-hosted, no rate limits, no blocking)
3. Brave Search (fast, generous free tier: 2000 req/month)  
4. Serper.dev (Google results via API, 2500 free credits)
5. Tavily (research-optimized, 1000 free/month)
6. DuckDuckGo (always available, slow, rate-limited, easy to block)

Usage:
    from behive.engine.search_backends import SearchEngine, search
    
    engine = SearchEngine()  # Auto-detects available backends
    results = await engine.search("quantum computing breakthroughs", max_results=30)
    # Each result: {"url": str, "title": str, "snippet": str, "source": str}
"""

import os
import logging
import asyncio
import aiohttp
from typing import Optional
from urllib.parse import quote_plus

log = logging.getLogger(__name__)


class SearchBackend:
    """Base class for search backends."""
    name: str = "base"
    priority: int = 99
    
    def __init__(self):
        self.available = self._check_available()
    
    def _check_available(self) -> bool:
        return False
    
    async def search(self, query: str, max_results: int = 30, 
                     session: Optional[aiohttp.ClientSession] = None) -> list[dict]:
        raise NotImplementedError


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
    
    async def search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None) -> list[dict]:
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


class SearXNGBackend(SearchBackend):
    """SearXNG — self-hosted, unlimited, no rate limits."""
    name = "searxng"
    priority = 1
    
    def _check_available(self) -> bool:
        self.url = os.environ.get("SEARXNG_URL", "").rstrip("/")
        return bool(self.url)
    
    async def search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None) -> list[dict]:
        if not self.url:
            return []
        
        params = {
            "q": query,
            "format": "json",
            "pageno": 1,
            "categories": "general",
            "language": "en",
        }
        
        own_session = session is None
        if own_session:
            session = aiohttp.ClientSession()
        
        try:
            results = []
            # Fetch up to 3 pages to get enough results
            pages_needed = (max_results + 9) // 10
            for page in range(1, min(pages_needed + 1, 4)):
                params["pageno"] = page
                async with session.get(
                    f"{self.url}/search",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json(content_type=None)
                    for r in data.get("results", []):
                        results.append({
                            "url": r.get("url", ""),
                            "title": r.get("title", ""),
                            "snippet": r.get("content", ""),
                            "source": "searxng",
                        })
                    if len(results) >= max_results:
                        break
            return results[:max_results]
        except Exception as e:
            log.debug(f"SearXNG error: {e}")
            return []
        finally:
            if own_session:
                await session.close()


class BraveBackend(SearchBackend):
    """Brave Search — fast, 2000 free req/month."""
    name = "brave"
    priority = 2
    
    def _check_available(self) -> bool:
        self.api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
        return bool(self.api_key)
    
    async def search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None) -> list[dict]:
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
                    log.debug(f"Brave HTTP {resp.status}")
                    return []
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
        except Exception as e:
            log.debug(f"Brave error: {e}")
            return []
        finally:
            if own_session:
                await session.close()


class SerperBackend(SearchBackend):
    """Serper.dev — Google SERP API, 2500 free credits."""
    name = "serper"
    priority = 3
    
    def _check_available(self) -> bool:
        self.api_key = os.environ.get("SERPER_API_KEY", "")
        return bool(self.api_key)
    
    async def search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None) -> list[dict]:
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
                    log.debug(f"Serper HTTP {resp.status}")
                    return []
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
        except Exception as e:
            log.debug(f"Serper error: {e}")
            return []
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
    
    async def search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None) -> list[dict]:
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
                    log.debug(f"Tavily HTTP {resp.status}")
                    return []
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
        except Exception as e:
            log.debug(f"Tavily error: {e}")
            return []
        finally:
            if own_session:
                await session.close()


class DuckDuckGoBackend(SearchBackend):
    """DuckDuckGo — always available fallback. Slow, rate-limited."""
    name = "duckduckgo"
    priority = 10  # Lowest priority — fallback only
    
    def _check_available(self) -> bool:
        return True  # Always available (no API key needed)
    
    async def search(self, query: str, max_results: int = 30,
                     session: Optional[aiohttp.ClientSession] = None) -> list[dict]:
        loop = asyncio.get_event_loop()
        try:
            results = await loop.run_in_executor(None, self._sync_search, query, max_results)
            return results
        except Exception as e:
            log.debug(f"DDG error: {e}")
            return []
    
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

ALL_BACKENDS = [
    PlaywrightBackend,
    SearXNGBackend,
    BraveBackend,
    SerperBackend,
    TavilyBackend,
    DuckDuckGoBackend,
]


class SearchEngine:
    """Multi-backend search engine. Tries backends in priority order.
    
    Falls through to next backend on failure/empty results.
    Chromium (Playwright) is top priority — scrapes Google/Bing for free.
    Set env vars to enable additional backends:
        BEHIVE_BROWSER_SEARCH=0               (disable Chromium)
        SEARXNG_URL=http://localhost:8080     (self-hosted, unlimited)
        BRAVE_SEARCH_API_KEY=BSA...           (2000 free/month)
        SERPER_API_KEY=...                    (2500 free credits)
        TAVILY_API_KEY=tvly-...              (1000 free/month)
        (DuckDuckGo always available as fallback)
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
                     session: Optional[aiohttp.ClientSession] = None) -> list[dict]:
        """Search with fallthrough. Returns results from first backend that succeeds.
        
        Args:
            query: Search query
            max_results: Maximum results to return
            preferred_backend: Force a specific backend (skip priority order)
            session: Shared aiohttp session (optional)
        """
        backends = self.backends
        
        # If preferred backend requested, try it first
        if preferred_backend:
            preferred = [b for b in self.backends if b.name == preferred_backend]
            others = [b for b in self.backends if b.name != preferred_backend]
            backends = preferred + others
        
        for backend in backends:
            try:
                results = await backend.search(query, max_results, session)
                if results:
                    log.debug(f"  Search [{backend.name}] '{query[:40]}': {len(results)} results")
                    return results
                # Empty results — try next backend
                log.debug(f"  Search [{backend.name}] '{query[:40]}': empty, trying next...")
            except Exception as e:
                log.debug(f"  Search [{backend.name}] error: {e}, trying next...")
                continue
        
        log.warning(f"  All search backends failed for: '{query[:60]}'")
        return []
    
    async def search_news(self, query: str, max_results: int = 20,
                          session: Optional[aiohttp.ClientSession] = None) -> list[dict]:
        """Search news specifically (Brave news, DDG news, etc.)."""
        # Try Brave news endpoint first
        for backend in self.backends:
            if isinstance(backend, BraveBackend) and backend.available:
                try:
                    own_session = session is None
                    if own_session:
                        session = aiohttp.ClientSession()
                    try:
                        headers = {
                            "Accept": "application/json",
                            "X-Subscription-Token": backend.api_key,
                        }
                        async with session.get(
                            "https://api.search.brave.com/res/v1/news/search",
                            headers=headers,
                            params={"q": query, "count": min(max_results, 20)},
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json(content_type=None)
                                results = []
                                for r in data.get("results", []):
                                    results.append({
                                        "url": r.get("url", ""),
                                        "title": r.get("title", ""),
                                        "snippet": r.get("description", ""),
                                        "source": "brave_news",
                                    })
                                if results:
                                    return results[:max_results]
                    finally:
                        if own_session:
                            await session.close()
                            session = None
                except Exception:
                    pass
        
        # Fallback: DDG news
        for backend in self.backends:
            if isinstance(backend, DuckDuckGoBackend):
                loop = asyncio.get_event_loop()
                try:
                    results = await loop.run_in_executor(
                        None, self._ddg_news_sync, query, max_results
                    )
                    return results
                except Exception:
                    pass
        
        # Final fallback: regular search with "news" appended
        return await self.search(f"{query} news", max_results, session=session)
    
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
