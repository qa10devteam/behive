#!/usr/bin/env python3
"""
HIVE 2.0 — hive2_falsifier.py
Numerical Falsification Engine

3-warstwowy deterministyczny silnik falsyfikacji numerycznej:

  Layer 1 — ClaimDeduplicator
    TF-IDF cosine similarity na hive_claims → kolapsuje duplikaty w unikalne twierdzenia
    Zostawia canonical claim + listę source_urls które go potwierdzają

  Layer 2 — NumericalConflictDetector
    Regex extraction liczb + entity matching na deduplicated claims
    Flaguje pary: ta sama encja/rok, rozbieżność >20% lub absolutna >threshold
    Zero LLM — deterministyczny, tanie, szybkie

  Layer 3 — ConflictResolver (LLM Haiku)
    Wywołuje LLM TYLKO dla zidentyfikowanych konfliktów numerycznych
    "Czy to realna sprzeczność czy różny kontekst/definicja/metodologia?"
    Zapisuje wynik do hive3_falsification_evidence + hive_claims(FALSIFICATION)

CLI:
  python3 hive2_falsifier.py --mission <mid>    # jedna misja
  python3 hive2_falsifier.py --all              # wszystkie misje
  python3 hive2_falsifier.py --report           # pokaż znalezione konflikty bez resolvowania
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

from behive.engine.db import connect as _db_connect

log = logging.getLogger("HiveFalsifier")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HiveFalsifier] %(levelname)-5s — %(message)s",
)

DB_PATH = ""  # Legacy PostgreSQL removed — PostgreSQL is primary

# ── Unit normalization table ──────────────────────────────────────────────────
UNIT_CANON: dict[str, str] = {
    # percentage
    "%": "%", "proc": "%", "percent": "%", "pct": "%", "procent": "%",
    # millions
    "mln": "M", "million": "M", "millions": "M", "million$": "M$",
    "m": "M", "mn": "M",
    # billions
    "mld": "B", "billion": "B", "billions": "B", "bn": "B", "b": "B",
    "billion$": "B$", "mld$": "B$",
    # thousands
    "tys": "K", "tys.": "K", "thousand": "K", "thousands": "K", "k": "K",
    # currencies (keep as-is)
    "usd": "USD", "eur": "EUR", "pln": "PLN", "try": "TRY", "gbp": "GBP",
    # multipliers
    "x": "x", "×": "x", "times": "x", "razy": "x",
    # score/index
    "pp": "pp", "pkt": "pp", "pts": "pp", "points": "pp",
}

# ── Number extraction regex ───────────────────────────────────────────────────
_NUM_RE = re.compile(
    r"""
    (?:^|[\s\(«"'])
    (
      -?                              # optional minus
      \d{1,3}(?:[,\s]\d{3})*          # integer with optional thousands sep
      (?:[.,]\d+)?                    # optional decimal
    )
    \s*
    (
      %|proc\.?|percent|pct|
      mld\.?|billion[s]?|bn|
      mln\.?|million[s]?|mn|
      tys\.?|thousand[s]?|
      USD|EUR|PLN|TRY|GBP|
      pp|pkt|pts|points|
      [xX×]|razy|times|
      M(?![a-zA-Z])|B(?![a-zA-Z])|K(?![a-zA-Z])
    )
    (?=[\s\.,;:)\]»"'!?]|$)
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Year patterns
_YEAR_RE = re.compile(r"\b(20[12]\d|199\d)\b")

# Entity/subject patterns — what the number is about
_ENTITY_TRIGGERS = [
    # growth/change
    (re.compile(r"(wzrósł?|wzrost|grew?|growth|increased?|increase|surge[d]?|skoczył?)", re.I), "growth"),
    (re.compile(r"(spadł?|spadek|fell?|fall|declined?|decline|decreased?|decrease)", re.I), "decline"),
    # market share / penetration
    (re.compile(r"(udział|market\s+share|penetracj|penetration|rynek|market)", re.I), "market_share"),
    # budget / spending
    (re.compile(r"(budżet|budget|wydatk|spending|expenditure|nakład)", re.I), "budget"),
    # trade / export / import
    (re.compile(r"(eksport|export|import|handel|trade|wymian)", re.I), "trade"),
    # GDP / economy
    (re.compile(r"(PKB|GDP|gospodark|economy|ekonom)", re.I), "gdp"),
    # defence / military
    (re.compile(r"(obron|defenc|militar|wojsk|armia|army)", re.I), "defence"),
    # sanctions
    (re.compile(r"(sankcj|sanction|embargo|BIS|Entity\s+List)", re.I), "sanctions"),
    # users / audience
    (re.compile(r"(użytkown|users?|audience|widzowie|viewers?|subscribers?)", re.I), "users"),
    # revenue / profit
    (re.compile(r"(przychód|przychody|revenue|profit|zysk|income|obrót|turnover)", re.I), "revenue"),
]


def _db_connect(read_only: bool = False) -> Any:
    from behive.engine.db import connect as _db_connect
    return _db_connect(read_only=read_only)


def _ensure_tables(db: Any) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS hive3_falsification_evidence (
            id                    VARCHAR PRIMARY KEY,
            mission_id            VARCHAR,
            doc_url               VARCHAR,
            target_claim          VARCHAR,
            contra_evidence       VARCHAR,
            evidence_quality      VARCHAR,
            contradiction_type    VARCHAR,
            impact                VARCHAR,
            confidence            FLOAT DEFAULT 0.0,
            overall_challenge_score FLOAT DEFAULT 0.0,
            falsification_status  VARCHAR,
            created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS hive3_numerical_conflicts (
            id              VARCHAR PRIMARY KEY,
            mission_id      VARCHAR,
            entity_category VARCHAR,
            year_context    VARCHAR,
            claim_a_id      VARCHAR,
            claim_a_text    VARCHAR,
            value_a         DOUBLE,
            unit_a          VARCHAR,
            claim_b_id      VARCHAR,
            claim_b_text    VARCHAR,
            value_b         DOUBLE,
            unit_b          VARCHAR,
            divergence_pct  DOUBLE,
            resolution      VARCHAR,   -- NULL / REAL_CONFLICT / DIFFERENT_CONTEXT / METHODOLOGICAL
            resolver_notes  VARCHAR,
            resolved_at     TIMESTAMP,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — ClaimDeduplicator
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CanonicalClaim:
    claim_id: str
    text: str
    confidence: float
    mission_id: str
    source_urls: list[str] = field(default_factory=list)
    duplicate_count: int = 1


class ClaimDeduplicator:
    """
    TF-IDF cosine similarity dedup na hive_claims.
    Threshold=0.82 — twierdzenia o tej samej treści ale różnym sformułowaniu.
    Zwraca listę CanonicalClaim z source_urls.
    """

    SIMILARITY_THRESHOLD = 0.82

    def __init__(self, mission_id: str):
        self.mission_id = mission_id

    def deduplicate(self, db: Any) -> list[CanonicalClaim]:
        # Cap input claims to avoid O(n²) explosion on depth-5 missions
        _max_claims = int(os.environ.get("BEHIVE_FALSIFIER_MAX_CLAIMS", "500"))
        rows = db.execute("""
            SELECT id, claim, confidence, source_url
            FROM hive_claims
            WHERE mission_id = ?
              AND claim_type != 'FALSIFICATION'
              AND claim IS NOT NULL
              AND LENGTH(claim) > 30
            ORDER BY confidence DESC
            LIMIT ?
        """, [self.mission_id, _max_claims]).fetchall()

        if not rows:
            return []

        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity
            import numpy as np
        except ImportError:
            log.warning("sklearn nie dostępne — brak deduplikacji, zwracam raw claims")
            return [
                CanonicalClaim(str(r[0]), str(r[1]), float(r[2] or 0.5), self.mission_id, [str(r[3] or "")])
                for r in rows
            ]

        texts = [str(r[1]) for r in rows]

        try:
            vec = TfidfVectorizer(max_features=5000, ngram_range=(1, 2), sublinear_tf=True)
            mat = vec.fit_transform(texts)
        except Exception as e:
            log.debug(f"TF-IDF failed: {e}")
            return [
                CanonicalClaim(str(r[0]), str(r[1]), float(r[2] or 0.5), self.mission_id, [str(r[3] or "")])
                for r in rows
            ]

        # Greedy clustering — O(n²) ale n≤4000 jest OK (~0.3s)
        n = len(rows)
        assigned = [False] * n
        canonicals: list[CanonicalClaim] = []

        for i in range(n):
            if assigned[i]:
                continue
            # Compute similarities from i to all unassigned
            sims = cosine_similarity(mat[i], mat).flatten()
            cluster_indices = [j for j in range(i, n) if not assigned[j] and sims[j] >= self.SIMILARITY_THRESHOLD]

            canonical = CanonicalClaim(
                claim_id=str(rows[i][0]),
                text=str(rows[i][1]),
                confidence=float(rows[i][2] or 0.5),
                mission_id=self.mission_id,
                source_urls=[str(rows[j][3] or "") for j in cluster_indices if rows[j][3]],
                duplicate_count=len(cluster_indices),
            )
            canonicals.append(canonical)

            for j in cluster_indices:
                assigned[j] = True

        log.info(f"  Dedup: {n} claims → {len(canonicals)} canonical ({n - len(canonicals)} duplikatów)")
        return canonicals


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1.5 — CrossSourceCorroboration
# ─────────────────────────────────────────────────────────────────────────────

def _domain_from_url(url: str) -> str:
    """Extract normalised domain (netloc) from a URL string. Returns '' on failure."""
    try:
        parsed = urlparse(url.strip())
        domain = parsed.netloc.lower()
        # strip www.
        if domain.startswith("www."):
            domain = domain[4:]
        return domain or url.strip()
    except Exception:
        return url.strip()


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    """Simple token-level Jaccard similarity between two strings."""
    tokens_a = set(re.findall(r"[a-zA-Z0-9]+", text_a.lower()))
    tokens_b = set(re.findall(r"[a-zA-Z0-9]+", text_b.lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def _cluster_claims_tfidf(claim_texts: list[str], threshold: float = 0.35) -> list[list[int]]:
    """
    Group claim indices into semantic clusters via TF-IDF cosine similarity.
    Returns a list of clusters, each cluster being a list of row indices.
    Threshold is intentionally looser than dedup (0.35 vs 0.82) so that
    claims about the same *topic* but different phrasing land in the same cluster.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity as cos_sim
        import numpy as np

        vec = TfidfVectorizer(max_features=5000, ngram_range=(1, 2), sublinear_tf=True)
        mat = vec.fit_transform(claim_texts)
    except Exception:
        return _cluster_claims_jaccard(claim_texts, threshold=0.25)

    n = len(claim_texts)
    assigned = [False] * n
    clusters: list[list[int]] = []

    for i in range(n):
        if assigned[i]:
            continue
        sims = cos_sim(mat[i], mat).flatten()
        cluster = [j for j in range(i, n) if not assigned[j] and sims[j] >= threshold]
        clusters.append(cluster)
        for j in cluster:
            assigned[j] = True

    return clusters


def _cluster_claims_jaccard(claim_texts: list[str], threshold: float = 0.25) -> list[list[int]]:
    """Fallback: greedy Jaccard-based clustering."""
    n = len(claim_texts)
    assigned = [False] * n
    clusters: list[list[int]] = []

    for i in range(n):
        if assigned[i]:
            continue
        cluster = [i]
        assigned[i] = True
        for j in range(i + 1, n):
            if not assigned[j] and _jaccard_similarity(claim_texts[i], claim_texts[j]) >= threshold:
                cluster.append(j)
                assigned[j] = True
        clusters.append(cluster)

    return clusters


def cross_source_corroboration(con: Any, mission_id: str) -> dict:
    """
    Layer 1.5 — Cross-source corroboration.

    JOINs hive_claims with hive_content (via source_url match or doc_id when present)
    to get the best available source URL per claim.

    For each semantic cluster of claims:
      - Count distinct source domains contributing to the cluster.
      - 1 domain  → verified=FALSE, prepend 'UNCONFIRMED: ' to claim text.
      - ≥2 domains → verified=TRUE,  confidence = min(0.90, confidence * 1.2)
      - ≥3 domains → verified=TRUE,  confidence = min(0.95, confidence * 1.3)

    Updates hive_claims in the DB and returns stats dict.
    """
    log.info(f"  [Layer 1.5] Cross-source corroboration for mission {mission_id}")

    # Ensure 'verified' column exists — the spec defines it but it may be absent in older DBs
    try:
        con.execute("ALTER TABLE hive_claims ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT FALSE")
    except Exception as e:
        log.debug(f"  [Layer 1.5] verified column check: {e}")

    # Detect whether doc_id column exists on hive_claims
    cols = {row[0].lower() for row in con.execute("DESCRIBE hive_claims").fetchall()}
    has_doc_id = "doc_id" in cols

    if has_doc_id:
        # Full spec JOIN: hive_claims.doc_id → hive_content.id
        rows = con.execute("""
            SELECT
                hc.id,
                hc.claim,
                hc.confidence,
                COALESCE(hc.verified, FALSE) AS verified,
                hc.source_url,
                COALESCE(hcont.url, hc.source_url, '') AS content_url
            FROM hive_claims hc
            LEFT JOIN hive_content hcont
                ON CAST(hc.doc_id AS INTEGER) = hcont.id
            WHERE hc.mission_id = ?
              AND hc.claim_type != 'FALSIFICATION'
              AND hc.claim IS NOT NULL
              AND LENGTH(hc.claim) > 20
            ORDER BY hc.id
        """, [mission_id]).fetchall()
    else:
        # Fallback: JOIN on matching source_url → hive_content.url for domain enrichment
        rows = con.execute("""
            SELECT
                hc.id,
                hc.claim,
                hc.confidence,
                COALESCE(hc.verified, FALSE) AS verified,
                hc.source_url,
                COALESCE(hcont.url, hc.source_url, '') AS content_url
            FROM hive_claims hc
            LEFT JOIN hive_content hcont
                ON hc.source_url = hcont.url
               AND hcont.mission_id = hc.mission_id
            WHERE hc.mission_id = ?
              AND hc.claim_type != 'FALSIFICATION'
              AND hc.claim IS NOT NULL
              AND LENGTH(hc.claim) > 20
            ORDER BY hc.id
        """, [mission_id]).fetchall()

    if not rows:
        log.info("  [Layer 1.5] No claims found — skipping")
        return {"total_claims": 0, "single_source": 0, "multi_source": 0, "corroborated": 0}

    claim_ids    = [r[0] for r in rows]
    claim_texts  = [str(r[1]) for r in rows]
    confidences  = [float(r[2] or 0.5) for r in rows]
    verified     = [bool(r[3]) for r in rows]
    content_urls = [str(r[5] or r[4] or "") for r in rows]   # prefer hive_content.url

    # Cluster
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: F401
        clusters = _cluster_claims_tfidf(claim_texts, threshold=0.35)
        method = "tfidf"
    except ImportError:
        clusters = _cluster_claims_jaccard(claim_texts, threshold=0.25)
        method = "jaccard"

    log.info(f"  [Layer 1.5] {len(rows)} claims → {len(clusters)} semantic clusters (method={method})")

    stats = {"total_claims": len(rows), "single_source": 0, "multi_source": 0, "corroborated": 0}

    updates: list[tuple] = []   # (new_claim_text, new_confidence, new_verified, claim_id)

    for cluster_indices in clusters:
        # Collect distinct domains for this cluster
        domains = set()
        for idx in cluster_indices:
            d = _domain_from_url(content_urls[idx])
            if d:
                domains.add(d)
        source_count = len(domains)

        for idx in cluster_indices:
            orig_text  = claim_texts[idx]
            orig_conf  = confidences[idx]

            if source_count >= 3:
                new_conf   = min(0.95, orig_conf * 1.3)
                new_verif  = True
                new_text   = orig_text  # no prefix change
                stats["corroborated"] += 1
                stats["multi_source"] += 1
            elif source_count >= 2:
                new_conf   = min(0.90, orig_conf * 1.2)
                new_verif  = True
                new_text   = orig_text
                stats["corroborated"] += 1
                stats["multi_source"] += 1
            else:
                # single source (or no domain found)
                new_conf  = orig_conf
                new_verif = False
                # Do NOT mutate claim text — verified=FALSE already signals unconfirmed
                new_text = orig_text
                stats["single_source"] += 1

            updates.append((new_text, round(new_conf, 4), new_verif, claim_ids[idx]))

    # Batch update — update verified only when the column exists
    has_verified = "verified" in cols or True  # we just added it above
    for new_text, new_conf, new_verif, cid in updates:
        try:
            con.execute(
                "UPDATE hive_claims SET claim = ?, confidence = ?, verified = ? WHERE id = ?",
                [new_text, new_conf, new_verif, cid],
            )
        except Exception:
            # Fallback: update without verified column
            try:
                con.execute(
                    "UPDATE hive_claims SET claim = ?, confidence = ? WHERE id = ?",
                    [new_text, new_conf, cid],
                )
            except Exception as e2:
                log.debug(f"  [Layer 1.5] update error for claim {cid}: {e2}")

    log.info(
        f"  [Layer 1.5] Done — total={stats['total_claims']}, "
        f"single_source={stats['single_source']}, "
        f"multi_source={stats['multi_source']}, "
        f"corroborated={stats['corroborated']}"
    )
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — NumericalConflictDetector
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NumericalValue:
    raw: str
    value: float
    unit_raw: str
    unit_canon: str


@dataclass
class NumericalConflict:
    conflict_id: str
    mission_id: str
    entity_category: str
    year_context: str
    claim_a: CanonicalClaim
    value_a: NumericalValue
    claim_b: CanonicalClaim
    value_b: NumericalValue
    divergence_pct: float
    severity: str  # CRITICAL(>100%), HIGH(>50%), MEDIUM(>20%)


def _extract_numbers(text: str) -> list[NumericalValue]:
    """Extract all numerical values with units from text."""
    results = []
    for m in _NUM_RE.finditer(text):
        raw_num = m.group(1).replace(",", "").replace(" ", "").replace("\xa0", "")
        raw_unit = m.group(2).strip()
        try:
            value = float(raw_num)
        except ValueError:
            continue
        unit_canon = UNIT_CANON.get(raw_unit.lower().rstrip("."), raw_unit.upper())
        results.append(NumericalValue(
            raw=f"{raw_num} {raw_unit}",
            value=value,
            unit_raw=raw_unit,
            unit_canon=unit_canon,
        ))
    return results


def _extract_entity_categories(text: str) -> list[str]:
    """Identify what domain/topic the text is about."""
    cats = []
    text_lower = text.lower()
    for pattern, category in _ENTITY_TRIGGERS:
        if pattern.search(text_lower):
            cats.append(category)
    return cats or ["general"]


def _extract_years(text: str) -> list[str]:
    return _YEAR_RE.findall(text)


def _normalize_value_for_comparison(val: NumericalValue) -> Optional[float]:
    """Convert to common scale for comparison (e.g. M$ vs B$ → both in $M)."""
    if val.unit_canon in ("%", "pp", "x"):
        return val.value
    if val.unit_canon in ("M", "M$", "MUSD", "MEUR"):
        return val.value
    if val.unit_canon in ("B", "B$", "BUSD", "BEUR"):
        return val.value * 1000  # normalize to M
    if val.unit_canon in ("K",):
        return val.value / 1000  # normalize to M
    return val.value


def _divergence_pct(a: float, b: float) -> float:
    if a == 0 and b == 0:
        return 0.0
    base = max(abs(a), abs(b))
    if base == 0:
        return 0.0
    return abs(a - b) / base * 100.0


class NumericalConflictDetector:
    """
    Deterministyczny detektor sprzeczności numerycznych.
    Grupuje canonical claims po:
      - wspólnych kategoriach encji (gdp, trade, budget, ...)
      - wspólnych latach (2022, 2023, 2024)
      - tej samej jednostce (% vs %, M vs M)
    Flaguje pary z rozbieżnością >20%.
    """

    # Minimalna rozbieżność żeby to było interesujące
    MIN_DIVERGENCE_PCT = 20.0
    # Minimalna wartość żeby unikać 0.1% vs 0.2% szumu
    MIN_VALUE_THRESHOLD = 0.5

    def __init__(self, mission_id: str):
        self.mission_id = mission_id

    def detect(self, canonicals: list[CanonicalClaim]) -> list[NumericalConflict]:
        # Cap enriched claims to prevent O(n²) explosion (600 claims = 180K pairs)
        _max_enriched = int(os.environ.get("BEHIVE_FALSIFIER_MAX_ENRICHED", "200"))
        _max_conflicts = int(os.environ.get("BEHIVE_FALSIFIER_MAX_CONFLICTS", "100"))

        # Dla każdego canonical claim — wyciągnij liczby + kategorie + lata
        enriched: list[dict] = []
        for c in canonicals:
            nums = _extract_numbers(c.text)
            if not nums:
                continue
            cats = _extract_entity_categories(c.text)
            years = _extract_years(c.text)
            enriched.append({
                "claim": c,
                "numbers": nums,
                "categories": set(cats),
                "years": set(years) or {"unknown"},
            })
            if len(enriched) >= _max_enriched:
                log.info(f"  Enriched cap reached ({_max_enriched}), truncating")
                break

        if len(enriched) < 2:
            return []

        conflicts: list[NumericalConflict] = []
        seen_pairs: set[frozenset] = set()

        for i in range(len(enriched)):
            for j in range(i + 1, len(enriched)):
                a, b = enriched[i], enriched[j]

                # Muszą dzielić co najmniej jedną kategorię encji
                shared_cats = a["categories"] & b["categories"]
                if "general" in shared_cats and len(shared_cats) == 1:
                    continue
                if not shared_cats:
                    continue

                # Muszą dzielić co najmniej jeden rok (lub oba unknown)
                shared_years = a["years"] & b["years"]
                if not shared_years:
                    continue

                pair_key = frozenset([a["claim"].claim_id, b["claim"].claim_id])
                if pair_key in seen_pairs:
                    continue

                # Porównaj każdą parę liczb o tej samej jednostce
                for num_a in a["numbers"]:
                    for num_b in b["numbers"]:
                        if num_a.unit_canon != num_b.unit_canon:
                            continue
                        if num_a.value < self.MIN_VALUE_THRESHOLD or num_b.value < self.MIN_VALUE_THRESHOLD:
                            continue

                        norm_a = _normalize_value_for_comparison(num_a)
                        norm_b = _normalize_value_for_comparison(num_b)
                        if norm_a is None or norm_b is None:
                            continue

                        div = _divergence_pct(norm_a, norm_b)
                        if div < self.MIN_DIVERGENCE_PCT:
                            continue

                        # Określ severity
                        if div > 100:
                            severity = "CRITICAL"
                        elif div > 50:
                            severity = "HIGH"
                        else:
                            severity = "MEDIUM"

                        cat_label = sorted(shared_cats - {"general"})[0] if (shared_cats - {"general"}) else "general"
                        year_label = sorted(shared_years)[0] if shared_years != {"unknown"} else "unknown"

                        conflicts.append(NumericalConflict(
                            conflict_id=str(uuid.uuid4())[:8],
                            mission_id=self.mission_id,
                            entity_category=cat_label,
                            year_context=year_label,
                            claim_a=a["claim"],
                            value_a=num_a,
                            claim_b=b["claim"],
                            value_b=num_b,
                            divergence_pct=div,
                            severity=severity,
                        ))
                        seen_pairs.add(pair_key)
                        break  # jedna kolizja per para claimów wystarczy

                # Early exit — cap max conflicts to prevent combinatorial explosion
                if len(conflicts) >= _max_conflicts:
                    log.info(f"  Conflicts cap reached ({_max_conflicts}), stopping detection early")
                    break
            if len(conflicts) >= _max_conflicts:
                break

        # Sort by severity + divergence
        severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
        conflicts.sort(key=lambda c: (severity_order.get(c.severity, 3), -c.divergence_pct))

        log.info(
            f"  Konfliktów numerycznych: {len(conflicts)} "
            f"(CRITICAL={sum(1 for c in conflicts if c.severity=='CRITICAL')}, "
            f"HIGH={sum(1 for c in conflicts if c.severity=='HIGH')}, "
            f"MEDIUM={sum(1 for c in conflicts if c.severity=='MEDIUM')})"
        )
        return conflicts


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — ConflictResolver (LLM Haiku)
# ─────────────────────────────────────────────────────────────────────────────

class ConflictResolver:
    """
    LLM call TYLKO dla flagowanych konfliktów numerycznych.
    Zadanie: "Czy to realna sprzeczność czy różny kontekst/definicja/metodologia?"
    Batch: wszystkie konflikty jednej misji w jednym lub kilku callach.
    """

    BEDROCK_MODEL  = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    BEDROCK_REGION = "eu-central-1"
    BEDROCK_URL    = "http://127.0.0.1:8766/v1"
    MAX_TOKENS     = 1024
    BATCH_SIZE     = 8  # konfliktów per LLM call

    RESOLUTION_TYPES = {
        "REAL_CONFLICT":       "Realna sprzeczność — liczby odnoszą się do tego samego, różnią się",
        "DIFFERENT_CONTEXT":   "Różny kontekst — inne daty, definicje, zakresy geograficzne",
        "METHODOLOGICAL":      "Metodologiczny — różne metody pomiaru lub źródła definicji",
        "COMPLEMENTARY":       "Komplementarne — obie liczby mogą być prawdziwe jednocześnie",
        "NOISE":               "Szum — zbyt mała wartość informacyjna żeby rozstrzygać",
    }

    def __init__(self, mission_id: str):
        self.mission_id = mission_id
        self._client = self._init_client()

    def _init_client(self):
        # Try local SGLang proxy first (port 8766), fallback to Bedrock direct
        try:
            import urllib.request as _ur
            _ur.urlopen("http://127.0.0.1:8766/health", timeout=2)
            from openai import OpenAI
            return OpenAI(base_url=self.BEDROCK_URL, api_key="bedrock-local")
        except Exception:
            pass
        # Bedrock direct fallback via llm.complete()
        try:
            from behive.engine.llm import complete as _llm_complete
            class _LLMConflictClient:
                class _FakeCompletion:
                    def __init__(self, text):
                        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]
                def chat(self): return self
                def completions(self): return self
                def create(self, model, messages, max_tokens=512, **kw):
                    prompt = "\n\n".join(m["content"] for m in messages)
                    txt = _llm_complete(prompt, stage="process", max_tokens=max_tokens)
                    return _LLMConflictClient._FakeCompletion(txt)
            client = _LLMConflictClient()
            client.chat = type("Chat", (), {"completions": type("Comp", (), {"create": client.create})()})()
            log.info("ConflictResolver: using BYOK llm.complete()")
            return client
        except Exception as e:
            log.warning(f"ConflictResolver: brak klienta LLM: {e}")
            return None

    def _format_conflict_for_prompt(self, conflict: NumericalConflict, idx: int) -> str:
        return (
            f"CONFLICT #{idx} (severity={conflict.severity}, divergence={conflict.divergence_pct:.0f}%)\n"
            f"  Category: {conflict.entity_category} | Year: {conflict.year_context}\n"
            f"  Claim A [{conflict.value_a.value} {conflict.value_a.unit_canon}]: {conflict.claim_a.text[:300]}\n"
            f"  Claim B [{conflict.value_b.value} {conflict.value_b.unit_canon}]: {conflict.claim_b.text[:300]}\n"
        )

    def _resolve_batch(self, conflicts: list[NumericalConflict]) -> list[dict]:
        if not self._client:
            return [{"resolution": "UNRESOLVED", "notes": "no LLM client"} for _ in conflicts]

        conflicts_text = "\n".join(
            self._format_conflict_for_prompt(c, i + 1)
            for i, c in enumerate(conflicts)
        )

        prompt = f"""You are an intelligence analyst resolving numerical conflicts in OSINT data.

For each numbered CONFLICT below, determine:
1. Are the two numbers a REAL contradiction, or do they refer to different things?
2. What is the most likely explanation?

Resolution types:
- REAL_CONFLICT: Same entity, same time period, genuinely different numbers → real data contradiction
- DIFFERENT_CONTEXT: Different years, geographies, definitions, or scopes → not actually conflicting
- METHODOLOGICAL: Different measurement methods, data sources, or calculation bases
- COMPLEMENTARY: Both can be true simultaneously (e.g. two different metrics)
- NOISE: Trivial difference or insufficient context to determine

CONFLICTS TO ANALYZE:
{conflicts_text}

Return JSON array with one object per conflict (same order):
[
  {{
    "conflict_n": 1,
    "resolution": "REAL_CONFLICT|DIFFERENT_CONTEXT|METHODOLOGICAL|COMPLEMENTARY|NOISE",
    "confidence": 0.0-1.0,
    "notes": "one sentence explanation",
    "impact": "CRITICAL|HIGH|MEDIUM|LOW"
  }},
  ...
]
Start with [. No markdown."""

        import time as _t
        for _attempt in range(3):
            try:
                resp = self._client.chat.completions.create(
                    model=self.BEDROCK_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=self.MAX_TOKENS,
                )
                raw = resp.choices[0].message.content.strip()
                if raw.startswith("["):
                    return json.loads(raw)
                m = re.search(r"\[.*\]", raw, re.DOTALL)
                if m:
                    return json.loads(m.group())
            except Exception as e:
                log.warning(f"ConflictResolver LLM error (attempt {_attempt+1}/3): {e}")
                if _attempt < 2:
                    _t.sleep(3 * (_attempt + 1))

        return [{"resolution": "UNRESOLVED", "notes": "LLM parse error"} for _ in conflicts]

    def resolve(self, conflicts: list[NumericalConflict]) -> list[tuple[NumericalConflict, dict]]:
        """Resolve all conflicts in batches. Returns list of (conflict, resolution) tuples.
        
        Has internal time budget — if resolution takes too long, remaining
        conflicts are marked UNRESOLVED (graceful degradation, not failure).
        """
        if not conflicts:
            return []

        # Time budget for resolution phase (prevents depth-5 explosion)
        _time_budget = int(os.environ.get("BEHIVE_FALSIFIER_RESOLVE_TIMEOUT", "300"))
        _start = time.time()

        results: list[tuple[NumericalConflict, dict]] = []

        for batch_start in range(0, len(conflicts), self.BATCH_SIZE):
            # Check time budget
            elapsed = time.time() - _start
            if elapsed > _time_budget:
                log.warning(
                    f"  ⏱️ Resolution time budget exhausted ({_time_budget}s) — "
                    f"marking remaining {len(conflicts) - batch_start} conflicts as UNRESOLVED"
                )
                for conflict in conflicts[batch_start:]:
                    results.append((conflict, {"resolution": "UNRESOLVED", "notes": "timeout — time budget exhausted"}))
                break

            batch = conflicts[batch_start: batch_start + self.BATCH_SIZE]
            log.info(f"  Resolving batch {batch_start // self.BATCH_SIZE + 1}: {len(batch)} konfliktów")
            resolutions = self._resolve_batch(batch)

            for i, conflict in enumerate(batch):
                resolution = resolutions[i] if i < len(resolutions) else {"resolution": "UNRESOLVED"}
                results.append((conflict, resolution))

        real_conflicts = sum(
            1 for _, r in results if r.get("resolution") == "REAL_CONFLICT"
        )
        log.info(f"  ✅ Resolved: {len(results)} konfliktów, {real_conflicts} REAL_CONFLICT")
        return results


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

def _persist_conflicts(
    db: Any,
    resolved: list[tuple[NumericalConflict, dict]],
) -> int:
    """Save resolved conflicts to hive3_numerical_conflicts + hive3_falsification_evidence."""
    saved = 0

    for conflict, resolution in resolved:
        res_type = resolution.get("resolution", "UNRESOLVED")
        notes = resolution.get("notes", "")
        conf = float(resolution.get("confidence", 0.5))
        impact = resolution.get("impact", "MEDIUM")

        # hive3_numerical_conflicts
        try:
            db.execute("""
                INSERT INTO hive3_numerical_conflicts
                    (id, mission_id, entity_category, year_context,
                     claim_a_id, claim_a_text, value_a, unit_a,
                     claim_b_id, claim_b_text, value_b, unit_b,
                     divergence_pct, resolution, resolver_notes, resolved_at, created_at)
                VALUES (?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, [
                conflict.conflict_id, conflict.mission_id,
                conflict.entity_category, conflict.year_context,
                conflict.claim_a.claim_id, conflict.claim_a.text[:500],
                conflict.value_a.value, conflict.value_a.unit_canon,
                conflict.claim_b.claim_id, conflict.claim_b.text[:500],
                conflict.value_b.value, conflict.value_b.unit_canon,
                round(conflict.divergence_pct, 1), res_type, notes[:500],
            ])
        except Exception as e:
            log.debug(f"Persist numerical_conflicts error: {e}")

        # Only persist REAL_CONFLICTs to falsification_evidence
        if res_type != "REAL_CONFLICT":
            saved += 1
            continue

        try:
            db.execute("""
                INSERT INTO hive3_falsification_evidence
                    (id, mission_id, doc_url,
                     target_claim, contra_evidence,
                     evidence_quality, contradiction_type, impact,
                     confidence, overall_challenge_score, falsification_status, created_at)
                VALUES (?, ?, ?,
                        ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, CURRENT_TIMESTAMP)
            """, [
                str(uuid.uuid4())[:12],
                conflict.mission_id,
                conflict.claim_b.source_urls[0] if conflict.claim_b.source_urls else "",
                conflict.claim_a.text[:500],
                (
                    f"NUMERICAL CONFLICT ({conflict.divergence_pct:.0f}% divergence): "
                    f"Claim A says {conflict.value_a.value} {conflict.value_a.unit_canon}, "
                    f"Claim B says {conflict.value_b.value} {conflict.value_b.unit_canon}. "
                    f"Resolver: {notes}"
                )[:800],
                "STRONG" if conflict.divergence_pct > 50 else "MODERATE",
                "conflicting_numbers",
                impact,
                round(conf, 3),
                round(min(1.0, conflict.divergence_pct / 200.0), 3),
                "CONTRADICTIONS_FOUND",
            ])
        except Exception as e:
            log.debug(f"Persist falsification_evidence error: {e}")

        # Also save to hive_claims as FALSIFICATION type
        try:
            db.execute("""
                INSERT INTO hive_claims
                    (id, mission_id, claim, evidence, source_url,
                     claim_type, confidence, extracted_at)
                VALUES (nextval('hive_facts_seq'), ?, ?, ?, ?,
                        'FALSIFICATION', ?, CURRENT_TIMESTAMP)
            """, [
                conflict.mission_id,
                (
                    f"[NUMERICAL CONFLICT] {conflict.entity_category.upper()} {conflict.year_context}: "
                    f"{conflict.value_a.value} {conflict.value_a.unit_canon} vs "
                    f"{conflict.value_b.value} {conflict.value_b.unit_canon} "
                    f"({conflict.divergence_pct:.0f}% divergence)"
                )[:800],
                notes[:400],
                conflict.claim_a.source_urls[0] if conflict.claim_a.source_urls else "",
                round(conf, 3),
            ])
        except Exception as e:
            log.debug(f"Persist hive_claims FALSIFICATION error: {e}")

        saved += 1

    return saved


# ─────────────────────────────────────────────────────────────────────────────
# NumericalFalsificationPipeline — orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class NumericalFalsificationPipeline:
    """
    Full 4-layer pipeline per mission:
      Layer 1:   ClaimDeduplicator
      Layer 1.5: CrossSourceCorroboration (cross-source corroboration)
      Layer 2:   NumericalConflictDetector
      Layer 3:   ConflictResolver (LLM Haiku, batched)
    """

    def __init__(self, mission_id: str, resolve: bool = True, dry_run: bool = False):
        self.mission_id = mission_id
        self.resolve = resolve
        self.dry_run = dry_run

    def run(self) -> dict:
        t0 = time.time()
        db = _db_connect()
        _ensure_tables(db)

        topic = db.execute(
            "SELECT topic FROM hive_missions WHERE id = ?", [self.mission_id]
        ).fetchone()
        topic_str = topic[0] if topic else "unknown"

        log.info(f"🔴 NumericalFalsifier: misja {self.mission_id}")
        log.info(f"   Temat: {topic_str[:80]}")
        if self.dry_run:
            log.info("   [DRY-RUN mode — no DB writes]")

        # Layer 1 — deduplicate claims
        deduper = ClaimDeduplicator(self.mission_id)
        canonicals = deduper.deduplicate(db)

        if not canonicals:
            log.info("   Brak claims — pomijam")
            db.close()
            return {"mission_id": self.mission_id, "conflicts": 0, "real_conflicts": 0}

        # Layer 1.5 — cross-source corroboration
        if not self.dry_run:
            corroboration_stats = cross_source_corroboration(db, self.mission_id)
            db.commit()
        else:
            # In dry-run: run computation (for stats), then rollback writes.
            # PostgreSQL may silently auto-commit ALTER TABLE but UPDATEs inside
            # an explicit transaction can be rolled back.
            corroboration_stats = {"total_claims": 0, "single_source": 0, "multi_source": 0, "corroborated": 0}
            try:
                db.execute("BEGIN TRANSACTION")
                corroboration_stats = cross_source_corroboration(db, self.mission_id)
                db.execute("ROLLBACK")
                log.info("   [DRY-RUN] rolled back corroboration changes")
            except Exception as e_rb:
                log.debug(f"   [DRY-RUN] transaction note: {e_rb}")
                # If BEGIN failed (already in txn) just run and note we can't rollback
                if corroboration_stats["total_claims"] == 0:
                    corroboration_stats = cross_source_corroboration(db, self.mission_id)
                log.info("   [DRY-RUN] corroboration stats computed (no rollback available)")

        # Layer 2 — detect numerical conflicts
        detector = NumericalConflictDetector(self.mission_id)
        conflicts = detector.detect(canonicals)

        if not conflicts:
            log.info("   Brak konfliktów numerycznych")
            db.close()
            return {
                "mission_id": self.mission_id,
                "canonical_claims": len(canonicals),
                "corroboration": corroboration_stats,
                "conflicts": 0,
                "real_conflicts": 0,
                "elapsed": round(time.time() - t0, 1),
            }

        # Layer 3 — LLM resolution (jeśli włączone)
        if self.resolve and not self.dry_run:
            resolver = ConflictResolver(self.mission_id)
            resolved = resolver.resolve(conflicts)
        else:
            resolved = [(c, {"resolution": "UNRESOLVED", "notes": "resolve=False or dry-run"}) for c in conflicts]

        # Persist
        if not self.dry_run:
            saved = _persist_conflicts(db, resolved)
            db.commit()
        else:
            saved = 0
            log.info(f"   [DRY-RUN] would save {len(resolved)} conflict records")

        db.close()

        real_conflicts = sum(1 for _, r in resolved if r.get("resolution") == "REAL_CONFLICT")
        elapsed = round(time.time() - t0, 1)

        log.info(
            f"   ✅ Done in {elapsed}s — "
            f"{len(canonicals)} claims → {len(conflicts)} konflikty → "
            f"{real_conflicts} REAL_CONFLICT"
        )

        # Print real conflicts
        for conflict, resolution in resolved:
            if resolution.get("resolution") == "REAL_CONFLICT":
                log.info(
                    f"   🔴 REAL [{conflict.entity_category}/{conflict.year_context}] "
                    f"{conflict.value_a.value}{conflict.value_a.unit_canon} vs "
                    f"{conflict.value_b.value}{conflict.value_b.unit_canon} "
                    f"({conflict.divergence_pct:.0f}%): {resolution.get('notes', '')[:80]}"
                )

        return {
            "mission_id": self.mission_id,
            "canonical_claims": len(canonicals),
            "corroboration": corroboration_stats,
            "conflicts_detected": len(conflicts),
            "real_conflicts": real_conflicts,
            "saved": saved,
            "elapsed": elapsed,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Public API — hook dla hive2_synth.py + hive2_process.py
# ─────────────────────────────────────────────────────────────────────────────

def run_falsification_for_mission(mission_id: str, resolve: bool = True, dry_run: bool = False) -> dict:
    """
    Hook wywoływany z hive2_synth.py po zakończeniu syntezy.
    resolve=True → LLM resolution layer; False → tylko detekcja (tańsze)
    dry_run=True → run full pipeline but rollback all DB writes
    """
    try:
        pipeline = NumericalFalsificationPipeline(mission_id, resolve=resolve, dry_run=dry_run)
        return pipeline.run()
    except Exception as e:
        log.warning(f"NumericalFalsificationPipeline error dla {mission_id}: {e}")
        return {"mission_id": mission_id, "error": str(e)}


def get_falsification_report(mission_id: str) -> str:
    """
    Generuje tekstowy raport sprzeczności numerycznych dla podanej misji.
    Używany przez hive2_synth._build_honey_context() jako dodatkowa sekcja kontekstu.
    """
    try:
        db = _db_connect(read_only=True)
        rows = db.execute("""
            SELECT entity_category, year_context, value_a, unit_a, value_b, unit_b,
                   divergence_pct, resolution, resolver_notes, claim_a_text, claim_b_text
            FROM hive3_numerical_conflicts
            WHERE mission_id = ?
              AND resolution IN ('REAL_CONFLICT', 'UNRESOLVED')
            ORDER BY divergence_pct DESC
            LIMIT 15
        """, [mission_id]).fetchall()
        db.close()

        if not rows:
            return ""

        lines = ["=== ZIDENTYFIKOWANE SPRZECZNOŚCI NUMERYCZNE ==="]
        for r in rows:
            cat, year, va, ua, vb, ub, div, res, notes, ta, tb = r
            lines.append(
                f"⚡ [{cat}/{year}] {va}{ua} vs {vb}{ub} ({div:.0f}% rozbieżność) [{res}]"
            )
            if notes:
                lines.append(f"   → {notes}")
            lines.append(f"   Claim A: {str(ta)[:120]}")
            lines.append(f"   Claim B: {str(tb)[:120]}")
        lines.append("")
        return "\n".join(lines)
    except Exception as e:
        log.debug(f"get_falsification_report error: {e}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="HIVE Numerical Falsification Engine")
    # Positional mission_id (optional) — allows: python3 hive2_falsifier.py <mission_id>
    parser.add_argument("mission_id_pos", nargs="?", help="Mission ID (positional shortcut)")
    parser.add_argument("--mission", help="Run for specific mission_id")
    parser.add_argument("--all", action="store_true", help="Run for all missions")
    parser.add_argument("--report", action="store_true", help="Show conflicts without LLM resolve")
    parser.add_argument("--no-resolve", action="store_true", help="Skip LLM resolution layer")
    parser.add_argument("--dry-run", action="store_true", help="Run pipeline but rollback all DB writes")
    args = parser.parse_args()

    resolve  = not (args.report or args.no_resolve)
    dry_run  = args.dry_run

    # Resolve mission_id: positional arg takes precedence, then --mission flag
    mission_id = args.mission_id_pos or args.mission

    if mission_id:
        result = run_falsification_for_mission(mission_id, resolve=resolve, dry_run=dry_run)
        print(f"\n✅ {result}")
        if args.report:
            print(get_falsification_report(mission_id))
        return

    if args.all:
        db = _db_connect(read_only=True)
        missions = db.execute(
            "SELECT id, topic FROM hive_missions WHERE status IN ('done','synth') ORDER BY created_at"
        ).fetchall()
        db.close()

        total_conflicts = 0
        total_real = 0
        t0 = time.time()

        for mid, topic in missions:
            result = run_falsification_for_mission(str(mid), resolve=resolve, dry_run=dry_run)
            total_conflicts += result.get("conflicts_detected", 0)
            total_real += result.get("real_conflicts", 0)

        elapsed = round(time.time() - t0, 1)
        print(f"\n✅ Gotowe: {len(missions)} misji, {total_conflicts} konfliktów, "
              f"{total_real} REAL_CONFLICT, {elapsed}s")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
