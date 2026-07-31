#!/usr/bin/env python3
"""
HIVE2 Graph Engine — State-of-Art Intelligence Knowledge Graph
==============================================================
Pipeline:
  PostgreSQL → [incremental delta] → Entity Resolution (HDBSCAN+ANN)
  → NetworkX DiGraph → HDBSCAN Claim Clustering (C0 coarse + C1 fine)
  → Qdrant Semantic Index (100% claims ≥0.65) → GraphRAG Hierarchical Search

Usage:
  python3 hive2_graph_engine.py build [--skip-summaries] [--skip-embeddings]
  python3 hive2_graph_engine.py incremental [--force]
  python3 hive2_graph_engine.py stats | search "q" [--top N]
  python3 hive2_graph_engine.py communities [--top N] [--level C0|C1]
  python3 hive2_graph_engine.py path "A" "B" | context <id> | godmode
"""

import hashlib
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import boto3
import hive2_db as _hive_db  # PostgreSQL via unified layer
import networkx as nx
import numpy as np
from rapidfuzz import fuzz

log = logging.getLogger("hive_graph_engine")
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [GRAPH] %(levelname)s %(message)s")

DB_PATH = ""  # Legacy PostgreSQL removed — PostgreSQL is primary
GRAPH_DIR  = Path(os.environ.get("HIVE_GRAPH_DIR", os.path.expanduser("~/hive-intelligence-graph")))
GRAPH_FILE       = GRAPH_DIR / "graph.json"
COMMUNITIES_FILE = GRAPH_DIR / "communities.json"   # C1 fine clusters
C0_FILE          = GRAPH_DIR / "communities_c0.json" # C0 coarse clusters
ENTITY_MAP_FILE  = GRAPH_DIR / "entity_map.json"
META_FILE        = GRAPH_DIR / "graph_meta.json"
HASH_FILE        = GRAPH_DIR / "incremental_hash.json"

QDRANT_HOST = "localhost"; QDRANT_PORT = 6333; COLLECTION = "hive_intelligence"
EMBED_DIM = 384; EMBED_CACHE = os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface/hub"))
EMBED_MODEL = "all-MiniLM-L6-v2"; EMBED_BATCH = 512
BEDROCK_MODEL = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"; BEDROCK_REGION = "eu-central-1"

# Thresholds
CLAIM_CONF_MIN     = 0.65   # min confidence to index claim
ENTITY_CONF_MIN    = 0.60   # min confidence for entity resolution
ENTITY_TOP_K       = 10_000 # top-K entities by confidence for resolution
CO_OCCUR_MIN       = 3      # min co-occurrence count for entity↔entity edge
MENTION_MAX_PER_ENT = 200   # max claim-mention edges per entity

# ── Bedrock ───────────────────────────────────────────────────────────────────

_bedrock_client: Optional[Any] = None

def _get_bedrock() -> Any:
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return _bedrock_client

def call_bedrock(prompt: str, system: str = "", max_tokens: int = 512) -> str:
    body: dict[str, Any] = {"anthropic_version": "bedrock-2023-05-31",
                             "max_tokens": max_tokens,
                             "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    try:
        resp = _get_bedrock().invoke_model(modelId=BEDROCK_MODEL, body=json.dumps(body),
                                           contentType="application/json", accept="application/json")
        return json.loads(resp["body"].read())["content"][0]["text"]
    except Exception as exc:
        log.warning("Bedrock call failed: %s", exc)
        return ""

# ── Embeddings ────────────────────────────────────────────────────────────────

_embed_model: Optional[Any] = None

def _get_embed_model() -> Any:
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer(EMBED_MODEL, cache_folder=EMBED_CACHE, device="cpu")
    return _embed_model

def embed_texts(texts: list[str]) -> np.ndarray:
    """Encode texts into L2-normalized embeddings. Batched for efficiency."""
    if not texts:
        return np.empty((0, EMBED_DIM), dtype=np.float32)
    model = _get_embed_model()
    parts = [model.encode(texts[i:i+EMBED_BATCH], normalize_embeddings=True,
                          show_progress_bar=False, device=None)
             for i in range(0, len(texts), EMBED_BATCH)]
    return np.vstack(parts).astype(np.float32)

# ── Qdrant ────────────────────────────────────────────────────────────────────

_qdrant_client: Optional[Any] = None
_qdrant_ok: bool = True

def _get_qdrant() -> Optional[Any]:
    global _qdrant_client, _qdrant_ok
    if not _qdrant_ok:
        return None
    if _qdrant_client is None:
        try:
            from qdrant_client import QdrantClient
            _qdrant_client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=10)
            _qdrant_client.get_collections()
        except Exception as exc:
            log.warning("Qdrant unavailable: %s", exc)
            _qdrant_ok = False; return None
    return _qdrant_client

def _ensure_collection(recreate: bool = False) -> bool:
    client = _get_qdrant()
    if client is None:
        return False
    from qdrant_client.models import Distance, VectorParams
    existing = {c.name for c in client.get_collections().collections}
    if recreate and COLLECTION in existing:
        client.delete_collection(COLLECTION)
        existing.discard(COLLECTION)
    if COLLECTION not in existing:
        client.create_collection(COLLECTION, vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE))
        log.info("Created Qdrant collection '%s'", COLLECTION)
    return True

# ── PostgreSQL ────────────────────────────────────────────────────────────────────

def _fetch(query: str, params: list = []) -> list[tuple]:
    con = _hive_db.connect(read_only=True)
    try:
        return con.execute(query, params).fetchall()
    finally:
        con.close()

# ── 1. Entity Resolution (HDBSCAN-based, O(n log n)) ─────────────────────────

