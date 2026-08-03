"""
HIVE 2.0 RAG Knowledge Base Engine
====================================
Embedding pipeline + PostgreSQL VSS vector store for Queen query retrieval.

Architecture:
  Build : hive_content docs → chunk(500 words, 50 overlap) → Titan embed-text-v2 → PostgreSQL hive_rag_chunks
  Query : queen_query → embed → cosine_similarity HNSW search → top-k chunks → formatted context string

Corrective RAG (C-RAG) extension — hive2_rag v2.1
===================================================
Problem: trutnie często zwracają fragmenty tekstowo podobne do zapytania, ale semantycznie
         niezwiązane z rzeczywistym pytaniem Queeny. Cosine similarity mierzy bliskość
         reprezentacji wektorowych, nie relevancję merytoryczną.

Rozwiązanie (C-RAG wzorzec):
  1. RETRIEVE  — standardowe pobieranie top_k chunks (cosine similarity)
  2. GRADE     — LLM (Haiku) ocenia każdy chunk: RELEVANT / IRRELEVANT / PARTIAL
  3. DECIDE    — jeśli ≥ MIN_RELEVANT_RATIO chunków jest relevantnych → generuj normalnie
               — jeśli za mało relevantnych → TRANSFORM query → dodatkowe wyszukiwanie
  4. TRANSFORM — query rewriting: Haiku przepisuje zapytanie by wydobyć inny aspekt
  5. AUGMENT   — ponowne wyszukiwanie z nowym zapytaniem, wyniki łączone z relevantnymi

Flow:
  corrective_retrieve(query, mission_id, con)
    ↓ retrieve() top_k*2
    ↓ _grade_chunks_batch() → tagged: RELEVANT/PARTIAL/IRRELEVANT
    ↓ if enough relevant → return filtered_context
    ↓ else → _transform_query() → retrieve() again → merge → return
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from typing import Any, List, Optional

from behive.engine.db import connect as _db_connect

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
EMBED_DIMS = int(os.environ.get("BEHIVE_EMBED_DIMS", "512"))
CHUNK_WORDS = 500
CHUNK_OVERLAP = 50
BATCH_SIZE = 10          # docs per embedding batch
DEFAULT_TOP_K = 15
DEFAULT_MIN_SIM = 0.72

# ---------------------------------------------------------------------------
# C-RAG Config
# ---------------------------------------------------------------------------
CRAG_GRADE_MODEL    = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
CRAG_MIN_RELEVANT   = 0.40   # jeśli < 40% chunków RELEVANT/PARTIAL → transform + augment
CRAG_MAX_GRADE_CHUNKS = 20   # max chunks do gradingu (koszt Haiku)
CRAG_TRANSFORM_ATTEMPTS = 2  # ile razy przepisujemy query przed rezygnacją

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RAG] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hive2_rag")

# ---------------------------------------------------------------------------
# Qdrant Vector Store — primary backend (replaces PostgreSQL VSS)
# PostgreSQL hive_rag_chunks kept as legacy / fallback read source.
# ---------------------------------------------------------------------------
QDRANT_HOST       = "localhost"
QDRANT_PORT       = 6333
QDRANT_COLLECTION = "hive_rag"   # 512-dim Cosine, payload indexed on mission_id + doc_id

_qdrant_client: Optional[object] = None
_qdrant_ok: bool = True          # circuit-breaker: False after first hard failure


def _get_qdrant():
    """Lazy Qdrant client singleton. Returns None if Qdrant unavailable."""
    global _qdrant_client, _qdrant_ok
    if not _qdrant_ok:
        return None
    if _qdrant_client is not None:
        return _qdrant_client
    try:
        from qdrant_client import QdrantClient
        _qdrant_client = QdrantClient(QDRANT_HOST, port=QDRANT_PORT, timeout=10)
        # Verify collection exists, create if missing
        cols = [c.name for c in _qdrant_client.get_collections().collections]
        if QDRANT_COLLECTION not in cols:
            from qdrant_client.models import VectorParams, Distance, HnswConfigDiff, PayloadSchemaType
            _qdrant_client.create_collection(
                collection_name=QDRANT_COLLECTION,
                vectors_config=VectorParams(size=EMBED_DIMS, distance=Distance.COSINE),
                hnsw_config=HnswConfigDiff(m=16, ef_construct=100),
                on_disk_payload=True,
            )
            _qdrant_client.create_payload_index(QDRANT_COLLECTION, "mission_id", PayloadSchemaType.KEYWORD)
            _qdrant_client.create_payload_index(QDRANT_COLLECTION, "doc_id",     PayloadSchemaType.KEYWORD)
            log.info("Qdrant collection '%s' created (512-dim Cosine)", QDRANT_COLLECTION)
        return _qdrant_client
    except Exception as e:
        log.warning("Qdrant unavailable (%s) — falling back to PostgreSQL VSS", e)
        _qdrant_ok = False
        return None


def _qdrant_upsert(chunks: List[dict]) -> int:
    """Upsert chunk embeddings into Qdrant. Returns number of points upserted."""
    qc = _get_qdrant()
    if qc is None:
        return 0
    from qdrant_client.models import PointStruct
    points = []
    for c in chunks:
        emb = c.get("embedding")
        if not emb:
            continue
        # Use chunk id as deterministic int hash for Qdrant point id
        point_id = abs(hash(c["id"])) % (2**63)
        points.append(PointStruct(
            id=point_id,
            vector=list(emb),
            payload={
                "chunk_id":    c["id"],
                "mission_id":  c["mission_id"],
                "doc_id":      str(c["doc_id"]),
                "url":         c.get("url", ""),
                "domain":      c.get("domain", ""),
                "language":    c.get("language", ""),
                "chunk_index": c.get("chunk_index", 0),
                "chunk_text":  c.get("chunk_text", ""),
                "word_count":  c.get("word_count", 0),
                "confidence":  c.get("confidence", 1.0),
            },
        ))
    if not points:
        return 0
    try:
        # Batch upsert — no write lock, concurrent-safe
        qc.upsert(collection_name=QDRANT_COLLECTION, points=points, wait=False)
        return len(points)
    except Exception as e:
        log.error("Qdrant upsert failed: %s", e)
        return 0


def _qdrant_search(
    query_emb: List[float],
    mission_id: str,
    top_k: int = 15,
    min_score: float = 0.60,
    cross_mission: bool = False,
    extra_mission_ids: Optional[List[str]] = None,
) -> List[dict]:
    """
    Search Qdrant for top_k chunks.
    Returns list of dicts with keys: chunk_id, chunk_text, url, domain, score, mission_id.
    Falls back to empty list if Qdrant unavailable.
    """
    qc = _get_qdrant()
    if qc is None:
        return []
    from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny
    try:
        if cross_mission and extra_mission_ids:
            all_ids = [mission_id] + extra_mission_ids
            flt = Filter(must=[FieldCondition(key="mission_id", match=MatchAny(any=all_ids))])
        else:
            flt = Filter(must=[FieldCondition(key="mission_id", match=MatchValue(value=mission_id))])

        hits = qc.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=list(query_emb),
            query_filter=flt,
            limit=top_k,
            score_threshold=min_score,
            with_payload=True,
        )
        results = []
        for h in hits:
            p = h.payload or {}
            results.append({
                "chunk_id":   p.get("chunk_id", ""),
                "chunk_text": p.get("chunk_text", ""),
                "url":        p.get("url", ""),
                "domain":     p.get("domain", ""),
                "score":      h.score,
                "mission_id": p.get("mission_id", ""),
            })
        return results
    except Exception as e:
        log.error("Qdrant search failed: %s", e)
        return []


def _qdrant_count(mission_id: str) -> int:
    """Count Qdrant points for this mission. 0 if Qdrant unavailable."""
    qc = _get_qdrant()
    if qc is None:
        return 0
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        r = qc.count(
            collection_name=QDRANT_COLLECTION,
            count_filter=Filter(must=[FieldCondition(key="mission_id", match=MatchValue(value=mission_id))]),
            exact=False,
        )
        return r.count
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Bedrock client (lazy singleton)
# ---------------------------------------------------------------------------
_bedrock_client: Optional[object] = None  # Legacy — kept for backward compat


def _get_bedrock():
    """Legacy stub — embeddings now routed via llm.embed()."""
    return None


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_text(text: str) -> List[float]:
    """Generate embedding for text via BYOK llm.embed()."""
    from behive.engine.llm import embed
    try:
        results = embed([text[:8000]])
        return results[0] if results else [0.0] * EMBED_DIMS
    except Exception as e:
        log.warning("embed_text failed: %s — returning zero vector", e)
        return [0.0] * EMBED_DIMS


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_document(doc: dict) -> List[dict]:
    """
    Split a hive_content row-dict into overlapping word-based chunks.

    Parameters
    ----------
    doc : dict  with keys: id, mission_id, url, domain, language, raw_text
                optional:  title, published_date, author, keywords (JSON)

    Returns
    -------
    list of chunk dicts, each ready to insert into hive_rag_chunks.

    Contextual Chunking (v2.1)
    --------------------------
    Każdy chunk otrzymuje context_header — 1-2 zdania opisujące dokument z którego
    pochodzi. Header budowany jest z metadanych (title, domain, date, keywords) —
    bez LLM calls, zerowy koszt.

    Przy embeddowaniu (build_rag_index) do Titana idzie:
        context_header + "\\n\\n" + chunk_text

    W ten sposób embedding "wie" czego dotyczy dokument, nie tylko fragment.
    Przykład:
        Przed: "wzrost o 40% w Q3"  → embedding traci kontekst
        Po:    "Artykuł: Q3 2025 Results – Allegro Group | allegro.eu | e-commerce\\n\\n
                wzrost o 40% w Q3"  → embedding łapie kontekst firmy i okresu
    """
    raw_text: str = (doc.get("raw_text") or "").strip()
    if not raw_text:
        return []

    words = raw_text.split()
    if not words:
        return []

    doc_id     = str(doc.get("id", ""))
    mission_id = str(doc.get("mission_id", ""))
    url        = doc.get("url") or ""
    domain     = doc.get("domain") or ""
    language   = doc.get("language") or "unknown"

    # Buduj context_header z dostępnych metadanych
    context_header = _build_context_header(doc)

    chunks: List[dict] = []
    step = CHUNK_WORDS - CHUNK_OVERLAP  # stride between chunk starts
    start = 0
    chunk_index = 0

    while start < len(words):
        end = min(start + CHUNK_WORDS, len(words))
        chunk_words = words[start:end]
        chunk_text = " ".join(chunk_words)

        # Stable deterministic id: sha256 of mission+doc+index
        chunk_id = hashlib.sha256(
            f"{mission_id}:{doc_id}:{chunk_index}".encode()
        ).hexdigest()[:32]

        chunks.append({
            "id":             chunk_id,
            "mission_id":     mission_id,
            "doc_id":         doc_id,
            "url":            url,
            "domain":         domain,
            "language":       language,
            "chunk_index":    chunk_index,
            "chunk_text":     chunk_text,
            "context_header": context_header,
            "word_count":     len(chunk_words),
        })

        chunk_index += 1
        if end == len(words):
            break
        start += step

    return chunks


def _build_context_header(doc: dict) -> str:
    """
    Buduje context header dla chunku — bez LLM calls.

    Używa pól z hive_content:
        title          → temat dokumentu
        domain         → źródło / autorytatywność
        published_date → świeżość informacji
        author         → kto napisał
        keywords       → JSON array tagów (np. ["fintech", "IPO", "Q3"])
        language       → język (istotne dla misji multi-lingual)

    Format wyjściowy (max ~150 znaków):
        "Article: <title> | <domain> | <date> | tags: <kw1>, <kw2>"

    Jeśli brak tytułu — używa domeny jako fallback.
    Jeśli brak wszystkiego — pusty string (brak narzutu na embedding).
    """
    import json as _json

    parts: List[str] = []

    title = (doc.get("title") or "").strip()
    domain = (doc.get("domain") or "").strip()
    date = (doc.get("published_date") or "").strip()
    author = (doc.get("author") or "").strip()
    keywords_raw = doc.get("keywords")

    # Tytuł lub fallback na domenę
    if title:
        parts.append(f"Article: {title[:100]}")
    elif domain:
        parts.append(f"Source: {domain}")

    # Domena jako sygnał autorytatywności
    if domain and not title:
        pass  # już dodana wyżej
    elif domain:
        parts.append(domain)

    # Data — kluczowa dla świeżości danych rynkowych
    if date:
        parts.append(date[:10])  # tylko YYYY-MM-DD

    # Autor — istotny przy raportach analitycznych
    if author and len(author) < 60:
        parts.append(f"by {author}")

    # Keywords — bezpośrednio wzmacniają relevancję embeddingu
    if keywords_raw:
        try:
            kws = _json.loads(keywords_raw) if isinstance(keywords_raw, str) else keywords_raw
            if isinstance(kws, list) and kws:
                kw_str = ", ".join(str(k) for k in kws[:6])
                parts.append(f"tags: {kw_str}")
        except Exception:
            pass

    if not parts:
        return ""

    # ── REC-09: Trofallaxis signal — jakość źródła jako sygnał embeddingu ──
    trof = doc.get("_trofallaxis_signal")
    if trof == "HIGH_QUALITY_SOURCE":
        parts.append("quality:high")
    elif trof == "LOW_QUALITY_SOURCE":
        parts.append("quality:low")

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def ensure_schema(con: Any) -> None:
    """Create hive_rag_chunks table + vector index if they don't exist."""
    # PostgreSQL: use pgvector extension instead of DuckDB vss
    try:
        con.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    except Exception:
        pass  # Extension may already exist or not be available
    con.execute("""
        CREATE TABLE IF NOT EXISTS hive_rag_chunks (
            id            VARCHAR PRIMARY KEY,
            mission_id    VARCHAR,
            doc_id        VARCHAR,
            url           VARCHAR,
            domain        VARCHAR,
            language      VARCHAR,
            chunk_index   INTEGER,
            chunk_text    VARCHAR,
            context_header VARCHAR,
            word_count    INTEGER,
            embedding     FLOAT[512],
            confidence    FLOAT DEFAULT 0.7,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migracja: dodaj context_header jeśli tabela istniała bez tej kolumny
    try:
        con.execute("ALTER TABLE hive_rag_chunks ADD COLUMN context_header VARCHAR")
        log.info("Migrated hive_rag_chunks: added context_header column.")
    except Exception:
        pass  # kolumna już istnieje

    # HNSW index - create only once; PostgreSQL VSS doesn't support IF NOT EXISTS on index
    existing_indexes = con.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename='hive_rag_chunks'"
    ).fetchall()
    if not any("hive_rag_hnsw" in str(r) for r in existing_indexes):
        try:
            con.execute(
                "CREATE INDEX hive_rag_hnsw ON hive_rag_chunks USING HNSW (embedding)"
            )
            log.info("HNSW index created on hive_rag_chunks.")
        except Exception as exc:
            log.warning("Could not create HNSW index (may already exist): %s", exc)


# ---------------------------------------------------------------------------
# Build index
# ---------------------------------------------------------------------------

def build_rag_index(mission_id: str, con: Any) -> int:
    """
    Embed all hive_content documents for *mission_id* and upsert into hive_rag_chunks.

    Skips docs whose chunks are already present.
    Processes docs in batches of BATCH_SIZE to respect Titan rate limits.

    Contextual Chunking (v2.1):
        Embeddowany tekst = context_header + "\\n\\n" + chunk_text
        Zapisany chunk_text pozostaje czysty (bez headera) — retrieval zwraca oryginalny tekst.
        Dzięki temu Queen nie widzi zdublowanego headera, ale embedding "rozumie" kontekst.

    Returns
    -------
    int  total new chunks inserted.
    """
    ensure_schema(con)

    # Fetch docs — pre-cap do 200 najwyżej scorowanych (unika timeout na 1964 docs)
    rows = con.execute("""
        SELECT c.id, c.mission_id, c.url, c.domain, c.language, c.raw_text,
               c.title, c.published_date, c.author, c.keywords,
               COALESCE(c.quality_score, 0.0) AS quality_score,
               COALESCE(s.score_total, 0) AS source_score
        FROM hive_content c
        LEFT JOIN hive_sources s ON s.url = c.url AND s.mission_id = c.mission_id
        WHERE c.mission_id = ?
          AND c.raw_text IS NOT NULL
          AND length(c.raw_text) > 50
        ORDER BY COALESCE(s.score_total, s.score_authority, 0) DESC, c.id
        LIMIT 200
    """, [mission_id]).fetchall()

    if not rows:
        log.warning("No hive_content docs found for mission_id=%s", mission_id)
        return 0

    col_names = ["id", "mission_id", "url", "domain", "language", "raw_text",
                 "title", "published_date", "author", "keywords",
                 "quality_score", "source_score"]
    docs = [dict(zip(col_names, r)) for r in rows]

    # ── REC-09: Trofallaxis RAG — inject harvest quality as embedding signal ──
    # Wysoka jakość źródła → wzmocnienie context_header → lepszy embedding.
    # "Trofallaxis" = pszczoły przekazują sobie nektar (tu: jakość metadata).
    for d in docs:
        qs = float(d.get("quality_score") or 0.0)
        ss = float(d.get("source_score") or 0.0)
        if qs >= 0.7 or ss >= 60:
            d["_trofallaxis_signal"] = "HIGH_QUALITY_SOURCE"
        elif qs < 0.3 and ss < 20:
            d["_trofallaxis_signal"] = "LOW_QUALITY_SOURCE"
        else:
            d["_trofallaxis_signal"] = None
    total_docs = len(docs)
    log.info("Found %d docs for mission %s", total_docs, mission_id)

    # Collect already-embedded doc_ids to allow incremental updates
    existing = con.execute(
        "SELECT DISTINCT doc_id FROM hive_rag_chunks WHERE mission_id = ?",
        [mission_id]
    ).fetchall()
    embedded_doc_ids = {r[0] for r in existing}
    log.info("%d docs already embedded, skipping.", len(embedded_doc_ids))

    pending = [d for d in docs if str(d["id"]) not in embedded_doc_ids]
    log.info("%d docs to embed.", len(pending))

    if not pending:
        total = con.execute(
            "SELECT COUNT(*) FROM hive_rag_chunks WHERE mission_id=?", [mission_id]
        ).fetchone()[0]
        log.info("Nothing new to embed. Total chunks: %d", total)
        return 0

    new_chunks_total = 0
    doc_count = 0

    # Process in batches
    for batch_start in range(0, len(pending), BATCH_SIZE):
        batch = pending[batch_start: batch_start + BATCH_SIZE]

        for doc in batch:
            doc_count += 1
            chunks = chunk_document(doc)
            if not chunks:
                log.debug("Doc %s produced no chunks (empty text).", doc["id"])
                continue

            # ── P1: INPUT GUARDRAIL ──────────────────────────────────────
            # Filtruj chunki zawierające prompt injection PRZED embeddingiem
            try:
                from behive.engine.guardrail import scan_batch
                clean_chunks, blocked = scan_batch(
                    chunks, url_key="url", text_key="chunk_text", fast=False
                )
                if blocked:
                    doc_url = doc.get("url", "?")
                    log.warning(
                        "P1:GUARDRAIL — blocked %d/%d chunks from %s | threats: %s",
                        len(blocked), len(chunks), doc_url[:60],
                        [b.threat_type for b in blocked],
                    )
                chunks = clean_chunks
            except ImportError:
                pass  # guardrail nie zainstalowany — skip
            if not chunks:
                continue
            # ─────────────────────────────────────────────────────────────

            embedded_chunks = []
            for chunk in chunks:
                try:
                    # Contextual embedding: header + chunk_text → Titan
                    # chunk_text przechowywany czysty, embedding wzbogacony kontekstem
                    header = chunk.get("context_header", "")
                    embed_input = (
                        f"{header}\n\n{chunk['chunk_text']}"
                        if header else chunk["chunk_text"]
                    )
                    emb = embed_text(embed_input)
                    chunk["embedding"] = emb
                    embedded_chunks.append(chunk)
                except Exception as exc:
                    log.error(
                        "Embed failed for doc %s chunk %d: %s",
                        doc["id"], chunk["chunk_index"], exc
                    )

            if embedded_chunks:
                _upsert_chunks(con, embedded_chunks)
                new_chunks_total += len(embedded_chunks)

            log.info(
                "Embedded doc %d/%d (%d chunks)",
                doc_count, len(pending), len(embedded_chunks),
            )

        # Small pause between batches to be gentle on Titan rate limits
        if batch_start + BATCH_SIZE < len(pending):
            time.sleep(0.5)

    log.info(
        "Build complete. %d new chunks inserted for mission %s.",
        new_chunks_total, mission_id
    )
    return new_chunks_total


def _upsert_chunks(con: Any, chunks: List[dict]) -> None:
    """
    Insert chunks into Qdrant (primary) + PostgreSQL hive_rag_chunks (legacy/fallback).

    Qdrant write is async (wait=False) — no write lock.
    PostgreSQL write is best-effort — errors are logged but not raised.
    """
    # ── PRIMARY: Qdrant (no write lock, concurrent-safe) ─────────────────
    q_inserted = _qdrant_upsert(chunks)
    if q_inserted > 0:
        log.debug("Qdrant: upserted %d chunks", q_inserted)

    # ── FALLBACK: PostgreSQL (kept for legacy retrieve / hybrid BM25 search) ─
    try:
        con.executemany(
            """
            INSERT INTO hive_rag_chunks
                (id, mission_id, doc_id, url, domain, language,
                 chunk_index, chunk_text, context_header, word_count, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    c["id"],
                    c["mission_id"],
                    c["doc_id"],
                    c["url"],
                    c["domain"],
                    c["language"],
                    c["chunk_index"],
                    c["chunk_text"],
                    c.get("context_header", ""),
                    c["word_count"],
                    c["embedding"],
                )
                for c in chunks
            ],
        )
    except Exception as exc:
        log.warning("PostgreSQL hive_rag_chunks insert failed (non-critical): %s", exc)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def retrieve(
    query: str,
    mission_id: str,
    con: Any,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIM,
) -> str:
    """
    Embed *query*, search for top_k chunks, return formatted context string.

    Primary: Qdrant (no lock, concurrent-safe).
    Fallback: PostgreSQL VSS cosine_similarity (original path).
    """
    ensure_schema(con)
    query_emb = embed_text(query)

    # ── PRIMARY: Qdrant ───────────────────────────────────────────────────
    qdrant_hits = _qdrant_search(query_emb, mission_id, top_k=top_k, min_score=min_similarity)
    if qdrant_hits:
        parts: List[str] = []
        for idx, h in enumerate(qdrant_hits, start=1):
            header = f"[SOURCE {idx}] {h['url']} | {h['domain']} | similarity: {h['score']:.4f}"
            parts.append(f"{header}\n{h['chunk_text']}")
        log.info("Qdrant retrieve: %d chunks (mission=%s)", len(qdrant_hits), mission_id)
        return "\n\n".join(parts)

    # ── FALLBACK: PostgreSQL VSS ──────────────────────────────────────────────
    log.info("Qdrant returned 0 results — trying PostgreSQL VSS fallback")
    count = con.execute(
        "SELECT COUNT(*) FROM hive_rag_chunks WHERE mission_id=?", [mission_id]
    ).fetchone()[0]
    if count == 0:
        log.warning("No chunks found for mission %s in PostgreSQL either — run build first.", mission_id)
        return ""

    rows = con.execute(
        """
        SELECT
            id,
            chunk_text,
            url,
            domain,
            array_cosine_similarity(embedding, ?::FLOAT[512]) AS sim
        FROM hive_rag_chunks
        WHERE mission_id = ?
        ORDER BY sim DESC
        LIMIT ?
        """,
        [query_emb, mission_id, top_k],
    ).fetchall()

    rows = [r for r in rows if r[4] is not None and r[4] >= min_similarity]
    if not rows:
        log.info(
            "No chunks with similarity >= %.2f found for query: %s",
            min_similarity, query[:80]
        )
        return ""

    parts: List[str] = []
    for idx, (chunk_id, chunk_text, url, domain, sim) in enumerate(rows, start=1):
        header = f"[SOURCE {idx}] {url} | {domain} | similarity: {sim:.4f}"
        parts.append(f"{header}\n{chunk_text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Hybrid Search — BM25 + Vector with Reciprocal Rank Fusion (RRF)
# ---------------------------------------------------------------------------

# Hybrid search config
HYBRID_RRF_K       = 60      # RRF constant — higher → smoother rank blending
HYBRID_BM25_TOP    = 40      # how many BM25 candidates to retrieve
HYBRID_VECTOR_TOP  = 40      # how many vector candidates to retrieve
HYBRID_FINAL_TOP   = 15      # final top-K after RRF fusion
HYBRID_BM25_K1     = 1.2     # BM25 term saturation
HYBRID_BM25_B      = 0.75    # BM25 document length normalization


def _ensure_fts_index(con: Any, mission_id: str) -> bool:
    """
    Tworzy FTS index na hive_rag_chunks dla danej misji (jeśli nie istnieje).
    Używa PostgreSQL FTS extension z BM25 scoring.

    Returns True jeśli index istnieje/stworzony, False przy błędzie.

    UWAGA: PostgreSQL FTS 1.5.x wymaga minimum 2 tokenów w query do match_bm25.
    Dla single-word queries używamy fallback: query duplikowany ('word word').
    """
    try:
        # PostgreSQL: use built-in FTS or pg_trgm instead of DuckDB fts extension
        try:
            con.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        except Exception:
            pass
        # Sprawdź czy schema fts_main_hive_rag_chunks już istnieje
        schemas = [r[0] for r in con.execute(
            "SELECT schema_name FROM information_schema.schemata"
        ).fetchall()]
        if "fts_main_hive_rag_chunks" not in schemas:
            con.execute("PRAGMA create_fts_index(hive_rag_chunks, id, chunk_text)")
            log.info("FTS index created on hive_rag_chunks.")
        return True
    except Exception as exc:
        log.warning("FTS index creation failed (BM25 disabled): %s", exc)
        return False


def _bm25_search(
    query: str,
    mission_id: str,
    con: Any,
    top_n: int = HYBRID_BM25_TOP,
) -> List[tuple]:
    """
    Szuka chunków przez BM25 full-text search.
    Zwraca listę (chunk_id, bm25_score) posortowaną malejąco.

    PostgreSQL FTS caveats:
    - match_bm25 wymaga 2+ tokenów w query → single-word queries duplikujemy
    - match_bm25 operuje na CAŁEJ tabeli hive_rag_chunks (brak filtrowania po mission_id)
      → wyniki filtrowane post-hoc po mission_id
    """
    if not _ensure_fts_index(con, mission_id):
        return []

    # Normalizuj query — usuń nadmiarowe spacje, lower
    q = " ".join(query.lower().split())

    # PostgreSQL FTS wymaga 2+ tokenów — duplikuj single-word queries
    tokens = q.split()
    if len(tokens) == 1:
        q = f"{q} {q}"

    try:
        rows = con.execute(
            f"""
            SELECT
                hrc.id,
                fts_main_hive_rag_chunks.match_bm25(hrc.id, ?) AS bm25_score
            FROM hive_rag_chunks hrc
            WHERE hrc.mission_id = ?
              AND fts_main_hive_rag_chunks.match_bm25(hrc.id, ?) IS NOT NULL
            ORDER BY bm25_score DESC
            LIMIT ?
            """,
            [q, mission_id, q, top_n],
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception as exc:
        log.warning("BM25 search failed: %s", exc)
        return []


def _rrf_fusion(
    vector_results: List[tuple],
    bm25_results: List[tuple],
    k: int = HYBRID_RRF_K,
    top_n: int = HYBRID_FINAL_TOP,
) -> List[tuple]:
    """
    Reciprocal Rank Fusion (RRF) — łączy dwa rankingi bez normalizacji scorów.

    Wzór: RRF(d) = Σ 1 / (k + rank_i(d))
    k=60 (standard) — zmniejsza wpływ wyników z bardzo wysoką pozycją.

    Parametry:
        vector_results: [(chunk_id, sim_score), ...] posortowane malejąco
        bm25_results:   [(chunk_id, bm25_score), ...] posortowane malejąco

    Zwraca: [(chunk_id, rrf_score), ...] posortowane malejąco, top_n wyników.
    """
    scores: dict = {}

    for rank, (chunk_id, _) in enumerate(vector_results, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)

    for rank, (chunk_id, _) in enumerate(bm25_results, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return ranked[:top_n]


def hybrid_retrieve(
    query: str,
    mission_id: str,
    con: Any,
    top_k: int = HYBRID_FINAL_TOP,
    min_similarity: float = 0.30,
) -> str:
    """
    Hybrid Search RAG: BM25 + Vector Embeddings → Reciprocal Rank Fusion (RRF).

    Problem który rozwiązuje:
        Cosine similarity zawodzi przy exact-match terminach (akronimy, tickery firm,
        numery produktów, nazwy własne). BM25 zawodzi przy parafrazach i synonimy.
        RRF łączy oba sygnały bez normalizacji — wyniki pojawiające się w obu
        rankingach dostają bonus.

    Przykład:
        Zapytanie: "ECS capacity metrics AWS Q3"
        - Vector: łapie "Amazon Elastic Container Service performance report" ✓
        - BM25:   łapie "ECS metrics 2025" (exact "ECS") ✓
        - RRF:    chunk który pasuje obu idzie na top ✓

    Pipeline:
        1. embed(query)        → vector search → top HYBRID_VECTOR_TOP chunks
        2. BM25(query)         → FTS search    → top HYBRID_BM25_TOP chunks
        3. RRF fusion          → HYBRID_FINAL_TOP najlepszych
        4. Fetch chunk_text    → format context string

    Fallback: jeśli BM25 nie zadziała → czyste vector retrieve().

    Returns
    -------
    str  multi-source context block z adnotacją [HYBRID] lub [VECTOR-ONLY].
    """
    ensure_schema(con)

    count = con.execute(
        "SELECT COUNT(*) FROM hive_rag_chunks WHERE mission_id=?", [mission_id]
    ).fetchone()[0]
    if count == 0:
        log.warning("No chunks found for mission %s – run build first.", mission_id)
        return ""

    # --- 1. Vector search ---
    query_emb = embed_text(query)
    vec_rows = con.execute(
        """
        SELECT id, array_cosine_similarity(embedding, ?::FLOAT[512]) AS sim
        FROM hive_rag_chunks
        WHERE mission_id = ?
        ORDER BY sim DESC
        LIMIT ?
        """,
        [query_emb, mission_id, HYBRID_VECTOR_TOP],
    ).fetchall()
    # Filtruj zbyt słabe wyniki (szersze sito niż w retrieve() — BM25 to wyrówna)
    vec_results = [(r[0], r[1]) for r in vec_rows if r[1] is not None and r[1] >= min_similarity]

    # --- 2. BM25 search ---
    bm25_results = _bm25_search(query, mission_id, con, top_n=HYBRID_BM25_TOP)
    bm25_ok = len(bm25_results) > 0

    if not vec_results and not bm25_results:
        log.info("Hybrid: no results (vector sim < %.2f, BM25 empty). Query: %s",
                 min_similarity, query[:80])
        return ""

    # --- 3. RRF fusion ---
    if bm25_ok:
        fused = _rrf_fusion(vec_results, bm25_results, k=HYBRID_RRF_K, top_n=top_k)
        mode = "HYBRID"
        log.info("Hybrid: vec=%d BM25=%d → fused=%d (RRF)",
                 len(vec_results), len(bm25_results), len(fused))
    else:
        # BM25 fallback do czystego vector
        fused = [(cid, score) for cid, score in vec_results[:top_k]]
        mode = "VECTOR-ONLY"
        log.info("Hybrid: BM25 unavailable → fallback to vector-only (%d results)", len(fused))

    if not fused:
        return ""

    # --- 4. Fetch chunk text dla top RRF chunków ---
    chunk_ids = [cid for cid, _ in fused]
    placeholders = ", ".join("?" * len(chunk_ids))
    chunk_rows = con.execute(
        f"""
        SELECT id, chunk_text, url, domain, context_header
        FROM hive_rag_chunks
        WHERE id IN ({placeholders})
        """,
        chunk_ids,
    ).fetchall()

    # Zachowaj kolejność RRF
    chunk_map = {r[0]: r for r in chunk_rows}

    parts: List[str] = []
    for idx, (chunk_id, rrf_score) in enumerate(fused, start=1):
        if chunk_id not in chunk_map:
            continue
        _, chunk_text, url, domain, ctx_header = chunk_map[chunk_id]
        header = f"[SOURCE {idx}] {url} | {domain} | rrf: {rrf_score:.4f} [{mode}]"
        if ctx_header:
            header += f"\n  context: {ctx_header}"
        parts.append(f"{header}\n{chunk_text}")

    return "\n\n".join(parts)




# ---------------------------------------------------------------------------
# C-RAG — Corrective Retrieval-Augmented Generation
# ---------------------------------------------------------------------------

def _haiku_call(prompt: str, max_tokens: int = 256) -> str:
    """LLM call via BYOK llm.complete()."""
    try:
        from behive.engine.llm import complete
        return complete(prompt, stage="process", max_tokens=max_tokens)
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return ""


def _grade_chunks_batch(query: str, chunks: List[tuple]) -> List[tuple]:
    """
    Ocenia relevancję listy chunków względem query.

    Parametry
    ---------
    query  : oryginalne zapytanie
    chunks : lista (chunk_id, chunk_text, url, domain, sim) z retrieve()

    Zwraca
    ------
    Lista (chunk_id, chunk_text, url, domain, sim, grade)
    gdzie grade ∈ {"RELEVANT", "PARTIAL", "IRRELEVANT"}

    Strategia: jeden batch call z listą N chunków (max CRAG_MAX_GRADE_CHUNKS).
    Haiku dostaje pytanie + skrócone fragmenty → zwraca JSON array z labelami.
    Jeśli parsowanie się nie uda → fallback: wszystkie PARTIAL.
    """
    import json as _json, re as _re

    if not chunks:
        return []

    to_grade = chunks[:CRAG_MAX_GRADE_CHUNKS]

    # Buduj prompt batchowy
    snippets = []
    for i, (cid, ctext, url, domain, sim) in enumerate(to_grade):
        snippet = ctext[:300].replace("\n", " ")
        snippets.append(f"[{i}] {snippet}")

    prompt = (
        f"Query: {query}\n\n"
        "Below are document fragments. For each, classify relevance to the query.\n"
        "Respond ONLY with a valid JSON array, one label per fragment, in order.\n"
        'Labels: "RELEVANT" | "PARTIAL" | "IRRELEVANT"\n'
        'Example for 3 fragments: ["RELEVANT", "IRRELEVANT", "PARTIAL"]\n\n'
        "Fragments:\n" + "\n".join(snippets)
    )

    raw = _haiku_call(prompt, max_tokens=max(128, len(to_grade) * 20))

    grades: List[str] = []
    try:
        # wyciągnij tablicę JSON z odpowiedzi
        match = _re.search(r'\[.*?\]', raw, _re.DOTALL)
        if match:
            parsed = _json.loads(match.group())
            grades = [str(g).upper() for g in parsed]
    except Exception:
        pass

    # fallback: jeśli parsowanie nie wyszło lub za mało wyników
    while len(grades) < len(to_grade):
        grades.append("PARTIAL")
    grades = grades[:len(to_grade)]

    # Dołącz grady do chunków
    graded = []
    for i, (cid, ctext, url, domain, sim) in enumerate(to_grade):
        g = grades[i] if grades[i] in ("RELEVANT", "PARTIAL", "IRRELEVANT") else "PARTIAL"
        graded.append((cid, ctext, url, domain, sim, g))

    # Pozostałe (ponad CRAG_MAX_GRADE_CHUNKS) → PARTIAL bez gradingu
    for c in chunks[CRAG_MAX_GRADE_CHUNKS:]:
        graded.append((*c, "PARTIAL"))

    log.info(
        "C-RAG grade: %d RELEVANT, %d PARTIAL, %d IRRELEVANT / %d total",
        sum(1 for r in graded if r[5] == "RELEVANT"),
        sum(1 for r in graded if r[5] == "PARTIAL"),
        sum(1 for r in graded if r[5] == "IRRELEVANT"),
        len(graded),
    )
    return graded


def _transform_query(query: str, attempt: int = 1) -> str:
    """
    Przepisuje query by wydobyć inny kąt semantyczny.
    Używane gdy pierwotne wyszukiwanie nie zwróciło dość relevantnych chunków.
    """
    prompt = (
        f"Rewrite this research query to retrieve different but related information.\n"
        f"Attempt #{attempt} — use synonyms, different perspective, or more specific terms.\n"
        f"Return ONLY the rewritten query, no explanation.\n\n"
        f"Original: {query}"
    )
    result = _haiku_call(prompt, max_tokens=120)
    if result and result.lower() != query.lower() and len(result) > 5:
        log.info("C-RAG transform query [attempt %d]: %s → %s", attempt, query[:60], result[:60])
        return result
    return query  # fallback: oryginalne


def _retrieve_raw(
    query: str,
    mission_id: str,
    con: Any,
    top_k: int,
    min_similarity: float,
) -> List[tuple]:
    """
    Zwraca listę surowych krotek (id, chunk_text, url, domain, sim) BEZ formatowania.
    Wewnętrzna wersja retrieve() dla C-RAG.

    Używa hybrid BM25+vector gdy FTS index jest dostępny (automatycznie),
    fallback do czystego vector gdy FTS niedostępne.
    """
    count = con.execute(
        "SELECT COUNT(*) FROM hive_rag_chunks WHERE mission_id=?", [mission_id]
    ).fetchone()[0]
    if count == 0:
        return []

    # Spróbuj hybrid — jeśli FTS dostępne, użyj BM25+vector
    # Potrzebujemy raw tuples, więc reimplementujemy hybrid pipeline bez formatowania
    q_emb = embed_text(query)
    vec_rows = con.execute(
        """
        SELECT id, array_cosine_similarity(embedding, ?::FLOAT[512]) AS sim
        FROM hive_rag_chunks
        WHERE mission_id = ?
        ORDER BY sim DESC
        LIMIT ?
        """,
        [q_emb, mission_id, top_k * 2],
    ).fetchall()
    vec_results = [(r[0], r[1]) for r in vec_rows if r[1] is not None and r[1] >= min_similarity]

    bm25_results = _bm25_search(query, mission_id, con, top_n=top_k * 2)

    if bm25_results:
        # Hybrid RRF → lista (chunk_id, rrf_score)
        fused = _rrf_fusion(vec_results, bm25_results, k=HYBRID_RRF_K, top_n=top_k)
        if fused:
            chunk_ids = [cid for cid, _ in fused]
            # Fetch chunk details — użyj rrf_score jako "sim" dla zgodności z C-RAG
            rrf_map = dict(fused)
            placeholders = ", ".join("?" * len(chunk_ids))
            rows = con.execute(
                f"SELECT id, chunk_text, url, domain FROM hive_rag_chunks WHERE id IN ({placeholders})",
                chunk_ids,
            ).fetchall()
            # Zwróć jako (id, chunk_text, url, domain, rrf_score) — kompatybilne z grader
            return [(r[0], r[1], r[2], r[3], rrf_map.get(r[0], 0.0)) for r in rows]

    # Fallback: pure vector
    rows = con.execute(
        """
        SELECT
            id, chunk_text, url, domain,
            array_cosine_similarity(embedding, ?::FLOAT[512]) AS sim
        FROM hive_rag_chunks
        WHERE mission_id = ?
        ORDER BY sim DESC
        LIMIT ?
        """,
        [q_emb, mission_id, top_k],
    ).fetchall()
    return [r for r in rows if r[4] is not None and r[4] >= min_similarity]



def _format_graded_chunks(graded: List[tuple], include_grades: bool = True) -> str:
    """Formatuje ocenione chunki do stringa kontekstu dla Queeny."""
    parts: List[str] = []
    for idx, row in enumerate(graded, start=1):
        chunk_id, chunk_text, url, domain, sim, grade = row
        grade_tag = f" | grade:{grade}" if include_grades else ""
        header = f"[SOURCE {idx}] {url} | {domain} | sim:{sim:.4f}{grade_tag}"
        parts.append(f"{header}\n{chunk_text}")
    return "\n\n".join(parts)


def corrective_retrieve(
    query: str,
    mission_id: str,
    con: Any,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = 0.35,
    enable_grading: bool = True,
    enable_transform: bool = True,
) -> str:
    """
    C-RAG: Corrective Retrieval — retrieve → grade → decide → (transform → augment).

    Parametry
    ---------
    query            : zapytanie (może być po polsku — tłumaczone wewnętrznie)
    mission_id       : ID misji HIVE
    con              : połączenie PostgreSQL
    top_k            : ile chunków pobrać w pierwszym kroku
    min_similarity   : próg cosine (niższy niż w retrieve() — gradujemy zamiast twardego progu)
    enable_grading   : wyłącz ocenianie (fallback do zwykłego retrieve())
    enable_transform : wyłącz rewriting (tylko filtruj złe chunki, nie augmentuj)

    Zwraca
    ------
    str  — sformatowany kontekst dla Queeny, z tagami grade: dla transparentności.
          Puste stringi tylko gdy naprawdę brak danych.

    Diagram przepływu
    -----------------
    query
      → _retrieve_raw(top_k * 2)                      ← szerokie sito
          → _grade_chunks_batch()                      ← Haiku ocenia
              → relevant_ratio >= CRAG_MIN_RELEVANT?
                  YES → zwróć RELEVANT + PARTIAL       ← dobry kontekst
                  NO  → _transform_query()
                          → _retrieve_raw() ponownie
                              → merge z RELEVANT chunks
                                → zwróć najlepsze top_k
    """
    ensure_schema(con)

    # Sprawdź czy w ogóle są chunki
    count = con.execute(
        "SELECT COUNT(*) FROM hive_rag_chunks WHERE mission_id=?", [mission_id]
    ).fetchone()[0]
    if count == 0:
        log.warning("C-RAG: No chunks for mission %s — triggering P2:WEB_FALLBACK", mission_id)
        try:
            from behive.engine.rag_extensions import web_search_fallback
            ctx = web_search_fallback(query, max_results=4)
            if ctx:
                return ctx
        except ImportError:
            pass
        return ""

    if not enable_grading:
        # tryb bez C-RAG — zachowanie kompatybilne z retrieve()
        return retrieve(query, mission_id, con, top_k=top_k, min_similarity=min_similarity)

    # ── Step 1: RETRIEVE (szerokie sito — używa hybrid BM25+vector) ──────────
    broad_k = min(top_k * 2, 30)

    # ── P4: QUERY REWRITING (RRR) ────────────────────────────────────────────
    # Deterministyczne przepisanie query → warianty → multi-query RRF
    if enable_transform:
        try:
            from behive.engine.rag_extensions import rewrite_query_fast, multi_query_rrf
            variants = rewrite_query_fast(query)
            if len(variants) > 1:
                log.info("P4:RRR — %d query variants, multi-query retrieval", len(variants))
                def _retrieve_variant(q: str) -> List[tuple]:
                    return _retrieve_raw(q, mission_id, con, top_k=broad_k, min_similarity=min_similarity)
                raw_chunks = multi_query_rrf(variants, _retrieve_variant, top_k=broad_k)
            else:
                raw_chunks = _retrieve_raw(query, mission_id, con, top_k=broad_k, min_similarity=min_similarity)
        except ImportError:
            raw_chunks = _retrieve_raw(query, mission_id, con, top_k=broad_k, min_similarity=min_similarity)
    else:
        raw_chunks = _retrieve_raw(query, mission_id, con, top_k=broad_k, min_similarity=min_similarity)
    # ─────────────────────────────────────────────────────────────────────────

    if not raw_chunks:
        log.info("C-RAG: No chunks above sim %.2f — triggering P2:WEB_FALLBACK", min_similarity)
        try:
            from behive.engine.rag_extensions import web_search_fallback
            ctx = web_search_fallback(query, max_results=4)
            if ctx:
                return ctx
        except ImportError:
            pass
        return ""

    log.info("C-RAG: Retrieved %d raw chunks for grading", len(raw_chunks))

    # ── Step 2: GRADE ────────────────────────────────────────────────────────
    graded = _grade_chunks_batch(query, raw_chunks)

    relevant     = [r for r in graded if r[5] in ("RELEVANT", "PARTIAL")]
    relevant_r   = len(relevant) / max(len(graded), 1)

    # ── Step 3: DECIDE ───────────────────────────────────────────────────────
    if relevant_r >= CRAG_MIN_RELEVANT or not enable_transform:
        # Dość relevantnych chunków → zwróć bez augmentacji
        # Posortuj: RELEVANT pierwsze, PARTIAL drugie
        ordered = (
            [r for r in graded if r[5] == "RELEVANT"] +
            [r for r in graded if r[5] == "PARTIAL"]
        )
        final = ordered[:top_k]
        log.info(
            "C-RAG: %.0f%% relevant — returning %d chunks (no augmentation needed)",
            relevant_r * 100, len(final),
        )
        return _format_graded_chunks(final)

    # ── Step 4+5: TRANSFORM → AUGMENT ────────────────────────────────────────
    log.info(
        "C-RAG: Only %.0f%% relevant (< %.0f%%) — transforming query and augmenting",
        relevant_r * 100, CRAG_MIN_RELEVANT * 100,
    )

    augmented_relevant: List[tuple] = list(relevant)  # zacznij od tego co mamy

    for attempt in range(1, CRAG_TRANSFORM_ATTEMPTS + 1):
        transformed = _transform_query(query, attempt=attempt)
        if transformed == query:
            log.info("C-RAG: Transform attempt %d produced identical query — stopping", attempt)
            break

        aug_raw = _retrieve_raw(
            transformed, mission_id, con,
            top_k=broad_k, min_similarity=min_similarity,
        )
        if not aug_raw:
            continue

        aug_graded = _grade_chunks_batch(transformed, aug_raw)
        aug_relevant = [r for r in aug_graded if r[5] in ("RELEVANT", "PARTIAL")]

        # Deduplikuj po chunk_id
        existing_ids = {r[0] for r in augmented_relevant}
        new_unique = [r for r in aug_relevant if r[0] not in existing_ids]
        augmented_relevant.extend(new_unique)

        log.info(
            "C-RAG: Augment attempt %d → +%d unique relevant chunks (total: %d)",
            attempt, len(new_unique), len(augmented_relevant),
        )

        # Sprawdź czy mamy już dość
        new_ratio = len(augmented_relevant) / max(len(raw_chunks), 1)
        if new_ratio >= CRAG_MIN_RELEVANT:
            log.info("C-RAG: Sufficient relevant chunks after augmentation — stopping")
            break

    # Finalny output: posortuj, ogranicz do top_k
    ordered_final = (
        [r for r in augmented_relevant if r[5] == "RELEVANT"] +
        [r for r in augmented_relevant if r[5] == "PARTIAL"]
    )

    # ── P3: CROSSENCODER RERANKING ────────────────────────────────────
    # Rerankuj RELEVANT+PARTIAL przed obcięciem do top_k
    try:
        from behive.engine.rag_extensions import rerank_chunks
        # Krotki: (id, chunk_text, url, domain, score, grade)
        # rerank_chunks oczekuje (id, chunk_text, ...) — format zgodny
        reranked = rerank_chunks(query, ordered_final, top_k=top_k)
        if reranked:
            ordered_final = reranked
            log.info("P3:RERANK — reranked %d chunks", len(reranked))
    except ImportError:
        pass
    # ─────────────────────────────────────────────────────────────────

    final = ordered_final[:top_k]

    if not final:
        # Absolutny fallback: zwróć oryginalne chunki bez gradingu
        log.warning("C-RAG: No relevant chunks after augmentation — falling back to raw results")
        fallback = [(*r, "PARTIAL") for r in raw_chunks[:top_k]]
        raw_ctx = _format_graded_chunks(fallback)
        if raw_ctx:
            return raw_ctx
        # ── P2: WEB SEARCH FALLBACK ──────────────────────────────────────
        # Lokalny RAG jest pusty → szukamy w internecie
        log.warning("C-RAG: Raw fallback also empty — triggering P2:WEB_FALLBACK")
        try:
            from behive.engine.rag_extensions import web_search_fallback
            return web_search_fallback(query, max_results=4)
        except ImportError:
            pass
        # ─────────────────────────────────────────────────────────────────
        return ""

    log.info("C-RAG: Final context: %d chunks (after corrective augmentation)", len(final))
    return _format_graded_chunks(final)

def rag_status(mission_id: str, con: Any) -> dict:
    """Return chunk counts and metadata for *mission_id*."""
    ensure_schema(con)
    row = con.execute(
        """
        SELECT
            COUNT(*)                  AS total_chunks,
            COUNT(DISTINCT doc_id)    AS embedded_docs,
            MIN(created_at)           AS first_embedded,
            MAX(created_at)           AS last_embedded,
            AVG(word_count)           AS avg_chunk_words
        FROM hive_rag_chunks
        WHERE mission_id = ?
        """,
        [mission_id],
    ).fetchone()

    total_docs = con.execute(
        "SELECT COUNT(*) FROM hive_content WHERE mission_id=?", [mission_id]
    ).fetchone()[0]

    return {
        "mission_id": mission_id,
        "total_chunks": row[0],
        "embedded_docs": row[1],
        "total_docs": total_docs,
        "first_embedded": str(row[2]) if row[2] else None,
        "last_embedded": str(row[3]) if row[3] else None,
        "avg_chunk_words": round(float(row[4]), 1) if row[4] else 0,
    }


# ---------------------------------------------------------------------------
# Citation Verification — verbatim key-phrase grounding
# ---------------------------------------------------------------------------
#
# Problem:
#   Queen generuje zdania faktyczne (claims) na podstawie RAG context. Nie wiemy
#   czy te claims mają pokrycie w chunkach. hive2_falsifier wykrywa konflikty
#   MIĘDZY claimami — ale nie weryfikuje czy claim w ogóle ma źródło w RAG.
#
# Wzorzec: agentic_typed_rag_pydanticai/agent.py — _valid_citations():
#   quoted_span = normalize(citation.quoted_span)
#   valid if quoted_span in normalize(chunk.text)
#
# Adaptacja dla HIVE:
#   1. extract_key_phrases(claim)    — n-gramy 3-5 słów, odfiltruj stop-words
#   2. _verify_single_claim(claim, chunks) — sprawdź ile key phrases trafia
#      dokładnie (substring) w chunki RAG
#   3. verify_citations(...)         — przetwarza claims z hive_claims,
#      uzupełnia pole cross_validated w hive_citations, zwraca raport
#
# Wynik: każdy claim w hive_claims ma:
#   verified=True  — min 1 key phrase znaleziona dosłownie w RAG chunku
#   verified=False — brak pokrycia (hallucination risk)
#   match_url      — URL chunku który potwierdza claim
#   match_span     — znaleziony fragment (do podglądu)

CITATION_MIN_PHRASE_LEN = 4    # min słów w key phrase
CITATION_MIN_SPAN_CHARS = 8    # min znaków w matched span (jak w tutorial)
CITATION_NGRAM_SIZES = (4, 3)  # próbuj 4-gramy, potem 3-gramy
CITATION_STOP_WORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or",
    "but", "is", "are", "was", "were", "has", "have", "had", "be", "been",
    "that", "this", "it", "its", "from", "by", "with", "as", "not", "no",
    "also", "both", "which", "who", "how", "what", "when", "where",
})


def _normalize_span(text: str) -> str:
    """Lowercase + usuń znaki interpunkcyjne — normalizacja do substring check."""
    import re as _re
    return _re.sub(r"[^\w\s]", " ", text.lower()).strip()


def extract_key_phrases(claim: str) -> List[str]:
    """
    Wyciąga n-gramy (3-4 słowa) z claimu, filtrując stop-words.
    Zwraca listę fraz do substring-check w chunkach.
    """
    import re as _re
    words = _re.findall(r'\b[a-zA-Z]{3,}\b', claim.lower())
    # Zachowaj tylko non-stop-words w gramsach
    phrases: List[str] = []
    for n in CITATION_NGRAM_SIZES:
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            # Min 2 znaczące słowa (nie stop-words)
            significant = [w for w in gram if w not in CITATION_STOP_WORDS]
            if len(significant) >= 2:
                phrases.append(" ".join(gram))
    # Deduplikuj zachowując kolejność
    seen: set = set()
    deduped: List[str] = []
    for p in phrases:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped[:20]  # max 20 fraz per claim


def _verify_single_claim(
    claim: str,
    chunks: List[tuple],  # lista (url, domain, chunk_text) z RAG
) -> dict:
    """
    Sprawdza czy claim ma dosłowne pokrycie w chunkach RAG.

    Dwa tryby (jak w agentic_typed_rag_pydanticai + rozszerzenie dla parafraz):
    1. STRICT: n-gram substring match (verbatim, jak tutorial)
    2. PROXIMITY: 3+ kluczowe tokeny w oknie 30 słów (dla parafraz Queen)

    Returns dict:
        verified      : bool
        match_url     : str | None
        match_domain  : str | None
        match_span    : str | None  (znaleziona fraza)
        match_mode    : "strict" | "proximity" | None
        phrases_tried : int
        phrases_found : int
    """
    import re as _re5

    phrases = extract_key_phrases(claim)
    if not phrases:
        return {
            "verified": False, "match_url": None, "match_domain": None,
            "match_span": None, "match_mode": None, "phrases_tried": 0, "phrases_found": 0,
        }

    phrases_found = 0
    best_match: Optional[dict] = None

    # --- Pass 1: STRICT n-gram substring ---
    for phrase in phrases:
        norm_phrase = _normalize_span(phrase)
        if len(norm_phrase) < CITATION_MIN_SPAN_CHARS:
            continue
        for url, domain, chunk_text in chunks:
            norm_chunk = _normalize_span(chunk_text or "")
            if norm_phrase in norm_chunk:
                phrases_found += 1
                if best_match is None:
                    best_match = {
                        "verified": True, "match_url": url, "match_domain": domain,
                        "match_span": phrase, "match_mode": "strict",
                        "phrases_tried": len(phrases), "phrases_found": 0,
                    }
                break

    if best_match is not None:
        best_match["phrases_found"] = phrases_found
        return best_match

    # --- Pass 2: PROXIMITY — 3+ kluczowe tokeny w oknie 30 słów ---
    # Wyciągnij znaczące tokeny z claimu (non-stop, 4+ znaków)
    sig_tokens = [
        w for w in _re5.findall(r'\b[a-zA-Z]{4,}\b', claim.lower())
        if w not in CITATION_STOP_WORDS
    ]
    if len(sig_tokens) < 3:
        return {
            "verified": False, "match_url": None, "match_domain": None,
            "match_span": None, "match_mode": None,
            "phrases_tried": len(phrases), "phrases_found": 0,
        }

    sig_set = set(sig_tokens)
    PROXIMITY_WINDOW = 30
    MIN_TOKEN_HITS = min(3, len(sig_tokens))

    for url, domain, chunk_text in chunks:
        chunk_words = _re5.findall(r'\b[a-zA-Z]{4,}\b', (chunk_text or "").lower())
        # Przesuń okno 30 słów po chunkach
        for i in range(max(1, len(chunk_words) - PROXIMITY_WINDOW + 1)):
            window = set(chunk_words[i:i + PROXIMITY_WINDOW])
            hits = sig_set & window
            if len(hits) >= MIN_TOKEN_HITS:
                span = " ".join(list(hits)[:4])
                best_match = {
                    "verified": True, "match_url": url, "match_domain": domain,
                    "match_span": span, "match_mode": "proximity",
                    "phrases_tried": len(phrases), "phrases_found": len(hits),
                }
                break
        if best_match:
            break

    if best_match:
        return best_match

    return {
        "verified": False, "match_url": None, "match_domain": None,
        "match_span": None, "match_mode": None,
        "phrases_tried": len(phrases), "phrases_found": 0,
    }


def verify_citations(
    mission_id: str,
    rag_context: str,
    con: Any,
    *,
    update_hive_claims: bool = True,
) -> dict:
    """
    Weryfikuje claims z hive_claims dla danej misji — sprawdza pokrycie
    verbatim w chunkach RAG z rag_context.

    Wzorzec: agentic_typed_rag_pydanticai — _valid_citations() substring check.

    Parameters
    ----------
    mission_id        : ID misji
    rag_context       : wynik cross_mission_retrieve (stringowy blok)
    con               : PostgreSQL connection (write dla update)
    update_hive_claims : jeśli True, doda kolumnę rag_verified do hive_claims

    Returns
    -------
    dict z:
        total_claims    : int
        verified_count  : int
        unverified_count: int
        verification_rate: float
        unverified_claims: list[str]  (do logowania)
    """
    import re as _re4

    # --- Wyciągnij chunki z rag_context string ---
    # Format: [SOURCE N] url | domain | rrf:X [PRIMARY/CROSS]
    #         tekst chunku
    chunks: List[tuple] = []  # (url, domain, text)
    current_url = ""
    current_domain = ""
    current_lines: List[str] = []

    for line in rag_context.split("\n"):
        source_match = _re4.match(r'\[SOURCE \d+\] (\S+) \| ([^|]+) \|', line)
        if source_match:
            if current_url and current_lines:
                chunks.append((current_url, current_domain, " ".join(current_lines)))
            current_url = source_match.group(1)
            current_domain = source_match.group(2).strip()
            current_lines = []
        elif current_url and line.strip():
            current_lines.append(line.strip())

    # Ostatni chunk
    if current_url and current_lines:
        chunks.append((current_url, current_domain, " ".join(current_lines)))

    if not chunks:
        return {
            "total_claims": 0, "verified_count": 0, "unverified_count": 0,
            "verification_rate": 0.0, "unverified_claims": [],
        }

    # --- Pobierz claims z DB ---
    try:
        claim_rows = con.execute(
            """
            SELECT id, claim, source_url
            FROM hive_claims
            WHERE mission_id = ?
              AND claim IS NOT NULL
              AND LENGTH(claim) > 20
              AND claim_type != 'FALSIFICATION'
            ORDER BY id
            """,
            [mission_id],
        ).fetchall()
    except Exception:
        claim_rows = []

    if not claim_rows:
        return {
            "total_claims": 0, "verified_count": 0, "unverified_count": 0,
            "verification_rate": 0.0, "unverified_claims": [],
        }

    # --- Dodaj kolumnę rag_verified jeśli nie istnieje ---
    if update_hive_claims:
        try:
            existing_cols = [r[0] for r in con.execute("DESCRIBE hive_claims").fetchall()]
            if "rag_verified" not in existing_cols:
                con.execute("ALTER TABLE hive_claims ADD COLUMN rag_verified BOOLEAN DEFAULT FALSE")
            if "rag_match_url" not in existing_cols:
                con.execute("ALTER TABLE hive_claims ADD COLUMN rag_match_url VARCHAR")
            if "rag_match_span" not in existing_cols:
                con.execute("ALTER TABLE hive_claims ADD COLUMN rag_match_span VARCHAR")
        except Exception as exc:
            log.warning("Citation: alter table failed: %s", exc)
            update_hive_claims = False

    # --- Weryfikuj każdy claim ---
    verified_count = 0
    unverified_claims: List[str] = []

    for claim_id, claim_text, claim_source_url in claim_rows:
        result = _verify_single_claim(claim_text, chunks)

        if result["verified"]:
            verified_count += 1
            if update_hive_claims:
                try:
                    con.execute(
                        "UPDATE hive_claims SET rag_verified=TRUE, rag_match_url=?, rag_match_span=? WHERE id=?",
                        [result["match_url"], result["match_span"], claim_id],
                    )
                except Exception:
                    pass
        else:
            unverified_claims.append(claim_text[:120])
            if update_hive_claims:
                try:
                    con.execute(
                        "UPDATE hive_claims SET rag_verified=FALSE WHERE id=?",
                        [claim_id],
                    )
                except Exception:
                    pass

    total = len(claim_rows)
    unverified_count = total - verified_count
    verification_rate = verified_count / total if total > 0 else 0.0

    return {
        "total_claims": total,
        "verified_count": verified_count,
        "unverified_count": unverified_count,
        "verification_rate": round(verification_rate, 3),
        "unverified_claims": unverified_claims[:10],  # top 10 do logowania
    }


# ---------------------------------------------------------------------------
# RAG Evaluation — automatyczny scoring jakości retrieval po każdej misji
# ---------------------------------------------------------------------------
#
# Problem:
#   HIVE nie miało żadnego mechanizmu mierzenia czy RAG faktycznie pomaga.
#   Queen mogła dostawać 0 relevantnych chunków i nikt tego nie zauważał poza
#   logiem "No relevant chunks". Brak tendencji czasowych, brak porównania
#   między misjami, brak sygnału do strojenia progów.
#
# Rozwiązanie:
#   evaluate_rag() oblicza 5 metryk po każdym retrieve — zero LLM calls,
#   deterministycznie, w < 5ms. Wyniki zapisywane do hive_rag_eval.
#   hive2_master_intelligence.py wywołuje evaluate_rag() po Step 1 i loguje
#   podsumowanie do konsoli.
#
# Metryki:
#   context_precision  — float [0,1]: ile tokenów z rag_context pojawia się
#                        w synthesis (proxy "czy Queen użyła kontekstu")
#   coverage_ratio     — float [0,1]: retrieved_chunks / top_k_requested
#   source_diversity   — int: liczba unikalnych domen w retrieved context
#   cross_mission_rate — float [0,1]: cross-mission chunks / total chunks
#   bm25_lift          — float [0,1]: chunk_ids tylko z BM25 / total chunks
#                        (mierzy ile BM25 wniosło ponad czysty vector)
#   retrieval_latency_ms — float: czas retrieve w ms (mierzony z zewnątrz)

RAG_EVAL_TABLE = "hive_rag_eval"


def _ensure_eval_schema(con: Any) -> None:
    """Tworzy tabelę hive_rag_eval jeśli nie istnieje."""
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {RAG_EVAL_TABLE} (
            id                   VARCHAR DEFAULT gen_random_uuid(),
            mission_id           VARCHAR NOT NULL,
            query                VARCHAR,
            retrieved_chunks     INTEGER,
            top_k_requested      INTEGER,
            coverage_ratio       FLOAT,
            source_diversity     INTEGER,
            cross_mission_rate   FLOAT,
            bm25_lift            FLOAT,
            context_precision    FLOAT,
            retrieval_latency_ms FLOAT,
            has_cross_mission    BOOLEAN,
            eval_flags           VARCHAR,
            created_at           TIMESTAMP DEFAULT now(),
            PRIMARY KEY (id)
        )
    """)


def evaluate_rag(
    query: str,
    rag_context: str,
    mission_id: str,
    con: Any,
    *,
    top_k_requested: int = 10,
    synthesis_text: str = "",
    retrieval_latency_ms: float = 0.0,
    vec_chunk_ids: Optional[List[str]] = None,
    bm25_chunk_ids: Optional[List[str]] = None,
) -> dict:
    """
    Oblicza metryki jakości RAG i zapisuje do hive_rag_eval.

    Parameters
    ----------
    query             : zapytanie Queen
    rag_context       : wynik cross_mission_retrieve / hybrid_retrieve
    mission_id        : ID misji
    con               : połączenie PostgreSQL (write)
    top_k_requested   : ile chunków było żądanych
    synthesis_text    : opcjonalnie tekst syntezy Queen (dla context_precision)
    retrieval_latency_ms : czas retrieve w ms
    vec_chunk_ids     : opcjonalnie lista IDs z pure vector (dla bm25_lift)
    bm25_chunk_ids    : opcjonalnie lista IDs z BM25 (dla bm25_lift)

    Returns
    -------
    dict z metrykami (zawsze, nawet gdy rag_context jest pusty)
    """
    import re as _re3

    # --- podstawowe liczenie z rag_context string ---
    source_lines = [l for l in rag_context.split("\n") if l.startswith("[SOURCE ")]
    retrieved_chunks = len(source_lines)

    # Source diversity — unikalne domeny z "[SOURCE N] url | domain |"
    domains: set = set()
    for line in source_lines:
        parts = line.split("|")
        if len(parts) >= 2:
            domains.add(parts[1].strip())
    source_diversity = len(domains)

    # Cross-mission rate
    cross_count = rag_context.count("[CROSS:")
    cross_mission_rate = cross_count / retrieved_chunks if retrieved_chunks > 0 else 0.0
    has_cross_mission = cross_count > 0

    # Coverage ratio
    coverage_ratio = min(1.0, retrieved_chunks / top_k_requested) if top_k_requested > 0 else 0.0

    # BM25 lift — chunk_ids które są TYLKO w BM25 (nie w vector)
    bm25_lift = 0.0
    if vec_chunk_ids is not None and bm25_chunk_ids is not None:
        vec_set = set(vec_chunk_ids)
        bm25_only = [cid for cid in bm25_chunk_ids if cid not in vec_set]
        total = len(set(vec_chunk_ids) | set(bm25_chunk_ids))
        bm25_lift = len(bm25_only) / total if total > 0 else 0.0

    # Context precision — czy Queen użyła kontekstu w syntezie
    # Proxy: overlap tokenów między rag_context a synthesis_text
    context_precision = 0.0
    if synthesis_text and retrieved_chunks > 0:
        # Wyciągnij fragment tekstów chunków (bez headerów SOURCE)
        chunk_texts: List[str] = []
        current: List[str] = []
        for line in rag_context.split("\n"):
            if line.startswith("[SOURCE "):
                if current:
                    chunk_texts.append(" ".join(current))
                current = []
            else:
                current.append(line)
        if current:
            chunk_texts.append(" ".join(current))

        # Tokenizuj synthesis i chunk texts
        synth_tokens = set(_re3.findall(r'\b[a-zA-Z]{4,}\b', synthesis_text.lower()))
        if synth_tokens:
            used_count = 0
            for ct in chunk_texts:
                ct_tokens = set(_re3.findall(r'\b[a-zA-Z]{4,}\b', ct.lower()))
                if len(synth_tokens & ct_tokens) >= 3:  # min 3 wspólne tokeny
                    used_count += 1
            context_precision = used_count / len(chunk_texts) if chunk_texts else 0.0

    # ---------------------------------------------------------------------------
    # Eval flags — P01–P12 z rag_failure_diagnostics_clinic
    # Źródło: awesome-llm-apps/rag_tutorials/rag_failure_diagnostics_clinic
    # Każda flaga = konkretny wzorzec błędu RAG zamiast generycznego opisu
    # ---------------------------------------------------------------------------
    flags: List[str] = []

    if retrieved_chunks == 0:
        # P05: router nie znalazł żadnych chunków — brak danych w indeksie lub
        # query trafiło do złej ścieżki (brak misji, zły mission_id)
        flags.append("P05:NO_CHUNKS")

    elif coverage_ratio < 0.5:
        # P04: indeks zwrócił za mało chunków względem żądanego top_k
        # — staleness lub niepełna indeksacja dokumentów
        flags.append("P04:LOW_COVERAGE")

    if source_diversity <= 1 and retrieved_chunks > 3:
        # P12: wszystkie chunki z jednej domeny — echo-chamber, brak dywersyfikacji
        # źródeł; może też oznaczać P03 (embedding faworyzuje jeden corpus)
        flags.append("P12:LOW_DIVERSITY")

    if context_precision < 0.2 and synthesis_text and retrieved_chunks > 0:
        # P01: Queen zignorowała retrieved context — grounding drift
        # Odpowiedź nie używa pobranych dokumentów (hallucination risk)
        flags.append("P01:GROUNDING_DRIFT")

    if cross_mission_rate > 0.5:
        # P06: ponad połowa chunków z obcych misji — Queen może stracić focus
        # na zadaniu misji (reasoning drift przez zbyt szeroki kontekst)
        flags.append("P06:HIGH_CROSS_DRIFT")

    if retrieval_latency_ms > 8000:
        # P10: retrieve zajął > 8s — problem z gotowością indeksu lub lockiem DB
        flags.append("P10:SLOW_RETRIEVAL")

    if bm25_lift == 0.0 and vec_chunk_ids is not None and bm25_chunk_ids is not None:
        # P03: BM25 nie wniósł nic ponad vector — embedding może dominować
        # kosztem dokładnych keyword matches (mismatch semantyczny)
        flags.append("P03:BM25_NO_LIFT")

    metrics = {
        "mission_id": mission_id,
        "query": query[:200],
        "retrieved_chunks": retrieved_chunks,
        "top_k_requested": top_k_requested,
        "coverage_ratio": round(coverage_ratio, 3),
        "source_diversity": source_diversity,
        "cross_mission_rate": round(cross_mission_rate, 3),
        "bm25_lift": round(bm25_lift, 3),
        "context_precision": round(context_precision, 3),
        "retrieval_latency_ms": round(retrieval_latency_ms, 1),
        "has_cross_mission": has_cross_mission,
        "eval_flags": ",".join(flags) if flags else "",
    }

    # Zapisz do DB
    try:
        _ensure_eval_schema(con)
        con.execute(
            f"""
            INSERT INTO {RAG_EVAL_TABLE} (
                mission_id, query, retrieved_chunks, top_k_requested,
                coverage_ratio, source_diversity, cross_mission_rate,
                bm25_lift, context_precision, retrieval_latency_ms,
                has_cross_mission, eval_flags
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                metrics["mission_id"], metrics["query"],
                metrics["retrieved_chunks"], metrics["top_k_requested"],
                metrics["coverage_ratio"], metrics["source_diversity"],
                metrics["cross_mission_rate"], metrics["bm25_lift"],
                metrics["context_precision"], metrics["retrieval_latency_ms"],
                metrics["has_cross_mission"], metrics["eval_flags"],
            ],
        )
    except Exception as exc:
        log.warning("RAG eval insert failed: %s", exc)

    return metrics


