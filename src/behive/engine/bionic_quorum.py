#!/usr/bin/env python3
"""
HIVE 2.0 — hive2_bionic_quorum.py
════════════════════════════════════
Quorum Threshold System — analog waggle dance decyzji roju.

Biologia:
  Pszczoły zwiadowczki nie uruchamiają roju zanim co najmniej 15-20 zwiadowczyń
  tańczy TO SAMO MIEJSCE. Quorum eliminuje przypadkowe jednoźródłowe sygnały.
  Żadna pszczoła nie leci na pojedynczą obserwację.

Problem HIVE (zdiagnozowany):
  Scout zwraca np. 12 URL — HIVE rusza do F4 (harvest).
  Przy słabej coverability tematu → 12 URL = 0 confirmed facts = Queen blind.
  Pszczoła by NIE wyleciała na 12 zwiadowczyń bez quorum.

Rozwiązanie:
  QUORUM_MIN = 25 harvestabl URLs wymagane do startu harvest.
  Poniżej → automatyczne rozszerzenie zasięgu (range expansion),
  czyli dodatkowa runda scouting z alternatywnymi queries.

Scheme:
  ┌──────────────────────────────────────────────────────────┐
  │  QUORUM CHECK (po F1 prescout, przed F4 harvest)         │
  │                                                          │
  │  harvestable_count >= QUORUM_MIN (25)?                   │
  │      YES → harvest (pszczoły wylatują)                   │
  │      NO  → RANGE_EXPANSION (nowe queries, drugi scout)   │
  │           max 2 expansions, then harvest anyway          │
  └──────────────────────────────────────────────────────────┘

Public API:
    from hive2_bionic_quorum import QuorumChecker
    qc = QuorumChecker(mission_id)
    result = qc.check()       # QuorumResult
    if result.needs_expansion:
        extra_queries = result.expansion_queries
        # → uruchom kolejny scout z tymi queries
    else:
        # → harvest normalnie
"""

from __future__ import annotations

import os
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

log = logging.getLogger("hive2_bionic_quorum")
DB_PATH = os.environ.get("BEHIVE_LEGACY_DB", "")  # Legacy — PostgreSQL is primary

# ─────────────────────────────────────────────────────────────────────────────
# THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────
QUORUM_MIN          = 25     # min harvestable sources → pszczoły lecą
QUORUM_SOFT         = 40     # soft target — poniżej uruchamia expansion ale not blokuje
QUORUM_HARD_MIN     = 15     # absolutne minimum — poniżej ZAWSZE expansion
MAX_EXPANSIONS      = 2      # maks ile razy rozszerzamy zasięg
EXPANSION_QUERIES   = 5      # ile dodatkowych queries per expansion


@dataclass
class QuorumResult:
    """Bionic quorum voting result with confidence and expansion queries."""
    mission_id:         str
    harvestable_count:  int
    total_sources:      int
    skip_count:         int
    quorum_met:         bool
    needs_expansion:    bool
    expansion_queries:  List[str] = field(default_factory=list)
    expansion_round:    int = 0
    report:             str = ""

    def summary(self) -> str:
        """Return human-readable summary of the quorum result."""
        status = "✅ QUORUM MET" if self.quorum_met else "⚠️ QUORUM FAILED"
        exp    = f" → expansion round {self.expansion_round + 1}" if self.needs_expansion else ""
        return (
            f"🐝 {status}: {self.harvestable_count}/{self.total_sources} harvestable "
            f"(skip={self.skip_count}){exp}"
        )


def _pg_connect(read_only: bool = False):
    """Connect to PostgreSQL (via connection pool)."""
    from behive.engine.db import connect
    return connect(read_only=read_only)


def _extract_keywords(topic: str) -> List[str]:
    _STOP = {"the","and","for","with","that","this","are","from","have",
             "more","also","will","been","can","not","was","jako","jest",
             "dla","oraz","jak","nie","się","przez"}
    words = re.findall(r'[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]{4,}', topic.lower())
    return [w for w in words if w not in _STOP]


