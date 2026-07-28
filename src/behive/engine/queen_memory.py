#!/usr/bin/env python3
"""
HIVE 2.0 — Queen Memory (Trwały wektor analityczny)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Doktryna z dokumentu:
  "idea ta musi stać się beeqeen — strukturą kluczową dla istnienia Ula
   implikującą wektory analityczne"

Beeqeen to NIE jest jednorazowy prompt. To jest PAMIĘĆ OPERACYJNA — struktura
która akumuluje wiedzę między misjami i kieruje wektory kolejnych misji.

Co robi ten moduł:
  1. RECALL — przed planowaniem nowej misji Queen ładuje z DuckDB:
       - FUIR (Follow-Up Intelligence Requirements) z poprzednich ocen
       - Luki badawcze (hive_gaps) z powiązanych misji
       - Kluczowe ustalenia (hive3_mission_memory) z podobnych tematów
       - Oceny Queen (hive_queen_assessments) — co już wiemy z pewnością

  2. CONTEXT INJECTION — kompiluje to w strukturę kontekstu dla planera:
       - "Wiemy to: [confirmed intelligence]"
       - "Nie wiemy tego: [intelligence voids]"
       - "Szukaj tego: [FUIR directives]"
       - "Nie powtarzaj: [already covered queries]"

  3. PERSIST — po misji zapisuje nowe ustalenia do pamięci

  4. CLUSTERING — grupuje misje tematycznie — Queen widzi 
     cały klaster wiedzy, nie tylko ostatnią misję

Używany przez QueenPlanner w hive2.py:
  memory = QueenMemory(topic)
  ctx = memory.recall()
  # ctx injektowany do Queen prompt → perfekcyjne instrukcje dla scoutów
"""

import json
import re
import time
from datetime import datetime, timezone
from typing import Optional

DB_PATH = os.environ.get("BEHIVE_LEGACY_DB", "")  # Legacy — PostgreSQL is primary

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _tokenize(text: str) -> set[str]:
    """Proste tokenizowanie do podobieństwa tematycznego."""
    stopwords = {
        "the", "a", "an", "and", "or", "of", "in", "for", "to", "on",
        "at", "by", "with", "from", "is", "are", "was", "were", "be",
        "i", "w", "z", "do", "na", "o", "się", "jako", "oraz", "czy",
    }
    tokens = re.findall(r'\b[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]{3,}\b', text.lower())
    return {t for t in tokens if t not in stopwords}


def _topic_similarity(topic_a: str, topic_b: str) -> float:
    """Jaccard similarity między dwoma tematami."""
    ta = _tokenize(topic_a)
    tb = _tokenize(topic_b)
    if not ta or not tb:
        return 0.0
    intersection = ta & tb
    union = ta | tb
    return len(intersection) / len(union)


def _duckdb_ro():
    _hive_db = __import__("hive2_db")
    for attempt in range(3):
        try:
            return _hive_db.connect(read_only=True)
        except Exception:
            time.sleep(1)
    _hive_db = __import__("hive2_db")
    return _hive_db.connect(read_only=True)


