"""EUR-Lex via Cellar SPARQL — never eur-lex.europa.eu (WAF)."""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
UA = "BeHiveCellar/1.0 (+https://github.com/qa10devteam/behive)"
CELEX_RE = re.compile(r"\b([1-7]\d{4}[A-Z]{1,2}\d{3,5})\b", re.I)
CASE_RE = re.compile(r"\b([CTF])[-\u2011\u2013 ]?(\d{1,5})\s*/\s*(\d{2,4})\b", re.I)
_COURT_CELEX = {"C": "CJ", "T": "TJ", "F": "FT"}


def case_to_celex(text: str) -> str:
    """C-128/21 → 62021CJ0128. T-xxx → TJ. Not InfoCuria."""
    m = CASE_RE.search(text or "")
    if not m:
        return ""
    court, num, year = m.group(1).upper(), m.group(2), m.group(3)
    if len(year) == 2:
        yi = int(year)
        year = str(1900 + yi if yi >= 50 else 2000 + yi)
    kind = _COURT_CELEX.get(court, "CJ")
    return f"6{year}{kind}{int(num):04d}"


def extract_celex(text: str) -> str:
    m = CELEX_RE.search(text or "")
    if m:
        return m.group(1).upper()
    return case_to_celex(text)


def _sparql(query: str, timeout: float = 20.0) -> list[dict]:
    data = urllib.parse.urlencode({"query": query, "format": "application/sparql-results+json"}).encode()
    req = urllib.request.Request(
        SPARQL,
        data=data,
        headers={
            "Accept": "application/sparql-results+json, application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UA,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for b in (payload.get("results") or {}).get("bindings") or []:
        row = {}
        for k, v in b.items():
            row[k] = (v or {}).get("value") or ""
        out.append(row)
    return out


def _safe_kw(topic: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9ąćęłńóśźżÄÖÜäöüß \\-]", " ", topic or "")
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    # drop domain-only tokens
    drop = {"eur", "lex", "eurlex", "eurolaw", "cellar", "celex", "sparql", "official", "journal"}
    words = [w for w in s.split() if w not in drop and len(w) > 2]
    return " ".join(words[:6])


def _bif_arg(words: list[str]) -> str:
    """Virtuoso bif:contains. Word = 'gdpr'. Phrase = '"a b c"'."""
    words = [w for w in words if w]
    if not words:
        return ""
    if len(words) == 1:
        return f"\"'{words[0]}'\""
    return f"'\"{' '.join(words[:8])}\"'"


def _title_search(words: list[str], fetch: int = 40, lang: str = "", celex_prefix: str = "") -> list[dict]:
    """Indexed title search. work_has_expression is empty; belongs_to_work is live.
    FILTER(CONTAINS) on 19M titles times out — do not use it."""
    arg = _bif_arg(words)
    if not arg:
        return []
    lang_triple = ""
    if lang:
        lang_triple = (
            "?exp cdm:expression_uses_language "
            f"<http://publications.europa.eu/resource/authority/language/{lang}> ."
        )
    prefix_f = ""
    if celex_prefix:
        p = re.sub(r"[^0-9A-Za-z]", "", celex_prefix)[:8]
        if p:
            prefix_f = f'FILTER(STRSTARTS(STR(?celex), "{p}"))'
    q = f'''PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?work ?celex ?title WHERE {{
  ?exp cdm:expression_title ?title .
  ?title bif:contains {arg} .
  ?exp cdm:expression_belongs_to_work ?work .
  ?work cdm:resource_legal_id_celex ?celex .
  {lang_triple}
  {prefix_f}
}} LIMIT {int(fetch)}'''
    try:
        return _sparql(q, timeout=20.0)
    except Exception:
        return []


def _hit_rank(celex: str, title: str, words: list[str]) -> int:
    tl = (title or "").lower()
    cover = sum(10 for w in words if w in tl)
    kind = {"3": 50, "1": 40, "2": 35, "6": 20, "5": 15, "4": 10, "7": 8, "9": 5}
    return cover + kind.get((celex or "9")[:1], 0)


def search_cellar(topic: str, limit: int = 12) -> list[dict]:
    """Return hits: celex, title, work. CELEX first, else bif:contains on expression_title."""
    celex = extract_celex(topic)
    if celex:
        q = f'''PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?work ?celex WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  FILTER(STRSTARTS(STR(?celex), "{celex}"))
}} LIMIT {int(limit)}'''
        rows = _sparql(q)
        hits = []
        for row in rows:
            hits.append({
                "celex": row.get("celex") or celex,
                "title": "",
                "work": row.get("work") or "",
            })
        if hits:
            return hits

    kw = _safe_kw(topic)
    words = [w for w in kw.split() if w]
    if not words:
        return []
    pl = any(c in "ąćęłńóśźż" for c in kw)
    rows = _title_search(words, fetch=max(40, int(limit) * 4), lang="POL" if pl else "")
    if len({(r.get("celex") or "") for r in rows}) < 3:
        extra = _title_search(words, fetch=max(40, int(limit) * 4), lang="")
        seen_c = {(r.get("celex") or "") for r in rows}
        for r in extra:
            if (r.get("celex") or "") not in seen_c:
                rows.append(r)
                seen_c.add(r.get("celex") or "")
    ranked = sorted(rows, key=lambda r: _hit_rank(r.get("celex") or "", r.get("title") or "", words), reverse=True)
    hits = []
    seen: set[str] = set()
    for row in ranked:
        c = row.get("celex") or ""
        if not c or c in seen:
            continue
        seen.add(c)
        hits.append({"celex": c, "title": row.get("title") or "", "work": row.get("work") or ""})
        if len(hits) >= int(limit):
            break
    return hits


_LANG_AL = {"POL": "pl", "ENG": "en", "ENU": "en"}


_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# Last publication read: byte_size is len(body), never Content-Length.
_last_http: dict = {"status": 0, "content_type": "", "byte_size": 0, "url": "", "error": ""}


def _https_location(url: str) -> str:
    """CELLAR 303 Location is http://. Follow the same path over https."""
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme.lower() == "http":
        return urllib.parse.urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))
    return url