def rag_eval_summary(mission_id: str, con: Any) -> dict:
    """
    Zwraca statystyki ewaluacyjne RAG dla danej misji (avg/min/max metryk).
    Przydatne do raportu końcowego misji.
    """
    tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    if RAG_EVAL_TABLE not in tables:
        return {"mission_id": mission_id, "eval_count": 0}

    row = con.execute(
        f"""
        SELECT
            COUNT(*)                        AS eval_count,
            AVG(coverage_ratio)             AS avg_coverage,
            AVG(source_diversity)           AS avg_diversity,
            AVG(cross_mission_rate)         AS avg_cross_rate,
            AVG(bm25_lift)                  AS avg_bm25_lift,
            AVG(context_precision)          AS avg_precision,
            AVG(retrieval_latency_ms)       AS avg_latency_ms,
            SUM(CASE WHEN eval_flags LIKE '%NO_CHUNKS%' THEN 1 ELSE 0 END) AS no_chunks_count,
            SUM(CASE WHEN has_cross_mission THEN 1 ELSE 0 END) AS cross_mission_hits
        FROM {RAG_EVAL_TABLE}
        WHERE mission_id = ?
        """,
        [mission_id],
    ).fetchone()

    # Najczęstsze flagi
    flag_rows = con.execute(
        f"""
        SELECT eval_flags, COUNT(*) as cnt
        FROM {RAG_EVAL_TABLE}
        WHERE mission_id = ? AND eval_flags != ''
        GROUP BY eval_flags
        ORDER BY cnt DESC
        LIMIT 5
        """,
        [mission_id],
    ).fetchall()

    return {
        "mission_id": mission_id,
        "eval_count": row[0] or 0,
        "avg_coverage": round(float(row[1] or 0), 3),
        "avg_diversity": round(float(row[2] or 0), 1),
        "avg_cross_rate": round(float(row[3] or 0), 3),
        "avg_bm25_lift": round(float(row[4] or 0), 3),
        "avg_precision": round(float(row[5] or 0), 3),
        "avg_latency_ms": round(float(row[6] or 0), 1),
        "no_chunks_count": row[7] or 0,
        "cross_mission_hits": row[8] or 0,
        "top_flags": [r[0] for r in flag_rows],
    }