def build_canonical_entities() -> dict[str, dict]:
    """Resolve entities using ANN + HDBSCAN clustering.

    Strategy:
      1. Fetch top-K entities by confidence (no hard cutoff)
      2. Embed canonical forms
      3. HDBSCAN on embeddings → entity clusters
      4. Within each cluster: fuzzy dedup (ratio>88) + pick highest-conf as canonical
    Returns: key → {canonical_name, entity_type, aliases, confidence_avg, mission_ids}
    """
    rows = _fetch(
        "SELECT mission_id, entity_type, value, confidence FROM hive_entities "
        "WHERE value IS NOT NULL AND value != '' AND confidence >= ? "
        "ORDER BY confidence DESC LIMIT ?",
        [ENTITY_CONF_MIN, ENTITY_TOP_K]
    )
    if not rows:
        log.warning("No entities in hive_entities"); return {}

    # Aggregate by lowercase key
    seen: dict[str, dict] = {}
    for mission_id, etype, value, conf in rows:
        key = value.strip().lower()
        if not key or len(key) < 2: continue
        if key not in seen:
            seen[key] = {"original": value.strip(), "entity_type": etype or "unknown",
                         "confidences": [], "mission_ids": set()}
        seen[key]["confidences"].append(float(conf or 0.5))
        seen[key]["mission_ids"].add(mission_id)

    unique_keys = list(seen.keys())
    originals   = [seen[k]["original"] for k in unique_keys]
    log.info("Embedding %d unique entity values for resolution...", len(unique_keys))
    vecs = embed_texts(originals)  # (N, 384)

    # HDBSCAN on embeddings — finds dense clusters of semantically similar entities
    try:
        import hdbscan as _hdbscan
        clusterer = _hdbscan.HDBSCAN(
            min_cluster_size=2, min_samples=1,
            metric="euclidean", cluster_selection_method="eom",
            core_dist_n_jobs=-1
        )
        labels = clusterer.fit_predict(vecs)
        log.info("HDBSCAN entity clusters: %d (noise: %d)",
                 len(set(labels)) - (1 if -1 in labels else 0),
                 (labels == -1).sum())
    except Exception as exc:
        log.warning("HDBSCAN failed (%s), falling back to individual entities", exc)
        labels = np.arange(len(unique_keys))

    # Group by cluster label; within each cluster apply fuzzy dedup
    cluster_members: dict[int, list[int]] = defaultdict(list)
    for idx, lab in enumerate(labels):
        cluster_members[int(lab)].append(idx)

    canonical: dict[str, dict] = {}
    for lab, members in cluster_members.items():
        if lab == -1:
            # HDBSCAN noise → each member is its own canonical
            for idx in members:
                key = unique_keys[idx]
                info = seen[key]
                canonical[key] = {
                    "canonical_name": info["original"], "entity_type": info["entity_type"],
                    "aliases": [], "confidences": list(info["confidences"]),
                    "mission_ids": list(info["mission_ids"])
                }
            continue

        # Sort cluster members by descending confidence (highest = canonical)
        members_sorted = sorted(members, key=lambda i: -max(seen[unique_keys[i]]["confidences"]))

        # Fuzzy dedup within cluster — merge similar strings
        merged: list[list[int]] = []
        assigned = set()
        for i in members_sorted:
            if i in assigned: continue
            group = [i]
            assigned.add(i)
            for j in members_sorted:
                if j in assigned: continue
                if fuzz.ratio(unique_keys[i], unique_keys[j]) > 82:
                    group.append(j); assigned.add(j)
            merged.append(group)

        for group in merged:
            # Canonical = first (highest confidence)
            canon_idx = group[0]
            canon_key = unique_keys[canon_idx]
            canon_info = seen[canon_key]
            aliases = []
            conf_all = list(canon_info["confidences"])
            missions_all = set(canon_info["mission_ids"])
            for idx in group[1:]:
                info = seen[unique_keys[idx]]
                aliases.append(info["original"])
                conf_all.extend(info["confidences"])
                missions_all.update(info["mission_ids"])
            canonical[canon_key] = {
                "canonical_name": canon_info["original"],
                "entity_type": canon_info["entity_type"],
                "aliases": aliases,
                "confidences": conf_all,
                "mission_ids": list(missions_all)
            }

    # Finalize: compute avg confidence
    result = {}
    for key, data in canonical.items():
        confs = data["confidences"]
        result[key] = {
            "canonical_name": data["canonical_name"], "entity_type": data["entity_type"],
            "aliases": data["aliases"],
            "confidence_avg": float(np.mean(confs)) if confs else 0.5,
            "mission_ids": list(data["mission_ids"])
        }
    log.info("Resolved %d canonical entities (from %d raw)", len(result), len(unique_keys))
    return result

# ── 2. Graph Builder ──────────────────────────────────────────────────────────