def _is_eurlex(url: str) -> bool:
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return host == "eur-lex.europa.eu" or host.endswith(".eur-lex.europa.eu")


def _header(headers, name: str) -> str:
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    value = getter(name) or getter(name.lower())
    return str(value) if value else ""


def _status_of(resp) -> int:
    return int(getattr(resp, "status", None) or getattr(resp, "code", None) or 0)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Leave 3xx to the caller so an http:// Location can be rewritten to https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_once(req: urllib.request.Request, timeout: float):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc


def _remember(status: int, content_type: str, byte_size: int, url: str, error: str) -> None:
    global _last_http
    _last_http = {
        "status": status,
        "content_type": content_type,
        "byte_size": byte_size,
        "url": url,
        "error": error,
    }


def _http_html(url: str, extra_headers: Optional[dict] = None, max_bytes: int = 250_000) -> tuple[str, str]:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)
    _remember(0, "", 0, "", "")
    current = _https_location(url)
    for _hop in range(6):
        if _is_eurlex(current):
            _remember(0, "", 0, current, "refused eur-lex.europa.eu")
            return "", ""
        req = urllib.request.Request(current, headers=headers)
        try:
            resp = _open_once(req, timeout=20)
        except Exception as exc:
            _remember(0, "", 0, current, f"{type(exc).__name__}: {exc}")
            raise
        try:
            status = _status_of(resp)
            if status in _REDIRECTS:
                loc = _header(resp.headers, "Location")
                if not loc:
                    _remember(status, "", 0, current, "no_location")
                    return "", ""
                nxt = _https_location(urllib.parse.urljoin(current, loc))
                if _is_eurlex(nxt):
                    _remember(status, "", 0, nxt, "refused eur-lex.europa.eu")
                    return "", ""
                try:
                    resp.read()
                except Exception:
                    pass
                current = nxt
                continue
            raw = resp.read(max_bytes)
            ctype = _header(resp.headers, "Content-Type")
            final = resp.geturl() if hasattr(resp, "geturl") else current
            error = raw.decode("utf-8", "replace") if status >= 400 else ""
            # Measured from the bytes read. DOC_1 sends no Content-Length.
            _remember(status, ctype, len(raw), final, error)
            if status != 200:
                return "", ""
            html = raw.decode("utf-8", "replace")
            if "rdf+xml" in ctype.lower() or html.lstrip().startswith("<rdf:RDF"):
                return "", ""
            if len(html) < 400:
                return "", ""
            return final, html
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
    _remember(0, "", 0, current, "too_many_redirects")
    return "", ""