def _duckdb_rw():
    _hive_db = __import__("hive2_db")
    for attempt in range(3):
        try:
            return _hive_db.connect(read_only=False)
        except Exception:
            time.sleep(2 ** attempt)
    _hive_db = __import__("hive2_db")
    return _hive_db.connect(read_only=False)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# QueenMemoryContext — co Queen dostaje przed planowaniem
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class QueenMemoryContext:
    """
    Skompilowany kontekst pamięci dla Queen — przekazywany do planera.
    """
    def __init__(self):
        self.related_missions:   list[dict] = []   # poprzednie misje (topic, status, date)
        self.confirmed_facts:    list[str]  = []   # co wiemy z pewnością (CI z assessments)
        self.intelligence_voids: list[str]  = []   # czego nie wiemy (IV z assessments)
        self.fuir_directives:    list[dict] = []   # FUIR — konkretne pytania do zbadania
        self.open_gaps:          list[str]  = []   # luki z hive_gaps (nierozwiązane)
        self.covered_queries:    list[str]  = []   # zapytania już wykonane (nie powtarzać)
        self.key_findings:       list[str]  = []   # kluczowe ustalenia z mission_memory
        self.learned_patterns:   list[str]  = []   # wzorce zachowań (co działa, co nie)
        self.total_missions:     int        = 0    # ile misji w tym klastrze
        self.cluster_confidence: float      = 0.0  # ogólna pewność w tym temacie

    def is_empty(self) -> bool:
        return (not self.confirmed_facts and not self.intelligence_voids
                and not self.fuir_directives and not self.open_gaps)

    def to_prompt_block(self) -> str:
        """
        Kompiluje kontekst w blok tekstu dla Queen prompt.
        Format: czytelny dla LLM, maksymalnie informacyjny.
        """
        if self.is_empty():
            return ""

        lines = []
        lines.append("═" * 60)
        lines.append("QUEEN MEMORY — AKTYWNA PAMIĘĆ OPERACYJNA")
        lines.append("═" * 60)
        lines.append(
            f"Ten temat był badany {self.total_missions}× wcześniej. "
            f"Skumulowana pewność: {self.cluster_confidence:.0%}"
        )
        lines.append("")

        if self.confirmed_facts:
            lines.append("── POTWIERDZONA WIEDZA (nie zbieraj ponownie) ──")
            for i, f in enumerate(self.confirmed_facts[:8], 1):
                lines.append(f"  CI-{i}: {f}")
            lines.append("")

        if self.fuir_directives:
            lines.append("── KRYTYCZNE PYTANIA OTWARTE (FUIR — zbierz to!) ──")
            for d in self.fuir_directives[:10]:
                priority = d.get('priority', 'MEDIUM')
                req      = d.get('requirement', '')
                domain   = d.get('target_domain', '')
                method   = d.get('collection_method', '')
                lines.append(
                    f"  [{priority}] {req}\n"
                    f"    → Źródła: {domain}\n"
                    f"    → Metoda: {method}"
                )
            lines.append("")

        if self.intelligence_voids:
            lines.append("── LUKI BADAWCZE (obszary bez danych) ──")
            for i, v in enumerate(self.intelligence_voids[:6], 1):
                lines.append(f"  IV-{i}: {v}")
            lines.append("")

        if self.open_gaps:
            lines.append("── OTWARTE LUKI (zidentyfikowane, niezbadane) ──")
            for g in self.open_gaps[:5]:
                lines.append(f"  ○ {g[:120]}")
            lines.append("")

        if self.key_findings:
            lines.append("── KLUCZOWE USTALENIA Z POPRZEDNICH MISJI ──")
            for f in self.key_findings[:5]:
                lines.append(f"  ✓ {f}")
            lines.append("")

        if self.covered_queries:
            lines.append("── ZAPYTANIA JUŻ WYKONANE (nie powtarzaj) ──")
            for q in self.covered_queries[:10]:
                lines.append(f"  ✗ {q}")
            lines.append("")

        lines.append("═" * 60)
        lines.append(
            "INSTRUKCJA: Zbuduj plan tak by UZUPEŁNIAĆ luki (FUIR + IV), "
            "NIE powtarzać pokrytych tematów (CI), "
            "i TRAFIAĆ w zidentyfikowane źródła z FUIR directives."
        )
        lines.append("═" * 60)

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "total_missions":     self.total_missions,
            "cluster_confidence": self.cluster_confidence,
            "confirmed_facts":    self.confirmed_facts,
            "intelligence_voids": self.intelligence_voids,
            "fuir_directives":    self.fuir_directives,
            "open_gaps":          self.open_gaps,
            "key_findings":       self.key_findings,
            "covered_queries":    self.covered_queries,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# QueenMemory — główna klasa
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class QueenMemory:
    """
    Trwały wektor analityczny Queen — czyta i pisze pamięć operacyjną.

    Używanie:
        memory = QueenMemory(topic)
        ctx = memory.recall()
        prompt_block = ctx.to_prompt_block()
        # → wstrzyknij do Queen prompt przed planowaniem

        # Po zakończeniu misji:
        memory.persist(mission_id, findings, gaps, confidence)
    """

    SIMILARITY_THRESHOLD = 0.15   # min Jaccard do uznania misji za powiązaną
    MAX_RELATED_MISSIONS = 5      # max poprzednich misji do załadowania
    MAX_FUIR = 10                 # max FUIR dyrektyw per kontekst

    def __init__(self, topic: str):
        self.topic = topic
        self._topic_tokens = _tokenize(topic)

    # ── Główna metoda: załaduj wszystko co Queen powinna wiedzieć ────────────

    def recall(self) -> QueenMemoryContext:
        """
        Ładuje pełen kontekst pamięci dla bieżącego tematu.
        Zwraca QueenMemoryContext gotowy do wstrzyknięcia w prompt.
        """
        ctx = QueenMemoryContext()

        try:
            db = _duckdb_ro()

            # 1. Znajdź powiązane misje
            related = self._find_related_missions(db)
            ctx.related_missions = related
            ctx.total_missions = len(related)

            if not related:
                db.close()
                return ctx  # brak historii — świeży temat

            mission_ids = [m['id'] for m in related]

            # 2. Załaduj FUIR i Intelligence Voids z ocen Queen
            self._load_assessments(db, mission_ids, ctx)

            # 3. Załaduj otwarte luki
            self._load_gaps(db, mission_ids, ctx)

            # 4. Załaduj kluczowe ustalenia i wzorce
            self._load_mission_memory(db, mission_ids, ctx)

            # 5. Załaduj pokryte zapytania (nie powtarzać)
            self._load_covered_queries(db, mission_ids, ctx)

            # 6. Oblicz skumulowaną pewność
            confidences = [m.get('confidence', 0.0) for m in related if m.get('confidence')]
            ctx.cluster_confidence = (
                sum(confidences) / len(confidences) if confidences else 0.0
            )

            db.close()

        except Exception as e:
            print(f"  [QueenMemory] recall error (non-fatal): {e}")

        return ctx

    # ── Znajdź powiązane misje ───────────────────────────────────────────────

    def _find_related_missions(self, db) -> list[dict]:
        """
        Szuka misji o podobnym temacie (Jaccard similarity).
        Preferuje zakończone misje (status='done') i świeże.
        """
        try:
            rows = db.execute("""
                SELECT id, topic, status, created_at,
                       sources_harvested, total_facts
                FROM hive_missions
                WHERE status IN ('done', 'synth', 'process')
                  AND topic IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 100
            """).fetchall()
        except Exception:
            return []

        scored = []
        for row in rows:
            mid, topic, status, created_at, sources, facts = row
            sim = _topic_similarity(self.topic, topic or "")
            if sim >= self.SIMILARITY_THRESHOLD:
                scored.append({
                    "id":       mid,
                    "topic":    topic,
                    "status":   status,
                    "date":     str(created_at)[:16],
                    "sources":  sources or 0,
                    "facts":    facts or 0,
                    "sim":      round(sim, 3),
                    "confidence": None,  # wypełnione później z assessments
                })

        # Sortuj: im wyższe podobieństwo i świeższe, tym lepiej
        scored.sort(key=lambda x: (x['sim'], x['date']), reverse=True)
        return scored[:self.MAX_RELATED_MISSIONS]

    # ── Załaduj oceny Queen (FUIR + IV + CI) ────────────────────────────────

    def _load_assessments(self, db, mission_ids: list[str],
                          ctx: QueenMemoryContext) -> None:
        """
        Parsuje hive_queen_assessments.queen_json → ekstrahuje:
        - section_1: confirmed intelligence → ctx.confirmed_facts
        - section_3: intelligence voids → ctx.intelligence_voids
        - section_6: next_mission_directive (FUIR) → ctx.fuir_directives
        - hive_confidence_score → ctx.related_missions[i].confidence
        """
        if not mission_ids:
            return

        placeholders = ", ".join(["?" for _ in mission_ids])
        try:
            rows = db.execute(f"""
                SELECT mission_id, queen_json, must_know_insight,
                       confidence_calibrated, hausdorff_score
                FROM hive_queen_assessments
                WHERE mission_id IN ({placeholders})
                ORDER BY created_at DESC
            """, mission_ids).fetchall()
        except Exception:
            return

        seen_fuir: set[str] = set()

        for row in rows:
            mid, queen_json_str, must_know, conf, hausdorff = row

            # Zaktualizuj confidence w related_missions
            for m in ctx.related_missions:
                if m['id'] == mid and m['confidence'] is None:
                    m['confidence'] = float(conf or 0.0)

            if not queen_json_str:
                continue

            try:
                qj = json.loads(queen_json_str)
            except Exception:
                continue

            sections = qj.get('sections', {})

            # CI — potwierdzona wiedza
            for ci in sections.get('section_1_confirmed_intelligence', []):
                claim = ci.get('claim', '')
                if claim and float(ci.get('confidence', 0)) > 0.3:
                    ctx.confirmed_facts.append(claim)

            # IV — intelligence voids
            for iv in sections.get('section_3_intelligence_voids', []):
                void = iv.get('void', '')
                if void:
                    ctx.intelligence_voids.append(void)

            # FUIR — follow-up intelligence requirements
            for fuir in sections.get('section_6_next_mission_directive', []):
                req = fuir.get('requirement', '')
                key = req[:80]
                if req and key not in seen_fuir:
                    seen_fuir.add(key)
                    priority = fuir.get('priority', 'MEDIUM')
                    # Sortuj CRITICAL na górę
                    if len(ctx.fuir_directives) < self.MAX_FUIR:
                        ctx.fuir_directives.append({
                            'priority':          priority,
                            'requirement':       req,
                            'target_domain':     fuir.get('target_domain', ''),
                            'collection_method': fuir.get('collection_method', ''),
                            'mission_id':        mid,
                        })

        # Sortuj FUIR: CRITICAL > HIGH > MEDIUM
        priority_order = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}
        ctx.fuir_directives.sort(
            key=lambda x: priority_order.get(x.get('priority', 'LOW'), 9)
        )

    # ── Załaduj otwarte luki ─────────────────────────────────────────────────

    def _load_gaps(self, db, mission_ids: list[str],
                   ctx: QueenMemoryContext) -> None:
        """Ładuje nierozwiązane luki z hive_gaps."""
        if not mission_ids:
            return

        placeholders = ", ".join(["?" for _ in mission_ids])
        try:
            rows = db.execute(f"""
                SELECT gap_query, gap_type, priority
                FROM hive_gaps
                WHERE mission_id IN ({placeholders})
                  AND resolved = FALSE
                ORDER BY priority DESC
                LIMIT 15
            """, mission_ids).fetchall()
        except Exception:
            return

        for row in rows:
            gap_query, gap_type, priority = row
            if gap_query:
                ctx.open_gaps.append(gap_query)

    # ── Załaduj kluczowe ustalenia i wzorce ─────────────────────────────────

    def _load_mission_memory(self, db, mission_ids: list[str],
                              ctx: QueenMemoryContext) -> None:
        """Ładuje key_findings i learned_patterns z hive3_mission_memory."""
        if not mission_ids:
            return

        placeholders = ", ".join(["?" for _ in mission_ids])
        try:
            rows = db.execute(f"""
                SELECT mission_id, mission_topic, key_findings,
                       learned_patterns, confidence
                FROM hive3_mission_memory
                WHERE mission_id IN ({placeholders})
                ORDER BY confidence DESC
                LIMIT 10
            """, mission_ids).fetchall()
        except Exception:
            return

        for row in rows:
            mid, topic, key_findings_json, patterns_json, confidence = row

            if key_findings_json:
                try:
                    findings = json.loads(key_findings_json)
                    if isinstance(findings, list):
                        for f in findings:
                            fact = f.get('fact', '') if isinstance(f, dict) else str(f)
                            if fact:
                                ctx.key_findings.append(fact)
                    elif isinstance(findings, str):
                        ctx.key_findings.append(findings)
                except Exception:
                    pass

            if patterns_json:
                try:
                    patterns = json.loads(patterns_json)
                    if isinstance(patterns, list):
                        ctx.learned_patterns.extend(
                            [str(p) for p in patterns if p]
                        )
                except Exception:
                    pass

    # ── Załaduj pokryte zapytania ────────────────────────────────────────────

    def _load_covered_queries(self, db, mission_ids: list[str],
                               ctx: QueenMemoryContext) -> None:
        """
        Ładuje domeny i tytuły które były już zbierane w powiązanych misjach.
        Queen nie powinna generować identycznych ścieżek.
        """
        if not mission_ids:
            return

        placeholders = ", ".join(["?" for _ in mission_ids])
        try:
            rows = db.execute(f"""
                SELECT DISTINCT title, score_total
                FROM hive_sources
                WHERE mission_id IN ({placeholders})
                  AND title IS NOT NULL
                  AND score_total >= 40
                ORDER BY score_total DESC
                LIMIT 30
            """, mission_ids).fetchall()
        except Exception:
            return

        ctx.covered_queries = [r[0] for r in rows if r[0]]

    # ── Persist: zapisz ustalenia po misji ──────────────────────────────────

    def persist(self, mission_id: str, mission_topic: str,
                key_findings: list[dict], gaps: list[str],
                confidence: float, ops_executed: int = 0) -> None:
        """
        Zapisuje kluczowe ustalenia po zakończonej misji.
        Wywoływany przez hive2_synth.py po syntezie.
        """
        try:
            db = _duckdb_rw()

            # Upewnij się że tabela istnieje
            db.execute("""
                CREATE TABLE IF NOT EXISTS hive3_mission_memory (
                    id              INTEGER PRIMARY KEY,
                    mission_id      VARCHAR NOT NULL,
                    mission_topic   VARCHAR NOT NULL,
                    completed_at    TIMESTAMP,
                    tier_count      INTEGER DEFAULT 0,
                    ops_executed    INTEGER DEFAULT 0,
                    confidence      FLOAT DEFAULT 0.0,
                    key_findings    VARCHAR,
                    learned_patterns VARCHAR,
                    evolution_fitness FLOAT DEFAULT 0.0,
                    engrams         VARCHAR DEFAULT '[]',
                    immune_patterns VARCHAR DEFAULT '[]',
                    mycelium_edges  VARCHAR DEFAULT '[]',
                    tags            VARCHAR DEFAULT '[]',
                    created_at      TIMESTAMP DEFAULT NOW()
                )
            """)

            # Sprawdź czy już istnieje
            existing = db.execute(
                "SELECT id FROM hive3_mission_memory WHERE mission_id = ?",
                [mission_id]
            ).fetchone()

            if existing:
                db.execute("""
                    UPDATE hive3_mission_memory
                    SET key_findings = ?, ops_executed = ?, confidence = ?,
                        completed_at = NOW()
                    WHERE mission_id = ?
                """, [
                    json.dumps(key_findings, ensure_ascii=False),
                    ops_executed,
                    confidence,
                    mission_id,
                ])
            else:
                # Generuj tagi z tematu
                tags = list(_tokenize(mission_topic))[:10]
                # Learned patterns: derive from key_findings
                learned = [
                    f"Topic '{mission_topic[:50]}': "
                    f"{f.get('claim', f.get('finding', ''))[:120]}"
                    for f in key_findings[:5]
                    if isinstance(f, dict)
                ]
                db.execute("""
                    INSERT INTO hive3_mission_memory
                    (mission_id, mission_topic, completed_at, ops_executed,
                     confidence, key_findings, learned_patterns, tags, evolution_fitness)
                    VALUES (?, ?, NOW(), ?, ?, ?, ?, ?, ?)
                """, [
                    mission_id,
                    mission_topic,
                    ops_executed,
                    confidence,
                    json.dumps(key_findings, ensure_ascii=False),
                    json.dumps(learned, ensure_ascii=False),
                    json.dumps(tags),
                    confidence * 0.9,
                ])

            db.commit()
            db.close()

        except Exception as e:
            print(f"  [QueenMemory] persist error (non-fatal): {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI — diagnostyka pamięci Queen
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 hive2_queen_memory.py \"topic\" [--verbose]")
        sys.exit(1)

    topic   = sys.argv[1]
    verbose = "--verbose" in sys.argv

    print(f"\n👑 QUEEN MEMORY RECALL — topic: \"{topic}\"\n")

    memory = QueenMemory(topic)
    ctx    = memory.recall()

    if ctx.is_empty():
        print("  [EMPTY] Brak historycznych danych dla tego tematu.")
        print("  → Queen zaczyna od zera — brak amnezji bo brak historii.\n")
    else:
        print(f"  Powiązane misje:     {ctx.total_missions}")
        print(f"  Potwierdzone fakty:  {len(ctx.confirmed_facts)}")
        print(f"  Luki (IV):           {len(ctx.intelligence_voids)}")
        print(f"  FUIR dyrektyw:       {len(ctx.fuir_directives)}")
        print(f"  Otwarte luki:        {len(ctx.open_gaps)}")
        print(f"  Pokryte zapytania:   {len(ctx.covered_queries)}")
        print(f"  Klaster pewność:     {ctx.cluster_confidence:.0%}")
        print()

        if verbose:
            print(ctx.to_prompt_block())
        else:
            print("  (--verbose aby zobaczyć pełen prompt block)\n")

        if ctx.fuir_directives:
            print("  TOP FUIR (krytyczne pytania otwarte):")
            for d in ctx.fuir_directives[:3]:
                print(f"    [{d['priority']}] {d['requirement'][:100]}")
