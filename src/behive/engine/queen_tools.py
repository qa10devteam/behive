#!/usr/bin/env python3
"""
HIVE 2.0 — Queen Intelligence Toolkit v2.0
===========================================
Wyselekcjonowane 25 API z public-apis + własne HIVE tools.
Królowa wywołuje narzędzia AKTYWNIE podczas syntezy — nie jest pasywnym
agregratorem, ale nadinteligencją która sama uzupełnia luki wiedzy.

Narzędzia pogrupowane w 7 domen:
  1. NEWS       — GNews, NewsData, Mediastack
  2. ECONOMY    — World Bank, OECD, Frankfurter FX, EconDB
  3. KNOWLEDGE  — Wikipedia, Wikidata SPARQL, arXiv, Crossref, Semantic Scholar
  4. SEARCH     — DuckDuckGo, Firecrawl/Trafilatura deep-fetch
  5. JOBS       — Open Skills, JobDataLake
  6. GEO        — REST Countries, GeoNames, Warnely, IPInfo
  7. HIVE       — PostgreSQL RAG query, HIVE sources top, HIVE mission stats
"""

import os, json, re, time, urllib.request, urllib.parse, logging
from typing import Optional

from behive.engine.db import connect as _db_connect

DB_PATH = ""  # Legacy PostgreSQL removed — PostgreSQL is primary
log = logging.getLogger("hive.queen.tools")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. NEWS APIs
# ═══════════════════════════════════════════════════════════════════════════════

def gnews_search(query: str, lang: str = "en", country: str = "", max_results: int = 10) -> list[dict]:
    """GNews — 60,000+ źródeł, filtry: kraj, język, temat."""
    api_key = os.environ.get("GNEWS_API_KEY", "")
    if not api_key:
        return [{"error": "GNEWS_API_KEY not set"}]
    try:
        params = {"q": query, "lang": lang, "max": min(max_results, 10), "token": api_key}
        if country:
            params["country"] = country
        url = "https://gnews.io/api/v4/search?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        return [{"title": a.get("title",""), "url": a.get("url",""),
                 "source": a.get("source",{}).get("name",""), "published": a.get("publishedAt",""),
                 "desc": a.get("description","")[:200]} for a in data.get("articles", [])]
    except Exception as e:
        return [{"error": str(e)}]


def newsdata_search(query: str, country: str = "", language: str = "en",
                    category: str = "", max_results: int = 10) -> list[dict]:
    """NewsData.io — 40,000+ źródeł, sentiment scoring, archiwa historyczne."""
    api_key = os.environ.get("NEWSDATA_API_KEY", "")
    if not api_key:
        return [{"error": "NEWSDATA_API_KEY not set"}]
    try:
        params = {"apikey": api_key, "q": query, "language": language, "size": min(max_results, 10)}
        if country:
            params["country"] = country
        if category:
            params["category"] = category
        url = "https://newsdata.io/api/1/news?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        return [{"title": a.get("title",""), "url": a.get("link",""),
                 "source": a.get("source_id",""), "published": a.get("pubDate",""),
                 "sentiment": a.get("sentiment",""), "desc": (a.get("description") or "")[:200]}
                for a in data.get("results", [])]
    except Exception as e:
        return [{"error": str(e)}]


def mediastack_search(query: str, countries: str = "", languages: str = "en",
                      limit: int = 10) -> list[dict]:
    """Mediastack — 7,500+ źródeł, real-time news."""
    api_key = os.environ.get("MEDIASTACK_API_KEY", "")
    if not api_key:
        return [{"error": "MEDIASTACK_API_KEY not set"}]
    try:
        params = {"access_key": api_key, "keywords": query, "limit": min(limit, 25), "languages": languages}
        if countries:
            params["countries"] = countries
        url = "http://api.mediastack.com/v1/news?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        return [{"title": a.get("title",""), "url": a.get("url",""),
                 "source": a.get("source",""), "published": a.get("published_at",""),
                 "desc": (a.get("description") or "")[:200]} for a in data.get("data", [])]
    except Exception as e:
        return [{"error": str(e)}]


# ═══════════════════════════════════════════════════════════════════════════════
# 2. ECONOMY & FINANCE APIs
# ═══════════════════════════════════════════════════════════════════════════════

def worldbank_data(country_iso2: str, indicator: str, year_from: int = 2020, year_to: int = 2024) -> dict:
    """World Bank Open Data — PKB, internet penetration, populacja. Bezpłatne."""
    # Popularne wskaźniki:
    # IT.NET.USER.ZS = % internautów, IT.CEL.SETS.P2 = mobile subscriptions,
    # NY.GDP.PCAP.CD = GDP per capita, NY.GDP.MKTP.CD = GDP total
    try:
        url = (f"https://api.worldbank.org/v2/country/{country_iso2}/indicator/{indicator}"
               f"?format=json&date={year_from}:{year_to}&per_page=20")
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        if len(data) < 2 or not data[1]:
            return {"error": "No data", "country": country_iso2, "indicator": indicator}
        records = [{"year": d["date"], "value": d["value"]} for d in data[1] if d["value"] is not None]
        meta = data[0]
        return {"country": country_iso2, "indicator": indicator,
                "records": records, "total": meta.get("total", 0)}
    except Exception as e:
        return {"error": str(e)}


def frankfurter_fx(base: str = "EUR", targets: str = "TRY,PLN,GEL,USD") -> dict:
    """Exchange rates — open.er-api.com (ECB data). Bezpłatne, bez limitu."""
    try:
        url = f"https://open.er-api.com/v6/latest/{base}"
        req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        target_list = [t.strip().upper() for t in targets.split(",")]
        rates = {k: v for k, v in data.get("rates", {}).items() if k in target_list}
        return {"base": base, "rates": rates, "date": data.get("time_last_update_utc", "")}
    except Exception as e:
        # Fallback: exchangerate-api
        try:
            url2 = f"https://api.exchangerate-api.com/v4/latest/{base}"
            req2 = urllib.request.Request(url2, headers={"User-Agent": "HIVE-Queen/2.0"})
            with urllib.request.urlopen(req2, timeout=8) as r:
                data = json.loads(r.read())
            target_list = [t.strip().upper() for t in targets.split(",")]
            rates = {k: v for k, v in data.get("rates", {}).items() if k in target_list}
            return {"base": base, "rates": rates, "source": "exchangerate-api"}
        except Exception as e2:
            return {"error": str(e2)}