def celex_work_uri(celex: str) -> str:
    celex = (celex or "").upper()
    if not celex:
        return ""
    q = f'''PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?work WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  FILTER(STR(?celex) = "{celex}")
}} LIMIT 1'''
    try:
        rows = _sparql(q, timeout=20.0)
    except Exception:
        return ""
    return (rows[0].get("work") if rows else "") or ""


def celex_family(celex: str) -> list[dict]:
    """Base act + corrigenda with document dates. New R(n) = legal change."""
    celex = (celex or "").upper()
    if not celex:
        return []
    q = f'''PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?celex ?date ?work WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  FILTER(STRSTARTS(STR(?celex), "{celex}"))
  OPTIONAL {{ ?work cdm:work_date_document ?date }}
}} LIMIT 20'''
    try:
        rows = _sparql(q, timeout=20.0)
    except Exception:
        return []
    out = []
    seen = set()
    for row in rows:
        c = row.get("celex") or ""
        if c in seen:
            continue
        seen.add(c)
        out.append({
            "celex": c,
            "date": (row.get("date") or "")[:10],
            "work": row.get("work") or "",
        })
    out.sort(key=lambda r: (r.get("date") or "", r.get("celex") or ""))
    return out


def fetch_celex_html(
    celex: str,
    langs: tuple[str, ...] = ("POL", "ENG", "ENU"),
    max_bytes: int = 250_000,
) -> tuple[str, str]:
    """OJ XHTML: celex.LANG, then Cellar work URI + Accept-Language (2026+ acts)."""
    celex = (celex or "").upper()
    if not celex:
        return "", ""
    # Parentheses of a suffixed id (52023XC0914(01)) go out unencoded: CELLAR reads the path
    # literally and takes %28 / %29 as part of the id (live reading 2026-11-19, tests/fixtures/legal).
    celex_path = urllib.parse.quote(celex, safe="()")
    for lang in langs:
        try:
            url, html = _http_html(
                f"https://publications.europa.eu/resource/celex/{celex_path}.{lang}",
                max_bytes=max_bytes,
            )
        except Exception:
            url, html = "", ""
        if html:
            return url, html
    work = celex_work_uri(celex)
    if work:
        for lang in langs:
            al = _LANG_AL.get(lang, "en")
            try:
                url, html = _http_html(
                    work, extra_headers={"Accept-Language": al}, max_bytes=max_bytes
                )
            except Exception:
                url, html = "", ""
            if html:
                return url, html
    return "", ""


def watch_celex(celex: str, previous_sha: str = "") -> dict:
    """Re-fetch family + body hash. SAME/NEW/CHANGED. Does not write state."""
    celex = (celex or "").upper()
    family = celex_family(celex)
    url, html = fetch_celex_html(celex, max_bytes=3_000_000)
    sha = hashlib.sha256((html or "").encode("utf-8")).hexdigest() if html else ""
    if not html and not family:
        status = "MISS"
    elif previous_sha and sha and previous_sha != sha:
        status = "CHANGED"
    elif previous_sha and sha and previous_sha == sha:
        status = "SAME"
    else:
        status = "NEW"
    corrigenda = [f for f in family if "R(" in (f.get("celex") or "")]
    return {
        "celex": celex,
        "status": status,
        "sha256": sha,
        "html_url": url,
        "html_bytes": len(html or ""),
        "family": family,
        "corrigenda": corrigenda,
        "n_family": len(family),
        "n_corrigenda": len(corrigenda),
        "html": html or "",
    }


