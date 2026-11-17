#!/usr/bin/env python3
"""
BeHive → Singularity Room Bridge
=================================
Hook that runs after a BeHive mission completes.
Extracts claims from hive_claims and POSTs them to the SR /ingest endpoint.

Features:
- Sends only hygiene-useful claims (not regex_fallback), longer than 20
  characters, from missions that have a product (non-blank report and a
  useful claim). A batch with no explicit product flag is not a product.
  JSON blobs and rows below the quality / confidence thresholds are still skipped
- Maps confidence + quality_score → SR confidence
- Tags source as "behive:{mission_id}" for traceability
- Idempotent: sr_bridge_log records bridged missions
- Batches claims in groups of 50 for API efficiency

Encoder: there is deliberately NO re-encode step here. SR's /ingest handler
already extends its live encoder in-process for every new claim and persists
it, so a full re-encode of the snapshot is redundant, takes ~20 min on CPU for the whole index, and a hot-reload of an
index built from an older snapshot could drop vectors of concurrently ingested claims.
After sending, the bridge only VERIFIES the encoder by reading SR's /health:
ok == encoder.attached and encoder.n == occupied. The result is stored in
sr_bridge_log.sr_response["encoder"] and returned; a failed check logs a
warning but never fails the bridge.

Usage:
  from behive.sr_bridge import bridge_mission
  bridge_mission("hive_1234567_topic_name")

  python -m behive.sr_bridge <mission_id> | --all
"""

import os
import sys
import json
import time
import logging
from typing import List, Dict, Optional

import psycopg2
import requests

from behive.hygiene import SQL_USEFUL_CLAIM, sql_done_product

log = logging.getLogger("behive-sr-bridge")
log.setLevel(logging.INFO)
if not log.handlers:
    log.addHandler(logging.StreamHandler())

# ─── Configuration ──────────────────────────────────────────────────────────

SR_INGEST_URL = os.environ.get("SR_INGEST_URL", "http://localhost:8092/ingest")
SR_HEALTH_URL = os.environ.get("SR_HEALTH_URL", "http://127.0.0.1:8092/health")
SR_TIMEOUT = int(os.environ.get("SR_BRIDGE_TIMEOUT", "30"))
BATCH_SIZE = 50
MIN_QUALITY = 0.4  # Skip claims below this quality threshold
MIN_CONFIDENCE = 0.3  # Skip claims below this confidence

# DB config — reads from env (same as SR server.py)
DB_CONFIG = {
    'host': 'localhost',
    'port': 5432,
    'dbname': 'hive',
    'user': 'hive_app',
    'password': os.environ.get('HIVE_PG_PASSWORD', ''),
}


# ─── Bridge State (dedup) ──────────────────────────────────────────────────

def _get_db():
    return psycopg2.connect(**DB_CONFIG)


def _ensure_bridge_table():
    """Create tracking table if not exists (idempotent)."""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sr_bridge_log (
            mission_id TEXT PRIMARY KEY,
            claims_sent INTEGER NOT NULL DEFAULT 0,
            bridged_at TIMESTAMP DEFAULT NOW(),
            sr_response JSONB
        )
    """)
    conn.commit()
    conn.close()


def _was_already_bridged(mission_id: str) -> bool:
    """Check if mission was already sent to SR."""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sr_bridge_log WHERE mission_id = %s", (mission_id,))
    exists = cur.fetchone() is not None
    conn.close()
    return exists


def _mark_bridged(mission_id: str, claims_sent: int, sr_response: dict):
    """Record that mission was bridged."""
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sr_bridge_log (mission_id, claims_sent, sr_response)
        VALUES (%s, %s, %s)
        ON CONFLICT (mission_id) DO UPDATE SET
            claims_sent = EXCLUDED.claims_sent,
            sr_response = EXCLUDED.sr_response,
            bridged_at = NOW()
    """, (mission_id, claims_sent, json.dumps(sr_response)))
    conn.commit()
    conn.close()


