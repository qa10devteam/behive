#!/usr/bin/env python3
"""
HIVE 2.0 — hive2_synth.py  (QUEEN 3.0 — Multi-Layer Intelligence Synthesis)
The QUEEN: reads structured honey from PostgreSQL, produces final intelligence brief
via a 4-pass epistemological analysis pipeline.

Architecture:
  Pass 1 — EVIDENCE HIERARCHY    : categorizes data by certainty level
  Pass 2 — FALSIFICATION CHECK   : active adversarial challenge of top claims
  Pass 3 — MAIN SYNTHESIS        : full intelligence brief (Queen 3.0 prompt)
  Pass 4 — FUIR GENERATION       : Follow-Up Intelligence Requirements

Usage: python3 hive2_synth.py <mission_id>
"""

import sys
import os
import json
import time
import uuid
import traceback
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger("hive2_synth")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s — %(message)s")

DB_PATH = ""  # Legacy — PostgreSQL is primary
VENV_PYTHON = 'python3'
REPORTS_DIR = Path('hive-reports')


# ---------------------------------------------------------------------------
# LLM helper (BYOK via llm.py — works with any provider)
# ---------------------------------------------------------------------------

def _llm(prompt: str, system: str = "", max_tokens: int = 4096) -> str:
    """Call LLM via BYOK layer (litellm). Works with OpenAI, Anthropic, Bedrock, local, etc."""
    try:
        from behive.engine.llm import complete
        result = complete(prompt, stage="synth", system=system, max_tokens=max_tokens)
        if result:
            return result
    except Exception as e:
        log.warning(f"[SYNTH] BYOK LLM failed: {e}")
    
    raise RuntimeError("LLM call failed — check BEHIVE_MODEL or API key configuration")


# ---------------------------------------------------------------------------
# Multi-role synthesis pipeline (Editor → Reviewer → Writer)
# Uses SGLang/Qwen on port 8002 — same model as hive2_process bees
# ---------------------------------------------------------------------------

def _sglang_call(prompt: str, max_tokens: int = 4096) -> str:
    """LLM call for refinement passes — uses BYOK layer."""
    from behive.engine.llm import complete
    result = complete(prompt, stage="synth", max_tokens=min(max_tokens, 4096))
    if result:
        return result
    raise RuntimeError("LLM call failed for synth refinement")


def multi_role_synthesis(mission_id: str, initial_report: str, topic: str = "") -> str:
    """
    3-pass refinement pipeline run after Queen's initial 4-pass synthesis.
    Calls SGLang/Qwen (port 8002) — NOT Bedrock.

    Pass 1 — Editor:   restructure, cut redundancy, sharpen section headers
    Pass 2 — Reviewer: fact-check, flag unverified claims with [UNVERIFIED]
    Pass 3 — Writer:   polish prose, rewrite executive summary, final coherence

    Falls back to initial_report if any pass fails.
    """
    if len(initial_report) < 1000:
        return initial_report

    report = initial_report
    today = __import__('datetime').date.today().isoformat()

    # ── Pass 1: Editor ──────────────────────────────────────────────────────
    try:
        # All multi-role passes use Bedrock — Qwen/SGLang is fine-tuned for bee JSON extraction
        # and corrupts Markdown reports. _llm() routes to Bedrock Claude Haiku.
        edited = _llm(f"""Today: {today}. You are a senior research EDITOR.

TOPIC: {topic}

REPORT:
{report[:10000]}

Tasks:
1. Reorganise sections: context → findings → implications → recommendations
2. Cut duplicate sentences and redundant paragraphs
3. Sharpen section headers (specific, not generic like "Analysis")
4. Executive summary ≤ 150 words, 3 most critical findings only
5. Do NOT add new facts — restructure only

Return the full restructured report in Markdown.""", max_tokens=4096)
        if len(edited) > 500:
            report = edited
            log.info("[QUEEN] Multi-role: Editor pass ✓ (Bedrock)")
    except Exception as e:
        log.warning(f"[QUEEN] Multi-role: Editor skipped ({e})")

    # ── Pass 2: Reviewer ────────────────────────────────────────────────────
    try:
        reviewed = _llm(f"""Today: {today}. You are a rigorous research REVIEWER.

TOPIC: {topic}

REPORT:
{report[:10000]}

Tasks:
1. Prefix speculative/single-sourced claims with [UNVERIFIED]
2. Note internal contradictions with [CONTRADICTION: ...]
3. Verify statistics include source context (year, organisation)
4. Add "## Reviewer Notes" at end: top 3 reliability concerns
5. Do NOT remove content — only annotate

Return the full annotated report in Markdown.""", max_tokens=4096)
        if len(reviewed) > 500:
            report = reviewed
            log.info("[QUEEN] Multi-role: Reviewer pass ✓ (Bedrock)")
    except Exception as e:
        log.warning(f"[QUEEN] Multi-role: Reviewer skipped ({e})")

    # ── Pass 3: Writer ──────────────────────────────────────────────────────
    try:
        # Writer uses Bedrock (not Qwen/SGLang) — Qwen is fine-tuned for bee extraction JSON
        # and would corrupt the report format. Bedrock Haiku handles long Markdown cleanly.
        written = _llm(f"""Today: {today}. You are an elite research WRITER.

TOPIC: {topic}

REPORT:
{report[:10000]}

Tasks:
1. Rewrite executive summary: punchy, concrete, decision-ready (≤ 200 words)
2. Smooth paragraph transitions — one coherent document
3. Replace vague language ("various", "some", "many") with specific quantifiers
4. All section headers in title case
5. Add bold **Key Takeaway:** one-liner at the very top

Return the complete final report in Markdown.""", max_tokens=4096)
        if len(written) > 500:
            report = written
            log.info("[QUEEN] Multi-role: Writer pass ✓ (Bedrock)")
    except Exception as e:
        log.warning(f"[QUEEN] Multi-role: Writer skipped ({e})")

    return report


# ---------------------------------------------------------------------------
# Queen class
# ---------------------------------------------------------------------------

