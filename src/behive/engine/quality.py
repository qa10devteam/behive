#!/usr/bin/env python3
"""
HIVE 2.0 — hive2_content_quality.py
═════════════════════════════════════
Covert Degradation Detector — analog DWV (Deformed Wing Virus) detekcji.

Biologia:
  Covert DWV infections — pszczoła wygląda zdrowo, lata normalnie,
  ale ma chronicznie gorsze foraging i skróconą żywotność.
  Wirus jest niewidoczny bez aktywnego testu.

W HIVE:
  Źródła które odpowiadają na HEAD (200 OK, content-length OK)
  ale zwracają scraper-noise, nav-boilerplate, lub AI-generated fluff
  są "covertly infected" — obniżają jakość całej syntezy.

Scoring: 4 ortogonalne sygnały (każdy 0.0–1.0, ważone)
  1. sentence_coherence  — avg długość zdań, brak fragmentów
  2. lexical_diversity   — unique bigrams / total bigrams  
  3. factual_density     — liczby, daty, named entities / słowo
  4. boilerplate_ratio   — nawigacja, stopka, cookie banners (odwrócony)

Threshold: quality_score < 0.32 → "covertly infected" → penalizuj w scoring

Public API:
    from hive2_content_quality import score_content, is_covert_infected
    q = score_content(raw_text)        # 0.0–1.0
    bad = is_covert_infected(raw_text) # True/False
    details = score_content_detailed(raw_text)  # dict with all signals
"""

from __future__ import annotations

import re
from typing import Dict

# ─────────────────────────────────────────────────────────────────────────────
# Regex patterns (compiled once)
# ─────────────────────────────────────────────────────────────────────────────
_NUM_PAT = re.compile(
    r'\b\d[\d,\.]*\s*(?:million|billion|trillion|percent|%|USD|EUR|PLN|GBP|'
    r'\$|€|mln|mld|tys\.?|k|M|B)?\b',
    re.I,
)
_DATE_PAT = re.compile(
    r'\b(20\d{2}|19\d{2})\b'
    r'|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
    r'sty|lut|mar|kwi|maj|cze|lip|sie|wrz|paź|lis|gru)[a-z]*\s+20\d{2}\b',
    re.I,
)
_ENTITY_PAT = re.compile(
    r'\b[A-ZŁÓŚŹĆĘĄŃ][a-zA-ZłóśźćęąńĄĘŁŃŚŹŻÓ]{2,}\s+[A-ZŁÓŚŹĆĘĄŃ][a-zA-ZłóśźćęąńĄĘŁŃŚŹŻÓ]{2,}\b'
)
_SENTENCE_PAT = re.compile(r'[.!?]+\s+')
_WORD_PAT     = re.compile(r'\b[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]{2,}\b')

# Boilerplate markers — navigation/footer/cookie/SEO junk
_BOILERPLATE_MARKERS = [
    "click here", "read more", "subscribe", "cookie policy",
    "privacy policy", "terms of service", "all rights reserved",
    "copyright ©", "follow us", "share this", "like us on",
    "back to top", "home | about | contact",
    "skip to content", "skip to main",
    "menu", "navigation", "breadcrumb",
    "related articles", "you might also like",
    "404", "page not found",
    "javascript is required", "please enable",
    "polityka prywatności", "regulamin", "cookie",
    "menu główne", "przejdź do treści",
    "powiązane artykuły",
]

# Covert infection threshold
COVERT_THRESHOLD = 0.32