def _strip_html(html: str) -> str:
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    s = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


HUDOC_RESULTS = "https://hudoc.echr.coe.int/app/query/results"
HUDOC_HTML = "https://hudoc.echr.coe.int/app/conversion/docx/html/body"
HUDOC_RANKING = "22222222-eeee-0000-0000-000000000000"
HUDOC_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HUDOC_DROP = {"hudoc", "echr", "strasbourg", "ecthr", "echrcoe"}


def _hudoc_kw(topic: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9ąćęłńóśźż \\-]", " ", topic or "")
    s = re.sub(r"\s+", " ", s).strip().lower()
    words = [w for w in s.split() if w not in _HUDOC_DROP and len(w) > 2]
    return " ".join(words[:6])


def hudoc_search(topic: str, timeout: float = 20.0, length: int = 10) -> Optional[dict]:
    """HUDOC /app/query/results — needs full Lucene query, not a bare path (bare = 404)."""
    kw = _hudoc_kw(topic)
    if len(kw) < 3:
        return None
    q = (
        "contentsitename:ECHR AND (NOT (doctype=PR OR doctype=HFCOMOLD OR doctype=HECOMOLD)) "
        f"AND ({kw}) AND ((documentcollectionid=\"GRANDCHAMBER\") OR (documentcollectionid=\"CHAMBER\"))"
    )
    params = urllib.parse.urlencode(
        {
            "query": q,
            "select": (
                "itemid,docname,appno,kpdate,importance,ecli,respondent,"
                "conclusion,languageisocode,doctypebranch"
            ),
            "sort": "",
            "start": "0",
            "length": str(int(length)),
            "rankingModelId": HUDOC_RANKING,
        }
    )
    url = f"{HUDOC_RESULTS}?{params}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": HUDOC_UA, "Accept": "application/json, */*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("results")
    total = data.get("resultcount")
    if (not isinstance(rows, list) or not rows
            or type(total) is not int or total < len(rows)):
        return None
    # A populated results envelope alone is not evidence of a judgment.
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("columns"), dict):
            return None
        cols = row["columns"]
        if not all(isinstance(cols.get(key), str) and cols[key].strip()
                   for key in ("itemid", "docname")):
            return None
    lines = [f"HUDOC ECHR hits for: {topic} (total={total})", ""]
    first_id = ""
    for row in rows[:length]:
        cols = row.get("columns") or {}
        iid = cols.get("itemid") or ""
        if not first_id and iid:
            first_id = iid
        lines.append(
            f"{cols.get('kpdate','')[:10]} {cols.get('appno','')} "
            f"{cols.get('respondent','')} {cols.get('docname','')[:180]}"
        )
        if iid:
            lines.append(f"  itemid={iid} ecli={cols.get('ecli','')}")
    html_url, body = "", ""
    if first_id:
        html_url, html = fetch_hudoc_html(first_id)
        if html:
            body = _strip_html(html)
            lines.append("")
            lines.append(f"Judgment HTML via {html_url}")
            lines.append(body[:6000])
    text = "\n".join(lines)
    return {
        "source": "api:HUDOC ECHR",
        "url": html_url or url,
        "api_name": "HUDOC ECHR",
        "category": "regulatory",
        "query_type": "regulations",
        "endpoint_returns": "ECHR judgments JSON + HTML",
        "data": {"count": len(rows), "total": total, "itemid": first_id},
        "raw_text": text[:20000],
    }


def fetch_hudoc_html(itemid: str, timeout: float = 20.0) -> tuple[str, str]:
    iid = (itemid or "").strip()
    if not iid:
        return "", ""
    url = f"{HUDOC_HTML}?library=ECHR&id={urllib.parse.quote(iid)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": HUDOC_UA, "Accept": "text/html,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return "", ""
            html = r.read(250_000).decode("utf-8", "replace")
    except Exception:
        return "", ""
    if len(html) < 200:
        return "", ""
    return url, html


