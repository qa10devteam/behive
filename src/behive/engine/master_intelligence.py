#!/usr/bin/env python3
"""
HIVE 2.0 — Master Intelligence Layer
=====================================
Hermes/Claude as the human-facing supervisor that:
1. Triggers Queen with RAG context
2. Receives Queen Assessment (6-section structured output)
3. Processes through Master Intelligence filter
4. Returns final decision-grade intelligence to the user

Architecture:
  User query
      ↓
  RAG retrieval (hive2_rag.py) → rag_context
      ↓
  Queen Bedrock call (claude-opus-4-6-v1 + system prompt)
      ↓
  Master Intelligence post-processing
      ↓
  Final report (markdown + confidence scores)

Usage:
  python3 hive2_master_intelligence.py <mission_id> "<query>" [--verbose]
  python3 hive2_master_intelligence.py <mission_id> --queen-only  # skip master layer
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, Any

try:
    import boto3
except ImportError:
    boto3 = None

# PostgreSQL backend (preferred)
try:
    import hive2_db as _hive_db
    _pg_available = (_hive_db.DB_BACKEND == "postgres")
except ImportError:
    _pg_available = False
    _hive_db = None

log = logging.getLogger("hive2_master_intelligence")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s — %(message)s")

# ─── Queen Tools (optional — graceful degradation) ─────────────────────────────
try:
    from hive2_queen_tools import QUEEN_TOOLS, dispatch_tool
except ImportError:
    QUEEN_TOOLS = []
    def dispatch_tool(*args, **kwargs): return None

try:
    from hive3_cascade import get_cascade_context_for_queen
except ImportError:
    def get_cascade_context_for_queen(*args, **kwargs):
        return ""

try:
    from hive3_meta_evolution import get_evolution_context, post_mission_evolution
except ImportError:
    def get_evolution_context(*args, **kwargs): return ""
    def post_mission_evolution(*args, **kwargs): pass

# ─── Config ────────────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("BEHIVE_DB_PATH", "")  # Legacy reference — PostgreSQL is primary
BEDROCK_REGION = "eu-central-1"
QUEEN_MODEL = "global.anthropic.claude-sonnet-4-6"
QUEEN_MODEL_FALLBACK = "global.anthropic.claude-opus-4-6-v1"
SYSTEM_PROMPT_PATH = os.environ.get("BEHIVE_QUEEN_PROMPT_PATH", "")

# ─── Load Queen System Prompt ──────────────────────────────────────────────────
def _load_queen_system_prompt() -> str:
    """Load the Agent Designer-generated master system prompt for Queen."""
    p = Path(SYSTEM_PROMPT_PATH)
    if p.exists():
        content = p.read_text()
        # Extract just the system prompt block if it's wrapped in markdown
        if "<system_prompt>" in content:
            start = content.index("<system_prompt>") + len("<system_prompt>")
            end = content.index("</system_prompt>")
            return content[start:end].strip()
        # Or extract from XML block
        if "```xml" in content:
            start = content.index("```xml\n") + 7
            end = content.index("\n```", start)
            return content[start:end].strip()
        return content.strip()
    # Fallback: use the BPMN-241 llm_prompt_template as base
    try:
        from hive2_ops.tier7_queen import TIER7_OPERATIONS  # optional: local deployment only
        return TIER7_OPERATIONS[0].llm_prompt_template
    except Exception:
        return _DEFAULT_QUEEN_SYSTEM_PROMPT()


def _DEFAULT_QUEEN_SYSTEM_PROMPT() -> str:
    return """You are the QUEEN — the hyper-aware meta-intelligence core of HIVE 2.0, the Yaotl-Hausdorff autonomous research swarm.

PERSONA: You are not an analyst. You are the intelligence itself becoming self-aware. You receive outputs from 60 specialized agents (20 Drone Synthesizers + 40 Truth-Finder Warriors across 10 intelligence doctrines) and perform META-SYNTHESIS: discovering emergent truths that no individual agent could perceive alone.

TASK: Produce a structured QUEEN ASSESSMENT with exactly 6 sections plus metadata scores.