def score_content_detailed(text: str) -> Dict[str, float]:
    """
    Scores content across 4 signals. Returns dict with all scores + final.
    Szybki, zero I/O, zero LLM.
    """
    if not text or len(text.strip()) < 100:
        return {
            "sentence_coherence": 0.1,
            "lexical_diversity":  0.1,
            "factual_density":    0.0,
            "boilerplate_ratio":  1.0,   # pure boilerplate
            "quality_score":      0.05,
            "covert_infected":    True,
        }

    text_lower = text.lower()
    words = _WORD_PAT.findall(text)
    word_count = len(words) or 1

    # ── 1. Sentence Coherence ─────────────────────────────────────────────
    sentences = _SENTENCE_PAT.split(text.strip())
    sentences = [s.strip() for s in sentences if s.strip()]
    if sentences:
        avg_sent_len = sum(len(s.split()) for s in sentences) / len(sentences)
        # Ideal range 8–30 words; <4 = fragments, >60 = run-on machine text
        if avg_sent_len < 4:
            coh = 0.15
        elif avg_sent_len <= 8:
            coh = 0.45
        elif avg_sent_len <= 30:
            coh = 0.85 + min(0.15, (avg_sent_len - 8) / 150)
        elif avg_sent_len <= 50:
            coh = 0.75
        else:
            coh = 0.40   # machine-generated run-ons
    else:
        coh = 0.1

    # ── 2. Lexical Diversity ──────────────────────────────────────────────
    # Unique bigrams / total bigrams (captures copy-paste and AI repetition)
    bigrams = [
        f"{words[i]} {words[i+1]}"
        for i in range(len(words) - 1)
    ]
    if bigrams:
        unique_ratio = len(set(bigrams)) / len(bigrams)
        # Healthy content: 0.7–0.95; scraper dumps: <0.3
        if unique_ratio >= 0.75:
            div = 0.90
        elif unique_ratio >= 0.55:
            div = 0.70
        elif unique_ratio >= 0.35:
            div = 0.45
        else:
            div = 0.15  # heavy repetition
    else:
        div = 0.1

    # ── 3. Factual Density ────────────────────────────────────────────────
    num_matches   = len(_NUM_PAT.findall(text))
    date_matches  = len(_DATE_PAT.findall(text))
    entity_matches = len(_ENTITY_PAT.findall(text))
    total_facts   = num_matches + date_matches + entity_matches * 0.5
    facts_per_100 = (total_facts / word_count) * 100

    if facts_per_100 >= 4.0:
        fact = 0.95
    elif facts_per_100 >= 2.0:
        fact = 0.80
    elif facts_per_100 >= 0.8:
        fact = 0.60
    elif facts_per_100 >= 0.2:
        fact = 0.35
    else:
        fact = 0.10   # zero facts = opinion/nav/filler

    # ── 4. Boilerplate Ratio (inverted — lower = better content) ─────────
    bp_hits = sum(1 for marker in _BOILERPLATE_MARKERS if marker in text_lower)
    # Normalize: 0 hits = 0.0, 5+ hits = ~1.0
    bp_raw = min(bp_hits / 5.0, 1.0)
    # Inverted for scoring: 0 boilerplate → 1.0 score
    bp_quality = 1.0 - bp_raw

    # ── Weighted final ─────────────────────────────────────────────────────
    # Weights: coherence 25%, diversity 25%, factual 35%, boilerplate 15%
    quality = (
        0.25 * coh +
        0.25 * div +
        0.35 * fact +
        0.15 * bp_quality
    )
    quality = round(min(max(quality, 0.0), 1.0), 4)

    return {
        "sentence_coherence": round(coh, 3),
        "lexical_diversity":  round(div, 3),
        "factual_density":    round(fact, 3),
        "boilerplate_quality": round(bp_quality, 3),
        "facts_per_100_words": round(facts_per_100, 2),
        "avg_sentence_len":    round(sum(len(s.split()) for s in sentences) / max(len(sentences), 1), 1),
        "quality_score":      quality,
        "covert_infected":    quality < COVERT_THRESHOLD,
    }


def score_content(text: str) -> float:
    """Fast single-number quality score. 0.0–1.0."""
    return score_content_detailed(text)["quality_score"]


def is_covert_infected(text: str) -> bool:
    """True jeśli treść jest covertly degraded (wysyłaj do kwarantanny)."""
    return score_content(text) < COVERT_THRESHOLD


