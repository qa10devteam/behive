#!/usr/bin/env python3
"""
HIVE 2.0 — hive2_prescout.py
=============================
Dwufazowy rekonesans PRZED harvest:

  Phase 0A: dance()       — taniec pszczół (HEAD sweep + tier + snippet density)
                             ZERO pełnych fetchów, mapuje źródła w ~10s
  Phase 0B: BeeMasterGate — gate operatora (auto / manual / api)
                             Operator decyduje o paywall/login sources w <60s
  Phase 0C: TrueScout     — weryfikacja faktycznych zasobów
                             Partial fetch 3KB per source → data_volume_estimate + routing_final

Public API:
    asyncio.run(run_prescout_pipeline(mission_id, bm_mode="auto"))

CLI:
    python3 hive2_prescout.py <mission_id> [--manual]
"""

from __future__ import annotations

import os
import asyncio
import json
import re
import time
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

import hive2_db as _hive_db  # PostgreSQL via unified layer

DB_PATH = os.environ.get("BEHIVE_LEGACY_DB", "")  # Legacy — PostgreSQL is primary

BM_MODE = Literal["auto", "manual", "api"]


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — DOMAIN TIER CLASSIFIER  (O(1), zero I/O)
# ─────────────────────────────────────────────────────────────────────────────

_TIER_HIGH: set[str] = {
    # Gov / intergovernmental
    "europa.eu", "ted.europa.eu", "ec.europa.eu", "sam.gov",
    "contracts.finder.service.gov.uk", "find-tender.service.gov.uk",
    "uzp.gov.pl", "ezamowienia.gov.pl", "biznes.gov.pl",
    # Financial / market data premium
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "statista.com", "datareportal.com", "worldbank.org", "imf.org",
    "gartner.com", "forrester.com", "idc.com", "mckinsey.com",
    "deloitte.com", "pwc.com", "kpmg.com", "ey.com",
    # Academic
    "arxiv.org", "nature.com", "sciencedirect.com",
    "pubmed.ncbi.nlm.nih.gov", "semanticscholar.org",
    # Competitive intel / review platforms
    "clutch.co", "g2.com", "capterra.com", "trustpilot.com", "getapp.com",
    # News premium
    "techcrunch.com", "wired.com", "venturebeat.com", "hbr.org",
    "businessinsider.com", "forbes.com",
    # PL premium
    "stat.gov.pl", "nask.pl", "computerworld.pl", "spidersweb.pl",
    "antyweb.pl", "telepolis.pl",
    # Procurement databases
    "eb2b.com.pl", "logintrade.net",
}

_TIER_LOW: set[str] = {
    "pinterest.com", "tumblr.com", "quora.com", "reddit.com",
    "blogspot.com", "wordpress.com", "medium.com",
    "youtube.com", "tiktok.com", "twitter.com", "x.com",
    "facebook.com", "instagram.com", "linkedin.com",
    "amazon.com", "ebay.com", "etsy.com", "aliexpress.com",
    "bit.ly", "tinyurl.com", "linktr.ee",
    "yahoo.com", "answers.com",
}

_PAYWALL_DOMAINS: set[str] = {
    "jstor.org", "springer.com", "wiley.com", "tandfonline.com",
    "elsevier.com", "ieee.org", "acm.org",
}

_PROCUREMENT_PORTALS: dict[str, str] = {
    "ted.europa.eu":                     "dedicated_connector",
    "sam.gov":                           "dedicated_connector",
    "contracts.finder.service.gov.uk":   "dedicated_connector",
    "find-tender.service.gov.uk":        "dedicated_connector",
    "ezamowienia.gov.pl":                "dedicated_connector",
    "eb2b.com.pl":                       "dedicated_connector",
    "logintrade.net":                    "dedicated_connector",
}

_API_PATTERNS: list[str] = [
    r"/api/v\d+/", r"/api/public/", r"\.json$", r"\.xml$",
    r"/rest/", r"/v\d+/[a-z]", r"/feed\.xml", r"/rss\.xml",
    r"\?format=json", r"\?output=json",
]

_SPA_PREFIXES: tuple[str, ...] = (
    "app.", "dashboard.", "console.", "portal.", "platform.", "admin.",
)


