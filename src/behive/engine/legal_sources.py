"""Identifiers and read-only fetchers for CELLAR, Sejm ELI and Sejm prints.

CELEX text comes from cellar.fetch_celex_html. This module never builds or
requests a eur-lex.europa.eu URL.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from behive.engine import cellar

CELEX_MAX_BYTES = 3_000_000
PDF_MAX_BYTES = 8_000_000
_SEJM = "https://api.sejm.gov.pl"
_LEGAL_HOSTS = frozenset({
    "eur-lex.europa.eu",
    "publications.europa.eu",
    "api.sejm.gov.pl",
    "eli.gov.pl",
})
_CELEX_ID = re.compile(r"([1-7]\d{4}[A-Z]{1,2}\d{3,5})", re.I)
_ELI_DIR = re.compile(r"/eli/dir/(\d{4})/(\d{1,5})(?:/|$)", re.I)
_CELEX_PATH = re.compile(r"/resource/celex/([1-7]\d{4}[A-Z]{1,2}\d{3,5})", re.I)
_ELI_ACT = re.compile(r"/(?:eli/acts|api/acts)/([A-Za-z0-9]+)/(\d{4})/(\d+)(?:/|$)", re.I)
_PRINT = re.compile(r"/sejm/term(\d+)/prints/([^/?#]+)", re.I)
_ELI_HOSTS = frozenset({"api.sejm.gov.pl", "eli.gov.pl"})


@dataclass(frozen=True)
class LegalFetch:
    ok: bool
    source: str
    identifier: str
    title: str
    fetch_url: str
    content_type: str
    http_status: int
    byte_size: int
    text: str
    reason: str


def _blank(source: str, identifier: str, reason: str, **extra) -> LegalFetch:
    fields = dict(
        ok=False,
        source=source,
        identifier=identifier,
        title="",
        fetch_url="",
        content_type="",
        http_status=0,
        byte_size=0,
        text="",
        reason=reason,
    )
    fields.update(extra)
    return LegalFetch(**fields)


def _is_eurlex(url: str) -> bool:
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return host == "eur-lex.europa.eu" or host.endswith(".eur-lex.europa.eu")


def _celex_token(value: str) -> str:
    if not value:
        return ""
    marked = re.search(r"CELEX:([0-9A-Za-z]+)", value, re.I)
    token = marked.group(1) if marked else value
    hit = _CELEX_ID.search(token)
    return hit.group(1).upper() if hit else ""


def celex_from_url(url: str) -> str:
    """CELEX from uri=CELEX:…, /eli/dir/YYYY/N, or /resource/celex/. Empty if absent."""
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for key in ("uri", "celex"):
        for value in query.get(key, []):
            hit = _celex_token(value)
            if hit:
                return hit
    raw_query = urllib.parse.unquote(parsed.query or "")
    if "CELEX" in raw_query.upper():
        hit = _celex_token(raw_query)
        if hit:
            return hit
    path = urllib.parse.unquote(parsed.path or "")
    eli = _ELI_DIR.search(path)
    if eli:
        return f"3{eli.group(1)}L{int(eli.group(2)):04d}"
    cellar_path = _CELEX_PATH.search(path)
    if cellar_path:
        return cellar_path.group(1).upper()
    return ""


def eli_from_url(url: str) -> tuple[str, int, int] | None:
    """api.sejm.gov.pl/eli/acts/{publisher}/{year}/{pos} or eli.gov.pl/api/acts/…."""
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host not in _ELI_HOSTS or "/processes/" in path or "/prints/" in path:
        return None
    match = _ELI_ACT.search(path)
    if not match:
        return None
    return match.group(1).upper(), int(match.group(2)), int(match.group(3))


def sejm_print_from_url(url: str) -> tuple[int, str] | None:
    """/sejm/term{n}/prints/{number} or …/{number}.pdf → (term, number)."""
    if not url:
        return None
    match = _PRINT.search(urllib.parse.urlparse(url).path or "")
    if not match:
        return None
    number = match.group(2)
    if number.lower().endswith(".pdf"):
        number = number[:-4]
    if not number:
        return None
    return int(match.group(1)), number


def url_like_title(title: str, url: str) -> bool:
    """True when the title is empty, the URL, host+path, or a spaceless host string."""
    title = (title or "").strip()
    if not title:
        return True
    url = url or ""
    if title == url or title.rstrip("/") == url.rstrip("/"):
        return True
    parsed = urllib.parse.urlparse(url)
    host_path = f"{parsed.netloc}{parsed.path}"
    if host_path and (title == host_path or title.rstrip("/") == host_path.rstrip("/")):
        return True
    host = parsed.netloc or ""
    return bool(host) and " " not in title and host.lower() in title.lower()


def legal_fetch_host(url: str) -> bool:
    """Hosts fetched via CELLAR, ELI or a Sejm print, not via the EUR-Lex WAF."""
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return host in _LEGAL_HOSTS


def _fail_reason(meta: dict) -> str:
    status = int(meta.get("status") or 0)
    err = (meta.get("error") or "").strip()
    if status == 400 or "without language" in err or "Invalid content type" in err:
        return f"http_400: {err}" if err else "http_400"
    if status >= 400:
        return f"http_{status}: {err}" if err else f"http_{status}"
    if err:
        return f"no_html: {err}"
    return "no_html"


def _html_title(html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    if not match:
        return ""
    text = re.sub(r"(?is)<[^>]+>", " ", match.group(1))
    return re.sub(r"\s+", " ", text).strip()


def fetch_celex_text(celex: str, langs: tuple[str, ...] = ("POL", "ENG")) -> LegalFetch:
    """OJ text via cellar.fetch_celex_html. Empty HTML is ok=False, not an exception."""
    celex = (celex or "").strip().upper()
    if not celex:
        return _blank("cellar", "", reason="no_language")
    try:
        url, html = cellar.fetch_celex_html(celex, langs=langs, max_bytes=CELEX_MAX_BYTES)
    except Exception as exc:
        return _blank("cellar", celex, reason=f"no_html: {type(exc).__name__}")
    meta = dict(getattr(cellar, "_last_http", None) or {})
    if html:
        measured = meta.get("byte_size")
        return LegalFetch(
            ok=True,
            source="cellar",
            identifier=celex,
            title=_html_title(html),
            fetch_url=url or str(meta.get("url") or ""),
            content_type=str(meta.get("content_type") or "application/xhtml+xml"),
            http_status=int(meta.get("status") or 200),
            byte_size=int(measured) if measured is not None else len(html.encode("utf-8")),
            text=html,
            reason="",
        )
    return _blank(
        "cellar",
        celex,
        reason=_fail_reason(meta),
        fetch_url=str(meta.get("url") or ""),
        content_type=str(meta.get("content_type") or ""),
        http_status=int(meta.get("status") or 0),
        byte_size=int(meta.get("byte_size") or 0),
    )


def _http_get(
    url: str,
    headers: dict | None = None,
    max_bytes: int = PDF_MAX_BYTES,
    timeout: float = 20.0,
) -> dict:
    """GET url. byte_size is the bytes read, never Content-Length. No eur-lex."""
    if _is_eurlex(url):
        return {
            "status": 0,
            "url": url,
            "content_type": "",
            "body": b"",
            "byte_size": 0,
            "truncated": False,
            "error": "refused eur-lex.europa.eu",
        }
    req = urllib.request.Request(url, headers=headers or {})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        status = int(getattr(resp, "status", 0) or 0)
    except urllib.error.HTTPError as exc:
        resp = exc
        status = int(exc.code)
    raw = resp.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    try:
        final = resp.geturl()
    except Exception:
        final = url
    ctype = ""
    headers_out = getattr(resp, "headers", None)
    if headers_out is not None:
        ctype = headers_out.get("Content-Type") or ""
    close = getattr(resp, "close", None)
    if close:
        close()
    return {
        "status": status,
        "url": final,
        "content_type": ctype,
        "body": raw,
        "byte_size": len(raw),
        "truncated": truncated,
    }


def _pdf_attachment(attachments, number: str) -> str:
    names = []
    preferred = f"{number}.pdf"
    for item in attachments or []:
        name = item if isinstance(item, str) else (item.get("name") or item.get("fileName") or "")
        name = str(name or "")
        if not name or "/" in name or "\\" in name or name.startswith("."):
            continue
        if name.lower().endswith(".pdf"):
            names.append(name)
    for name in names:
        if name == preferred:
            return name
    return names[0] if names else ""


def _pdf_text(pdf_bytes: bytes) -> str:
    """Same parser as harvest._harvest_pdf: fitz page.get_text(), on bytes already limited."""
    if not pdf_bytes:
        return ""
    try:
        import fitz
    except ImportError:
        return ""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return ""
    try:
        return "\n".join(page.get_text() for page in doc).strip()
    finally:
        doc.close()


def _pdf_fetch(source: str, identifier: str, title: str, pdf_url: str) -> LegalFetch:
    if _is_eurlex(pdf_url):
        return _blank(source, identifier, reason="refused_eurlex", title=title)
    try:
        got = _http_get(
            pdf_url,
            headers={"User-Agent": cellar.ELI_UA, "Accept": "application/pdf,*/*"},
            max_bytes=PDF_MAX_BYTES,
        )
    except Exception as exc:
        return _blank(source, identifier, reason=f"no_pdf: {type(exc).__name__}", title=title, fetch_url=pdf_url)
    ctype = got.get("content_type") or "application/pdf"
    if got.get("truncated"):
        return _blank(
            source, identifier, reason="pdf_too_large", title=title,
            fetch_url=got.get("url") or pdf_url, content_type=ctype,
            http_status=int(got.get("status") or 0), byte_size=int(got.get("byte_size") or 0),
        )
    if int(got.get("status") or 0) != 200 or not got.get("body"):
        status = int(got.get("status") or 0)
        return _blank(
            source, identifier, reason=f"http_{status}" if status else "no_pdf", title=title,
            fetch_url=got.get("url") or pdf_url, content_type=ctype, http_status=status,
            byte_size=int(got.get("byte_size") or 0),
        )
    text = _pdf_text(got["body"])
    if not text:
        return _blank(
            source, identifier, reason="no_pdf", title=title,
            fetch_url=got.get("url") or pdf_url, content_type=ctype,
            http_status=int(got.get("status") or 0), byte_size=int(got.get("byte_size") or 0),
        )
    return LegalFetch(
        ok=True,
        source=source,
        identifier=identifier,
        title=title,
        fetch_url=got.get("url") or pdf_url,
        content_type=ctype,
        http_status=int(got.get("status") or 200),
        byte_size=int(got.get("byte_size") if got.get("byte_size") is not None else len(got["body"])),
        text=text,
        reason="",
    )


def _json_get(url: str) -> tuple[dict, dict]:
    got = _http_get(url, headers={"User-Agent": cellar.ELI_UA, "Accept": "application/json"})
    if int(got.get("status") or 0) != 200 or not got.get("body"):
        raise ValueError(f"http_{got.get('status') or 0}")
    return json.loads(got["body"].decode("utf-8", "replace")), got


def fetch_eli_act(publisher: str, year: int, pos: int) -> LegalFetch:
    """ELI metadata JSON. HTML only when textHTML; otherwise the API text PDF."""
    publisher = (publisher or "").strip().upper()
    try:
        year_i, pos_i = int(year), int(pos)
    except (TypeError, ValueError):
        return _blank("eli", f"{publisher}/{year}/{pos}", reason="bad_json")
    ident = f"{publisher}/{year_i}/{pos_i}"
    meta_url = f"{cellar.ELI_ACT}/{publisher}/{year_i}/{pos_i}"
    try:
        data, got = _json_get(meta_url)
    except Exception as exc:
        reason = str(exc) if str(exc).startswith("http_") else f"no_html: {type(exc).__name__}"
        return _blank("eli", ident, reason=reason, fetch_url=meta_url)
    title = str(data.get("title") or "").strip()
    if data.get("textHTML"):
        try:
            html_url, html = cellar.fetch_eli_html(year_i, pos_i)
        except Exception:
            html_url, html = "", ""
        if html:
            return LegalFetch(
                ok=True,
                source="eli",
                identifier=ident,
                title=title,
                fetch_url=html_url,
                content_type="text/html",
                http_status=200,
                byte_size=len(html.encode("utf-8")),
                text=html,
                reason="",
            )
    if data.get("textPDF"):
        return _pdf_fetch(source="eli", identifier=ident, title=title, pdf_url=f"{meta_url}/text.pdf")
    return _blank("eli", ident, reason="no_html", title=title, fetch_url=meta_url, http_status=int(got.get("status") or 0))


def fetch_sejm_print(term: int, number: str) -> LegalFetch:
    """Print title from JSON. Text from the .pdf attachment, not from the 329 B metadata."""
    try:
        term_i = int(term)
    except (TypeError, ValueError):
        return _blank("sejm_print", str(number or ""), reason="bad_json")
    number = str(number or "").strip()
    ident = f"{term_i}/{number}"
    meta_url = f"{_SEJM}/sejm/term{term_i}/prints/{number}"
    try:
        data, got = _json_get(meta_url)
    except Exception as exc:
        reason = str(exc) if str(exc).startswith("http_") else f"no_html: {type(exc).__name__}"
        return _blank("sejm_print", ident, reason=reason, fetch_url=meta_url)
    title = str(data.get("title") or "").strip()
    pdf_name = _pdf_attachment(data.get("attachments") or [], number)
    if not pdf_name:
        return _blank(
            "sejm_print", ident, reason="no_pdf", title=title, fetch_url=meta_url,
            http_status=int(got.get("status") or 0),
        )
    return _pdf_fetch(source="sejm_print", identifier=ident, title=title, pdf_url=f"{meta_url}/{pdf_name}")


_META_TEXT_CHARS = 2000
_scorers: dict = {}


def _scout_scorer(topic: str):
    """An existing-scorer instance for the topic. SwarmScout.__init__ opens a database
    connection and a thread pool; scoring needs only the topic, so it is skipped."""
    from behive.engine.scout import SwarmScout

    scorer = _scorers.get(topic)
    if scorer is None:
        scorer = SwarmScout.__new__(SwarmScout)
        scorer.topic = topic
        if len(_scorers) >= 8:
            _scorers.clear()
        _scorers[topic] = scorer
    return scorer


def rescore_legal_source(
    topic: str,
    url: str,
    title: str,
    snippet: str,
    meta_title: str,
    meta_text: str = "",
    source_type: str = "web",
) -> dict:
    """Score a legal source by its metadata title instead of its URL.

    Calls the scout scorer (SwarmScout._score_source); the 5/15 harvest threshold and the
    cap (relevance < 5 -> total <= 14) live there and are not touched. A URL-like stored
    title is replaced by meta_title + meta_text (never host+path). Any other stored title
    is kept and meta_title is added to it, so another manifestation of the same act (an
    English EUR-Lex row of a Polish CELEX title) is scored on the act's own title. An
    empty meta_title scores the stored inputs, i.e. the old scores.
    """
    meta_title = (meta_title or "").strip()
    scored_title = title or ""
    if meta_title:
        meta = f"{meta_title} {(meta_text or '').strip()[:_META_TEXT_CHARS]}".strip()
        scored_title = meta if url_like_title(title, url) else f"{scored_title} {meta}".strip()
    return _scout_scorer(topic)._score_source(url, scored_title, snippet or "", source_type or "web")
