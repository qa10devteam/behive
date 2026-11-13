"""Scout module — query generation, source discovery, RSS/DDG/API routing, and URL scoring."""

import logging
log = logging.getLogger(__name__)

import sys
import asyncio
import json
import re
import time
import unicodedata
import urllib.request
from datetime import datetime
from urllib.parse import urlparse, quote_plus, unquote
from typing import Optional

from behive.engine.db import connect as _db_connect
import feedparser

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

DB_PATH = ""  # Legacy — PostgreSQL is primary


def _pg_connect_retry(path: str = DB_PATH, read_only: bool = False, max_retries: int = 5):
    """Connect via hive2_db (PostgreSQL)."""
    return _db_connect(read_only=read_only)

# Legacy public compatibility constant. Scoring no longer treats this list as
# unconditional authority: domain prestige is not evidence of topic relevance.
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


# Reuse process.RelevanceFilter's Unicode tokenizer and light PL/EN matcher;
# supplement its EN stopwords with PL function words, not topic-specific terms.
_SCOUT_STOPWORDS = frozenset({
    "dla", "oraz", "jak", "jest", "się", "sie", "przez", "przy", "nad", "pod", "bez",
    "czy", "jego", "jej", "ich", "tak", "ten", "tej", "tego", "też", "tez", "tym",
    "jako", "aby", "które", "który", "która", "których", "między", "miedzy",
    "był", "była", "były", "będzie", "będą", "also", "are", "was", "were",
})
_REGULATORY_INTENT = ("ustaw", "rozporządze", "regulat", "directive", "dyrektyw", "legisla", "przepis")
_ACADEMIC_INTENT = ("study", "studies", "paper", "academic", "naukow")
_COMMERCIAL_INTENT = ("dostawc", "ofert", "cennik", "wdroż", "supplier", "vendor", "pricing", "product", "zakup")
# Listing/intent words describe WHAT to find about the subject, not the subject.
_LISTING_INTENT = _COMMERCIAL_INTENT + (
    "producen", "firm", "lista", "ranking", "manufacturer", "provider", "compan", "hersteller",
    "anbieter", "leading", "list")
_PROCUREMENT_TOPIC = ("przetarg", "zamówie", "zamowie", "tender", "procurement", "ausschreib")
_JOB_TOPIC = ("prac", "rekrutac", "job", "career", "kariera", "zatrudni")
# Pages that are rarely supplier/product evidence for a commercial question:
# public procurement notices, legal-act PDFs and BIP bulletins, job boards,
# and author/tag/search archives. Soft penalty only (never dropped).
_PROCUREMENT_LAW_DOMAINS = (
    "ted.europa.eu", "op.europa.eu", "bazakonkurencyjnosci.funduszeeuropejskie.gov.pl",
    "ezamowienia.gov.pl", "platformazakupowa.pl", "sejm.gov.pl", "dziennikustaw.gov.pl",
    "monitorpolski.gov.pl",
)
_JOB_BOARD_DOMAINS = ("aplikuj.pl", "pracuj.pl", "praca.pl", "indeed.com", "glassdoor.com", "gowork.pl")
_PROCUREMENT_PATH = re.compile(
    r"/public-procurement/|/procurement-details/|/notice/|/ogloszenia/|/eli/acts/"
    r"|/tenders?/|/przetargi?/|/zamowienia-publiczne/")
_ARCHIVE_PATH = re.compile(r"(?:^|/)(?:author|autor|tag|tagi|search|szukaj|wyszukiwarka)(?:/|$)")


def _concept_synonym(keyword: str, word: str) -> bool:
    """Tiny, explicit concept equivalences for subject coverage (prefix based).

    Telemetry IS remote monitoring: supplier pages often say "monitoring
    zbiorników" and never "telemetria". Keep this list short and generic.
    """
    for concept, synonyms in _CONCEPT_SYNONYMS:
        if keyword.startswith(concept) and word.startswith(synonyms):
            return True
    return False


# Share of relevance from subject coverage; the rest is breadth over all topic words.
_SUBJECT_WEIGHT = 0.5

_CONCEPT_SYNONYMS = (
    ("telemetr", ("monitor",)),
    ("monitor", ("telemetr",)),
)


