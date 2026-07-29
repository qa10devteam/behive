"""BeHive Neo4j Knowledge Graph integration.

Connects to Neo4j for entity/relationship queries.
Falls back to PostgreSQL ILIKE if Neo4j is unavailable.
"""

import os
import logging
from typing import Optional

log = logging.getLogger(__name__)

_driver = None
_available = None


def _get_neo4j_password() -> str:
    """Get Neo4j password from env or password file."""
    pw = os.environ.get("BEHIVE_NEO4J_PASSWORD", "")
    if pw:
        return pw
    # Try password file (useful in containers)
    pw_file = os.environ.get("BEHIVE_NEO4J_PASSWORD_FILE", "/tmp/.neo4j_pass")
    if os.path.exists(pw_file):
        return open(pw_file).read().strip()
    return "neo4j"  # default


def get_driver():
    """Get or create Neo4j driver. Returns None if unavailable."""
    global _driver, _available
    if _available is False:
        return None
    if _driver is not None:
        return _driver
    
    try:
        from neo4j import GraphDatabase
        uri = os.environ.get("BEHIVE_NEO4J_URI", "bolt://localhost:7687")
        user = os.environ.get("BEHIVE_NEO4J_USER", "neo4j")
        password = _get_neo4j_password()
        
        _driver = GraphDatabase.driver(uri, auth=(user, password))
        # Verify connectivity
        with _driver.session() as s:
            s.run("RETURN 1").single()
        _available = True
        log.info(f"Neo4j connected: {uri}")
        return _driver
    except Exception as e:
        log.warning(f"Neo4j unavailable ({e}), falling back to PostgreSQL")
        _available = False
        return None


def entity_lookup(name: str, limit: int = 50) -> Optional[dict]:
    """Get entity from knowledge graph with relationships."""
    driver = get_driver()
    if not driver:
        return None
    
    try:
        with driver.session() as s:
            # Find entity
            result = s.run("""
                MATCH (e:HiveEntity)
                WHERE e.name =~ $pattern
                RETURN e.name as name, e.entity_type as type, 
                       e.mention_count as mentions, e.first_seen as first_seen
                ORDER BY e.mention_count DESC
                LIMIT 1
            """, pattern=f"(?i).*{name}.*")
            entity_rec = result.single()
            if not entity_rec:
                return None
            
            entity_name = entity_rec["name"]
            
            # Get relationships (outgoing)
            result = s.run("""
                MATCH (e:HiveEntity)-[r]->(target)
                WHERE e.name = $name
                RETURN type(r) as rel_type, 
                       CASE WHEN target:HiveEntity THEN target.name 
                            WHEN target:HiveMission THEN coalesce(target.topic, target.mission_id)
                            ELSE labels(target)[0] + ':' + coalesce(target.name, toString(id(target))) 
                       END as target_name,
                       CASE WHEN target:HiveEntity THEN target.entity_type ELSE labels(target)[0] END as target_type
                LIMIT $limit
            """, name=entity_name, limit=limit)
            outgoing = [{"relationship": r["rel_type"], "target": r["target_name"], "target_type": r["target_type"]} 
                       for r in result]
            
            # Get relationships (incoming)
            result = s.run("""
                MATCH (source)-[r]->(e:HiveEntity)
                WHERE e.name = $name
                RETURN type(r) as rel_type,
                       CASE WHEN source:HiveEntity THEN source.name
                            WHEN source:HiveMission THEN source.topic
                            ELSE labels(source)[0] + ':' + coalesce(source.name, '') 
                       END as source_name,
                       CASE WHEN source:HiveEntity THEN source.entity_type ELSE labels(source)[0] END as source_type
                LIMIT $limit
            """, name=entity_name, limit=limit)
            incoming = [{"relationship": r["rel_type"], "source": r["source_name"], "source_type": r["source_type"]}
                       for r in result]
            
            # Get co-occurring entities (via shared missions)
            result = s.run("""
                MATCH (e:HiveEntity {name: $name})-[:MENTIONED_IN]->(m)<-[:MENTIONED_IN]-(other:HiveEntity)
                WHERE other.name <> $name
                RETURN other.name as entity, other.entity_type as type, count(m) as shared_missions
                ORDER BY shared_missions DESC
                LIMIT 20
            """, name=entity_name)
            co_occurring = [{"entity": r["entity"], "type": r["type"], "shared_missions": r["shared_missions"]}
                           for r in result]
            
            return {
                "entity": entity_name,
                "type": entity_rec["type"],
                "mentions": entity_rec["mentions"],
                "relationships_out": outgoing,
                "relationships_in": incoming,
                "co_occurring": co_occurring,
                "source": "neo4j",
            }
    except Exception as e:
        log.error(f"Neo4j entity_lookup error: {e}")
        return None