def _verify_encoder() -> Dict:
    """Check SR's live encoder via /health. Never raises.

    ok is True only when encoder.attached is True and encoder.n == occupied.
    """
    try:
        resp = requests.get(SR_HEALTH_URL, timeout=10)
        if resp.status_code != 200:
            result = {"ok": False, "error": f"http_{resp.status_code}"}
        else:
            data = resp.json()
            enc = data.get("encoder") or {}
            attached = enc.get("attached")
            n = enc.get("n")
            occupied = data.get("occupied")
            result = {
                "ok": attached is True and n is not None and n == occupied,
                "attached": attached,
                "n": n,
                "occupied": occupied,
            }
    except Exception as e:
        result = {"ok": False, "error": e.__class__.__name__}
    if not result["ok"]:
        log.warning("[BRIDGE] SR encoder not in sync: %s", result)
    return result


# ─── Core Logic ────────────────────────────────────────────────────────────

class _FetchedClaims(list):
    """Claim batch plus an explicit product flag.

    from_product is True only when hygiene.sql_done_product holds. A plain
    list has no flag. bridge_mission treats a missing flag as no product:
    send nothing and record nothing.
    """

    def __init__(self, from_product: bool):
        super().__init__()
        self.from_product = from_product


def _fetch_mission_claims(mission_id: str) -> List[Dict]:
    """Useful claims of a mission that has a product (hygiene.sql_done_product).

    Eligibility is hygiene.SQL_USEFUL_CLAIM (sourced, not a blob, not
    regex_fallback) and LENGTH(c.claim) > 20. Quality and confidence
    thresholds still apply afterwards. from_product is the sql_done_product
    result, not whether a row survived the length filter.
    """
    conn = _get_db()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT 1
        FROM hive_missions m
        WHERE m.id = %s
          AND {sql_done_product('m')}
        """,
        (mission_id,),
    )
    from_product = bool(cur.fetchall())
    cur.execute(
        f"""
        SELECT c.claim, c.confidence, c.quality_score, c.source_url, c.claim_type
        FROM hive_claims c
        JOIN hive_missions m ON m.id = c.mission_id
        WHERE c.mission_id = %s
          AND {SQL_USEFUL_CLAIM}
          AND LENGTH(c.claim) > 20
          AND {sql_done_product('m')}
        ORDER BY c.quality_score DESC NULLS LAST
        """,
        (mission_id,),
    )

    rows = cur.fetchall()
    conn.close()

    # Python thresholds may still drop every row of a real product; the
    # no-claims log path applies then. from_product False means no product.
    claims = _FetchedClaims(from_product)
    for claim_text, confidence, quality, source_url, claim_type in rows:
        # Combined confidence: average of BeHive confidence and quality_score
        conf = confidence or 0.7
        qual = quality or 0.5
        combined_confidence = (conf + qual) / 2.0
        
        # Apply thresholds
        if combined_confidence < MIN_CONFIDENCE:
            continue
        if qual is not None and qual < MIN_QUALITY:
            continue
        s = (claim_text or "").lstrip()
        if not s or s[0] in "{[":
            continue
        claims.append({
            "claim": claim_text[:500],  # SR has 500 char limit
            "confidence": round(min(combined_confidence, 0.99), 3),
            "source": f"behive:{mission_id}",
        })
    
    return claims


def _send_to_sr(claims: List[Dict]) -> Dict:
    """POST claims to SR /ingest in batches with rate limiting."""
    total_ingested = 0
    last_response = {}
    max_retries = 2
    
    for i in range(0, len(claims), BATCH_SIZE):
        batch = claims[i:i + BATCH_SIZE]
        
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(
                    SR_INGEST_URL,
                    json={"claims": batch},
                    timeout=SR_TIMEOUT,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    total_ingested += data.get("ingested", 0)
                    last_response = data
                    break  # Success
                else:
                    log.warning(f"SR ingest returned {resp.status_code}: {resp.text[:200]}")
                    break  # Don't retry on 4xx
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries:
                    wait = 10 * (attempt + 1)
                    log.warning(f"SR ingest attempt {attempt+1} failed ({e.__class__.__name__}), retry in {wait}s")
                    time.sleep(wait)
                else:
                    log.error(f"SR ingest failed after {max_retries+1} attempts for batch {i//BATCH_SIZE + 1}")
            except Exception as e:
                log.error(f"SR ingest unexpected error: {e}")
                break
        
        # Rate limit: 2s between batches to avoid overloading SR event loop
        if i + BATCH_SIZE < len(claims):
            time.sleep(2)
    
    last_response["total_ingested"] = total_ingested
    return last_response


# ─── Public API ────────────────────────────────────────────────────────────

def bridge_mission(mission_id: str) -> Optional[Dict]:
    """
    Bridge a completed BeHive mission's claims into Singularity Room.
    
    Returns:
        dict with {mission_id, claims_fetched, claims_sent, sr_response, encoder} on success
        None if already bridged, the mission has no product, or no claims found
    """
    _ensure_bridge_table()
    
    # Idempotency check
    if _was_already_bridged(mission_id):
        log.info(f"[BRIDGE] Mission {mission_id} already bridged — skipping")
        return None
    
    # Fetch claims. No explicit product flag (plain list, or this query saying
    # there is no product): send nothing and record nothing. Fail closed.
    claims = _fetch_mission_claims(mission_id)
    if getattr(claims, "from_product", None) is not True:
        log.info(f"[BRIDGE] Mission {mission_id} has no product — skipping")
        return None
    if not claims:
        log.info(f"[BRIDGE] Mission {mission_id} has no eligible claims — skipping")
        _mark_bridged(mission_id, 0, {"skipped": "no_claims"})
        return None
    
    log.info(f"[BRIDGE] Mission {mission_id}: {len(claims)} claims → SR")
    
    # Send to SR
    sr_response = _send_to_sr(claims)
    total_sent = sr_response.get("total_ingested", 0)
    
    # Verify SR's live encoder (no re-encode: /ingest extends it in-process)
    sr_response["encoder"] = _verify_encoder()
    
    # Record
    _mark_bridged(mission_id, total_sent, sr_response)
    
    log.info(f"[BRIDGE] Mission {mission_id}: {total_sent}/{len(claims)} claims ingested into SR")
    return {
        "mission_id": mission_id,
        "claims_fetched": len(claims),
        "claims_sent": total_sent,
        "sr_response": sr_response,
        "encoder": sr_response["encoder"],
    }


def bridge_all_pending() -> List[Dict]:
    """Bridge done missions that have a product and have not been bridged yet."""
    _ensure_bridge_table()
    
    conn = _get_db()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT m.id
        FROM hive_missions m
        LEFT JOIN sr_bridge_log b ON m.id = b.mission_id
        WHERE m.status = 'done'
          AND b.mission_id IS NULL
          AND {sql_done_product('m')}
        ORDER BY m.created_at DESC
        LIMIT 50
        """
    )
    pending = [row[0] for row in cur.fetchall()]
    conn.close()
    
    results = []
    for mid in pending:
        result = bridge_mission(mid)
        if result:
            results.append(result)
    
    return results


# ─── CLI ────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if argv and argv[0] == "--all":
        # Bridge all pending missions
        results = bridge_all_pending()
        print(f"Bridged {len(results)} missions")
        for r in results:
            print(f"  {r['mission_id']}: {r['claims_sent']}/{r['claims_fetched']} claims")
    elif argv:
        # Bridge specific mission
        result = bridge_mission(argv[0])
        if result:
            print(json.dumps(result, indent=2))
        else:
            print("Already bridged or no claims")
    else:
        print("Usage: python -m behive.sr_bridge <mission_id> | --all")


if __name__ == "__main__":
    main()