_EN_FUNCTION_WORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "use", "using", "uses", "of", "in", "on", "at",
    "to", "by", "an", "as", "or", "is", "are", "this", "that", "these", "those", "how", "what",
})
# A coordinated part with a pronoun referring back ("w ich produkcji", "in their
# manufacturing") continues the previous subject instead of being an alternative.
_ANAPHORA = re.compile(r"(?<![^\W_])(?:ich|jego|jej|nich|nim|nimi|its|their|them)(?![^\W_])", re.I)


def _topic_subject_alternatives(topic: str) -> list[tuple[str, ...]]:
    """Required subject concepts of a topic, per coordinated alternative.

    "Systemy telemetrii zbiorników paliw i chemikaliów" -> (telemetrii, zbiorników,
    paliw) | (telemetrii, zbiorników, chemikaliów): a single coordinated word
    replaces the last modifier. "Dostawcy systemów WMS i telemetrii zbiorników"
    -> (wms,) | (telemetrii, zbiorników). Facets after ':' and listing/intent
    words (dostawcy, producenci, cennik ...), generic words and years are not
    subject concepts.
    """
    from behive.engine.process import RelevanceFilter

    subject = re.split(r"[:;]", unicodedata.normalize("NFKC", topic), maxsplit=1)[0]
    parts = re.split(r"\s+(?:i|oraz|lub|albo|and|or|und|oder)\s+|\s+/\s+|,", subject, flags=re.I)
    alternatives: list[tuple[str, ...]] = []
    for part in parts:
        anaphoric = bool(_ANAPHORA.search(part))
        words = []
        acronyms = {t.casefold() for t in re.findall(r"(?<![^\W_])[A-Z]{2}(?![^\W_])", part)}
        for token in re.findall(r"[^\W_]{2,}", part.casefold()):
            if len(token) == 2 and token not in acronyms:
                continue  # keep two-letter acronyms such as AI/ML only
            if (token in _SCOUT_STOPWORDS or token in _EN_FUNCTION_WORDS or _ANAPHORA.fullmatch(token) or token in RelevanceFilter._GENERIC_KEYWORDS
                    or token in RelevanceFilter._CODE_KEYWORDS or token.isdigit()
                    or token.startswith(_LISTING_INTENT)
                    or any(RelevanceFilter._keywords_match(token, w) for w in words)):
                continue  # inflections of one word are one concept
            words.append(token)
        if not words:
            continue
        if alternatives and anaphoric:
            # "X i ... w ICH produkcji": the part refers back to X -> one subject.
            merged = alternatives.pop()
            alternatives.append(merged + tuple(w for w in words if w not in merged))
        elif alternatives and len(words) == 1 and len(alternatives[-1]) > 1:
            alternatives.append(alternatives[-1][:-1] + (words[0],))
        else:
            alternatives.append(tuple(words))
    return alternatives


