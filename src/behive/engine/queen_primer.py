#!/usr/bin/env python3
"""
HIVE 2.0 — hive2_queen_primer.py
══════════════════════════════════
QMP Primer System — analog Queen Mandibular Pheromone długoterminowej (primer pheromone).

Biologia:
  Ekspozycja na QMP w 1. dniu życia pszczoły zmienia ekspresję genów na całe jej życie.
  Matka nie musi być obecna — jej "primer" przetrwa.

W HIVE:
  Po syntezie Queen zapisuje PRIMER do DuckDB.
  Następna misja na podobny temat ładuje primer jako boosted seeds + enhanced queries.
  Primer działa jak "wzmocnione DNA" dla nowej misji — skraca czas cold-start.

Tabela: hive_queen_primers
  mission_id, topic, topic_embedding_hash, top_domains (JSON), 
  recommended_queries (JSON), depth_modifier FLOAT, primer_quality FLOAT, created_at

Public API:
    from hive2_queen_primer import QueenPrimer
    primer = QueenPrimer(mission_id)

    # Po syntezie — zapisz primer
    primer.save(synthesis_result, top_claims, top_domains)

    # Przed kolejną misją — załaduj
    p = primer.load_for_topic("nowy temat o podobnej dziedzinie")
    if p:
        boosted_domains = p.top_domains
        seed_queries    = p.recommended_queries
        depth_mod       = p.depth_modifier  # 0.8–1.3
"""

from __future__ import annotations

import os
import json
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List

