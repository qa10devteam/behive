#!/usr/bin/env python3
"""
HIVE 2.0 — hive2_domain_reputation.py
═══════════════════════════════════════
Domain Reputation Engine — analog GROOMING BEHAVIOR pszczelego ula.

Pszczoły z Urugwaju wyewoluowały wyższe grooming behavior → niższe infekcje DWV.
Ten moduł robi to samo dla domen: akumuluje wiedzę negatywną cross-mission
i eliminuje "zarażone" źródła z kolejnych harvests.

Schemat:
  hive_domain_reputation (DuckDB persistent)
    domain          VARCHAR PK
    avg_quality     FLOAT   — średnia jakość treści (0.0–1.0)
    harvest_count   INT     — ile razy harvestowane
    success_count   INT     — ile razy z treścią
    last_harvest_at TIMESTAMP
    last_bad_at     TIMESTAMP  — ostatni bad harvest
    consecutive_bad INT     — ile złych pod rząd
    blacklisted     BOOLEAN
    blacklist_reason VARCHAR

Public API:
    from hive2_domain_reputation import DomainReputation
    rep = DomainReputation()

    # Przed harvest
    factor = rep.get_score_boost(url)  # 0.5–1.5 (mnożnik dla score_total)
    skip = rep.should_skip(url)        # True jeśli blacklisted

    # Po harvest (aktualizacja)
    rep.update(url, quality_score, success)

    # Po misji — zapis zbiorczy
    rep.flush()

    # Statystyki
    rep.report()
"""

from __future__ import annotations

import re
import time
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("hive2_domain_reputation")

