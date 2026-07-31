#!/usr/bin/env python3
"""
HIVE 2.0 — Drone Layer (Trutnie / Wall-Breakers) v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Doktryna z dokumentu:
  "Trutnie – drony pełniące funkcję wall-breakerów lub agentów mających za
   zadanie obejść strukturę oprogramowania baz danych. Torują one drogę
   następnemu rodzajowi dronów zewnętrznych. Po wykonaniu zadania wycofują
   się jako ostatnie, zacierając cyfrowy ślad działalności roju."

ARSENAŁ v2.0 — 8-poziomowa eskalacja:
  1. DIRECT          — plain aiohttp, full Chrome headers
  2. ROTATE_UA       — rotacja 10 UA + delay
  3. CURL_CFFI       — TLS JA4 impersonation (chrome146, firefox147)
  4. PRIMP           — Rust TLS via rquest (chrome_148, safari_26.3)
  5. NODRIVER        — headless Chrome CDP, zero WebDriver leaks
  6. PATCHRIGHT      — Playwright fork z TLS patches
  7. JINA_PROXY      — r.jina.ai relay (paywall/captcha bypass)
  8. WAYBACK/ARCHIVE — web.archive.org / archive.ph fallback
  SKIP              — nieobchodzone, flagowane

Kluczowe zmiany v2.0:
  - Pełny zestaw Chrome 131 headers (Sec-Ch-Ua, Sec-Fetch-*, Priority)
  - primp (Rust TLS) jako Layer 4 — szybszy od curl_cffi, nowsze fingerprints
  - nodriver jako Layer 5 — przechodzi CF Bot Management, JS challenges
  - patchright jako Layer 6 — stealth Playwright (brak Runtime.enable/Console.enable leak)
  - archive.ph jako dodatkowy fallback obok Wayback
  - Cookie jar persistence per session
  - Telegram/Reddit/X special handlers

Usage:
    python3 hive2_drones.py <mission_id> [--domains domain1,domain2,...]
    python3 hive2_drones.py <mission_id> --from-sources
    python3 hive2_drones.py <mission_id> --test
"""

import asyncio
import glob
import json
import logging
import os
import random
import re
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("hive2.drones")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

DB_PATH = ""  # Legacy — PostgreSQL is primary

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# User-Agent pool — aktualny Chrome 131 + diverse
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

UA_POOL = [
    # Chrome 131 Windows (default — highest trust)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 131 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 131 Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Firefox 133 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    # Firefox 133 Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    # Safari 18 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    # Edge 131 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    # Chrome Mobile Android
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    # Googlebot (some sites whitelist)
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    # Academic
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
]

# Chrome 131 full headers — these MUST accompany UA to avoid fingerprinting
CHROME_131_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Priority": "u=0, i",
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
}

# Firefox 133 headers (jako alternatywne, gdy Chrome spalony)
FIREFOX_133_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/png,image/svg+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
    "Priority": "u=0, i",
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TLS Impersonation targets
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# curl_cffi 0.15.0 targets (C-based, libcurl)
CFFI_IMPERSONATE_POOL = [
    "chrome131", "chrome136", "chrome142", "chrome145", "chrome146",
    "firefox133", "firefox135", "firefox144", "firefox147",
    "safari17_2", "safari18",
]