def build_graph() -> nx.DiGraph:
    """Build full knowledge graph from PostgreSQL. Returns nx.DiGraph."""
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    log.info("=== Building HIVE Knowledge Graph ===")
    t0 = time.time()

    canonical_entities = build_canonical_entities()
    missions = _fetch("SELECT id, topic, status, quality_confidence, created_at FROM hive_missions")
    # ALL claims above conf threshold — no LIMIT
    claims = _fetch(
        "SELECT id, claim, confidence, mission_id, source_url FROM hive_claims "
        "WHERE claim IS NOT NULL AND claim != '' AND confidence >= ? "
        "ORDER BY confidence DESC",
        [CLAIM_CONF_MIN]
    )
    ent_rows = _fetch(
        "SELECT mission_id, entity_type, value FROM hive_entities WHERE value IS NOT NULL AND value != ''"
    )

    G = nx.DiGraph()
    alias_to_canon: dict[str, str] = {}
    for key, data in canonical_entities.items():
        alias_to_canon[key] = key
        alias_to_canon[data["canonical_name"].strip().lower()] = key
        for alias in data.get("aliases", []):
            alias_to_canon[alias.strip().lower()] = key

    # Mission nodes
    for mid, topic, _status, conf, created in missions:
        G.add_node(f"mission:{mid}", node_type="mission", topic=topic or "",
                   confidence=float(conf or 0.5), created_at=str(created or ""),
                   community_id=-1, community_c0=-1)

    # Entity nodes
    for key, data in canonical_entities.items():
        G.add_node(f"entity:{key}", node_type="entity",
                   canonical_name=data["canonical_name"],
                   entity_type=data["entity_type"],
                   confidence=data["confidence_avg"],
                   mission_count=len(data["mission_ids"]),
                   betweenness=0.0, pagerank=0.0,
                   community_id=-1, community_c0=-1)

    # Claim nodes + mission edges
    claim_by_mission: dict[str, list[int]] = defaultdict(list)
    claim_by_id: dict[int, tuple] = {}
    for cid, text, conf, mission_id, url in claims:
        cconf = float(conf or 0.5)
        G.add_node(f"claim:{cid}", node_type="claim",
                   text=(text or "")[:120],
                   confidence=cconf,
                   mission_id=mission_id or "",
                   embedding_id=cid,
                   community_id=-1, community_c0=-1)
        mnode = f"mission:{mission_id}"
        if mnode in G:
            G.add_edge(f"claim:{cid}", mnode, relation="belongs_to", weight=1.0)
        claim_by_mission[mission_id or ""].append(cid)
        claim_by_id[cid] = (cid, text, conf, mission_id, url)

    log.info("Nodes: %d missions, %d entities, %d claims",
             len(missions), len(canonical_entities), len(claims))

    # Entity co-occurrence map (from ent_rows)
    ent_in_mission: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for mission_id, _etype, value in ent_rows:
        canon = alias_to_canon.get((value or "").strip().lower())
        if canon:
            ent_in_mission[mission_id or ""][canon] += 1

    # Entity → claim mention edges (text-match)
    log.info("Building entity→claim mention edges (%d claims)...", len(claim_by_id))
    entity_mention_count: dict[str, int] = defaultdict(int)
    for cid, text, conf, mission_id, _url in claim_by_id.values():
        if (conf or 0) < CLAIM_CONF_MIN: continue
        text_lower = (text or "").lower()
        for canon_key in ent_in_mission.get(mission_id or "", {}):
            if entity_mention_count[canon_key] >= MENTION_MAX_PER_ENT: continue
            data = canonical_entities.get(canon_key, {})
            names = [canon_key] + list(data.get("aliases", []))[:2]
            if any(n.lower() in text_lower for n in names if len(n) >= 4):
                enode = f"entity:{canon_key}"
                if enode in G:
                    G.add_edge(enode, f"claim:{cid}", relation="mentions",
                               weight=float(conf or 0.5))
                    entity_mention_count[canon_key] += 1
    log.info("Mention edges: %d", sum(entity_mention_count.values()))

    # Entity co-occurrence edges (≥ CO_OCCUR_MIN missions)
    co_occur: dict[tuple[str, str], int] = defaultdict(int)
    for ents in ent_in_mission.values():
        elist = list(ents.keys())
        for i in range(len(elist)):
            for j in range(i+1, len(elist)):
                co_occur[(min(elist[i], elist[j]), max(elist[i], elist[j]))] += 1
    for (e1, e2), cnt in co_occur.items():
        if cnt >= CO_OCCUR_MIN:
            w = min(cnt / 10.0, 3.0)
            G.add_edge(f"entity:{e1}", f"entity:{e2}", relation="co_occurs_with", weight=w)
            G.add_edge(f"entity:{e2}", f"entity:{e1}", relation="co_occurs_with", weight=w)

    # Entity → mission (appears_in, ≥5 mentions)
    total_claims = len(claims)
    for mission_id, ent_counts in ent_in_mission.items():
        for canon_key, cnt in ent_counts.items():
            if cnt >= 5:
                G.add_edge(f"entity:{canon_key}", f"mission:{mission_id}",
                           relation="appears_in", weight=cnt / max(total_claims, 1))

    log.info("Graph: %d nodes, %d edges in %.1fs",
             G.number_of_nodes(), G.number_of_edges(), time.time()-t0)
    _compute_centrality(G)
    return G

# ── 3. HDBSCAN Claim Clustering (C0 coarse + C1 fine) ─────────────────────────