# Cross-mission config
CROSS_MIN_KEYWORD_OVERLAP = 1     # min shared keywords between query and cluster
CROSS_MAX_RELATED         = 3     # max related missions to search
CROSS_RELATED_CHUNK_K     = 8     # chunks per related mission
CROSS_RRF_K_CROSS         = 80    # slightly higher k for cross-mission (less weight vs primary)


def find_related_missions(
    query: str,
    primary_mission_id: str,
    con: Any,
    max_results: int = CROSS_MAX_RELATED,
) -> List[dict]:
    """
    Szuka misji powiązanych tematycznie z zapytaniem przez hive3_cluster_atlas.

    Algorytm (bez LLM, bez dodatkowych API calls):
    1. Tokenizuj query → zbiór słów (lowercase, min 3 znaki)
    2. Dla każdego klastra w cluster_atlas: policz overlap query_tokens ∩ theme_keywords
    3. Rankinuj klastry po overlap score
    4. Zwróć mission_ids z top klastrów (z wyłączeniem primary_mission_id)
       które mają chunks w hive_rag_chunks

    Returns list of dicts: [{mission_id, topic, cluster_theme, overlap_score}, ...]
    """
    try:
        # Sprawdź czy tabela istnieje
        tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
        if "hive3_cluster_atlas" not in tables:
            log.debug("Cross-mission: hive3_cluster_atlas not found — skipping")
            return []

        # Tokenizuj query
        import re as _re2
        query_tokens = set(
            t.lower() for t in _re2.findall(r'[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+', query)
            if len(t) >= 3
        )
        if not query_tokens:
            return []

        # Pobierz klastry z ich keywords i mission_ids
        clusters = con.execute(
            """
            SELECT id, cluster_theme, theme_keywords, mission_ids
            FROM hive3_cluster_atlas
            WHERE mission_count > 0
            ORDER BY last_seen DESC
            """
        ).fetchall()

        import json as _json2

        # Misje które mają RAG chunks
        missions_with_chunks = set(
            r[0] for r in con.execute(
                "SELECT DISTINCT mission_id FROM hive_rag_chunks"
            ).fetchall()
        )

        # Policz overlap dla każdego klastra
        scored: List[dict] = []
        for cluster_id, cluster_theme, keywords_json, mission_ids_json in clusters:
            try:
                keywords = set(
                    t.lower() for kw in _json2.loads(keywords_json or "[]")
                    for t in kw.split()
                    if len(t) >= 3
                )
                mission_ids = _json2.loads(mission_ids_json or "[]")
            except Exception:
                continue

            overlap = len(query_tokens & keywords)
            if overlap < CROSS_MIN_KEYWORD_OVERLAP:
                continue

            # Dodaj każdą powiązaną misję (nie primary, ma chunks)
            for mid in mission_ids:
                # mission_ids w cluster_atlas mogą mieć dłuższy suffix niż w hive_missions
                # Sprawdzamy prefix match: 'hive_1784501198_0fa5d0'.startswith('hive_1784501198_0fa5') → True
                is_primary = (
                    mid == primary_mission_id
                    or mid.startswith(primary_mission_id)
                    or primary_mission_id.startswith(mid)
                )
                if is_primary:
                    continue
                if mid not in missions_with_chunks:
                    continue
                scored.append({
                    "mission_id": mid,
                    "cluster_theme": cluster_theme,
                    "overlap_score": overlap,
                    "cluster_id": cluster_id,
                })

        if not scored:
            return []

        # Deduplikuj po mission_id (weź max overlap)
        best: dict = {}
        for s in scored:
            mid = s["mission_id"]
            if mid not in best or s["overlap_score"] > best[mid]["overlap_score"]:
                best[mid] = s

        # Wzbogać o topic z hive_missions
        related_sorted = sorted(best.values(), key=lambda x: x["overlap_score"], reverse=True)
        top = related_sorted[:max_results]

        if top:
            mid_list = [r["mission_id"] for r in top]
            placeholders = ", ".join("?" * len(mid_list))
            topics = {
                r[0]: r[1] for r in con.execute(
                    f"SELECT id, topic FROM hive_missions WHERE id IN ({placeholders})",
                    mid_list
                ).fetchall()
            }
            for r in top:
                r["topic"] = topics.get(r["mission_id"], "unknown")

        return top

    except Exception as exc:
        log.warning("Cross-mission find_related_missions error: %s", exc)
        return []