# primp 1.3.1 targets (Rust-based, rquest) — NOTE: underscores!
PRIMP_IMPERSONATE_POOL = [
    "chrome_148", "chrome_147", "chrome_146", "chrome_145",
    "firefox_148", "firefox_147", "firefox_146",
    "safari_26.3", "safari_26", "safari_18.5",
    "edge_148", "edge_147",
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Block detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BLOCK_SIGNALS = {
    403: "hard_block",
    429: "rate_limited",
    503: "ddos_protection",
    401: "auth_required",
    302: "redirect",
    301: "redirect",
    451: "geo_block",
}

BODY_BLOCK_PATTERNS = [
    (r"cloudflare", "cloudflare"),
    (r"cf-ray", "cloudflare"),
    (r"just a moment", "cloudflare_js"),
    (r"enable javascript", "js_required"),
    (r"captcha", "captcha"),
    (r"access denied", "hard_block"),
    (r"subscribe to read", "paywall"),
    (r"subscription required", "paywall"),
    (r"sign in to continue", "login_wall"),
    (r"create a free account", "registration_wall"),
    (r"rate limit", "rate_limited"),
    (r"too many requests", "rate_limited"),
    (r"bot detected", "bot_detection"),
    (r"please verify you are a human", "captcha"),
    (r"turnstile", "captcha"),
    (r"datadome", "datadome"),
    (r"perimeterx", "perimeterx"),
    (r"akamai", "akamai_block"),
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Domain classification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

KNOWN_CF_DOMAINS = {
    "medium.com", "substack.com", "bloomberg.com", "wsj.com",
    "ft.com", "economist.com", "wired.com", "technologyreview.com",
    "sciencedirect.com", "elsevier.com", "springer.com", "wiley.com",
    "nature.com", "newscientist.com", "foreignaffairs.com",
}

KNOWN_OPEN_DOMAINS = {
    "arxiv.org", "pubmed.ncbi.nlm.nih.gov", "biorxiv.org", "medrxiv.org",
    "plos.org", "frontiersin.org", "mdpi.com", "semanticscholar.org",
    "researchgate.net", "ssrn.com", "un.org", "worldbank.org",
    "oecd.org", "imf.org", "europa.eu", "gov.pl",
    "reddit.com", "github.com", "wikipedia.org", "wikimedia.org",
    "archive.org", "gov.uk", "state.gov", "treasury.gov",
    "europarl.europa.eu", "eur-lex.europa.eu",
}

# Domains requiring special handler (not typical recon)
SPECIAL_HANDLERS = {
    "linkedin.com":  "PLAYWRIGHT_STEALTH",
    "twitter.com":   "ARCHIVE_FALLBACK",
    "x.com":         "ARCHIVE_FALLBACK",
    "facebook.com":  "PLAYWRIGHT_STEALTH",
    "instagram.com": "ARCHIVE_FALLBACK",
    "t.me":          "TELEGRAM_SCRAPE",
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ReconResult
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ReconResult:
    """Domain reconnaissance result: status, content type, headers, timing."""
    def __init__(self, domain: str):
        self.domain        = domain
        self.strategy      = "DIRECT"
        self.block_type    = None
        self.working_ua    = UA_POOL[0]
        self.cffi_target   = None
        self.primp_target  = None          # NEW: primp impersonation target
        self.robots_blocked: list[str] = []
        self.delay_ms      = 0
        self.via_jina      = False
        self.via_wayback   = False
        self.via_archive_ph = False        # NEW: archive.ph fallback
        self.via_nodriver  = False         # NEW: nodriver CDP
        self.via_patchright = False        # NEW: patchright stealth
        self.probe_status  = None
        self.probe_ms      = 0
        self.notes: list[str] = []

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "domain":          self.domain,
            "strategy":        self.strategy,
            "block_type":      self.block_type,
            "working_ua":      self.working_ua,
            "cffi_target":     self.cffi_target,
            "primp_target":    self.primp_target,
            "robots_blocked":  self.robots_blocked,
            "delay_ms":        self.delay_ms,
            "via_jina":        self.via_jina,
            "via_wayback":     self.via_wayback,
            "via_archive_ph":  self.via_archive_ph,
            "via_nodriver":    self.via_nodriver,
            "via_patchright":  self.via_patchright,
            "probe_status":    self.probe_status,
            "probe_ms":        self.probe_ms,
            "notes":           self.notes,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DroneRecon — 8-level escalation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DroneRecon:
    """
    Trutnie v2.0 — 8-poziomowa eskalacja penetracji.

    Escalation ladder:
      1. Direct (aiohttp + Chrome headers)
      2. UA rotation (10 diverse UAs)
      3. curl_cffi (TLS JA4 impersonation, C-based)
      4. primp (TLS impersonation, Rust-based — nowsze fingerprints)
      5. nodriver (CDP headless Chrome — passes CF Bot Management)
      6. patchright (stealth Playwright — no Runtime.enable leak)
      7. Jina relay proxy (paywall/captcha bypass)
      8. Wayback + archive.ph (archival fallback)
    """

    SEM_LIMIT     = 20
    PROBE_TIMEOUT = 10
    JINA_BASE     = "https://r.jina.ai/"
    ARCHIVE_PH    = "https://archive.ph/newest/"

    def __init__(self, mission_id: str):
        self.mission_id = mission_id
        self._sem = asyncio.Semaphore(self.SEM_LIMIT)
        self._results: dict[str, ReconResult] = {}
        self._cookie_jar: dict[str, dict] = {}
        self._init_db()

    def _init_db(self):
        import hive2_db as _hive_db
        db = _hive_db.connect(read_only=False)
        db.execute("""
            CREATE TABLE IF NOT EXISTS drone_map (
                mission_id    VARCHAR,
                domain        VARCHAR,
                strategy      VARCHAR,
                block_type    VARCHAR,
                working_ua    VARCHAR,
                cffi_target   VARCHAR,
                primp_target  VARCHAR,
                robots_json   VARCHAR,
                delay_ms      INTEGER DEFAULT 0,
                via_jina      BOOLEAN DEFAULT FALSE,
                via_wayback   BOOLEAN DEFAULT FALSE,
                via_archive_ph BOOLEAN DEFAULT FALSE,
                via_nodriver  BOOLEAN DEFAULT FALSE,
                via_patchright BOOLEAN DEFAULT FALSE,
                probe_status  INTEGER,
                probe_ms      INTEGER,
                notes_json    VARCHAR,
                created_at    TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (mission_id, domain)
            )
        """)
        # Migration — add missing columns to old table
        existing = [c[0] for c in db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='drone_map'"
        ).fetchall()]
        for col, typ, dflt in [
            ("primp_target", "VARCHAR", "NULL"),
            ("via_archive_ph", "BOOLEAN", "FALSE"),
            ("via_nodriver", "BOOLEAN", "FALSE"),
            ("via_patchright", "BOOLEAN", "FALSE"),
        ]:
            if col not in existing:
                db.execute(f"ALTER TABLE drone_map ADD COLUMN {col} {typ} DEFAULT {dflt}")
        db.close()

    # ── Chrome-grade headers builder ──────────────────────────────────────────

    def _build_headers(self, ua: str) -> dict:
        """Buduje pełny zestaw headers dopasowany do UA."""
        headers = {"User-Agent": ua}
        if "Firefox" in ua:
            headers.update(FIREFOX_133_HEADERS)
        else:
            headers.update(CHROME_131_HEADERS)
            # Dynamiczne Sec-Ch-Ua z wersji
            if "Edg/" in ua:
                headers["Sec-Ch-Ua"] = '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"'
            if "Macintosh" in ua:
                headers["Sec-Ch-Ua-Platform"] = '"macOS"'
            elif "Linux" in ua:
                headers["Sec-Ch-Ua-Platform"] = '"Linux"'
            elif "Android" in ua:
                headers["Sec-Ch-Ua-Platform"] = '"Android"'
                headers["Sec-Ch-Ua-Mobile"] = "?1"
        return headers

    # ── Probe with full headers ───────────────────────────────────────────────

    async def _probe_url(self, url: str, ua: str,
                         session=None) -> tuple[int, str, float]:
        """GET request z pełnym zestawem Chrome/Firefox headers."""
        import aiohttp
        t0 = time.time()
        headers = self._build_headers(ua)
        try:
            timeout = aiohttp.ClientTimeout(connect=4, total=self.PROBE_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url, headers=headers,
                                    allow_redirects=True,
                                    ssl=False) as resp:
                    elapsed = (time.time() - t0) * 1000
                    body = ""
                    try:
                        raw = await resp.read()
                        body = raw[:3000].decode("utf-8", errors="ignore").lower()
                    except Exception as e:
                        log.debug(f"Suppressed: {e}")
                    return resp.status, body, elapsed
        except asyncio.TimeoutError:
            return 0, "timeout", (time.time() - t0) * 1000
        except Exception as e:
            log.debug(f"Exception in drones.py: {e}")
            return 0, str(e)[:200], (time.time() - t0) * 1000

    # ── Block detection ───────────────────────────────────────────────────────

    def _detect_block(self, status: int, body: str) -> Optional[str]:
        if status in BLOCK_SIGNALS:
            block = BLOCK_SIGNALS[status]
            for pattern, btype in BODY_BLOCK_PATTERNS:
                if re.search(pattern, body, re.IGNORECASE):
                    return btype
            return block
        if status == 200:
            for pattern, btype in BODY_BLOCK_PATTERNS:
                if re.search(pattern, body, re.IGNORECASE):
                    return btype
        return None

    # ── Robots.txt ────────────────────────────────────────────────────────────

    def _check_robots(self, domain: str) -> list[str]:
        blocked = []
        try:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"https://{domain}/robots.txt")
            rp.read()
            for path in ["/search", "/api/", "/data/", "/download/", "/pdf/",
                         "/wp-json/", "/feed/", "/rss/"]:
                if not rp.can_fetch("*", f"https://{domain}{path}"):
                    blocked.append(path)
        except (OSError, Exception) as e:  # network/HTTP
            log.debug(f"HTTP error: {e}")
        return blocked

    # ══════════════════════════════════════════════════════════════════════════
    # LAYERS 3-6 — Heavy weapons
    # ══════════════════════════════════════════════════════════════════════════

    # ── Layer 3: curl_cffi ────────────────────────────────────────────────────

    async def _try_cffi(self, url: str) -> tuple[bool, str, int, str, float]:
        """TLS impersonation via curl_cffi. Returns (success, target, status, body, ms)."""
        try:
            from curl_cffi.requests import Session
        except ImportError:
            return False, "", 0, "no curl_cffi", 0

        loop = asyncio.get_event_loop()
        targets = random.sample(CFFI_IMPERSONATE_POOL, min(4, len(CFFI_IMPERSONATE_POOL)))

        for target in targets:
            try:
                def _do():
                    t0 = time.time()
                    with Session(impersonate=target) as sess:
                        resp = sess.get(url, timeout=self.PROBE_TIMEOUT,
                                       allow_redirects=True)
                        body = (resp.text or "")[:3000].lower()
                        return resp.status_code, body, (time.time() - t0) * 1000

                status, body, ms = await loop.run_in_executor(None, _do)
                if status == 200 and not self._detect_block(status, body):
                    return True, target, status, body, ms
            except Exception as e:
                log.debug(f"Suppressed in drones.py: {e}")
                continue
        return False, "", 0, "all cffi failed", 0

    # ── Layer 4: primp (Rust TLS) ────────────────────────────────────────────

    async def _try_primp(self, url: str) -> tuple[bool, str, int, str, float]:
        """TLS impersonation via primp (Rust/rquest). Returns (success, target, status, body, ms)."""
        try:
            import primp
        except ImportError:
            return False, "", 0, "no primp", 0

        loop = asyncio.get_event_loop()
        targets = random.sample(PRIMP_IMPERSONATE_POOL, min(4, len(PRIMP_IMPERSONATE_POOL)))

        for target in targets:
            try:
                def _do():
                    t0 = time.time()
                    client = primp.Client(
                        impersonate=target,
                        follow_redirects=True,
                        timeout=self.PROBE_TIMEOUT,
                        cookie_store=True,
                        referer=True,
                    )
                    resp = client.get(url)
                    body = (resp.text or "")[:3000].lower()
                    return resp.status_code, body, (time.time() - t0) * 1000

                status, body, ms = await loop.run_in_executor(None, _do)
                if status == 200 and not self._detect_block(status, body):
                    return True, target, status, body, ms
            except Exception as e:
                log.debug(f"Suppressed in drones.py: {e}")
                continue
        return False, "", 0, "all primp failed", 0

    # ── Layer 5: nodriver (headless CDP) ──────────────────────────────────────

    async def _try_nodriver(self, url: str) -> tuple[bool, int, str, float]:
        """Headless Chrome via nodriver — bypasses CF Bot Management."""
        try:
            import nodriver as uc
        except ImportError:
            return False, 0, "no nodriver", 0

        t0 = time.time()
        browser = None
        try:
            browser = await uc.start(
                headless=True,
                browser_args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1920,1080",
                ]
            )
            page = await browser.get(url)
            # Wait for CF challenge to resolve (up to 8s)
            await asyncio.sleep(5)
            content = await page.get_content()
            body = (content or "")[:3000].lower()
            ms = (time.time() - t0) * 1000

            # Check if still blocked
            block = self._detect_block(200, body)
            if block is None and len(body) > 200:
                return True, 200, body, ms
            return False, 200, f"nodriver-blocked: {block}", ms
        except Exception as e:
            log.debug(f"Exception in drones.py: {e}")
            return False, 0, str(e)[:200], (time.time() - t0) * 1000
        finally:
            if browser:
                try:
                    browser.stop()
                except Exception as e:
                    log.debug(f"Suppressed: {e}")

    # ── Layer 6: patchright (stealth Playwright) ──────────────────────────────

    async def _try_patchright(self, url: str) -> tuple[bool, int, str, float]:
        """Stealth Playwright fork — no Runtime.enable/Console.enable leak."""
        try:
            from patchright.async_api import async_playwright
        except ImportError:
            return False, 0, "no patchright", 0

        t0 = time.time()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                context = await browser.new_context(
                    user_agent=UA_POOL[0],
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                )
                page = await context.new_page()
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(3000)
                content = await page.content()
                body = (content or "")[:3000].lower()
                status = resp.status if resp else 0
                ms = (time.time() - t0) * 1000
                await browser.close()

                block = self._detect_block(status, body)
                if block is None and status == 200 and len(body) > 200:
                    return True, status, body, ms
                return False, status, f"patchright-blocked: {block}", ms
        except Exception as e:
            log.debug(f"Exception in drones.py: {e}")
            return False, 0, str(e)[:200], (time.time() - t0) * 1000

    # ── Layer 7: Jina relay ───────────────────────────────────────────────────

    async def _try_jina(self, domain: str) -> tuple[bool, int, str, float]:
        jina_url = f"{self.JINA_BASE}https://{domain}/"
        s, b, t = await self._probe_url(jina_url, UA_POOL[0])
        if s == 200 and not self._detect_block(s, b):
            return True, s, b, t
        return False, s, b, t

    # ── Layer 8: Archive fallbacks ────────────────────────────────────────────

    async def _try_archives(self, domain: str) -> tuple[bool, str, int, str, float]:
        """Try Wayback Machine and archive.ph."""
        # Wayback
        wb_url = f"https://web.archive.org/web/2024/{domain}/"
        s1, b1, t1 = await self._probe_url(wb_url, UA_POOL[0])
        if s1 == 200 and len(b1) > 100:
            return True, "WAYBACK", s1, b1, t1

        # archive.ph
        arch_url = f"{self.ARCHIVE_PH}https://{domain}/"
        s2, b2, t2 = await self._probe_url(arch_url, UA_POOL[0])
        if s2 == 200 and len(b2) > 100:
            return True, "ARCHIVE_PH", s2, b2, t2

        return False, "", 0, "archives failed", 0

    # ══════════════════════════════════════════════════════════════════════════
    # Main recon logic per domain — 8-level escalation
    # ══════════════════════════════════════════════════════════════════════════

    async def _recon_domain(self, domain: str) -> ReconResult:
        result = ReconResult(domain)

        # ── Special handlers (LinkedIn, Twitter, Telegram) ────────────────────
        if any(sd in domain for sd in SPECIAL_HANDLERS):
            for sd, handler in SPECIAL_HANDLERS.items():
                if sd in domain:
                    result.strategy = handler
                    result.notes.append(f"special-handler: {handler}")
                    return result

        # ── Known open domains → DIRECT ───────────────────────────────────────
        if any(od in domain for od in KNOWN_OPEN_DOMAINS):
            result.strategy = "DIRECT"
            result.notes.append("known-open: skip probe")
            return result

        # ── Known CF domains → skip to cffi ───────────────────────────────────
        if any(cd in domain for cd in KNOWN_CF_DOMAINS):
            result.strategy = "CURL_CFFI"
            result.block_type = "cloudflare"
            result.cffi_target = "chrome146"
            result.notes.append("known-cloudflare: skip to cffi")
            return result

        async with self._sem:
            probe_url = f"https://{domain}/"

            # ── LEVEL 1: Direct probe with Chrome headers ─────────────────────
            status, body, elapsed_ms = await self._probe_url(probe_url, UA_POOL[0])
            result.probe_status = status
            result.probe_ms = int(elapsed_ms)
            block = self._detect_block(status, body)

            if block is None and status == 200:
                result.strategy = "DIRECT"
                result.working_ua = UA_POOL[0]
                result.robots_blocked = self._check_robots(domain)
                result.notes.append(f"open: status=200 t={int(elapsed_ms)}ms")
                return result

            result.block_type = block
            result.notes.append(f"L1-block={block} status={status}")

            # ── LEVEL 2: UA rotation ──────────────────────────────────────────
            if block in ("rate_limited", "hard_block", "redirect", None):
                for ua in random.sample(UA_POOL[1:7], 3):
                    s2, b2, t2 = await self._probe_url(probe_url, ua)
                    if self._detect_block(s2, b2) is None and s2 == 200:
                        result.strategy = "ROTATE_UA"
                        result.working_ua = ua
                        result.delay_ms = 500
                        result.notes.append(f"L2-ua-rotation: ok t={int(t2)}ms")
                        result.robots_blocked = self._check_robots(domain)
                        return result

            # ── LEVEL 3: curl_cffi TLS impersonation ──────────────────────────
            if block in ("cloudflare", "cloudflare_js", "ddos_protection",
                         "js_required", "hard_block", "rate_limited",
                         "bot_detection", "datadome", "perimeterx",
                         "akamai_block", "captcha"):
                ok, target, s3, b3, t3 = await self._try_cffi(probe_url)
                if ok:
                    result.strategy = "CURL_CFFI"
                    result.cffi_target = target
                    result.delay_ms = 200
                    result.notes.append(f"L3-cffi: target={target} t={int(t3)}ms")
                    return result

            # ── LEVEL 4: primp (Rust TLS) ─────────────────────────────────────
            if block in ("cloudflare", "cloudflare_js", "ddos_protection",
                         "js_required", "hard_block", "bot_detection",
                         "datadome", "perimeterx", "akamai_block", "captcha"):
                ok, target, s4, b4, t4 = await self._try_primp(probe_url)
                if ok:
                    result.strategy = "PRIMP"
                    result.primp_target = target
                    result.delay_ms = 300
                    result.notes.append(f"L4-primp: target={target} t={int(t4)}ms")
                    return result

            # ── LEVEL 5: nodriver (headless CDP) ──────────────────────────────
            if block in ("cloudflare_js", "js_required", "captcha",
                         "bot_detection", "datadome", "perimeterx",
                         "akamai_block"):
                ok, s5, b5, t5 = await self._try_nodriver(probe_url)
                if ok:
                    result.strategy = "NODRIVER"
                    result.via_nodriver = True
                    result.delay_ms = 2000  # heavy — throttle
                    result.notes.append(f"L5-nodriver: ok t={int(t5)}ms")
                    return result

            # ── LEVEL 6: patchright (stealth Playwright) ──────────────────────
            if block in ("cloudflare_js", "js_required", "captcha",
                         "bot_detection"):
                ok, s6, b6, t6 = await self._try_patchright(probe_url)
                if ok:
                    result.strategy = "PATCHRIGHT"
                    result.via_patchright = True
                    result.delay_ms = 2000
                    result.notes.append(f"L6-patchright: ok t={int(t6)}ms")
                    return result

            # ── LEVEL 7: Jina relay ───────────────────────────────────────────
            if block in ("cloudflare", "cloudflare_js", "paywall",
                         "login_wall", "captcha", "registration_wall"):
                ok, s7, b7, t7 = await self._try_jina(domain)
                if ok:
                    result.strategy = "JINA_PROXY"
                    result.via_jina = True
                    result.notes.append(f"L7-jina: ok t={int(t7)}ms")
                    return result

            # ── LEVEL 8: Archives ─────────────────────────────────────────────
            if block in ("paywall", "hard_block", "auth_required",
                         "login_wall", "geo_block", "registration_wall"):
                ok, arch_type, s8, b8, t8 = await self._try_archives(domain)
                if ok:
                    result.strategy = arch_type
                    if arch_type == "WAYBACK":
                        result.via_wayback = True
                    else:
                        result.via_archive_ph = True
                    result.notes.append(f"L8-{arch_type.lower()}: ok t={int(t8)}ms")
                    return result

            # ── No bypass found ───────────────────────────────────────────────
            result.strategy = "SKIP"
            result.notes.append(f"no-bypass: exhausted 8 levels, block={block}")
            return result

    # ── Main recon loop ────────────────────────────────────────────────────

    async def recon(self, domains: list[str]) -> dict[str, ReconResult]:
        """Perform full domain reconnaissance."""
        log.info(f"[TRUTNIE v2.0] Starting 8-level recon — {len(domains)} domains")

        tasks = [self._recon_domain(d) for d in domains]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        for domain, res in zip(domains, results_list):
            if isinstance(res, Exception):
                log.warning(f"  ✗ {domain}: {res}")
                r = ReconResult(domain)
                r.strategy = "DIRECT"
                r.notes.append(f"recon-error: {res}")
                self._results[domain] = r
            else:
                self._results[domain] = res
                icon = {
                    "DIRECT": "✓", "CURL_CFFI": "⚡", "PRIMP": "🦀",
                    "NODRIVER": "🌐", "PATCHRIGHT": "🎭",
                    "JINA_PROXY": "↪", "WAYBACK": "⏮", "ARCHIVE_PH": "📦",
                    "ROTATE_UA": "🔄", "SKIP": "✗",
                    "PLAYWRIGHT_STEALTH": "🔑", "ARCHIVE_FALLBACK": "📦",
                    "TELEGRAM_SCRAPE": "📨",
                }.get(res.strategy, "?")
                log.info(
                    f"  {icon} {domain:40s} → {res.strategy:18s} "
                    f"block={res.block_type or 'none':15s} "
                    f"{', '.join(res.notes[:1])}"
                )

        return self._results

    # ── Save to PostgreSQL ────────────────────────────────────────────────────────

    def save_to_db(self) -> None:
        """Persist recon results to database."""
        import hive2_db as _hive_db
        db = _hive_db.connect(read_only=False)
        rows = []
        for domain, res in self._results.items():
            rows.append((
                self.mission_id,
                domain,
                res.strategy,
                res.block_type,
                res.working_ua,
                res.cffi_target,
                res.primp_target,
                json.dumps(res.robots_blocked),
                res.delay_ms,
                res.via_jina,
                res.via_wayback,
                res.via_archive_ph,
                res.via_nodriver,
                res.via_patchright,
                res.probe_status,
                res.probe_ms,
                json.dumps(res.notes),
            ))
        if rows:
            for row in rows:
                db.execute("""
                    INSERT INTO drone_map
                    (mission_id, domain, strategy, block_type, working_ua,
                     cffi_target, primp_target, robots_json, delay_ms,
                     via_jina, via_wayback, via_archive_ph, via_nodriver,
                     via_patchright, probe_status, probe_ms, notes_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, row)
        db.close()
        log.info(f"[TRUTNIE] Saved {len(rows)} domain strategies to drone_map")

    # ── Retreat — covering tracks ───────────────────────────────────────────

    def retreat(self) -> None:
        """Trutnie wycofują się — czyszczą identyfikatory."""
        self._cookie_jar.clear()

        for pattern in ["/tmp/hive_drone_session_*", "/tmp/hive_nodriver_*",
                       "/tmp/hive_patchright_*"]:
            for f in glob.glob(pattern):
                try:
                    os.remove(f)
                except Exception as e:
                    log.debug(f"Suppressed: {e}")

        log.info("[TRUTNIE] Retreat complete — all session identifiers cleared")

    # ── Report ────────────────────────────────────────────────────────────────

    def report(self) -> dict:
        """Generate human-readable report."""
        strategy_counts: dict[str, int] = {}
        blocked = []
        for domain, res in self._results.items():
            strategy_counts[res.strategy] = \
                strategy_counts.get(res.strategy, 0) + 1
            if res.strategy == "SKIP":
                blocked.append(domain)

        total = len(self._results)
        accessible = total - len(blocked)

        return {
            "total_domains":    total,
            "accessible":       accessible,
            "blocked":          len(blocked),
            "strategy_counts":  strategy_counts,
            "blocked_domains":  blocked,
            "coverage_pct":     round(accessible / max(total, 1) * 100, 1),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public API — used by hive2.py and hive2_harvest.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_recon(mission_id: str, domains: list[str]) -> dict:
    """Run full 8-level recon, save to DB, retreat. Returns report dict."""
    drone = DroneRecon(mission_id)
    asyncio.run(drone.recon(domains))
    drone.save_to_db()
    report = drone.report()
    drone.retreat()

    # Print summary
    log.debug(f"\n  🪲 DRONE RECON v2.0 COMPLETE — Mission: {mission_id}")
    log.debug(f"     Domains scanned:  {report['total_domains']}")
    log.debug(f"     Accessible:       {report['accessible']} ({report['coverage_pct']}%)")
    log.debug(f"     Blocked (skip):   {report['blocked']}")
    log.debug(f"     Strategies:")
    for strat, cnt in sorted(report['strategy_counts'].items(), key=lambda x: -x[1]):
        icons = {
            "DIRECT": "✓", "CURL_CFFI": "⚡", "PRIMP": "🦀",
            "NODRIVER": "🌐", "PATCHRIGHT": "🎭",
            "JINA_PROXY": "↪", "WAYBACK": "⏮", "ARCHIVE_PH": "📦",
            "ROTATE_UA": "🔄", "SKIP": "✗",
            "PLAYWRIGHT_STEALTH": "🔑", "ARCHIVE_FALLBACK": "📦",
            "TELEGRAM_SCRAPE": "📨",
        }
        log.debug(f"       {icons.get(strat,'?')} {strat:18s} × {cnt}")
    if report['blocked_domains']:
        log.debug(f"     Blocked: {', '.join(report['blocked_domains'][:5])}"
              + (" ..." if len(report['blocked_domains']) > 5 else ""))
    return report


def get_strategy(mission_id: str, domain: str) -> dict:
    """Get strategy for domain from drone_map. Used by harvest."""
    try:
        import hive2_db as _hive_db
        db = _hive_db.connect(read_only=True)
        row = db.execute("""
            SELECT strategy, block_type, working_ua, cffi_target, primp_target,
                   delay_ms, via_jina, via_wayback, via_archive_ph,
                   via_nodriver, via_patchright
            FROM drone_map
            WHERE mission_id = ? AND domain = ?
        """, [mission_id, domain]).fetchone()
        db.close()
        if row:
            return {
                "strategy":       row[0],
                "block_type":     row[1],
                "working_ua":     row[2],
                "cffi_target":    row[3],
                "primp_target":   row[4],
                "delay_ms":       row[5],
                "via_jina":       row[6],
                "via_wayback":    row[7],
                "via_archive_ph": row[8],
                "via_nodriver":   row[9],
                "via_patchright": row[10],
            }
    except Exception as e:
        log.debug(f"Suppressed: {e}")
    return {"strategy": "DIRECT", "working_ua": UA_POOL[0],
            "delay_ms": 0, "via_jina": False, "via_wayback": False,
            "via_archive_ph": False, "via_nodriver": False, "via_patchright": False}


def extract_domains_from_sources(mission_id: str, max_domains: int = 50) -> list[str]:
    """Extract top unique domains from hive_sources for a mission.
    Only uses sources that passed relevance filter (score_relevance>=2, score_total>=15).
    Capped at max_domains to keep drone recon tractable.
    """
    try:
        import hive2_db as _hive_db
        db = _hive_db.connect(read_only=True)
        # Top sources by score_total, relevance-filtered
        rows = db.execute("""
            SELECT url FROM hive_sources
            WHERE mission_id = ?
              AND url IS NOT NULL
              AND score_relevance >= 2
              AND score_total >= 15
            ORDER BY score_total DESC
        """, [mission_id]).fetchall()
        if not rows:
            # Fallback: top-100 without filter
            rows = db.execute("""
                SELECT url FROM hive_sources
                WHERE mission_id = ? AND url IS NOT NULL
                ORDER BY score_total DESC NULLS LAST
                LIMIT 100
            """, [mission_id]).fetchall()
        db.close()
        # Extract unique domains preserving order (best first)
        seen = set()
        domains = []
        for (url,) in rows:
            try:
                d = urlparse(url).netloc.lower().lstrip("www.")
                if d and d not in seen:
                    seen.add(d)
                    domains.append(d)
                    if len(domains) >= max_domains:
                        break
            except Exception as e:
                log.debug(f"Suppressed: {e}")
        log.info(f"extract_domains: {len(domains)} unique domains (from {len(rows)} filtered sources)")
        return domains
    except Exception as e:
        log.warning(f"extract_domains error: {e}")
        return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="HIVE Drone Layer v2.0 — 8-level escalation recon"
    )
    parser.add_argument("mission_id", help="Mission ID")
    parser.add_argument(
        "--domains", default="",
        help="Comma-separated list of domains"
    )
    parser.add_argument(
        "--from-sources", action="store_true",
        help="Read domains from hive_sources"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Test mode — recon on a diverse test set"
    )
    args = parser.parse_args()

    if args.test:
        test_domains = [
            "arxiv.org", "bloomberg.com", "ft.com",
            "medium.com", "sciencedirect.com", "reddit.com",
            "worldbank.org", "reuters.com", "substack.com",
            "linkedin.com", "t.me", "twitter.com",
        ]
        run_recon(args.mission_id, test_domains)

    elif args.from_sources:
        domains = extract_domains_from_sources(args.mission_id)
        if not domains:
            print(f"No sources found for mission {args.mission_id}")
            sys.exit(1)
        run_recon(args.mission_id, domains)

    elif args.domains:
        domains = [d.strip() for d in args.domains.split(",") if d.strip()]
        run_recon(args.mission_id, domains)

    else:
        parser.print_help()
        sys.exit(1)