def econdb_series(ticker: str) -> dict:
    """EconDB — dane makroekonomiczne: CPI, PKB, stopy procentowe.
    Przykłady: TURCPI (inflacja TR), POLCPI, GEOCPI, OECD/MEI/..."""
    try:
        url = f"https://www.econdb.com/api/series/?ticker={urllib.parse.quote(ticker)}&format=json"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        # Wyciągnij ostatnie 10 obserwacji
        results = data.get("results", [])
        if results:
            obs = results[0].get("data", {}).get("observations", [])
            return {"ticker": ticker, "name": results[0].get("verbose_name",""),
                    "unit": results[0].get("unit",""),
                    "latest_10": obs[-10:] if len(obs) >= 10 else obs}
        return {"error": "No data", "ticker": ticker}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. KNOWLEDGE APIs
# ═══════════════════════════════════════════════════════════════════════════════

def wikipedia_summary(title: str, lang: str = "en") -> dict:
    """Wikipedia REST API — streszczenie artykułu. Bezpłatne."""
    try:
        safe_title = urllib.parse.quote(title.replace(" ", "_"))
        url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{safe_title}"
        req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0 (research)"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        return {"title": data.get("title",""), "extract": data.get("extract","")[:1000],
                "url": data.get("content_urls",{}).get("desktop",{}).get("page",""),
                "lang": lang}
    except Exception as e:
        return {"error": str(e), "title": title}


