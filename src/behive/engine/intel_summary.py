from typing import Any
#!/usr/bin/env python3
"""
HIVE Master Intelligence Summary — Cross-Mission Synthesis
===========================================================
Aggregates Queen assessments from multiple missions into a
unified intelligence picture. Runs after every synthesis phase.

Outputs:
  - hive_intel_summary table: timestamped meta-reports
  - /intelligence/summary API endpoint feeds from here

Usage:
  python3 hive2_intel_summary.py update <mission_id>   # post-synth hook
  python3 hive2_intel_summary.py build                  # full rebuild
  python3 hive2_intel_summary.py show                   # print latest
  python3 hive2_intel_summary.py topics                 # list topic clusters
"""

import os
import sys
import json
import time
import logging
import uuid
from datetime import datetime
from typing import Optional

import hive2_db as _hive_db  # PostgreSQL via unified layer

log = logging.getLogger("hive_intel_summary")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [INTEL] %(message)s")

DB_PATH = ""  # Legacy PostgreSQL removed — PostgreSQL is primary
HIVE_DIR = os.environ.get("HIVE_DIR", os.path.expanduser("~"))
SGLANG_URL = os.environ.get("SGLANG_URL", "http://localhost:8002/v1")


# ── DB schema ────────────────────────────────────────────────────────────────

def _init_db(con: Any):
    con.execute("""
        CREATE TABLE IF NOT EXISTS hive_intel_summary (
            id              VARCHAR PRIMARY KEY,
            missions_count  INTEGER,
            topics_json     VARCHAR,
            summary_md      VARCHAR,
            key_findings    VARCHAR,
            confidence_avg  FLOAT,
            created_at      TIMESTAMP DEFAULT now(),
            trigger_mission VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS hive_intel_topics (
            id          VARCHAR PRIMARY KEY,
            topic_label VARCHAR,
            mission_ids VARCHAR,
            claim_count INTEGER,
            confidence  FLOAT,
            summary     VARCHAR,
            updated_at  TIMESTAMP DEFAULT now()
        )
    """)
    con.commit()


# ── SGLang call ───────────────────────────────────────────────────────────────