DB_PATH = ""

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hive_domain_reputation (
    domain              VARCHAR PRIMARY KEY,
    avg_quality         FLOAT   DEFAULT 0.5,
    harvest_count       INT     DEFAULT 0,
    success_count       INT     DEFAULT 0,
    last_harvest_at     TIMESTAMP,
    last_bad_at         TIMESTAMP,
    consecutive_bad     INT     DEFAULT 0,
    blacklisted         BOOLEAN DEFAULT FALSE,
    blacklist_reason    VARCHAR DEFAULT '',
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);
"""

# Thresholds — kalibracja bioniczna
_BLACKLIST_MIN_HARVESTS = 4      # min harvests zanim blacklist
_BLACKLIST_CONSECUTIVE  = 3      # ile złych pod rząd → blacklist
_BLACKLIST_AVG_QUALITY  = 0.15   # avg quality < X po 6+ harvests → blacklist
_BAD_QUALITY_THRESHOLD  = 0.3    # pojedynczy harvest < X = "zły"
_BOOST_HIGH             = 1.35   # boost dla zaufanych domen (avg > 0.75)
_BOOST_MED              = 1.10   # lekki boost (avg 0.55–0.75)
_BOOST_LOW              = 0.65   # kara (avg 0.25–0.45)
_BOOST_VERY_LOW         = 0.35   # duża kara (avg < 0.25, nie blacklisted yet)


def _extract_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        # strip www.
        if domain.startswith("www."):
            domain = domain[4:]
        # strip port
        domain = domain.split(":")[0]
        return domain
    except Exception:
        return url[:100]


def _duckdb_connect(read_only: bool = False):
    """Connect via hive2_db (PostgreSQL)."""
    import hive2_db as _hive_db
    return _hive_db.connect(read_only=read_only)


class DomainReputation:
    """
    Persistent cross-mission domain reputation tracker.
    Jednorazowa inicjalizacja per run — flush() na końcu.
    """

    def __init__(self):
        self._pending_updates: dict[str, dict] = {}  # domain → pending data
        self._cache: dict[str, dict] = {}           # domain → loaded reputation
        self._ensure_schema()

    def _ensure_schema(self):
        try:
            con = _duckdb_connect()
            con.execute(_SCHEMA_SQL)
            con.close()
        except Exception as e:
            log.warning(f"Domain reputation schema init failed: {e}")

    def _load(self, domain: str) -> Optional[dict]:
        """Load domain record from DB. Returns None if not found."""
        if domain in self._cache:
            return self._cache[domain]
        try:
            con = _duckdb_connect(read_only=True)
            row = con.execute("""
                SELECT domain, avg_quality, harvest_count, success_count,
                       last_harvest_at, last_bad_at, consecutive_bad,
                       blacklisted, blacklist_reason
                FROM hive_domain_reputation
                WHERE domain = ?
            """, [domain]).fetchone()
            con.close()
            if row:
                rec = {
                    "domain":           row[0],
                    "avg_quality":      float(row[1] or 0.5),
                    "harvest_count":    int(row[2] or 0),
                    "success_count":    int(row[3] or 0),
                    "last_harvest_at":  row[4],
                    "last_bad_at":      row[5],
                    "consecutive_bad":  int(row[6] or 0),
                    "blacklisted":      bool(row[7]),
                    "blacklist_reason": row[8] or "",
                }
                self._cache[domain] = rec
                return rec
        except Exception as e:
            log.debug(f"Domain rep load failed for {domain}: {e}")
        return None

    # ─────────────────────────────────────────────────────────────────────
    # PUBLIC READ API
    # ─────────────────────────────────────────────────────────────────────

    def should_skip(self, url: str) -> bool:
        """True jeśli domena jest na blackliście — pomijaj w F1/F4."""
        domain = _extract_domain(url)
        rec = self._load(domain)
        if rec and rec["blacklisted"]:
            log.debug(f"  🐝 Grooming: skipping blacklisted domain {domain} ({rec['blacklist_reason']})")
            return True
        return False

    def get_score_boost(self, url: str) -> float:
        """
        Zwraca mnożnik dla score_total źródła.
        Wysoka reputacja → 1.35x, niska → 0.35x.
        Nowy domain → 1.0 (neutral).
        """
        domain = _extract_domain(url)
        rec = self._load(domain)
        if not rec:
            return 1.0  # nowy domain — neutral

        if rec["blacklisted"]:
            return 0.0  # wyklucz całkowicie

        # Potrzebujemy min. 2 harvests żeby mieć sens
        if rec["harvest_count"] < 2:
            return 1.0

        avg_q = rec["avg_quality"]
        if avg_q >= 0.75:
            return _BOOST_HIGH
        elif avg_q >= 0.55:
            return _BOOST_MED
        elif avg_q >= 0.35:
            return 1.0  # neutral
        elif avg_q >= 0.25:
            return _BOOST_LOW
        else:
            return _BOOST_VERY_LOW

    def get_reputation_summary(self, url: str) -> dict:
        """Pełne info o reputacji — do logów/debuggingu."""
        domain = _extract_domain(url)
        rec = self._load(domain)
        if not rec:
            return {"domain": domain, "status": "new", "boost": 1.0}
        return {
            "domain":      domain,
            "status":      "blacklisted" if rec["blacklisted"] else "known",
            "avg_quality": rec["avg_quality"],
            "harvests":    rec["harvest_count"],
            "consecutive_bad": rec["consecutive_bad"],
            "boost":       self.get_score_boost(url),
        }

    # ─────────────────────────────────────────────────────────────────────
    # PUBLIC WRITE API
    # ─────────────────────────────────────────────────────────────────────

    def update(self, url: str, quality_score: float, success: bool = True):
        """
        Rejestruje wynik harvestu dla domeny.
        Wywoływać po każdym _harvest_one() w HarvesterBee.
        Zapis jest lazy — flush() commituje do DB.
        """
        domain = _extract_domain(url)
        is_bad = not success or quality_score < _BAD_QUALITY_THRESHOLD

        if domain in self._pending_updates:
            # Akumuluj w ramach tej misji
            p = self._pending_updates[domain]
            p["qualities"].append(quality_score)
            p["successes"].append(success)
            p["consecutive_bad"] = p["consecutive_bad"] + 1 if is_bad else 0
        else:
            # Ładuj istniejące lub inicjuj nowe
            rec = self._load(domain)
            base_count   = rec["harvest_count"] if rec else 0
            base_quality = rec["avg_quality"]   if rec else 0.5
            base_consec  = rec["consecutive_bad"] if rec else 0

            self._pending_updates[domain] = {
                "base_count":   base_count,
                "base_quality": base_quality,
                "qualities":    [quality_score],
                "successes":    [success],
                "consecutive_bad": base_consec + 1 if is_bad else 0,
                "blacklisted":  rec["blacklisted"] if rec else False,
                "blacklist_reason": rec["blacklist_reason"] if rec else "",
            }

        # Aktualizuj cache
        if domain in self._cache:
            self._cache[domain]["consecutive_bad"] = (
                self._cache[domain]["consecutive_bad"] + 1 if is_bad else 0
            )

    def flush(self):
        """
        Commituje wszystkie pending updates do DuckDB.
        Wywołaj po zakończeniu harvest misji.
        """
        if not self._pending_updates:
            return

        blacklisted_new = []
        records = []

        for domain, p in self._pending_updates.items():
            new_harvests  = len(p["qualities"])
            new_successes = sum(1 for s in p["successes"] if s)
            total_harvests = p["base_count"] + new_harvests
            total_successes = (
                int(p["base_count"] * 0.7) + new_successes  # aprox prior successes
            )

            # Nowa średnia jakości (exponential moving average — faworyzuje nowe dane)
            if p["base_count"] > 0:
                alpha = min(0.4, new_harvests / (p["base_count"] + new_harvests))
                new_avg = (1 - alpha) * p["base_quality"] + alpha * (
                    sum(p["qualities"]) / new_harvests
                )
            else:
                new_avg = sum(p["qualities"]) / new_harvests

            # Blacklist decision — grooming activation
            blacklisted = p["blacklisted"]
            blacklist_reason = p["blacklist_reason"]

            if not blacklisted:
                consec_bad = p["consecutive_bad"]
                if (
                    total_harvests >= _BLACKLIST_MIN_HARVESTS
                    and consec_bad >= _BLACKLIST_CONSECUTIVE
                ):
                    blacklisted = True
                    blacklist_reason = f"consecutive_bad={consec_bad} (≥{_BLACKLIST_CONSECUTIVE})"
                    blacklisted_new.append(domain)
                elif (
                    total_harvests >= 6
                    and new_avg < _BLACKLIST_AVG_QUALITY
                ):
                    blacklisted = True
                    blacklist_reason = f"chronic_low_quality={new_avg:.2f} (<{_BLACKLIST_AVG_QUALITY})"
                    blacklisted_new.append(domain)

            records.append({
                "domain":           domain,
                "avg_quality":      round(new_avg, 4),
                "harvest_count":    total_harvests,
                "success_count":    total_successes,
                "consecutive_bad":  p["consecutive_bad"],
                "blacklisted":      blacklisted,
                "blacklist_reason": blacklist_reason,
                "now":              datetime.now(timezone.utc),
                "last_bad_at":      datetime.now(timezone.utc) if p["consecutive_bad"] > 0 else None,
            })

        try:
            con = _duckdb_connect()
            con.execute("BEGIN")
            for r in records:
                con.execute("""
                    INSERT INTO hive_domain_reputation
                        (domain, avg_quality, harvest_count, success_count,
                         last_harvest_at, last_bad_at, consecutive_bad,
                         blacklisted, blacklist_reason, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (domain) DO UPDATE SET
                        avg_quality      = excluded.avg_quality,
                        harvest_count    = excluded.harvest_count,
                        success_count    = excluded.success_count,
                        last_harvest_at  = excluded.last_harvest_at,
                        last_bad_at      = CASE WHEN excluded.last_bad_at IS NOT NULL
                                           THEN excluded.last_bad_at
                                           ELSE hive_domain_reputation.last_bad_at END,
                        consecutive_bad  = excluded.consecutive_bad,
                        blacklisted      = excluded.blacklisted,
                        blacklist_reason = excluded.blacklist_reason,
                        updated_at       = excluded.updated_at
                """, [
                    r["domain"], r["avg_quality"], r["harvest_count"],
                    r["success_count"], r["now"], r["last_bad_at"],
                    r["consecutive_bad"], r["blacklisted"],
                    r["blacklist_reason"], r["now"],
                ])
            con.execute("COMMIT")
            con.close()

            if blacklisted_new:
                log.info(f"🐝 Grooming: {len(blacklisted_new)} domain(s) blacklisted: {', '.join(blacklisted_new[:5])}")
            log.info(f"🐝 Domain reputation: flushed {len(records)} domain(s)")
            self._pending_updates.clear()

        except Exception as e:
            log.error(f"Domain reputation flush failed: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # REPORTING
    # ─────────────────────────────────────────────────────────────────────

    def report(self) -> str:
        """Drukuje raport reputacji — top bad + top good domains."""
        try:
            con = _duckdb_connect(read_only=True)
            total = con.execute("SELECT COUNT(*) FROM hive_domain_reputation").fetchone()[0]
            blacklisted = con.execute(
                "SELECT COUNT(*) FROM hive_domain_reputation WHERE blacklisted=TRUE"
            ).fetchone()[0]
            top_bad = con.execute("""
                SELECT domain, avg_quality, harvest_count, blacklist_reason
                FROM hive_domain_reputation
                WHERE harvest_count >= 2
                ORDER BY avg_quality ASC LIMIT 10
            """).fetchall()
            top_good = con.execute("""
                SELECT domain, avg_quality, harvest_count
                FROM hive_domain_reputation
                WHERE harvest_count >= 3
                ORDER BY avg_quality DESC LIMIT 10
            """).fetchall()
            con.close()

            lines = [
                f"\n🐝 DOMAIN REPUTATION REPORT",
                f"   Total tracked: {total} domains | Blacklisted: {blacklisted}",
                f"\n   TOP TRUSTED (boost {_BOOST_HIGH}x):",
            ]
            for d, q, h, *_ in top_good:
                boost = _BOOST_HIGH if q >= 0.75 else _BOOST_MED
                lines.append(f"     {d:<45} avg={q:.2f}  harvests={h}  boost={boost}x")
            lines.append(f"\n   TOP UNTRUSTED (penalty/blacklisted):")
            for d, q, h, *rest in top_bad:
                reason = rest[0] if rest else ""
                boost = self.get_score_boost(f"https://{d}/")
                lines.append(f"     {d:<45} avg={q:.2f}  harvests={h}  boost={boost}x  {reason[:40]}")
            return "\n".join(lines)
        except Exception as e:
            return f"Domain reputation report error: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# SINGLETON — jeden per run
# ─────────────────────────────────────────────────────────────────────────────
_global_rep: Optional[DomainReputation] = None


def get_reputation() -> DomainReputation:
    global _global_rep
    if _global_rep is None:
        _global_rep = DomainReputation()
    return _global_rep


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    rep = DomainReputation()
    if "--report" in sys.argv or len(sys.argv) == 1:
        print(rep.report())
    elif "--test" in sys.argv:
        # Symulacja grooming behavior
        test_urls = [
            "https://spamfarm123.com/article",
            "https://reuters.com/finance",
            "https://clutch.co/reviews",
            "https://g2.com/products",
        ]
        print("Testing domain reputation system...")
        rep.update("https://spamfarm123.com/article", 0.05, success=False)
        rep.update("https://spamfarm123.com/article", 0.08, success=False)
        rep.update("https://spamfarm123.com/article", 0.03, success=False)
        rep.update("https://reuters.com/finance", 0.92, success=True)
        rep.update("https://reuters.com/finance", 0.88, success=True)
        rep.flush()
        for url in test_urls:
            skip = rep.should_skip(url)
            boost = rep.get_score_boost(url)
            print(f"  {url[:50]:50s}  skip={skip}  boost={boost:.2f}x")
        print(rep.report())
    elif "--blacklist" in sys.argv:
        # ręczne blacklistowanie
        try:
            idx = sys.argv.index("--blacklist")
            domain = sys.argv[idx + 1]
            reason = sys.argv[idx + 2] if len(sys.argv) > idx + 2 else "manual"
            con = _duckdb_connect()
            con.execute(_SCHEMA_SQL)
            con.execute("""
                INSERT INTO hive_domain_reputation
                    (domain, blacklisted, blacklist_reason, updated_at)
                VALUES (?, TRUE, ?, NOW())
                ON CONFLICT (domain) DO UPDATE SET
                    blacklisted=TRUE, blacklist_reason=excluded.blacklist_reason,
                    updated_at=excluded.updated_at
            """, [domain, reason])
            con.close()
            print(f"✓ Blacklisted: {domain} ({reason})")
        except (IndexError, Exception) as e:
            print(f"Usage: python3 hive2_domain_reputation.py --blacklist domain.com [reason]")
            print(f"Error: {e}")