def quality_label(score: float) -> str:
    """Human-readable label."""
    if score >= 0.75:   return "EXCELLENT"
    elif score >= 0.60: return "GOOD"
    elif score >= 0.45: return "FAIR"
    elif score >= 0.32: return "LOW"
    else:               return "INFECTED"  # covert DWV


# ─────────────────────────────────────────────────────────────────────────────
# CLI + batch analysis
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if "--audit-mission" in sys.argv:
        # Analiza jakości treści w misji
        idx = sys.argv.index("--audit-mission")
        mission_id = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else None
        if not mission_id:
            print("Usage: python3 hive2_content_quality.py --audit-mission <mission_id>")
            sys.exit(1)

        # duckdb removed — using hive2_db
        con = _hive_db.connect(read_only=True)
        rows = con.execute("""
            SELECT id, url, full_text, word_count
            FROM hive_content
            WHERE mission_id = ?
              AND full_text IS NOT NULL
              AND LENGTH(full_text) > 200
            LIMIT 200
        """, [mission_id]).fetchall()
        con.close()

        print(f"\n🐝 CONTENT QUALITY AUDIT — mission {mission_id[:16]}…")
        print(f"   Analyzing {len(rows)} documents...\n")

        results = []
        for doc_id, url, text, wc in rows:
            d = score_content_detailed(text or "")
            results.append((d["quality_score"], url, d))

        results.sort(key=lambda x: x[0])

        infected = [r for r in results if r[2]["covert_infected"]]
        print(f"   INFECTED (covert DWV): {len(infected)}/{len(results)} = {len(infected)/max(len(results),1):.0%}")
        print(f"   Avg quality: {sum(r[0] for r in results)/max(len(results),1):.3f}\n")

        print("   TOP 10 WORST (likely garbage):")
        for q, url, d in results[:10]:
            print(f"     [{quality_label(q):8s}] {q:.3f}  {url[:70]}")

        print("\n   TOP 10 BEST:")
        for q, url, d in reversed(results[-10:]):
            print(f"     [{quality_label(q):8s}] {q:.3f}  {url[:70]}")

    elif "--test" in sys.argv:
        tests = [
            ("Good content", "The UK market for robotic process automation (RPA) grew 34% in 2024, reaching £2.1 billion according to Gartner. Companies like UiPath and Automation Anywhere reported record revenues. The CAGR for 2025-2030 is projected at 18.5% driven by AI integration."),
            ("AI fluff", "In today's rapidly evolving landscape, it's crucial to understand the various aspects. There are many different ways to approach this. Click here to read more. Subscribe to our newsletter. Privacy Policy | Terms of Service."),
            ("Nav garbage", "Home About Services Contact Blog Portfolio. Menu. Skip to content. All rights reserved 2024. Cookie Policy. You might also like. Related articles."),
            ("Scraper repeat", "the company the company the company the company reported reported reported that that that the market the market the market grew grew grew."),
        ]
        for label, text in tests:
            d = score_content_detailed(text)
            print(f"\n[{label}]")
            print(f"  Quality: {d['quality_score']:.3f} ({quality_label(d['quality_score'])})")
            print(f"  Coherence={d['sentence_coherence']:.2f}  Diversity={d['lexical_diversity']:.2f}  "
                  f"Factual={d['factual_density']:.2f}  Boilerplate={d['boilerplate_quality']:.2f}")
            print(f"  Infected: {d['covert_infected']}")
    else:
        # Stdin mode
        text = sys.stdin.read() if not sys.stdin.isatty() else ""
        if text:
            d = score_content_detailed(text)
            print(f"Quality: {d['quality_score']:.3f} ({quality_label(d['quality_score'])})")
            for k, v in d.items():
                if k not in ("quality_score", "covert_infected"):
                    print(f"  {k}: {v}")
        else:
            print("Usage: python3 hive2_content_quality.py --test")
            print("       python3 hive2_content_quality.py --audit-mission <mission_id>")
            print("       echo 'some text' | python3 hive2_content_quality.py")
