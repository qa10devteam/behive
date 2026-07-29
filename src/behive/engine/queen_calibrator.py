"""
QueenCalibrator — HIVE Auto-Calibration Module
================================================
Analyzes historical HIVE mission data (DuckDB) i generuje wektory
kalibracyjne dla Queen: które źródła, wzorce i strategie działają
najlepiej per topic_domain.

Autor: NEXUS AI Data Remediation Engineer
Wersja: 1.0.0  (Batch 3 — pełna implementacja)
DB: configured via BEHIVE_DB_PATH env var (read_only)
"""

from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import hive2_db as _hive_db  # PostgreSQL via unified layer

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("BEHIVE_LEGACY_DB", "")  # Legacy — PostgreSQL is primary
LOG_FMT = "%(asctime)s [QueenCalibrator] %(levelname)s — %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FMT)
log = logging.getLogger("QueenCalibrator")

# Mapping keywords in topic → topic_domain
TOPIC_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "regulatory_analysis": [
        "sanctions", "bypass", "dual-use", "eu regulations", "regulatory",
        "przepisy", "regulacje", "prawo",
    ],
    "geopolitical_intel": [
        "geopolitical", "intelligence", "oligarch", "money laundering",
        "evasion", "smuggling", "war", "military", "disinformation",
        "influence operations", "dezinformacja",
    ],
    "market_research": [
        "market", "rynek", "przychody", "revenue", "industry", "sector",
        "zamówienia", "outsourcing", "e-commerce", "b2b", "saas",
    ],
    "social_media_marketing": [
        "social media", "linkedin", "tiktok", "cpl", "cpm", "roi",
        "marketing", "b2b saas", "influencer",
    ],
    "corporate_intel": [
        "company", "corporation", "krs", "spółka", "holding", "reputacja",
        "kontrakty", "dostawcy", "grupa kapitałowa",
    ],
    "scientific_research": [
        "nanotechnology", "swarm", "quantum", "bio-inspired", "collective",
        "algorithm", "research", "academic", "stigmergy",
    ],
    "macroeconomics": [
        "inflation", "inflacja", "pkb", "gdp", "unemployment", "bezrobocie",
        "macroeconomics", "makroekonomia",
    ],
    "cybersecurity": [
        "cyber", "security", "hack", "bezpiecze", "malware", "phishing",
        "threat",
    ],
    "energy": [
        "energy", "energetyka", "wind", "solar", "renewable", "odnawialna",
        "farmy wiatrowe",
    ],
}

# ---------------------------------------------------------------------------
# DATA CLASSES
# ---------------------------------------------------------------------------

@dataclass
class SourceProfile:
    source_type: str
    avg_score: float
    avg_authority: float
    count: int
    recommendation: str = ""


@dataclass
class OperationProfile:
    operation_name: str
    tier: int
    avg_confidence: float
    avg_relevance: float
    avg_ms: float
    count: int


@dataclass
class CalibrationProfile:
    """Profil kalibracyjny dla danej domeny tematycznej."""
    topic_domain: str
    generated_at: datetime = field(default_factory=datetime.utcnow)

    # Sources
    source_profiles: list[SourceProfile] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    cloudflare_domains: list[str] = field(default_factory=list)

    # Operacje
    best_operations: list[OperationProfile] = field(default_factory=list)

    # Priors i fakty bazowe
    priors: list[dict] = field(default_factory=list)
    dead_hypotheses: list[dict] = field(default_factory=list)

    # Lekcje
    lessons: list[dict] = field(default_factory=list)

    # Tier tuning
    tier_weights: list[dict] = field(default_factory=list)

    # Tools
    tool_weights: dict[str, float] = field(default_factory=dict)

    # Queen accuracy (if available)
    prediction_accuracy: Optional[float] = None
    calibration_n: int = 0

    # Wzorce z pattern_library
    evasion_patterns: list[dict] = field(default_factory=list)

    # Statystyki misji
    mission_stats: dict = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"CalibrationProfile(domain={self.topic_domain}, generated={self.generated_at.isoformat()})",
            f"  sources: {len(self.source_profiles)} types | blocked: {len(self.blocked_domains)} | cf: {len(self.cloudflare_domains)}",
            f"  operations: {len(self.best_operations)} profiled",
            f"  priors: {len(self.priors)} | dead: {len(self.dead_hypotheses)} | lessons: {len(self.lessons)}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# QUEEN CALIBRATOR