def saos_search(topic: str, timeout: float = 25.0) -> Optional[dict]:
    """urllib + 25s — aiohttp from this host times out. Retry 502/503/504."""
    q = urllib.parse.quote_plus(topic or "")
    url = f"https://www.saos.org.pl/api/search/judgments?all={q}&pageSize=10&pageNumber=0"
    data = None
    last_err = None
    for attempt, wait in enumerate((0, 1.5, 3.0), start=1):
        if wait:
            time.sleep(wait)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    last_err = r.status
                    continue
                data = json.loads(r.read().decode("utf-8", "replace"))
                break
        except urllib.error.HTTPError as e:
            last_err = e.code
            if e.code not in (502, 503, 504) or attempt == 3:
                return None
        except Exception:
            return None
    if not data:
        return None
    items = data.get("items") or []
    if not items:
        return None
    lines = [f"SAOS hits for: {topic} (total={(data.get('info') or {}).get('totalResults')})", ""]
    for it in items[:10]:
        cases = it.get("courtCases") or []
        num = (cases[0] or {}).get("caseNumber") if cases else ""
        lines.append(
            f"{it.get('judgmentDate','')} {it.get('judgmentType','')} {num} "
            f"{(it.get('textContent') or '')[:240]}"
        )
        if it.get("href"):
            lines.append(f"  {it['href']}")
    text = "\n".join(lines)
    return {
        "source": "api:SAOS (Polish Court Judgments)",
        "url": url,
        "api_name": "SAOS (Polish Court Judgments)",
        "category": "regulatory",
        "query_type": "regulations",
        "endpoint_returns": "Polish judgments",
        "data": {"count": len(items), "total": (data.get("info") or {}).get("totalResults")},
        "raw_text": text[:12000],
    }


ELI_SEARCH = "https://api.sejm.gov.pl/eli/acts/search"
ELI_ACT = "https://api.sejm.gov.pl/eli/acts"
ELI_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_ELI_DROP = {
    "isap", "dziennik", "ustaw", "ustawy", "eli", "sejm", "dzu", "aktu", "aktów",
    "n-lex", "nlex",
}


def _eli_kw(topic: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9ąćęłńóśźż \\-]", " ", topic or "")
    s = re.sub(r"\s+", " ", s).strip().lower()
    words = [w for w in s.split() if w not in _ELI_DROP and len(w) > 2]
    return " ".join(words[:8])