log = logging.getLogger("hive2_queen_primer")
DB_PATH = os.environ.get("BEHIVE_LEGACY_DB", "")  # Legacy — PostgreSQL is primary

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hive_queen_primers (
    id                  VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::VARCHAR,
    mission_id          VARCHAR NOT NULL,
    topic               VARCHAR NOT NULL,
    topic_keywords      JSON,           -- extracted topic keywords
    top_domains         JSON,           -- top 15 domains by quality
    recommended_queries JSON,           -- top 5 gap queries for follow-up
    key_entities        JSON,           -- top entities from synthesis
    depth_modifier      FLOAT DEFAULT 1.0,  -- 0.8–1.3 based on quality
    primer_quality      FLOAT DEFAULT 0.5,  -- how good was this synthesis
    created_at          TIMESTAMP DEFAULT NOW()
);
"""

# Similarity — wystarczą shared keywords (bez embedding dla szybkości)
_MIN_KEYWORD_OVERLAP = 0.25   # 25% shared keywords → "related topic"


@dataclass
class Primer:
    mission_id:          str
    topic:               str
    top_domains:         List[str] = field(default_factory=list)
    recommended_queries: List[str] = field(default_factory=list)
    key_entities:        List[str] = field(default_factory=list)
    depth_modifier:      float = 1.0
    primer_quality:      float = 0.5
    created_at:          Optional[datetime] = None

    @property
    def is_high_quality(self) -> bool:
        return self.primer_quality >= 0.65

    def to_prompt_block(self) -> str:
        """Wstrzyknij do Queen planning prompt jako boosted context."""
        if not self.top_domains and not self.recommended_queries:
            return ""
        lines = ["[QMP PRIMER — Related Mission Intelligence]"]
        if self.top_domains:
            lines.append(f"Trusted domains from prior research: {', '.join(self.top_domains[:8])}")
        if self.recommended_queries:
            lines.append("Follow-up queries identified as high-value gaps:")
            for q in self.recommended_queries[:5]:
                lines.append(f"  • {q}")
        if self.key_entities:
            lines.append(f"Key entities to track: {', '.join(self.key_entities[:10])}")
        lines.append(f"Depth modifier: {self.depth_modifier:.1f}x (based on prior coverage quality)")
        return "\n".join(lines)


def _extract_keywords(topic: str) -> List[str]:
    """Extract meaningful keywords from topic string."""
    _STOP = {
        "the", "and", "for", "with", "that", "this", "are", "from", "have",
        "more", "also", "will", "been", "can", "not", "was", "jak", "jest",
        "dla", "oraz", "sie", "przez", "jako", "nie", "roku",
    }
    words = re.findall(r'[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]{4,}', topic.lower())
    return [w for w in words if w not in _STOP]


def _keyword_overlap(kw1: List[str], kw2: List[str]) -> float:
    if not kw1 or not kw2:
        return 0.0
    s1, s2 = set(kw1), set(kw2)
    intersection = s1 & s2
    union = s1 | s2
    return len(intersection) / len(union)


def _duckdb_connect(read_only: bool = False):
    """Connect via hive2_db (PostgreSQL)."""
    import hive2_db as _hive_db
    return _hive_db.connect(read_only=read_only)


class QueenPrimer:
    """
    Saves and loads mission primers — the Queen's long-term pheromone memory.
    """

    def __init__(self, mission_id: str):
        self.mission_id = mission_id
        self._ensure_schema()

    def _ensure_schema(self):
        try:
            con = _duckdb_connect()
            con.execute(_SCHEMA_SQL)
            con.close()
        except Exception as e:
            log.warning(f"Queen primer schema init failed: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # SAVE — wywołaj po syntezie
    # ─────────────────────────────────────────────────────────────────────

    def save(
        self,
        topic: str,
        synthesis_text: str = "",
        quality_score: float = 0.5,
    ):
        """
        Builds and saves primer from current mission's DB data.
        Wywołaj na końcu syntezy.
        """
        try:
            top_domains         = self._get_top_domains()
            recommended_queries = self._get_gap_queries()
            key_entities        = self._get_key_entities()
            keywords            = _extract_keywords(topic)
            depth_mod           = self._compute_depth_modifier(quality_score)

            con = _duckdb_connect()
            con.execute(_SCHEMA_SQL)
            con.execute("""
                INSERT INTO hive_queen_primers
                    (mission_id, topic, topic_keywords, top_domains,
                     recommended_queries, key_entities, depth_modifier,
                     primer_quality, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
            """, [
                self.mission_id, topic,
                json.dumps(keywords),
                json.dumps(top_domains),
                json.dumps(recommended_queries),
                json.dumps(key_entities),
                depth_mod,
                quality_score,
                datetime.now(timezone.utc),
            ])
            con.close()
            log.info(
                f"🐝 QMP Primer saved: {len(top_domains)} domains, "
                f"{len(recommended_queries)} queries, depth={depth_mod:.1f}x"
            )
        except Exception as e:
            log.warning(f"Queen primer save failed: {e}")

    def _get_top_domains(self, limit: int = 15) -> List[str]:
        try:
            con = _duckdb_connect(read_only=True)
            rows = con.execute("""
                SELECT domain,
                       COUNT(*) as cnt,
                       AVG(CASE WHEN raw_text IS NOT NULL AND LENGTH(raw_text) > 500
                               THEN 1.0 ELSE 0.2 END) AS quality
                FROM (
                    SELECT regexp_replace(
                               regexp_replace(url, '^https?://(www\\.)?', ''),
                               '/.*', ''
                           ) AS domain,
                           raw_text
                    FROM hive_content
                    WHERE mission_id = ?
                ) sub
                GROUP BY domain
                HAVING cnt >= 1
                ORDER BY quality DESC, cnt DESC
                LIMIT ?
            """, [self.mission_id, limit]).fetchall()
            con.close()
            result = []
            for row in rows:
                d = row[0].split("/")[0].split("?")[0].strip()
                if d and len(d) > 4:
                    result.append(d)
            return result[:limit]
        except Exception as e:
            log.debug(f"get_top_domains failed: {e}")
            return []

    def _get_gap_queries(self, limit: int = 8) -> List[str]:
        try:
            con = _duckdb_connect(read_only=True)
            rows = con.execute("""
                SELECT gap_query
                FROM hive_gaps
                WHERE mission_id = ?
                  AND gap_type != 'quality_warning'
                ORDER BY priority DESC
                LIMIT ?
            """, [self.mission_id, limit]).fetchall()
            con.close()
            return [r[0] for r in rows if r[0]]
        except Exception as e:
            log.debug(f"get_gap_queries failed: {e}")
            return []

    def _get_key_entities(self, limit: int = 20) -> List[str]:
        try:
            con = _duckdb_connect(read_only=True)
            rows = con.execute("""
                SELECT value, COUNT(*) as cnt
                FROM hive_entities
                WHERE mission_id = ?
                  AND entity_type IN ('person','organization','company','product','technology')
                GROUP BY value
                ORDER BY cnt DESC
                LIMIT ?
            """, [self.mission_id, limit]).fetchall()
            con.close()
            return [r[0] for r in rows if r[0] and len(r[0]) > 2]
        except Exception as e:
            log.debug(f"get_key_entities failed: {e}")
            return []

    def _compute_depth_modifier(self, quality_score: float) -> float:
        """
        Wysoka jakość → następna misja może być płytsza (wiemy już dużo).
        Niska jakość → następna misja powinna być głębsza.
        """
        if quality_score >= 0.75:
            return 0.85   # coverage dobra — nie przepracowuj
        elif quality_score >= 0.55:
            return 1.0    # neutralny
        elif quality_score >= 0.35:
            return 1.15   # copy niedokładna — idź głębiej
        else:
            return 1.30   # słaba coverage — pełna eksploracja

    # ─────────────────────────────────────────────────────────────────────
    # LOAD — wywołaj przed planowaniem nowej misji
    # ─────────────────────────────────────────────────────────────────────

    def load_for_topic(self, new_topic: str, max_age_days: int = 30) -> Optional[Primer]:
        """
        Szuka primerów z poprzednich misji na podobne tematy.
        Zwraca najlepszy pasujący primer lub None.
        """
        try:
            new_kw = _extract_keywords(new_topic)
            if not new_kw:
                return None

            con = _duckdb_connect(read_only=True)
            rows = con.execute("""
                SELECT mission_id, topic, topic_keywords, top_domains,
                       recommended_queries, key_entities, depth_modifier,
                       primer_quality, created_at
                FROM hive_queen_primers
                WHERE mission_id != ?
                  AND created_at >= NOW() - INTERVAL (?) DAY
                ORDER BY created_at DESC
                LIMIT 50
            """, [self.mission_id, max_age_days]).fetchall()
            con.close()

            best_primer = None
            best_overlap = _MIN_KEYWORD_OVERLAP

            for row in rows:
                try:
                    stored_kw = json.loads(row[2] or "[]")
                    overlap = _keyword_overlap(new_kw, stored_kw)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_primer = Primer(
                            mission_id          = row[0],
                            topic               = row[1],
                            top_domains         = json.loads(row[3] or "[]"),
                            recommended_queries = json.loads(row[4] or "[]"),
                            key_entities        = json.loads(row[5] or "[]"),
                            depth_modifier      = float(row[6] or 1.0),
                            primer_quality      = float(row[7] or 0.5),
                            created_at          = row[8],
                        )
                except Exception:
                    continue

            if best_primer:
                log.info(
                    f"🐝 QMP Primer loaded from mission {best_primer.mission_id[:12]}… "
                    f"(overlap={best_overlap:.0%}, {len(best_primer.top_domains)} domains)"
                )
            return best_primer

        except Exception as e:
            log.warning(f"Queen primer load failed: {e}")
            return None


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, duckdb

    if "--list" in sys.argv or len(sys.argv) == 1:
        try:
            con = _duckdb_connect(read_only=True)
            rows = con.execute("""
                SELECT mission_id, topic, primer_quality, depth_modifier,
                       JSON_ARRAY_LENGTH(top_domains),
                       JSON_ARRAY_LENGTH(recommended_queries),
                       created_at
                FROM hive_queen_primers
                ORDER BY created_at DESC LIMIT 20
            """).fetchall()
            con.close()
            print(f"\n🐝 QMP PRIMERS ({len(rows)} records)\n")
            for r in rows:
                print(
                    f"  {r[0][:16]}…  q={r[2]:.2f}  depth={r[3]:.1f}x  "
                    f"domains={r[4]}  queries={r[5]}  "
                    f"{str(r[6])[:16]}  {r[1][:60]}"
                )
        except Exception as e:
            print(f"Error listing primers: {e}")
    elif "--test-load" in sys.argv:
        idx = sys.argv.index("--test-load")
        test_topic = " ".join(sys.argv[idx+1:]) if len(sys.argv) > idx+1 else "test topic"
        qp = QueenPrimer("test_mission_xyz")
        primer = qp.load_for_topic(test_topic)
        if primer:
            print(f"Found primer: {primer.mission_id}")
            print(primer.to_prompt_block())
        else:
            print(f"No primer found for: {test_topic}")