def wikidata_sparql(sparql_query: str) -> list[dict]:
    """Wikidata SPARQL — knowledge graph. Firmy, osoby, powiązania. Bezpłatne."""
    try:
        params = urllib.parse.urlencode({"query": sparql_query, "format": "json"})
        url = "https://query.wikidata.org/sparql?" + params
        req = urllib.request.Request(url, headers={
            "User-Agent": "HIVE-Queen/2.0 (research)",
            "Accept": "application/sparql-results+json"
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        bindings = data.get("results", {}).get("bindings", [])
        # Spłaszcz bindings do prostych dict
        results = []
        for b in bindings[:20]:
            row = {}
            for k, v in b.items():
                row[k] = v.get("value", "")
            results.append(row)
        return results
    except Exception as e:
        return [{"error": str(e)}]


def arxiv_search(query: str, max_results: int = 5) -> list[dict]:
    """arXiv — badania akademickie. Bezpłatne, bez limitu."""
    try:
        q = urllib.parse.quote(query)
        url = f"http://export.arxiv.org/api/query?search_query=all:{q}&max_results={max_results}&sortBy=relevance"
        req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            xml = r.read().decode()
        entries = re.findall(r"<entry>(.*?)</entry>", xml, re.DOTALL)
        results = []
        for e in entries:
            title = re.search(r"<title>(.*?)</title>", e, re.DOTALL)
            summary = re.search(r"<summary>(.*?)</summary>", e, re.DOTALL)
            link = re.search(r'href="(https://arxiv\.org/abs/[^"]+)"', e)
            pub = re.search(r"<published>(.*?)</published>", e)
            results.append({
                "title": (title.group(1).strip() if title else ""),
                "summary": (summary.group(1).strip()[:400] if summary else ""),
                "url": (link.group(1) if link else ""),
                "published": (pub.group(1)[:10] if pub else ""),
                "source": "arxiv",
            })
        return results
    except Exception as e:
        return [{"error": str(e)}]


def crossref_search(query: str, max_results: int = 5) -> list[dict]:
    """Crossref — 100M+ publikacji naukowych. Bezpłatne."""
    try:
        params = urllib.parse.urlencode({"query": query, "filter": "type:journal-article", "rows": max_results})
        url = "https://api.crossref.org/works?" + params
        req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0 (hive@qa10.io)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        items = data.get("message", {}).get("items", [])
        results = []
        for item in items:
            authors = ", ".join([a.get("family","") for a in item.get("author",[])[:3]])
            results.append({
                "title": item.get("title",[""])[0],
                "authors": authors,
                "journal": item.get("container-title",[""])[0],
                "year": (item.get("published",{}).get("date-parts",[[None]])[0][0]),
                "doi": item.get("DOI",""),
                "url": item.get("URL",""),
                "citations": item.get("is-referenced-by-count", 0),
            })
        return results
    except Exception as e:
        return [{"error": str(e)}]


def semantic_scholar_search(query: str, max_results: int = 5) -> list[dict]:
    """Semantic Scholar — 200M+ paperów z AI-powered TLDR. Bezpłatne."""
    try:
        params = urllib.parse.urlencode({
            "query": query, "limit": max_results,
            "fields": "title,abstract,year,citationCount,tldr,externalIds"
        })
        url = "https://api.semanticscholar.org/graph/v1/paper/search?" + params
        req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        results = []
        for p in data.get("data", []):
            tldr = p.get("tldr") or {}
            results.append({
                "title": p.get("title",""),
                "year": p.get("year"),
                "citations": p.get("citationCount", 0),
                "tldr": tldr.get("text","")[:300] if tldr else "",
                "abstract": (p.get("abstract") or "")[:300],
                "doi": p.get("externalIds",{}).get("DOI",""),
            })
        return results
    except Exception as e:
        return [{"error": str(e)}]


# ═══════════════════════════════════════════════════════════════════════════════
# 4. SEARCH & WEB FETCH
# ═══════════════════════════════════════════════════════════════════════════════

def ddg_search(query: str, max_results: int = 10, news: bool = False) -> list[dict]:
    """DuckDuckGo — zawsze dostępne, zero kosztów."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            if news:
                results = list(ddgs.news(query, max_results=max_results))
            else:
                results = list(ddgs.text(query, max_results=max_results))
        return results[:max_results]
    except Exception as e:
        return [{"error": str(e), "query": query}]


def web_fetch(url: str, timeout: int = 15) -> dict:
    """Pobierz i wyciągnij treść strony. Używa trafilatura (najlepsza jakość)."""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            content = trafilatura.extract(downloaded, include_links=False, include_tables=True)
            if content and len(content) > 100:
                return {"url": url, "content": content[:8000], "source": "trafilatura", "len": len(content)}
    except Exception:
        pass
    # Fallback urllib
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 HIVE-Queen/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            clean = re.sub(r"<[^>]+>", " ", raw)
            clean = re.sub(r"\s+", " ", clean).strip()
            return {"url": url, "content": clean[:6000], "source": "urllib_fallback"}
    except Exception as e:
        return {"url": url, "error": str(e), "content": ""}


def wayback_fetch(url: str, year: str = "2024") -> dict:
    """Internet Archive Wayback Machine — historyczne snapshoty."""
    try:
        api_url = (f"http://archive.org/wayback/available?url={urllib.parse.quote(url)}"
                   f"&timestamp={year}0101")
        with urllib.request.urlopen(api_url, timeout=8) as r:
            data = json.loads(r.read())
        snap = data.get("archived_snapshots", {}).get("closest", {})
        if snap.get("available"):
            return {"url": snap["url"], "timestamp": snap["timestamp"], "original": url}
        return {"error": "no snapshot", "original_url": url}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# 5. JOBS & TALENT
# ═══════════════════════════════════════════════════════════════════════════════

def openskills_search(query: str) -> list[dict]:
    """Open Skills API — tytuły stanowisk i powiązane umiejętności. Bezpłatne."""
    try:
        q = urllib.parse.quote(query)
        url = f"http://api.dataatwork.org/v1/jobs/autocomplete?begins_with={q}"
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
        return data[:10] if isinstance(data, list) else []
    except Exception as e:
        return [{"error": str(e)}]


# ═══════════════════════════════════════════════════════════════════════════════
# 6. GEO & COUNTRIES
# ═══════════════════════════════════════════════════════════════════════════════

def restcountries_get(iso2: str) -> dict:
    """Country data — populacja, języki, waluta. GitHub mledoze/countries. Bezpłatne."""
    # Użyj statycznego JSON z GitHub (mledoze/countries — zawsze aktualne)
    _CACHE = getattr(restcountries_get, "_cache", None)
    if _CACHE is None:
        try:
            url = "https://raw.githubusercontent.com/mledoze/countries/master/countries.json"
            req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                restcountries_get._cache = json.loads(r.read())
        except Exception as e:
            return {"error": f"Could not load countries db: {e}"}
    countries = restcountries_get._cache
    iso2_upper = iso2.upper()
    found = [c for c in countries if c.get("cca2","").upper() == iso2_upper]
    if not found:
        return {"error": f"Country not found: {iso2}"}
    c = found[0]
    return {
        "name": c.get("name",{}).get("common",""),
        "capital": c.get("capital",[""])[0] if c.get("capital") else "",
        "population": c.get("population", 0),
        "area_km2": c.get("area", 0),
        "languages": list(c.get("languages",{}).values()),
        "currencies": list(c.get("currencies",{}).keys()),
        "region": c.get("region",""),
        "subregion": c.get("subregion",""),
        "timezones": c.get("timezones",[]),
        "iso2": iso2_upper,
        "iso3": c.get("cca3",""),
    }


def ipinfo_lookup(ip: str) -> dict:
    """IPInfo — geolokalizacja IP. 50k req/month bezpłatnie."""
    try:
        url = f"https://ipinfo.io/{ip}/json"
        req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# 7. HIVE INTERNAL TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def rag_query(mission_id: str, query: str, top_k: int = 10) -> list[dict]:
    """Odpytaj lokalne RAG chunks — dane już zebrane przez scouty."""
    try:
        db = _db_connect(read_only=False)
        keywords = [w.lower() for w in re.findall(r"\b\w{4,}\b", query)
                    if w.lower() not in {
                        "that","this","with","from","have","which","what","when","where",
                        "they","their","będzie","jest","jako","przez","oraz","tego","które",
                    }]
        kw_clause = " OR ".join([f"LOWER(chunk_text) LIKE '%{kw}%'" for kw in keywords[:6]])
        sql = f"""
            SELECT chunk_text, source_url, chunk_index, similarity_score
            FROM hive_rag_chunks
            WHERE mission_id = '{mission_id}'
            {f'AND ({kw_clause})' if kw_clause else ''}
            ORDER BY similarity_score DESC NULLS LAST
            LIMIT {top_k}
        """
        rows = db.execute(sql).fetchall()
        db.close()
        return [{"text": r[0][:600], "url": r[1], "chunk": r[2], "score": r[3]} for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def hive_sources_top(mission_id: str, top_n: int = 20) -> list[dict]:
    """Top N źródeł misji z wynikami scoutów."""
    try:
        db = _db_connect(read_only=False)
        rows = db.execute(f"""
            SELECT url, domain, title, score_total, score_relevance, score_authority, status
            FROM hive_sources
            WHERE mission_id = '{mission_id}'
            ORDER BY score_total DESC NULLS LAST
            LIMIT {top_n}
        """).fetchall()
        db.close()
        return [{"url": r[0], "domain": r[1], "title": r[2] or "",
                 "score": round(r[3] or 0, 2), "relevance": round(r[4] or 0, 2),
                 "authority": round(r[5] or 0, 2), "status": r[6]}
                for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def hive_mission_stats(mission_id: str) -> dict:
    """Statystyki misji: źródła, encje, claims, chunki RAG, top domeny."""
    try:
        db = _db_connect(read_only=True)
        stats = {}
        for key, sql in [
            ("sources_total",   f"SELECT COUNT(*) FROM hive_sources WHERE mission_id='{mission_id}'"),
            ("content_docs",    f"SELECT COUNT(*) FROM hive_content WHERE mission_id='{mission_id}'"),
            ("entities_total",  f"SELECT COUNT(*) FROM hive_entities WHERE mission_id='{mission_id}'"),
            ("claims_total",    f"SELECT COUNT(*) FROM hive_claims WHERE mission_id='{mission_id}'"),
            ("rag_chunks",      f"SELECT COUNT(*) FROM hive_rag_chunks WHERE mission_id='{mission_id}'"),
            ("bee_ops_total",   f"SELECT COUNT(*) FROM hive_bee_results WHERE mission_id='{mission_id}'"),
        ]:
            try:
                r = db.execute(sql).fetchone()
                stats[key] = r[0] if r else 0
            except Exception:
                stats[key] = 0
        try:
            rows = db.execute(f"""
                SELECT domain, COUNT(*) as cnt FROM hive_sources
                WHERE mission_id='{mission_id}' GROUP BY domain ORDER BY cnt DESC LIMIT 10
            """).fetchall()
            stats["top_domains"] = [{"domain": r[0], "count": r[1]} for r in rows]
        except Exception:
            stats["top_domains"] = []
        try:
            rows = db.execute(f"""
                SELECT entity_type, COUNT(*) FROM hive_entities
                WHERE mission_id='{mission_id}' GROUP BY 1 ORDER BY 2 DESC LIMIT 8
            """).fetchall()
            stats["entity_types"] = {r[0]: r[1] for r in rows}
        except Exception:
            stats["entity_types"] = {}
        db.close()
        return stats
    except Exception as e:
        return {"error": str(e)}


def hive_entities_top(mission_id: str, entity_type: str = "", top_n: int = 50) -> list:
    """Top encje zebrane przez pszczoły dla tej misji.
    entity_type: org, person, location, technology, platform, regulation, threat_actor, amount — lub pusty dla wszystkich.
    Zwraca listę {type, value, context, confidence, count} posortowaną wg frequency.
    """
    try:
        db = _db_connect(read_only=True)
        where = f"mission_id='{mission_id}'"
        if entity_type:
            where += f" AND entity_type='{entity_type}'"
        rows = db.execute(f"""
            SELECT entity_type, value, context, AVG(confidence) as conf, COUNT(*) as cnt
            FROM hive_entities
            WHERE {where}
            GROUP BY entity_type, value, context
            ORDER BY cnt DESC, conf DESC
            LIMIT {top_n}
        """).fetchall()
        db.close()
        return [{"type": r[0], "value": r[1], "context": r[2] or "", "confidence": round(r[3], 2), "count": r[4]} for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def hive_claims_top(mission_id: str, min_confidence: float = 0.7, top_n: int = 50) -> list:
    """Top claims (twierdzenia faktyczne) zebrane przez pszczoły dla tej misji.
    Zwraca listę {claim, evidence, source_url, claim_type, confidence} posortowaną wg confidence.
    Użyj do weryfikacji hipotez i budowania narracji — to są fakty wyciągnięte z 1964 dokumentów.
    """
    try:
        db = _db_connect(read_only=True)
        rows = db.execute(f"""
            SELECT claim, evidence, source_url, claim_type, confidence
            FROM hive_claims
            WHERE mission_id='{mission_id}' AND confidence >= {min_confidence}
            ORDER BY confidence DESC
            LIMIT {top_n}
        """).fetchall()
        db.close()
        return [{"claim": r[0], "evidence": r[1] or "", "source_url": r[2] or "", "claim_type": r[3], "confidence": round(r[4], 2)} for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


# ═══════════════════════════════════════════════════════════════════════════════
# DEFINICJE NARZĘDZI dla Bedrock Tool Use
# ═══════════════════════════════════════════════════════════════════════════════

QUEEN_TOOLS = [
    # ── HIVE INTERNAL (pierwsze — dane już zebrane przez rój) ─────────────────
    {
        "name": "rag_query",
        "description": (
            "FIRST TOOL TO USE. Query the local HIVE knowledge base (PostgreSQL) for this mission. "
            "Returns the most relevant document chunks already collected by scout bees. "
            "Always query this BEFORE making external API calls — data may already be in the hive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mission_id": {"type": "string", "description": "Mission ID (e.g. hive_123_abc)"},
                "query": {"type": "string", "description": "What to find in the knowledge base"},
                "top_k": {"type": "integer", "description": "Number of chunks to return (default 10)", "default": 10},
            },
            "required": ["mission_id", "query"],
        },
    },
    {
        "name": "hive_sources_top",
        "description": (
            "Get top-scored sources from this mission. Shows domain, title, authority score. "
            "Use to see what scouts already collected and which URLs to deep-fetch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mission_id": {"type": "string", "description": "Mission ID"},
                "top_n": {"type": "integer", "description": "Number of sources to return (default 20)", "default": 20},
            },
            "required": ["mission_id"],
        },
    },
    {
        "name": "hive_mission_stats",
        "description": (
            "Get full statistics for a HIVE mission: source count, content docs, entities, claims, "
            "RAG chunks, bee ops count, top domains, entity type breakdown. "
            "Use EARLY to understand scale of data already collected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mission_id": {"type": "string", "description": "Mission ID"},
            },
            "required": ["mission_id"],
        },
    },
    {
        "name": "hive_entities_top",
        "description": (
            "Get top entities extracted by scout bees from collected documents. "
            "Entities include: orgs, persons, locations, technologies, platforms, regulations, threat_actors, amounts. "
            "Much richer than hive_sources_top — use to map the actor/technology ecosystem. "
            "Filter by entity_type for focused queries (e.g. 'platform', 'threat_actor', 'regulation')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mission_id": {"type": "string", "description": "Mission ID"},
                "entity_type": {"type": "string", "description": "Filter by type: org, person, location, technology, platform, regulation, threat_actor, amount — or empty for all", "default": ""},
                "top_n": {"type": "integer", "description": "Number of entities to return (default 50)", "default": 50},
            },
            "required": ["mission_id"],
        },
    },
    {
        "name": "hive_claims_top",
        "description": (
            "Get top factual claims extracted by scout bees from collected documents. "
            "These are concrete statements with evidence extracted from 1964 source documents. "
            "HIGH confidence claims (>=0.9) are well-evidenced. Use to ground analysis in source data "
            "rather than relying solely on external searches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mission_id": {"type": "string", "description": "Mission ID"},
                "min_confidence": {"type": "number", "description": "Minimum confidence threshold (0.0-1.0, default 0.7)", "default": 0.7},
                "top_n": {"type": "integer", "description": "Number of claims to return (default 50)", "default": 50},
            },
            "required": ["mission_id"],
        },
    },
    # ── SEARCH & WEB ──────────────────────────────────────────────────────────
    {
        "name": "ddg_search",
        "description": (
            "DuckDuckGo web or news search. No API key needed, always available. "
            "Use for fresh articles, reports, market data. Set news=true for recent news."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (English or Polish)"},
                "max_results": {"type": "integer", "description": "Results count (default 10)", "default": 10},
                "news": {"type": "boolean", "description": "True = search news", "default": False},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Deep-fetch a URL and extract full text content. Uses trafilatura for high quality extraction. "
            "Use when a search result title/snippet is promising but you need the full content. "
            "Best for: reports, statistics pages, research articles, blog posts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch"},
                "timeout": {"type": "integer", "description": "Timeout seconds (default 15)", "default": 15},
            },
            "required": ["url"],
        },
    },
    {
        "name": "wayback_fetch",
        "description": (
            "Internet Archive Wayback Machine — retrieve historical page snapshot. "
            "Use for accessing paywalled reports, deleted pages, or historical data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to find in archive"},
                "year": {"type": "string", "description": "Preferred year (e.g. '2024')", "default": "2024"},
            },
            "required": ["url"],
        },
    },
    # ── NEWS ──────────────────────────────────────────────────────────────────
    {
        "name": "gnews_search",
        "description": (
            "GNews — 60,000+ news sources. Filter by country (tr=Turkey, pl=Poland, ge=Georgia) "
            "and language. 100 req/day free. Best for: regional news, business news."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "lang": {"type": "string", "description": "Language code: en, tr, pl, ka, de", "default": "en"},
                "country": {"type": "string", "description": "Country code: tr, pl, ge, us", "default": ""},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "newsdata_search",
        "description": (
            "NewsData.io — 40,000+ sources with sentiment scoring. 200 req/day free. "
            "Categories: business, technology, science, world. "
            "Best for: sentiment analysis of market news, historical news archives."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "country": {"type": "string", "description": "tr, pl, ge, us", "default": ""},
                "language": {"type": "string", "default": "en"},
                "category": {"type": "string", "description": "business, technology, science, world", "default": ""},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    # ── ECONOMY ───────────────────────────────────────────────────────────────
    {
        "name": "worldbank_data",
        "description": (
            "World Bank Open Data — official statistics for 250+ countries. FREE, unlimited. "
            "Key indicators: IT.NET.USER.ZS (internet users %), IT.CEL.SETS.P2 (mobile per 100), "
            "NY.GDP.PCAP.CD (GDP per capita), NY.GDP.MKTP.CD (GDP total), "
            "IC.BUS.EASE.XQ (ease of doing business). "
            "Use for: Turkey/Georgia/Poland digital adoption, economic context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "country_iso2": {"type": "string", "description": "ISO2 code: TR, GE, PL, US"},
                "indicator": {"type": "string", "description": "World Bank indicator code"},
                "year_from": {"type": "integer", "default": 2020},
                "year_to": {"type": "integer", "default": 2024},
            },
            "required": ["country_iso2", "indicator"],
        },
    },
    {
        "name": "frankfurter_fx",
        "description": (
            "Frankfurter — ECB exchange rates. FREE, unlimited. "
            "Get TRY (Turkish Lira), PLN (Polish Zloty), GEL (Georgian Lari) rates vs EUR/USD. "
            "Use for: economic context, SaaS pricing in local currencies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "base": {"type": "string", "description": "Base currency (EUR, USD)", "default": "EUR"},
                "targets": {"type": "string", "description": "Comma-separated targets: TRY,PLN,GEL,USD", "default": "TRY,PLN,GEL,USD"},
            },
            "required": [],
        },
    },
    {
        "name": "econdb_series",
        "description": (
            "EconDB — macroeconomic time series: CPI inflation, GDP, interest rates. FREE. "
            "Ticker examples: TURCPI (Turkey CPI), POLCPI (Poland CPI), GEOCPI (Georgia CPI), "
            "TURRGDP (Turkey real GDP). Use for: inflation context, economic trajectory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "EconDB ticker symbol e.g. TURCPI"},
            },
            "required": ["ticker"],
        },
    },
    # ── KNOWLEDGE & ACADEMIC ──────────────────────────────────────────────────
    {
        "name": "wikipedia_summary",
        "description": (
            "Wikipedia — get article summary (up to 1000 chars). FREE, unlimited. "
            "Use for: company backgrounds, market definitions, country context. "
            "Set lang='tr' for Turkish, 'ka' for Georgian Wikipedia."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Article title"},
                "lang": {"type": "string", "description": "Language: en, tr, pl, ka, de", "default": "en"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "wikidata_sparql",
        "description": (
            "Wikidata SPARQL — query the global knowledge graph. FREE, unlimited. "
            "Use for: finding companies (SaaS companies in Turkey), people, organizations, statistics. "
            "Example query: 'SELECT ?company ?name WHERE { ?company wdt:P31 wd:Q4830453; wdt:P17 wd:Q43 }' "
            "(companies in Turkey). Keep queries simple — max 20 results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sparql_query": {"type": "string", "description": "SPARQL query string"},
            },
            "required": ["sparql_query"],
        },
    },
    {
        "name": "arxiv_search",
        "description": (
            "arXiv academic papers — peer-reviewed research. FREE, unlimited. "
            "Use for: empirical data on social media ROI, B2B conversion rates, "
            "digital marketing metrics, SaaS growth benchmarks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Academic search query"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "crossref_search",
        "description": (
            "Crossref — 100M+ peer-reviewed publications. FREE, unlimited. "
            "Returns: title, authors, journal, year, DOI, citation count. "
            "Use for: verifying statistics, finding authoritative sources."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "semantic_scholar_search",
        "description": (
            "Semantic Scholar — 200M+ papers with AI-generated TLDRs. FREE. "
            "Returns AI summaries of papers — fast way to scan many papers. "
            "Sorted by citation count (most cited = most important)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    # ── GEO ───────────────────────────────────────────────────────────────────
    {
        "name": "restcountries_get",
        "description": (
            "REST Countries — country data: population, languages, currency, capital. FREE. "
            "Use for: demographic context of TR (Turkey), GE (Georgia), PL (Poland)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "iso2": {"type": "string", "description": "ISO2 country code: TR, GE, PL, US"},
            },
            "required": ["iso2"],
        },
    },
    # ── JOBS ──────────────────────────────────────────────────────────────────
    {
        "name": "openskills_search",
        "description": (
            "Open Skills API — job titles and required skills taxonomy. FREE. "
            "Use for: understanding ICP job roles, what skills buyers have, "
            "mapping decision-maker profiles in IT/SaaS."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Job title or skill to search"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "mediastack_search",
        "description": (
            "MediaStack — real-time news search across 7500+ sources. "
            "FREE tier: 500 requests/month. Best for: breaking news, media coverage, "
            "competitor PR, country-specific news. countries: pl,tr,de,us,gb etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "countries": {"type": "string", "description": "Country codes e.g. 'tr,pl'"},
                "languages": {"type": "string", "default": "en"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "ipinfo_lookup",
        "description": (
            "IPInfo — geolocation and ASN data for any IP address. FREE (50k/month). "
            "Use for: identifying infrastructure origin, company hosting intelligence, "
            "geographic footprint of tech companies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ip": {"type": "string", "description": "IP address to look up"},
            },
            "required": ["ip"],
        },
    },
    # ─── NEW: public-apis Tier-1 Intelligence Tools ───────────────────────────
    {
        "name": "gdelt_search",
        "description": (
            "GDELT Project — globalny monitoring 300k artykułów/dzień, 65+ języków, 210 krajów. "
            "FREE, no key. Najlepsze narzędzie do: śledzenia narracji medialnych w czasie, "
            "sentymentu geopolitycznego, emerging stories. "
            "mode=ArtList (artykuły) | TimelineVol (wolumen). "
            "timespan: 1week, 1month, 3months, 1year."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query — supports AND/OR/NOT"},
                "mode": {"type": "string", "enum": ["ArtList", "TimelineVol"],
                         "description": "ArtList=artykuły, TimelineVol=wolumen w czasie"},
                "max_records": {"type": "integer", "default": 10},
                "timespan": {"type": "string", "default": "1month",
                             "description": "1week, 1month, 3months, 6months, 1year"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "openalex_search",
        "description": (
            "OpenAlex — 250M prac naukowych, autorów, instytucji. FREE, no key. "
            "Lepsze od Semantic Scholar dla: academic validation, institutional mapping, "
            "research trend analysis. entity=works (default) | authors | institutions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "entity": {"type": "string", "enum": ["works", "authors", "institutions", "venues"],
                           "default": "works"},
                "per_page": {"type": "integer", "default": 10},
                "filter_str": {"type": "string",
                               "description": "e.g. 'publication_year:2023,open_access.is_oa:true'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "hackernews_search",
        "description": (
            "HackerNews Algolia — tech community sentiment, startup traction, developer buzz. "
            "FREE, no key. Idealne do: oceny czy tech/SaaS jest hot w community, "
            "competitor mentions, new tool adoption signals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "github_trending",
        "description": (
            "GitHub Trending repos — tech ecosystem momentum. FREE. "
            "Idealne do: identyfikacji emerging platforms, open-source traction, "
            "what developers are building. since=daily|weekly|monthly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "language": {"type": "string", "description": "e.g. 'python', 'typescript', ''=all"},
                "since": {"type": "string", "enum": ["daily", "weekly", "monthly"], "default": "weekly"},
            },
            "required": [],
        },
    },
    {
        "name": "fred_series",
        "description": (
            "FRED (Federal Reserve) — makroekonomia globalna. "
            "series_id: GDP, CPIAUCSL (inflation), UNRATE (unemployment), "
            "DGS10 (10yr Treasury), DEXUSEU (USD/EUR). "
            "FREE z FRED_API_KEY env var, fallback World Bank."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "series_id": {"type": "string", "description": "FRED series ID"},
                "observation_start": {"type": "string", "default": "2020-01-01"},
                "observation_end": {"type": "string", "default": "2025-01-01"},
            },
            "required": ["series_id"],
        },
    },
    {
        "name": "eurostat_data",
        "description": (
            "Eurostat — oficjalne statystyki UE. FREE, no key. "
            "dataset: nama_10_gdp (PKB), une_rt_a (bezrobocie), "
            "tin00028 (internet usage), isoc_ec_eseln2 (e-commerce). "
            "Użyj dla: EU market sizing, economic context, regulatory data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "description": "Eurostat dataset code"},
                "params": {"type": "string", "description": "Additional URL params e.g. 'geo=TR&time=2023'"},
            },
            "required": ["dataset"],
        },
    },
    {
        "name": "opencorporates_search",
        "description": (
            "OpenCorporates — 200M+ firm z 140 krajów. FREE (rate limited). "
            "Idealne do: competitor mapping, ownership intelligence, "
            "incorporation data, KYC. jurisdiction: pl, tr, de, gb, us-ca, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {"type": "string"},
                "jurisdiction": {"type": "string", "description": "ISO code e.g. 'pl', 'tr', 'de'"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["company_name"],
        },
    },
    {
        "name": "stackexchange_search",
        "description": (
            "StackExchange API — developer questions, tech problem patterns. FREE, no key. "
            "site=stackoverflow (dev) | datascience | ai | economics | stats. "
            "Użyj dla: tech adoption intelligence, pain points, toolchain mapping."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "site": {"type": "string", "default": "stackoverflow",
                         "description": "stackoverflow, datascience, ai, economics, stats"},
                "max_results": {"type": "integer", "default": 10},
                "tagged": {"type": "string", "description": "Filter by tag e.g. 'python;api'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "pubmed_search",
        "description": (
            "PubMed NCBI — 35M+ biomedical publications. FREE, no key. "
            "Użyj dla: health-tech research, clinical evidence, pharma intelligence, "
            "medical device market. Queries: company names, conditions, technologies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
                "sort": {"type": "string", "enum": ["relevance", "pub_date"], "default": "relevance"},
            },
            "required": ["query"],
        },
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# DISPATCHER
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# NOWE NARZĘDZIA — public-apis TIER-1 dla Research Intelligence
# ═══════════════════════════════════════════════════════════════════════════════

def gdelt_search(query: str, mode: str = "ArtList", max_records: int = 10, timespan: str = "1month") -> list[dict]:
    """GDELT Project — globalny monitoring mediów z 65+ języków, real-time.
    mode: ArtList (artykuły), TimelineVol (wolumen w czasie), ToneChart (sentyment)
    FREE, no key, unlimited. Pokrycie: ~300k artykułów dziennie, 210 krajów.
    """
    try:
        query_enc = urllib.parse.quote(query)
        if mode == "TimelineVol":
            url = (f"https://api.gdeltproject.org/api/v2/tv/tv?query={query_enc}"
                   f"&mode=TimelineVol&format=json&timespan={timespan}")
        else:
            url = (f"https://api.gdeltproject.org/api/v2/doc/doc?query={query_enc}"
                   f"&mode={mode}&maxrecords={max_records}&format=json&timespan={timespan}")
        req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        if mode == "ArtList":
            articles = data.get("articles", [])
            return [{"title": a.get("title",""), "url": a.get("url",""),
                     "domain": a.get("domain",""), "language": a.get("language",""),
                     "socialimage": a.get("socialimage",""),
                     "seendate": a.get("seendate","")[:8],
                     "tone": round(a.get("tone", 0), 2)} for a in articles[:max_records]]
        return data
    except Exception as e:
        return [{"error": str(e)}]


def openalex_search(query: str, entity: str = "works", per_page: int = 10,
                    filter_str: str = "") -> list[dict]:
    """OpenAlex — 250M prac naukowych, autorów, instytucji, czasopism.
    entity: works | authors | institutions | venues | concepts
    FREE, no key. Best academic API (Semantic Scholar alternative).
    """
    try:
        params = f"search={urllib.parse.quote(query)}&per-page={per_page}"
        if filter_str:
            params += f"&filter={urllib.parse.quote(filter_str)}"
        url = f"https://api.openalex.org/{entity}?{params}&mailto=hive@qa10.io"
        req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        results = data.get("results", [])
        out = []
        for w in results[:per_page]:
            if entity == "works":
                out.append({
                    "title": w.get("title",""),
                    "doi": w.get("doi",""),
                    "year": w.get("publication_year"),
                    "citations": w.get("cited_by_count",0),
                    "open_access": w.get("open_access",{}).get("is_oa",False),
                    "abstract": (w.get("abstract_inverted_index") or {}) and "available",
                    "venue": (w.get("primary_location") or {}).get("source",{}).get("display_name",""),
                })
            else:
                out.append({"id": w.get("id",""), "display_name": w.get("display_name",""),
                            "works_count": w.get("works_count",0),
                            "cited_by_count": w.get("cited_by_count",0)})
        return out
    except Exception as e:
        return [{"error": str(e)}]


def hackernews_search(query: str, max_results: int = 10, date_range: str = "last_week") -> list[dict]:
    """HackerNews Algolia API — tech community sentiment, startup traction, developer opinions.
    Idealne dla: tech trends, SaaS traction, developer tools, startup buzz.
    FREE, no key, unlimited.
    """
    try:
        query_enc = urllib.parse.quote(query)
        # numericFilters dla zakresu dat
        url = (f"https://hn.algolia.com/api/v1/search?query={query_enc}"
               f"&tags=story&hitsPerPage={max_results}")
        req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        hits = data.get("hits", [])
        return [{"title": h.get("title",""), "url": h.get("url",""),
                 "points": h.get("points",0), "comments": h.get("num_comments",0),
                 "author": h.get("author",""), "created_at": h.get("created_at","")[:10],
                 "hn_url": f"https://news.ycombinator.com/item?id={h.get('objectID','')}"}
                for h in hits]
    except Exception as e:
        return [{"error": str(e)}]


def github_trending(language: str = "", spoken_language: str = "",
                    since: str = "weekly") -> list[dict]:
    """GitHub Trending repos — tech ecosystem momentum, nascent platforms, emerging tools.
    Scrape GitHub trending (nie ma oficjalnego API — używamy Jina Reader).
    FREE.
    """
    try:
        lang_param = f"?l={urllib.parse.quote(language)}" if language else ""
        spoken = f"&spoken_language_code={spoken_language}" if spoken_language else ""
        url = f"https://github.com/trending{lang_param}{spoken}&since={since}"
        jina_url = f"https://r.jina.ai/{url}"
        req = urllib.request.Request(jina_url, headers={
            "User-Agent": "HIVE-Queen/2.0",
            "Accept": "text/plain",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            content = r.read().decode(errors="replace")[:8000]
        return [{"source": "github_trending", "language": language or "all",
                 "since": since, "content": content[:3000]}]
    except Exception as e:
        return [{"error": str(e)}]


def fred_series(series_id: str, observation_start: str = "2020-01-01",
                observation_end: str = "2025-01-01") -> dict:
    """FRED (Federal Reserve) — makroekonomia US i globalna: GDP, CPI, unemployment.
    series_id examples: GDP, CPIAUCSL, UNRATE, DGS10, T10YIE, DEXUSEU
    FREE, no key needed for public series via web scraping fallback.
    """
    try:
        # Próba przez FRED API (wymaga klucza) — fallback do World Bank
        api_key = os.environ.get("FRED_API_KEY", "")
        if api_key:
            url = (f"https://api.stlouisfed.org/fred/series/observations"
                   f"?series_id={series_id}&observation_start={observation_start}"
                   f"&observation_end={observation_end}&api_key={api_key}&file_type=json")
            req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read())
            obs = data.get("observations", [])
            return {"series_id": series_id, "count": len(obs),
                    "latest": obs[-1] if obs else None,
                    "observations": obs[-12:]}  # last 12 datapoints
        else:
            # World Bank fallback dla podobnych wskaźników
            wb_map = {"GDP": "NY.GDP.MKTP.CD", "CPIAUCSL": "FP.CPI.TOTL",
                      "UNRATE": "SL.UEM.TOTL.ZS"}
            wb_id = wb_map.get(series_id, "NY.GDP.MKTP.CD")
            return worldbank_data("US", wb_id, 2020, 2024)
    except Exception as e:
        return {"error": str(e), "series_id": series_id}


def eurostat_data(dataset: str, params: str = "") -> dict:
    """Eurostat — oficjalne statystyki UE: handel, praca, gospodarka, demografia.
    dataset examples: nama_10_gdp (GDP), une_rt_a (unemployment), irt_lt_mcby_a (interest rates)
    FREE, no key.
    """
    try:
        url = f"https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}?format=JSON"
        if params:
            url += f"&{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        # Extract key dimensions and values
        dimensions = {k: list(v.get("label",{}).values())[:5]
                      for k,v in (data.get("dimension",{}) or {}).items()}
        values = list((data.get("value",{}) or {}).values())[:20]
        return {"dataset": dataset, "dimensions": dimensions,
                "sample_values": values, "unit": data.get("extension",{}).get("unit",{})}
    except Exception as e:
        return {"error": str(e), "dataset": dataset}


def opencorporates_search(company_name: str, jurisdiction: str = "",
                          max_results: int = 10) -> list[dict]:
    """OpenCorporates — 200M+ firm z 140 krajów. Ownership, incorporation dates, officers.
    Idealny do: KYC, competitor mapping, supply chain intelligence.
    FREE (rate limited), no key needed.
    """
    try:
        query_enc = urllib.parse.quote(company_name)
        url = f"https://api.opencorporates.com/companies/search?q={query_enc}&per_page={max_results}"
        if jurisdiction:
            url += f"&jurisdiction_code={jurisdiction}"
        req = urllib.request.Request(url, headers={"User-Agent": "HIVE-Queen/2.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        companies = (data.get("results",{}).get("companies",[]) or [])
        return [{"name": c.get("company",{}).get("name",""),
                 "jurisdiction": c.get("company",{}).get("jurisdiction_code",""),
                 "company_number": c.get("company",{}).get("company_number",""),
                 "incorporation_date": c.get("company",{}).get("incorporation_date",""),
                 "company_type": c.get("company",{}).get("company_type",""),
                 "status": c.get("company",{}).get("current_status",""),
                 "url": c.get("company",{}).get("opencorporates_url","")}
                for c in companies[:max_results]]
    except Exception as e:
        return [{"error": str(e)}]


def stackexchange_search(query: str, site: str = "stackoverflow",
                          max_results: int = 10, tagged: str = "") -> list[dict]:
    """StackExchange API — developer questions, tech adoption, problem patterns.
    site: stackoverflow | serverfault | superuser | datascience | ai | economics
    FREE, no key.
    """
    try:
        params = (f"order=desc&sort=votes&intitle={urllib.parse.quote(query)}"
                  f"&site={site}&pagesize={max_results}&filter=withbody")
        if tagged:
            params += f"&tagged={urllib.parse.quote(tagged)}"
        url = f"https://api.stackexchange.com/2.3/questions?{params}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "HIVE-Queen/2.0",
            "Accept-Encoding": "gzip",
        })
        import gzip
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read()
            try:
                content = gzip.decompress(raw)
            except Exception:
                content = raw
            data = json.loads(content)
        items = data.get("items", [])
        return [{"title": q.get("title",""), "score": q.get("score",0),
                 "answers": q.get("answer_count",0), "views": q.get("view_count",0),
                 "tags": q.get("tags",[])[:5],
                 "created": q.get("creation_date",""),
                 "link": q.get("link","")}
                for q in items]
    except Exception as e:
        return [{"error": str(e)}]


def pubmed_search(query: str, max_results: int = 10, sort: str = "relevance") -> list[dict]:
    """PubMed — biomedical & life sciences. 35M+ publikacji. NCBI E-utilities.
    FREE, no key (rate limited to 3 req/s bez klucza).
    """
    try:
        # Krok 1: szukaj IDs
        search_url = (f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
                      f"?db=pubmed&term={urllib.parse.quote(query)}"
                      f"&retmax={max_results}&retmode=json&sort={sort}")
        req = urllib.request.Request(search_url, headers={"User-Agent": "HIVE-Queen/2.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            search_data = json.loads(r.read())
        ids = search_data.get("esearchresult",{}).get("idlist",[])
        if not ids:
            return []
        # Krok 2: pobierz summary dla IDs
        ids_str = ",".join(ids[:max_results])
        summary_url = (f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
                       f"?db=pubmed&id={ids_str}&retmode=json")
        req2 = urllib.request.Request(summary_url, headers={"User-Agent": "HIVE-Queen/2.0"})
        with urllib.request.urlopen(req2, timeout=15) as r2:
            summary_data = json.loads(r2.read())
        result_obj = summary_data.get("result", {})
        out = []
        for pmid in ids[:max_results]:
            art = result_obj.get(pmid, {})
            if art:
                out.append({
                    "pmid": pmid,
                    "title": art.get("title",""),
                    "authors": [a.get("name","") for a in (art.get("authors",[]) or [])[:3]],
                    "journal": art.get("source",""),
                    "pubdate": art.get("pubdate",""),
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                })
        return out
    except Exception as e:
        return [{"error": str(e)}]


TOOL_MAP = {
    "rag_query": rag_query,
    "hive_sources_top": hive_sources_top,
    "hive_mission_stats": hive_mission_stats,
    "hive_entities_top": hive_entities_top,
    "hive_claims_top": hive_claims_top,
    "ddg_search": ddg_search,
    "web_fetch": web_fetch,
    "wayback_fetch": wayback_fetch,
    "gnews_search": gnews_search,
    "newsdata_search": newsdata_search,
    "mediastack_search": mediastack_search,
    "worldbank_data": worldbank_data,
    "frankfurter_fx": frankfurter_fx,
    "econdb_series": econdb_series,
    "wikipedia_summary": wikipedia_summary,
    "wikidata_sparql": wikidata_sparql,
    "arxiv_search": arxiv_search,
    "crossref_search": crossref_search,
    "semantic_scholar_search": semantic_scholar_search,
    "restcountries_get": restcountries_get,
    "ipinfo_lookup": ipinfo_lookup,
    "openskills_search": openskills_search,
    # NEW — public-apis Tier-1
    "gdelt_search": gdelt_search,
    "openalex_search": openalex_search,
    "hackernews_search": hackernews_search,
    "github_trending": github_trending,
    "fred_series": fred_series,
    "eurostat_data": eurostat_data,
    "opencorporates_search": opencorporates_search,
    "stackexchange_search": stackexchange_search,
    "pubmed_search": pubmed_search,
}


def dispatch_tool(tool_name: str, tool_input: dict) -> str:
    """Wywołaj narzędzie po nazwie. Zwraca JSON string z wynikiem."""
    fn = TOOL_MAP.get(tool_name)
    if not fn:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    try:
        t0 = time.time()
        result = fn(**tool_input)
        elapsed = round(time.time() - t0, 2)
        log.debug(f"[Tool] {tool_name} → {elapsed}s")
        # Dodaj metadata
        if isinstance(result, (list, dict)):
            if isinstance(result, dict):
                result["_tool"] = tool_name
                result["_elapsed_s"] = elapsed
        return json.dumps(result, ensure_ascii=False, default=str)
    except TypeError as e:
        return json.dumps({"error": f"Bad args for {tool_name}: {e}"})
    except Exception as e:
        return json.dumps({"error": str(e), "tool": tool_name})


# ═══════════════════════════════════════════════════════════════════════════════
# QUICK TEST
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    print("🐝 HIVE Queen Tools v2.0 — Test")
    print(f"Załadowanych narzędzi: {len(QUEEN_TOOLS)}")
    print()
    for t in QUEEN_TOOLS:
        free = "🆓" if any(x in t["description"] for x in ["FREE","free","No API","unlimited"]) else "🔑"
        print(f"  {free} {t['name']:<30} — {t['description'][:60]}...")
    print()

    # Test World Bank
    print("Test: World Bank TR internet users...")
    r = worldbank_data("TR", "IT.NET.USER.ZS", 2019, 2023)
    print(json.dumps(r, indent=2, default=str))

    # Test Frankfurter
    print("\nTest: FX rates...")
    r = frankfurter_fx()
    print(json.dumps(r, indent=2))

    # Test REST Countries
    print("\nTest: Turkey...")
    r = restcountries_get("TR")
    print(json.dumps(r, indent=2))
