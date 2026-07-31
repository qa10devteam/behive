#!/usr/bin/env python3
"""
HIVE 2.0 — hive2_queen_feedback.py
Queen Feedback Loop — Prediction Verification & Calibration Closure

Moduł zamykania pętli epistemicznej HIVE:
  1. PredictionExtractor  — wyciąga predykcje z syntezy Queen (LLM)
  2. FeedbackValidator    — weryfikuje stare predykcje przez nowe evidence
  3. LessonsExtractor     — wyciąga lekcje post-mission (co działało / nie)
  4. PriorManager         — zarządza bazą faktów cross-mission
  5. IntelligenceClosure  — master sekwencja zamknięcia misji

Autor: Intelligence Feedback Engineer — HIVE Calibration Module
Model: eu.anthropic.claude-haiku-4-5-20251001-v1:0 (Bedrock eu-central-1)
DB:    PostgreSQL (BEHIVE_DB_URL or HIVE_PG_* vars)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from behive.engine.db import connect as _db_connect

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
DB_PATH = ""  # Legacy PostgreSQL removed — PostgreSQL is primary

# Model constants removed — BYOK via llm.py (BEHIVE_MODEL env var)

LOG_FMT = "%(asctime)s [QueenFeedback] %(levelname)s — %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FMT)
log = logging.getLogger("QueenFeedback")

# Minimalna confidence do uznania claim jako "confirmed prior"
HIGH_CONFIDENCE_THRESHOLD = 0.80
# Min overlap tokenów między predykcją a claim do uznania za evidence
MIN_KEYWORD_OVERLAP = 2


# ---------------------------------------------------------------------------
# DATA CLASSES
# ---------------------------------------------------------------------------

@dataclass
class Prediction:
    """Pojedyncza predykcja wyekstrahowana z syntezy misji."""
    mission_id: str
    prediction: str
    confidence_stated: float
    keywords: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Wynik walidacji jednej predykcji z hive3_calibration_log."""
    calibration_id: int
    prediction: str
    outcome: str  # "CONFIRMED" | "REFUTED" | "PARTIAL" | "UNRESOLVED"
    evidence_claims: list[str]
    confidence_before: float
    resolved: bool


@dataclass
class Lesson:
    """Lekcja wyciągnięta po misji."""
    mission_id: str
    lesson_type: str   # SUCCESS | FAILURE | WARNING | INSIGHT
    lesson: str
    recommendation: str
    applies_to: list[str]


@dataclass
class Prior:
    """Fakt cross-mission do zapisania w hive3_priors."""
    fact: str
    domain: str
    confidence: float
    source_mission: str
    is_new: bool
    prior_id: str = ""


# ---------------------------------------------------------------------------
# LLM HELPER — BYOK via llm.complete()
# ---------------------------------------------------------------------------

def _llm_call(
    prompt: str,
    system: str = "",
    model: str = "",  # ignored — routed via BEHIVE_MODEL
    max_tokens: int = 2048,
    retries: int = 3,
) -> str:
    """
    LLM call via BYOK llm.complete() — supports any provider.
    """
    from behive.engine.llm import complete
    return complete(prompt, stage="feedback", system=system, max_tokens=max_tokens)


# ---------------------------------------------------------------------------
# UTILS
# ---------------------------------------------------------------------------

def _db_connect(read_only: bool = False, retries: int = 10) -> Any:
    """Otwiera połączenie PostgreSQL z retries (plik może być locked)."""
    for attempt in range(retries):
        try:
            return _db_connect(read_only=read_only)
        except Exception as exc:
            if attempt < retries - 1:
                wait = min(5 * (attempt + 1), 30)
                log.warning(f"DB locked, retrying in {wait}s (attempt {attempt+1}/{retries})...")
                time.sleep(wait)
            else:
                raise RuntimeError(f"Cannot open PostgreSQL after {retries} attempts: {exc}") from exc
    raise RuntimeError("Unreachable")


def _tokenize(text: str) -> set[str]:
    """Proste tokenizowanie — dla keyword overlap."""
    stopwords = {
        "the", "a", "an", "and", "or", "of", "in", "for", "to", "on", "at",
        "by", "with", "from", "is", "are", "was", "were", "be", "will", "have",
        "has", "that", "this", "which", "their", "they", "it", "its", "as",
        "i", "w", "z", "do", "na", "o", "się", "jako", "oraz", "czy", "że",
        "nie", "jest", "być", "przez", "się", "by", "już",
    }
    tokens = re.findall(r"\b[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]{3,}\b", text.lower())
    return {t for t in tokens if t not in stopwords}


def _keyword_overlap(text_a: str, text_b: str) -> int:
    """Liczba wspólnych słów kluczowych między dwoma tekstami."""
    return len(_tokenize(text_a) & _tokenize(text_b))


def _fact_hash(fact: str) -> str:
    """Generuje krótki, deterministyczny ID dla faktu (8 hex chars)."""
    return hashlib.sha256(fact.lower().strip().encode()).hexdigest()[:16]


def _infer_domain(text: str) -> str:
    """Inferuje domenę z treści faktu/predykcji."""
    text_low = text.lower()
    domain_keywords = {
        "technology": ["tech", "software", "ai", "digital", "it ", "saas", "cloud", "api", "cyber"],
        "trade": ["export", "import", "trade", "sanctions", "bypass", "dual-use", "supply chain"],
        "market": ["market", "revenue", "gdp", "growth", "b2b", "saas", "sector", "industry"],
        "defense": ["defense", "military", "weapons", "aselsan", "arms", "nato", "warfare"],
        "geopolitics": ["russia", "ukraine", "turkey", "china", "uae", "eu", "nato", "oligarch"],
        "finance": ["finance", "money", "laundering", "fund", "invest", "bank", "capital"],
        "social": ["social", "media", "linkedin", "tiktok", "users", "platform", "influencer"],
        "energy": ["energy", "oil", "gas", "renewable", "solar", "wind", "nuclear"],
        "macroeconomics": ["inflation", "unemployment", "gdp", "pkb", "bezrobocie", "inflacja"],
    }
    scores: dict[str, int] = {}
    for domain, kws in domain_keywords.items():
        scores[domain] = sum(1 for kw in kws if kw in text_low)
    best = max(scores, key=lambda d: scores[d]) if any(scores.values()) else "general"
    return best if scores.get(best, 0) > 0 else "general"