class Queen:
    """Queen synthesizer — multi-pass evidence hierarchy, narrative generation, and PDF output."""
    def __init__(self, mission_id: str):
        self.mission_id = mission_id
        self._import_pg()

    def _import_pg(self):
        # PostgreSQL backend (preferred — always available)
        try:
            from behive.engine.db import connect as _db_connect
            self._pg_available = (hive2_db.DB_BACKEND == "postgres")
            self._hive_db = hive2_db
        except ImportError:
            self._pg_available = False
        try:
            pass  # pg removed
            self.pg = None
        except Exception as e:
            log.debug(f"Suppressed in synth.py: {e}")
            self.pg = None  # Not fatal — PostgreSQL is primary

    # ── PostgreSQL connection helper ──────────────────────────────────────────────

    def _connect_ro(self):
        """Return a read-only connection (PostgreSQL)."""
        return self._db_connect(read_only=True)

    def _connect_rw(self):
        """Return a read-write connection (PostgreSQL)."""
        return self._db_connect(read_only=False)

    # ── Data loading ─────────────────────────────────────────────────────────

    def _load_honey(self) -> dict:
        """Load all structured honey from PostgreSQL for this mission."""
        con = self._connect_ro()
        mid = self.mission_id

        avail_cols = {
            row[0].lower()
            for row in con.execute("DESCRIBE hive_missions").fetchall()
        }
        status_col  = "status"     if "status"     in avail_cols else "phase"
        created_col = "created_at" if "created_at" in avail_cols else "started_at"

        try:
            mission_rows = con.execute(
                f"""SELECT id, topic, {status_col},
                          sources_found, sources_harvested,
                          total_words,
                          COALESCE(total_entities, 0),
                          COALESCE(total_facts, 0),
                          {created_col}
                   FROM hive_missions WHERE id = ?""",
                [mid]
            ).fetchall()
            if not mission_rows:
                raise ValueError(f"Mission '{mid}' not found in DB.")
            mr = mission_rows[0]
            mission = {
                "id": mr[0], "topic": mr[1], "status": mr[2],
                "sources_found": mr[3], "sources_harvested": mr[4],
                "total_words": mr[5], "total_entities": mr[6],
                "total_facts": mr[7], "created_at": str(mr[8]),
            }

            def safe_query(sql, params=None) -> list:
                try:
                    return con.execute(sql, params or [mid]).fetchall()
                except Exception as e:
                    log.debug(f"Suppressed in synth.py: {e}")
                    return []

            # ── Core mission honey ────────────────────────────────────────────

            if "hive_entities" in all_tables:
                top_entities = safe_query(
                    """SELECT value, entity_type, value, confidence, 1, context
                       FROM hive_entities
                       WHERE mission_id = ?
                       ORDER BY confidence DESC LIMIT 150"""
                )
            else:
                top_entities = []

            if "hive_facts" in all_tables:
                confirmed_facts = safe_query(
                    """SELECT value, unit, context, source_count, sources,
                              domains, confidence
                       FROM hive_facts
                       WHERE mission_id = ? AND confidence IN ('HIGH','CONFIRMED')
                       ORDER BY source_count DESC"""
                )
                all_facts = safe_query(
                    """SELECT value, unit, context, source_count, sources,
                              domains, confidence
                       FROM hive_facts
                       WHERE mission_id = ?
                       ORDER BY source_count DESC LIMIT 50"""
                )
                outliers = safe_query(
                    """SELECT value, unit, context, source_count, sources,
                              domains, confidence
                       FROM hive_facts
                       WHERE mission_id = ? AND is_outlier = TRUE"""
                )
            elif "hive_numeric_facts" in all_tables:
                def _map_legacy_fact(row):
                    val, unit, ctx, domain, url, conf = row
                    return (val, unit, ctx, 1, json.dumps([url]),
                            json.dumps([domain]), conf or "LOW")
                raw = safe_query(
                    "SELECT value, unit, context, source_domain, source_url, "
                    "confidence FROM hive_numeric_facts WHERE mission_id = ?"
                )
                all_facts = [_map_legacy_fact(r) for r in raw]
                confirmed_facts = [f for f in all_facts
                                   if f[6] in ("HIGH", "CONFIRMED")]
                outliers = []
            else:
                confirmed_facts = all_facts = outliers = []

            if "hive_clusters" in all_tables:
                # Use build_cluster_context from hive2_cluster — clusters + cross-mission atlas
                try:
                    import importlib.util as _clu2
                    _cs2 = _clu2.spec_from_file_location("hive2_cluster", "hive2_cluster.py")
                    _cm2 = _clu2.module_from_spec(_cs2)
                    _cs2.loader.exec_module(_cm2)
                    cluster_context_block = _cm2.build_cluster_context(self.mission_id)
                except Exception as e:
                    log.debug(f"Suppressed in synth.py: {e}")
                    cluster_context_block = None
                clusters_raw = safe_query(
                    """SELECT cluster_id, topic_label, keywords,
                              document_count, representative_text
                       FROM hive_clusters WHERE mission_id = ?
                       ORDER BY document_count DESC"""
                )
                # P2.5 — filtruj NOISE klastry (< 5 docs) przed synth
                clusters = [c for c in clusters_raw if (c[3] or 0) >= 5]
                noise_count = len(clusters_raw) - len(clusters)
                if noise_count > 0:
                    log.info(f"  Pominięto {noise_count} NOISE klastrów (< 5 docs) z {len(clusters_raw)} total")
            else:
                clusters = []

            if "hive_citations" in all_tables:
                citations = safe_query(
                    """SELECT claim, source_url, source_domain, source_title,
                              author, published_date, confidence,
                              cross_validated, supporting_sources
                       FROM hive_citations
                       WHERE mission_id = ?
                       ORDER BY confidence DESC LIMIT 100"""
                )
            elif "hive_claims" in all_tables:
                raw_claims = safe_query(
                    "SELECT claim, source_url, confidence "
                    "FROM hive_claims WHERE mission_id = ? "
                    "ORDER BY confidence DESC LIMIT 100"
                )
                citations = [
                    (row[0], row[1], "", "", "", "", row[2], False, 1)
                    for row in raw_claims
                ]
            else:
                citations = []

            if "hive_gaps" in all_tables:
                gaps = safe_query(
                    """SELECT gap_query, gap_type, priority
                       FROM hive_gaps
                       WHERE mission_id = ? AND resolved = FALSE
                       ORDER BY priority DESC"""
                )
            else:
                gaps = []

            # ── NEW: hive_claims (raw claim table with confidence scores) ──────
            raw_claims_data = []
            if "hive_claims" in all_tables:
                raw_claims_data = safe_query(
                    """SELECT claim, evidence, source_url, claim_type, confidence
                       FROM hive_claims
                       WHERE mission_id = ?
                       ORDER BY confidence DESC LIMIT 200"""
                )

            # ── NEW: QuantumBee hypothesis space ─────────────────────────────
            quantum_hypotheses = []
            if "hive_bee_results" in all_tables:
                quantum_hypotheses = safe_query(
                    """SELECT operation_id, operation_name, tier,
                              confidence, output_json, mission_id
                       FROM hive_bee_results
                       WHERE mission_id = ?
                         AND (operation_name LIKE '%Quantum%'
                              OR operation_name LIKE '%Superposition%'
                              OR operation_name LIKE '%Hypothesis%'
                              OR tier >= 12)
                       ORDER BY confidence DESC LIMIT 30""",
                    [mid]
                )

            # ── NEW: cross-mission confirmed priors ───────────────────────────
            cross_mission_priors = []
            if "hive3_priors" in all_tables:
                cross_mission_priors = con.execute(
                    """SELECT fact, domain, confidence,
                              times_confirmed, times_contradicted
                       FROM hive3_priors
                       ORDER BY confidence DESC, times_confirmed DESC
                       LIMIT 20"""
                ).fetchall()

            # ── NEW: dead hypotheses (graveyard) ─────────────────────────────
            dead_hypotheses = []
            if "hive3_graveyard" in all_tables:
                dead_hypotheses = con.execute(
                    """SELECT hypothesis, topic_domain, failure_reason,
                              confidence_at_death
                       FROM hive3_graveyard
                       ORDER BY confidence_at_death DESC LIMIT 20"""
                ).fetchall()

            return {
                "mission":              mission,
                "top_entities":         top_entities,
                "confirmed_facts":      confirmed_facts,
                "all_facts":            all_facts,
                "outliers":             outliers,
                "clusters":             clusters,
                "cluster_context_block": locals().get("cluster_context_block"),
                "citations":            citations,
                "gaps":                 gaps,
                # Intelligence layer additions
                "raw_claims":           raw_claims_data,
                "quantum_hypotheses":   quantum_hypotheses,
                "cross_mission_priors": cross_mission_priors,
                "dead_hypotheses":      dead_hypotheses,
            }
        finally:
            con.close()

    # ── Context building ─────────────────────────────────────────────────────

    def _build_honey_context(self, honey: dict) -> str:
        """
        Build structured text context from honey — NEVER raw article text.
        Includes all intelligence layers: priors, graveyard, QuantumBee hypotheses,
        hive_claims with deduplication.
        """
        lines = []
        m = honey["mission"]

        lines.append(f"TEMAT MISJI: {m['topic']}")
        lines.append(
            f"STATYSTYKI: {m['sources_found']} znalezionych źródeł | "
            f"{m['sources_harvested']} zebranych | "
            f"{m['total_words']} słów | "
            f"{m['total_entities']} encji | "
            f"{m['total_facts']} faktów"
        )
        lines.append("")

        # ── NUMERIC FACT DIGEST — TOP (depth booster, pre-inject before claims) ──
        # Numeric value extraction z raw_claims konficdencja >= 0.65
        # Built-in at top — Queen sees numbers BEFORE receiving claims
        import re as _re_top
        _num_top = _re_top.compile(
            r'\b(\d[\d\s]*[\d](?:[.,]\d+)?)\s*'
            r'(%|PLN|EUR|USD|GBP|zł|mln|tys|k|M|B|GB|TB|ms|s|min|h|'
            r'dni|miesięcy|lat|gwiazdek|users|klientów|pracowników|firm|'
            r'projektów|calls|req|rps|stars|downloads|installs|seats|licenses)\b',
            _re_top.IGNORECASE
        )
        _num_digest = []
        for rc in honey.get("raw_claims", [])[:300]:
            if len(rc) < 5:
                continue
            claim_text = str(rc[0] or "")
            conf = rc[4]
            try:
                conf_f = float(conf)
            except Exception as e:
                log.debug(f"Suppressed in synth.py: {e}")
                conf_f = 0.5
            if conf_f < 0.65:
                continue
            for v, u in _num_top.findall(claim_text)[:2]:
                entry = f"[{conf_f:.2f}] {v.strip()} {u} — {claim_text[:120]}"
                if entry not in _num_digest:
                    _num_digest.append(entry)
            if len(_num_digest) >= 25:
                break
        if _num_digest:
            lines.append("=== LICZBY KLUCZOWE — DIGEST (do cytowania w raporcie) ===")
            lines.extend(_num_digest)
            lines.append("")


        # Must appear at TOP so Queen sees them even when context is truncated
        lines.append("=== KLUCZOWE USTALENIA WYWIADOWCZE (hive_claims — top 40) ===")
        if honey.get("raw_claims"):
            seen_claims: set = set()
            added = 0
            for row in honey["raw_claims"]:
                if len(row) == 5:
                    claim, evidence, src_url, claim_type, conf = row
                elif len(row) == 3:
                    claim, src_url, conf = row
                    evidence, claim_type = "", "intelligence"
                else:
                    continue
                dedup_key = (claim or "")[:100].lower().strip()
                if dedup_key in seen_claims:
                    continue
                seen_claims.add(dedup_key)
                try:
                    conf_val = f"{float(conf):.2f}"
                except Exception as e:
                    log.debug(f"Suppressed in synth.py: {e}")
                    conf_val = str(conf)
                domain = ""
                if src_url:
                    try:
                        from urllib.parse import urlparse
                        domain = urlparse(src_url).netloc.replace("www.", "")
                    except Exception as e:
                        log.debug(f"Suppressed: {e}")
                evidence_snippet = (evidence or "")[:80]
                lines.append(
                    f"[{conf_val}] [{claim_type or '?'}] {claim[:200]}"
                    + (f" | evid: {evidence_snippet}" if evidence_snippet else "")
                    + (f" | src: {domain}" if domain else "")
                )
                added += 1
                if added >= 40:
                    break
        else:
            lines.append("Brak twierdzeń wywiadowczych — pipeline extraction nie zebrał claims.")
        lines.append("")
        lines.append("=== POTWIERDZONE FAKTY NUMERYCZNE ===")
        if honey["confirmed_facts"]:
            for row in honey["confirmed_facts"]:
                val, unit, ctx, src_count, sources_json, domains_json, conf = row
                domains = []
                try:
                    domains = json.loads(domains_json) if domains_json else []
                except (json.JSONDecodeError, ValueError, KeyError) as e:
                    log.debug(f"Parse error: {e}")
                domain_str = ", ".join(domains[:3]) if domains else "—"
                val_str  = f"{val:g}" if val is not None else "?"
                unit_str = f" {unit}" if unit else ""
                lines.append(
                    f"[{conf}] {val_str}{unit_str} — {ctx} "
                    f"(źródła: {domain_str}, n={src_count})"
                )
        else:
            # hive_facts empty (competitive intel missions store in hive_claims, not hive_facts).
            # Extract numerical values from high-confidence claims as a substitute.
            lines.append("⚠️  hive_facts pusta — ekstrakcja wartości numerycznych z hive_claims:")
            import re as _re
            _num_pattern = _re.compile(
                r'\b(\d[\d\s]*[\d](?:[.,]\d+)?)\s*'
                r'(%|PLN|EUR|USD|zł|mln|tys|k|M|B|GB|TB|ms|s|min|h|dni|miesięcy|lat|'
                r'gwiazdek|users|klientów|pracowników|firm|projektów|calls|req|rps|'
                r'stars|downloads|installs)\b',
                _re.IGNORECASE
            )
            _num_claims_added = 0
            for rc in honey.get("raw_claims", [])[:200]:
                if len(rc) < 5:
                    continue
                claim_text = str(rc[0] or "")
                src_url    = str(rc[2] or "")
                conf       = rc[4]
                try:
                    conf_f = float(conf)
                except Exception as e:
                    log.debug(f"Suppressed in synth.py: {e}")
                    conf_f = 0.5
                if conf_f < 0.6:
                    continue
                matches = _num_pattern.findall(claim_text)
                if matches:
                    try:
                        from urllib.parse import urlparse as _up
                        domain = _up(src_url).netloc.replace("www.", "")
                    except Exception as e:
                        log.debug(f"Suppressed in synth.py: {e}")
                        domain = src_url[:40]
                    for val_str, unit_str in matches[:2]:
                        lines.append(
                            f"[{conf_f:.2f}] {val_str.strip()} {unit_str} "
                            f"— {claim_text[:150]} ({domain})"
                        )
                        _num_claims_added += 1
                        if _num_claims_added >= 30:
                            break
                if _num_claims_added >= 30:
                    break
            if _num_claims_added == 0:
                lines.append("Brak wartości numerycznych w claims dla tej misji.")
        lines.append("")

        # ── All facts ────────────────────────────────────────────────────────
        if honey["all_facts"]:
            lines.append("=== WSZYSTKIE FAKTY NUMERYCZNE (TOP 50) ===")
            for row in honey["all_facts"]:
                val, unit, ctx, src_count, sources_json, domains_json, conf = row
                domains = []
                try:
                    domains = json.loads(domains_json) if domains_json else []
                except (json.JSONDecodeError, ValueError, KeyError) as e:
                    log.debug(f"Parse error: {e}")
                domain_str = ", ".join(domains[:2]) if domains else "—"
                val_str  = f"{val:g}" if val is not None else "?"
                unit_str = f" {unit}" if unit else ""
                lines.append(
                    f"[{conf}] {val_str}{unit_str} — {ctx} ({domain_str})"
                )
            lines.append("")

        # ── Key entities ─────────────────────────────────────────────────────
        lines.append("=== KLUCZOWE ENCJE ===")
        if honey["top_entities"]:
            by_type: dict = {}
            for row in honey["top_entities"]:
                text, etype, canonical, conf, src_count, context = row
                display = canonical or text
                by_type.setdefault(etype, []).append(f"{display}:{src_count}")
            for etype, items in by_type.items():
                lines.append(f"{etype}: " + " | ".join(items))
        else:
            lines.append("Brak encji.")
        lines.append("")

        # ── Topic clusters ───────────────────────────────────────────────────
        # Use cluster_context_block if available (contains clusters + cross-mission atlas)
        _ccb = honey.get("cluster_context_block")
        if _ccb:
            lines.append(_ccb)
        elif honey["clusters"]:
            lines.append("=== KLASTRY TEMATYCZNE ===")
            for i, row in enumerate(honey["clusters"], 1):
                cid, label, kw_json, doc_count, repr_text = row
                kws = []
                try:
                    kws = json.loads(kw_json) if kw_json else []
                except (json.JSONDecodeError, ValueError, KeyError) as e:
                    log.debug(f"Parse error: {e}")
                kw_str = ", ".join(kws[:6]) if kws else "—"
                lines.append(f"{i}. {label} ({doc_count} dokumentów): {kw_str}")
        else:
            lines.append("=== KLASTRY TEMATYCZNE ===")
            lines.append("Brak klastrów tematycznych — dokumenty nie były klastrowane.")
        lines.append("")

        # ── Numerical Falsification evidence ─────────────────────────────────
        try:
            import importlib.util as _flu2
            _fs2 = _flu2.spec_from_file_location("hive2_falsifier", "hive2_falsifier.py")
            _fm2 = _flu2.module_from_spec(_fs2)
            _fs2.loader.exec_module(_fm2)
            _falsif_block = _fm2.get_falsification_report(self.mission_id)
            if _falsif_block:
                lines.append(_falsif_block)
        except Exception as e:
            log.debug(f"Suppressed: {e}")

        # ── Outliers ─────────────────────────────────────────────────────────
        lines.append("=== WARTOŚCI ODSTAJĄCE (OUTLIERS) ===")
        if honey["outliers"]:
            for row in honey["outliers"]:
                val, unit, ctx, src_count, _, domains_json, conf = row
                domains = []
                try:
                    domains = json.loads(domains_json) if domains_json else []
                except (json.JSONDecodeError, ValueError, KeyError) as e:
                    log.debug(f"Parse error: {e}")
                domain_str = ", ".join(domains[:2]) if domains else "?"
                val_str  = f"{val:g}" if val is not None else "?"
                unit_str = f" {unit}" if unit else ""
                lines.append(
                    f"[OUTLIER] {val_str}{unit_str} — {ctx} "
                    f"(tylko {src_count} źródło, {domain_str})"
                )
        else:
            lines.append("Brak wartości odstających.")
        lines.append("")

        # ── Citations ────────────────────────────────────────────────────────
        lines.append("=== CYTOWANIA (TOP 30) ===")
        if honey["citations"]:
            for row in honey["citations"][:30]:
                claim, url, domain, title, author, pub_date, conf, cross_val, sup_src = row
                year    = pub_date[:4] if pub_date and len(pub_date) >= 4 else "?"
                cv_mark = " ✓" if cross_val else ""
                try:
                    conf_val = f"{float(conf):.2f}"
                except Exception as e:
                    log.debug(f"Suppressed in synth.py: {e}")
                    conf_val = str(conf)
                lines.append(
                    f"• {claim} [{domain}, {year}]{cv_mark} "
                    f"(pewność: {conf_val}, wspierające źródła: {sup_src})"
                )
        else:
            lines.append("Brak cytowań.")
        lines.append("")

        # ── Research gaps ────────────────────────────────────────────────────
        # Quality warnings (priority=10, type=quality_warning) go FIRST
        quality_warnings = [row for row in (honey["gaps"] or [])
                            if row[1] == 'quality_warning']
        regular_gaps     = [row for row in (honey["gaps"] or [])
                            if row[1] != 'quality_warning']

        if quality_warnings:
            lines.append("⚠️  === OSTRZEŻENIA JAKOŚCI DANYCH ===")
            for row in quality_warnings:
                gap_query, gap_type, priority = row
                lines.append(gap_query)
            lines.append("")

        lines.append("=== LUKI BADAWCZE ===")
        if regular_gaps:
            for i, row in enumerate(regular_gaps, 1):
                gap_query, gap_type, priority = row
                lines.append(f"{i}. [{gap_type or 'unknown'}, prio={priority}] {gap_query}")
        else:
            lines.append("Brak zidentyfikowanych luk badawczych.")
        lines.append("")

        # ════════════════════════════════════════════════════════════════════
        # NEW INTELLIGENCE LAYERS
        # ════════════════════════════════════════════════════════════════════

        # ── TOP CLAIMS (hive_claims with deduplication) ───────────────────
        lines.append("=== TOP CLAIMS (hive_claims — deduplicated, top 30) ===")
        if honey.get("raw_claims"):
            seen_claims: set = set()
            added = 0
            for row in honey["raw_claims"]:
                claim, evidence, src_url, claim_type, conf = row
                # Deduplicate by first 100 chars of claim
                dedup_key = (claim or "")[:100].lower().strip()
                if dedup_key in seen_claims:
                    continue
                seen_claims.add(dedup_key)
                try:
                    conf_val = f"{float(conf):.2f}"
                except Exception as e:
                    log.debug(f"Suppressed in synth.py: {e}")
                    conf_val = str(conf)
                domain = ""
                if src_url:
                    try:
                        from urllib.parse import urlparse
                        domain = urlparse(src_url).netloc.replace("www.", "")
                    except Exception as e:
                        log.debug(f"Suppressed: {e}")
                evidence_snippet = (evidence or "")[:80]
                lines.append(
                    f"[{conf_val}] [{claim_type or '?'}] {claim[:150]}"
                    + (f" | evid: {evidence_snippet}" if evidence_snippet else "")
                    + (f" | src: {domain}" if domain else "")
                )
                added += 1
                if added >= 60:
                    break
        else:
            lines.append("Brak danych z hive_claims.")
        lines.append("")

        # ── HYPOTHESIS SPACE (QuantumBee — tier ≥ 12) ────────────────────
        lines.append("=== HYPOTHESIS SPACE (QuantumBee / tier≥12) ===")
        if honey.get("quantum_hypotheses"):
            hypothesis_count = 0
            for row in honey["quantum_hypotheses"]:
                op_id, op_name, tier, conf, output_json_raw, bee_mission = row
                try:
                    oj = json.loads(output_json_raw) if output_json_raw else {}
                except Exception as e:
                    log.debug(f"Suppressed in synth.py: {e}")
                    oj = {}

                # Extract hypotheses from various output_json shapes
                extracted = []
                if "hypotheses" in oj and isinstance(oj["hypotheses"], list):
                    for h in oj["hypotheses"][:3]:
                        if isinstance(h, dict):
                            hyp_text = h.get("hypothesis", h.get("text", ""))
                            prob     = h.get("probability", h.get("confidence", conf))
                            extracted.append((hyp_text, prob))
                        elif isinstance(h, str):
                            extracted.append((h, conf))
                elif "resonances" in oj and isinstance(oj["resonances"], list):
                    for r_item in oj["resonances"][:2]:
                        if isinstance(r_item, dict):
                            r_text = r_item.get("pattern",
                                       r_item.get("description", str(r_item)[:120]))
                            extracted.append((r_text, conf))
                elif "key_finding" in oj:
                    extracted.append((str(oj["key_finding"])[:200], conf))

                for hyp_text, prob in extracted:
                    if not hyp_text:
                        continue
                    try:
                        prob_val = f"P={float(prob):.2f}"
                    except Exception as e:
                        log.debug(f"Suppressed in synth.py: {e}")
                        prob_val = f"P=?"
                    lines.append(
                        f"{prob_val} — {str(hyp_text)[:200]} "
                        f"[{op_name}, tier={tier}, mission={bee_mission}]"
                    )
                    hypothesis_count += 1
                    if hypothesis_count >= 15:
                        break
                if hypothesis_count >= 15:
                    break
            if hypothesis_count == 0:
                lines.append("Brak wyekstrahowanych hipotez z QuantumBee.")
        else:
            lines.append("Brak danych QuantumBee (tier≥12) dla tej misji.")
        lines.append("")

        # ── CONFIRMED PRIORS (cross-mission) ─────────────────────────────
        lines.append("=== CONFIRMED PRIORS (cross-mission) ===")
        if honey.get("cross_mission_priors"):
            for row in honey["cross_mission_priors"]:
                fact, domain, confidence, times_confirmed, times_contradicted = row
                try:
                    conf_val = f"{float(confidence):.2f}"
                except Exception as e:
                    log.debug(f"Suppressed in synth.py: {e}")
                    conf_val = str(confidence)
                contra_note = (
                    f" ⚠️ CONTRADICTED {times_contradicted}×"
                    if times_contradicted and times_contradicted > 0 else ""
                )
                lines.append(
                    f"[conf={conf_val}, n={times_confirmed}✓{contra_note}] "
                    f"[{domain}] {fact}"
                )
        else:
            lines.append("Brak potwierdzonych priorów z poprzednich misji.")
        lines.append("")

        # ── DEAD HYPOTHESES (Graveyard) ───────────────────────────────────
        lines.append("=== DEAD HYPOTHESES (Graveyard — czego NIE jest prawdą) ===")
        if honey.get("dead_hypotheses"):
            for row in honey["dead_hypotheses"]:
                hypothesis, topic_domain, failure_reason, conf_at_death = row
                try:
                    conf_val = f"{float(conf_at_death):.2f}"
                except Exception as e:
                    log.debug(f"Suppressed in synth.py: {e}")
                    conf_val = "?"
                lines.append(
                    f"✗ [{topic_domain or '?'}] {hypothesis} "
                    f"→ OBALONO: {failure_reason} "
                    f"(conf_at_death={conf_val})"
                )
        else:
            lines.append("Brak obalonych hipotez w Graveyard.")

        return "\n".join(lines)

    # ── 4-Pass Intelligence Synthesis ────────────────────────────────────────

    def synthesize(self, honey: dict) -> dict:
        """
        4-pass epistemological analysis pipeline:
          Pass 1 — EVIDENCE HIERARCHY
          Pass 2 — FALSIFICATION CHECK
          Pass 3 — MAIN SYNTHESIS (Queen 3.0)
          Pass 4 — FUIR GENERATION
        """
        topic         = honey["mission"]["topic"]
        honey_context = self._build_honey_context(honey)

        # Queen 3.0 system prompt — epistemological discipline
        system_queen = (
            "Jesteś Queen 3.0 — starszym analitykiem wywiadu strategicznego w elitarnym think tanku. "
            "Twoje analizy są wzorcem epistemologicznej dyscypliny. "
            "ZAWSZE stosujesz hierarchię pewności: "
            "CONFIRMED (wiele źródeł, cross-validated) | "
            "PROBABLE (1-2 źródła, wysoka relevancja) | "
            "SPECULATIVE (jedno źródło, niska autorytatywność) | "
            "CONTRADICTED (konflikt między źródłami) | "
            "DEAD (obalona hipoteza z Graveyard). "
            "Każde twierdzenie faktyczne wymaga: (1) znacznika pewności, (2) cytowania inline [domena, rok]. "
            "Aktywnie szukasz falsyfikacji — nie potwierdzasz, lecz starasz się obalić każdy wniosek. "
            "Piszesz po polsku. Cytowania i kody FUIR są w języku angielskim."
        )

        # ── Graph context — hive2_graph_engine (P4 RAG inject) ──────────────
        graph_context = ""
        try:
            import importlib.util as _ilu
            _gs = _ilu.spec_from_file_location(
                "hive2_graph_engine", "hive2_graph_engine.py"
            )
            _gm = _ilu.module_from_spec(_gs)
            _gs.loader.exec_module(_gm)

            # Semantic claims relevant to this mission's topic
            sem_hits = _gm.semantic_search(topic, top_k=12)
            # Hierarchical community context
            grag = _gm.graphrag_search(topic, top_k_c0=2, top_k_c1=4)

            lines = []
            comms = grag.get("communities_used", [])
            if comms:
                lines.append("[KNOWLEDGE GRAPH — Topic Intelligence]")
                for c in comms:
                    lvl   = c.get("level", "C1")
                    label = c.get("label", "")
                    summ  = c.get("summary", "")
                    if summ:
                        prefix = "[Topic]" if lvl == "C0" else "[Subtopic]"
                        lines.append(f"  {prefix} {label}: {summ[:200]}")

            high_hits = [h for h in sem_hits if h.get("score", 0) >= 0.55]
            if high_hits:
                lines.append("\n[KNOWLEDGE GRAPH — Cross-mission Evidence]")
                for h in high_hits[:10]:
                    lines.append(f"  [{h['score']:.2f}] {h.get('claim_text','')[:130]}")

            if lines:
                graph_context = "\n".join(lines)
        except Exception as e:
            log.debug(f"Suppressed: {e}")

        # ════════════════════════════════════════════════════════════════════
        # PASS 1 — EVIDENCE HIERARCHY
        # ════════════════════════════════════════════════════════════════════
        pass1_prompt = f"""Jesteś analitykiem wywiadu. Dokonujesz kategoryzacji materiału dowodowego.

TEMAT: {topic}

Na podstawie poniższych danych, skategoryzuj dostępne dowody i twierdzenia w 5 warstw pewności:

**CONFIRMED** — wielokrotnie zweryfikowane, cross-validated, priors z poprzednich misji
**PROBABLE** — 1-2 źródła, wysoka relevancja, brak sprzeczności
**SPECULATIVE** — pojedyncze źródło, niska autorytatywność lub nieweryfikowalne
**CONTRADICTED** — istnieje wyraźny konflikt między źródłami
**DEAD** — hipoteza obalona (z Graveyard)

Dla każdej kategorii podaj 3-6 konkretnych przykładów z danych.
Format odpowiedzi:

## CONFIRMED
- [przykład z cytatem domeny]

## PROBABLE
- [przykład]

## SPECULATIVE
- [przykład]

## CONTRADICTED
- [opis konfliktu: źródło A twierdzi X, źródło B twierdzi Y]

## DEAD
- [obalona hipoteza i powód]

===== INTELLIGENCE DATA =====
{honey_context[:12000]}
==========================="""

        log.info("[QUEEN] Pass 1 — EVIDENCE HIERARCHY (25%)…")
        _t0 = time.time()
        pass1_result = _llm(pass1_prompt, system=system_queen, max_tokens=4096)
        log.info(f"[QUEEN] Pass 1 done in {time.time()-_t0:.1f}s "
              f"(~{len(pass1_result)} chars)")

        # ════════════════════════════════════════════════════════════════════
        # PASS 2 — FALSIFICATION CHECK
        # ════════════════════════════════════════════════════════════════════
        pass2_prompt = f"""You are a counterintelligence analyst. Your role is active falsification of conclusions.

TEMAT: {topic}

Poniżej podano hierarchię pewności (Pass 1) oraz surowe dane.
Zidentyfikuj 5 kluczowych twierdzeń z kategorii CONFIRMED lub PROBABLE.

Dla każdego twierdzenia odpowiedz:
1. **Twierdzenie**: [treść]
2. **Co musiałoby być prawdą, żeby to twierdzenie było BŁĘDNE?** (counterfactual condition)
3. **Czy mamy evidence contra?** (TAK/NIE + opis)
4. **Ocena po falsyfikacji**: UTRZYMANE / OSŁABIONE / ODRZUCONE
5. **Rewizja pewności**: [stary poziom] → [nowy poziom]

===== HIERARCHIA PEWNOŚCI (Pass 1) =====
{pass1_result[:2000]}

===== SUROWE DANE =====
{honey_context[:8000]}
==========================="""

        log.info("[QUEEN] Pass 2 — FALSIFICATION CHECK (50%)…")
        _t0 = time.time()
        pass2_result = _llm(pass2_prompt, system=system_queen, max_tokens=3072)
        log.info(f"[QUEEN] Pass 2 done in {time.time()-_t0:.1f}s "
              f"(~{len(pass2_result)} chars)")

        # ════════════════════════════════════════════════════════════════════
        # PASS 3 — MAIN SYNTHESIS (Queen 3.0) — STORM section-aware pattern
        # ════════════════════════════════════════════════════════════════════
        # STORM Pattern: pre-route claims to sections before synthesis.
        # Each report section gets its own filtered claim set → Queen writes
        # sections in order with targeted evidence → less hallucination, more depth.
        # ════════════════════════════════════════════════════════════════════
        graph_section = (
            f"\n===== KNOWLEDGE GRAPH CONTEXT (cross-mission intelligence) =====\n"
            f"{graph_context}\n"
            if graph_context else ""
        )

        # Build section-specific claim buckets from honey raw_claims
        _section_claims: dict[str, list[str]] = {
            "pricing":    [],
            "customer":   [],
            "market":     [],
            "feature":    [],
            "review":     [],
            "technology": [],
            "general":    [],
        }
        for rc in honey.get("raw_claims", [])[:200]:
            # rc is a tuple: (claim, evidence, source_url, claim_type, confidence)
            if isinstance(rc, dict):
                ct  = str(rc.get("claim_type") or rc.get("type") or "general").lower()
                claim_txt = str(rc.get("claim") or "")[:300]
                conf = float(rc.get("confidence") or 0.7)
                src  = str(rc.get("source_url") or "")
            else:
                # tuple shape: (claim, evidence, source_url, claim_type, confidence)
                if len(rc) >= 5:
                    claim_txt, _ev, src, ct, conf = str(rc[0] or "")[:300], rc[1], str(rc[2] or ""), str(rc[3] or "general"), 0.7
                    try: conf = float(rc[4])
                    except Exception as e:
                        log.debug(f"Suppressed in synth.py: {e}")
                        conf = 0.7
                elif len(rc) >= 3:
                    claim_txt, src, ct, conf = str(rc[0] or "")[:300], str(rc[1] or ""), "general", 0.7
                    try: conf = float(rc[2])
                    except Exception as e:
                        log.debug(f"Suppressed in synth.py: {e}")
                        conf = 0.7
                else:
                    continue
                ct = str(ct or "general").lower()
            bucket = ct if ct in _section_claims else "general"
            domain = src.split("/")[2] if src.startswith("http") and "/" in src[8:] else src[:40]
            _section_claims[bucket].append(f"  [{conf:.2f}] {claim_txt} [{domain}]")

        def _fmt_section_claims(keys: list[str], cap: int = 15) -> str:
            lines = []
            for k in keys:
                lines.extend(_section_claims.get(k, [])[:cap])
            return "\n".join(lines[:cap * len(keys)]) if lines else "  (brak danych)"

        section_pricing   = _fmt_section_claims(["pricing"])
        section_customers = _fmt_section_claims(["customer"])
        section_market    = _fmt_section_claims(["market"])
        section_features  = _fmt_section_claims(["feature", "technology"])
        section_reviews   = _fmt_section_claims(["review"])
        section_general   = _fmt_section_claims(["general"], cap=10)

        pass3_prompt = f"""Na podstawie poniższych ustrukturyzowanych danych wywiadowczych dotyczących tematu:
**{topic}**

Oraz po przeprowadzeniu hierarchizacji dowodów i analizy falsyfikacyjnej, napisz pełny raport wywiadowczy Queen 3.0.

**WYMOGI OBLIGATORYJNE:**
- Każde twierdzenie MUSI mieć znacznik pewności: [CONFIRMED], [PROBABLE], [SPECULATIVE], [CONTRADICTED] lub [DEAD]
- Każde twierdzenie MUSI mieć cytowanie inline: [domena.com, ROK]
- Sekcja SPRZECZNOŚCI jest OBOWIĄZKOWA (minimum 2 sprzeczności)
- Hipotezy QuantumBee muszą być zintegrowane jako sekcja HYPOTHESIS SPACE
- Obalonych hipotezy z Graveyard muszą pojawić się na końcu jako DEAD HYPOTHESES

**STORM SECTION-AWARE SYNTHESIS — każda sekcja ma dedykowane dowody poniżej:**

1. **STRESZCZENIE WYKONAWCZE** (3-4 zdania, bez znaczników pewności)

2. **KLUCZOWE USTALENIA** (5-7 ustaleń, każde z: znacznik pewności + cytowanie)
   Format: [CONFIRMED/PROBABLE/SPECULATIVE] Treść ustalenia [domena, rok].

3. **TABELA DANYCH ILOŚCIOWYCH** (Markdown):
   | Metryka | Wartość | Źródło | Pewność |
   |---------|---------|--------|---------|
   [Użyj danych z sekcji MARKET CLAIMS poniżej]

4. **CENY I WARUNKI KOMERCYJNE** (cenniki, tier-y, warunki umów, ROI)
   [Użyj danych z sekcji PRICING CLAIMS poniżej]

5. **KLIENCI I ADOPCJA** (kto używa jakich narzędzi, case studies, job postings jako sygnał)
   [Użyj danych z sekcji CUSTOMER CLAIMS poniżej]

6. **FUNKCJONALNOŚCI I STACK TECHNOLOGICZNY** (porównanie feature, integracje, tech stack)
   [Użyj danych z sekcji FEATURE/TECH CLAIMS poniżej]

7. **OPINIE I RECENZJE** (Clutch, G2, Capterra, fora — sentyment, NPS, skargi)
   [Użyj danych z sekcji REVIEW CLAIMS poniżej]

8. **ANALIZA TRENDÓW** (co rośnie, co spada, dynamika zmian)

9. **HYPOTHESIS SPACE** (hipotezy z QuantumBee z prawdopodobieństwami)
   Format: P=0.XX — Hipoteza — [status po falsyfikacji]

10. **SPRZECZNOŚCI I ANOMALIE** (obowiązkowe — konflikty między źródłami)
    Format: KONFLIKT-N: Źródło A twierdzi X [domena], Źródło B twierdzi Y [domena] — Ocena: …

11. **REKOMENDACJE** (3-5 konkretnych, mierzalnych zaleceń)

12. **DEAD HYPOTHESES** (co zostało obalone)
{graph_section}
===== HIERARCHIA PEWNOŚCI (Pass 1) =====
{pass1_result[:1500]}

===== ANALIZA FALSYFIKACYJNA (Pass 2) =====
{pass2_result[:1500]}

===== PRICING CLAIMS (sekcja 4) =====
{section_pricing}

===== CUSTOMER CLAIMS (sekcja 5) =====
{section_customers}

===== FEATURE/TECH CLAIMS (sekcja 6) =====
{section_features}

===== REVIEW CLAIMS (sekcja 7) =====
{section_reviews}

===== MARKET CLAIMS (tabela danych) =====
{section_market}

===== POZOSTAŁE DOWODY (general) =====
{section_general}

===== PEŁNE INTELLIGENCE DATA =====
{honey_context}
===========================

Pisz w języku polskim. Każde stwierdzenie faktyczne musi mieć cytowanie inline.

OBOWIĄZKOWE WYMOGI GŁĘBOKOŚCI — bez spełnienia raportu jest niekompletny:
• Minimum 8 konkretnych liczb/cen/dat/procent w całym raporcie (np. "$49/mies.", "wzrost 34% YoY", "2024-Q3", "3 500 klientów")
• Minimum 3 cytowania nazw firm z konkretnymi danymi (nie ogólniki)
• Minimum 2 porównania numeryczne (X vs Y: $A vs $B)
• Jeśli brakuje liczb — napisz explicite: "VOID: brak danych ilościowych w tej sekcji" zamiast pisać ogólniki"""

        log.info("[QUEEN] Pass 3 — STORM SECTION-AWARE SYNTHESIS (75%)…")
        _t0 = time.time()
        pass3_result = _llm(pass3_prompt, system=system_queen, max_tokens=8192)
        log.info(f"[QUEEN] Pass 3 done in {time.time()-_t0:.1f}s "
              f"(~{len(pass3_result)} chars)")

        # ════════════════════════════════════════════════════════════════════
        # PASS 4 — FUIR GENERATION
        # ════════════════════════════════════════════════════════════════════
        pass4_prompt = f"""Jesteś Intelligence Requirements Officer. Generujesz formalne wymagania wywiadowcze.

TEMAT: {topic}

Na podstawie przeprowadzonej analizy (hierarchia dowodów, falsyfikacja, synteza),
wygeneruj sformalizowane Follow-Up Intelligence Requirements (FUIR) w następującym formacie:

**Format każdego wpisu:**
[KOD] [TYP] Treść — wymagane źródło: [opis źródła] — priorytet: HIGH/MEDIUM/LOW

**Kody typów:**
- IV-N   : [VOID] — brak danych, pilna luka informacyjna
- CI-N   : [CONFIRMED] — potwierdzone, nie wymaga dalszej weryfikacji
- FUIR-N : [REQUIRED] — wymagana weryfikacja w następnej misji
- PRED-N : [PREDICTION] — predykcja do weryfikacji (format: X wzrośnie/spadnie o Y% do Z roku)
- CONTRA-N: [CONTRADICTION] — konflikt wymagający rozstrzygnięcia

**Przykłady:**
IV-1: [VOID] Brak danych o udziale rynkowym — wymagane źródło: raport branżowy lub SEC filing — priorytet: HIGH
CI-2: [CONFIRMED] Wzrost przychodów o 23% YoY potwierdzone przez 3 źródła — nie wymaga weryfikacji — priorytet: LOW
FUIR-3: [REQUIRED] Check powiązania personalne między X a Y w kontekście Z — wymagane źródło: rejestry korporacyjne — priorytet: HIGH
PRED-4: [PREDICTION] Rynek X osiągnie wartość Y mld USD do 2026 roku — confidence: 0.65 — weryfikacja: 2026-Q1
CONTRA-5: [CONTRADICTION] Konflikt danych między źródłem A (X=10%) a B (X=35%) — wymagane źródło: trzecie niezależne — priorytet: HIGH

Wygeneruj 8-12 pozycji FUIR obejmujących: luki, potwierdzenia, predykcje, sprzeczności.
Każda pozycja na osobnej linii. Poprzedź je linią nagłówkową: "=FUIR_START="

===== SYNTEZA (Pass 3 — fragment) =====
{pass3_result[:3000]}

===== HIERARCHIA DOWODÓW (Pass 1 — fragment) =====
{pass1_result[:1500]}

===== INTELLIGENCE DATA =====
{honey_context[:4000]}
==========================="""

        log.info("[QUEEN] Pass 4 — FUIR GENERATION (90%)…")
        _t0 = time.time()
        pass4_result = _llm(pass4_prompt, system=system_queen, max_tokens=4096)
        log.info(f"[QUEEN] Pass 4 done in {time.time()-_t0:.1f}s "
              f"(~{len(pass4_result)} chars)")

        # Parse FUIR entries
        fuir_entries = self._parse_fuir(pass4_result)

        # Parse follow-up queries (legacy format for compatibility)
        follow_up_queries = [
            e["text"] for e in fuir_entries
            if e.get("type") in ("VOID", "REQUIRED", "CONTRADICTION")
        ]

        # Build confidence score from evidence hierarchy (simple heuristic)
        confirmed_count  = pass1_result.upper().count("CONFIRMED")
        probable_count   = pass1_result.upper().count("PROBABLE")
        speculative_count = pass1_result.upper().count("SPECULATIVE")
        total_signals    = confirmed_count + probable_count + speculative_count + 1
        confidence_raw   = (confirmed_count * 1.0 + probable_count * 0.6 +
                            speculative_count * 0.2) / (total_signals * 1.0)
        confidence_raw   = min(max(confidence_raw, 0.05), 0.95)

        return {
            "brief":              pass3_result,
            "evidence_hierarchy": pass1_result,
            "falsification":      pass2_result,
            "fuir_raw":           pass4_result,
            "fuir_entries":       fuir_entries,
            "key_findings":       [],
            "data_table":         [],
            "trends":             "",
            "contradictions":     "",
            "follow_up_queries":  follow_up_queries,
            "confidence_raw":     confidence_raw,
            "metadata": {
                "topic":        topic,
                "mission_id":   self.mission_id,
                "synthesized_at": datetime.utcnow().isoformat(),
                "sources_used": honey["mission"]["sources_harvested"],
                "facts_used":   len(honey["all_facts"]) + len(honey.get("raw_claims", [])),  # counts facts + claims
                "entities_used": len(honey["top_entities"]),
                "claims_used":  len(honey.get("raw_claims", [])),
                "hypotheses_used": len(honey.get("quantum_hypotheses", [])),
                "priors_used":  len(honey.get("cross_mission_priors", [])),
                "graveyard_used": len(honey.get("dead_hypotheses", [])),
                "passes":       4,
            },
        }

    # ── FUIR Parser ───────────────────────────────────────────────────────────

    def _parse_fuir(self, raw_text: str) -> list:
        """
        Parse FUIR entries from Pass 4 output.
        Returns list of dicts: {code, type, text, source, priority, prediction, confidence}.
        """
        import re
        entries = []

        # Find the FUIR block (after =FUIR_START= marker or just scan all lines)
        fuir_section = raw_text
        if "=FUIR_START=" in raw_text:
            fuir_section = raw_text.split("=FUIR_START=", 1)[1]

        # Pattern: CODE-N: [TYPE] text — ... — priorytet: HIGH/MEDIUM/LOW
        pattern = re.compile(
            r'^(IV|CI|FUIR|PRED|CONTRA)-(\d+)\s*:\s*'
            r'\[([A-Z_]+)\]\s*(.+?)$',
            re.MULTILINE | re.IGNORECASE
        )

        for match in pattern.finditer(fuir_section):
            prefix, num, fuir_type, rest = match.groups()
            rest = rest.strip()
            original_rest = rest  # keep for multi-field search

            # Extract priority (search on original_rest)
            priority = "MEDIUM"
            prio_match = re.search(r'priorytet\s*:\s*(HIGH|MEDIUM|LOW)',
                                   original_rest, re.IGNORECASE)
            if prio_match:
                priority = prio_match.group(1).upper()

            # Extract required source (search on original_rest)
            source = ""
            src_match = re.search(r'wymagane\s+źródło\s*:\s*([^—]+)',
                                  original_rest, re.IGNORECASE)
            if src_match:
                source = src_match.group(1).strip()
                # strip trailing priority clause if captured
                source = re.split(r'\s*—\s*priorytet', source,
                                  flags=re.IGNORECASE)[0].strip()

            # Extract prediction confidence (search on original_rest)
            pred_conf = None
            conf_match = re.search(r'confidence\s*:\s*([\d.]+)',
                                   original_rest, re.IGNORECASE)
            if conf_match:
                try:
                    pred_conf = float(conf_match.group(1))
                except Exception as e:
                    log.debug(f"Suppressed: {e}")

            # Extract verification date — search on original_rest
            verify_date = None
            vd_match = re.search(r'weryfikacja\s*:\s*(\S+)',
                                 original_rest, re.IGNORECASE)
            if vd_match:
                verify_date = vd_match.group(1).rstrip(".,;")

            # Build clean text: strip all metadata clauses from rest
            text = original_rest
            for rm_pattern in [
                r'\s*—\s*wymagane\s+źródło\s*:[^—]+',
                r'\s*—\s*priorytet\s*:\s*(HIGH|MEDIUM|LOW)',
                r'\s*—\s*confidence\s*:\s*[\d.]+',
                r'\s*—\s*weryfikacja\s*:\s*\S+',
                r'\s*—\s*nie\s+wymaga\s+weryfikacji',
            ]:
                text = re.sub(rm_pattern, '', text, flags=re.IGNORECASE)
            rest = text.strip(" —").strip()

            entries.append({
                "code":        f"{prefix.upper()}-{num}",
                "type":        fuir_type.upper(),
                "text":        rest.strip(" —").strip(),
                "source":      source,
                "priority":    priority,
                "pred_conf":   pred_conf,
                "verify_date": verify_date,
            })

        # Fallback: grab lines with keyword markers if regex found nothing
        if not entries:
            for line in fuir_section.splitlines():
                line = line.strip()
                if not line:
                    continue
                for marker in ("IV-", "CI-", "FUIR-", "PRED-", "CONTRA-"):
                    if line.upper().startswith(marker):
                        entries.append({
                            "code":     line.split(":")[0].strip(),
                            "type":     "UNKNOWN",
                            "text":     line,
                            "source":   "",
                            "priority": "MEDIUM",
                            "pred_conf": None,
                            "verify_date": None,
                        })
                        break

        return entries

    # ── Report formatting ────────────────────────────────────────────────────

    def format_report(self, synthesis: dict, honey: dict) -> str:
        """Build full Markdown report with all 4 passes."""
        m     = honey["mission"]
        topic = m["topic"]
        now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        mid   = self.mission_id

        # FUIR section
        fuir_entries = synthesis.get("fuir_entries", [])
        if fuir_entries:
            fuir_lines = []
            for e in fuir_entries:
                prio_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(
                    e.get("priority", ""), "⚪"
                )
                src_note = f" — *wymagane źródło: {e['source']}*" if e.get("source") else ""
                fuir_lines.append(
                    f"- **{e['code']}** [{e['type']}] {e['text']}{src_note} {prio_emoji}"
                )
            fuir_md = "\n".join(fuir_lines)
        else:
            # Fallback: raw FUIR text
            fuir_raw = synthesis.get("fuir_raw", "")
            if "=FUIR_START=" in fuir_raw:
                fuir_md = fuir_raw.split("=FUIR_START=", 1)[1].strip()
            else:
                fuir_md = fuir_raw.strip() or "_Brak wygenerowanych FUIR._"

        # Unresolved DB gaps
        db_gaps    = honey.get("gaps", [])
        db_gaps_md = (
            "\n".join(f"- [{g[1] or '?'}, prio={g[2]}] {g[0]}" for g in db_gaps)
            if db_gaps else "_Brak niezrealizowanych luk w bazie._"
        )

        # Stats block
        meta = synthesis.get("metadata", {})
        conf_raw = synthesis.get("confidence_raw", 0.0)
        try:
            conf_str = f"{float(conf_raw):.2f}"
        except Exception as e:
            log.debug(f"Suppressed in synth.py: {e}")
            conf_str = "?"

        report = f"""# HIVE 2.0 — Intelligence Brief (Queen 3.0)
## Temat: {topic}

> Wygenerowano: {now}
> Mission ID: `{mid}`
> Status: {m['status']}
> Passes: 4 (Evidence Hierarchy → Falsification → Synthesis → FUIR)
> Confidence Score: {conf_str}

---

{synthesis['brief']}

---

## Analiza Falsyfikacyjna (Pass 2)

{synthesis.get('falsification', '_Brak_')}

---

## Follow-Up Intelligence Requirements (FUIR)

{fuir_md}

---

## Luki Badawcze

### Niezrealizowane luki z bazy danych
{db_gaps_md}

---

## Metadane Syntezy

| Parametr | Wartość |
|----------|---------|
| Sources used | {meta.get('sources_used', '?')} |
| Facts used | {meta.get('facts_used', '?')} |
| Entities used | {meta.get('entities_used', '?')} |
| Claims used | {meta.get('claims_used', '?')} |
| QuantumBee hypotheses | {meta.get('hypotheses_used', '?')} |
| Cross-mission priors | {meta.get('priors_used', '?')} |
| Graveyard entries | {meta.get('graveyard_used', '?')} |
| Confidence raw | {conf_str} |
| Synthesized at | {meta.get('synthesized_at', '?')} |

---

*HIVE 2.0 Intelligence System (Queen 3.0) | Misja: `{mid}` | {now}*
*Sources znalezione: {m['sources_found']} | Zebrane: {m['sources_harvested']} | \
Słowa: {m['total_words']} | Encje: {m['total_entities']} | Fakty: {m['total_facts']}*
"""
        return report

    # ── Save report ──────────────────────────────────────────────────────────

    def save_report(self, report_text: str, mission_id: str, topic: str) -> dict:
        """Save Markdown (and optionally PDF) to hive-reports/."""
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        safe_topic = "".join(
            c if c.isalnum() or c in "-_ " else "_" for c in topic
        )
        safe_topic = safe_topic[:60].strip().replace(" ", "_")
        timestamp  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        base_name  = f"{timestamp}_{safe_topic}_{mission_id[:12]}"

        md_path = REPORTS_DIR / f"{base_name}.md"
        md_path.write_text(report_text, encoding="utf-8")
        log.info(f"[QUEEN] Markdown saved: {md_path}")

        pdf_path = None
        try:
            from fpdf import FPDF

            FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

            class UnicodePDF(FPDF):
                pass

            pdf = UnicodePDF()
            pdf.set_auto_page_break(auto=True, margin=15)
            pdf.add_page()
            pdf.add_font("DejaVu",      fname=FONT_REG)
            pdf.add_font("DejaVu", "B", fname=FONT_BOLD)
            pdf.set_font("DejaVu", size=9)   # domyślny font — zapobiega "no space" po tabeli
            pdf.set_text_color(0, 0, 0)

            # ── Kolory PDF ────────────────────────────────────────────────────
            PDF_HEADER_BG  = (13, 110, 90)    # #0D6E5A teal
            PDF_HEADER_FG  = (255, 255, 255)  # white
            PDF_ALT_ROW    = (224, 242, 237)  # #E0F2ED — potwierdzone wizualnie

            # Zbierz linie tabeli markdown w buforze, renderuj jako blok
            table_buf = []
            flush_needed = False

            def flush_table(buf) -> None:
                if not buf:
                    return
                # Odfiltruj separator (|---|---|)
                rows = [r for r in buf if not all(c in '-| ' for c in r)]
                if not rows:
                    return
                # Split cells
                parsed = []
                for r in rows:
                    cells = [c.strip() for c in r.strip().strip('|').split('|')]
                    parsed.append(cells)
                if not parsed:
                    return
                n_cols = max(len(r) for r in parsed)
                col_w = 185.0 / n_cols
                row_h = 6

                for ri, cells in enumerate(parsed):
                    is_header = (ri == 0)
                    is_alt    = (ri % 2 == 0) and not is_header
                    if is_header:
                        pdf.set_fill_color(*PDF_HEADER_BG)
                        pdf.set_text_color(*PDF_HEADER_FG)
                        pdf.set_font("DejaVu", "B", 8)
                    elif is_alt:
                        pdf.set_fill_color(*PDF_ALT_ROW)
                        pdf.set_text_color(0, 0, 0)
                        pdf.set_font("DejaVu", size=8)
                    else:
                        pdf.set_fill_color(255, 255, 255)
                        pdf.set_text_color(0, 0, 0)
                        pdf.set_font("DejaVu", size=8)
                    # Fit cells to width
                    for ci in range(n_cols):
                        text = cells[ci] if ci < len(cells) else ""
                        pdf.cell(col_w, row_h, text[:60], border=1, fill=True)
                    pdf.ln(row_h)
                # Reset colors
                pdf.set_fill_color(255, 255, 255)
                pdf.set_text_color(0, 0, 0)
                pdf.set_font("DejaVu", size=9)  # reset do domyślnego po tabeli
                pdf.ln(2)

            lines = report_text.splitlines()
            i = 0
            while i < len(lines):
                line = lines[i]
                try:
                    s = line.strip()
                    # Detect markdown table start
                    if s.startswith('|') and '|' in s[1:]:
                        table_buf.append(line)
                        i += 1
                        continue
                    else:
                        # Flush table if buffer full i non-table encountered
                        if table_buf:
                            flush_table(table_buf)
                            table_buf = []

                    if line.startswith("# ") and not line.startswith("## "):
                        pdf.set_font("DejaVu", "B", 16)
                        pdf.set_text_color(0, 0, 0)
                        pdf.multi_cell(0, 9, line[2:])
                    elif line.startswith("## ") and not line.startswith("### "):
                        pdf.ln(2)
                        pdf.set_font("DejaVu", "B", 13)
                        pdf.set_text_color(0, 0, 0)
                        pdf.multi_cell(0, 8, line[3:])
                    elif line.startswith("### "):
                        pdf.set_font("DejaVu", "B", 11)
                        pdf.set_text_color(0, 0, 0)
                        pdf.multi_cell(0, 7, line[4:])
                    elif line.startswith("---"):
                        pdf.ln(2)
                        pdf.set_line_width(0.3)
                        pdf.line(pdf.get_x(), pdf.get_y(), pdf.get_x() + 190, pdf.get_y())
                        pdf.ln(3)
                    elif s == "":
                        pdf.ln(3)
                    else:
                        pdf.set_font("DejaVu", size=9)
                        pdf.set_text_color(0, 0, 0)
                        pdf.multi_cell(0, 5, line)
                except Exception as e:
                    log.debug(f"Suppressed: {e}")
                i += 1
            # Flush remaining table at end
            if table_buf:
                flush_table(table_buf)

            pdf_path = REPORTS_DIR / f"{base_name}.pdf"
            pdf.output(str(pdf_path))
            log.info(f"[QUEEN] PDF saved: {pdf_path}")
        except ImportError:
            log.info("[QUEEN] fpdf2 niedostępne — skipping PDF.")
        except Exception as e:
            log.info(f"[QUEEN] Błąd generowania PDF: {e}")

        return {
            "md":  str(md_path),
            "pdf": str(pdf_path) if pdf_path else None,
        }

    # ── PostgreSQL persistence ────────────────────────────────────────────────────

    def _persist_to_db(self, synthesis: dict, honey: dict, report_text: str = ""):
        """
        Persist Queen outputs to PostgreSQL:
          1. hive_queen_assessments  — FUIR + confidence + brief
          2. hive3_mission_memory    — key_findings from synthesis
          3. hive3_calibration_log   — PRED entries as verifiable predictions
        """
        import time as _t
        con = None

        for att in range(5):
            try:
                con = self._connect_rw()
                break
            except Exception as e:
                log.debug(f"Exception in synth.py: {e}")
                if att < 4:
                    _t.sleep(2 ** att * 2)
                else:
                    log.info(f"[QUEEN] Cannot acquire RW lock for persist: {e}")
                    return

        try:
            topic      = honey["mission"]["topic"]
            mid        = self.mission_id
            meta       = synthesis.get("metadata", {})
            conf_raw   = float(synthesis.get("confidence_raw", 0.5))
            # Calibrated confidence (simple Platt scaling placeholder)
            conf_cal   = max(0.1, min(0.9, conf_raw * 0.85 + 0.05))

            fuir_entries = synthesis.get("fuir_entries", [])
            fuir_json    = json.dumps(fuir_entries, ensure_ascii=False)

            # Build must_know_insight from top CONFIRMED finding
            brief = synthesis.get("brief", "")
            # Extract first CONFIRMED line as must-know
            must_know = ""
            for line in brief.splitlines():
                if "[CONFIRMED]" in line and len(line.strip()) > 20:
                    must_know = line.strip()[:500]
                    break
            if not must_know:
                must_know = (brief[:300] + "…") if len(brief) > 300 else brief

            bee_ops_count = meta.get("hypotheses_used", 0)
            rag_count     = meta.get("facts_used", 0) + meta.get("claims_used", 0)

            # ── 1. hive_queen_assessments ────────────────────────────────────
            assessment_id = uuid.uuid4().hex[:16]
            queen_json_payload = json.dumps({
                "evidence_hierarchy": synthesis.get("evidence_hierarchy", "")[:2000],
                "falsification":      synthesis.get("falsification", "")[:2000],
                "fuir_entries":       fuir_entries,
                "passes":             4,
                "synthesized_at":     meta.get("synthesized_at"),
            }, ensure_ascii=False)

            con.execute("""
                INSERT INTO hive_queen_assessments
                    (id, mission_id, query, queen_json,
                     must_know_insight, hausdorff_score,
                     confidence_raw, confidence_calibrated,
                     bee_ops_count, rag_chunks_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                assessment_id, mid, topic, queen_json_payload,
                must_know, None,
                conf_raw, conf_cal,
                bee_ops_count, rag_count,
            ])

            # ── 2. hive3_mission_memory ──────────────────────────────────────
            # Extract key_findings: CONFIRMED lines from brief
            key_findings_list = []
            for line in brief.splitlines():
                stripped = line.strip()
                if ("[CONFIRMED]" in stripped or "[PROBABLE]" in stripped) \
                        and len(stripped) > 30:
                    key_findings_list.append({
                        "fact":       stripped[:300],
                        "confidence": conf_raw,
                        "domain":     "synthesis",
                    })
                if len(key_findings_list) >= 5:
                    break

            key_findings_json = json.dumps(
                key_findings_list, ensure_ascii=False
            )

            # Upsert: if mission exists update, else insert
            existing = con.execute(
                "SELECT id FROM hive3_mission_memory WHERE mission_id = ?", [mid]
            ).fetchone()

            if existing:
                con.execute("""
                    UPDATE hive3_mission_memory
                    SET key_findings = ?,
                        confidence   = ?,
                        completed_at = NOW()
                    WHERE mission_id = ?
                """, [key_findings_json, conf_raw, mid])
                log.info("[QUEEN] hive3_mission_memory updated.")
            else:
                # Need next id
                max_id = con.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM hive3_mission_memory"
                ).fetchone()[0]
                con.execute("""
                    INSERT INTO hive3_mission_memory
                        (id, mission_id, mission_topic, completed_at,
                         key_findings, confidence)
                    VALUES (?, ?, ?, NOW(), ?, ?)
                """, [max_id + 1, mid, topic, key_findings_json, conf_raw])
                log.info("[QUEEN] hive3_mission_memory inserted.")

            # ── 3. hive3_calibration_log — PRED entries ──────────────────────
            pred_entries = [
                e for e in fuir_entries
                if e.get("type", "").upper() in ("PREDICTION", "PRED")
            ]
            if pred_entries:
                for pred in pred_entries:
                    pred_text = pred.get("text", "")
                    pred_conf = pred.get("pred_conf") or conf_raw
                    try:
                        pred_conf = float(pred_conf)
                    except Exception as e:
                        log.debug(f"Suppressed in synth.py: {e}")
                        pred_conf = 0.5

                    con.execute("""
                        INSERT INTO hive3_calibration_log
                            (mission_id, prediction, confidence_stated)
                        VALUES (?, ?, ?)
                    """, [mid, pred_text[:500], pred_conf])

                log.info(f"[QUEEN] {len(pred_entries)} predictions saved to "
                      f"hive3_calibration_log.")

            con.commit()
            log.info(f"[QUEEN] Data persisted to DB "
                  f"(assessment_id={assessment_id}).")

        except Exception as e:
            log.info(f"[QUEEN] BŁĄD persystencji: {e}")
            traceback.print_exc(file=sys.stderr)
        finally:
            if con:
                con.close()

    # ── Update mission status ────────────────────────────────────────────────

    def _mark_done(self, report_text: str = None, synthesis: dict = None):
        """Mark mission done. Also saves synthesis text + quality metrics to DB."""
        import time as _t

        # ── Oblicz quality metrics ──────────────────────────────────────────
        quality_coverage   = None
        quality_depth      = None
        quality_confidence = None

        try:
            con_r = self._db_connect(read_only=True)
            mid = self.mission_id

            # coverage: % sources with full content (word_count > 0)
            total_src = con_r.execute(
                "SELECT COUNT(*) FROM hive_sources WHERE mission_id=? AND status='done'",
                [mid]).fetchone()[0] or 0
            rich_src = con_r.execute(
                "SELECT COUNT(*) FROM hive_content WHERE mission_id=? AND word_count > 200",
                [mid]).fetchone()[0] or 0
            quality_coverage = round(rich_src / total_src, 3) if total_src > 0 else 0.0

            # depth: ile claims HIGH confidence / total claims
            total_claims = con_r.execute(
                "SELECT COUNT(*) FROM hive_claims WHERE mission_id=?", [mid]).fetchone()[0] or 0
            high_claims = con_r.execute(
                "SELECT COUNT(*) FROM hive_claims WHERE mission_id=? AND confidence >= 0.85",
                [mid]).fetchone()[0] or 0
            quality_depth = round(high_claims / total_claims, 3) if total_claims > 0 else 0.0

            # confidence: avg confidence z claims
            avg_conf = con_r.execute(
                "SELECT AVG(confidence) FROM hive_claims WHERE mission_id=?",
                [mid]).fetchone()[0] or 0.0
            quality_confidence = round(float(avg_conf), 3)

            con_r.close()
        except Exception as qe:
            log.error(f"[QUEEN] quality metrics calc error: {qe}")

        synthesis_text = report_text or ""

        for att in range(5):
            try:
                con = self._connect_rw()
                con.execute(
                    "UPDATE hive_missions SET status='done', "
                    "phase='done', "
                    "synth_done_at=NOW(), "
                    "synthesis=?, "
                    "quality_coverage=?, "
                    "quality_depth=?, "
                    "quality_confidence=? "
                    "WHERE id=?",
                    [synthesis_text, quality_coverage, quality_depth,
                     quality_confidence, self.mission_id]
                )
                con.commit()
                con.close()
                log.info(f"[QUEEN] _mark_done: synthesis={len(synthesis_text)} chars, "
                    f"quality_coverage={quality_coverage}, "
                    f"quality_depth={quality_depth}, "
                    f"quality_confidence={quality_confidence}")
                return
            except Exception as e:
                log.debug(f"Exception in synth.py: {e}")
                if 'IO Error' in str(e) or 'lock' in str(e).lower():
                    _t.sleep(2 ** att * 2)
                else:
                    raise

    # ── Main entry point ─────────────────────────────────────────────────────

    def run(self) -> dict:
        """Execute the main pipeline loop."""
        t0 = time.time()
        log.debug(f"\n👑 QUEEN 3.0 — synteza misji: {self.mission_id}")
        log.info("[QUEEN] Pipeline: Evidence Hierarchy → Falsification → "
              "Synthesis → FUIR → Persist")

        # Load honey
        _t_load = time.time()
        honey   = self._load_honey()
        topic   = honey["mission"]["topic"]
        log.info(f"[QUEEN] Honey loaded in {time.time() - _t_load:.1f}s: "
            f"{len(honey['all_facts'])} faktów, "
            f"{len(honey['top_entities'])} encji, "
            f"{len(honey['clusters'])} klastrów, "
            f"{len(honey.get('raw_claims', []))} twierdzeń, "
            f"{len(honey.get('quantum_hypotheses', []))} hipotez QuantumBee, "
            f"{len(honey.get('cross_mission_priors', []))} priorów, "
            f"{len(honey.get('dead_hypotheses', []))} Graveyard.")

        # 4-Pass synthesis
        _t_synth = time.time()
        synthesis = self.synthesize(honey)
        log.info(f"[QUEEN] 4-Pass synteza zakończona w "
              f"{time.time() - _t_synth:.1f}s")

        # Format report
        _t_fmt = time.time()
        report  = self.format_report(synthesis, honey)
        log.info(f"[QUEEN] Format raportu zajął {time.time() - _t_fmt:.1f}s")

        # Multi-role refinement (Editor → Reviewer → Writer)
        try:
            _t_multi = time.time()
            report = multi_role_synthesis(self.mission_id, report, topic=topic)
            log.info(f"[QUEEN] Multi-role synthesis: {time.time() - _t_multi:.1f}s")
        except Exception as _mr_err:
            log.info(f"[QUEEN] Multi-role synthesis pominięty: {_mr_err}")

        # Save report
        paths = self.save_report(report, self.mission_id, topic)

        # Persist to PostgreSQL
        _t_persist = time.time()
        self._persist_to_db(synthesis, honey, report_text=report)
        log.info(f"[QUEEN] Persistence PostgreSQL: {time.time() - _t_persist:.1f}s")

        # Mark done (passes report text so synthesis column gets populated)
        self._mark_done(report_text=report, synthesis=synthesis)

        # ── Obsidian sync ────────────────────────────────────────────────────
        try:
            import importlib.util
            _spec = importlib.util.spec_from_file_location(
                "hive2_obsidian", "hive2_obsidian.py"
            )
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _obs = _mod.HiveObsidian()
            _obs.export_mission(self.mission_id)
            if paths.get("md"):
                _obs.import_report(paths["md"], self.mission_id)
            _obs.push_to_couchdb()
            log.debug("📒 Obsidian vault zaktualizowany → obsidian-vault/")
        except Exception as _e:
            log.warning(f"⚠️  Obsidian sync pominięty: {_e}")

        # ── Graphify post-synth hook ─────────────────────────────────────────
        try:
            import importlib.util as _ilu2
            _gspec = _ilu2.spec_from_file_location(
                "hive2_graphify", "hive2_graphify.py"
            )
            _gmod = _ilu2.module_from_spec(_gspec)
            _gspec.loader.exec_module(_gmod)
            _gmod.post_synth_hook(self.mission_id)
            log.debug("🕸️  Graphify vault graph rebuild triggered")
        except Exception as _ge:
            log.warning(f"⚠️  Graphify hook pominięty: {_ge}")

        # ── Intelligence Closure — closing the epistemic loop ────────────
        try:
            import importlib.util as _ilu2
            _fc_spec = _ilu2.spec_from_file_location(
                "hive2_queen_feedback", "hive2_queen_feedback.py")
            _fc_mod = _ilu2.module_from_spec(_fc_spec)
            _fc_spec.loader.exec_module(_fc_mod)
            closure = _fc_mod.IntelligenceClosure()
            brief_text = synthesis.get("brief", "")
            cr = closure.close_mission(self.mission_id, brief_text)
            log.debug(f"🧠 Intelligence Closure: "
                f"{cr.get('predictions_extracted', 0)} predykcji, "
                f"{cr.get('priors_updated', 0)} priors, "
                f"{cr.get('lessons_extracted', 0)} lekcji, "
                f"{cr.get('pending_validated', 0)} predykcji zweryfikowanych")
        except Exception as _fe:
            log.warning(f"⚠️  Intelligence Closure pominięty: {_fe}")

        # ── QueenMemory persist — key_findings to cross-mission memory ──────
        try:
            from behive.engine.queen_memory import QueenMemory
            qm = QueenMemory(topic)
            key_findings = synthesis.get("follow_up_queries", [])
            brief_lines = [
                l.strip() for l in synthesis.get("brief", "").split("\n")
                if l.strip().startswith(("**", "•", "-", "1.", "2.", "3."))
                and len(l.strip()) > 30
            ]
            if brief_lines:
                key_findings = brief_lines[:5] + key_findings[:3]
            qm.persist(self.mission_id, key_findings)
            log.debug(f"🧠 QueenMemory: {len(key_findings)} key_findings do pamięci cross-mission")
        except Exception as _qme:
            log.warning(f"⚠️  QueenMemory persist pominięty: {_qme}")

        # ── Numerical Falsification hook ─────────────────────────────────────
        try:
            import importlib.util as _flu
            _fspec = _flu.spec_from_file_location("hive2_falsifier", "hive2_falsifier.py")
            _fmod  = _flu.module_from_spec(_fspec)
            _fspec.loader.exec_module(_fmod)
            _fr = _fmod.run_falsification_for_mission(self.mission_id, resolve=True)
            log.info(
                f"🔴 NumericalFalsifier: {_fr.get('real_conflicts', 0)} REAL_CONFLICT "
                f"z {_fr.get('conflicts_detected', 0)} wykrytych"
            )
        except Exception as _fe:
            log.debug(f"NumericalFalsifier hook pominięty: {_fe}")

        elapsed = time.time() - t0
        log.info(f"\n[QUEEN] Synteza zakończona w {elapsed:.1f}s")
        log.info(f"[QUEEN] Raport MD: {paths['md']}")
        if paths["pdf"]:
            log.info(f"[QUEEN] Raport PDF: {paths['pdf']}")

        log.debug("\n" + "=" * 70)
        log.debug(report)
        log.debug("=" * 70)
        log.debug(f"\nMD:  {paths['md']}")
        if paths["pdf"]:
            log.debug(f"PDF: {paths['pdf']}")


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 hive2_synth.py <mission_id>", file=sys.stderr)
        sys.exit(1)

    mission_id = sys.argv[1]
    try:
        queen = Queen(mission_id)
        queen.run()
    except Exception as exc:
        log.debug(f"Exception in synth.py: {exc}")
        print(f"[QUEEN] BŁĄD KRYTYCZNY: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