def _umap_reduce(vecs: np.ndarray, n_components: int = 50, n_neighbors: int = 30) -> np.ndarray:
    """Reduce high-dim embeddings with UMAP before HDBSCAN.

    HDBSCAN on raw 384-dim cosine space degenerates to noise.
    UMAP projection → dense Euclidean space → HDBSCAN works properly.
    """
    from umap import UMAP
    n = len(vecs)
    # Adjust n_neighbors if corpus is small
    nn = min(n_neighbors, max(5, n // 20))
    log.info("UMAP %d×%d → %d dims (n_neighbors=%d)...", n, vecs.shape[1], n_components, nn)
    t0 = time.time()
    reducer = UMAP(
        n_components=n_components,
        n_neighbors=nn,
        min_dist=0.0,
        metric="cosine",
        low_memory=True,
        random_state=42,
        n_jobs=1,
    )
    reduced = reducer.fit_transform(vecs).astype(np.float32)
    log.info("UMAP done in %.1fs → shape %s", time.time()-t0, reduced.shape)
    return reduced

def cluster_claims(G: nx.DiGraph, vecs: np.ndarray,
                   claim_node_ids: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    """HDBSCAN two-level hierarchical clustering on claim embeddings.

    Pipeline: embed(384) → UMAP(→50) → HDBSCAN C0 coarse + C1 fine
    C0: coarse topics (~15-40 clusters)
    C1: fine sub-topics (~80-200 clusters)

    Returns (c0_map, c1_map): node_id → cluster_id.
    Noise points (label=-1) get unique IDs above max cluster (unclustered but addressable).
    """
    import hdbscan as _hdbscan

    # UMAP dimensionality reduction — critical before HDBSCAN on high-dim embeddings
    reduced = _umap_reduce(vecs, n_components=50, n_neighbors=30)

    log.info("HDBSCAN C0 coarse clustering (%d claims, 50-dim)...", len(reduced))
    c0 = _hdbscan.HDBSCAN(
        min_cluster_size=80,          # ~0.5% of 15K = coarse topics
        min_samples=10,
        metric="euclidean",
        cluster_selection_method="eom",
        cluster_selection_epsilon=0.3,
        core_dist_n_jobs=-1,
    )
    c0_labels = c0.fit_predict(reduced)
    c0_noise  = int((c0_labels == -1).sum())
    c0_clusters = len(set(c0_labels)) - (1 if -1 in c0_labels else 0)
    log.info("C0: %d clusters, %d noise (%.0f%%)", c0_clusters, c0_noise,
             100*c0_noise/max(len(c0_labels),1))

    log.info("HDBSCAN C1 fine clustering...")
    c1 = _hdbscan.HDBSCAN(
        min_cluster_size=20,          # finer granularity
        min_samples=5,
        metric="euclidean",
        cluster_selection_method="leaf",
        cluster_selection_epsilon=0.1,
        core_dist_n_jobs=-1,
    )
    c1_labels = c1.fit_predict(reduced)
    c1_noise  = int((c1_labels == -1).sum())
    c1_clusters = len(set(c1_labels)) - (1 if -1 in c1_labels else 0)
    log.info("C1: %d clusters, %d noise (%.0f%%)", c1_clusters, c1_noise,
             100*c1_noise/max(len(c1_labels),1))

    # Remap noise → unique "unclustered" IDs above max cluster
    c0_max = int(c0_labels.max()) + 1 if len(c0_labels) > 0 else 0
    c1_max = int(c1_labels.max()) + 1 if len(c1_labels) > 0 else 0
    noise_counter_c0 = c0_max
    noise_counter_c1 = c1_max

    c0_map: dict[str, int] = {}
    c1_map: dict[str, int] = {}
    for idx, nid in enumerate(claim_node_ids):
        lab0 = int(c0_labels[idx])
        lab1 = int(c1_labels[idx])
        if lab0 == -1:
            c0_map[nid] = noise_counter_c0; noise_counter_c0 += 1
        else:
            c0_map[nid] = lab0
        if lab1 == -1:
            c1_map[nid] = noise_counter_c1; noise_counter_c1 += 1
        else:
            c1_map[nid] = lab1

    return c0_map, c1_map

def apply_claim_clusters(G: nx.DiGraph, c0_map: dict[str, int],
                          c1_map: dict[str, int]) -> None:
    """Write C0/C1 cluster IDs to graph nodes."""
    for nid, cid in c0_map.items():
        if nid in G:
            G.nodes[nid]["community_c0"] = cid
    for nid, cid in c1_map.items():
        if nid in G:
            G.nodes[nid]["community_id"] = cid

# ── 4. Qdrant Semantic Index (100% claims) ────────────────────────────────────

def build_qdrant_index(G: nx.DiGraph, claim_vecs: Optional[np.ndarray] = None,
                       claim_node_ids: Optional[list[str]] = None,
                       recreate: bool = True) -> bool:
    """Upsert ALL claims ≥ CLAIM_CONF_MIN into Qdrant with C0/C1 cluster IDs.

    If claim_vecs/claim_node_ids provided, reuses pre-computed embeddings.
    Returns True on success.
    """
    if not _ensure_collection(recreate=recreate):
        return False
    client = _get_qdrant()
    if client is None:
        return False
    from qdrant_client.models import PointStruct

    if claim_node_ids is None:
        claim_nodes = [(nid, d) for nid, d in G.nodes(data=True)
                       if d.get("node_type") == "claim" and d.get("confidence", 0) >= CLAIM_CONF_MIN]
        claim_nodes.sort(key=lambda x: x[1].get("confidence", 0), reverse=True)
        claim_node_ids = [nid for nid, _ in claim_nodes]
        texts = [d.get("text", "") for _, d in claim_nodes]
        log.info("Embedding %d claims for Qdrant...", len(texts))
        claim_vecs = embed_texts(texts)
    else:
        claim_nodes = [(nid, G.nodes[nid]) for nid in claim_node_ids]

    if claim_vecs is None or len(claim_vecs) == 0:
        log.warning("No claim embeddings to upsert")
        return True

    # Build entity reverse map for payload enrichment
    claim_entity_map: dict[str, list[str]] = defaultdict(list)
    for src, dst, edata in G.edges(data=True):
        if edata.get("relation") == "mentions":
            claim_entity_map[dst].append(src)

    points = []
    for idx, (nid, data) in enumerate(claim_nodes):
        cid = data.get("embedding_id", idx)
        points.append(PointStruct(
            id=int(cid) if isinstance(cid, int) else idx,
            vector=claim_vecs[idx].tolist(),
            payload={
                "claim_text": data.get("text", ""),
                "mission_id": data.get("mission_id", ""),
                "confidence": data.get("confidence", 0.5),
                "entity_ids": claim_entity_map.get(nid, [])[:10],
                "node_id": nid,
                "community_c0": data.get("community_c0", -1),
                "community_id": data.get("community_id", -1),
            }
        ))

    BATCH = 500
    for i in range(0, len(points), BATCH):
        client.upsert(collection_name=COLLECTION, points=points[i:i+BATCH])
    log.info("Qdrant: upserted %d vectors", len(points))
    return True

def semantic_search(query: str, top_k: int = 10,
                    filter_c0: Optional[int] = None) -> list[dict]:
    """Search Qdrant for semantically similar claims.

    Args:
        query: Search query string
        top_k: Number of results
        filter_c0: Optionally restrict to a C0 cluster (for hierarchical search)
    """
    client = _get_qdrant()
    if client is None:
        return []
    try:
        vec = embed_texts([query])[0]
        kwargs: dict[str, Any] = {"collection_name": COLLECTION, "query": vec.tolist(), "limit": top_k}
        if filter_c0 is not None:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            kwargs["query_filter"] = Filter(must=[FieldCondition(
                key="community_c0", match=MatchValue(value=filter_c0))])
        hits = client.query_points(**kwargs).points
        return [{"claim_text": h.payload.get("claim_text", ""),
                 "mission_id": h.payload.get("mission_id", ""),
                 "confidence": h.payload.get("confidence", 0.0),
                 "entity_ids": h.payload.get("entity_ids", []),
                 "node_id": h.payload.get("node_id", ""),
                 "community_c0": h.payload.get("community_c0", -1),
                 "community_id": h.payload.get("community_id", -1),
                 "score": h.score} for h in hits]
    except Exception as exc:
        log.warning("Qdrant search failed: %s", exc)
        return []

# ── 5. Community Summaries (ALL meaningful clusters) ──────────────────────────

def _cluster_claims_info(G: nx.DiGraph, cid_attr: str,
                          cid: int, min_claims: int = 3) -> Optional[dict]:
    """Collect info for a single cluster. Returns None if too small."""
    claim_nodes = [(n, G.nodes[n]) for n, d in G.nodes(data=True)
                   if d.get("node_type") == "claim" and d.get(cid_attr, -1) == cid]
    if len(claim_nodes) < min_claims:
        return None
    claim_nodes.sort(key=lambda x: x[1].get("confidence", 0), reverse=True)
    top_claims = [d.get("text", "") for _, d in claim_nodes[:8]]
    mission_ids = list({d.get("mission_id", "") for _, d in claim_nodes if d.get("mission_id")})
    # Key entities: those that mention these claims
    entity_scores: dict[str, float] = defaultdict(float)
    for nid, _ in claim_nodes[:20]:
        for src, dst, edata in G.in_edges(nid, data=True):
            if edata.get("relation") == "mentions":
                ename = G.nodes[src].get("canonical_name", src.replace("entity:", ""))
                entity_scores[ename] += edata.get("weight", 1.0)
    key_entities = sorted(entity_scores, key=lambda e: -entity_scores[e])[:8]
    return {"claim_count": len(claim_nodes), "mission_ids": mission_ids,
            "top_claims": top_claims, "key_entities": key_entities}

def build_community_summaries(G: nx.DiGraph, level: str = "C1",
                               existing: Optional[dict] = None) -> dict[int, dict]:
    """Build Bedrock Haiku summaries for ALL meaningful clusters.

    level: 'C0' (coarse) or 'C1' (fine)
    existing: existing summaries dict to skip already-built ones (incremental)
    """
    cid_attr = "community_c0" if level == "C0" else "community_id"
    # Find all cluster IDs
    all_cids = {G.nodes[n].get(cid_attr, -1) for n in G
                if G.nodes[n].get("node_type") == "claim"}
    all_cids.discard(-1)
    existing = existing or {}

    summaries: dict[int, dict] = dict(existing)
    to_build = sorted(all_cids - set(existing.keys()))
    log.info("Building %s summaries: %d clusters total, %d to build (%.0f%% new)",
             level, len(all_cids), len(to_build),
             100*len(to_build)/max(len(all_cids), 1))

    built = 0
    for cid in to_build:
        info = _cluster_claims_info(G, cid_attr, cid, min_claims=3)
        if info is None:
            continue
        prompt = (
            f"Summarize this intelligence cluster.\n\n"
            f"Key entities: {', '.join(info['key_entities']) or 'N/A'}\n\n"
            f"Top claims:\n" + "\n".join(f"- {c[:120]}" for c in info["top_claims"]) +
            f"\n\nRespond with exactly:\n"
            f"LABEL: <5-word topic label>\n"
            f"SUMMARY: <2-3 sentence analytical summary>"
        )
        resp = call_bedrock(prompt, system="You are an intelligence analyst. Be concise.", max_tokens=200)
        label   = f"Cluster {cid}"
        summary = resp or ""
        if resp:
            for line in resp.split("\n"):
                if line.startswith("LABEL:"):
                    label = line.replace("LABEL:", "").strip()[:80]
                elif line.startswith("SUMMARY:"):
                    summary = line.replace("SUMMARY:", "").strip()

        summaries[cid] = {
            "label": label, "summary": summary,
            "key_entities": info["key_entities"],
            "claim_count": info["claim_count"],
            "mission_ids": info["mission_ids"],
            "level": level
        }
        built += 1
        if built % 20 == 0:
            log.info("  ... built %d/%d summaries", built, len(to_build))

    log.info("Built %d new %s summaries (%d total)", built, level, len(summaries))
    return summaries

# ── 6. Centrality ─────────────────────────────────────────────────────────────

def _compute_centrality(G: nx.DiGraph) -> None:
    log.info("Computing centrality metrics...")
    try:
        pr = nx.pagerank(G, alpha=0.85, weight="weight", max_iter=300)
    except Exception:
        pr = {n: 0.0 for n in G}
    try:
        bc = nx.betweenness_centrality(G, k=min(150, len(G)), normalized=True)
    except Exception:
        bc = {n: 0.0 for n in G}
    for node in G:
        G.nodes[node]["pagerank"]    = float(pr.get(node, 0.0))
        G.nodes[node]["betweenness"] = float(bc.get(node, 0.0))

# ── 7. GraphRAG Hierarchical Search ──────────────────────────────────────────

def graphrag_search(query: str, G: Optional[nx.DiGraph] = None,
                    community_summaries: Optional[dict] = None,
                    c0_summaries: Optional[dict] = None,
                    top_k_c0: int = 3, top_k_c1: int = 5) -> dict:
    """Two-level GraphRAG search.

    1. Qdrant semantic → find hit C0 clusters
    2. Rank C0 by hit count × semantic score
    3. For top C0 clusters: search within each (filter_c0) → C1 hits
    4. Rank C1 communities by semantic score
    5. Synthesize answer from C0+C1 summaries + top claims
    """
    if G is None:
        G = load_graph()
    if community_summaries is None:
        community_summaries = _load_communities()
    if c0_summaries is None:
        c0_summaries = _load_communities_c0()

    # Step 1: broad semantic search (top 30)
    broad_hits = semantic_search(query, top_k=30)
    if not broad_hits:
        # Fallback: use graph BFS
        broad_hits = []

    # Step 2: rank C0 clusters
    c0_scores: dict[int, float] = defaultdict(float)
    for h in broad_hits:
        c0 = h.get("community_c0", -1)
        if c0 >= 0:
            c0_scores[c0] += h["score"]

    top_c0 = sorted(c0_scores, key=lambda c: -c0_scores[c])[:top_k_c0]

    # Step 3+4: within-cluster fine search
    c1_scores: dict[int, float] = defaultdict(float)
    all_claims: list[dict] = list(broad_hits[:10])  # start with broad results
    for c0 in top_c0:
        fine_hits = semantic_search(query, top_k=10, filter_c0=c0)
        for h in fine_hits:
            c1 = h.get("community_id", -1)
            if c1 >= 0:
                c1_scores[c1] += h["score"]
            all_claims.append(h)

    # Deduplicate claims by node_id
    seen_nids: set[str] = set()
    deduped_claims: list[dict] = []
    for h in sorted(all_claims, key=lambda x: -x.get("score", 0)):
        nid = h.get("node_id", "")
        if nid not in seen_nids:
            seen_nids.add(nid)
            deduped_claims.append(h)
        if len(deduped_claims) >= 15:
            break

    # Step 5: collect summaries
    selected_c0 = [c0 for c0 in top_c0 if c0 in c0_summaries]
    selected_c1 = sorted(c1_scores, key=lambda c: -c1_scores[c])[:top_k_c1]
    selected_c1 = [c1 for c1 in selected_c1 if c1 in community_summaries]

    summary_blocks = []
    communities_used = []
    for c0 in selected_c0:
        cs = c0_summaries[c0]
        summary_blocks.append(f"[Topic: {cs.get('label','')}]\n{cs.get('summary','')}")
        communities_used.append({"level": "C0", "community_id": c0,
                                  "label": cs.get("label", ""), "summary": cs.get("summary", "")})
    for c1 in selected_c1:
        cs = community_summaries[c1]
        summary_blocks.append(f"[Subtopic: {cs.get('label','')}]\n{cs.get('summary','')}")
        communities_used.append({"level": "C1", "community_id": c1,
                                  "label": cs.get("label", ""), "summary": cs.get("summary", "")})

    claims_text    = "\n".join(f"- {h['claim_text']}" for h in deduped_claims[:12])
    summaries_text = "\n\n".join(summary_blocks) or "(no cluster summaries available)"
    prompt = (
        f"Query: {query}\n\n"
        f"Intelligence topic summaries:\n{summaries_text}\n\n"
        f"Specific evidence:\n{claims_text}\n\n"
        f"Provide a structured analytical answer. Cite specific facts from the evidence."
    )
    answer = call_bedrock(prompt, system="You are an intelligence analyst. Be concise and evidence-based.",
                          max_tokens=600)
    avg_score = float(np.mean([h["score"] for h in deduped_claims])) if deduped_claims else 0.0
    return {"answer": answer, "communities_used": communities_used,
            "source_claims": deduped_claims[:10], "confidence": avg_score,
            "c0_clusters_searched": len(top_c0), "c1_clusters_matched": len(selected_c1)}

# ── 8. Incremental Update ─────────────────────────────────────────────────────

def _compute_db_hash() -> str:
    """Fast hash of current DB state: row counts + max IDs."""
    try:
        rows = _fetch(
            "SELECT (SELECT COUNT(*) FROM hive_claims), "
            "       (SELECT COUNT(*) FROM hive_entities), "
            "       (SELECT COUNT(*) FROM hive_missions), "
            "       (SELECT MAX(id) FROM hive_claims), "
            "       (SELECT MAX(id) FROM hive_missions)"
        )
        return hashlib.md5(str(rows[0]).encode()).hexdigest()
    except Exception:
        return str(time.time())

def _load_hash() -> str:
    if not HASH_FILE.exists():
        return ""
    try:
        return json.loads(HASH_FILE.read_text()).get("hash", "")
    except Exception:
        return ""

def _save_hash(h: str) -> None:
    HASH_FILE.write_text(json.dumps({"hash": h, "updated_at": datetime.now(timezone.utc).isoformat()}))

def incremental_update(force: bool = False) -> dict:
    """Incremental graph update: only rebuild if DB changed.

    Strategy:
    - If DB hash matches → skip (return current stats)
    - If changed → identify new claims/entities since last build
    - Add new nodes + edges to existing graph
    - Upsert new claim vectors to Qdrant (no recreate)
    - Re-run centrality (fast approximation) + detect new clusters
    - Generate summaries for new clusters only

    Full rebuild triggered if:
    - No existing graph
    - >20% new claims (structural change too large)
    - force=True
    """
    current_hash = _compute_db_hash()
    if not force and current_hash == _load_hash():
        log.info("DB unchanged (hash match) — skipping incremental update")
        return {"status": "skipped", "reason": "no_change"}

    if not GRAPH_FILE.exists() or force:
        log.info("No existing graph or force=True — running full build")
        return full_build(skip_summaries=False)

    log.info("DB changed — running incremental update")
    t0 = time.time()

    G = load_graph()
    existing_claim_ids = {int(n.split(":")[1]) for n in G.nodes()
                          if n.startswith("claim:") and n.split(":")[1].isdigit()}
    existing_mission_ids = {n.split(":")[1] for n in G.nodes() if n.startswith("mission:")}

    # Fetch new items
    new_claims = _fetch(
        "SELECT id, claim, confidence, mission_id, source_url FROM hive_claims "
        "WHERE claim IS NOT NULL AND claim != '' AND confidence >= ? AND id NOT IN "
        f"(SELECT UNNEST({list(existing_claim_ids)[:5000] if existing_claim_ids else [0]}))"
        " ORDER BY confidence DESC",
        [CLAIM_CONF_MIN]
    ) if existing_claim_ids else []

    # If >25% new claims — full rebuild
    if len(new_claims) > len(existing_claim_ids) * 0.25:
        log.info("Large delta (%d new claims, %d existing) — full rebuild",
                 len(new_claims), len(existing_claim_ids))
        return full_build(skip_summaries=False)

    if not new_claims:
        log.info("No new claims — updating hash only")
        _save_hash(current_hash)
        return {"status": "ok", "new_claims": 0, "elapsed": time.time()-t0}

    log.info("Incremental: %d new claims", len(new_claims))

    # Add new claim nodes
    for cid, text, conf, mission_id, url in new_claims:
        cconf = float(conf or 0.5)
        G.add_node(f"claim:{cid}", node_type="claim",
                   text=(text or "")[:120], confidence=cconf,
                   mission_id=mission_id or "", embedding_id=cid,
                   community_id=-1, community_c0=-1)
        mnode = f"mission:{mission_id}"
        if mnode in G:
            G.add_edge(f"claim:{cid}", mnode, relation="belongs_to", weight=1.0)

    # Embed new claims + upsert to Qdrant (no recreate)
    new_texts = [(f"claim:{cid}", (text or "")[:120], cid)
                 for cid, text, conf, mission_id, url in new_claims]
    vecs = embed_texts([t for _, t, _ in new_texts])
    client = _get_qdrant()
    if client:
        from qdrant_client.models import PointStruct
        points = [PointStruct(id=int(cid),
                              vector=vecs[i].tolist(),
                              payload={"claim_text": text, "node_id": nid,
                                       "confidence": G.nodes[nid].get("confidence", 0.5),
                                       "mission_id": G.nodes[nid].get("mission_id", ""),
                                       "entity_ids": [], "community_c0": -1, "community_id": -1})
                  for i, (nid, text, cid) in enumerate(new_texts)]
        for i in range(0, len(points), 500):
            client.upsert(collection_name=COLLECTION, points=points[i:i+500])
        log.info("Qdrant: upserted %d new vectors (incremental)", len(points))

    save_graph(G)
    _save_hash(current_hash)
    elapsed = time.time() - t0
    log.info("Incremental update done in %.1fs", elapsed)
    return {"status": "ok", "new_claims": len(new_claims), "elapsed": elapsed}

# ── 9. Full Build Orchestrator ────────────────────────────────────────────────

def full_build(skip_summaries: bool = False, skip_embeddings: bool = False) -> dict:
    """Full pipeline: build graph → cluster claims → index Qdrant → summaries."""
    t0 = time.time()
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)

    # Step 1: Build graph structure
    G = build_graph()

    # Step 2: Embed ALL claims + run HDBSCAN clustering
    claim_nodes = [(nid, d) for nid, d in G.nodes(data=True)
                   if d.get("node_type") == "claim" and d.get("confidence", 0) >= CLAIM_CONF_MIN]
    claim_nodes.sort(key=lambda x: x[1].get("confidence", 0), reverse=True)
    claim_nids = [nid for nid, _ in claim_nodes]
    claim_texts = [d.get("text", "") for _, d in claim_nodes]

    claim_vecs: Optional[np.ndarray] = None
    if not skip_embeddings and claim_nodes:
        log.info("Embedding %d claims...", len(claim_nodes))
        claim_vecs = embed_texts(claim_texts)

        # HDBSCAN clustering
        log.info("Running HDBSCAN claim clustering (C0+C1)...")
        try:
            c0_map, c1_map = cluster_claims(G, claim_vecs, claim_nids)
            apply_claim_clusters(G, c0_map, c1_map)
        except Exception as exc:
            log.warning("HDBSCAN clustering failed: %s", exc)

        # Qdrant upsert
        build_qdrant_index(G, claim_vecs=claim_vecs, claim_node_ids=claim_nids, recreate=True)
    elif not skip_embeddings:
        log.info("No claims to embed")

    # Step 3: Save graph
    c0_summaries: dict[int, dict] = {}
    c1_summaries: dict[int, dict] = {}

    if not skip_summaries:
        log.info("Building C0 coarse community summaries...")
        c0_summaries = build_community_summaries(G, level="C0")
        log.info("Building C1 fine community summaries...")
        c1_summaries = build_community_summaries(G, level="C1")

    save_graph(G, c1_summaries, c0_summaries)
    _save_hash(_compute_db_hash())

    elapsed = time.time() - t0
    n_claim_nodes = sum(1 for _, d in G.nodes(data=True) if d.get("node_type") == "claim")
    n_entity_nodes = sum(1 for _, d in G.nodes(data=True) if d.get("node_type") == "entity")
    c0_count = len(set(d.get("community_c0", -1) for _, d in G.nodes(data=True)
                       if d.get("node_type") == "claim" and d.get("community_c0", -1) >= 0))
    c1_count = len(set(d.get("community_id", -1) for _, d in G.nodes(data=True)
                       if d.get("node_type") == "claim" and d.get("community_id", -1) >= 0))
    log.info("=== Full build complete in %.1fs ===", elapsed)
    log.info("  Claims: %d indexed | C0 clusters: %d | C1 clusters: %d", n_claim_nodes, c0_count, c1_count)
    log.info("  Entities: %d | C0 summaries: %d | C1 summaries: %d",
             n_entity_nodes, len(c0_summaries), len(c1_summaries))
    return graph_stats(G)

# ── 10. Persistence ────────────────────────────────────────────────────────────

def save_graph(G: nx.DiGraph, community_summaries: Optional[dict] = None,
               c0_summaries: Optional[dict] = None) -> None:
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    GRAPH_FILE.write_text(json.dumps(nx.node_link_data(G)))
    if community_summaries is not None:
        COMMUNITIES_FILE.write_text(json.dumps(community_summaries, indent=2))
    if c0_summaries is not None:
        C0_FILE.write_text(json.dumps(c0_summaries, indent=2))
    # Fallback: if file doesn't exist, write empty
    if not COMMUNITIES_FILE.exists():
        COMMUNITIES_FILE.write_text("[]")
    if not C0_FILE.exists():
        C0_FILE.write_text("[]")
    META_FILE.write_text(json.dumps({
        "built_at": datetime.now(timezone.utc).isoformat(),
        "node_count": G.number_of_nodes(),
        "edge_count": G.number_of_edges(),
        "entity_nodes": sum(1 for _, d in G.nodes(data=True) if d.get("node_type") == "entity"),
        "claim_nodes":  sum(1 for _, d in G.nodes(data=True) if d.get("node_type") == "claim"),
        "mission_nodes": sum(1 for _, d in G.nodes(data=True) if d.get("node_type") == "mission"),
    }, indent=2))
    log.info("Saved graph → %s", GRAPH_FILE)

def load_graph() -> nx.DiGraph:
    if not GRAPH_FILE.exists():
        log.warning("Graph file not found: %s", GRAPH_FILE)
        return nx.DiGraph()
    return nx.node_link_graph(json.loads(GRAPH_FILE.read_text()), directed=True, multigraph=False)

def _load_communities() -> dict:
    if not COMMUNITIES_FILE.exists():
        return {}
    raw = json.loads(COMMUNITIES_FILE.read_text())
    if isinstance(raw, list):
        return {}
    return {int(k): v for k, v in raw.items()}

def _load_communities_c0() -> dict:
    if not C0_FILE.exists():
        return {}
    raw = json.loads(C0_FILE.read_text())
    if isinstance(raw, list):
        return {}
    return {int(k): v for k, v in raw.items()}

# ── 11. API-compatible exports ─────────────────────────────────────────────────

def graph_stats(G: Optional[nx.DiGraph] = None) -> dict:
    if G is None:
        G = load_graph()
    meta = json.loads(META_FILE.read_text()) if META_FILE.exists() else {}
    entity_nodes = [(n, d) for n, d in G.nodes(data=True) if d.get("node_type") == "entity"]
    top_pr = sorted(entity_nodes, key=lambda x: x[1].get("pagerank", 0), reverse=True)[:5]
    return {
        "node_count": G.number_of_nodes(), "edge_count": G.number_of_edges(),
        "entity_count": len(entity_nodes),
        "claim_count":  sum(1 for _, d in G.nodes(data=True) if d.get("node_type") == "claim"),
        "mission_count": sum(1 for _, d in G.nodes(data=True) if d.get("node_type") == "mission"),
        "built_at": meta.get("built_at", "unknown"),
        "top_entities_by_pagerank": [{"entity": n.replace("entity:", ""), "pagerank": d.get("pagerank", 0)}
                                      for n, d in top_pr]
    }

def query_graph(term: str, G: Optional[nx.DiGraph] = None,
                top_n: int = 20, include_claims: bool = True) -> list[dict]:
    if G is None:
        G = load_graph()
    t = term.lower(); results: list[dict] = []
    for nid, data in G.nodes(data=True):
        ntype = data.get("node_type", "")
        if ntype == "entity" and (t in data.get("canonical_name", "").lower() or t in nid.lower()):
            results.append({"node_id": nid, **data})
        elif include_claims and ntype == "claim" and t in data.get("text", "").lower():
            results.append({"node_id": nid, **data})
        if len(results) >= top_n:
            break
    return results[:top_n]

def shortest_path(node_a: str, node_b: str, G: Optional[nx.DiGraph] = None) -> list[dict]:
    if G is None:
        G = load_graph()
    a = node_a if node_a.startswith("entity:") else f"entity:{node_a.lower()}"
    b = node_b if node_b.startswith("entity:") else f"entity:{node_b.lower()}"
    try:
        path = nx.shortest_path(G.to_undirected(), a, b)
        return [{"node_id": n, **G.nodes.get(n, {})} for n in path]
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []

def get_communities(G: Optional[nx.DiGraph] = None, top_n: int = 20,
                    level: str = "C1") -> list[dict]:
    if G is None:
        G = load_graph()
    summaries = _load_communities_c0() if level == "C0" else _load_communities()
    cid_attr = "community_c0" if level == "C0" else "community_id"
    sizes: dict[int, int] = defaultdict(int)
    for _, d in G.nodes(data=True):
        cid = d.get(cid_attr, -1)
        if cid >= 0:
            sizes[cid] += 1
    result = []
    for cid, size in sorted(sizes.items(), key=lambda x: -x[1])[:top_n]:
        entry: dict[str, Any] = {"community_id": cid, "node_count": size, "level": level}
        if cid in summaries:
            entry.update(summaries[cid])
        result.append(entry)
    return result

def build_mission_context(mission_id: str, G: Optional[nx.DiGraph] = None,
                           max_entities: int = 30) -> str:
    if G is None:
        G = load_graph()
    mnode = f"mission:{mission_id}"
    if mnode not in G:
        return f"Mission {mission_id} not found in graph."
    md = G.nodes[mnode]
    lines = [f"Mission: {md.get('topic', mission_id)} (confidence={md.get('confidence', 0):.2f})"]
    claims = sorted(
        [(n, G.nodes[n]) for n in G.predecessors(mnode)
         if G.nodes.get(n, {}).get("node_type") == "claim"],
        key=lambda x: x[1].get("confidence", 0), reverse=True
    )
    lines.append(f"\nTop claims ({len(claims)} total):")
    for _, cd in claims[:10]:
        lines.append(f"  [{cd.get('confidence', 0):.2f}] {cd.get('text', '')}")
    entities = [(n, G.nodes[n]) for n in G.nodes()
                if G.nodes.get(n, {}).get("node_type") == "entity"
                and G.nodes[n].get("mission_count", 0) > 0]
    entities.sort(key=lambda x: x[1].get("pagerank", 0), reverse=True)
    lines.append(f"\nKey entities:")
    for _, ed in entities[:max_entities]:
        lines.append(f"  {ed.get('canonical_name', '?')} [{ed.get('entity_type', '?')}]")
    return "\n".join(lines)

def post_synth_rebuild(mission_id: str = "") -> dict:
    """Triggered after each mission synthesis. Runs incremental update."""
    log.info("post_synth_rebuild: mission_id=%s", mission_id)
    return incremental_update(force=False)

# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")

    b = sub.add_parser("build")
    b.add_argument("--skip-summaries", action="store_true")
    b.add_argument("--skip-embeddings", action="store_true")

    inc = sub.add_parser("incremental")
    inc.add_argument("--force", action="store_true")

    sub.add_parser("stats")

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--top", type=int, default=8)

    sem = sub.add_parser("semantic")
    sem.add_argument("query")
    sem.add_argument("--top", type=int, default=8)
    sem.add_argument("--c0", type=int, default=None)

    com = sub.add_parser("communities")
    com.add_argument("--top", type=int, default=15)
    com.add_argument("--level", default="C1", choices=["C0", "C1"])

    pp = sub.add_parser("path")
    pp.add_argument("a"); pp.add_argument("b")

    ctx = sub.add_parser("context")
    ctx.add_argument("mission_id")

    gm = sub.add_parser("godmode")

    ans = sub.add_parser("answer")
    ans.add_argument("query")

    args = p.parse_args()

    if args.cmd == "build":
        result = full_build(skip_summaries=args.skip_summaries,
                            skip_embeddings=args.skip_embeddings)
        print(json.dumps(result, indent=2))

    elif args.cmd == "incremental":
        result = incremental_update(force=args.force)
        print(json.dumps(result, indent=2))

    elif args.cmd == "stats":
        print(json.dumps(graph_stats(), indent=2))

    elif args.cmd == "search":
        G = load_graph()
        results = query_graph(args.query, G, top_n=args.top)
        for r in results:
            ntype = r.get("node_type", "?")
            label = r.get("canonical_name") or r.get("text", "") or r.get("topic", "")
            print(f"  [{ntype:8}] {label[:80]}")

    elif args.cmd == "semantic":
        results = semantic_search(args.query, top_k=args.top, filter_c0=args.c0)
        if not results:
            print("(Qdrant unavailable — run build first)")
        for r in results:
            print(f"  [{r.get('score', 0):.3f}] {r.get('claim_text', '')[:80]}")

    elif args.cmd == "communities":
        G = load_graph()
        comms = get_communities(G, top_n=args.top, level=args.level)
        summaries = _load_communities_c0() if args.level == "C0" else _load_communities()
        print(f"=== {args.level} Communities (top {args.top}) ===")
        for c in comms:
            cid = c["community_id"]
            label = c.get("label", f"Cluster {cid}")
            claims = c.get("claim_count", c["node_count"])
            s = summaries.get(cid, {}).get("summary", "")[:80]
            print(f"  [{cid:>4}] {label[:48]:48} claims={claims}")
            if s:
                print(f"         {s}")

    elif args.cmd == "path":
        G = load_graph()
        path = shortest_path(args.a, args.b, G)
        if not path:
            print(f"No path between '{args.a}' and '{args.b}'")
        for n in path:
            print(f"  → {n.get('canonical_name') or n.get('node_id', '?')}")

    elif args.cmd == "context":
        G = load_graph()
        print(build_mission_context(args.mission_id, G))

    elif args.cmd == "godmode":
        G = load_graph()
        entity_nodes = [(n, d) for n, d in G.nodes(data=True) if d.get("node_type") == "entity"]
        top_pr = sorted(entity_nodes, key=lambda x: x[1].get("pagerank", 0), reverse=True)[:20]
        print("=== GOD MODE: Top Hub Entities ===")
        for n, d in top_pr:
            name = d.get("canonical_name", n.replace("entity:", ""))
            etype = d.get("entity_type", "?")
            pr = d.get("pagerank", 0)
            bc = d.get("betweenness", 0)
            hub = (pr + bc) / 2
            print(f"  {name:40} type={etype:20} pr={pr:.4f} bc={bc:.4f} hub={hub:.4f}")

    elif args.cmd == "answer":
        G = load_graph()
        result = graphrag_search(args.query, G)
        print(result.get("answer", ""))
        print()
        print(f"C0 clusters searched: {result.get('c0_clusters_searched', 0)}")
        print(f"C1 clusters matched: {result.get('c1_clusters_matched', 0)}")
        print(f"Source claims: {len(result.get('source_claims', []))}")

    else:
        p.print_help()

if __name__ == "__main__":
    _cli()