# ---------------------------------------------------------------------------
# 1. PREDICTION EXTRACTOR
# ---------------------------------------------------------------------------

class PredictionExtractor:
    """
    Wyciąga predykcje ze syntezy Queen (tekst misji) używając LLM.

    Predykcja = zdanie zawierające:
      - podmiot (kto/co)
      - zmiana / trend
      - timeframe (rok, kwartał, "w ciągu X lat")
      - opcjonalnie: confidence_stated (% lub słowne)

    Zapisuje do hive3_calibration_log.
    """

    SYSTEM_PROMPT = """
Jesteś ekspertem od analizy predyktywnej. Twoje zadanie: wyekstrahuj z tekstu
wszystkie PREDYKCJE — zdania zawierające prognozę na przyszłość.

Predykcja musi zawierać:
1. Podmiot (kto lub co będzie się zmieniać)
2. Zmianę lub trend (wzrośnie / spadnie / osiągnie / zostanie / będzie)
3. Timeframe (rok, miesiąc, kwartał lub "w ciągu X lat")

Opcjonalnie: wyrażoną confidence (%, "prawdopodobnie", "likely", "wysoka pewność")

NIE wyciągaj faktów z przeszłości. Tylko prognozy/predykcje dotyczące przyszłości.

Odpowiedz WYŁĄCZNIE w formacie JSON (bez markdown):
{
  "predictions": [
    {
      "prediction": "pełne zdanie predykcji",
      "confidence_stated": 0.75,
      "keywords": ["kluczowe", "słowa", "z predykcji"]
    }
  ]
}

Jeśli brak predykcji → {"predictions": []}
"""

    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path

    def extract_predictions(
        self, synthesis_text: str, mission_id: str
    ) -> list[Prediction]:
        """
        Używa LLM Haiku do ekstrakcji predykcji z tekstu syntezy Queen.
        Zapisuje do hive3_calibration_log i zwraca listę Prediction.
        """
        log.info(f"[PredictionExtractor] Extracting predictions for mission {mission_id}")

        if not synthesis_text or len(synthesis_text) < 50:
            log.warning(f"[PredictionExtractor] Synthesis too short, skipping")
            return []

        # Truncate do 8000 chars (Haiku context limit safe zone)
        text_chunk = synthesis_text[:8000]

        prompt = f"""Przeanalizuj poniższy tekst i wyekstrahuj wszystkie predykcje/prognozy.

TEKST:
{text_chunk}

Odpowiedz w formacie JSON jak opisano w instrukcji systemowej."""

        try:
            raw = _llm_call(prompt, system=self.SYSTEM_PROMPT)
        except Exception as exc:
            log.error(f"[PredictionExtractor] LLM call failed: {exc}")
            return []

        # Parse JSON
        parsed = self._parse_llm_response(raw)
        if not parsed:
            log.warning("[PredictionExtractor] Could not parse LLM response")
            return []

        predictions_data = parsed.get("predictions", [])
        predictions: list[Prediction] = []

        for item in predictions_data:
            pred_text = item.get("prediction", "").strip()
            if not pred_text or len(pred_text) < 10:
                continue

            conf_raw = item.get("confidence_stated", 0.6)
            conf = float(conf_raw) if conf_raw is not None else 0.6
            conf = max(0.0, min(1.0, conf))  # clamp [0,1]
            keywords = item.get("keywords", [])
            if not keywords:
                keywords = list(_tokenize(pred_text))[:8]

            pred = Prediction(
                mission_id=mission_id,
                prediction=pred_text,
                confidence_stated=conf,
                keywords=keywords,
            )
            predictions.append(pred)

        # Zapisz do PostgreSQL
        saved = self._save_predictions(predictions)
        log.info(
            f"[PredictionExtractor] Extracted {len(predictions)} predictions, saved {saved}"
        )
        return predictions

    def _parse_llm_response(self, raw: str) -> Optional[dict]:
        """Parsuje odpowiedź LLM — obsługuje JSON w markdown blokach."""
        # Spróbuj wyciągnąć JSON z markdown code block
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if match:
            raw = match.group(1)
        # Szukaj raw JSON object
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group(0)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.debug(f"JSON parse failed, raw: {raw[:200]}")
            return None

    def _save_predictions(self, predictions: list[Prediction]) -> int:
        """Zapisuje predykcje do hive3_calibration_log. Zwraca liczbę zapisanych."""
        if not predictions:
            return 0
        saved = 0
        con = _db_connect(read_only=False)
        try:
            for pred in predictions:
                # Sprawdź duplikat (ta sama predykcja dla tej samej misji)
                existing = con.execute(
                    "SELECT id FROM hive3_calibration_log WHERE mission_id=? AND prediction=?",
                    [pred.mission_id, pred.prediction],
                ).fetchone()
                if existing:
                    log.debug(f"Prediction already exists: {pred.prediction[:60]}")
                    continue
                con.execute(
                    """
                    INSERT INTO hive3_calibration_log
                        (mission_id, prediction, confidence_stated, outcome,
                         outcome_evidence, time_to_outcome_hours, created_at)
                    VALUES (?, ?, ?, NULL, NULL, NULL, ?)
                    """,
                    [
                        pred.mission_id,
                        pred.prediction,
                        pred.confidence_stated,
                        datetime.now(timezone.utc).replace(tzinfo=None),
                    ],
                )
                saved += 1
        finally:
            con.close()
        return saved

    def extract_from_mission(self, mission_id: str) -> list[Prediction]:
        """
        Wygodna metoda — ładuje synthesis z DB i extraktuje predykcje.
        """
        con = _db_connect(read_only=True)
        try:
            row = con.execute(
                "SELECT synthesis FROM hive_missions WHERE id=?",
                [mission_id],
            ).fetchone()
        finally:
            con.close()

        if not row or not row[0]:
            log.warning(f"[PredictionExtractor] No synthesis found for mission {mission_id}")
            return []

        return self.extract_predictions(row[0], mission_id)