def _explicit_topic_entities(topic: str) -> list[str]:
    """Conservative anchors, matching the planner's rule, not a name guesser.

    Quotes declare a name. Otherwise require a capitalized multiword run away
    from a sentence start; a first word such as 'Systemy' is never an entity.
    Ambiguous title-case runs starting a sentence require explicit quotes.
    """
    text = unicodedata.normalize("NFKC", topic)
    names = [next(part for part in match if part).strip() for match in re.findall(
        r'"([^"\n]{2,100})"|[„“]([^”"\n]{2,100})[”"]|«([^»\n]{2,100})»|(?<!\w)\'([^\'\n]{2,100})\'', text)]
    run = []
    tokens = list(re.finditer(r"[^\W\d_][^\W_]*(?:[-'][^\W\d_][^\W_]*)*", text))
    for token in tokens + [None]:
        capitalized = token is not None and token.group()[0].isupper()
        if run and (token is None or not capitalized or not text[run[-1].end():token.start()].isspace()):
            prefix = text[:run[0].start()].rstrip()
            if len(run) >= 2 and prefix and prefix[-1] not in ".!?":
                names.append(text[run[0].start():run[-1].end()])
            run = []
        if capitalized:
            run.append(token)
    return list({name.casefold(): name for name in names if any(c.isalpha() for c in name)}.values())


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
        self.db = _pg_connect_retry(DB_PATH)
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
        """Make sure hive_sources_id_seq exists and is ahead of max(id). Never drop it.

        The old DROP/CREATE ran on every mission: in production the runtime role
        does not own the sequence (ERROR 'must be owner' in the PostgreSQL log), and
        on a fresh install a second concurrent scout could drop the sequence between
        another scout's DROP and CREATE, failing its inserts. Now: create only when
        missing, advance (setval) only when behind max(id) and only with the
        privileges to read and update it; never rewind. A session advisory lock
        serialises concurrent scouts of this database.
        """
        locked = False
        try:
            self.db.execute("SELECT pg_advisory_lock(hashtextextended('behive:hive_sources_id_seq', 0))")
            locked = True
            exists = self.db.execute(
                "SELECT to_regclass('hive_sources_id_seq') IS NOT NULL").fetchone()[0]
            if not exists:
                self.db.execute("CREATE SEQUENCE IF NOT EXISTS hive_sources_id_seq")
            can_read, can_update = self.db.execute(
                "SELECT has_sequence_privilege('hive_sources_id_seq', 'SELECT'), "
                "has_sequence_privilege('hive_sources_id_seq', 'UPDATE')").fetchone()
            if not (can_read and can_update):
                log.debug("  [init_seq] no SELECT/UPDATE on hive_sources_id_seq; leaving it as is")
                return
            max_id = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM hive_sources").fetchone()[0]
            last_value, is_called = self.db.execute(
                "SELECT last_value, is_called FROM hive_sources_id_seq").fetchone()
            next_value = last_value + 1 if is_called else last_value
            if next_value <= max_id:
                # GREATEST keeps any advance made meanwhile by nextval; never rewinds.
                self.db.execute(
                    "SELECT setval('hive_sources_id_seq', GREATEST(%s, "
                    "(SELECT last_value FROM hive_sources_id_seq)), true)", [max_id])
        except Exception as e:
            log.debug(f"  [init_seq] warning: {e}")
        finally:
            if locked:
                try:
                    self.db.execute("SELECT pg_advisory_unlock(hashtextextended('behive:hive_sources_id_seq', 0))")
                except Exception as e:
                    log.debug(f"  [init_seq] unlock warning: {e}")

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _score_source(self, url: str, title: str, snippet: str,
                      source_type: str) -> dict:
        from behive.engine.process import RelevanceFilter

        parsed = urlparse(url)
        path = unquote(parsed.path)
        combined = f"{title} {snippet} {path}".casefold()
        topic_words = (RelevanceFilter._extract_keywords(self.topic)
                       - _SCOUT_STOPWORDS - RelevanceFilter._GENERIC_KEYWORDS)
        words = RelevanceFilter._extract_keywords(combined) - _SCOUT_STOPWORDS
        result_words = sorted(words)

        # Count independent matches, not substrings or domain/query tokens.
        # One result token cannot satisfy several inflected topic keywords.
        matches = 0
        for keyword in sorted(topic_words):
            word = next((w for w in sorted(words)
                         if RelevanceFilter._keywords_match(keyword, w)), None)
            if word is not None:
                matches += 1
                words.remove(word)
        # Five subject matches saturate 25; a longer brief must not dilute a
        # specific supplier page below harvest's relevance>=5 threshold.
        breadth = min(25, int(25 * matches / max(1, min(5, len(topic_words)))))

        # Ranking needs the topic's SUBJECT, not any topic word: a tank seller
        # or a fuel-price article shares "zbiorników"/"paliw" with a tank-telemetry
        # topic but not "telemetrii". Mix half the best coverage of one coordinated
        # subject alternative with half the old breadth of all topic words
        # (_SUBJECT_WEIGHT, chosen on labelled pools of 5 benchmark topics).
        if getattr(self, "_subject_alts_topic", None) != self.topic:
            self._subject_alts = _topic_subject_alternatives(self.topic)
            self._subject_alts_topic = self.topic
        coverage = 0.0
        for alternative in self._subject_alts:
            hit = sum(1 for keyword in alternative
                      if any(RelevanceFilter._keywords_match(keyword, w)
                             or _concept_synonym(keyword, w) for w in result_words))
            coverage = max(coverage, hit / len(alternative))
        if self._subject_alts:
            # Never below the old breadth score: harvest's relevance>=5 admission
            # (recall) stays as before; subject coverage only lifts the ranking,
            # and only when at least half of one subject alternative is present
            # (a dictionary entry for one word of a three-word subject is not).
            mixed = int(round(25 * _SUBJECT_WEIGHT * coverage + (1 - _SUBJECT_WEIGHT) * breadth))
            score_relevance = min(25, max(breadth, mixed) if coverage >= 0.5 else breadth)
        else:
            score_relevance = breadth

        # Retain entity anchoring only for an explicit name, never a topic's
        # first generic noun. Match the whole name, not an arbitrary substring
        # in a hostname, query parameter or a similarly named vendor.
        entities = _explicit_topic_entities(self.topic)
        if entities:
            entity_match = entity_url = False
            host_labels = (parsed.hostname or "").casefold().split(".")
            for entity in entities:
                tokens = re.findall(r"[^\W_]+", entity.casefold())
                pattern = r"(?<!\w)" + r"[\W_]+".join(re.escape(t) for t in tokens) + r"(?!\w)"
                own_host = any(label in {"-".join(tokens), "".join(tokens)} for label in host_labels)
                entity_match |= own_host or bool(re.search(pattern, combined))
                entity_url |= own_host or bool(re.search(pattern, path.casefold()))
            score_relevance = min(25, score_relevance + 10) if entity_match else max(0, score_relevance - 5)
            if entity_url:
                score_relevance = 25

        # Freshness — default 8 (neutral, nie kara za brak roku w snippecie)
        score_freshness = 0
        for pattern, pts in FRESHNESS_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE):
                score_freshness = max(score_freshness, pts)
        if score_freshness == 0:
            score_freshness = 8  # neutral baseline (previously 3 — punished undated pages)

        # All domains start equal. Official/academic provenance adds only 3
        # points, and only for explicit regulatory/academic topic intent. A TLD
        # (.org/.int/.gov) or arXiv alone says nothing about a supplier topic.
        domain = (parsed.hostname or "").casefold()
        regulatory = any(w.startswith(_REGULATORY_INTENT) for w in topic_words)
        academic = any(w.startswith(_ACADEMIC_INTENT) for w in topic_words)
        commercial = any(w.startswith(_COMMERCIAL_INTENT) for w in topic_words)
        official = (bool(re.search(r"(?:^|\.)(?:gov|int)(?:\.[a-z]{2,3})?$", domain))
                    or any(domain == d or domain.endswith("." + d)
                           for d in ("europa.eu", "un.org")))
        scholarly = (bool(re.search(r"(?:^|\.)(?:edu|ac)(?:\.[a-z]{2,3})?$", domain))
                     or domain == "arxiv.org" or domain.endswith(".arxiv.org"))
        score_authority = 6 + (3 if (regulatory and official) or (academic and scholarly) else 0)

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
        # Metadata cannot outrank actual subject evidence, even in harvest's
        # relaxed fallback. Keep the row for review, but below its total>=15 gate.
        if score_relevance < 5:
            total = min(14, total)
        # A commercial supplier inquiry needs content, not a definition, ad or
        # sign-in/cart screen. This is a soft 8/100 penalty, never an admission ban.
        non_content = (
            any(domain == d or domain.endswith("." + d) for d in ("sjp.pwn.pl", "wsjp.pl", "olx.pl"))
            or bool(re.search(r"(?:^|/)(?:login|logowanie|signin|sign-in|wp-login|cart|koszyk|checkout)"
                              r"(?:\.(?:php|aspx?|html?))?(?:/|$)", path.casefold()))
        )
        if commercial and non_content:
            total -= 8
        # Procurement notices, legal-act/BIP documents, job boards and archive
        # pages are not supplier evidence unless the topic asks for them.
        topic_cf = self.topic.casefold()
        wants_procurement = any(p in topic_cf for p in _PROCUREMENT_TOPIC)
        wants_jobs = any(w.startswith(_JOB_TOPIC) for w in topic_words)
        host_labels = domain.split(".")
        procurement_or_law = (
            any(domain == d or domain.endswith("." + d) for d in _PROCUREMENT_LAW_DOMAINS)
            or any(label == "bip" or label.startswith("bip-") for label in host_labels)
            or bool(_PROCUREMENT_PATH.search(path.casefold())))
        job_board = any(domain == d or domain.endswith("." + d) for d in _JOB_BOARD_DOMAINS)
        archive = bool(_ARCHIVE_PATH.search(path.casefold()))
        if commercial and not regulatory and (
                (procurement_or_law and not wants_procurement)
                or (job_board and not wants_jobs) or archive):
            total -= 8
        return {
            "score_total":     max(0, min(100, total)),
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
            # Encyclopedic: wikipedia.org is ALLOWED — on this host Google/DDG/Brave
            # are CAPTCHA/429, MediaWiki is the only search that returns URLs.
            "wiktionary.org", "wikimedia.org", "wikisource.org",
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

        # Scoring/harvest decide topical value. A premium-domain/fixed-keyword
        # admission gate used to discard undated supplier pages *before* their
        # topic relevance could be scored. Unknown domains are not low quality.

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

    # ── Scout method: web search (multi-backend) ────────────────────────────────

    async def _ddg_text(self, query: str, region: str = "en-us",
                        max_results: int = 40, *, lang: Optional[str] = None) -> list[dict]:
        """Generic web search; retain the legacy method name for saved plans."""
        async with self._ddg_sem:
            try:
                from behive.engine.search_backends import get_engine
                engine = get_engine()
                try:
                    results = await engine.search(query, max_results, lang=lang or region.split("-")[0])
                except TypeError as exc:
                    # Older SearchEngine installations lack the lang kwarg. Do
                    # not retry TypeErrors raised *inside* a modern backend.
                    if (exc.__traceback__ is None or exc.__traceback__.tb_next is not None
                            or not re.search(r"unexpected keyword argument ['\"]lang['\"]", str(exc))):
                        raise
                    results = await engine.search(query, max_results)
                return [
                    {"url": r.get("url", ""), "title": r.get("title", ""),
                     "snippet": r.get("snippet", "")}
                    for r in (results or [])
                ]
            except Exception as e:
                log.debug(f"  [web_search] '{query[:60]}': {e}")
                return []

    # ── Scout method: news search (multi-backend) ──────────────────────────────

    async def _ddg_news(self, query: str, region: str = "en-us",
                        max_results: int = 40) -> list[dict]:
        """News search using best available backend."""
        async with self._ddg_sem:
            try:
                from behive.engine.search_backends import get_engine
                engine = get_engine()
                results = await engine.search_news(query, max_results)
                return [
                    {"url": r.get("url", ""), "title": r.get("title", ""),
                     "snippet": r.get("snippet", "")}
                    for r in (results or [])
                ]
            except Exception as e:
                log.debug(f"  [news_search] '{query[:60]}': {e}")
                return []

    # ── Scout method: DDG site: ───────────────────────────────────────────────

    async def _ddg_site(self, query: str, site: str,
                        max_results: int = 35, *, lang: Optional[str] = None) -> list[dict]:
        full_query = f"site:{site} {query}" if site else query
        return await self._ddg_text(full_query, max_results=max_results, lang=lang)

    # ── Scout method: DDG filetype:pdf ───────────────────────────────────────

    async def _ddg_filetype(self, query: str, max_results: int = 30, *, lang: Optional[str] = None) -> list[dict]:
        return await self._ddg_text(f"{query} filetype:pdf", max_results=max_results, lang=lang)

    # ── Scout method: DDG reddit ──────────────────────────────────────────────

    async def _ddg_reddit(self, query: str, max_results: int = 30, *, lang: Optional[str] = None) -> list[dict]:
        return await self._ddg_text(f"site:reddit.com {query}", max_results=max_results, lang=lang)

    # ── Scout method: DDG academic ────────────────────────────────────────────

    async def _ddg_academic(self, query: str, max_results: int = 25, *, lang: Optional[str] = None) -> list[dict]:
        full_q = f"site:arxiv.org OR site:scholar.google.com {query}"
        return await self._ddg_text(full_q, max_results=max_results, lang=lang)

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

    # ── Scout method: Direct URL fetch (API endpoints, known pages) ─────────

    async def _direct_url(self, url: str, title: str = "") -> list[dict]:
        """Fetch a known URL directly — bypasses search engines entirely.
        
        Use for: KRS API, Biała Lista VAT, company websites, registry endpoints,
        any URL that search engines won't surface but is known from the topic.
        """
        async with self._http_sem:
            try:
                import aiohttp
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/json,application/xhtml+xml,*/*",
                }
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers, ssl=False) as resp:
                        if resp.status == 200:
                            content_type = resp.headers.get("Content-Type", "")
                            # For JSON APIs — the URL itself is the result
                            # For HTML pages — the URL itself is the result
                            snippet = ""
                            if "json" in content_type:
                                body = await resp.text()
                                snippet = body[:512]
                            elif "html" in content_type:
                                body = await resp.text()
                                # Extract title from HTML if not provided
                                if not title:
                                    m = re.search(r"<title>([^<]+)</title>", body, re.I)
                                    if m:
                                        title = m.group(1).strip()
                                snippet = re.sub(r"<[^>]+>", " ", body)[:512]
                            
                            return [{"url": url, "title": title or url, "snippet": snippet}]
                        else:
                            log.debug(f"  [direct_url] {url} → HTTP {resp.status}")
                            return []
            except Exception as e:
                log.debug(f"  [direct_url] {url}: {e}")
                return []

    # ── Scout method: Direct URL batch (multiple known endpoints) ─────────

    async def _direct_url_batch(self, urls: list[str]) -> list[dict]:
        """Fetch multiple known URLs concurrently."""
        coros = [self._direct_url(u) for u in urls[:10]]  # cap at 10
        results_nested = await asyncio.gather(*coros, return_exceptions=True)
        all_results = []
        for r in results_nested:
            if isinstance(r, list):
                all_results.extend(r)
        return all_results

    # ── Task dispatcher ───────────────────────────────────────────────────────

    async def _run_task(self, task: dict) -> int:
        """Execute the requested method; companion news searches are opt-in."""
        method      = task.get("method", "ddg_text")
        query       = task.get("query", self.topic)
        region      = task.get("region", "en-us")
        site        = task.get("site", "")
        source_type = "web" if method == "web" else task.get("source", "ddg")
        lang        = task.get("language", task.get("lang", "en"))

        try:
            fan_coros = []
            fan_methods = set()

            # Primary (explicitly requested); old saved plans stay executable.
            primary_map = {
                "web":            lambda: self._ddg_text(query, region, lang=lang),
                "ddg_text":       lambda: self._ddg_text(query, region, lang=lang),
                "ddg_news":       lambda: self._ddg_news(query, region),
                "ddg_site":       lambda: self._ddg_site(query, site, lang=lang),
                "ddg_filetype":   lambda: self._ddg_filetype(query, lang=lang),
                "ddg_reddit":     lambda: self._ddg_reddit(query, lang=lang),
                "ddg_academic":   lambda: self._ddg_academic(query, lang=lang),
                "google_rss":     lambda: self._google_rss(query, lang),
                "wayback_cdx":    lambda: self._wayback_cdx(site, query),
                "jina_search":    lambda: self._jina_search(query),
                "curl_sitesearch":lambda: self._curl_sitesearch(query, site),
                "direct_url":     lambda: self._direct_url(task.get("url", query), task.get("title", "")),
                "direct_url_batch": lambda: self._direct_url_batch(task.get("urls", [query])),
            }
            if method in primary_map:
                fan_coros.append(primary_map[method]())
                fan_methods.add(method)

            # A legacy unknown method still gets a generic search, not three
            # unrelated searches. News/RSS companions require explicit intent.
            if not fan_coros:
                fan_coros.append(self._ddg_text(query, region, lang=lang))
            news_intent = (
                method in {"ddg_news", "google_rss"}
                or task.get("source", "").lower() in {"news", "rss", "gnews", "rss_news"}
                or task.get("intent", "").lower() in {"news", "latest_news", "current_news", "wiadomości", "aktualności"}
            )
            if news_intent:
                if "ddg_news" not in fan_methods:
                    fan_coros.append(self._ddg_news(query, region))
                if "google_rss" not in fan_methods:
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
    """Reuse Queen's bounded deterministic fallback; standalone never calls a model."""
    from behive.engine.orchestrator import QueenPlanner
    return QueenPlanner(topic)._fallback_plan()


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
