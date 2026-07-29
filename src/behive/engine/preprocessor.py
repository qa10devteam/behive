"""Content preprocessor — HTML cleaning, markdown extraction, and text normalization."""

import logging
#!/usr/bin/env python3
"""
HIVE 2.0 — hive2_preprocessor.py
Query Clarifier + Specificity Scorer


log = logging.getLogger(__name__)
Inspired by: aitmpl.com deep-research-team / query-clarifier agent pattern
Runs BEFORE scout phase to catch ambiguous topics early.

Usage:
  result = preprocess_query("AI trends")   # returns {ok, refined_topic, clarifications}
  # if ok=False → print clarifications and exit
  # if ok=True  → proceed with result['refined_topic']
"""

import re
import sys
from dataclasses import dataclass, field
from typing import Optional


# ── Thresholds ──────────────────────────────────────────────────────────────
SPECIFICITY_THRESHOLD   = 0.55   # below → ask for clarification
AUTO_EXPAND_THRESHOLD   = 0.35   # below → refuse, too vague even to clarify
INTERACTIVE_DEFAULT     = True   # set False for non-interactive (API/SSE usage)


# ── Signals ─────────────────────────────────────────────────────────────────

VAGUE_SIGNALS = [
    # single-word topics
    r'^[a-zA-Z]+$',
    # ultra-short (≤ 15 chars)
    r'^.{1,15}$',
    # pure buzzwords
    r'\b(trends?|analysis|research|study|overview|review|update|news|latest|everything|anything)\b',
    # no domain signal
]

DOMAIN_SIGNALS = [
    # has a year → temporal specificity
    r'\b(20\d\d|19\d\d)\b',
    # has a geography → spatial specificity
    r'\b(poland|eu|europe|usa|china|turkey|georgia|market|country|region|city)\b',
    # has a company / product name pattern (CamelCase or quoted)
    r'[A-Z][a-z]+[A-Z]|"[^"]+"|\'[^\']+\'',
    # has a metric / number
    r'\b\d+[%$€kKmMbB]?\b',
    # long query (> 40 chars)
    r'^.{40,}$',
    # contains question words
    r'\b(how|why|what|when|where|which|who|compare|versus|vs\.?|impact|effect|cause|risk)\b',
    # contains procurement/business specific terms
    r'\b(tender|procurement|rfp|contract|bid|import|export|regulation|compliance|pricing|revenue|gdp|market\s+share)\b',
]


def _specificity_score(topic: str) -> float:
    """
    Return a 0.0-1.0 specificity score for a research topic.
    0.0 = completely vague ("AI")
    1.0 = hyper-specific ("Impact of EU AI Act on Polish SME procurement 2024")
    """
    t = topic.strip().lower()
    words = t.split()

    score = 0.0

    # Base: word count contribution (saturates at ~8 words)
    score += min(len(words) / 8.0, 1.0) * 0.30

    # Domain signals (each adds 0.10, max 0.50)
    domain_hits = 0
    for pattern in DOMAIN_SIGNALS:
        if re.search(pattern, topic, re.IGNORECASE):
            domain_hits += 1
    score += min(domain_hits * 0.12, 0.50)

    # Penalise for vague signals
    vague_hits = 0
    for pattern in VAGUE_SIGNALS:
        if re.search(pattern, topic, re.IGNORECASE):
            vague_hits += 1
    score -= vague_hits * 0.15

    # Clamp
    return max(0.0, min(1.0, score))


def _generate_clarifications(topic: str) -> list[str]:
    """
    Generate 3 targeted clarifying questions for a vague topic.
    Deterministic — no LLM needed.
    """
    t = topic.strip()
    questions = []

    # Q1: Scope / geography
    if not re.search(r'\b(poland|eu|europe|usa|china|global|worldwide|market|country|region)\b', t, re.I):
        questions.append(
            f"Geographic / market scope — which region or country should the research focus on? "
            f"(e.g. Poland, EU, global)"
        )

    # Q2: Time frame
    if not re.search(r'\b(20\d\d|19\d\d|recent|last \d|past \d|current|today|2024|2025)\b', t, re.I):
        questions.append(
            f"Time frame — which period matters most? "
            f"(e.g. last 12 months, 2022–2025, historical context)"
        )

    # Q3: Purpose / output type
    questions.append(
        f"Purpose — what decision should this research support? "
        f"(e.g. market entry, competitor analysis, investor brief, regulatory compliance)"
    )

    # Limit to 3
    return questions[:3]