def _sglang(prompt: str, system: str = "", max_tokens: int = 2048) -> str:
    """Call SGLang/Qwen on port 8002."""
    try:
        import urllib.request, json as _json
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = _json.dumps({
            "model": "default",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }).encode()
        req = urllib.request.Request(
            f"{SGLANG_URL}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read())
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning(f"SGLang failed ({e}) — fallback to Bedrock")
        return _bedrock_fallback(prompt, system, max_tokens)


def _bedrock_fallback(prompt: str, system: str = "", max_tokens: int = 2048) -> str:
    """LLM call via BYOK llm.complete() — supports any provider."""
    try:
        from behive.engine.llm import complete
        return complete(prompt, stage="process", system=system or "You are an intelligence analyst.", max_tokens=max_tokens)
    except Exception as e:
        log.error(f"LLM call failed: {e}")
        return "[synthesis unavailable]"


# ── Data collection ──────────────────────────────────────────────────────────

def _get_recent_assessments(con: Any, limit: int = 20) -> list[dict]:
    """Fetch recent Queen assessments."""
    try:
        rows = con.execute("""
            SELECT mission_id, query, must_know_insight,
                   confidence_calibrated, created_at, queen_json
            FROM hive_queen_assessments
            ORDER BY created_at DESC
            LIMIT ?
        """, [limit]).fetchall()
        result = []
        for r in rows:
            qa = {}
            try:
                full = json.loads(r[5] or "{}")
                qa = full.get("queen_assessment", {})
            except Exception:
                pass
            result.append({
                "mission_id": r[0],
                "query": r[1],
                "must_know": r[2] or "",
                "confidence": r[3] or 0.0,
                "created_at": str(r[4]),
                "confirmed": qa.get("section_1_confirmed_intelligence", ""),
                "recommendation": qa.get("section_5_strategic_recommendation", ""),
                "next_directive": qa.get("section_6_next_mission_directive", ""),
            })
        return result
    except Exception as e:
        log.warning(f"queen_assessments unavailable ({e}), falling back to missions")
        return []


def _get_recent_missions(con: Any, limit: int = 20) -> list[dict]:
    """Fallback: get missions with synthesis data."""
    rows = con.execute("""
        SELECT id, topic, status, quality_coverage, quality_confidence,
               quality_depth, created_at
        FROM hive_missions
        WHERE status = 'done'
        ORDER BY created_at DESC
        LIMIT ?
    """, [limit]).fetchall()
    return [
        {
            "mission_id": r[0], "query": r[1], "status": r[2],
            "coverage": r[3], "confidence": r[4] or 0.0, "depth": r[5],
            "created_at": str(r[6]),
        }
        for r in rows
    ]


def _get_top_claims_cross(con: Any,
                          mission_ids: list[str], limit: int = 60) -> list[str]:
    """Get top verified claims across multiple missions."""
    if not mission_ids:
        return []
    placeholders = ", ".join("?" * len(mission_ids))
    try:
        rows = con.execute(f"""
            SELECT claim, confidence, mission_id
            FROM hive_claims
            WHERE mission_id IN ({placeholders})
              AND confidence >= 0.55
            ORDER BY confidence DESC
            LIMIT ?
        """, mission_ids + [limit]).fetchall()
        return [f"[{r[2][:20]}] {r[0]}" for r in rows]
    except Exception:
        return []


# ── Topic clustering ─────────────────────────────────────────────────────────

def _cluster_topics(missions: list[dict], con: Any) -> list[dict]:
    """
    Lightweight topic clustering: group missions by keyword overlap.
    Returns list of topic clusters with their missions.
    """
    if not missions:
        return []

    # Build keyword sets per mission
    import re
    stopwords = {"the", "a", "an", "in", "of", "for", "on", "and", "or",
                 "to", "with", "is", "are", "was", "were", "be", "been",
                 "jak", "czy", "się", "na", "w", "z", "do", "dla", "i"}

    def keywords(text: str) -> set:
        words = re.findall(r'\b[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]{4,}\b', text.lower())
        return {w for w in words if w not in stopwords}

    # Simple greedy clustering
    clusters: list[dict] = []
    used = set()

    for i, m in enumerate(missions):
        if m["mission_id"] in used:
            continue
        kw_i = keywords(m["query"])
        cluster_missions = [m]
        used.add(m["mission_id"])

        for j, m2 in enumerate(missions[i+1:], i+1):
            if m2["mission_id"] in used:
                continue
            kw_j = keywords(m2["query"])
            overlap = len(kw_i & kw_j)
            if overlap >= 2:
                cluster_missions.append(m2)
                used.add(m2["mission_id"])
                kw_i = kw_i | kw_j  # merge keywords

        # Build cluster label from most common keywords
        all_kw = keywords(" ".join(m["query"] for m in cluster_missions))
        label = " · ".join(sorted(all_kw, key=len, reverse=True)[:4])
        if not label:
            label = cluster_missions[0]["query"][:60]

        avg_conf = sum(m.get("confidence", 0) for m in cluster_missions) / len(cluster_missions)
        mission_ids = [m["mission_id"] for m in cluster_missions]
        claims = _get_top_claims_cross(con, mission_ids, limit=20)

        clusters.append({
            "label": label,
            "mission_count": len(cluster_missions),
            "mission_ids": mission_ids,
            "queries": [m["query"] for m in cluster_missions],
            "avg_confidence": round(avg_conf, 3),
            "top_claims": claims[:10],
        })

    return sorted(clusters, key=lambda c: c["mission_count"], reverse=True)


# ── Cross-mission synthesis ──────────────────────────────────────────────────

def _build_synthesis_prompt(assessments: list[dict], missions: list[dict],
                             clusters: list[dict]) -> str:
    """Build the cross-mission synthesis prompt for Qwen."""
    lines = [
        f"You are synthesizing intelligence from {len(missions)} HIVE research missions.",
        f"Date: {datetime.utcnow().strftime('%Y-%m-%d')}",
        "",
        "## MISSIONS ANALYZED",
    ]

    for m in missions[:15]:
        conf = m.get("confidence", 0)
        lines.append(f"- [{m['mission_id'][:20]}] {m['query'][:80]} (confidence: {conf:.0%})")

    lines.append("")
    lines.append("## MUST-KNOW INSIGHTS FROM QUEEN ASSESSMENTS")
    for a in assessments[:10]:
        if a.get("must_know"):
            lines.append(f"- [{a['mission_id'][:20]}] {a['must_know'][:200]}")

    lines.append("")
    lines.append("## TOP CONFIRMED FINDINGS (cross-mission)")
    all_claims = []
    for c in clusters:
        all_claims.extend(c["top_claims"][:5])
    for claim in all_claims[:25]:
        lines.append(f"- {claim[:180]}")

    lines.append("")
    lines.append("## TOPIC CLUSTERS")
    for c in clusters[:6]:
        lines.append(f"- **{c['label']}** — {c['mission_count']} missions, "
                     f"avg confidence {c['avg_confidence']:.0%}")

    lines.append("")
    lines.append("""## YOUR TASK
Produce a concise Master Intelligence Summary in markdown with these sections:

### 🔑 Key Findings (3-5 bullet points across ALL missions)
### 🗺️ Knowledge Map (what domains/topics HIVE has covered, gaps)
### ⚡ Conflicting Evidence (where missions disagree — only if real conflicts found)
### 🎯 Strategic Intelligence (actionable conclusions from the corpus)
### 🔭 Coverage Gaps (what HIVE should research next)

Be specific, cite mission IDs where relevant. No filler. Max 600 words.""")

    return "\n".join(lines)


# ── Main build function ──────────────────────────────────────────────────────

def build_intel_summary(trigger_mission: str = "", max_missions: int = 25) -> dict:
    """
    Build or refresh the cross-mission intelligence summary.
    Returns dict with summary_md, topics, key_findings, etc.
    """
    con = _hive_db.connect(read_only=False)
    _init_db(con)

    # Collect data
    assessments = _get_recent_assessments(con, limit=max_missions)
    missions_raw = _get_recent_missions(con, limit=max_missions)

    # Merge: use assessments data if available, else missions
    assessment_ids = {a["mission_id"] for a in assessments}
    missions_merged = []
    for a in assessments:
        missions_merged.append(a)
    for m in missions_raw:
        if m["mission_id"] not in assessment_ids:
            missions_merged.append(m)

    if not missions_merged:
        con.close()
        return {"summary_md": "No completed missions yet.", "missions_count": 0}

    # Cluster topics
    clusters = _cluster_topics(missions_merged, con)

    # Save topic clusters
    for c in clusters:
        topic_id = f"topic_{uuid.uuid4().hex[:10]}"
        try:
            con.execute("DELETE FROM hive_intel_topics WHERE topic_label = ?", [c["label"][:100]])
            con.execute("""
                INSERT INTO hive_intel_topics
                    (id, topic_label, mission_ids, claim_count, confidence, summary, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, now())
            """, [
                topic_id, c["label"][:100],
                json.dumps(c["mission_ids"]),
                len(c["top_claims"]),
                c["avg_confidence"],
                "; ".join(c["queries"][:3])[:500],
            ])
        except Exception as e:
            log.warning(f"Topic save failed: {e}")
    con.commit()

    # Build synthesis prompt
    prompt = _build_synthesis_prompt(assessments, missions_merged, clusters)
    system = (
        "You are HIVE Master Intelligence — an expert research analyst synthesizing "
        "findings across multiple intelligence missions. "
        "CRITICAL: Respond ONLY in clean markdown text. Do NOT output JSON, code blocks, "
        "or structured data. Write prose with markdown headers and bullet points only. "
        "Be precise, evidence-based, and actionable."
    )

    log.info(f"Synthesizing cross-mission summary ({len(missions_merged)} missions)...")
    # Use Bedrock for narrative synthesis — bee is fine-tuned for JSON, not prose
    summary_md = _bedrock_fallback(prompt, system=system, max_tokens=1500)

    # Extract key findings (first ## section)
    key_findings = ""
    for line in summary_md.splitlines():
        if "Key Findings" in line or "key_findings" in line.lower():
            continue
        if line.startswith("### ") and "Key" not in line:
            break
        if line.startswith("- ") or line.startswith("• "):
            key_findings += line + "\n"

    avg_conf = sum(m.get("confidence", 0) for m in missions_merged) / max(len(missions_merged), 1)
    summary_id = f"intel_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    # Save summary
    con.execute("""
        INSERT INTO hive_intel_summary
            (id, missions_count, topics_json, summary_md, key_findings,
             confidence_avg, trigger_mission)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, [
        summary_id, len(missions_merged),
        json.dumps([c["label"] for c in clusters[:10]]),
        summary_md, key_findings.strip(),
        round(avg_conf, 3),
        trigger_mission or None,
    ])
    con.commit()
    con.close()

    log.info(f"Intel summary saved: {summary_id} ({len(missions_merged)} missions)")
    return {
        "id": summary_id,
        "summary_md": summary_md,
        "key_findings": key_findings.strip(),
        "missions_count": len(missions_merged),
        "topics": [c["label"] for c in clusters[:10]],
        "confidence_avg": round(avg_conf, 3),
        "topic_clusters": clusters[:8],
    }


def get_latest_summary(con: Any = None) -> Optional[dict]:
    """Get the most recent intel summary from DB."""
    close_after = con is None
    if con is None:
        con = _hive_db.connect(read_only=True)
    try:
        tables = [t[0] for t in con.execute("SHOW TABLES").fetchall()]
        if "hive_intel_summary" not in tables:
            return None
        row = con.execute("""
            SELECT id, missions_count, topics_json, summary_md,
                   key_findings, confidence_avg, created_at, trigger_mission
            FROM hive_intel_summary
            ORDER BY created_at DESC
            LIMIT 1
        """).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "missions_count": row[1],
            "topics": json.loads(row[2] or "[]"),
            "summary_md": row[3] or "",
            "key_findings": row[4] or "",
            "confidence_avg": row[5] or 0.0,
            "created_at": str(row[6]),
            "trigger_mission": row[7],
        }
    finally:
        if close_after:
            con.close()


def get_topic_clusters(limit: int = 20) -> list[dict]:
    """Get all topic clusters."""
    con = _hive_db.connect(read_only=True)
    try:
        tables = [t[0] for t in con.execute("SHOW TABLES").fetchall()]
        if "hive_intel_topics" not in tables:
            return []
        rows = con.execute("""
            SELECT id, topic_label, mission_ids, claim_count, confidence, summary, updated_at
            FROM hive_intel_topics
            ORDER BY claim_count DESC
            LIMIT ?
        """, [limit]).fetchall()
        return [
            {
                "id": r[0],
                "label": r[1],
                "mission_ids": json.loads(r[2] or "[]"),
                "claim_count": r[3] or 0,
                "confidence": r[4] or 0.0,
                "summary": r[5] or "",
                "updated_at": str(r[6]),
            }
            for r in rows
        ]
    finally:
        con.close()


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="HIVE Intel Summary CLI")
    sub = parser.add_subparsers(dest="cmd")

    upd = sub.add_parser("update", help="Post-synth hook: update summary after mission")
    upd.add_argument("mission_id")

    sub.add_parser("build", help="Full rebuild of intel summary")
    sub.add_parser("show",  help="Print latest summary")
    sub.add_parser("topics", help="List topic clusters")

    args = parser.parse_args()

    if args.cmd in ("update", "build"):
        mid = getattr(args, "mission_id", "")
        result = build_intel_summary(trigger_mission=mid)
        print(f"✅ Intel summary built: {result['missions_count']} missions, "
              f"{len(result['topics'])} topics")
        print(f"Confidence avg: {result['confidence_avg']:.0%}")
        if result.get("key_findings"):
            print("\n--- Key Findings ---")
            print(result["key_findings"][:500])

    elif args.cmd == "show":
        s = get_latest_summary()
        if s:
            print(f"[{s['id']}] {s['created_at']} | {s['missions_count']} missions")
            print(s["summary_md"])
        else:
            print("No summary yet. Run: python3 hive2_intel_summary.py build")

    elif args.cmd == "topics":
        clusters = get_topic_clusters()
        for c in clusters:
            mids = ", ".join(c["mission_ids"][:3])
            print(f"  {c['label'][:60]:60} | {c['claim_count']} claims | "
                  f"conf {c['confidence']:.0%} | missions: {mids}")

    else:
        parser.print_help()