# ---------------------------------------------------------------------------

class QueenCalibrator:
    """
    Analyzes historical HIVE mission data i generuje instrukcje kalibracyjne
    dla modelu Queen.

    Użycie:
        calibrator = QueenCalibrator()
        profile = calibrator.analyze_historical_performance("regulatory_analysis")
        block = calibrator.generate_calibration_block("regulatory_analysis")
        # → inject `block` into Queen prompt before generating tasks
    """

    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"HIVE DuckDB nie znaleziono: {self.db_path}")
        log.info("QueenCalibrator init — DB: %s", self.db_path)

    # ------------------------------------------------------------------
    # INTERNAL: connection helper
    # ------------------------------------------------------------------
    def _conn(self) -> Any:
        return __import__("hive2_db").connect(read_only=True)

    # ------------------------------------------------------------------
    # HELPER: infer domain from topic string
    # ------------------------------------------------------------------
    @staticmethod
    def infer_domain(topic: str) -> str:
        """Mapuje surowy temat misji na topic_domain."""
        topic_lower = topic.lower()
        for domain, keywords in TOPIC_DOMAIN_KEYWORDS.items():
            if any(kw in topic_lower for kw in keywords):
                return domain
        return "general"

    # ------------------------------------------------------------------
    # MAIN: analyze_historical_performance
    # ------------------------------------------------------------------
    def analyze_historical_performance(
        self, topic: str
    ) -> CalibrationProfile:
        """
        Analizuje historyczne dane HIVE dla podanego tematu/domeny.

        Parametry
        ---------
        topic : str
            Temat misji lub nazwa domeny (np. 'regulatory_analysis',
            'Turkish sanctions bypass').

        Zwraca
        ------
        CalibrationProfile
            Pełny profil kalibracyjny dla domeny.
        """
        domain = (
            topic
            if topic in TOPIC_DOMAIN_KEYWORDS
            else self.infer_domain(topic)
        )
        log.info("analyze_historical_performance: topic=%r → domain=%s", topic, domain)

        profile = CalibrationProfile(topic_domain=domain)

        with self._conn() as conn:
            # 1. Sources — per source_type and domain
            profile.source_profiles = self._analyze_sources(conn, domain)

            # 2. Zablokowane domeny (drone_map)
            profile.blocked_domains, profile.cloudflare_domains = (
                self._analyze_drone_map(conn)
            )

            # 3. Bee operations (bee_results)
            profile.best_operations = self._analyze_operations(conn, domain)

            # 4. Priors (znane fakty bazowe)
            profile.priors = self._load_priors(conn, domain)

            # 5. Dead hypotheses (graveyard)
            profile.dead_hypotheses = self._load_graveyard(conn, domain)

            # 6. Lekcje (lessons)
            profile.lessons = self._load_lessons(conn, domain)

            # 7. Tier tuning (activation weights)
            profile.tier_weights = self._load_tier_tuning(conn, domain)

            # 8. Tool weights (ecosystem state)
            profile.tool_weights = self._load_tool_weights(conn)

            # 9. Queen prediction accuracy (calibration_log)
            profile.prediction_accuracy, profile.calibration_n = (
                self._calc_prediction_accuracy(conn)
            )

            # 10. Pattern library (evasion patterns etc.)
            profile.evasion_patterns = self._load_patterns(conn, domain)

            # 11. Statystyki misji (mission_stats)
            profile.mission_stats = self._load_mission_stats(conn, domain)

        log.info("Profile ready: %s", profile.summary())
        return profile

    # ------------------------------------------------------------------
    # MAIN: generate_calibration_block
    # ------------------------------------------------------------------
    def generate_calibration_block(self, topic: str) -> str:
        """
        Generuje blok tekstowy do wstrzyknięcia do promptu Queen.

        Parametry
        ---------
        topic : str
            Temat misji.

        Zwraca
        ------
        str
            Wieloliniowy tekst kalibracyjny w formacie Queen-readable.
        """
        profile = self.analyze_historical_performance(topic)
        return self._render_calibration_block(profile)

    # ------------------------------------------------------------------
    # ANALYSIS HELPERS
    # ------------------------------------------------------------------

    def _analyze_sources(
        self, conn: Any, domain: str
    ) -> list[SourceProfile]:
        """Pobiera profil jakości źródeł per source_type dla domeny."""
        # Map domain → keywords for mission filtering
        keywords = TOPIC_DOMAIN_KEYWORDS.get(domain, [])
        kw_filter = " OR ".join(
            f"LOWER(m.topic) LIKE '%{kw}%'" for kw in keywords
        ) if keywords else "1=1"

        query = f"""
            SELECT
                s.source_type,
                COUNT(*) AS cnt,
                ROUND(AVG(s.score_total)::numeric, 2)     AS avg_score,
                ROUND(AVG(s.score_authority)::numeric, 2) AS avg_authority,
                ROUND(AVG(s.score_relevance)::numeric, 2) AS avg_relevance
            FROM hive_sources s
            JOIN hive_missions m ON s.mission_id = m.id
            WHERE s.status IN ('scored', 'done')
              AND ({kw_filter})
            GROUP BY s.source_type
            ORDER BY avg_score DESC
        """
        try:
            rows = conn.execute(query).fetchall()
        except Exception as e:
            log.warning("_analyze_sources query failed: %s — falling back to global", e)
            rows = conn.execute("""
                SELECT source_type, COUNT(*), ROUND(AVG(score_total)::numeric,2),
                       ROUND(AVG(score_authority)::numeric,2), ROUND(AVG(score_relevance)::numeric,2)
                FROM hive_sources WHERE status IN ('scored','done')
                GROUP BY source_type ORDER BY 3 DESC
            """).fetchall()

        results = []
        for source_type, cnt, avg_score, avg_auth, avg_rel in rows:
            rec = _source_recommendation(source_type, avg_score)
            results.append(SourceProfile(
                source_type=source_type,
                avg_score=avg_score or 0.0,
                avg_authority=avg_auth or 0.0,
                count=cnt,
                recommendation=rec,
            ))
        return results

    def _analyze_drone_map(
        self, conn: Any
    ) -> tuple[list[str], list[str]]:
        """Zwraca (blocked_domains, cloudflare_domains)."""
        try:
            rows = conn.execute("""
                SELECT domain, block_type, strategy
                FROM drone_map
                ORDER BY domain
            """).fetchall()
            blocked = [r[0] for r in rows if r[2] == "SKIP"]
            cloudflare = [r[0] for r in rows if r[1] == "cloudflare"]
            return blocked, cloudflare
        except Exception:
            return [], []  # drone_map nie istnieje lub pusta — ignoruj

    def _analyze_operations(
        self, conn: Any, domain: str
    ) -> list[OperationProfile]:
        """Top operacje pszczół wg avg_confidence i relevance."""
        keywords = TOPIC_DOMAIN_KEYWORDS.get(domain, [])
        kw_filter = " OR ".join(
            f"LOWER(m.topic) LIKE '%{kw}%'" for kw in keywords
        ) if keywords else "1=1"

        query = f"""
            SELECT
                br.operation_name,
                br.tier,
                COUNT(*) AS cnt,
                ROUND(AVG(br.confidence)::numeric, 4)       AS avg_conf,
                ROUND(AVG(br.relevance_score)::numeric, 4)  AS avg_rel,
                ROUND(AVG(br.processing_ms)::numeric, 0)    AS avg_ms
            FROM hive_bee_results br
            JOIN hive_missions m ON br.mission_id = m.id
            WHERE ({kw_filter})
            GROUP BY br.operation_name, br.tier
            ORDER BY avg_conf DESC, avg_rel DESC
            LIMIT 15
        """
        try:
            rows = conn.execute(query).fetchall()
        except Exception as e:
            log.warning("_analyze_operations domain query failed: %s — global fallback", e)
            rows = conn.execute("""
                SELECT operation_name, tier, COUNT(*),
                       ROUND(AVG(confidence)::numeric,4), ROUND(AVG(relevance_score)::numeric,4),
                       ROUND(AVG(processing_ms)::numeric,0)
                FROM hive_bee_results
                GROUP BY operation_name, tier ORDER BY 4 DESC LIMIT 15
            """).fetchall()

        results = []
        for op_name, tier, cnt, avg_c, avg_r, avg_ms in rows:
            results.append(OperationProfile(
                operation_name=op_name,
                tier=tier or 0,
                avg_confidence=avg_c or 0.0,
                avg_relevance=avg_r or 0.0,
                avg_ms=avg_ms or 0.0,
                count=cnt,
            ))
        return results

    def _load_priors(
        self, conn: Any, domain: str
    ) -> list[dict]:
        """Wczytuje zweryfikowane fakty bazowe (priors)."""
        rows = conn.execute("""
            SELECT fact, domain, confidence, times_confirmed, times_contradicted
            FROM hive3_priors
            WHERE times_contradicted = 0
            ORDER BY times_confirmed DESC, confidence DESC
        """).fetchall()
        # Filter per domain if match, else return all
        matched = [
            {"fact": r[0], "domain": r[1], "confidence": r[2],
             "confirmed": r[3], "contradicted": r[4]}
            for r in rows
            if r[1] == domain or domain == "general"
        ]
        return matched if matched else [
            {"fact": r[0], "domain": r[1], "confidence": r[2],
             "confirmed": r[3], "contradicted": r[4]}
            for r in rows
        ]

    def _load_graveyard(
        self, conn: Any, domain: str
    ) -> list[dict]:
        """Hipotezy odrzucone — czego NIE zakładać."""
        rows = conn.execute("""
            SELECT hypothesis, topic_domain, failure_reason, confidence_at_death
            FROM hive3_graveyard
            ORDER BY confidence_at_death ASC
        """).fetchall()
        return [
            {"hypothesis": r[0], "domain": r[1],
             "failure_reason": r[2], "conf_at_death": r[3]}
            for r in rows
        ]

    def _load_lessons(
        self, conn: Any, domain: str
    ) -> list[dict]:
        """Lekcje z przeszłych misji."""
        rows = conn.execute("""
            SELECT lesson_type, lesson, recommendation, applies_to, mission_id
            FROM hive3_lessons
            ORDER BY created_at DESC
        """).fetchall()
        return [
            {"type": r[0], "lesson": r[1], "recommendation": r[2],
             "applies_to": r[3], "mission_id": r[4]}
            for r in rows
        ]

    def _load_tier_tuning(
        self, conn: Any, domain: str
    ) -> list[dict]:
        """Tuning wag tierów per domain."""
        rows = conn.execute("""
            SELECT tier, topic_domain, activation_weight, avg_confidence, mission_count
            FROM hive3_tier_tuning
            ORDER BY avg_confidence DESC
        """).fetchall()
        matched = [
            {"tier": r[0], "domain": r[1], "weight": r[2],
             "avg_conf": r[3], "missions": r[4]}
            for r in rows
            if r[1] == domain or r[1] == "general"
        ]
        return matched if matched else [
            {"tier": r[0], "domain": r[1], "weight": r[2],
             "avg_conf": r[3], "missions": r[4]}
            for r in rows
        ]

    def _load_tool_weights(
        self, conn: Any
    ) -> dict[str, float]:
        """Wagi narzędzi z ecosystem_state."""
        row = conn.execute(
            "SELECT value FROM hive3_ecosystem_state WHERE key='tool_weights'"
        ).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                pass
        return {}

    def _calc_prediction_accuracy(
        self, conn: Any
    ) -> tuple[Optional[float], int]:
        """Accuracy predykcji Queen (calibration_log)."""
        row = conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN outcome='correct' THEN 1 ELSE 0 END) AS correct
            FROM hive3_calibration_log
            WHERE outcome IS NOT NULL
        """).fetchone()
        if row and row[0] > 0:
            accuracy = round(row[1] / row[0], 4)
            return accuracy, row[0]
        return None, 0

    def _load_patterns(
        self, conn: Any, domain: str
    ) -> list[dict]:
        """Wzorce z hive3_pattern_library."""
        rows = conn.execute("""
            SELECT pattern_name, pattern_type, description,
                   confidence, observation_count
            FROM hive3_pattern_library
            ORDER BY confidence DESC, observation_count DESC
        """).fetchall()
        return [
            {"name": r[0], "type": r[1], "description": r[2],
             "confidence": r[3], "observations": r[4]}
            for r in rows
        ]

    def _load_mission_stats(
        self, conn: Any, domain: str
    ) -> dict:
        """Zbiorcze statystyki misji — timing, yield, coverage."""
        keywords = TOPIC_DOMAIN_KEYWORDS.get(domain, [])
        kw_filter = " OR ".join(
            f"LOWER(topic) LIKE '%{kw}%'" for kw in keywords
        ) if keywords else "1=1"

        try:
            row = conn.execute(f"""
                SELECT
                    COUNT(*)                                AS total_missions,
                    ROUND(AVG(sources_found)::numeric, 1)           AS avg_sources_found,
                    ROUND(AVG(sources_harvested)::numeric, 1)       AS avg_sources_harvested,
                    ROUND(AVG(total_words)::numeric, 0)             AS avg_words,
                    ROUND(AVG(total_entities)::numeric, 1)          AS avg_entities,
                    ROUND(AVG(total_facts)::numeric, 1)             AS avg_facts,
                    ROUND(AVG(CASE WHEN sources_found > 0
                        THEN CAST(sources_harvested AS FLOAT)/sources_found
                        ELSE NULL END), 3)                 AS harvest_ratio
                FROM hive_missions
                WHERE ({kw_filter}) AND sources_found > 0
            """).fetchone()
        except Exception as e:
            log.warning("mission_stats query failed: %s", e)
            return {}

        if row:
            return {
                "total_missions": row[0],
                "avg_sources_found": row[1],
                "avg_sources_harvested": row[2],
                "avg_words": row[3],
                "avg_entities": row[4],
                "avg_facts": row[5],
                "harvest_ratio": row[6],
            }
        return {}

    # ------------------------------------------------------------------
    # RENDERER
    # ------------------------------------------------------------------

    def _render_calibration_block(self, p: CalibrationProfile) -> str:
        """Renderuje profil kalibracyjny do tekstu promptowego."""
        lines = [
            "╔══════════════════════════════════════════════════════════════╗",
            f"║  QUEEN CALIBRATION BLOCK — domain: {p.topic_domain:<25}║",
            f"║  Generated: {p.generated_at.strftime('%Y-%m-%d %H:%M UTC'):<49}║",
            "╚══════════════════════════════════════════════════════════════╝",
            "",
        ]

        # --- 1. SOURCE STRATEGY ---
        lines.append("## SOURCE STRATEGY")
        if p.source_profiles:
            lines.append("Priority order (avg score / authority):")
            for sp in p.source_profiles[:6]:
                flag = "✅ PREFER" if sp.avg_score >= 40 else ("⚠️ USE" if sp.avg_score >= 25 else "❌ AVOID")
                lines.append(
                    f"  {flag} {sp.source_type:<20} avg_score={sp.avg_score:.1f} "
                    f"authority={sp.avg_authority:.1f} n={sp.count}"
                )
                if sp.recommendation:
                    lines.append(f"    → {sp.recommendation}")
        else:
            lines.append("  (brak danych per-domain — używaj ogólnych wag)")

        lines.append("")

        # --- 2. BLOCKED / CLOUDFLARE DOMAINS ---
        if p.blocked_domains:
            lines.append("## ACCESS CONSTRAINTS")
            lines.append("SKIP (blocked, no bypass):")
            for d in p.blocked_domains:
                lines.append(f"  ✗ {d}")

        if p.cloudflare_domains:
            lines.append("USE curl_cffi/chrome131 (Cloudflare):")
            for d in p.cloudflare_domains:
                lines.append(f"  ⚡ {d}")
        lines.append("")

        # --- 3. BEST OPERATIONS ---
        lines.append("## RECOMMENDED BEE OPERATIONS (by confidence)")
        if p.best_operations:
            for op in p.best_operations[:8]:
                lines.append(
                    f"  tier={op.tier:>2} conf={op.avg_confidence:.3f} "
                    f"rel={op.avg_relevance:.3f} ms≈{op.avg_ms/1000:.1f}s  "
                    f"[{op.operation_name}]"
                )
        else:
            lines.append("  (brak danych — użyj domyślnej kolejności tierów)")
        lines.append("")

        # --- 4. TOOL WEIGHTS ---
        lines.append("## TOOL WEIGHTS (current evolution)")
        if p.tool_weights:
            sorted_tools = sorted(p.tool_weights.items(), key=lambda x: -x[1])
            for tool, weight in sorted_tools[:8]:
                bar = "█" * int(weight * 10)
                lines.append(f"  {tool:<28} {weight:.4f}  {bar}")
        else:
            lines.append("  (brak danych — używaj równych wag)")
        lines.append("")

        # --- 5. TIER WEIGHTS ---
        lines.append("## TIER ACTIVATION WEIGHTS")
        if p.tier_weights:
            for tw in p.tier_weights:
                lines.append(
                    f"  tier={tw['tier']:<10} domain={tw['domain']:<20} "
                    f"weight={tw['weight']:.3f}  avg_conf={tw['avg_conf']:.3f}"
                )
        else:
            lines.append("  (brak per-domain — tier weights = 1.0)")
        lines.append("")

        # --- 6. PRIORS ---
        if p.priors:
            lines.append("## VERIFIED PRIORS (pre-seeded knowledge)")
            for pr in p.priors[:5]:
                lines.append(
                    f"  [{pr['domain']}|conf={pr['confidence']:.2f}|"
                    f"confirmed={pr['confirmed']}] {pr['fact']}"
                )
            lines.append("")

        # --- 7. DEAD HYPOTHESES ---
        if p.dead_hypotheses:
            lines.append("## DEAD HYPOTHESES ⚠️ (do NOT pursue)")
            for dh in p.dead_hypotheses:
                lines.append(
                    f"  ✗ [{dh['domain']}] \"{dh['hypothesis']}\""
                )
                if dh["failure_reason"]:
                    lines.append(f"    reason: {dh['failure_reason']}")
            lines.append("")

        # --- 8. LESSONS ---
        if p.lessons:
            lines.append("## LESSONS FROM PAST MISSIONS")
            for ls in p.lessons[:5]:
                lines.append(f"  [{ls['type']}] {ls['lesson']}")
                if ls.get("recommendation"):
                    lines.append(f"    → Rec: {ls['recommendation']}")
            lines.append("")

        # --- 9. QUEEN ACCURACY ---
        lines.append("## QUEEN PREDICTION ACCURACY")
        if p.prediction_accuracy is not None:
            lines.append(
                f"  Accuracy: {p.prediction_accuracy*100:.1f}% "
                f"(n={p.calibration_n} resolved predictions)"
            )
        else:
            lines.append(
                "  Accuracy: N/A — brak rozwiązanych predykcji. "
                "Queen powinna używać konserwatywnego prior confidence=0.65."
            )
        lines.append("")

        # --- 10. PATTERN LIBRARY ---
        if p.evasion_patterns:
            lines.append("## KNOWN PATTERNS (pattern_library)")
            for pat in p.evasion_patterns[:5]:
                lines.append(
                    f"  [{pat['type']}|conf={pat['confidence']:.2f}|"
                    f"obs={pat['observations']}] {pat['name']}: {pat['description']}"
                )
            lines.append("")

        # --- 11. MISSION STATS ---
        if p.mission_stats and p.mission_stats.get("total_missions", 0) > 0:
            ms = p.mission_stats
            lines.append("## MISSION PERFORMANCE BASELINE")
            lines.append(f"  Missions analyzed: {ms.get('total_missions', 0)}")
            lines.append(f"  Avg sources found/harvested: {ms.get('avg_sources_found', 0):.0f} / {ms.get('avg_sources_harvested', 0):.0f}")
            lines.append(f"  Harvest ratio: {(ms.get('harvest_ratio') or 0)*100:.1f}%")
            lines.append(f"  Avg words harvested: {ms.get('avg_words', 0):,.0f}")
            lines.append("")

        lines.append("╔══════════════════════════════════════════════════════════════╗")
        lines.append("║  END CALIBRATION BLOCK                                       ║")
        lines.append("╚══════════════════════════════════════════════════════════════╝")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# UTILITY
# ---------------------------------------------------------------------------

def _source_recommendation(source_type: str, avg_score: float) -> str:
    """Generuje krótką rekomendację tekstową dla danego source_type."""
    recs = {
        "file_pdf": "Dokumenty PDF zawierają strukturalne dane — najwyższy priorytet",
        "gov_registry": "Rejestry rządowe: authority~95 — idealne dla danych faktycznych",
        "eu_source": "Sources EU: authority~95, dobre dla regulacji i statystyk",
        "search": "Wyszukiwarka: dobry balans relevance/authority przy scored",
        "news": "Newsy: uwaga na świeżość, sprawdź datę publikacji",
        "academic": "Akademickie: niska authority (paywall), ale wysokie relevance gdy dostępne",
        "document": "Dokumenty web: zróżnicowane — weryfikuj domain",
        "primary_intel": "Rekonesans bezpośredni: niska authority, wysoka unikalność",
        "ddg": "DuckDuckGo scouting: dobry pokrycie, ale wymaga filtrowania",
        "rss": "RSS: świeże, ale niska gęstość informacji",
        "government": "Rządowe: niskie score (często HTML nawigacja) — preferuj file_pdf",
        "reddit": "Reddit: niskie authority — tylko do analizy sentymentu",
    }
    default_rec = (
        "PREFERUJ" if avg_score >= 45
        else "UŻYWAJ OSTROŻNIE" if avg_score >= 25
        else "UNIKAJ — niski score"
    )
    return recs.get(source_type, default_rec)


# ---------------------------------------------------------------------------
# CLI DEMO
# ---------------------------------------------------------------------------

def _demo():
    """Demonstracja działania QueenCalibrator — uruchamiana jako skrypt."""
    log.debug("=" * 70)
    log.debug("  QueenCalibrator DEMO — HIVE NEXUS")
    log.debug("=" * 70)

    calibrator = QueenCalibrator()

    demo_topics = [
        "Turkish intermediaries bypassing EU sanctions on Russian dual-use electronics",
        "UAE Dubai free zones sanctions evasion money laundering",
        "Chinese disinformation influence operations European social media",
        "regulatory_analysis",
        "market_research",
    ]

    for topic in demo_topics:
        log.debug(f"\n{'─'*70}")
        log.debug(f"  TOPIC: {topic}")
        log.debug("─" * 70)
        block = calibrator.generate_calibration_block(topic)
        log.debug(block)
        log.debug("")


if __name__ == "__main__":
    _demo()