@dataclass
class PreprocessResult:
    """Preprocessing result: cleaned text, metadata, quality signals."""
    ok: bool                              # True → proceed with refined_topic
    refined_topic: str                    # cleaned / unchanged topic
    specificity_score: float              # 0.0–1.0
    clarifications: list[str] = field(default_factory=list)   # questions to ask user
    message: str = ""                     # human-readable status


def preprocess_query(
    topic: str,
    interactive: bool = INTERACTIVE_DEFAULT,
    force: bool = False,
) -> PreprocessResult:
    """
    Analyse topic specificity and optionally prompt for clarification.

    Args:
        topic:       raw research topic from user
        interactive: if True, prints questions and reads stdin
        force:       skip clarification even if vague (--force flag)

    Returns:
        PreprocessResult with ok=True if pipeline should proceed
    """
    topic = topic.strip()
    if not topic:
        return PreprocessResult(
            ok=False,
            refined_topic=topic,
            specificity_score=0.0,
            message="Empty topic — nothing to research.",
        )

    score = _specificity_score(topic)

    # ── Too vague to even clarify ────────────────────────────────────────────
    if score < AUTO_EXPAND_THRESHOLD and not force:
        clarifications = _generate_clarifications(topic)
        msg = (
            f"Topic '{topic}' is too broad (specificity={score:.2f}).\n"
            f"Please answer these questions to narrow the scope:\n"
        )
        for i, q in enumerate(clarifications, 1):
            msg += f"  {i}. {q}\n"
        return PreprocessResult(
            ok=False,
            refined_topic=topic,
            specificity_score=score,
            clarifications=clarifications,
            message=msg,
        )

    # ── Vague but usable → ask if interactive ───────────────────────────────
    if score < SPECIFICITY_THRESHOLD and interactive and not force:
        clarifications = _generate_clarifications(topic)

        log.debug(f"\n  ⚠️  Topic specificity score: {score:.2f}/1.00 (threshold: {SPECIFICITY_THRESHOLD})")
        log.debug(f"  Topic '{topic}' is somewhat broad. Answer these to improve research quality:\n")
        for i, q in enumerate(clarifications, 1):
            log.debug(f"  {i}. {q}")
        log.debug(f"\n  Press Enter to continue with broad topic, or type a more specific query:")

        try:
            user_input = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            user_input = ""

        if user_input:
            # Re-score the refined topic
            new_score = _specificity_score(user_input)
            return PreprocessResult(
                ok=True,
                refined_topic=user_input,
                specificity_score=new_score,
                message=f"Refined topic accepted (score: {new_score:.2f})",
            )
        # User pressed Enter → proceed with original
        return PreprocessResult(
            ok=True,
            refined_topic=topic,
            specificity_score=score,
            message=f"Proceeding with broad topic (score: {score:.2f})",
        )

    # ── Specific enough ─────────────────────────────────────────────────────
    return PreprocessResult(
        ok=True,
        refined_topic=topic,
        specificity_score=score,
        message=f"Topic accepted (specificity: {score:.2f})",
    )


# ── CLI self-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_topics = [
        "AI",
        "AI trends",
        "artificial intelligence",
        "AI trends 2025",
        "Impact of AI regulation on Polish fintech market entry 2024-2025",
        "procurement software market in EU 2024 competitive analysis",
        "social media marketing",
        "n8n automation trends in Eastern Europe Q1 2025",
        "HIVE",
        "open-source intelligence gathering tools for B2B market research Poland",
    ]

    if len(sys.argv) > 1:
        test_topics = [" ".join(sys.argv[1:])]

    print("=" * 65)
    print("  HIVE Query Preprocessor — Specificity Scorer")
    print("=" * 65)
    for t in test_topics:
        result = preprocess_query(t, interactive=False)
        status = "✅ OK" if result.ok else "⚠️  VAGUE"
        print(f"\n  {status}  score={result.specificity_score:.2f}  '{t}'")
        if not result.ok:
            for q in result.clarifications:
                print(f"         → {q}")
    print("\n" + "=" * 65)