# ---------------------------------------------------------------------------
# 2. FEEDBACK VALIDATOR
# ---------------------------------------------------------------------------

class FeedbackValidator:
    """
    Weryfikuje predykcje z hive3_calibration_log gdzie outcome IS NULL.

    Logika:
    - Dla każdej otwartej predykcji szuka evidence w hive_claims z innych misji
    - Używa keyword overlap do matchowania
    - Jeśli znaleziono potwierdzające evidence → CONFIRMED
    - Jeśli znaleziono obalające evidence → REFUTED
    - Aktualizuje hive3_calibration_log, hive3_graveyard, hive3_priors
    """

    VALIDATION_SYSTEM = """
Jesteś analitykiem intelligence. Masz predykcję i znalezione evidence (claimy z różnych źródeł).
Oceń: czy evidence POTWIERDZA, OBALA, CZĘŚCIOWO POTWIERDZA czy jest NIEROZSTRZYGNIĘTE wobec predykcji?

Odpowiedz WYŁĄCZNIE w formacie JSON:
{
  "outcome": "CONFIRMED" | "REFUTED" | "PARTIAL" | "UNRESOLVED",
  "reasoning": "krótkie uzasadnienie (max 2 zdania)",
  "key_evidence": "najważniejszy claim potwierdzający/obalający"
}
"""

    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path

    def validate_pending_predictions(
        self, db_path: Optional[str] = None
    ) -> list[ValidationResult]:
        """
        Bierze wszystkie predykcje z hive3_calibration_log gdzie outcome IS NULL.
        Dla każdej szuka evidence w hive_claims z INNYCH misji.
        Aktualizuje DB jeśli znaleziono rozstrzygnięcie.

        Zwraca listę ValidationResult.
        """
        dp = db_path or self.db_path
        log.info("[FeedbackValidator] Checking pending predictions...")

        # Pobierz otwarte predykcje
        con = _db_connect(read_only=True)
        try:
            pending = con.execute(
                """
                SELECT id, mission_id, prediction, confidence_stated
                FROM hive3_calibration_log
                WHERE outcome IS NULL
                ORDER BY created_at ASC
                """
            ).fetchall()
        finally:
            con.close()

        log.info(f"[FeedbackValidator] Found {len(pending)} pending predictions")
        results: list[ValidationResult] = []

        for cal_id, mission_id, prediction, conf_stated in pending:
            result = self._validate_single(
                cal_id, mission_id, prediction, conf_stated or 0.6
            )
            results.append(result)
            if result.resolved:
                self._persist_validation(result, cal_id, mission_id, conf_stated or 0.6)

        confirmed = sum(1 for r in results if r.outcome == "CONFIRMED")
        refuted = sum(1 for r in results if r.outcome == "REFUTED")
        log.info(
            f"[FeedbackValidator] Results: {confirmed} confirmed, "
            f"{refuted} refuted, "
            f"{len(results) - confirmed - refuted} unresolved/partial"
        )
        return results

    def _validate_single(
        self,
        cal_id: int,
        source_mission_id: str,
        prediction: str,
        conf_stated: float,
    ) -> ValidationResult:
        """Waliduje jedną predykcję przez przeszukanie hive_claims."""
        pred_tokens = _tokenize(prediction)
        if not pred_tokens:
            return ValidationResult(
                calibration_id=cal_id,
                prediction=prediction,
                outcome="UNRESOLVED",
                evidence_claims=[],
                confidence_before=conf_stated,
                resolved=False,
            )

        # Szukaj w hive_claims z innych misji
        con = _db_connect(read_only=True)
        try:
            # Pobierz claimy z innych misji (nie tej samej)
            candidate_claims = con.execute(
                """
                SELECT claim, confidence, mission_id
                FROM hive_claims
                WHERE mission_id != ?
                  AND confidence >= 0.6
                ORDER BY confidence DESC
                LIMIT 500
                """,
                [source_mission_id],
            ).fetchall()
        finally:
            con.close()

        # Filtruj przez keyword overlap
        relevant: list[tuple[str, float, str]] = []
        for claim_text, claim_conf, claim_mission in candidate_claims:
            overlap = _keyword_overlap(prediction, claim_text)
            if overlap >= MIN_KEYWORD_OVERLAP:
                relevant.append((claim_text, claim_conf, claim_mission))

        if len(relevant) < 2:
            # Za mało evidence — nie rozstrzygaj
            return ValidationResult(
                calibration_id=cal_id,
                prediction=prediction,
                outcome="UNRESOLVED",
                evidence_claims=[c[0] for c in relevant],
                confidence_before=conf_stated,
                resolved=False,
            )

        # Mamy potencjalne evidence — pytamy LLM o ocenę
        evidence_text = "\n".join(
            f"- [{c[1]:.2f}] {c[0]}" for c in relevant[:10]
        )
        prompt = f"""PREDYKCJA: {prediction}

ZNALEZIONE EVIDENCE (z innych misji HIVE):
{evidence_text}

Oceń: czy to evidence POTWIERDZA, OBALA, CZĘŚCIOWO POTWIERDZA czy jest NIEROZSTRZYGNIĘTE?"""

        try:
            raw = _llm_call(
                prompt, system=self.VALIDATION_SYSTEM
            )
        except Exception as exc:
            log.error(f"[FeedbackValidator] LLM failed for cal_id={cal_id}: {exc}")
            return ValidationResult(
                calibration_id=cal_id,
                prediction=prediction,
                outcome="UNRESOLVED",
                evidence_claims=[c[0] for c in relevant[:5]],
                confidence_before=conf_stated,
                resolved=False,
            )

        parsed = self._parse_validation_response(raw)
        outcome = parsed.get("outcome", "UNRESOLVED")
        key_evidence = parsed.get("key_evidence", "")
        reasoning = parsed.get("reasoning", "")

        resolved = outcome in ("CONFIRMED", "REFUTED")
        evidence_summary = f"{reasoning} | {key_evidence}" if key_evidence else reasoning

        return ValidationResult(
            calibration_id=cal_id,
            prediction=prediction,
            outcome=outcome,
            evidence_claims=[c[0] for c in relevant[:5]] + ([evidence_summary] if evidence_summary else []),
            confidence_before=conf_stated,
            resolved=resolved,
        )

    def _parse_validation_response(self, raw: str) -> dict:
        """Parsuje odpowiedź LLM z walidacji."""
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        # Fallback — szukaj outcome keyword
        for outcome in ("CONFIRMED", "REFUTED", "PARTIAL", "UNRESOLVED"):
            if outcome in raw.upper():
                return {"outcome": outcome, "reasoning": raw[:200], "key_evidence": ""}
        return {"outcome": "UNRESOLVED", "reasoning": "Parse failed", "key_evidence": ""}

    def _persist_validation(
        self,
        result: ValidationResult,
        cal_id: int,
        source_mission_id: str,
        conf_stated: float,
    ) -> None:
        """Zapisuje wynik walidacji do DB i aktualizuje powiązane tabele."""
        evidence_str = " | ".join(result.evidence_claims[:3])
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        con = _db_connect(read_only=False)
        try:
            # Aktualizuj calibration_log
            con.execute(
                """
                UPDATE hive3_calibration_log
                SET outcome = ?,
                    outcome_evidence = ?,
                    resolved_at = ?
                WHERE id = ?
                """,
                [result.outcome, evidence_str[:2000], now, cal_id],
            )

            if result.outcome == "REFUTED":
                # Dodaj do graveyard
                hyp_id = _fact_hash(result.prediction + source_mission_id)
                existing = con.execute(
                    "SELECT id FROM hive3_graveyard WHERE id=?", [hyp_id]
                ).fetchone()
                if not existing:
                    domain = _infer_domain(result.prediction)
                    con.execute(
                        """
                        INSERT INTO hive3_graveyard
                            (id, hypothesis, topic_domain, failure_reason, mission_id,
                             confidence_at_death, buried_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            hyp_id,
                            result.prediction,
                            domain,
                            evidence_str[:500],
                            source_mission_id,
                            conf_stated,
                            now,
                        ],
                    )
                    log.info(
                        f"[FeedbackValidator] Buried refuted hypothesis: {result.prediction[:80]}"
                    )

            elif result.outcome == "CONFIRMED":
                # Dodaj do priors jeśli confidence wysoka
                if conf_stated >= HIGH_CONFIDENCE_THRESHOLD - 0.05:
                    prior_id = _fact_hash(result.prediction)
                    existing = con.execute(
                        "SELECT id, times_confirmed FROM hive3_priors WHERE id=?",
                        [prior_id],
                    ).fetchone()
                    domain = _infer_domain(result.prediction)
                    if existing:
                        con.execute(
                            """
                            UPDATE hive3_priors
                            SET times_confirmed = times_confirmed + 1,
                                last_confirmed = ?
                            WHERE id = ?
                            """,
                            [now, prior_id],
                        )
                    else:
                        con.execute(
                            """
                            INSERT INTO hive3_priors
                                (id, fact, domain, confidence, source_mission,
                                 cited_by_missions, times_confirmed, times_contradicted,
                                 created_at, last_confirmed)
                            VALUES (?, ?, ?, ?, ?, '[]', 1, 0, ?, ?)
                            """,
                            [
                                prior_id,
                                result.prediction,
                                domain,
                                conf_stated,
                                source_mission_id,
                                now,
                                now,
                            ],
                        )
                    log.info(
                        f"[FeedbackValidator] Confirmed prediction → prior: {result.prediction[:80]}"
                    )
        finally:
            con.close()


# ---------------------------------------------------------------------------
# 3. LESSONS EXTRACTOR
# ---------------------------------------------------------------------------

class LessonsExtractor:
    """
    Wyciąga lekcje operacyjne po zakończeniu misji.

    Analizuje:
    - Które source_types dały wysoką confidence (SUCCESS)
    - Które domeny/URLs były zablokowane lub dały niskie wyniki (FAILURE)
    - Wzorce w claims które były szczególnie informatywne (INSIGHT)
    """

    LESSONS_SYSTEM = """
Jesteś ekspertem OSINT analizującym skuteczność misji zbierania intelligence.
Na podstawie danych o źródłach i claimach misji wyciągnij wnioski operacyjne.

Zidentyfikuj:
1. Co DZIAŁAŁO (source_types z wysokimi scorami, domeny z bogatymi claimami)
2. Co NIE DZIAŁAŁO (źródła zablokowane, niskie score_total, brak claimów)
3. Rekomendacje dla przyszłych misji o podobnej tematyce

Odpowiedz w formacie JSON:
{
  "lessons": [
    {
      "lesson_type": "SUCCESS" | "FAILURE" | "WARNING" | "INSIGHT",
      "lesson": "Konkretny opis lekcji",
      "recommendation": "Rekomendacja dla przyszłych misji",
      "applies_to": ["tag1", "tag2"]
    }
  ]
}
"""

    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path

    def extract_lessons(self, mission_id: str) -> list[Lesson]:
        """
        Analizuje dane misji (sources, claims) i wyciąga lekcje operacyjne.
        Zapisuje do hive3_lessons.
        """
        log.info(f"[LessonsExtractor] Extracting lessons for mission {mission_id}")

        mission_data = self._load_mission_data(mission_id)
        if not mission_data:
            log.warning(f"[LessonsExtractor] No data found for mission {mission_id}")
            return []

        # Generuj lekcje oparte na danych (bez LLM — szybkie i deterministyczne)
        rule_based_lessons = self._extract_rule_based_lessons(mission_id, mission_data)

        # LLM-based lessons (dla bogatszego kontekstu)
        llm_lessons = self._extract_llm_lessons(mission_id, mission_data)

        all_lessons = rule_based_lessons + llm_lessons
        saved = self._save_lessons(all_lessons)
        log.info(f"[LessonsExtractor] Extracted {len(all_lessons)} lessons, saved {saved}")
        return all_lessons

    def _load_mission_data(self, mission_id: str) -> Optional[dict]:
        """Ładuje dane potrzebne do analizy lekcji."""
        con = _db_connect(read_only=True)
        try:
            # Podsumowanie źródeł
            source_stats = con.execute(
                """
                SELECT source_type,
                       COUNT(*) as cnt,
                       AVG(score_total) as avg_score,
                       SUM(CASE WHEN status='harvested' THEN 1 ELSE 0 END) as harvested
                FROM hive_sources
                WHERE mission_id = ?
                GROUP BY source_type
                ORDER BY avg_score DESC
                """,
                [mission_id],
            ).fetchall()

            # Statystyki claimów
            claim_stats = con.execute(
                """
                SELECT claim_type,
                       COUNT(*) as cnt,
                       AVG(confidence) as avg_conf
                FROM hive_claims
                WHERE mission_id = ?
                GROUP BY claim_type
                ORDER BY avg_conf DESC
                """,
                [mission_id],
            ).fetchall()

            # Domeny z blokowaniem (score_accessibility < 30)
            blocked_domains = con.execute(
                """
                SELECT domain, score_accessibility, score_total
                FROM hive_sources
                WHERE mission_id = ? AND score_accessibility < 30
                ORDER BY score_total ASC
                LIMIT 10
                """,
                [mission_id],
            ).fetchall()

            # Top performing domains
            top_domains = con.execute(
                """
                SELECT domain, AVG(score_total) as avg_score, COUNT(*) as cnt
                FROM hive_sources
                WHERE mission_id = ?
                GROUP BY domain
                HAVING avg_score > 50
                ORDER BY avg_score DESC
                LIMIT 5
                """,
                [mission_id],
            ).fetchall()

            # Ogólne statystyki misji
            mission_info = con.execute(
                "SELECT topic, sources_found, sources_scored, sources_harvested FROM hive_missions WHERE id=?",
                [mission_id],
            ).fetchone()

            return {
                "source_stats": source_stats,
                "claim_stats": claim_stats,
                "blocked_domains": blocked_domains,
                "top_domains": top_domains,
                "mission_info": mission_info,
            }
        finally:
            con.close()

    def _extract_rule_based_lessons(
        self, mission_id: str, data: dict
    ) -> list[Lesson]:
        """Deterministyczne lekcje oparte na progach statystycznych."""
        lessons: list[Lesson] = []
        source_stats = data.get("source_stats", [])
        blocked = data.get("blocked_domains", [])
        top_domains = data.get("top_domains", [])

        # Lekcja: sukces source_type (avg_score > 50)
        for source_type, cnt, avg_score, harvested in source_stats:
            if avg_score and avg_score > 50 and cnt >= 2:
                lessons.append(
                    Lesson(
                        mission_id=mission_id,
                        lesson_type="SUCCESS",
                        lesson=f"Source type '{source_type}' performed well: avg_score={avg_score:.1f}, "
                               f"count={cnt}, harvested={harvested}",
                        recommendation=f"Priorytetyzuj '{source_type}' dla podobnych tematów.",
                        applies_to=[source_type, "source_selection"],
                    )
                )
            elif avg_score and avg_score < 20 and cnt >= 2:
                lessons.append(
                    Lesson(
                        mission_id=mission_id,
                        lesson_type="FAILURE",
                        lesson=f"Source type '{source_type}' underperformed: avg_score={avg_score:.1f}, "
                               f"count={cnt}",
                        recommendation=f"Ogranicz użycie '{source_type}' lub dostosuj scoring.",
                        applies_to=[source_type, "source_selection"],
                    )
                )

        # Lekcja: zablokowane domeny
        if blocked:
            blocked_list = ", ".join(d[0] for d in blocked[:5])
            lessons.append(
                Lesson(
                    mission_id=mission_id,
                    lesson_type="WARNING",
                    lesson=f"Zablokowane/niedostępne domeny: {blocked_list}",
                    recommendation="Dodaj alternatywne źródła; rozważ cached/mirror versions.",
                    applies_to=["accessibility", "source_selection"],
                )
            )

        # Lekcja: top performing domains
        for domain, avg_score, cnt in top_domains:
            if avg_score > 70:
                lessons.append(
                    Lesson(
                        mission_id=mission_id,
                        lesson_type="INSIGHT",
                        lesson=f"Domena '{domain}' była high-value: avg_score={avg_score:.1f}, {cnt} sources",
                        recommendation=f"Dodaj '{domain}' do whitelisty dla podobnych tematów.",
                        applies_to=[domain, "domain_whitelist"],
                    )
                )

        return lessons

    def _extract_llm_lessons(
        self, mission_id: str, data: dict
    ) -> list[Lesson]:
        """LLM-generated deeper lessons z kontekstem misji."""
        if not data.get("mission_info"):
            return []

        topic = data["mission_info"][0] if data["mission_info"] else "unknown"
        source_summary = "\n".join(
            f"  - {s[0]}: count={s[1]}, avg_score={s[2]:.1f}" if s[2] else f"  - {s[0]}: count={s[1]}"
            for s in data.get("source_stats", [])[:8]
        )
        claim_summary = "\n".join(
            f"  - {c[0]}: count={c[1]}, avg_conf={c[2]:.2f}" if c[2] else f"  - {c[0]}: count={c[1]}"
            for c in data.get("claim_stats", [])[:6]
        )
        blocked_summary = "\n".join(
            f"  - {b[0]} (accessibility={b[1]}, total={b[2]})"
            for b in data.get("blocked_domains", [])[:5]
        )

        prompt = f"""MISJA: {mission_id}
TEMAT: {topic}

STATYSTYKI ŹRÓDEŁ:
{source_summary or '(brak danych)'}

STATYSTYKI CLAIMÓW:
{claim_summary or '(brak danych)'}

ZABLOKOWANE DOMENY:
{blocked_summary or '(brak)'}

Wyciągnij lekcje operacyjne dla przyszłych misji o podobnej tematyce."""

        try:
            raw = _llm_call(
                prompt, system=self.LESSONS_SYSTEM
            )
        except Exception as exc:
            log.error(f"[LessonsExtractor] LLM call failed: {exc}")
            return []

        parsed = self._parse_lessons_response(raw)
        if not parsed:
            return []

        lessons: list[Lesson] = []
        for item in parsed.get("lessons", []):
            lesson_text = item.get("lesson", "").strip()
            if not lesson_text:
                continue
            lessons.append(
                Lesson(
                    mission_id=mission_id,
                    lesson_type=item.get("lesson_type", "INSIGHT"),
                    lesson=lesson_text,
                    recommendation=item.get("recommendation", ""),
                    applies_to=item.get("applies_to", []),
                )
            )
        return lessons

    def _parse_lessons_response(self, raw: str) -> Optional[dict]:
        """Parsuje JSON z LLM response."""
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return None

    def _save_lessons(self, lessons: list[Lesson]) -> int:
        """Zapisuje lekcje do hive3_lessons."""
        if not lessons:
            return 0
        saved = 0
        con = _db_connect(read_only=False)
        try:
            for lesson in lessons:
                # Sprawdź duplikat
                existing = con.execute(
                    "SELECT id FROM hive3_lessons WHERE mission_id=? AND lesson=?",
                    [lesson.mission_id, lesson.lesson],
                ).fetchone()
                if existing:
                    continue
                con.execute(
                    """
                    INSERT INTO hive3_lessons
                        (mission_id, lesson_type, lesson, recommendation, applies_to, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        lesson.mission_id,
                        lesson.lesson_type,
                        lesson.lesson,
                        lesson.recommendation,
                        lesson.applies_to,
                        datetime.now(timezone.utc).replace(tzinfo=None),
                    ],
                )
                saved += 1
        finally:
            con.close()
        return saved