def classify_domain(url: str) -> dict:
    """
    O(1) domain classification. Returns:
    {
        "tier":       "HIGH" | "MEDIUM" | "LOW",
        "paywall":    bool,
        "api_likely": bool,
        "spa_likely": bool,
    }
    No I/O. Safe to call in tight loops.
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().lstrip("www.")
        path   = parsed.path.lower()
        url_l  = url.lower()
    except Exception:
        return {"tier": "MEDIUM", "paywall": False, "api_likely": False, "spa_likely": False}

    # Tier
    tier = "MEDIUM"
    if any(domain == d or domain.endswith("." + d) for d in _TIER_HIGH):
        tier = "HIGH"
    elif any(domain == d or domain.endswith("." + d) for d in _TIER_LOW):
        tier = "LOW"
    elif any(x in domain for x in (".gov", ".gov.", ".edu", ".int")):
        tier = "HIGH"
    elif ".org" in domain:
        tier = "MEDIUM"  # orgs can be low-quality; keep MEDIUM not HIGH

    paywall    = any(domain == d or domain.endswith("." + d) for d in _PAYWALL_DOMAINS)
    api_likely = any(re.search(p, url_l) for p in _API_PATTERNS)
    spa_likely = any(domain.startswith(pfx) for pfx in _SPA_PREFIXES)

    return {
        "tier": tier,
        "paywall": paywall,
        "api_likely": api_likely,
        "spa_likely": spa_likely,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — SNIPPET DENSITY SCORER
# ─────────────────────────────────────────────────────────────────────────────

_NUM_PAT    = re.compile(r'\b\d[\d,\.]*\b')
_DATE_PAT   = re.compile(
    r'\b(20\d{2}|19\d{2})\b'
    r'|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+20\d{2}\b',
    re.I,
)
_PROPER_PAT = re.compile(r'\b[A-Z][a-zA-Z]{2,}\b')
_UNIT_PAT   = re.compile(
    r'\b(million|billion|trillion|percent|%|USD|EUR|PLN|GBP|\$|€|M|B|K|mln|mld)\b',
    re.I,
)


def snippet_density(title: str, snippet: str, url: str = "") -> float:
    """
    Proxy faktycznej gęstości informacyjnej snippetu.
    Zwraca 0.0–1.0 (wyżej = więcej liczb/dat/named entities).

    Dobry snippet:  "McKinsey 2025: RPA market Poland 1.2B PLN, CAGR 14.5%"  → ~0.7
    Słaby snippet:  "Find out more about our services for your business"       → ~0.05
    """
    combined = f"{title or ''} {snippet or ''} {url or ''}".strip()
    if not combined:
        return 0.0
    words = len(combined.split())
    if words < 2:
        return 0.0

    nums   = len(_NUM_PAT.findall(combined))
    dates  = len(_DATE_PAT.findall(combined))
    units  = len(_UNIT_PAT.findall(combined))
    proper = len(_PROPER_PAT.findall(combined))

    # Weighted score per 100 words; normalize to 0-1 (8.0 = top)
    raw = (nums * 2 + dates * 3 + units * 2 + proper * 0.5) / max(words, 1) * 100
    return round(min(1.0, raw / 8.0), 3)


def topic_dq_filter(title: str, snippet: str, topic: str) -> dict:
    """
    F2 Semantic DQ Filter — keyword-based (bez GPU, <1ms per call).

    Sprawdza czy snippet jest tematycznie spójny z query misji.
    Zwraca {dq: bool, dq_score: float, dq_reason: str|None}.

    dq=True oznacza: źródło prawdopodobnie irrelevant → depriorytetyzuj, nie odrzucaj.
    dq=False oznacza: przeszło filtr → normalne scorowanie.

    Logika:
      - Wyciąga słowa kluczowe z topic (≥4 znaki, pomija stop words)
      - Liczy ile z nich pojawia się w title+snippet
      - Jeśli <20% słów kluczowych pasuje → dq=True
      - Jeśli topic jest krótki (<3 słów kluczowych) → dq=False (zbyt mało danych)
    """
    if not topic or not (title or snippet):
        return {"dq": False, "dq_score": 1.0, "dq_reason": None}

    _STOP = {
        "the", "and", "for", "with", "that", "this", "are", "from", "have",
        "more", "also", "will", "been", "its", "can", "not", "but", "was",
        "jako", "jest", "dla", "oraz", "jak", "nie", "się", "przez", "roku",
        "które", "jego", "jej", "ich", "ten", "tym", "przy", "pod", "nad",
    }

    # Słowa kluczowe z topic
    topic_words = [
        w.lower() for w in re.findall(r'[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]{4,}', topic)
        if w.lower() not in _STOP
    ]
    if len(topic_words) < 3:
        return {"dq": False, "dq_score": 1.0, "dq_reason": None}  # za mało danych

    text = (f"{title or ''} {snippet or ''}").lower()

    hits = sum(1 for w in topic_words if w in text)
    score = hits / len(topic_words)

    if score < 0.2:
        return {
            "dq": True,
            "dq_score": round(score, 3),
            "dq_reason": f"topic_overlap={score:.2f} ({hits}/{len(topic_words)} keywords)",
        }
    return {"dq": False, "dq_score": round(score, 3), "dq_reason": None}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — ASYNC HEAD SWEEP
# ─────────────────────────────────────────────────────────────────────────────

_HEAD_SEM_SIZE = 24
_HEAD_TIMEOUT  = 6  # seconds


async def _head_one(
    session,   # aiohttp.ClientSession
    url: str,
    sem: asyncio.Semaphore,
) -> dict:
    """Single HEAD request. Never raises — returns dict with head_error on failure."""
    async with sem:
        t0 = time.monotonic()
        result: dict = {
            "url":             url,
            "http_status":     None,
            "content_type":    None,
            "content_length":  None,
            "last_modified":   None,
            "robots_tag":      None,
            "head_latency_ms": None,
            "head_error":      None,
        }
        try:
            import aiohttp
            async with session.head(
                url,
                timeout=aiohttp.ClientTimeout(total=_HEAD_TIMEOUT),
                allow_redirects=True,
                ssl=False,
            ) as resp:
                result["http_status"]    = resp.status
                ct = resp.headers.get("Content-Type", "").split(";")[0].strip()
                result["content_type"]   = ct or None
                cl = resp.headers.get("Content-Length")
                result["content_length"] = int(cl) if (cl and cl.isdigit()) else None
                result["last_modified"]  = resp.headers.get("Last-Modified")
                result["robots_tag"]     = resp.headers.get("X-Robots-Tag")
                result["head_latency_ms"] = int((time.monotonic() - t0) * 1000)
        except asyncio.TimeoutError:
            result["head_error"] = "timeout"
        except Exception as e:
            result["head_error"] = str(e)[:80]
        return result


async def head_sweep(urls: list[str]) -> dict[str, dict]:
    """
    Concurrent HEAD sweep for all URLs.
    Returns {url: head_metadata_dict}.
    """
    import aiohttp
    sem       = asyncio.Semaphore(_HEAD_SEM_SIZE)
    connector = aiohttp.TCPConnector(ssl=False, limit=_HEAD_SEM_SIZE + 4)
    async with aiohttp.ClientSession(
        connector=connector,
        headers={"User-Agent": "Mozilla/5.0 (compatible; HIVE-PreScout/2.0)"},
    ) as session:
        tasks   = [_head_one(session, url, sem) for url in urls]
        results = await asyncio.gather(*tasks)
    return {r["url"]: r for r in results}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — RESOURCE ROUTER
# ─────────────────────────────────────────────────────────────────────────────

ResourceType     = Literal[
    "api_endpoint", "pdf", "static_html", "rss_feed",
    "spa", "paywall", "thin_content", "database_portal", "unknown",
]
RoutingDecision  = Literal[
    "api_bee", "pdf_drone", "standard_drone", "heavy_drone",
    "rss_bee", "dedicated_connector", "bee_master_needed", "skip",
]


def route_resource(
    url: str,
    head_meta: dict,
    domain_info: dict,
    source_score: float = 0.0,
) -> tuple[ResourceType, RoutingDecision, list[str]]:
    """
    Klasyfikuje zasób + decyduje o metodzie ekstrakcji.
    Returns: (resource_type, routing_decision, flags[])
    """
    domain  = urlparse(url).netloc.lower().lstrip("www.")
    path    = urlparse(url).path.lower()
    flags:  list[str] = []
    status  = head_meta.get("http_status")
    ct      = (head_meta.get("content_type") or "").lower()
    cl      = head_meta.get("content_length")

    # ── Procurement portal override ──────────────────────────────────────────
    for portal_domain, routing in _PROCUREMENT_PORTALS.items():
        if portal_domain in domain:
            flags.append("procurement_portal")
            return "database_portal", routing, flags  # type: ignore[return-value]

    # ── HEAD failed / error ───────────────────────────────────────────────────
    if head_meta.get("head_error") or status is None:
        flags.append("head_failed")
        # HIGH tier despite HEAD fail → try anyway
        if domain_info["tier"] == "HIGH":
            flags.append("high_tier_head_fail")
            return "static_html", "standard_drone", flags
        return "unknown", "skip", flags

    # ── 401 Unauthorized ──────────────────────────────────────────────────────
    if status == 401:
        flags.append("auth_required")
        return "paywall", "bee_master_needed", flags

    # ── 403 Forbidden ────────────────────────────────────────────────────────
    if status == 403:
        if domain_info["paywall"]:
            flags.append("paywall_known")
            return "paywall", "bee_master_needed", flags
        if domain_info["tier"] == "HIGH":
            # Known high-tier that blocks HEAD but allows GET (e.g. arxiv)
            flags.append("high_tier_403_head_may_get")
            return "static_html", "standard_drone", flags
        flags.append("blocked_403")
        return "thin_content", "skip", flags

    # ── 429 Rate limited ──────────────────────────────────────────────────────
    if status == 429:
        flags.append("rate_limited")
        return "unknown", "bee_master_needed", flags

    # ── Other 4xx/5xx ────────────────────────────────────────────────────────
    if status >= 400:
        flags.append(f"http_{status}")
        return "thin_content", "skip", flags

    # ── API endpoint ─────────────────────────────────────────────────────────
    if domain_info["api_likely"] or ct in (
        "application/json", "application/xml",
        "text/xml", "application/atom+xml",
    ):
        flags.append("api_available")
        return "api_endpoint", "api_bee", flags

    # ── PDF ──────────────────────────────────────────────────────────────────
    if "pdf" in ct or path.endswith(".pdf"):
        if cl is not None and cl > 5_000_000:
            flags.append("pdf_too_large")
            return "pdf", "skip", flags
        if cl is not None and cl < 200_000:
            flags.append("pdf_small")
        return "pdf", "pdf_drone", flags

    # ── RSS / Atom ────────────────────────────────────────────────────────────
    if (
        "rss" in ct or "atom" in ct
        or path.endswith(".xml") or path.endswith("/feed")
        or path.endswith("/rss") or "/feed/" in path
    ):
        return "rss_feed", "rss_bee", flags

    # ── Thin content gate ────────────────────────────────────────────────────
    if cl is not None and cl < 5_000:
        flags.append("thin_content")
        return "thin_content", "skip", flags

    # ── LOW tier + low score → skip ──────────────────────────────────────────
    # ── REC-06: Domain Reputation score boost (grooming signal) ──────────────
    # Jeśli domain ma historię wysokiej jakości → override LOW tier → MEDIUM
    _rep_boost = 0.0
    try:
        from hive2_domain_reputation import get_reputation
        _rep_entry = get_reputation().get(domain)
        if _rep_entry:
            _rep_boost = _rep_entry.get("avg_quality", 0.0)
            _rep_count = _rep_entry.get("harvest_count", 0)
            if _rep_boost >= 0.65 and _rep_count >= 3:
                if domain_info["tier"] == "LOW":
                    domain_info = dict(domain_info, tier="MEDIUM")
                    flags.append("rep_promoted_to_medium")
            elif _rep_boost < 0.2 and _rep_count >= 5:
                # Historycznie słaby domain → skip nawet jeśli MEDIUM
                flags.append("rep_low_quality")
                return "thin_content", "skip", flags
    except Exception:
        pass

    if domain_info["tier"] == "LOW" and source_score < 20 and _rep_boost < 0.5:
        flags.append("low_tier_low_score")
        return "thin_content", "skip", flags

    # ── SPA detection ────────────────────────────────────────────────────────
    if domain_info["spa_likely"]:
        flags.append("js_heavy")
        return "spa", "heavy_drone", flags

    # ── Standard HTML ────────────────────────────────────────────────────────
    return "static_html", "standard_drone", flags


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — PRE-SCOUT DANCER
# ─────────────────────────────────────────────────────────────────────────────

def _duckdb_connect_retry(path: str, read_only: bool = False, retries: int = 4) -> Any:
    for attempt in range(retries):
        try:
            return _hive_db.connect(read_only=read_only)
        except Exception as e:
            if "lock" in str(e).lower() or "IO Error" in str(e):
                time.sleep(2 ** attempt)
            else:
                raise
    return _hive_db.connect(read_only=read_only)


def _ensure_prescout_schema(con: Any) -> None:
    """Create prescout tables if they don't exist."""
    con.execute("CREATE SEQUENCE IF NOT EXISTS hive_prescout_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS hive_prescout_map (
            id INTEGER DEFAULT nextval('hive_prescout_seq') PRIMARY KEY,
            mission_id     VARCHAR NOT NULL,
            url            VARCHAR NOT NULL,
            domain         VARCHAR,
            domain_tier    VARCHAR,
            http_status    INTEGER,
            content_type   VARCHAR,
            content_length INTEGER,
            last_modified  VARCHAR,
            robots_tag     VARCHAR,
            head_latency_ms INTEGER,
            snippet_density DOUBLE,
            resource_type  VARCHAR,
            routing_decision VARCHAR,
            flags          JSON,
            bm_decision    VARCHAR DEFAULT 'pending',
            bm_note        VARCHAR,
            prescout_at    TIMESTAMP DEFAULT NOW(),
            UNIQUE(mission_id, url)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS hive_true_scout (
            id                      INTEGER DEFAULT nextval('hive_prescout_seq') PRIMARY KEY,
            mission_id              VARCHAR NOT NULL,
            url                     VARCHAR NOT NULL,
            data_volume_words       INTEGER,
            data_volume_records     INTEGER,
            api_schema              JSON,
            pdf_pages               INTEGER,
            content_preview         TEXT,
            extraction_method       VARCHAR,
            estimated_harvest_s     FLOAT,
            extraction_confidence   DOUBLE,
            routing_final           VARCHAR,
            priority                INTEGER DEFAULT 5,
            verified_at             TIMESTAMP DEFAULT NOW(),
            UNIQUE(mission_id, url)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS hive_bm_sessions (
            id              INTEGER DEFAULT nextval('hive_prescout_seq') PRIMARY KEY,
            mission_id      VARCHAR NOT NULL,
            mode            VARCHAR,
            total_sources   INTEGER,
            approved        INTEGER,
            skipped         INTEGER,
            escalated       INTEGER,
            bm_added        INTEGER DEFAULT 0,
            summary_report  JSON,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)


class PreScoutDancer:
    """
    Orchestruje taniec pszczół dla całej misji.
    1. Ładuje hive_sources z DB (read-only)
    2. Concurrent HEAD sweep wszystkich URLi
    3. Klasyfikuje każde źródło
    4. Zapisuje do hive_prescout_map
    5. Zwraca source_map {url: entry_dict}
    """

    def __init__(self, mission_id: str, db_path: str = DB_PATH):
        self.mission_id = mission_id
        self.db_path    = db_path
        # Pobierz topic misji dla DQ filter
        try:
            con = _duckdb_connect_retry(db_path, read_only=True)
            row = con.execute(
                "SELECT topic FROM hive_missions WHERE id=?", [mission_id]
            ).fetchone()
            con.close()
            self.topic = row[0] if row else ""
        except Exception:
            self.topic = ""

    def _get_sources(self) -> list[dict]:
        con = _duckdb_connect_retry(self.db_path, read_only=True)
        try:
            rows = con.execute("""
                SELECT url, title, snippet, source_type, score_total
                FROM hive_sources
                WHERE mission_id = ?
                  AND status NOT IN ('error', 'blocked')
                ORDER BY score_total DESC
            """, [self.mission_id]).fetchall()
        finally:
            con.close()
        return [
            {
                "url":          r[0],
                "title":        r[1] or "",
                "snippet":      r[2] or "",
                "source_type":  r[3] or "",
                "score_total":  float(r[4] or 0),
            }
            for r in rows
        ]

    def _save_map(self, entries: list[dict]) -> None:
        con = _duckdb_connect_retry(self.db_path)
        try:
            _ensure_prescout_schema(con)
            con.execute("BEGIN")
            for e in entries:
                con.execute("""
                    INSERT INTO hive_prescout_map
                        (mission_id, url, domain, domain_tier, http_status,
                         content_type, content_length, last_modified, robots_tag,
                         head_latency_ms, snippet_density, resource_type,
                         routing_decision, flags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                """, [
                    self.mission_id,
                    e["url"],
                    urlparse(e["url"]).netloc.lower().lstrip("www."),
                    e["domain_tier"],
                    e["http_status"],
                    e["content_type"],
                    e["content_length"],
                    e["last_modified"],
                    e["robots_tag"],
                    e["head_latency_ms"],
                    e["snippet_density"],
                    e["resource_type"],
                    e["routing_decision"],
                    json.dumps(e["flags"]),
                ])
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            con.close()

    async def dance(self) -> dict[str, dict]:
        """
        Główna metoda tańca. Async.
        Zwraca {url: prescout_entry_dict}.
        """
        sources = self._get_sources()
        if not sources:
            print(f"  🐝 PreScout: no sources found for {self.mission_id}")
            return {}

        urls           = [s["url"] for s in sources]
        url_to_source  = {s["url"]: s for s in sources}

        print(f"  🐝 Taniec: HEAD sweep {len(urls)} sources...")
        t0           = time.monotonic()
        head_results = await head_sweep(urls)
        elapsed      = time.monotonic() - t0
        print(f"  🐝 HEAD sweep done: {len(head_results)} results in {elapsed:.1f}s")

        entries: list[dict] = []
        routing_stats: dict[str, int] = {}

        for url, head_meta in head_results.items():
            src         = url_to_source.get(url, {})
            domain_info = classify_domain(url)
            density     = snippet_density(
                src.get("title", ""), src.get("snippet", ""), url
            )
            # ── F2 DQ filter — topical relevance ─────────────────────────────
            dq_result = topic_dq_filter(
                src.get("title", ""), src.get("snippet", ""), self.topic
            )
            rt, routing, flags = route_resource(
                url, head_meta, domain_info, src.get("score_total", 0)
            )
            # DQ fail → dodaj flagę, nie odrzucaj (routing niezmieniony)
            if dq_result["dq"] and "dq_fail" not in flags:
                flags.append(f"dq_fail:{dq_result['dq_reason']}")
            entry = {
                "url":              url,
                "domain_tier":      domain_info["tier"],
                "http_status":      head_meta.get("http_status"),
                "content_type":     head_meta.get("content_type"),
                "content_length":   head_meta.get("content_length"),
                "last_modified":    head_meta.get("last_modified"),
                "robots_tag":       head_meta.get("robots_tag"),
                "head_latency_ms":  head_meta.get("head_latency_ms"),
                "snippet_density":  density,
                "dq_score":         dq_result["dq_score"],
                "resource_type":    rt,
                "routing_decision": routing,
                "flags":            flags,
            }
            entries.append(entry)
            routing_stats[routing] = routing_stats.get(routing, 0) + 1

        dq_fails = sum(1 for e in entries if any("dq_fail" in f for f in e["flags"]))
        self._save_map(entries)

        print(f"  🐝 Taniec zakończony. DQ filter: {dq_fails}/{len(entries)} irrelevant flagged.")
        print(f"  🐝 Routing distribution:")
        for r, cnt in sorted(routing_stats.items(), key=lambda x: -x[1]):
            print(f"     {r:30s}: {cnt}")

        return {e["url"]: e for e in entries}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — BEE MASTER GATE
# ─────────────────────────────────────────────────────────────────────────────

class BeeMasterGate:
    """
    Operator gate między tańcem a harvest.

    Modes:
      auto   — auto-approve wszystko co routing != skip/paywall (default, non-blocking)
      manual — wyświetla raport, czeka na input CLI (<60s decision time)
      api    — reserved for BeHive REST API (not implemented in v1)
    """

    def __init__(
        self,
        mission_id: str,
        mode: BM_MODE = "auto",
        db_path: str = DB_PATH,
    ):
        self.mission_id = mission_id
        self.mode       = mode
        self.db_path    = db_path

    def _load_map(self) -> list[dict]:
        con = _duckdb_connect_retry(self.db_path, read_only=True)
        try:
            rows = con.execute("""
                SELECT url, domain, domain_tier, resource_type, routing_decision,
                       flags, snippet_density, content_length, http_status
                FROM hive_prescout_map
                WHERE mission_id = ?
                ORDER BY snippet_density DESC
            """, [self.mission_id]).fetchall()
        finally:
            con.close()
        cols = [
            "url", "domain", "domain_tier", "resource_type", "routing_decision",
            "flags", "snippet_density", "content_length", "http_status",
        ]
        return [dict(zip(cols, r)) for r in rows]

    def _generate_report(self, entries: list[dict]) -> str:
        total    = len(entries)
        by_route: dict[str, list[dict]] = {}
        for e in entries:
            rd = e["routing_decision"]
            by_route.setdefault(rd, []).append(e)

        bm_needed = by_route.get("bee_master_needed", [])
        api_bees  = by_route.get("api_bee", [])
        skip_list = by_route.get("skip", [])
        ready     = total - len(skip_list) - len(bm_needed)

        lines = [
            "",
            "╔══════════════════════════════════════════════════════════════╗",
            "║           🐝  BEE MASTER BRIEFING — PRE-SCOUT MAP           ║",
            "╚══════════════════════════════════════════════════════════════╝",
            f"  Mission: {self.mission_id}",
            f"  Scouted: {total} sources",
            "",
            "  SUMMARY",
            f"  ├── ✅  Ready to harvest   : {ready}",
            f"  ├── 🔌  API endpoints      : {len(api_bees)}",
            f"  ├── ⚠️   Awaiting decision  : {len(bm_needed)}",
            f"  └── ❌  Recommended skip   : {len(skip_list)}",
            "",
        ]

        if bm_needed:
            lines += [
                "  ── SOURCES REQUIRING DECISION ──────────────────────────────",
                "",
            ]
            for e in bm_needed:
                raw_flags = e.get("flags") or "[]"
                if isinstance(raw_flags, str):
                    try:
                        flag_list = json.loads(raw_flags)
                    except Exception:
                        flag_list = []
                else:
                    flag_list = list(raw_flags)
                lines += [
                    f"  [{', '.join(flag_list)}]",
                    f"  URL  : {e['url'][:80]}",
                    f"  Tier : {e['domain_tier']} | Type: {e['resource_type']} | HTTP: {e['http_status']}",
                    "",
                ]

        if api_bees:
            lines += [
                "  ── API ENDPOINTS (auto-approved, structured data) ───────────",
                "",
            ]
            for e in api_bees[:8]:
                lines.append(f"  {e['url'][:80]}")
            if len(api_bees) > 8:
                lines.append(f"  ... and {len(api_bees) - 8} more")
            lines.append("")

        lines += [
            "  ── COMMANDS ──────────────────────────────────────────────────",
            "  [Enter]         Auto-approve all ready sources",
            "  s <url>         Skip specific URL",
            "  a <url>         Force-approve (overrides bee_master_needed)",
            "  add <url>       Add seed URL (will be harvested)",
            "  done            Proceed to True Scouting",
            "",
        ]
        return "\n".join(lines)

    def _apply_auto(self, entries: list[dict]) -> None:
        """Auto mode: approve all non-skip sources; skip paywalls and skip-routed."""
        con = _duckdb_connect_retry(self.db_path)
        try:
            _ensure_prescout_schema(con)
            con.execute("BEGIN")
            for e in entries:
                rd  = e["routing_decision"]
                raw = e.get("flags") or "[]"
                if isinstance(raw, str):
                    try:
                        flag_list = json.loads(raw)
                    except Exception:
                        flag_list = []
                else:
                    flag_list = list(raw)

                if rd == "skip":
                    decision = "skipped"
                elif rd == "bee_master_needed":
                    # Auto: skip known paywalls, approve rate-limited or auth-required that might work
                    if any(f in flag_list for f in ("paywall_known", "paywall")):
                        decision = "skipped"
                    else:
                        decision = "approved"  # let harvest try
                else:
                    decision = "approved"

                con.execute("""
                    UPDATE hive_prescout_map
                    SET bm_decision = ?, bm_note = 'auto'
                    WHERE mission_id = ? AND url = ?
                """, [decision, self.mission_id, e["url"]])
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            con.close()

    def _manual_session(self, entries: list[dict]) -> None:
        """Interactive CLI session. Manual mode only."""
        skip_set:    set[str] = set()
        approve_set: set[str] = set()
        add_urls:    list[str] = []

        print("  Enter commands (or press Enter / type 'done' to proceed):")
        import select, sys
        while True:
            try:
                # 5-minute timeout
                print("  🐝 > ", end="", flush=True)
                ready, _, _ = select.select([sys.stdin], [], [], 300)
                if not ready:
                    print("[timeout — auto-approving]")
                    break
                cmd = sys.stdin.readline().strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not cmd or cmd == "done":
                break
            if cmd.startswith("s "):
                u = cmd[2:].strip()
                skip_set.add(u)
                print(f"  ↳ Skip: {u}")
            elif cmd.startswith("a "):
                u = cmd[2:].strip()
                approve_set.add(u)
                print(f"  ↳ Force-approve: {u}")
            elif cmd.startswith("add "):
                u = cmd[4:].strip()
                add_urls.append(u)
                print(f"  ↳ Added seed: {u}")
            else:
                print("  Commands: [Enter]/done | s <url> | a <url> | add <url>")

        con = _duckdb_connect_retry(self.db_path)
        try:
            _ensure_prescout_schema(con)
            con.execute("BEGIN")
            for e in entries:
                url = e["url"]
                if url in skip_set:
                    dec = "skipped"
                elif url in approve_set:
                    dec = "approved"
                elif e["routing_decision"] == "bee_master_needed":
                    dec = "skipped"
                else:
                    dec = "skipped" if e["routing_decision"] == "skip" else "approved"
                con.execute("""
                    UPDATE hive_prescout_map
                    SET bm_decision = ?, bm_note = 'manual'
                    WHERE mission_id = ? AND url = ?
                """, [dec, self.mission_id, url])
            # Add seed URLs
            for url in add_urls:
                con.execute("""
                    INSERT INTO hive_prescout_map
                        (mission_id, url, domain, domain_tier, resource_type,
                         routing_decision, flags, bm_decision, bm_note, snippet_density)
                    VALUES (?, ?, ?, 'HIGH', 'static_html', 'standard_drone',
                            '["bee_master_seed"]', 'approved', 'bee_master_added', 0.5)
                    ON CONFLICT DO NOTHING
                """, [self.mission_id, url, urlparse(url).netloc.lower()])
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            con.close()

    def _add_seed_urls(self, seed_urls: list[dict]) -> None:
        """Dodaje seed URLs podane przez Bee Master (API/CLI)."""
        if not seed_urls:
            return
        con = _duckdb_connect_retry(self.db_path)
        try:
            _ensure_prescout_schema(con)
            con.execute("BEGIN")
            for s in seed_urls:
                url = s.get("url", "").strip()
                if not url:
                    continue
                con.execute("""
                    INSERT INTO hive_prescout_map
                        (mission_id, url, domain, domain_tier, resource_type,
                         routing_decision, flags, bm_decision, bm_note, snippet_density)
                    VALUES (?, ?, ?, 'HIGH', 'static_html', 'standard_drone',
                            '["bee_master_seed"]', 'approved', 'bee_master_added', 0.5)
                    ON CONFLICT DO NOTHING
                """, [
                    self.mission_id, url,
                    urlparse(url).netloc.lower().lstrip("www."),
                ])
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            con.close()

    def _log_session(self, entries: list[dict]) -> None:
        """Audit trail: zapisz wynik sesji do hive_bm_sessions."""
        con = _duckdb_connect_retry(self.db_path)
        try:
            _ensure_prescout_schema(con)
            approved  = sum(1 for e in entries if e.get("bm_decision") == "approved")
            skipped   = sum(1 for e in entries if e.get("bm_decision") == "skipped")
            escalated = sum(1 for e in entries if e.get("bm_decision") == "escalated")
            con.execute("""
                INSERT INTO hive_bm_sessions
                    (mission_id, mode, total_sources, approved, skipped, escalated)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [self.mission_id, self.mode, len(entries), approved, skipped, escalated])
        except Exception:
            pass
        finally:
            con.close()

    def gate(self, seed_urls: list[dict] | None = None) -> list[dict]:
        """
        Główna metoda gate.
        Zwraca listę approved entries.
        """
        entries = self._load_map()
        if not entries:
            print(f"  👨‍✈️  BeeMaster: no entries found for {self.mission_id}")
            return []

        if self.mode == "auto":
            print(f"  👨‍✈️  BeeMaster: AUTO mode — applying decisions to {len(entries)} sources...")
            self._apply_auto(entries)
        elif self.mode == "manual":
            print(self._generate_report(entries))
            self._manual_session(entries)
        else:
            # api mode fallback → auto
            self._apply_auto(entries)

        if seed_urls:
            self._add_seed_urls(seed_urls)
            print(f"  👨‍✈️  BeeMaster: added {len(seed_urls)} seed URLs")

        # Reload with decisions
        entries_with_dec = self._load_map()
        # Re-read with bm_decision
        con = _duckdb_connect_retry(self.db_path, read_only=True)
        try:
            rows = con.execute("""
                SELECT url, domain_tier, resource_type, routing_decision,
                       snippet_density, bm_decision
                FROM hive_prescout_map
                WHERE mission_id = ? AND bm_decision = 'approved'
                ORDER BY snippet_density DESC
            """, [self.mission_id]).fetchall()
        finally:
            con.close()

        approved = [
            {
                "url":              r[0],
                "domain_tier":      r[1],
                "resource_type":    r[2],
                "routing_decision": r[3],
                "snippet_density":  r[4],
            }
            for r in rows
        ]
        self._log_session(entries_with_dec)
        print(f"  👨‍✈️  BeeMaster: {len(approved)} approved → True Scout")
        return approved


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — TRUE SCOUT
# ─────────────────────────────────────────────────────────────────────────────

_TRUE_SCOUT_SEM    = 10
_PARTIAL_BYTES     = 3000   # bytes to fetch for HTML preview
_HTML_WORDS_MULT   = 10     # partial 3KB ≈ 10% of page → x10 estimate


class TrueScout:
    """
    Weryfikuje faktyczne zasoby dla approved sources.

    Per resource type:
      static_html / spa: partial GET 3KB → word count estimate + preview
      pdf:               Range GET 512B → PDF header → page count
      api_endpoint:      GET limit=1 → schema probe + record count
      rss_feed:          full GET → item count
      database_portal:   skip (dedykowany connector)
    """

    def __init__(self, mission_id: str, db_path: str = DB_PATH):
        self.mission_id = mission_id
        self.db_path    = db_path
        self._sem       = asyncio.Semaphore(_TRUE_SCOUT_SEM)

    # ── Probe methods ─────────────────────────────────────────────────────────

    async def _probe_html(self, url: str, session) -> dict:
        async with self._sem:
            try:
                import aiohttp
                headers = {
                    "Range":        f"bytes=0-{_PARTIAL_BYTES - 1}",
                    "User-Agent":   "Mozilla/5.0 (compatible; HIVE-TrueScout/2.0)",
                    "Accept":       "text/html,application/xhtml+xml",
                }
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=12),
                    ssl=False,
                    allow_redirects=True,
                ) as resp:
                    raw  = await resp.read()
                    text = raw.decode("utf-8", errors="ignore")
                    # Strip HTML tags
                    clean = re.sub(r'<[^>]+>', ' ', text)
                    clean = re.sub(r'\s+', ' ', clean).strip()
                    partial_words = len(clean.split())
                    estimate      = partial_words * _HTML_WORDS_MULT
                    return {
                        "data_volume_words":    max(estimate, partial_words),
                        "content_preview":      clean[:400],
                        "extraction_method":    "jina",
                        "extraction_confidence": 0.7,
                    }
            except Exception as e:
                return {
                    "data_volume_words":    0,
                    "content_preview":      f"probe_failed: {str(e)[:60]}",
                    "extraction_method":    "skip",
                    "extraction_confidence": 0.0,
                }

    async def _probe_pdf(self, url: str, session) -> dict:
        async with self._sem:
            try:
                import aiohttp
                async with session.get(
                    url,
                    headers={"Range": "bytes=0-1024"},
                    timeout=aiohttp.ClientTimeout(total=10),
                    ssl=False,
                ) as resp:
                    header_bytes = await resp.read()
                    pages_match  = re.search(rb'/N\s+(\d+)', header_bytes)
                    pages        = int(pages_match.group(1)) if pages_match else None
                    return {
                        "data_volume_words":    (pages or 10) * 300,
                        "pdf_pages":            pages,
                        "content_preview":      f"PDF ({pages or '?'} pages)",
                        "extraction_method":    "pymupdf",
                        "extraction_confidence": 0.9,
                    }
            except Exception:
                return {
                    "data_volume_words":    1000,
                    "pdf_pages":            None,
                    "content_preview":      "PDF",
                    "extraction_method":    "pymupdf",
                    "extraction_confidence": 0.5,
                }

    async def _probe_api(self, url: str, session) -> dict:
        async with self._sem:
            try:
                import aiohttp
                probe_url = url + ("&" if "?" in url else "?") + "limit=1&per_page=1&pageSize=1"
                async with session.get(
                    probe_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                    ssl=False,
                ) as resp:
                    body = await resp.text()
                    data = json.loads(body)
                    count = None
                    if isinstance(data, dict):
                        for key in ("total", "count", "totalItems", "total_count",
                                    "totalRecords", "hits", "nbHits"):
                            if key in data:
                                count = data[key]
                                break
                    schema_keys = list(data.keys()) if isinstance(data, dict) else []
                    return {
                        "data_volume_records":  count,
                        "data_volume_words":    (count or 100) * 20,
                        "api_schema":           json.dumps({"sample_keys": schema_keys[:10]}),
                        "content_preview":      f"API: {count or '?'} records, keys: {schema_keys[:5]}",
                        "extraction_method":    "api_bee",
                        "extraction_confidence": 0.95,
                    }
            except Exception as e:
                return {
                    "data_volume_records":  None,
                    "data_volume_words":    500,
                    "content_preview":      f"API probe failed: {str(e)[:60]}",
                    "extraction_method":    "api_bee",
                    "extraction_confidence": 0.4,
                }

    async def _probe_rss(self, url: str, session) -> dict:
        async with self._sem:
            try:
                import aiohttp
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                    ssl=False,
                ) as resp:
                    body = await resp.text()
                item_count = body.count("<item>") + body.count("<entry>")
                return {
                    "data_volume_words":    item_count * 150,
                    "content_preview":      f"RSS: {item_count} items",
                    "extraction_method":    "rss_bee",
                    "extraction_confidence": 0.9,
                }
            except Exception:
                return {
                    "data_volume_words":    300,
                    "content_preview":      "RSS",
                    "extraction_method":    "rss_bee",
                    "extraction_confidence": 0.5,
                }

    async def _probe_one(self, entry: dict, session) -> dict:
        url = entry["url"]
        rt  = entry.get("resource_type", "static_html")
        result: dict = {
            "url":           url,
            "routing_final": entry.get("routing_decision", "standard_drone"),
            "priority":      5,
        }

        if rt == "database_portal":
            probe = {
                "data_volume_words":    5000,
                "content_preview":      "Dedicated procurement portal connector",
                "extraction_method":    "dedicated_connector",
                "extraction_confidence": 1.0,
            }
        elif rt == "api_endpoint":
            probe = await self._probe_api(url, session)
        elif rt == "pdf":
            probe = await self._probe_pdf(url, session)
        elif rt == "rss_feed":
            probe = await self._probe_rss(url, session)
        elif rt in ("static_html", "spa", "unknown"):
            probe = await self._probe_html(url, session)
        else:
            probe = {
                "data_volume_words":    500,
                "content_preview":      rt,
                "extraction_method":    "standard_drone",
                "extraction_confidence": 0.5,
            }

        result.update(probe)
        words = probe.get("data_volume_words", 500)
        result["estimated_harvest_s"] = max(2.0, words / 5000 * 10)

        # Priority: HIGH tier + high density → priority 9; LOW tier → 2
        tier    = entry.get("domain_tier", "MEDIUM")
        density = entry.get("snippet_density", 0.0) or 0.0
        if tier == "HIGH" and density > 0.3:
            result["priority"] = 9
        elif tier == "HIGH":
            result["priority"] = 7
        elif tier == "LOW":
            result["priority"] = 2
        else:
            result["priority"] = 5

        return result

    def _save_true_scout(self, results: list[dict]) -> None:
        con = _duckdb_connect_retry(self.db_path)
        try:
            _ensure_prescout_schema(con)
            con.execute("BEGIN")
            for r in results:
                con.execute("""
                    INSERT INTO hive_true_scout
                        (mission_id, url, data_volume_words, data_volume_records,
                         api_schema, pdf_pages, content_preview, extraction_method,
                         estimated_harvest_s, extraction_confidence,
                         routing_final, priority)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                """, [
                    self.mission_id,
                    r["url"],
                    r.get("data_volume_words"),
                    r.get("data_volume_records"),
                    r.get("api_schema"),
                    r.get("pdf_pages"),
                    (r.get("content_preview") or "")[:500],
                    r.get("extraction_method", "standard_drone"),
                    r.get("estimated_harvest_s"),
                    r.get("extraction_confidence"),
                    r.get("routing_final", "standard_drone"),
                    r.get("priority", 5),
                ])
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            con.close()

    async def verify(self, approved_entries: list[dict]) -> list[dict]:
        """
        Weryfikuje zasoby dla approved entries.
        Zwraca routing_map z verified metadata.
        """
        if not approved_entries:
            return []

        print(f"  🔍 True Scout: verifying {len(approved_entries)} approved sources...")
        t0 = time.monotonic()

        import aiohttp
        connector = aiohttp.TCPConnector(ssl=False, limit=_TRUE_SCOUT_SEM + 2)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks   = [self._probe_one(e, session) for e in approved_entries]
            results = await asyncio.gather(*tasks)

        self._save_true_scout(list(results))
        elapsed     = time.monotonic() - t0
        total_words = sum(r.get("data_volume_words") or 0 for r in results)
        api_count   = sum(1 for r in results if r.get("extraction_method") == "api_bee")
        pdf_count   = sum(1 for r in results if r.get("extraction_method") == "pymupdf")

        print(f"  🔍 True Scout done in {elapsed:.1f}s")
        print(f"     ~{total_words:,} words estimated | {api_count} APIs | {pdf_count} PDFs")

        return list(results)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

async def run_prescout_pipeline(
    mission_id: str,
    bm_mode: BM_MODE = "auto",
    seed_urls: list[dict] | None = None,
    db_path: str = DB_PATH,
) -> list[dict]:
    """
    Pełny pipeline: PreScout → BeeMaster → TrueScout.

    Wywołać z hive2.py cmd_run() MIĘDZY scout a harvest:

        from hive2_prescout import run_prescout_pipeline
        routing_map = asyncio.run(run_prescout_pipeline(mission_id))
        # routing_map: [{url, routing_final, extraction_method, priority, ...}]

    Env vars (opcjonalne):
        HIVE_BM_MANUAL=1              → manual mode (CLI interactive)
        HIVE_BM_SEEDS='[{"url":...}]' → seed URLs od operatora

    Args:
        mission_id: ID misji (musi mieć hive_sources w DB)
        bm_mode:    "auto" | "manual" | "api"
        seed_urls:  [{"url": str, "title": str}]
        db_path:    ścieżka do DuckDB

    Returns:
        routing_map: list of verified source dicts with routing_final
    """
    sep = "═" * 62
    print(f"\n{sep}")
    print(f"  🐝 PHASE 0 — TANIEC + BEE MASTER + TRUE SCOUT")
    print(f"{sep}")

    # ── Phase A: Taniec ──────────────────────────────────────────────────────
    dancer     = PreScoutDancer(mission_id, db_path)
    source_map = await dancer.dance()
    if not source_map:
        print(f"  ⚠️  PreScout: no sources → skipping")
        return []

    # ── REC-01: Bionic Quorum check ──────────────────────────────────────────
    # Analogia: pszczoły wymagają quorum ≥25 przed lotem do nowego gniazda.
    # Jeśli za mało harvestowalnych URL → wygeneruj expansion queries zamiast
    # wysyłać trutnie na bezproduktywny harvest.
    try:
        from hive2_bionic_quorum import QuorumChecker
        _qc = QuorumChecker(mission_id)
        _qr = _qc.check()
        print(f"  🐝 Quorum: {_qr.harvestable_count}/{_qr.total_sources} harvestable "
              f"({'✅ MET' if _qr.quorum_met else '⚠️ NOT MET'})")
        if not _qr.quorum_met:
            if _qr.expansion_queries:
                print(f"  🐝 Quorum: generating {len(_qr.expansion_queries)} expansion queries")
                # Wstrzyknij expansion queries jako dodatkowe źródła do scout
                import importlib
                _scout_mod = importlib.import_module('hive2')
                # Zapisz queries do hive_gaps dla następnego runquerycycle
                try:
                    pass  # duckdb removed
                    _gc = _ddb2.connect(db_path)
                    for _eq in _qr.expansion_queries[:5]:
                        _gc.execute(
                            "INSERT INTO hive_gaps (id, mission_id, gap_query, gap_type, priority, resolved, created_at) "
                            "VALUES (nextval('hive_facts_seq'), ?, ?, 'quorum_expansion', 9, FALSE, CURRENT_TIMESTAMP)",
                            [mission_id, f"[QUORUM-EXPAND] {_eq}"]
                        )
                    _gc.close()
                    print(f"  🐝 Quorum: {len(_qr.expansion_queries[:5])} expansion queries → hive_gaps (priority=9)")
                except Exception as _gce:
                    pass
            else:
                print(f"  🐝 Quorum: insufficient sources, proceeding anyway (no expansions available)")
    except Exception as _qce:
        print(f"  🐝 Quorum check skipped: {_qce}")

    # ── Phase B: Bee Master gate ─────────────────────────────────────────────
    bm       = BeeMasterGate(mission_id, mode=bm_mode, db_path=db_path)
    approved = bm.gate(seed_urls=seed_urls)
    if not approved:
        print(f"  ⚠️  BeeMaster approved 0 sources → skipping True Scout")
        return []

    # ── Phase C: True Scout ──────────────────────────────────────────────────
    scout       = TrueScout(mission_id, db_path)
    routing_map = await scout.verify(approved)

    print(f"\n  ✅ Pre-Scout complete: {len(routing_map)} sources routed for harvest")
    total_est = sum(r.get("estimated_harvest_s") or 0 for r in routing_map)
    print(f"     Estimated harvest time: {total_est:.0f}s ({total_est/60:.1f} min)")
    print(f"{sep}\n")
    return routing_map


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import os

    if len(sys.argv) < 2:
        print("Usage: python3 hive2_prescout.py <mission_id> [--manual] [--test-classify]")
        sys.exit(1)

    if "--test-classify" in sys.argv:
        # Quick unit tests
        print("=== classify_domain tests ===")
        assert classify_domain("https://ted.europa.eu/api/notices")["tier"] == "HIGH", "ted HIGH"
        assert classify_domain("https://g2.com/categories/rpa")["tier"] == "HIGH", "g2 HIGH"
        assert classify_domain("https://reddit.com/r/python")["tier"] == "LOW", "reddit LOW"
        assert classify_domain("https://jstor.org/stable/123")["paywall"] is True, "jstor paywall"
        assert classify_domain("https://arxiv.org/abs/1234.5678")["tier"] == "HIGH", "arxiv HIGH"
        print("  ALL classify_domain tests PASSED ✅")

        print("\n=== snippet_density tests ===")
        d1 = snippet_density("McKinsey 2025", "RPA market Poland 1.2B PLN CAGR 14.5% through 2028", "")
        d2 = snippet_density("", "Find out more about our services for your business", "")
        assert d1 > d2, f"ordering: {d1} vs {d2}"
        assert d1 > 0.3, f"factual snippet density too low: {d1}"
        print(f"  factual={d1:.3f}, generic={d2:.3f}  ✅")
        sys.exit(0)

    mission_id = sys.argv[1]
    mode: BM_MODE = "manual" if "--manual" in sys.argv else "auto"

    # Seed URLs from env
    seeds_env = os.environ.get("HIVE_BM_SEEDS", "")
    seed_urls = json.loads(seeds_env) if seeds_env else None

    result = asyncio.run(
        run_prescout_pipeline(mission_id, bm_mode=mode, seed_urls=seed_urls)
    )

    print(f"\nRouting map ({len(result)} entries):")
    for r in result[:15]:
        print(f"  {r.get('routing_final', '?'):20s} | {r.get('extraction_method', '?'):18s} "
              f"| ~{r.get('data_volume_words', 0):,}w | {r['url'][:55]}")
    if len(result) > 15:
        print(f"  ... and {len(result) - 15} more")
