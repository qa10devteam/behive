"""
HIVE 2.0 — Queen Semantic Fact Memory (P5)
============================================
Inspiracja: mem0 V3 pipeline (mem0/memory/main.py + mem0/configs/prompts.py linia 468)

Uzupełnia istniejący hive2_queen_memory.py o warstwę SEMANTYCZNĄ:
  hive2_queen_memory.py  → pamięć STRUKTURALNA (misje, gaps, FUIR, CI, IV)
  hive2_queen_fact_mem.py → pamięć SEMANTYCZNA (fakty z tekstu, vector search)

Architektura — wzorzec z mem0 V3:
  add(text)   → 1 LLM call (Haiku) → ekstrahuje fakty → embed → DuckDB hive_queen_facts
  search(q)   → embed query → cosine similarity + BM25 → hybrid ranking → top-k faktów
  recall(q)   → search() + formatowanie do bloku promptu

Tabela: hive_queen_facts
  id          : SHA256[:32] (hash faktu)
  fact_text   : wyekstrahowany fakt (1-2 zdania)
  source_type : 'synthesis'|'session'|'claim'|'manual'
  mission_id  : opcjonalnie — powiązana misja
  agent_id    : 'queen' (izolacja per agent)
  run_id      : UUID sesji (izolacja per run)
  embedding   : FLOAT[512] — Titan embed-text-v2
  created_at  : TIMESTAMP
  linked_ids  : VARCHAR — JSON lista powiązanych fact_id (conflict linking)

Różnica od QueenMemory:
  QueenMemory.recall()       → "misje o temacie X istniały, znaleziono: ..."
  QueenFactMemory.search()   → "fakty semantycznie bliskie query: ..."

Użycie:
  from hive2_queen_fact_mem import QueenFactMemory

  mem = QueenFactMemory()

  # Po syntezie misji — zapamiętaj fakty
  mem.add(synthesis_text, source_type="synthesis", mission_id="m-123")

  # Przed planowaniem nowej misji — przypomnij relevantne fakty
  facts = mem.search("przychody Allegro Q3 2025", top_k=8)
  prompt_block = mem.format_for_prompt(facts)
"""

from __future__ import annotations

import os
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

import boto3
import hive2_db as _hive_db  # PostgreSQL via unified layer

log = logging.getLogger(__name__)

DUCKDB_PATH = os.environ.get("BEHIVE_LEGACY_DB", "")  # Legacy — PostgreSQL is primary
BEDROCK_REGION = "eu-central-1"
EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBED_DIMS     = 512
HAIKU_MODEL_ID = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"

# Wzorzec z mem0/configs/prompts.py linia 468 — ADDITIVE_EXTRACTION_PROMPT
# Zmodyfikowany do HIVE: wyciąga fakty z synthów/raportów wywiadowczych
_FACT_EXTRACTION_PROMPT = """\
You are an intelligence analyst extracting precise, reusable facts from research text.

Extract ONLY concrete, verifiable facts. Each fact should be:
- Self-contained (understandable without surrounding context)
- Specific (has numbers, names, dates, percentages where available)
- Unique (no duplicates or paraphrases of the same fact)

EXCLUDE:
- Opinions, predictions, uncertain statements
- Meta-commentary about the research process
- Vague generalizations without supporting data

Return a JSON array of fact strings. Maximum 12 facts.
Return ONLY the JSON array, no explanation.

Example output:
["Allegro GMV grew 18.2% YoY in Q2 2025 to PLN 14.3B",
 "Amazon entered Polish market in 2021 with amazon.pl",
 "Polish e-commerce penetration reached 67% of internet users in 2024"]

Research text to extract from:
{text}
"""