def fetch_eli_html(year: int | str, pos: int | str, timeout: float = 20.0) -> tuple[str, str]:
    url = f"{ELI_ACT}/DU/{int(year)}/{int(pos)}/text.html"
    req = urllib.request.Request(url, headers={"User-Agent": ELI_UA, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return "", ""
            html = r.read(250_000).decode("utf-8", "replace")
    except Exception:
        return "", ""
    if len(html) < 200:
        return "", ""
    return url, html


def eli_search(topic: str, timeout: float = 20.0, limit: int = 8) -> Optional[dict]:
    """Sejm ELI JSON — not isap.sejm.gov.pl ByYear (WAF/JS)."""
    kw = _eli_kw(topic)
    if len(kw) < 3:
        return None
    url = ELI_SEARCH + "?" + urllib.parse.urlencode({"title": kw})
    req = urllib.request.Request(
        url,
        headers={"User-Agent": ELI_UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    items = data.get("items") or []
    total = data.get("totalCount") or data.get("count") or 0
    if not items:
        return None
    lines = [f"Dziennik Ustaw ELI hits for: {topic} (total={total})", ""]
    first_html = ""
    first_url = url
    for it in items[:limit]:
        disp = it.get("displayAddress") or ""
        title = it.get("title") or ""
        lines.append(f"{it.get('promulgation','')} {disp} {title[:180]}")
        lines.append(f"  {ELI_ACT}/{it.get('address','')}")
        if not first_html and it.get("textHTML") and it.get("year") and it.get("pos"):
            html_url, html = fetch_eli_html(it["year"], it["pos"])
            if html:
                first_url, first_html = html_url, html
                lines.append("")
                lines.append(f"Act HTML via {html_url}")
                lines.append(_strip_html(html)[:6000])
    text = "\n".join(lines)
    return {
        "source": "api:Dziennik Ustaw ELI",
        "url": first_url,
        "api_name": "Dziennik Ustaw ELI",
        "category": "regulatory",
        "query_type": "regulations",
        "endpoint_returns": "Dz.U. ELI JSON + HTML",
        "data": {"count": len(items), "total": total},
        "raw_text": text[:20000],
    }


def hits_to_doc(topic: str, hits: list[dict]) -> Optional[dict]:
    if not hits:
        return None
    lines = [f"EUR-Lex / Cellar hits for: {topic}", ""]
    for h in hits:
        cellar = h.get("work") or ""
        lines.append(f"CELEX {h.get('celex','')} | {h.get('title','')[:200]}")
        if cellar:
            lines.append(f"  {cellar}")
    # Full OJ: first main CELEX (skip corrigenda R() )
    main = next((h.get("celex") for h in hits if h.get("celex") and "R(" not in (h.get("celex") or "")), "") or (hits[0].get("celex") or "")
    family = celex_family(str(main)) if main else []
    if family:
        lines.append("")
        lines.append("Family (base + corrigenda; new R(n) = zmiana aktu):")
        for f in family:
            lines.append(f"  {f.get('date','')} {f.get('celex','')}")
    html_url, html = fetch_celex_html(str(main))
    if html:
        body = _strip_html(html)
        lines.append("")
        lines.append(f"OJ text via {html_url}")
        lines.append(body[:8000])
    text = "\n".join(lines)
    if len(text) < 30:
        return None
    first = html_url or hits[0].get("work") or SPARQL
    return {
        "source": "api:EUR-Lex Cellar",
        "url": first,
        "api_name": "EUR-Lex Cellar",
        "category": "regulatory",
        "query_type": "eu_law",
        "endpoint_returns": "CELEX + Cellar URI + OJ XHTML + family dates",
        "data": {"hits": hits, "count": len(hits), "html_url": html_url, "family": family},
        "raw_text": text[:20000],
    }


_CURIA_DROP = {
    "curia", "cjeu", "tsue", "infocuria", "ecj", "rpex", "infocuria",
    "trybunal", "trybunału", "trybunale",
}


def curia_search(topic: str) -> Optional[dict]:
    """CJEU via Cellar CELEX 6… — not InfoCuria RPEX/JSF, not eur-lex.europa.eu."""
    celex = extract_celex(topic)
    hits: list[dict] = []
    if celex.startswith("6"):
        hits = search_cellar(celex)
    else:
        kw = _safe_kw(topic)
        words = [w for w in kw.split() if w not in _CURIA_DROP]
        if not words:
            return None
        rows = _title_search(words, fetch=40, celex_prefix="6")
        ranked = sorted(
            rows,
            key=lambda r: _hit_rank(r.get("celex") or "", r.get("title") or "", words),
            reverse=True,
        )
        seen: set[str] = set()
        for row in ranked:
            c = row.get("celex") or ""
            if not c.startswith("6") or c in seen:
                continue
            seen.add(c)
            hits.append({"celex": c, "title": row.get("title") or "", "work": row.get("work") or ""})
            if len(hits) >= 8:
                break
    if not hits and celex.startswith("6"):
        url, html = fetch_celex_html(celex)
        if not html:
            return None
        body = _strip_html(html)
        text = f"CURIA / Cellar {celex}\n{url}\n{body[:8000]}"
        return {
            "source": "api:CURIA CJEU",
            "url": url,
            "api_name": "CURIA CJEU",
            "category": "regulatory",
            "query_type": "eu_law",
            "endpoint_returns": "CJEU judgment XHTML via Cellar",
            "data": {"celex": celex, "count": 1},
            "raw_text": text[:20000],
        }
    doc = hits_to_doc(topic, hits)
    if not doc:
        return None
    doc["api_name"] = "CURIA CJEU"
    doc["source"] = "api:CURIA CJEU"
    doc["endpoint_returns"] = "CJEU judgment XHTML via Cellar (CELEX 6… or title)"
    return doc