def cross_mission_retrieve(
    query: str,
    primary_mission_id: str,
    con: Any,
    top_k: int = HYBRID_FINAL_TOP,
    min_similarity: float = 0.30,
    max_related: int = CROSS_MAX_RELATED,
) -> str:
    """
    Cross-Mission RAG: szuka w bieżącej misji + tematycznie powiązanych misjach.

    Problem który rozwiązuje:
        HIVE prowadzi wiele misji na powiązane tematy (np. UAE sanctions, Turkish sanctions,
        Russian oil). Queen pytając o jedną misję może nie mieć dostępu do uzupełniających
        danych z poprzednich misji. Cross-mission routing łączy te bazy wiedzy.

    Przykład:
        Misja: "Turkish intermediaries EU sanctions"
        Query: "jak firmy omijają sankcje przez Dubai"
        → primary: 0 relevantnych chunków (brak danych o Dubai)
        → related: hive_1784531018_65a0 "UAE Dubai free zones sanctions evasion" → 8 chunków ✓
        → wynik: Queen dostaje uzupełniające dane z pokrewnej misji

    Pipeline:
        1. hybrid_retrieve(primary_mission_id) → primary chunks
        2. find_related_missions() → top-3 powiązane misje (via cluster_atlas)
        3. hybrid_retrieve(related_mission) dla każdej → related chunks
        4. RRF fusion wszystkich wyników (primary chunks: niższy k = wyższy priorytet)
        5. Format z adnotacją [PRIMARY] / [CROSS:mission_id]

    Fallback: jeśli brak related missions lub 0 powiązanych chunków → czyste hybrid_retrieve.

    Returns
    -------
    str  context block z adnotacjami źródła misji.
    """
    ensure_schema(con)

    count = con.execute(
        "SELECT COUNT(*) FROM hive_rag_chunks WHERE mission_id=?", [primary_mission_id]
    ).fetchone()[0]
    if count == 0:
        log.warning("Cross-mission: No chunks for mission %s", primary_mission_id)
        return ""

    # --- 1. Primary mission hybrid search ---
    q_emb = embed_text(query)

    def _vec_search(mission_id: str, k: int) -> List[tuple]:
        rows = con.execute(
            """
            SELECT id, array_cosine_similarity(embedding, ?::FLOAT[512]) AS sim
            FROM hive_rag_chunks
            WHERE mission_id = ?
            ORDER BY sim DESC
            LIMIT ?
            """,
            [q_emb, mission_id, k],
        ).fetchall()
        return [(r[0], r[1]) for r in rows if r[1] is not None and r[1] >= min_similarity]

    primary_vec = _vec_search(primary_mission_id, HYBRID_VECTOR_TOP)
    primary_bm25 = _bm25_search(query, primary_mission_id, con, top_n=HYBRID_BM25_TOP)

    # --- 2. Znajdź powiązane misje ---
    related = find_related_missions(query, primary_mission_id, con, max_results=max_related)

    if related:
        log.info(
            "Cross-mission: found %d related missions: %s",
            len(related),
            ", ".join(f"{r['mission_id'][:20]} (overlap:{r['overlap_score']})" for r in related)
        )
    else:
        log.info("Cross-mission: no related missions found — using primary only")

    # --- 3. Related missions search ---
    # Buduj połączony ranking: primary ma niższy k (wyższy priorytet w RRF)
    all_scores: dict = {}

    # Primary — k=HYBRID_RRF_K (standardowy priorytet)
    if primary_bm25:
        p_fused = _rrf_fusion(primary_vec, primary_bm25, k=HYBRID_RRF_K, top_n=HYBRID_VECTOR_TOP)
    else:
        p_fused = primary_vec[:HYBRID_VECTOR_TOP]

    for rank, (cid, _) in enumerate(p_fused, start=1):
        all_scores[cid] = all_scores.get(cid, 0.0) + 1.0 / (HYBRID_RRF_K + rank)

    # Śledź które chunki należą do której misji
    chunk_mission_map: dict = {cid: primary_mission_id for cid, _ in p_fused}

    # Related missions — k=CROSS_RRF_K_CROSS (nieco wyższe k = niższy priorytet vs primary)
    for rel in related:
        rel_mid = rel["mission_id"]
        rel_vec = _vec_search(rel_mid, CROSS_RELATED_CHUNK_K * 2)
        rel_bm25 = _bm25_search(query, rel_mid, con, top_n=CROSS_RELATED_CHUNK_K * 2)

        if not rel_vec and not rel_bm25:
            continue

        if rel_bm25:
            rel_fused = _rrf_fusion(rel_vec, rel_bm25, k=HYBRID_RRF_K, top_n=CROSS_RELATED_CHUNK_K)
        else:
            rel_fused = rel_vec[:CROSS_RELATED_CHUNK_K]

        for rank, (cid, _) in enumerate(rel_fused, start=1):
            all_scores[cid] = all_scores.get(cid, 0.0) + 1.0 / (CROSS_RRF_K_CROSS + rank)
            chunk_mission_map[cid] = rel_mid

    if not all_scores:
        return ""

    # --- 4. Final ranking ---
    final_ranked = sorted(all_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

    if not final_ranked:
        return ""

    # Fetch chunk text
    chunk_ids = [cid for cid, _ in final_ranked]
    placeholders = ", ".join("?" * len(chunk_ids))
    chunk_rows = con.execute(
        f"""
        SELECT id, chunk_text, url, domain, context_header
        FROM hive_rag_chunks
        WHERE id IN ({placeholders})
        """,
        chunk_ids,
    ).fetchall()
    chunk_map = {r[0]: r for r in chunk_rows}

    # Zlicz ile chunków z każdej misji
    mission_counts: dict = {}
    for cid, _ in final_ranked:
        mid = chunk_mission_map.get(cid, primary_mission_id)
        mission_counts[mid] = mission_counts.get(mid, 0) + 1

    log.info(
        "Cross-mission: final %d chunks — primary:%d, related: %s",
        len(final_ranked),
        mission_counts.get(primary_mission_id, 0),
        ", ".join(
            f"{mid[:20]}:{cnt}" for mid, cnt in mission_counts.items()
            if mid != primary_mission_id
        ) or "none",
    )

    parts: List[str] = []
    for idx, (cid, rrf_score) in enumerate(final_ranked, start=1):
        if cid not in chunk_map:
            continue
        _, chunk_text, url, domain, ctx_header = chunk_map[cid]
        src_mid = chunk_mission_map.get(cid, primary_mission_id)

        if src_mid == primary_mission_id:
            mission_tag = "[PRIMARY]"
        else:
            # Pokaż skróconą nazwę misji i temat
            rel_info = next((r for r in related if r["mission_id"] == src_mid), None)
            topic_short = rel_info["topic"][:40] if rel_info else src_mid[:20]
            mission_tag = f"[CROSS: {topic_short}]"

        header = f"[SOURCE {idx}] {url} | {domain} | rrf:{rrf_score:.4f} {mission_tag}"
        if ctx_header:
            header += f"\n  context: {ctx_header}"
        parts.append(f"{header}\n{chunk_text}")

    return "\n\n".join(parts)


def cross_mission_retrieve_enhanced(
    query: str,
    primary_mission_id: str,
    con: Any,
    top_k: int = HYBRID_FINAL_TOP,
    min_similarity: float = 0.30,
    max_related: int = CROSS_MAX_RELATED,
    enable_rerank: bool = True,
    enable_web_fallback: bool = True,
) -> str:
    """
    cross_mission_retrieve + P3 reranking + P2 web fallback.

    Używane przez hive2_master_intelligence zamiast cross_mission_retrieve,
    żeby P3 CrossEncoder i P2 web fallback wchodziły do syntezy.
    """
    ctx = cross_mission_retrieve(
        query, primary_mission_id, con,
        top_k=top_k,
        min_similarity=min_similarity,
        max_related=max_related,
    )

    if ctx:
        # ── P3: CrossEncoder reranking ───────────────────────────────────────
        if enable_rerank:
            try:
                from behive.engine.rag_extensions import rerank_text_blocks
                ctx = rerank_text_blocks(query, ctx, top_k=top_k)
                log.info("Cross-mission P3:RERANK applied")
            except ImportError:
                pass
        return ctx

    # ── P2: Web fallback gdy 0 chunks ───────────────────────────────────────
    if enable_web_fallback:
        log.warning("Cross-mission: 0 chunks — triggering P2:WEB_FALLBACK")
        try:
            from behive.engine.rag_extensions import web_search_fallback
            return web_search_fallback(query, max_results=4)
        except ImportError:
            pass

    return ""


def _cli_build(mission_id: str) -> None:
    con = _db_connect(read_only=False)
    inserted = build_rag_index(mission_id, con)
    print(f"Build done. {inserted} new chunks inserted for mission {mission_id}.")
    con.close()


def _cli_query(mission_id: str, query_text: str) -> None:
    con = _db_connect(read_only=False)
    result = retrieve(query_text, mission_id, con)
    con.close()
    if result:
        print(result)
    else:
        print("(no results above similarity threshold)")


def _cli_status(mission_id: str) -> None:
    con = _db_connect(read_only=False)
    info = rag_status(mission_id, con)
    con.close()
    print(f"Mission       : {info['mission_id']}")
    print(f"Total chunks  : {info['total_chunks']}")
    print(f"Embedded docs : {info['embedded_docs']} / {info['total_docs']}")
    print(f"Avg chunk size: {info['avg_chunk_words']} words")
    print(f"First embedded: {info['first_embedded']}")
    print(f"Last embedded : {info['last_embedded']}")


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage:")
        print("  python3 hive2_rag.py build  <mission_id>")
        print("  python3 hive2_rag.py query  <mission_id> \"query text\"")
        print("  python3 hive2_rag.py status <mission_id>")
        sys.exit(1)

    command = sys.argv[1].lower()
    mission_id = sys.argv[2]

    if command == "build":
        _cli_build(mission_id)
    elif command == "query":
        if len(sys.argv) < 4:
            print("Error: query command requires a query string.")
            sys.exit(1)
        query_text = sys.argv[3]
        _cli_query(mission_id, query_text)
    elif command == "status":
        _cli_status(mission_id)
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