class QuorumChecker:
    """
    Checks whether the current pre-scout has enough harvestable sources.
    Generates targeted expansion queries if quorum is not met.
    """

    def __init__(self, mission_id: str):
        self.mission_id = mission_id
        self._topic = self._load_topic()
        self._expansion_count = self._load_expansion_count()

    def _load_topic(self) -> str:
        try:
            con = _pg_connect(read_only=True)
            row = con.execute(
                "SELECT topic FROM hive_missions WHERE id=?", [self.mission_id]
            ).fetchone()
            con.close()
            return row[0] if row else ""
        except Exception as e:
            log.debug(f"Suppressed in bionic_quorum.py: {e}")
            return ""

    def _load_expansion_count(self) -> int:
        """Jak wiele range expansions już zrobiliśmy dla tej misji."""
        try:
            con = _pg_connect(read_only=True)
            row = con.execute("""
                SELECT COUNT(*) FROM hive_quorum_expansions
                WHERE mission_id = ?
            """, [self.mission_id]).fetchone()
            con.close()
            return row[0] if row else 0
        except Exception as e:
            log.debug(f"Suppressed in bionic_quorum.py: {e}")
            return 0

    def check(self) -> QuorumResult:
        """Main check — returns QuorumResult with decision and optional expansion queries."""
        try:
            con = _pg_connect(read_only=True)
            total = con.execute(
                "SELECT COUNT(*) FROM hive_sources WHERE mission_id=?",
                [self.mission_id]
            ).fetchone()[0]

            # Harvestable = pending/scouted, nie skip, nie error, nie blocked
            harvestable = con.execute("""
                SELECT COUNT(*) FROM hive_sources
                WHERE mission_id = ?
                  AND status IN ('pending', 'scouted')
                  AND score_total >= 10
            """, [self.mission_id]).fetchone()[0]

            # Prescout skip count (if prescout already ran)
            try:
                skip_count = con.execute("""
                    SELECT COUNT(*) FROM hive_prescout_map
                    WHERE mission_id = ?
                      AND routing_decision = 'skip'
                """, [self.mission_id]).fetchone()[0]
            except Exception as e:
                log.debug(f"Suppressed in bionic_quorum.py: {e}")
                skip_count = 0

            con.close()

        except Exception as e:
            log.warning(f"Quorum check DB query failed: {e}")
            return QuorumResult(
                mission_id=self.mission_id,
                harvestable_count=0,
                total_sources=0,
                skip_count=0,
                quorum_met=False,
                needs_expansion=False,
                report=f"Quorum check failed: {e}",
            )

        quorum_met = harvestable >= QUORUM_MIN
        needs_expansion = (
            harvestable < QUORUM_SOFT
            and self._expansion_count < MAX_EXPANSIONS
        )
        # Absolutne minimum — zawsze expanduj
        if harvestable < QUORUM_HARD_MIN and self._expansion_count < MAX_EXPANSIONS:
            needs_expansion = True

        expansion_queries: List[str] = []
        if needs_expansion:
            expansion_queries = self._generate_expansion_queries(harvestable, total)
            self._record_expansion(harvestable)

        result = QuorumResult(
            mission_id=self.mission_id,
            harvestable_count=harvestable,
            total_sources=total,
            skip_count=skip_count,
            quorum_met=quorum_met,
            needs_expansion=needs_expansion,
            expansion_queries=expansion_queries,
            expansion_round=self._expansion_count,
        )
        result.report = result.summary()
        log.info(result.summary())
        return result

    def _generate_expansion_queries(self, current: int, total: int) -> List[str]:
        """
        Generuje dodatkowe queries dla zasięgu.
        Używa Bedrock jeśli dostępny, fallback na keyword-based templates.
        """
        keywords = _extract_keywords(self._topic)
        if not keywords:
            return []

        try:
            return self._bedrock_expansion(current, total)
        except Exception as e:
            log.debug(f"Bedrock expansion failed: {e} — using template fallback")
            return self._template_expansion(keywords)

    def _bedrock_expansion(self, current: int, total: int) -> List[str]:
        from behive.engine.llm import complete
        import json

        prompt = f"""Research topic: "{self._topic}"

The initial web scout returned only {current} harvestable sources out of {total} total.
This is below the quorum threshold of {QUORUM_MIN}. We need more high-quality sources.

Generate exactly {EXPANSION_QUERIES} alternative search queries to expand coverage.
Focus on:
1. Different angle or framing of the topic
2. Adjacent / related subtopics that will have data
3. Source-specific queries (site:statista.com, site:clutch.co, etc.)
4. Academic / industry report angle
5. Regional / language variant if applicable

Rules:
- Each query must be DIFFERENT from a standard direct search
- Prioritize queries likely to find quantitative data (numbers, stats, reports)
- Short queries work better than long ones

Return ONLY a JSON array of {EXPANSION_QUERIES} strings:
["query 1", "query 2", "query 3", "query 4", "query 5"]"""

        text = complete(prompt, stage="scout", max_tokens=500)
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        queries = json.loads(text)
        return [q for q in queries if isinstance(q, str) and len(q) > 5][:EXPANSION_QUERIES]

    def _template_expansion(self, keywords: List[str]) -> List[str]:
        """Fallback — keyword-based template queries."""
        kw = " ".join(keywords[:3])
        queries = [
            f'"{kw}" statistics 2024 2025',
            f'{kw} market report site:statista.com OR site:gartner.com',
            f'{kw} analysis filetype:pdf',
            f'{kw} industry overview annual report',
            f'"{keywords[0] if keywords else kw}" case study results',
        ]
        return queries[:EXPANSION_QUERIES]

    def _record_expansion(self, harvestable: int):
        """Zapisuje fakt ekspansji do DB (żeby nie loopować w nieskończoność)."""
        try:
            con = _pg_connect()
            con.execute("""
                CREATE TABLE IF NOT EXISTS hive_quorum_expansions (
                    id          VARCHAR DEFAULT gen_random_uuid()::VARCHAR PRIMARY KEY,
                    mission_id  VARCHAR NOT NULL,
                    round_num   INT,
                    harvestable INT,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """)
            con.execute("""
                INSERT INTO hive_quorum_expansions (mission_id, round_num, harvestable)
                VALUES (?, ?, ?)
            """, [self.mission_id, self._expansion_count + 1, harvestable])
            con.close()
            self._expansion_count += 1
        except Exception as e:
            log.debug(f"Quorum expansion record failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(f"Usage: python3 hive2_bionic_quorum.py <mission_id>")
        sys.exit(1)

    mission_id = sys.argv[1]
    qc = QuorumChecker(mission_id)
    result = qc.check()
    print(result.summary())
    if result.needs_expansion:
        print(f"\nExpansion queries ({len(result.expansion_queries)}):")
        for i, q in enumerate(result.expansion_queries, 1):
            print(f"  {i}. {q}")