# ---------------------------------------------------------------------------
# 4. PRIOR MANAGER
# ---------------------------------------------------------------------------

class PriorManager:
    """
    Zarządza bazą faktów cross-mission w hive3_priors.

    Logika:
    - Bierze hive_claims z misji gdzie confidence > HIGH_CONFIDENCE_THRESHOLD
    - Sprawdza keyword overlap z istniejącymi priors
    - Jeśli overlap wysoki → UPDATE times_confirmed
    - Jeśli nowy → INSERT
    - Jeśli sprzeczny → UPDATE times_contradicted
    """

    CONTRADICTION_THRESHOLD = 0.7   # min overlap ratio do wykrycia sprzeczności
    SIMILARITY_OVERLAP_MIN = 3       # min wspólne tokeny do uznania za "ten sam fakt"

    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path

    def update_priors(self, mission_id: str) -> int:
        """
        Przetwarza claimy z misji i aktualizuje hive3_priors.
        Zwraca liczbę zaktualizowanych/nowych priors.
        """
        log.info(f"[PriorManager] Updating priors from mission {mission_id}")

        # Pobierz wysokiej jakości claimy z tej misji
        con = _db_connect(read_only=True)
        try:
            new_claims = con.execute(
                """
                SELECT claim, confidence, claim_type
                FROM hive_claims
                WHERE mission_id = ?
                  AND confidence >= ?
                ORDER BY confidence DESC
                LIMIT 100
                """,
                [mission_id, HIGH_CONFIDENCE_THRESHOLD],
            ).fetchall()

            # Pobierz istniejące priory do porównania
            existing_priors = con.execute(
                "SELECT id, fact, domain, confidence, times_confirmed, times_contradicted FROM hive3_priors"
            ).fetchall()
        finally:
            con.close()

        log.info(
            f"[PriorManager] {len(new_claims)} high-confidence claims, "
            f"{len(existing_priors)} existing priors"
        )

        updated_count = 0
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        for claim_text, claim_conf, claim_type in new_claims:
            if not claim_text or len(claim_text) < 15:
                continue

            # Szukaj podobnych priors przez keyword overlap
            best_match: Optional[tuple] = None
            best_overlap: int = 0

            for prior in existing_priors:
                prior_id, prior_fact, prior_domain, prior_conf, times_conf, times_contra = prior
                overlap = _keyword_overlap(claim_text, prior_fact)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_match = prior

            if best_match and best_overlap >= self.SIMILARITY_OVERLAP_MIN:
                prior_id, prior_fact, prior_domain, prior_conf, times_conf, times_contra = best_match

                # Sprawdź czy to potwierdzenie czy sprzeczność
                is_contradiction = self._detect_contradiction(claim_text, prior_fact)

                con = _db_connect(read_only=False)
                try:
                    if is_contradiction:
                        con.execute(
                            """
                            UPDATE hive3_priors
                            SET times_contradicted = times_contradicted + 1
                            WHERE id = ?
                            """,
                            [prior_id],
                        )
                        log.debug(
                            f"[PriorManager] Contradiction detected for prior: {prior_fact[:60]}"
                        )
                    else:
                        con.execute(
                            """
                            UPDATE hive3_priors
                            SET times_confirmed = times_confirmed + 1,
                                last_confirmed = ?
                            WHERE id = ?
                            """,
                            [now, prior_id],
                        )
                        log.debug(
                            f"[PriorManager] Confirmed prior: {prior_fact[:60]}"
                        )
                    updated_count += 1
                finally:
                    con.close()

            else:
                # Nowy prior — INSERT
                domain = _infer_domain(claim_text)
                prior_id = _fact_hash(claim_text)

                con = _db_connect(read_only=False)
                try:
                    # Sprawdź czy ID już istnieje (hash collision guard)
                    existing = con.execute(
                        "SELECT id FROM hive3_priors WHERE id=?", [prior_id]
                    ).fetchone()
                    if not existing:
                        con.execute(
                            """
                            INSERT INTO hive3_priors
                                (id, fact, domain, confidence, source_mission,
                                 cited_by_missions, times_confirmed, times_contradicted,
                                 created_at, last_confirmed)
                            VALUES (?, ?, ?, ?, ?, '[]', 1, 0, ?, ?)
                            """,
                            [
                                prior_id,
                                claim_text,
                                domain,
                                float(claim_conf),
                                mission_id,
                                now,
                                now,
                            ],
                        )
                        updated_count += 1
                        log.debug(f"[PriorManager] New prior: {claim_text[:60]}")
                finally:
                    con.close()

        log.info(f"[PriorManager] Updated/added {updated_count} priors")
        return updated_count

    def _detect_contradiction(self, claim: str, prior_fact: str) -> bool:
        """
        Prosta heurystyka wykrywania sprzeczności.
        Sprawdza negacje i antonimy w powiązanych zdaniach.
        """
        negation_markers = [
            "not ", "no longer", "denied", "false", "incorrect",
            "nie ", "brak ", "fałszywe", "incorrect", "refuted", "obalono",
            "contrary", "despite", "however",
        ]
        claim_low = claim.lower()
        prior_low = prior_fact.lower()

        # Jeśli claim zawiera negację i dużo wspólnych tokenów z prior → sprzeczność
        has_negation = any(neg in claim_low for neg in negation_markers)
        overlap = _keyword_overlap(claim, prior_fact)
        # Wysoki overlap + negacja = prawdopodobna sprzeczność
        return has_negation and overlap >= 4

    def get_priors_summary(self) -> dict:
        """Zwraca podsumowanie aktualnego stanu priors."""
        con = _db_connect(read_only=True)
        try:
            total = con.execute("SELECT COUNT(*) FROM hive3_priors").fetchone()[0]
            by_domain = con.execute(
                "SELECT domain, COUNT(*) FROM hive3_priors GROUP BY domain ORDER BY COUNT(*) DESC"
            ).fetchall()
            high_conf = con.execute(
                "SELECT COUNT(*) FROM hive3_priors WHERE confidence >= 0.85"
            ).fetchone()[0]
            contradicted = con.execute(
                "SELECT COUNT(*) FROM hive3_priors WHERE times_contradicted > 0"
            ).fetchone()[0]
            return {
                "total": total,
                "high_confidence": high_conf,
                "contradicted": contradicted,
                "by_domain": dict(by_domain),
            }
        finally:
            con.close()