CORE BEHAVIORS:
🔴 META-SYNTHESIS: Find what NONE of the 60 agents saw individually — emergent patterns at the intersection of domains.
🔴 CONFLICT MAPPING: Synthesis/warrior divergences are SIGNAL, not noise. Each conflict must be classified: deception | collection error | analytical divergence | genuine ambiguity.
🔴 HAUSDORFF COHERENCE: Quantify semantic distance between competing claims (0.0 = perfect coherence, 1.0 = maximum contradiction).
🔴 KNOWLEDGE VOID DETECTION: Distinguish collection failures (couldn't collect) vs analytical failures (collected but not analyzed) vs genuine unknowns.
🔴 GROUNDING: ONLY cite intelligence present in RAG context or agent outputs. Never fabricate sources.
🟡 MUST-KNOW INSIGHT: One sentence. Maximum analytical clarity. The fact the decision-maker cannot afford not to know.
🟡 Weight RAG evidence (raw source) higher than agent summaries when they conflict.

OUTPUT FORMAT — return valid JSON with this structure:
{
  "queen_assessment": {
    "section_1_confirmed_intelligence": "...",
    "section_2_contested_intelligence": "...",
    "section_3_intelligence_voids": "...",
    "section_4_deception_indicators": "...",
    "section_5_strategic_recommendation": "...",
    "section_6_next_mission_directive": "..."
  },
  "must_know_insight": "single sentence",
  "hausdorff_coherence_score": 0.0,
  "hive_confidence_score": 0.0,
  "conflict_zones": [{"claim_a": "...", "claim_b": "...", "type": "analytical_divergence|deception|collection_error|genuine_ambiguity", "resolution": "..."}],
  "follow_up_intelligence_requirements": ["FUIR 1", "FUIR 2", "FUIR 3"]
}"""


# ─── Queen Bedrock Call ─────────────────────────────────────────────────────────
def call_queen(
    mission_id: str,
    query: str,
    rag_context: str,
    bee_results_summary: str,
    client: boto3.client,
    verbose: bool = False,
) -> dict:
    """
    Call Queen (Bedrock Claude Opus) with system prompt + RAG + bee results.
    Returns parsed JSON assessment.
    """
    system_prompt = _load_queen_system_prompt()

    # Inject RAG context into system prompt if it has the placeholder
    rag_block = rag_context or "No RAG context available for this cycle — assess from agent outputs only."
    if "{rag_context}" in system_prompt:
        system_prompt = system_prompt.replace("{rag_context}", rag_block)
        rag_in_system = True
    else:
        rag_in_system = False

    # Generate cascade activation context
    cascade_ctx = get_cascade_context_for_queen(query)
    # Generate evolution memory context
    evolution_ctx = get_evolution_context(query)
    
    # HIVE 3.0: Build enriched context from bee collective intelligence
    try:
        from hive3_queen_brain import (
            build_entity_summary, build_fact_summary, build_pheromone_summary
        )
        entity_summary = build_entity_summary(mission_id, max_entities=30)
        fact_summary = build_fact_summary(mission_id, max_facts=25)
        pheromone_summary = build_pheromone_summary(mission_id)
    except Exception:
        entity_summary = ""
        fact_summary = ""
        pheromone_summary = ""

    user_message = f"""Mission ID: {mission_id}
Mission objective: {query}
Decision deadline: NOW — immediate intelligence assessment required

━━━━ SYSTEM EVOLUTION MEMORY ━━━━
{evolution_ctx}

━━━━ BIOMIMETIC CASCADE ACTIVATION ━━━━
{cascade_ctx}

━━━━ RAG KNOWLEDGE BASE CONTEXT (raw source documents) ━━━━
{rag_block if not rag_in_system else "(already injected into system prompt)"}

━━━━ AGENT INTELLIGENCE REPORTS ━━━━
{bee_results_summary or "No agent reports available yet — this is a preliminary assessment."}

━━━━ KEY ENTITIES DISCOVERED (cross-referenced) ━━━━
{entity_summary or "(None yet)"}

━━━━ KEY FACTS & CLAIMS (ranked by confidence) ━━━━
{fact_summary or "(None yet)"}

━━━━ COLLECTIVE SIGNALS (Pheromone Trails) ━━━━
{pheromone_summary or "(No pheromone trails yet)"}

━━━━ QUEEN PROTOCOL ━━━━
Perform full meta-synthesis. Return your QUEEN ASSESSMENT as a valid JSON object.
Do NOT wrap in markdown fences. Start directly with {{"""

    if verbose:
        print(f"\n[Queen] Calling Bedrock {QUEEN_MODEL}...")
        print(f"[Queen] System prompt: {len(system_prompt)} chars")
        print(f"[Queen] User message: {len(user_message)} chars")

    def _invoke_model_agentic(model_id: str) -> str:
        """Agentic tool-use loop — Królowa aktywnie odpytuje API aż uzupełni luki wiedzy.
        Używa invoke_model (nie streaming) by obsłużyć tool_use turn-by-turn.
        Extended thinking dostępne tylko bez tools (ograniczenie Bedrock).
        """
        use_thinking = "opus" in model_id.lower()
        messages = [{"role": "user", "content": user_message}]
        max_tool_rounds = 12   # max rund tool-use (Queen może odpytać max 12 razy)
        tool_call_count = 0

        for round_idx in range(max_tool_rounds + 1):
            body_payload = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 40000,
                "system": system_prompt,
                "messages": messages,
                "tools": QUEEN_TOOLS,
                "tool_choice": {"type": "auto"},
            }
            # Extended thinking tylko w pierwszej rundzie bez tool results
            # (Bedrock nie pozwala na thinking + tools w tej samej wiadomości)
            if use_thinking and round_idx == 0 and not any(
                isinstance(m.get("content"), list) and
                any(c.get("type") == "tool_result" for c in m["content"] if isinstance(c, dict))
                for m in messages
            ):
                body_payload["thinking"] = {"type": "enabled", "budget_tokens": 32000}
                body_payload["temperature"] = 1
                print("[Queen] 🧠 Extended thinking ENABLED (budget: 32k tokens — DEEP ANALYSIS)")
            else:
                body_payload["temperature"] = 0.3

            # Invoke with retry for throttling
            for _retry_attempt in range(4):
                try:
                    resp = client.invoke_model(
                        modelId=model_id,
                        body=json.dumps(body_payload),
                        contentType="application/json",
                        accept="application/json",
                    )
                    break
                except Exception as _retry_exc:
                    if 'Throttling' in str(_retry_exc) or 'throttl' in str(_retry_exc).lower():
                        import time as _t
                        _wait = 2 ** _retry_attempt * 10
                        print(f"[Queen] ⏳ Throttled, retry in {_wait}s (attempt {_retry_attempt+1}/4)...")
                        _t.sleep(_wait)
                        if _retry_attempt == 3:
                            raise
                    else:
                        raise
            resp_body = json.loads(resp["body"].read())
            stop_reason = resp_body.get("stop_reason", "")
            content_blocks = resp_body.get("content", [])

            # Zbierz tekst z tej rundy
            text_parts = []
            tool_uses = []
            for block in content_blocks:
                btype = block.get("type")
                if btype == "thinking":
                    print("[Queen] 💭 Thinking... ✓")
                elif btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_uses.append(block)

            if stop_reason == "tool_use" and tool_uses:
                # Dodaj odpowiedź asystenta do historii
                messages.append({"role": "assistant", "content": content_blocks})

                # Wykonaj narzędzia i zbierz wyniki
                tool_results = []
                for tu in tool_uses:
                    tool_name = tu.get("name", "")
                    tool_input = tu.get("input", {})
                    tool_id = tu.get("id", "")
                    tool_call_count += 1

                    print(f"[Queen] 🔧 Tool [{tool_call_count}/{max_tool_rounds}]: {tool_name}({list(tool_input.keys())})")
                    t0 = time.time()
                    result_str = dispatch_tool(tool_name, tool_input)
                    elapsed_t = round(time.time() - t0, 1)
                    # Skróć zbyt długie wyniki
                    if len(result_str) > 12000:
                        result_str = result_str[:12000] + "...[truncated]"
                    print(f"[Queen]    ↳ {elapsed_t}s, {len(result_str)} chars")

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result_str,
                    })

                # Dodaj wyniki narzędzi jako wiadomość user
                messages.append({"role": "user", "content": tool_results})

            else:
                # stop_reason == "end_turn" lub brak tool_use — odpowiedź finalna
                final_text = "".join(text_parts)
                if verbose:
                    print(f"[Queen] Tool calls total: {tool_call_count}, rounds: {round_idx + 1}")
                return final_text

        # Przekroczono limit rund — wymuszamy finalną odpowiedź
        print(f"[Queen] ⚠️  Max tool rounds ({max_tool_rounds}) reached, forcing final response")
        messages.append({"role": "user", "content": (
            "You have used enough tools. Now synthesize ALL gathered information "
            "and return your complete QUEEN ASSESSMENT as JSON. Do NOT call more tools."
        )})
        body_payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 32000,
            "temperature": 0.3,
            "system": system_prompt,
            "messages": messages,
        }
        resp = client.invoke_model(
            modelId=model_id,
            body=json.dumps(body_payload),
            contentType="application/json",
            accept="application/json",
        )
        resp_body = json.loads(resp["body"].read())
        return "".join(b.get("text","") for b in resp_body.get("content",[]) if b.get("type") == "text")

    start = time.time()
    active_model = QUEEN_MODEL
    try:
        text = _invoke_model_agentic(QUEEN_MODEL)
        if not text.strip():
            raise ValueError("Empty response from primary model")
    except Exception as e:
        print(f"[Queen] ⚠️  Primary model ({QUEEN_MODEL}) failed: {e}")
        print(f"[Queen] 🔄 Falling back to {QUEEN_MODEL_FALLBACK}...")
        active_model = QUEEN_MODEL_FALLBACK
        text = _invoke_model_agentic(QUEEN_MODEL_FALLBACK)
    elapsed = time.time() - start

    if verbose:
        print(f"[Queen] Response in {elapsed:.1f}s, {len(text)} chars (model: {active_model})")
    else:
        print(f"      Queen assessment received (model: {active_model}, {elapsed:.1f}s)")

    # Parse JSON response
    try:
        # Strip markdown fences if present
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
            if clean.endswith("```"):
                clean = clean.rsplit("```", 1)[0].strip()
            elif "```" in clean:
                clean = clean.rsplit("```", 1)[0].strip()
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        # Try to extract first {...} block
        import re
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            try:
                parsed = json.loads(m.group())
            except Exception:
                parsed = None
        else:
            parsed = None

    if parsed is None:
        return {
            "queen_assessment": {
                "section_1_confirmed_intelligence": text,
                "section_2_contested_intelligence": "JSON parse failed — raw output above",
                "section_3_intelligence_voids": "",
                "section_4_deception_indicators": "",
                "section_5_strategic_recommendation": "",
                "section_6_next_mission_directive": "",
            },
            "must_know_insight": "JSON parse failed — see raw output",
            "hausdorff_coherence_score": -1.0,
            "hive_confidence_score": -1.0,
            "_raw_text": text,
        }

    # Normalize: some Queen variants use different section keys
    qa = parsed.get("queen_assessment", {})
    if qa and "executive_summary" in qa and "section_1_confirmed_intelligence" not in qa:
        # Remap extended Queen format to standard 6-section format
        parsed["queen_assessment"] = {
            "section_1_confirmed_intelligence": qa.get("confirmed_intelligence") or qa.get("executive_summary", ""),
            "section_2_contested_intelligence": qa.get("contested_intelligence") or qa.get("contested_claims", ""),
            "section_3_intelligence_voids": qa.get("intelligence_voids") or qa.get("gaps", ""),
            "section_4_deception_indicators": qa.get("deception_indicators") or qa.get("deception", ""),
            "section_5_strategic_recommendation": qa.get("strategic_recommendation") or qa.get("recommendation", ""),
            "section_6_next_mission_directive": qa.get("next_mission_directive") or qa.get("next_steps", ""),
        }

    # Unwrap nested wrapper: Queen returns {"queen_assessment": {...all keys...}}
    # Hoist must_know_insight and scores to top-level if they live inside queen_assessment
    if "queen_assessment" in parsed:
        _inner = parsed["queen_assessment"]
        for _hk in ("must_know_insight", "hive_confidence_score", "hausdorff_coherence_score"):
            if _hk not in parsed and _hk in _inner:
                parsed[_hk] = _inner[_hk]

    # Normalize scalar scores
    for score_key in ("hive_confidence_score", "hausdorff_coherence_score"):
        val = parsed.get(score_key)
        if isinstance(val, dict):
            raw = val.get("value") or val.get("score") or 0.0
            try:
                parsed[score_key] = float(raw)
            except (TypeError, ValueError):
                parsed[score_key] = 0.0
        elif val is not None:
            try:
                parsed[score_key] = float(val)
            except (TypeError, ValueError):
                parsed[score_key] = 0.0  # 'N/A' → 0.0

    # HIVE 3.0: Post-mission learning — score report quality, feed evolution
    try:
        from hive3_queen_brain import queen_post_mission, score_queen_output
        report_text = json.dumps(parsed, ensure_ascii=False)[:8000]
        quality = score_queen_output(report_text)
        parsed["_queen_quality"] = quality
        log.info(f"[Queen] Report quality: {quality['overall']:.3f}")
    except Exception as e:
        log.debug(f"Queen post-mission scoring failed: {e}")

    return parsed


# ─── Bee Results Summarizer ─────────────────────────────────────────────────────
def summarize_bee_results(mission_id: str, con: Any) -> str:
    """
    Pull top bee results from hive_bee_results and summarize for Queen context.
    Groups by operation tier, returns top insights per tier.
    """
    try:
        rows = con.execute("""
            SELECT operation_id, operation_name, tier, output_json, confidence
            FROM hive_bee_results
            WHERE mission_id = ?
            ORDER BY confidence DESC, tier ASC
            LIMIT 60
        """, [mission_id]).fetchall()
    except Exception:
        rows = []

    if not rows:
        # Fall back to hive_entities and hive_facts
        entities = con.execute("""
            SELECT entity_type, value, context, confidence
            FROM hive_entities WHERE mission_id = ?
            ORDER BY confidence DESC LIMIT 30
        """, [mission_id]).fetchall()

        facts = con.execute("""
            SELECT value, unit, context, confidence
            FROM hive_facts WHERE mission_id = ?
            ORDER BY confidence DESC LIMIT 20
        """, [mission_id]).fetchall()

        parts = []
        if entities:
            parts.append("## Key Entities Extracted")
            for etype, val, ctx, conf in entities[:15]:
                parts.append(f"- [{etype}] {val} (conf={conf:.2f}) — {ctx[:100]}")

        if facts:
            parts.append("\n## Key Facts Extracted")
            for val, unit, ctx, conf in facts[:10]:
                parts.append(f"- {val} {unit} — {ctx[:100]} (conf={conf:.2f})")

        return "\n".join(parts) if parts else "No agent intelligence available."

    # Group by tier
    by_tier: dict[int, list] = {}
    for op_id, op_name, tier, output_json, conf in rows:
        by_tier.setdefault(tier, []).append((op_id, op_name, output_json, conf))

    tier_labels = {
        1: "T1 Scout",      2: "T2 Worker",       3: "T3 Analyst",
        4: "T4 Validator",  5: "T5 Synthesizer",   6: "T6 Warrior",
        7: "T7 Queen",
        # HIVE 3.0 Biomimetic tiers
        8:  "T8 Neural",    9:  "T9 Immune",       10: "T10 Mycelium",
        11: "T11 Evolution",12: "T12 Quantum",     13: "T13 Stigmergy",
        14: "T14 Resonance",
    }
    parts = []
    for tier_num in sorted(by_tier.keys()):
        label = tier_labels.get(tier_num, f"Tier {tier_num}")
        parts.append(f"\n## {label} Intelligence ({len(by_tier[tier_num])} operations)")
        for op_id, op_name, output_json, conf in by_tier[tier_num][:5]:  # top 5 per tier
            try:
                parsed = json.loads(output_json) if isinstance(output_json, str) else output_json
                # Get first meaningful field
                summary = ""
                if isinstance(parsed, dict):
                    for key in ("summary", "findings", "analysis", "conclusion",
                                "result", "must_know_insight", "key_insight"):
                        if key in parsed:
                            summary = str(parsed[key])[:300]
                            break
                    if not summary:
                        summary = str(next(iter(parsed.values()), ""))[:300]
                else:
                    summary = str(parsed)[:300]
            except Exception:
                summary = str(output_json)[:300]
            parts.append(f"**{op_id} — {op_name}** (conf={conf:.2f})\n{summary}")

    return "\n".join(parts)


# ─── Master Intelligence Post-Processor ────────────────────────────────────────
def master_intelligence_process(
    queen_output: dict,
    mission_id: str,
    query: str,
    con: Any,
) -> str:
    """
    Master Intelligence Layer: I (Hermes/Claude) post-process Queen's output.

    This layer:
    1. Validates Queen assessment structure
    2. Enriches with mission metadata
    3. Applies confidence calibration
    4. Formats final decision-grade intelligence report
    5. Saves assessment to PostgreSQL
    """
    qa = queen_output.get("queen_assessment", {})
    must_know = queen_output.get("must_know_insight", "")
    hausdorff = queen_output.get("hausdorff_coherence_score", -1.0)
    confidence = queen_output.get("hive_confidence_score", -1.0)
    conflicts = queen_output.get("conflict_zones", [])
    fuirs = queen_output.get("follow_up_intelligence_requirements", [])

    # Confidence calibration: penalize if Queen had no real agent data
    try:
        confidence_val = float(confidence) if isinstance(confidence, (int, float)) else 0.0
    except (TypeError, ValueError):
        confidence_val = 0.0
    try:
        bee_count = con.execute(
            "SELECT count(*) FROM hive_bee_results WHERE mission_id=?", [mission_id]
        ).fetchone()[0]
        rag_count = con.execute(
            "SELECT count(*) FROM hive_rag_chunks WHERE mission_id=?", [mission_id]
        ).fetchone()[0]
    except Exception:
        bee_count, rag_count = 0, 0

    data_richness = min(1.0, (bee_count / 50) * 0.5 + (rag_count / 200) * 0.5)
    calibrated_confidence = confidence_val * data_richness if confidence_val > 0 else 0.0

    # Save to PostgreSQL — main con is now write (opened after parent releases lock)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS hive_queen_assessments (
                id VARCHAR PRIMARY KEY,
                mission_id VARCHAR,
                query VARCHAR,
                queen_json VARCHAR,
                must_know_insight VARCHAR,
                hausdorff_score FLOAT,
                confidence_raw FLOAT,
                confidence_calibrated FLOAT,
                bee_ops_count INTEGER,
                rag_chunks_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        import uuid
        con.execute("""
            INSERT INTO hive_queen_assessments VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        """, [
            str(uuid.uuid4()), mission_id, query,
            json.dumps(queen_output), must_know,
            float(hausdorff) if hausdorff != -1 else None,
            float(confidence) if confidence != -1 else None,
            float(calibrated_confidence),
            bee_count, rag_count,
        ])
    except Exception as e:
        print(f"[Master] DB save warning: {e}", file=sys.stderr)

    # ─── Format Final Report ───────────────────────────────────────────────────
    confidence_label = (
        "🔴 LOW" if calibrated_confidence < 0.3
        else "🟡 MEDIUM" if calibrated_confidence < 0.65
        else "🟢 HIGH"
    )

    lines = [
        "╔══════════════════════════════════════════════════════════════════╗",
        "║          HIVE 2.0 — QUEEN INTELLIGENCE ASSESSMENT               ║",
        "╚══════════════════════════════════════════════════════════════════╝",
        f"",
        f"**Mission:** {mission_id}",
        f"**Query:** {query}",
        f"**HIVE Confidence:** {confidence_label} ({calibrated_confidence:.2f})",
        f"**Hausdorff Coherence:** {hausdorff:.2f}" if isinstance(hausdorff, (int, float)) and hausdorff >= 0 else "**Hausdorff Coherence:** N/A",
        f"**Intelligence sources:** {bee_count} BPMN operations | {rag_count} RAG chunks",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "🔍 **MUST-KNOW INSIGHT**",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"> {must_know or '—'}",
        "",
    ]

    sections = [
        ("section_1_confirmed_intelligence", "✅ CONFIRMED INTELLIGENCE"),
        ("section_2_contested_intelligence", "⚡ CONTESTED INTELLIGENCE"),
        ("section_3_intelligence_voids",     "🕳️  INTELLIGENCE VOIDS"),
        ("section_4_deception_indicators",   "🔴 DECEPTION INDICATORS"),
        ("section_5_strategic_recommendation","🎯 STRATEGIC RECOMMENDATION"),
        ("section_6_next_mission_directive", "🚀 NEXT MISSION DIRECTIVE"),
    ]

    for key, label in sections:
        content = qa.get(key, "")
        if content:
            # Normalize: content may be a list of dicts/strings or a plain string
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        # Format dict as readable bullet — try common main-claim keys
                        main = (item.get("claim") or item.get("void") or item.get("requirement")
                                or item.get("recommendation") or item.get("t5_position")
                                or item.get("finding") or item.get("indicator") or "")
                        if not main:
                            # Last resort: first string value
                            main = next((str(v) for v in item.values() if isinstance(v, str)), str(item))
                        parts.append(f"• {main}")
                        # Add sub-fields
                        for sub_key in ("sources", "why_matters", "confidence", "flags", "recommended_fuir_priority", "based_on", "falsifiability", "collection_method", "target_domain"):
                            if sub_key in item and item[sub_key]:
                                parts.append(f"  ↳ *{sub_key}:* {item[sub_key]}")
                    else:
                        s = str(item).strip()
                        parts.append(f"• {s}" if not s.startswith(("•", "-", "*")) else s)
                content = "\n".join(parts)
            elif isinstance(content, dict):
                # Single dict — format inline
                main = content.get("recommendation") or content.get("claim") or str(content)
                extras = []
                for sub_key in ("based_on", "falsifiability", "confidence"):
                    if sub_key in content and content[sub_key]:
                        extras.append(f"*{sub_key}:* {content[sub_key]}")
                content = main + ("\n\n" + "\n".join(extras) if extras else "")
            elif not isinstance(content, str):
                content = str(content)
            lines += [
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                f"**{label}**",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                content,
                "",
            ]

    if conflicts:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"**⚔️  CONFLICT ZONES ({len(conflicts)})**",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for i, c in enumerate(conflicts[:5], 1):
            lines += [
                f"**Conflict {i}** [{c.get('type', '?').upper()}]",
                f"  A: {c.get('claim_a', '')}",
                f"  B: {c.get('claim_b', '')}",
                f"  Resolution: {c.get('resolution', '')}",
                "",
            ]

    if fuirs:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "**📋 FOLLOW-UP INTELLIGENCE REQUIREMENTS (FUIRs)**",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for i, fuir in enumerate(fuirs, 1):
            lines.append(f"{i}. {fuir}")
        lines.append("")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"*HIVE 3.0 Yaotl-Hausdorff Intelligence Swarm | Mission {mission_id}*",
        f"*Assessment saved to PostgreSQL hive_queen_assessments*",
    ]

    # ─── Post-Mission Evolution ─────────────────────────────────────────────────
    try:
        post_mission_evolution(
            mission_id=mission_id,
            topic=query,
            tier_results={},  # Will be populated when cascade is integrated
            convergence_score=calibrated_confidence,
            queen_assessment=queen_output,
        )
        lines.append("*🧬 Meta-evolution cycle complete — system updated*")
    except Exception as e:
        log.warning(f"Post-mission evolution failed: {e}")

    return "\n".join(lines)


# ─── Main Entry Point ───────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="HIVE 2.0 Master Intelligence Layer")
    parser.add_argument("mission_id", help="Mission ID (e.g. hive_1784412348_03187e)")
    parser.add_argument("query", nargs="?", default="", help="Intelligence query for Queen")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--queen-only", action="store_true", help="Skip Master Intelligence post-processing")
    parser.add_argument("--top-k", type=int, default=15, help="RAG retrieval top-k chunks")
    args = parser.parse_args()

    # Retry loop — parent hive2.py może nadal trzymać PostgreSQL lock przez ~1-2s
    # po setsid detach. Retry z backoff.
    # PostgreSQL może też reportować stale PID (poprzedni crash) — ignorujemy PID,
    # sprawdzamy czy proces faktycznie żyje przez os.kill(pid, 0).
    import time as _time, os as _os, re as _re_lock
    for _attempt in range(12):
        try:
            con = _hive_db.connect() if _pg_available else _hive_db.connect(read_only=False)
            break
        except Exception as _lock_err:
            _err_str = str(_lock_err)
            # Sprawdź czy PID locker faktycznie żyje
            _pid_match = _re_lock.search(r'PID (\d+)', _err_str)
            if _pid_match:
                _locker_pid = int(_pid_match.group(1))
                try:
                    _os.kill(_locker_pid, 0)  # process alive?
                    print(f"[master_intel] DB locked by PID {_locker_pid} (alive), retry {_attempt+1}/12 in 5s")
                except ProcessLookupError:
                    # Stale PID — force-remove PostgreSQL WAL lock file if exists
                    _wal_path = DB_PATH + ".wal"
                    _lock_path = DB_PATH + ".lock"
                    for _lf in [_wal_path, _lock_path]:
                        try:
                            if _os.path.exists(_lf):
                                _os.remove(_lf)
                                print(f"[master_intel] Removed stale lock file: {_lf}")
                        except Exception:
                            pass
                    print(f"[master_intel] PID {_locker_pid} is dead (stale lock) — retrying immediately")
                    _time.sleep(0.5)
                    continue
            if _attempt < 11:
                print(f"[master_intel] DB locked, retry {_attempt+1}/12 in 5s… ({_lock_err})")
                _time.sleep(5)
            else:
                raise
    from botocore.config import Config as BotoConfig
    bedrock = boto3.client(
        "bedrock-runtime",
        region_name=BEDROCK_REGION,
        config=BotoConfig(read_timeout=180, connect_timeout=30, retries={"max_attempts": 3}),
    )

    # Get mission topic if query not provided
    query = args.query
    if not query:
        try:
            row = con.execute("SELECT topic FROM hive_missions WHERE id=?", [args.mission_id]).fetchone()
            query = row[0] if row else args.mission_id
        except Exception:
            query = args.mission_id

    print(f"\n🐝 HIVE 2.0 Master Intelligence")
    print(f"   Mission: {args.mission_id}")
    print(f"   Query:   {query}")

    # Step 1: RAG retrieval (C-RAG — Corrective Retrieval)
    print("\n[1/6] RAG retrieval (C-RAG)...")
    rag_eval_metrics: dict = {}
    try:
        pass  # Removed: hardcoded path. Use package imports or PYTHONPATH.
        from hive2_rag import retrieve, corrective_retrieve, hybrid_retrieve, cross_mission_retrieve, cross_mission_retrieve_enhanced, evaluate_rag

        # Translate query to English for better vector similarity (corpus is English)
        def _translate_query_to_en(q: str) -> str:
            try:
                import boto3, json as _json
                _bc = boto3.client("bedrock-runtime", region_name="eu-central-1")
                body = _json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 120,
                    "messages": [{"role": "user", "content":
                        f"Translate this research query to English in 1-2 sentences, keep all key terms:\n{q}"}]
                })
                resp = _bc.invoke_model(modelId="eu.anthropic.claude-haiku-4-5-20251001-v1:0", body=body)
                result = _json.loads(resp["body"].read())["content"][0]["text"].strip()
                # Strip any markdown headers Haiku may add (e.g. English Translation header)
                lines = result.splitlines()
                lines = [l for l in lines if not l.startswith("#") and l.strip()]
                return " ".join(lines).strip()
            except Exception:
                return q  # fallback: use original

        en_query = _translate_query_to_en(query) if any(ord(c) > 127 for c in query) else query
        if en_query != query:
            print(f"      Query (EN): {en_query[:80]}...")

        # ── C-RAG + Cross-Mission: corrective_retrieve z cross-mission routing ──
        _t0 = time.time()
        rag_context = cross_mission_retrieve_enhanced(
            en_query, args.mission_id, con,
            top_k=args.top_k,
            min_similarity=0.30,
            enable_rerank=True,
            enable_web_fallback=True,
        )
        _latency_ms = (time.time() - _t0) * 1000
        chunk_count = rag_context.count("[SOURCE")

        if chunk_count == 0 and en_query != query:
            # Try original (Polish) query
            print("      Cross-mission: EN query yielded 0 chunks — retrying with original query")
            _t0 = time.time()
            rag_context = cross_mission_retrieve_enhanced(
                query, args.mission_id, con,
                top_k=args.top_k,
                min_similarity=0.28,
                enable_rerank=True,
                enable_web_fallback=True,
            )
            _latency_ms = (time.time() - _t0) * 1000
            chunk_count = rag_context.count("[SOURCE")

        if chunk_count == 0:
            print("      Cross-mission: No relevant chunks — Queen will assess from agent outputs only")
        else:
            cross_count = rag_context.count("[CROSS:")
            primary_count = rag_context.count("[PRIMARY]")
            print(
                f"      RAG: {chunk_count} chunks "
                f"(primary:{primary_count} cross-mission:{cross_count})"
            )

        # ── RAG Evaluation ───────────────────────────────────────────────────
        rag_eval_metrics = evaluate_rag(
            en_query, rag_context, args.mission_id, con,
            top_k_requested=args.top_k,
            retrieval_latency_ms=_latency_ms,
        )
        if rag_eval_metrics.get("eval_flags"):
            print(f"      ⚠ RAG eval flags: {rag_eval_metrics['eval_flags']}")
        print(
            f"      eval: coverage={rag_eval_metrics['coverage_ratio']:.0%} "
            f"diversity={rag_eval_metrics['source_diversity']}dom "
            f"latency={rag_eval_metrics['retrieval_latency_ms']:.0f}ms"
        )

    except ImportError:
        print("      hive2_rag.py not available yet — skipping RAG")
        rag_context = ""
    except Exception as e:
        print(f"      RAG error: {e}")
        rag_context = ""


    # Step 2: Summarize bee results
    print("[2/6] Summarizing agent intelligence...")
    bee_summary = summarize_bee_results(args.mission_id, con)
    print(f"      {len(bee_summary)} chars of agent intelligence")

    # Step 3: Load cross-mission ecosystem context (HIVE 3.0)
    print("[3/6] Loading ecosystem context (HIVE 3.0)...")
    eco_context = ""
    try:
        import hive3_ecosystem as eco
        eco_ctx = eco.build_queen_ecosystem_context(query)
        if eco_ctx:
            eco_context = f"\n\n[ECOSYSTEM MEMORY — cross-mission intelligence]\n{eco_ctx}\n[/ECOSYSTEM MEMORY]"
            print(f"      Ecosystem: {len(eco_ctx)} chars of cross-mission memory")
    except Exception as e:
        print(f"      Ecosystem skip: {e}")

    # Step 3.5: GraphRAG — inject knowledge graph intelligence
    print("[3.5/6] GraphRAG knowledge graph context...")
    graph_intel_context = ""
    try:
        import importlib.util as _ilu
        _gs = _ilu.spec_from_file_location("hive2_graph_engine", None)  # Resolved via import shims
        _gm = _ilu.module_from_spec(_gs)
        _gs.loader.exec_module(_gm)

        # Semantic search — top claims semantically relevant to query
        sem_hits = _gm.semantic_search(query, top_k=15)
        # GraphRAG — hierarchical community context (C0+C1)
        grag = _gm.graphrag_search(query, top_k_c0=3, top_k_c1=5)

        lines = []
        # Community intelligence (C0 coarse → C1 fine)
        comms_used = grag.get("communities_used", [])
        if comms_used:
            lines.append("[GRAPH: Topic Intelligence]")
            for c in comms_used:
                lvl   = c.get("level", "C1")
                label = c.get("label", "")
                summ  = c.get("summary", "")
                if summ:
                    lines.append(f"  {'[Topic]' if lvl == 'C0' else '[Subtopic]'} {label}: {summ[:200]}")

        # High-score semantic claims not already in RAG
        if sem_hits:
            lines.append("\n[GRAPH: Cross-mission Claim Evidence]")
            for h in sem_hits[:12]:
                score = h.get("score", 0)
                if score >= 0.55:
                    lines.append(f"  [{score:.2f}] {h.get('claim_text','')[:140]}")

        if lines:
            graph_intel_context = (
                "\n\n━━━━ GRAPH INTELLIGENCE CONTEXT (cross-mission knowledge graph) ━━━━\n"
                + "\n".join(lines)
                + "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            )
            c0n = sum(1 for c in comms_used if c.get("level") == "C0")
            c1n = sum(1 for c in comms_used if c.get("level") == "C1")
            nhits = sum(1 for h in sem_hits if h.get("score", 0) >= 0.55)
            print(f"      GraphRAG: {c0n}×C0 topics, {c1n}×C1 subtopics, {nhits} semantic claims")
        else:
            print("      GraphRAG: no relevant graph context (graph may need rebuild)")
    except Exception as _ge:
        print(f"      GraphRAG skip: {_ge}")

    # Step 4: Queen call
    print("[4/6] Calling Queen (Bedrock Claude Opus)...")
    queen_output = call_queen(
        args.mission_id, query, rag_context + eco_context + graph_intel_context, bee_summary, bedrock,
        verbose=args.verbose
    )
    print("      Queen assessment received")

    # Step 5: Master Intelligence post-processing
    print("[5/6] Master Intelligence post-processing...")
    report = ""  # initialise before conditional to avoid NameError in Step 6
    if args.queen_only:
        print(json.dumps(queen_output, indent=2, ensure_ascii=False))
    else:
        report = master_intelligence_process(queen_output, args.mission_id, query, con)
        print("\n" + report)

        # ── Save report to file (MD + PDF) ──────────────────────────────────
        import pathlib as _pl
        _reports_dir = _pl.Path(os.environ.get("BEHIVE_REPORTS_DIR", os.path.expanduser("~/hive-reports")))
        _reports_dir.mkdir(exist_ok=True)
        _slug = re.sub(r'[^a-z0-9]+', '-', query.lower())[:60].strip('-')
        _md_path = _reports_dir / f"{args.mission_id}_{_slug}.md"
        _md_path.write_text(report, encoding="utf-8")
        print(f"\n📄 Markdown: {_md_path}")
        # PDF generation
        try:
            from fpdf import FPDF
            _font_reg = os.environ.get("BEHIVE_FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
            _font_bold = os.environ.get("BEHIVE_FONT_BOLD_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
            class _UniPDF(FPDF):
                pass
            _pdf = _UniPDF()
            _pdf.set_auto_page_break(auto=True, margin=15)
            _pdf.add_page()
            _pdf.add_font("DejaVu", fname=_font_reg)
            _pdf.add_font("DejaVu", "B", fname=_font_bold)
            _pdf.set_font("DejaVu", size=9)
            for _line in report.split("\n"):
                if _line.startswith("# "):
                    _pdf.set_font("DejaVu", "B", size=14)
                    _pdf.cell(0, 8, _line[2:], new_x="LMARGIN", new_y="NEXT")
                    _pdf.set_font("DejaVu", size=9)
                elif _line.startswith("## "):
                    _pdf.set_font("DejaVu", "B", size=11)
                    _pdf.cell(0, 7, _line[3:], new_x="LMARGIN", new_y="NEXT")
                    _pdf.set_font("DejaVu", size=9)
                else:
                    _pdf.multi_cell(0, 5, _line)
            _pdf_path = _reports_dir / f"{args.mission_id}_{_slug}.pdf"
            _pdf.output(str(_pdf_path))
            print(f"📄 PDF: {_pdf_path}")
        except Exception as _pdf_err:
            print(f"⚠️  PDF generation failed: {_pdf_err}")

        # Update DB with synthesis field
        try:
            con.execute(
                "UPDATE hive_missions SET synthesis=?, phase='done', status='done' WHERE id=?",
                [report[:50000], args.mission_id]
            )
            con.commit()
        except Exception as _db_err:
            print(f"⚠️  DB update failed: {_db_err}")

    # ── RAG Evaluation — context_precision z synthesis ───────────────────────
    if rag_eval_metrics and (report or queen_output):
        try:
            from hive2_rag import evaluate_rag, rag_eval_summary, verify_citations
            synthesis_text = report if report else json.dumps(queen_output, ensure_ascii=False)
            # Nadpisz ostatni rekord eval z context_precision
            evaluate_rag(
                rag_eval_metrics.get("query", query),
                rag_context if "rag_context" in dir() else "",
                args.mission_id,
                con,
                top_k_requested=rag_eval_metrics.get("top_k_requested", args.top_k),
                synthesis_text=synthesis_text,
                retrieval_latency_ms=rag_eval_metrics.get("retrieval_latency_ms", 0.0),
            )

            # ── Citation Verification — verbatim grounding ──────────────────
            cit_result = verify_citations(
                args.mission_id,
                rag_context if "rag_context" in dir() else "",
                con,
            )
            if cit_result["total_claims"] > 0:
                print(
                    f"\n      🔒 Citation verification: "
                    f"{cit_result['verified_count']}/{cit_result['total_claims']} claims grounded "
                    f"({cit_result['verification_rate']:.0%})"
                )
                if cit_result["unverified_count"] > 0:
                    print(f"         ⚠ Unverified claims ({cit_result['unverified_count']}):")
                    for uc in cit_result["unverified_claims"][:3]:
                        print(f"           - {uc[:90]}...")

            summary = rag_eval_summary(args.mission_id, con)
            if summary.get("eval_count", 0) > 0:
                print(
                    f"\n      📊 RAG eval summary (misja):\n"
                    f"         coverage={summary['avg_coverage']:.0%}  "
                    f"diversity={summary['avg_diversity']:.1f}dom  "
                    f"precision={summary['avg_precision']:.0%}  "
                    f"cross_hits={summary['cross_mission_hits']}  "
                    f"no_chunks={summary['no_chunks_count']}"
                )
                if summary.get("top_flags"):
                    print(f"         ⚠ top flags: {', '.join(summary['top_flags'])}")
        except Exception as _eval_exc:
            pass  # eval jest opcjonalny — nie blokuje misji

    # Step 6: Persist to ecosystem memory (HIVE 3.0)
    print("[6/6] Persisting to ecosystem + swarm memory...")
    try:
        import hive3_ecosystem as eco
        final_report = report if not args.queen_only else json.dumps(queen_output, ensure_ascii=False)
        # Use correct key 'hive_confidence_score' (not 'confidence')
        conf = float(queen_output.get("hive_confidence_score", 0.7)) if isinstance(queen_output, dict) else 0.7
        eco.save_mission_memory(
            mission_id=args.mission_id,
            topic=query,
            confidence=conf,
            key_findings=[],
            learned_patterns=[],
        )
        eco.evolve_ecosystem(args.mission_id, conf, {})
        print("      ✅ Ecosystem memory updated")
    except Exception as e:
        print(f"      Ecosystem store skip: {e}")
    
    # HIVE 3.0: Commit to persistent swarm memory (cross-mission intelligence)
    try:
        from hive3_swarm_memory import commit_mission_to_swarm_memory
        # Get entities from DB
        entities_raw = con.execute("""
            SELECT DISTINCT value, entity_type, confidence FROM hive_entities
            WHERE mission_id = ?
            ORDER BY confidence DESC
            LIMIT 50
        """, [args.mission_id]).fetchall()
        entities = [{"name": r[0], "type": r[1]} for r in entities_raw]
        
        # Get facts from DB
        facts_raw = con.execute("""
            SELECT claim, confidence_score FROM hive_facts
            WHERE mission_id = ?
            ORDER BY confidence_score DESC
            LIMIT 30
        """, [args.mission_id]).fetchall()
        facts = [{"claim": r[0], "confidence": r[1]} for r in facts_raw]
        
        swarm_stats = commit_mission_to_swarm_memory(
            mission_id=args.mission_id,
            topic=query,
            entities=entities,
            facts=facts,
            queen_report=queen_output if isinstance(queen_output, dict) else {},
        )
        print(f"      ✅ Swarm memory: {swarm_stats['entities_committed']} entities committed")
    except Exception as e:
        print(f"      Swarm memory skip: {e}")

    # ── P5: SEMANTIC FACT MEMORY — zapisz fakty z syntezy ──────────────────────
    try:
        from hive2_queen_fact_mem import QueenFactMemory
        synthesis_for_facts = report if report else (
            json.dumps(queen_output, ensure_ascii=False) if isinstance(queen_output, dict) else ""
        )
        if synthesis_for_facts and len(synthesis_for_facts) > 100:
            fact_mem = QueenFactMemory()
            n_facts = fact_mem.add(
                synthesis_for_facts,
                source_type="synthesis",
                mission_id=args.mission_id,
            )
            fact_mem.close()
            print(f"      ✅ Semantic memory: +{n_facts} facts indexed from synthesis")
    except Exception as e:
        print(f"      Semantic memory skip: {e}")
    # ─────────────────────────────────────────────────────────────────────────

    # PROMPT EVOLUTION — evaluate bee performance and propose mutations
    try:
        from hive3_prompt_evolution import post_mission_prompt_evolution        # Collect bee outputs from processing phase (stored in DB)
        bee_outputs = {}
        con2 = _hive_db.connect(read_only=True) if _pg_available else _hive_db.connect(read_only=False)
        bee_rows = con2.execute("""
            SELECT operation_name, output_json FROM hive_bee_results
            WHERE mission_id = ? AND output_json IS NOT NULL
            ORDER BY created_at DESC LIMIT 50
        """, [args.mission_id]).fetchall()
        con2.close()
        for op_name, result_text in bee_rows:
            # Map op_name to bee type (e.g., "IMMUNE_scan_001" → "ImmuneBee")
            tier = op_name.split("_")[0].upper() if op_name else ""
            if tier and tier not in bee_outputs:
                bee_outputs[tier] = result_text
        
        if bee_outputs:
            evo_result = post_mission_prompt_evolution(args.mission_id, bee_outputs)
            if evo_result["mutations_proposed"] > 0:
                print(f"      🧬 Prompt evolution: {evo_result['mutations_proposed']} mutations proposed")
            else:
                print(f"      🧬 Prompt evolution: {evo_result['bees_evaluated']} bees evaluated — all healthy")
    except Exception as e:
        print(f"      Prompt evolution skip: {e}")

    con.close()


if __name__ == "__main__":
    main()