def entity_network(name: str, depth: int = 2) -> Optional[dict]:
    """Get entity network (N-hop neighborhood) from knowledge graph."""
    driver = get_driver()
    if not driver:
        return None
    
    try:
        with driver.session() as s:
            # Find exact or fuzzy match
            result = s.run("""
                MATCH (e:HiveEntity)
                WHERE e.name =~ $pattern
                RETURN e.name as name
                ORDER BY e.mention_count DESC
                LIMIT 1
            """, pattern=f"(?i).*{name}.*")
            rec = result.single()
            if not rec:
                return None
            center_name = rec["name"]
            
            # Get N-hop network
            result = s.run("""
                MATCH path = (center:HiveEntity {name: $name})-[r*1..""" + str(depth) + """]->(other:HiveEntity)
                WITH center, other, relationships(path) as rels, nodes(path) as nodes
                UNWIND rels as rel
                WITH startNode(rel) as src, endNode(rel) as tgt, type(rel) as rel_type
                WHERE src:HiveEntity AND tgt:HiveEntity
                RETURN DISTINCT src.name as source, tgt.name as target, rel_type as relationship,
                       src.entity_type as source_type, tgt.entity_type as target_type
                LIMIT 200
            """, name=center_name)
            
            nodes = set()
            edges = []
            node_types = {}
            for r in result:
                nodes.add(r["source"])
                nodes.add(r["target"])
                node_types[r["source"]] = r["source_type"]
                node_types[r["target"]] = r["target_type"]
                edges.append({
                    "source": r["source"], 
                    "target": r["target"], 
                    "relationship": r["relationship"],
                })
            
            return {
                "center": center_name,
                "depth": depth,
                "nodes": [{"name": n, "type": node_types.get(n)} for n in sorted(nodes)],
                "edges": edges,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "source": "neo4j",
            }
    except Exception as e:
        log.error(f"Neo4j entity_network error: {e}")
        return None


def graph_stats() -> Optional[dict]:
    """Get knowledge graph statistics."""
    driver = get_driver()
    if not driver:
        return None
    
    try:
        with driver.session() as s:
            stats = {}
            
            # Node counts by label
            result = s.run("MATCH (n) RETURN labels(n)[0] as label, count(n) as cnt ORDER BY cnt DESC")
            stats["node_counts"] = {r["label"]: r["cnt"] for r in result}
            
            # Relationship counts
            result = s.run("MATCH ()-[r]->() RETURN type(r) as rel, count(r) as cnt ORDER BY cnt DESC LIMIT 10")
            stats["relationship_counts"] = {r["rel"]: r["cnt"] for r in result}
            
            # Top entities
            result = s.run("""
                MATCH (e:HiveEntity) 
                RETURN e.name as name, e.entity_type as type, e.mention_count as mentions
                ORDER BY e.mention_count DESC LIMIT 20
            """)
            stats["top_entities"] = [{"name": r["name"], "type": r["type"], "mentions": r["mentions"]} for r in result]
            
            # Total
            stats["total_nodes"] = sum(stats["node_counts"].values())
            stats["total_relationships"] = sum(stats["relationship_counts"].values())
            
            return stats
    except Exception as e:
        log.error(f"Neo4j graph_stats error: {e}")
        return None