# ---------------------------------------------------------------------------
# 5. INTELLIGENCE CLOSURE
# ---------------------------------------------------------------------------

class IntelligenceClosure:
    """
    Master sekwencja zamknięcia pętli kalibracyjnej po zakończeniu misji.

    Wywołuje sekwencyjnie:
      1. PredictionExtractor  — ekstrahuje predykcje z syntezy
      2. FeedbackValidator    — waliduje STARE predykcje w świetle nowych danych
      3. LessonsExtractor     — wyciąga lekcje operacyjne
      4. PriorManager         — aktualizuje bazę faktów cross-mission
      5. Raportuje wyniki

    Użycie:
      closure = IntelligenceClosure()
      report = closure.close_mission("hive_1784501198_0fa5d0")
      print(report)
    """

    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self.extractor = PredictionExtractor(db_path)
        self.validator = FeedbackValidator(db_path)
        self.lessons = LessonsExtractor(db_path)
        self.priors = PriorManager(db_path)

    def close_mission(self, mission_id: str) -> dict:
        """
        Zamknięcie pełnej pętli kalibracyjnej dla jednej misji.
        Zwraca słownik z raportem zamknięcia.
        """
        log.info(f"[IntelligenceClosure] ═══ Starting closure for mission {mission_id} ═══")
        start_time = time.time()
        report: dict = {
            "mission_id": mission_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "steps": {},
        }

        # ── Krok 1: Ekstrakcja predykcji z syntezy ───────────────────────────
        log.info("[IntelligenceClosure] Step 1/4: Extracting predictions...")
        step1_start = time.time()
        try:
            predictions = self.extractor.extract_from_mission(mission_id)
            report["steps"]["predictions_extracted"] = {
                "count": len(predictions),
                "predictions": [p.prediction[:100] for p in predictions],
                "duration_s": round(time.time() - step1_start, 2),
            }
        except Exception as exc:
            log.error(f"[IntelligenceClosure] Step 1 failed: {exc}")
            report["steps"]["predictions_extracted"] = {"error": str(exc), "count": 0}
            predictions = []

        # ── Krok 2: Walidacja STARYCH predykcji w świetle nowych danych ──────
        log.info("[IntelligenceClosure] Step 2/4: Validating pending predictions...")
        step2_start = time.time()
        try:
            validation_results = self.validator.validate_pending_predictions()
            confirmed = [r for r in validation_results if r.outcome == "CONFIRMED"]
            refuted = [r for r in validation_results if r.outcome == "REFUTED"]
            partial = [r for r in validation_results if r.outcome == "PARTIAL"]
            report["steps"]["predictions_validated"] = {
                "total_pending": len(validation_results),
                "confirmed": len(confirmed),
                "refuted": len(refuted),
                "partial": len(partial),
                "unresolved": len(validation_results) - len(confirmed) - len(refuted) - len(partial),
                "duration_s": round(time.time() - step2_start, 2),
            }
        except Exception as exc:
            log.error(f"[IntelligenceClosure] Step 2 failed: {exc}")
            report["steps"]["predictions_validated"] = {"error": str(exc)}
            validation_results = []

        # ── Krok 3: Ekstrakcja lekcji operacyjnych ───────────────────────────
        log.info("[IntelligenceClosure] Step 3/4: Extracting lessons...")
        step3_start = time.time()
        try:
            lessons = self.lessons.extract_lessons(mission_id)
            by_type: dict[str, int] = {}
            for lesson in lessons:
                by_type[lesson.lesson_type] = by_type.get(lesson.lesson_type, 0) + 1
            report["steps"]["lessons_extracted"] = {
                "count": len(lessons),
                "by_type": by_type,
                "duration_s": round(time.time() - step3_start, 2),
            }
        except Exception as exc:
            log.error(f"[IntelligenceClosure] Step 3 failed: {exc}")
            report["steps"]["lessons_extracted"] = {"error": str(exc), "count": 0}
            lessons = []

        # ── Krok 4: Aktualizacja priors ───────────────────────────────────────
        log.info("[IntelligenceClosure] Step 4/4: Updating priors...")
        step4_start = time.time()
        try:
            priors_count = self.priors.update_priors(mission_id)
            priors_summary = self.priors.get_priors_summary()
            report["steps"]["priors_updated"] = {
                "updated_or_new": priors_count,
                "total_in_db": priors_summary["total"],
                "high_confidence": priors_summary["high_confidence"],
                "by_domain": priors_summary["by_domain"],
                "duration_s": round(time.time() - step4_start, 2),
            }
        except Exception as exc:
            log.error(f"[IntelligenceClosure] Step 4 failed: {exc}")
            report["steps"]["priors_updated"] = {"error": str(exc), "updated_or_new": 0}

        # ── Finalne podsumowanie ──────────────────────────────────────────────
        total_duration = round(time.time() - start_time, 2)
        report["summary"] = {
            "total_duration_s": total_duration,
            "predictions_new": report["steps"].get("predictions_extracted", {}).get("count", 0),
            "predictions_closed": (
                report["steps"].get("predictions_validated", {}).get("confirmed", 0)
                + report["steps"].get("predictions_validated", {}).get("refuted", 0)
            ),
            "lessons_added": report["steps"].get("lessons_extracted", {}).get("count", 0),
            "priors_updated": report["steps"].get("priors_updated", {}).get("updated_or_new", 0),
        }

        log.info(
            f"[IntelligenceClosure] ═══ Closure complete: "
            f"{report['summary']['predictions_new']} predictions extracted, "
            f"{report['summary']['predictions_closed']} resolved, "
            f"{report['summary']['lessons_added']} lessons, "
            f"{report['summary']['priors_updated']} priors updated "
            f"({total_duration}s) ═══"
        )
        return report

    def close_all_done_missions(self) -> list[dict]:
        """
        Convenience method — przetwarza wszystkie misje ze status='done'
        które nie mają jeszcze zamkniętej pętli.
        """
        con = _db_connect(read_only=True)
        try:
            missions = con.execute(
                """
                SELECT id FROM hive_missions
                WHERE status = 'done'
                  AND synthesis IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 20
                """
            ).fetchall()
        finally:
            con.close()

        log.info(f"[IntelligenceClosure] Processing {len(missions)} done missions...")
        reports: list[dict] = []
        for (mission_id,) in missions:
            try:
                report = self.close_mission(mission_id)
                reports.append(report)
            except Exception as exc:
                log.error(f"[IntelligenceClosure] Failed for {mission_id}: {exc}")
        return reports

    def print_report(self, report: dict) -> None:
        """Ładny wydruk raportu closure."""
        print("\n" + "═" * 70)
        print(f"  INTELLIGENCE CLOSURE REPORT — {report['mission_id']}")
        print(f"  {report['timestamp']}")
        print("═" * 70)
        s = report.get("summary", {})
        print(f"\n  📌 Predictions extracted:   {s.get('predictions_new', 0)}")
        print(f"  ✅ Predictions resolved:    {s.get('predictions_closed', 0)}")
        print(f"  📚 Lessons added:           {s.get('lessons_added', 0)}")
        print(f"  🧠 Priors updated/new:      {s.get('priors_updated', 0)}")
        print(f"  ⏱  Total duration:          {s.get('total_duration_s', 0)}s")

        steps = report.get("steps", {})
        pv = steps.get("predictions_validated", {})
        if pv and "confirmed" in pv:
            print(f"\n  Validation breakdown (all pending predictions):")
            print(f"    CONFIRMED: {pv.get('confirmed', 0)}")
            print(f"    REFUTED:   {pv.get('refuted', 0)}")
            print(f"    PARTIAL:   {pv.get('partial', 0)}")
            print(f"    PENDING:   {pv.get('unresolved', 0)}")

        pu = steps.get("priors_updated", {})
        if pu and "by_domain" in pu:
            print(f"\n  Priors knowledge base: {pu.get('total_in_db', 0)} facts total")
            print(f"  High-confidence facts: {pu.get('high_confidence', 0)}")
            print(f"  By domain: {pu.get('by_domain', {})}")
        print("═" * 70 + "\n")