# Wzorzec z mem0: conflict linking — wyciągamy ID powiązanych faktów
_CONFLICT_CHECK_PROMPT = """\
Given this new fact and a list of existing facts, identify which existing facts
are CONTRADICTED or SUPERSEDED by the new fact.
Return a JSON array of the IDs of conflicting facts, or an empty array [] if none.
Return ONLY the JSON array.

New fact: {new_fact}

Existing facts:
{existing_facts_json}
"""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hive_queen_facts (
    id           VARCHAR PRIMARY KEY,
    fact_text    VARCHAR NOT NULL,
    source_type  VARCHAR DEFAULT 'synthesis',
    mission_id   VARCHAR DEFAULT '',
    agent_id     VARCHAR DEFAULT 'queen',
    run_id       VARCHAR DEFAULT '',
    embedding    FLOAT[{dims}],
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    linked_ids   VARCHAR DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_queen_facts_agent
    ON hive_queen_facts(agent_id);

CREATE INDEX IF NOT EXISTS idx_queen_facts_mission
    ON hive_queen_facts(mission_id);
""".format(dims=EMBED_DIMS)

# FTS index — tworzony osobno (DDL nie może być w CREATE TABLE)
_FTS_INDEX_SQL = "PRAGMA create_fts_index(hive_queen_facts, id, fact_text, overwrite=1)"


# ---------------------------------------------------------------------------
# Bedrock helpers
# ---------------------------------------------------------------------------

def _embed(text: str) -> List[float]:
    bc = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    resp = bc.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=json.dumps({
            "inputText": text[:8000],
            "dimensions": EMBED_DIMS,   # wymuszamy 512 — domyślnie Titan v2 zwraca 1024
            "normalize": True,          # normalizacja dla cosine similarity
        }),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(resp["body"].read())["embedding"]


def _haiku(prompt: str, max_tokens: int = 600) -> Optional[str]:
    bc = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    })
    try:
        resp = bc.invoke_model(modelId=HAIKU_MODEL_ID, body=body)
        return json.loads(resp["body"].read())["content"][0]["text"].strip()
    except Exception as exc:
        log.warning("Haiku call failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# FactRecord
# ---------------------------------------------------------------------------

@dataclass
class FactRecord:
    id: str
    fact_text: str
    source_type: str
    mission_id: str
    agent_id: str
    run_id: str
    created_at: str
    score: float = 0.0       # similarity score przy retrieval


# ---------------------------------------------------------------------------
# QueenFactMemory
# ---------------------------------------------------------------------------

class QueenFactMemory:
    """
    Semantyczna warstwa pamięci faktów dla Queen.

    Wzorzec mem0 V3:
      - Addytywna ekstrakcja (nowe fakty linkowane do starych, nie nadpisywane)
      - Hybrid search: vector cosine + BM25 FTS
      - Izolacja: agent_id="queen", run_id=<session_uuid>

    Komplementarny do QueenMemory (strukturalny) z hive2_queen_memory.py.
    """

    AGENT_ID = "queen"

    def __init__(self, db_path: str = DUCKDB_PATH):
        self.db_path = db_path
        self._con: Optional[Any] = None
        self._fts_ready = False

    def _get_con(self) -> Any:
        if self._con is None:
            self._con = __import__("hive2_db").connect(read_only=False)
            self._ensure_schema()
        return self._con

    def _ensure_schema(self) -> None:
        con = self._con
        con.execute(_SCHEMA_SQL)
        # FTS index — graceful fail (może już istnieć lub nie być dostępny)
        try:
            con.execute("INSTALL fts; LOAD fts;")
            con.execute(_FTS_INDEX_SQL)
            self._fts_ready = True
        except Exception as exc:
            log.debug("FTS index for hive_queen_facts: %s", exc)

    def _fact_id(self, fact_text: str) -> str:
        """Deterministyczny ID faktu — SHA256[:32] z tekstu."""
        return hashlib.sha256(fact_text.strip().lower().encode()).hexdigest()[:32]

    # ------------------------------------------------------------------
    # add() — wzorzec mem0 V3 _add_to_vector_store()
    # ------------------------------------------------------------------

    def add(
        self,
        text: str,
        source_type: str = "synthesis",
        mission_id: str = "",
        run_id: str = "",
        infer: bool = True,
        max_facts: int = 12,
    ) -> int:
        """
        Ekstrahuje fakty z tekstu i zapisuje do hive_queen_facts.

        Wzorzec mem0 V3 pipeline:
          1. LLM extraction (1 Haiku call) → lista faktów
          2. MD5 dedup (bez LLM)
          3. Batch embed → DuckDB insert
          4. Linked IDs — powiązanie z istniejącymi

        Parameters
        ----------
        text        : tekst do analizy (synthesis, raport, notatka)
        source_type : 'synthesis'|'session'|'claim'|'manual'
        mission_id  : ID misji (do filtrowania)
        run_id      : UUID sesji (do izolacji per run)
        infer       : True = LLM extraction, False = traktuj text jako gotowy fakt
        max_facts   : max faktów z jednego tekstu

        Returns
        -------
        Liczba zapisanych (nowych) faktów
        """
        con = self._get_con()

        if not infer:
            # Raw insert — text jako jeden fakt
            facts = [text.strip()]
        else:
            facts = self._extract_facts(text, max_facts=max_facts)
            if not facts:
                log.warning("QueenFactMemory.add: no facts extracted from text (len=%d)", len(text))
                return 0

        log.info("QueenFactMemory.add: %d facts extracted, embedding...", len(facts))

        # Pobierz istniejące ID (do dedup)
        existing_ids = set(
            row[0] for row in con.execute(
                "SELECT id FROM hive_queen_facts WHERE agent_id=?", [self.AGENT_ID]
            ).fetchall()
        )

        saved = 0
        for fact_text in facts[:max_facts]:
            fact_id = self._fact_id(fact_text)

            # Dedup MD5-style — bez LLM
            if fact_id in existing_ids:
                log.debug("QueenFactMemory: skipping duplicate fact %s", fact_id[:8])
                continue

            try:
                embedding = _embed(fact_text)
            except Exception as exc:
                log.warning("QueenFactMemory: embed failed for fact: %s", exc)
                continue

            con.execute(
                """
                INSERT INTO hive_queen_facts
                    (id, fact_text, source_type, mission_id, agent_id, run_id, embedding, linked_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?::FLOAT[512], ?)
                ON CONFLICT (id) DO NOTHING
                """,
                [fact_id, fact_text, source_type, mission_id, self.AGENT_ID, run_id,
                 embedding, "[]"],
            )
            existing_ids.add(fact_id)
            saved += 1
            log.debug("QueenFactMemory: saved fact %s: %s...", fact_id[:8], fact_text[:60])

        # Odśwież FTS index po batch insert
        if saved > 0 and self._fts_ready:
            try:
                con.execute(_FTS_INDEX_SQL)
            except Exception:
                pass

        log.info("QueenFactMemory.add: saved %d/%d facts", saved, len(facts))
        return saved

    def _extract_facts(self, text: str, max_facts: int = 12) -> List[str]:
        """1 Haiku call → lista faktów. Wzorzec mem0 ADDITIVE_EXTRACTION_PROMPT."""
        # Ogranicz długość tekstu (Haiku context window)
        truncated = text[:6000] if len(text) > 6000 else text
        prompt = _FACT_EXTRACTION_PROMPT.format(text=truncated)
        result = _haiku(prompt, max_tokens=800)
        if not result:
            return []

        # Parse JSON — graceful fallback
        try:
            # Wyciągnij JSON array z odpowiedzi (może być owinięty w markdown)
            json_match = _find_json_array(result)
            if json_match:
                facts = json.loads(json_match)
                return [str(f).strip() for f in facts if isinstance(f, str) and len(f) > 15]
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("QueenFactMemory: JSON parse failed: %s | raw: %s...", exc, result[:100])

        # Fallback: linia po linii
        lines = [l.strip().lstrip("•-*123456789.) ") for l in result.split("\n")]
        return [l for l in lines if len(l) > 20 and not l.startswith("[")][:max_facts]

    # ------------------------------------------------------------------
    # search() — hybrid BM25 + vector
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 8,
        agent_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        min_score: float = 0.35,   # P5 fix: 0.60 filtrowało ~60% relevantnych faktów (Titan v2 512d)
    ) -> List[FactRecord]:
        """
        Hybrid semantic search po faktach.

        Wzorzec mem0 _search_vector_store():
          1. Embed query → cosine similarity
          2. BM25 FTS search (jeśli dostępne)
          3. RRF fusion → ranking

        Parameters
        ----------
        query      : zapytanie
        top_k      : ile faktów zwrócić
        agent_id   : filtr agent (domyślnie self.AGENT_ID)
        mission_id : opcjonalny filtr misji
        min_score  : minimalny cosine score (0-1)
        """
        con = self._get_con()
        agent = agent_id or self.AGENT_ID

        try:
            q_emb = _embed(query)
        except Exception as exc:
            log.warning("QueenFactMemory.search: embed failed: %s", exc)
            return []

        # Buduj WHERE clause
        where_parts = ["agent_id = ?"]
        params_vec = [q_emb, agent]
        if mission_id:
            where_parts.append("mission_id = ?")
            params_vec.append(mission_id)
        where_sql = " AND ".join(where_parts)

        # Vector search
        vec_rows = con.execute(
            f"""
            SELECT id, fact_text, source_type, mission_id, agent_id, run_id,
                   strftime(created_at, '%Y-%m-%d %H:%M'),
                   array_cosine_similarity(embedding, ?::FLOAT[{EMBED_DIMS}]) AS sim
            FROM hive_queen_facts
            WHERE {where_sql}
              AND embedding IS NOT NULL
            ORDER BY sim DESC
            LIMIT ?
            """,
            params_vec + [top_k * 3],
        ).fetchall()

        vec_results = {
            row[0]: (row, row[7]) for row in vec_rows if row[7] is not None and row[7] >= min_score
        }

        # BM25 search (jeśli FTS dostępne)
        bm25_results = {}
        if self._fts_ready:
            try:
                bm25_rows = con.execute(
                    f"""
                    SELECT id, score
                    FROM (
                        SELECT id, fts_main_hive_queen_facts.match_bm25(id, ?) AS score
                        FROM hive_queen_facts
                        WHERE {where_sql.replace('?', '?', 1)}
                    ) t
                    WHERE score IS NOT NULL
                    ORDER BY score DESC
                    LIMIT ?
                    """,
                    [query] + params_vec[1:] + [top_k * 3],
                ).fetchall()
                bm25_results = {row[0]: row[1] for row in bm25_rows}
            except Exception as exc:
                log.debug("QueenFactMemory BM25 search failed: %s", exc)

        # RRF Fusion
        if bm25_results:
            rrf_k = 60
            all_ids = set(vec_results) | set(bm25_results)
            vec_ranked  = sorted(vec_results, key=lambda x: vec_results[x][1], reverse=True)
            bm25_ranked = sorted(bm25_results, key=lambda x: bm25_results[x], reverse=True)

            vec_rank  = {cid: i for i, cid in enumerate(vec_ranked)}
            bm25_rank = {cid: i for i, cid in enumerate(bm25_ranked)}

            rrf_scores = {
                cid: (
                    1.0 / (rrf_k + vec_rank.get(cid, len(vec_ranked))) +
                    1.0 / (rrf_k + bm25_rank.get(cid, len(bm25_ranked)))
                )
                for cid in all_ids
            }
            sorted_ids = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)[:top_k]
        else:
            sorted_ids = sorted(vec_results, key=lambda x: vec_results[x][1], reverse=True)[:top_k]

        # Zbierz brakujące rekordy (mogą być z BM25, bez embedu powyżej progu)
        missing_ids = [cid for cid in sorted_ids if cid not in vec_results]
        if missing_ids:
            placeholders = ",".join(["?"] * len(missing_ids))
            extra = con.execute(
                f"SELECT id, fact_text, source_type, mission_id, agent_id, run_id, strftime(created_at, '%Y-%m-%d %H:%M'), 0.0 FROM hive_queen_facts WHERE id IN ({placeholders})",
                missing_ids,
            ).fetchall()
            for row in extra:
                vec_results[row[0]] = (row, 0.0)

        # Buduj wynik
        facts = []
        for fact_id in sorted_ids:
            if fact_id not in vec_results:
                continue
            row, score = vec_results[fact_id]
            facts.append(FactRecord(
                id=row[0], fact_text=row[1], source_type=row[2],
                mission_id=row[3], agent_id=row[4], run_id=row[5],
                created_at=row[6], score=score,
            ))

        log.info("QueenFactMemory.search: %d facts for query: %s...", len(facts), query[:60])
        return facts

    # ------------------------------------------------------------------
    # format_for_prompt() — Named Blocks wzorzec z Letta
    # ------------------------------------------------------------------

    def format_for_prompt(
        self,
        facts: List[FactRecord],
        header: str = "QUEEN SEMANTIC MEMORY — RELEVANTNE FAKTY",
        max_facts: int = 10,
    ) -> str:
        """
        Formatuje fakty do bloku XML dla Queen prompt.
        Wzorzec Named Blocks z Letta (Block → XML rendering w system prompt).
        """
        if not facts:
            return ""

        lines = [
            "═" * 60,
            header,
            "═" * 60,
        ]
        for i, f in enumerate(facts[:max_facts], 1):
            source = f"[{f.source_type}"
            if f.mission_id:
                source += f" | misja:{f.mission_id[:8]}"
            source += f" | {f.created_at[:10]}]"
            score_str = f" (relevance: {f.score:.2f})" if f.score > 0 else ""
            lines.append(f"  F-{i}{score_str}: {f.fact_text}")
            lines.append(f"       {source}")
        lines.append("═" * 60)
        lines.append(
            "INSTRUKCJA: Użyj tych faktów jako punkt wyjścia — "
            "nie zbieraj danych które tu są, uzupełniaj luki."
        )
        lines.append("═" * 60)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # recall() — combined QueenMemory integration point
    # ------------------------------------------------------------------

    def recall(
        self,
        query: str,
        top_k: int = 8,
        mission_id: Optional[str] = None,
    ) -> str:
        """
        Shortcut: search() + format_for_prompt() → gotowy blok do Queen prompt.
        """
        facts = self.search(query, top_k=top_k, mission_id=mission_id)
        return self.format_for_prompt(facts)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        con = self._get_con()
        row = con.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT mission_id), MIN(created_at), MAX(created_at)
            FROM hive_queen_facts WHERE agent_id=?
            """,
            [self.AGENT_ID],
        ).fetchone()
        return {
            "total_facts": row[0],
            "missions_covered": row[1],
            "oldest": str(row[2]) if row[2] else None,
            "newest": str(row[3]) if row[3] else None,
        }

    def close(self) -> None:
        if self._con:
            self._con.close()
            self._con = None


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _find_json_array(text: str) -> Optional[str]:
    """Wyciąga pierwszy JSON array [...] z tekstu (obsługuje markdown code blocks)."""
    import re
    # Usuń markdown backticks
    text = re.sub(r"```(?:json)?", "", text).strip()
    # Znajdź [...] 
    start = text.find("[")
    end   = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return None