def ingest_entities_from_claims(mission_id: str, claims: list[dict] | None = None) -> int:
    """Extract entities from claims and ingest into Neo4j knowledge graph.
    
    Called at the end of a research mission to persist entities/relationships.
    If claims is None, fetches them from PostgreSQL.
    Returns number of entities ingested.
    """
    driver = get_driver()
    if not driver:
        return 0
    
    # Fetch claims from DB if not provided
    if claims is None:
        try:
            import psycopg2
            conn = psycopg2.connect(
                dbname="hive", user="hive_app",
                password=os.environ.get("HIVE_PG_PASSWORD", open("/tmp/.hive_db_pass").read().strip() if os.path.exists("/tmp/.hive_db_pass") else ""),
                host="127.0.0.1", port=5432
            )
            cur = conn.cursor()
            cur.execute(
                "SELECT claim, evidence, source_url, claim_type, confidence, quality_score "
                "FROM hive_claims WHERE mission_id = %s AND quality_score >= 0.7",
                (mission_id,)
            )
            claims = [
                {"claim": r[0], "evidence": r[1], "source_url": r[2], 
                 "claim_type": r[3], "confidence": r[4], "quality_score": r[5]}
                for r in cur.fetchall()
            ]
            conn.close()
        except Exception as e:
            log.debug(f"Failed to fetch claims from DB: {e}")
            return 0
    
    if not claims:
        return 0
    
    ingested = 0
    try:
        with driver.session() as s:
            # Ensure mission node exists
            s.run("""
                MERGE (m:HiveMission {mission_id: $mid})
                SET m.last_updated = datetime()
            """, mid=mission_id)
            
            # For each claim with high quality, extract and store entities
            for claim in claims:
                if (claim.get("quality_score") or 0) < 0.7:
                    continue
                    
                text = claim.get("text") or claim.get("claim", "")
                if not text:
                    continue
                
                # Use simple NER patterns (entities already extracted by pipeline)
                # In production this would use spaCy or LLM extraction
                entities = _extract_entity_names(text)
                
                for entity_name, entity_type in entities:
                    # Merge entity node
                    s.run("""
                        MERGE (e:HiveEntity {name: $name})
                        ON CREATE SET e.entity_type = $type, e.first_seen = datetime(), e.mention_count = 1
                        ON MATCH SET e.mention_count = e.mention_count + 1, e.last_seen = datetime()
                        WITH e
                        MATCH (m:HiveMission {mission_id: $mid})
                        MERGE (e)-[:MENTIONED_IN]->(m)
                    """, name=entity_name, type=entity_type, mid=mission_id)
                    ingested += 1
            
            log.info(f"Ingested {ingested} entities for mission {mission_id}")
    except Exception as e:
        log.error(f"Neo4j ingest error: {e}")
    
    return ingested


def _extract_entity_names(text: str) -> list[tuple[str, str]]:
    """Extract (name, type) pairs from claim text.
    
    Uses patterns to identify: Organizations, People, Products, Amounts.
    """
    import re
    entities = []
    
    # Organizations (capitalized multi-word or known suffixes)
    org_pattern = re.compile(
        r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+(?:\s+(?:Inc|Corp|Ltd|LLC|AG|SA|GmbH|Co|plc)\.?)?)\b'
    )
    for m in org_pattern.finditer(text):
        name = m.group(0).strip()
        if len(name) > 3 and name not in _STOP_WORDS:
            entities.append((name, "Organization"))
    
    # Acronyms (ALL CAPS, 2-6 chars) - likely companies/orgs
    acr_pattern = re.compile(r'\b([A-Z]{2,6})\b')
    for m in acr_pattern.finditer(text):
        name = m.group(0)
        if name not in _STOP_ACRONYMS and len(name) >= 2:
            entities.append((name, "Organization"))
    
    # Money amounts
    money_pattern = re.compile(r'(\$[\d,.]+\s*(?:billion|million|trillion|B|M|T)\b)')
    for m in money_pattern.finditer(text):
        entities.append((m.group(0), "Amount"))
    
    return entities


_STOP_WORDS = {
    "The", "This", "That", "These", "However", "Although", "According",
    "Furthermore", "Additionally", "Meanwhile", "Moreover", "Research",
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
    "New York", "San Francisco", "United States",
}

_STOP_ACRONYMS = {
    "THE", "AND", "FOR", "BUT", "NOT", "ARE", "WAS", "HAS", "HAD",
    "ITS", "HIS", "HER", "OUR", "WHO", "ALL", "CAN", "MAY", "USE",
}