# ---------------------------------------------------------------------------
# CLI ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    """
    CLI: python3 hive2_queen_feedback.py <mission_id>
         python3 hive2_queen_feedback.py --all
         python3 hive2_queen_feedback.py --test
    """
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 hive2_queen_feedback.py <mission_id>")
        print("       python3 hive2_queen_feedback.py --all")
        print("       python3 hive2_queen_feedback.py --test")
        sys.exit(1)

    arg = sys.argv[1]
    closure = IntelligenceClosure()

    if arg == "--all":
        reports = closure.close_all_done_missions()
        for report in reports:
            closure.print_report(report)
        print(f"\nTotal missions processed: {len(reports)}")

    elif arg == "--test":
        # Test mode — używa syntetycznych danych lub pierwszej dostępnej misji
        log.info("Running in test mode...")
        con = _db_connect(read_only=True)
        try:
            row = con.execute(
                "SELECT id FROM hive_missions WHERE synthesis IS NOT NULL LIMIT 1"
            ).fetchone()
        finally:
            con.close()

        if row:
            report = closure.close_mission(row[0])
            closure.print_report(report)
        else:
            # Test z syntetycznym tekstem
            extractor = PredictionExtractor()
            test_text = """
            Turkey LinkedIn users will reach 15M by Q4 2026 according to current growth trends.
            B2B SaaS adoption in Turkey is projected to grow 20% YoY through 2027.
            Georgian IT market will consolidate, reducing to 2 dominant players by 2025.
            UAE sanctions evasion networks are expected to expand by 30% as controls tighten.
            """
            preds = extractor.extract_predictions(test_text, "test_feedback_001")
            print(f"\nTest extraction: {len(preds)} predictions found")
            for p in preds:
                print(f"  [{p.confidence_stated:.2f}] {p.prediction}")

    else:
        # Konkretna misja
        report = closure.close_mission(arg)
        closure.print_report(report)
        print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