# ---------------------------------------------------------------------------
# CLI test / smoke test (bez Bedrock — mock)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("=== QueenFactMemory — Schema test ===")

    # Test z in-memory DuckDB (bez Bedrock)
    test_con = __import__("hive2_db").connect(read_only=False)

    # Ręczne schema (bez HNSW, tylko cos similarity przez brute force)
    test_con.execute("""
        CREATE TABLE hive_queen_facts (
            id VARCHAR PRIMARY KEY,
            fact_text VARCHAR NOT NULL,
            source_type VARCHAR DEFAULT 'synthesis',
            mission_id VARCHAR DEFAULT '',
            agent_id VARCHAR DEFAULT 'queen',
            run_id VARCHAR DEFAULT '',
            embedding FLOAT[512],
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            linked_ids VARCHAR DEFAULT '[]'
        )
    """)

    # Insert testowy fakt
    import random
    fake_emb = [random.random() for _ in range(512)]
    test_con.execute(
        "INSERT INTO hive_queen_facts (id, fact_text, source_type, agent_id, embedding) VALUES (?, ?, ?, ?, ?::FLOAT[512])",
        ["test_id_001", "Allegro GMV grew 18.2% YoY in Q2 2025 to PLN 14.3B", "synthesis", "queen", fake_emb]
    )

    # Sprawdź
    rows = test_con.execute("SELECT id, fact_text FROM hive_queen_facts").fetchall()
    assert len(rows) == 1, "Insert failed"
    print(f"✅ Schema OK — inserted: {rows[0][1][:60]}")

    # Test fact extraction prompt format
    sample_text = """
    W Q2 2025 Allegro osiągnęło GMV 14.3 mld PLN, wzrost 18.2% r/r.
    Segment Smart! liczy już 5.2 mln aktywnych subskrybentów.
    Allegro Finance obsłużyło 890 tys. aktywnych pożyczek w kwartale.
    """
    print("\n=== ADDITIVE_EXTRACTION_PROMPT preview ===")
    prompt = _FACT_EXTRACTION_PROMPT.format(text=sample_text)
    print(prompt[:300] + "...")

    # Test _find_json_array
    test_json = '```json\n["fact one", "fact two", "fact three"]\n```'
    result = _find_json_array(test_json)
    parsed = json.loads(result)
    assert len(parsed) == 3, f"JSON parse failed: {result}"
    print(f"✅ JSON extraction OK: {parsed}")

    print("\n=== format_for_prompt preview ===")
    mem = QueenFactMemory.__new__(QueenFactMemory)
    sample_facts = [
        FactRecord("id1", "Allegro GMV grew 18.2% to PLN 14.3B in Q2 2025", "synthesis", "m-abc", "queen", "", "2025-07-01", 0.94),
        FactRecord("id2", "Smart! subscriptions reached 5.2M active users", "synthesis", "m-abc", "queen", "", "2025-07-01", 0.87),
        FactRecord("id3", "Allegro Finance: 890k active loans in Q2 2025", "synthesis", "m-abc", "queen", "", "2025-07-01", 0.81),
    ]
    block = mem.format_for_prompt(sample_facts)
    print(block)

    print("\n✅ All QueenFactMemory tests passed")
    sys.exit(0)
