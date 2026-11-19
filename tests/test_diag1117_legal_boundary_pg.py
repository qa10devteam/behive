"""Case 10 of the NIS2 legal-sources design: the balance of the 59 sources the mission lost.

Boundary test: private PostgreSQL (live_db) plus a real child process running the harvest CLI
(behive.engine.harvest.main) with a recorded legal transport (tests/fixtures/legal_boundary/) and no
network. The 59 rows are SYNTHETIC rows with the shape of the real NIS2 mission (hosts, score
bands, domain blacklist); no production row or text is copied.

Before the legal fix (deployed code): 44 rows under 5/15 are never loaded and the 15 rows over it
sit on blacklisted domains and are groomed: 0 of 59 harvested. After the fix every row ends in
exactly one bucket and the skip_summary line adds up:

  * eur-lex.europa.eu CELEX rows (above 5/15, and the below ones once rescored by the Polish title
    of the CELEX) are harvested by method cellar although the domain stays blacklisted, with
    hive_content ids from hive_content_seq;
  * sip.lex.pl rows end as byok_gap, news.google.com / gov.pl / smart.gov.pl / Sejm /processes/
    rows stay score_filter, ELI 901 and print 2608 are scored by the scorer on their (criminal
    law) metadata title, never forced through;
  * nothing is requested from eur-lex.europa.eu and no row leaks to a generic extractor.

The test avoids importing behive.engine.legal_sources at module level, so on the deployed code it
fails on an assertion (the bucket balance), not on an ImportError.
"""
import importlib
import json
import os
import re
import subprocess
import sys
import time
import warnings
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import pytest

from test_repair1022_support import live_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.pg_harness

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "legal_boundary"
CHILD = FIXTURES / "child_harvest.py"
TRANSPORT = FIXTURES / "transport.json"

M = "boundary-nis2-mission"
OTHER_M = "boundary-other-mission"
SEQ_START = 700
DECOY_CONTENT_ID = 9000   # a legacy row of another mission: MAX(id)+1 would be 9001, nextval gives 700
TOPIC = (
    "ustawa o krajowym systemie cyberbezpieczeństwa po NIS2, firmy produkcyjne jako "
    "podmioty kluczowe lub ważne, obowiązki, terminy, kary"
)
KPK_SNIPPET = "o zmianie ustawy - Kodeks postępowania karnego oraz ustawy - Kodeks karny skarbowy"
PROCESS_SNIPPET = '{"ELI": "DU/2024/1228", "title": "o zmianie ustawy"}'
EN_TITLE = "Synthetic English title of an act on measures for a high common level of cybersecurity"
CELEX_QUERY = "?uri=CELEX:32022L2555"
EL = "https://eur-lex.europa.eu/legal-content"
PRINT_URL = "https://api.sejm.gov.pl/sejm/term10/prints/2608/2608.pdf"
ELI_URL = "https://api.sejm.gov.pl/eli/acts/DU/2026/901"

BLACKLISTED = {   # domain -> consecutive_bad (the blacklist predates the mission)
    "news.google.com": 3,
    "eur-lex.europa.eu": 3,
    "api.sejm.gov.pl": 7,
    "sip.lex.pl": 3,
}
GROUP_ORDER = ["news", "eurlex_above", "eurlex_below", "sejm_print", "sejm_eli", "sejm_process",
               "lex", "gov", "smart"]
GROUP_SIZES = {"news": 27, "eurlex_above": 10, "eurlex_below": 10, "sejm_print": 1, "sejm_eli": 1,
               "sejm_process": 4, "lex": 3, "gov": 2, "smart": 1}


def _hp(url: str) -> str:
    """Host+path title, the way scout stored URL-like titles."""
    parsed = urlparse(url)
    return parsed.netloc + parsed.path


def _rows() -> list[dict]:
    """The 59 synthetic rows: group, url, title, snippet, source_type, relevance, total."""
    rows: list[dict] = []

    def add(group, url, title, rel, total, snippet="", stype="web"):
        rows.append(dict(group=group, url=url, title=title, snippet=snippet, source_type=stype,
                         rel=rel, total=total))

    for i in range(1, 28):                                   # news.google.com: 27 below, 0 / 14
        add("news", f"https://news.google.com/rss/articles/synthetic-{i:02d}?hl=pl",
            f"Synthetic headline {i:02d}", 0, 14)

    above = [("PL", "HTML"), ("PL", "PDF"), ("EN", "PDF"), ("DE", "PDF"), ("FR", "PDF"),
             ("ES", "PDF"), ("IT", "PDF"), ("NL", "PDF"), ("CS", "PDF"), ("SK", "PDF")]
    bands = [(12, 34), (12, 36), (6, 25), (18, 40), (6, 27), (12, 31), (6, 26), (18, 38), (6, 29), (12, 33)]
    for (lang, fmt), (rel, total) in zip(above, bands):      # 10 CELEX rows over 5/15
        add("eurlex_above", f"{EL}/{lang}/TXT/{fmt}/{CELEX_QUERY}", "Publications Office", rel, total,
            stype="pdf" if fmt == "PDF" else "web")
    below = [("EN", "TXT"), ("EN", "TXT/HTML"), ("DE", "TXT"), ("FR", "TXT"), ("ES", "TXT"),
             ("IT", "TXT"), ("NL", "TXT"), ("CS", "TXT"), ("SK", "TXT"), ("HU", "TXT")]
    for n, (lang, fmt) in enumerate(below):                  # 10 CELEX rows under 5/15, 0 / 14
        url = f"{EL}/{lang}/{fmt}/{CELEX_QUERY}"
        add("eurlex_below", url, EN_TITLE if n < 5 else _hp(url), 0, 14)

    add("sejm_print", PRINT_URL, _hp(PRINT_URL), 6, 34, stype="pdf")            # over 5/15
    add("sejm_eli", ELI_URL, _hp(ELI_URL), 6, 28, snippet=KPK_SNIPPET)          # over 5/15
    for n in (209, 2271, 2443, 3040):                                          # 4 below
        url = f"https://api.sejm.gov.pl/sejm/term10/processes/{n}"
        add("sejm_process", url, _hp(url), 0, 14, snippet=PROCESS_SNIPPET)

    for name, rel, total in (("roz-14", 18, 37), ("roz-3", 18, 37), ("doc-000000001", 6, 23)):
        add("lex", f"https://sip.lex.pl/synthetic/ksc/{name}", f"Synthetic LEX chapter {name}", rel, total)

    for i in (1, 2):                                                           # gov.pl, below, not blacklisted
        add("gov", f"https://www.gov.pl/web/synthetic/page-{i}", f"Synthetic gov.pl page {i}", 0, 14)
    smart = "https://smart.gov.pl/synthetic/lista-podmiotow.pdf"               # smart.gov.pl PDF, below
    add("smart", smart, _hp(smart), 0, 14, stype="pdf")
    return rows


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _domain(url: str) -> str:
    host = _host(url)
    return host[4:] if host.startswith("www.") else host


def _passes(rel, total) -> bool:
    return rel is not None and total is not None and rel >= 5 and total >= 15


def _legal_sources():
    """behive.engine.legal_sources, or None on a tree without the legal fix."""
    try:
        return importlib.import_module("behive.engine.legal_sources")
    except ImportError:
        return None


def _seed(live_db, rows):
    from behive.engine import domain_reputation

    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute(f"CREATE SEQUENCE hive_content_seq START {SEQ_START}")
        cur.execute(domain_reputation._SCHEMA_SQL)
        for dom, bad in BLACKLISTED.items():
            cur.execute(
                "INSERT INTO hive_domain_reputation (domain, blacklisted, blacklist_reason, consecutive_bad, "
                "harvest_count, avg_quality) VALUES (%s, TRUE, %s, %s, 8, 0.77)",
                (dom, f"consecutive_bad={bad} (≥3)", bad))
        cur.execute("INSERT INTO hive_missions (id, topic) VALUES (%s, %s)", (M, TOPIC))
        for r in rows:
            cur.execute(
                "INSERT INTO hive_sources (id, mission_id, url, domain, title, snippet, source_type, "
                "score_relevance, score_freshness, score_authority, score_depth, score_total, status) "
                "VALUES (nextval('hive_sources_id_seq'), %s, %s, %s, %s, %s, %s, %s, 3, 4, 2, %s, 'scouted')",
                (M, r["url"], _domain(r["url"]), r["title"], r["snippet"], r["source_type"], r["rel"], r["total"]))
        cur.execute(
            "INSERT INTO hive_content (id, mission_id, url, domain, raw_text, word_count, harvest_method) "
            "VALUES (%s, %s, 'https://legacy.example.test/x', 'legacy.example.test', 'legacy', 1, 'web')",
            (DECOY_CONTENT_ID, OTHER_M))


def _sources(live_db):
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute("SELECT id, url, title, score_relevance, score_total, status FROM hive_sources "
                    "WHERE mission_id = %s ORDER BY id", (M,))
        return {r[1]: dict(id=r[0], title=r[2], rel=r[3], total=r[4], status=r[5]) for r in cur.fetchall()}


def _content(live_db):
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute("SELECT id, url, harvest_method, word_count FROM hive_content WHERE mission_id = %s "
                    "ORDER BY id", (M,))
        return cur.fetchall()


def _reputation(live_db):
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute("SELECT domain, blacklisted, blacklist_reason FROM hive_domain_reputation ORDER BY domain")
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def _run_child(live_db, tmp_path):
    spy_path = tmp_path / "spy.json"
    # explicit environment: nothing of the service environment reaches the child
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
                                      "LITELLM_LOCAL_MODEL_COST_MAP", "VIRTUAL_ENV", "TMPDIR")
           if k in os.environ}
    env.update({
        "BEHIVE_BACKEND": "postgres",
        "BEHIVE_DB_URL": live_db.dsn,
        "LEGAL_BOUNDARY_DSN": live_db.dsn,
        "LEGAL_BOUNDARY_TRANSPORT": str(TRANSPORT),
        "LEGAL_BOUNDARY_SPY": str(spy_path),
        "BEHIVE_PHASE_DEADLINE": str(time.time() + 400),   # as run_phase sets it for a harvest phase
    })
    proc = subprocess.run([sys.executable, str(CHILD), M], env=env, cwd=str(FIXTURES.parents[2]),
                          capture_output=True, text=True, timeout=300)
    spy = json.loads(spy_path.read_text()) if spy_path.exists() else None
    return proc, spy


_SKIP = re.compile(r"^skip mission=(\S+) reason=(\S+) url=(\S+) detail=(.*)$")
_SUMMARY = re.compile(r"^skip_summary mission=(\S+) loaded=(\d+) groomed=(\d+) skipped=(\d+)(.*)$")


def _parse_log(stderr: str):
    skips: dict[str, list[tuple[str, str]]] = {}
    harvesting: Counter = Counter()
    summaries, rescore = [], []
    for line in stderr.splitlines():
        if not line.startswith("hive2.harvest INFO "):
            continue
        msg = line[len("hive2.harvest INFO "):]
        if (m := _SKIP.match(msg)) and m.group(1) == M:
            skips.setdefault(m.group(3), []).append((m.group(2), m.group(4)))
        elif msg.startswith("Harvesting: "):
            harvesting[msg[len("Harvesting: "):]] += 1
        elif (m := _SUMMARY.match(msg)) and m.group(1) == M:
            counts = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", m.group(5))}
            summaries.append(dict(loaded=int(m.group(2)), groomed=int(m.group(3)),
                                  skipped=int(m.group(4)), counts=counts))
        elif msg.startswith("legal_rescore mission=" + M + " candidates="):
            rescore.append(msg)
    return skips, harvesting, summaries, rescore


def _bucket_table(rows, buckets, final):
    names = sorted(set(buckets.values()))
    width = max([len(n) for n in names] + [6])
    head = f"{'group':<13}{'rows':>5}  " + "  ".join(f"{n:>{width}}" for n in names)
    lines = [head]
    for group in GROUP_ORDER:
        urls = [r["url"] for r in rows if r["group"] == group]
        tally = Counter(buckets[u] for u in urls)
        lines.append(f"{group:<13}{len(urls):>5}  " + "  ".join(f"{tally.get(n, 0):>{width}}" for n in names))
    total = Counter(buckets.values())
    lines.append(f"{'TOTAL':<13}{len(rows):>5}  " + "  ".join(f"{total.get(n, 0):>{width}}" for n in names))
    scores = [f"{r['group']}: rel {r['rel']}->{final[r['url']]['rel']} total {r['total']}->{final[r['url']]['total']}"
              for r in rows if r["group"] in ("eurlex_below", "sejm_print", "sejm_eli")]
    return "\n".join(lines) + "\nrescored rows (stored -> after run):\n  " + "\n  ".join(scores)


def test_nis2_mission_balance_on_the_real_harvest_child(live_db, tmp_path):
    rows = _rows()
    by_url = {r["url"]: r for r in rows}
    assert len(rows) == len(by_url) == 59
    assert Counter(r["group"] for r in rows) == GROUP_SIZES

    # --- the mission as it stood before the fix: 44 under 5/15, 15 over it on blacklisted domains ---
    old_filter = [r for r in rows if not _passes(r["rel"], r["total"])]
    old_groomed = [r for r in rows if _passes(r["rel"], r["total"]) and _domain(r["url"]) in BLACKLISTED]
    assert (len(old_filter), len(old_groomed)) == (44, 15)
    assert len(old_filter) + len(old_groomed) == 59      # old code: 0 harvested

    _seed(live_db, rows)
    before = _sources(live_db)
    rep_before = _reputation(live_db)
    assert len(before) == 59 and set(rep_before) == set(BLACKLISTED)

    proc, spy = _run_child(live_db, tmp_path)
    assert proc.returncode == 0 and spy is not None, proc.stderr[-3000:]
    skips, harvesting, summaries, rescore = _parse_log(proc.stderr)

    after = _sources(live_db)
    content = _content(live_db)
    method_by_url = {c[1]: c[2] for c in content}

    # --- every row ends in exactly one bucket ---
    buckets: dict[str, str] = {}
    for url in by_url:
        parts = []
        if url in method_by_url:
            parts.append(f"harvested:{method_by_url[url]}")
        parts += [f"skip:{reason}" for reason, _ in skips.get(url, [])]
        buckets[url] = parts[0] if len(parts) == 1 else ("UNACCOUNTED" if not parts else "MULTIPLE:" + "+".join(parts))
    table = _bucket_table(rows, buckets, after)
    warnings.warn("legal boundary bucket table (mission shape of 59 rows):\n" + table, UserWarning)
    print("\n" + table)
    assert all(b not in ("UNACCOUNTED",) and not b.startswith("MULTIPLE") for b in buckets.values()), \
        "rows not in exactly one bucket:\n" + table
    assert sum(1 for b in buckets.values() if b.startswith("harvested:")) + \
        sum(1 for b in buckets.values() if b.startswith("skip:")) == 59

    # --- the recorded transport: nothing left the process, nothing leaked to a generic extractor ---
    assert spy["network"] == [], spy["network"]
    assert spy["generic"] == [], spy["generic"]
    assert spy["legal_sources"] is True
    assert spy["fetch"], "no legal fetch was made"
    assert not any("eur-lex.europa.eu" in f["fetch_url"] for f in spy["fetch"])      # CELLAR, never the WAF host
    assert {f["id"] for f in spy["fetch"] if f["kind"] == "celex"} == {"32022L2555"}
    assert all(f["recorded"] for f in spy["fetch"]), spy["fetch"]
    assert any(f["kind"] == "eli" and f["id"] == "DU/2026/901" for f in spy["fetch"])    # metadata title for 901
    assert any(f["kind"] == "print" and f["id"] == "10/2608" for f in spy["fetch"])      # and for print 2608

    # --- hard spec rows ---
    for r in rows:
        url, group, b = r["url"], r["group"], buckets[r["url"]]
        if group == "eurlex_above":
            assert b == "harvested:cellar", (url, b)
        elif group == "lex":
            assert b == "skip:byok_gap", (url, b)
            assert skips[url] == [("byok_gap", "lex_login_required")]
        elif group in ("news", "gov", "smart", "sejm_process"):
            assert b == "skip:score_filter", (url, b)
            assert skips[url] == [("score_filter", f"relevance={r['rel']} total={r['total']}")]
        if b != "harvested:cellar" and group.startswith("eurlex"):
            assert b == "skip:score_filter", (url, b)           # a CELEX row is never groomed
        if group in ("sejm_print", "sejm_eli"):
            # not forced through: scored by the scorer on the metadata title, only ever lowered
            assert after[url]["rel"] <= r["rel"], (url, r["rel"], after[url]["rel"])
            if after[url]["rel"] < 5:
                assert after[url]["total"] <= 14 and b == "skip:score_filter", (url, after[url], b)
            else:
                assert b == ("harvested:sejm_print" if group == "sejm_print" else "harvested:eli"), (url, b)
        # not harvested means never fetched: no "Harvesting:" line, the row stays scouted
        if b.startswith("skip:") and b != "skip:legal_fetch_empty":
            assert harvesting[url] == 0 or b == "skip:byok_gap", (url, b)   # LEX reaches the worker, then no fetch
            assert after[url]["status"] == "scouted", (url, after[url])
        if b.startswith("harvested:"):
            assert harvesting[url] == 1 and after[url]["status"] == "done", (url, after[url])
    assert not any(url for url, b in buckets.items() if b == "skip:domain_blacklist"
                   and (by_url[url]["group"].startswith("eurlex") or by_url[url]["group"] == "lex"))

    # --- the CELEX rows under 5/15: whatever the real scorer decides is what the row gets ---
    ls = _legal_sources()
    assert ls is not None, "behive.engine.legal_sources is missing: no legal fix in this tree"
    celex_meta = json.loads(TRANSPORT.read_text())["celex"]["32022L2555"]["title"]
    meta = {ELI_URL: json.loads(TRANSPORT.read_text())["eli"]["DU/2026/901"]["title"],
            PRINT_URL: json.loads(TRANSPORT.read_text())["print"]["10/2608"]["title"]}
    for r in rows:
        url = r["url"]
        if ls.celex_from_url(url):
            want = ls.rescore_legal_source(TOPIC, url, r["title"], r["snippet"], celex_meta, source_type=r["source_type"])
        elif url in meta:
            want = ls.rescore_legal_source(TOPIC, url, r["title"], r["snippet"], meta[url], source_type=r["source_type"])
        else:
            assert (after[url]["rel"], after[url]["total"], after[url]["title"]) == \
                (before[url]["rel"], before[url]["total"], before[url]["title"]), url   # nothing else rescored
            continue
        assert (after[url]["rel"], after[url]["total"]) == (want["score_relevance"], want["score_total"]), url
        want_bucket = ("skip:score_filter" if not _passes(want["score_relevance"], want["score_total"])
                       else "harvested:" + ("cellar" if ls.celex_from_url(url) else
                                            "eli" if ls.eli_from_url(url) else "sejm_print"))
        assert buckets[url] == want_bucket, (url, buckets[url], want_bucket)

    # --- skip_summary: one line, per-reason counts add up, harvested + skipped = 59 ---
    assert len(summaries) == 1, summaries
    summary = summaries[0]
    per_url = Counter(b[len("skip:"):] for b in buckets.values() if b.startswith("skip:"))
    harvested = [b for b in buckets.values() if b.startswith("harvested:")]
    assert summary["skipped"] == sum(summary["counts"].values()) == sum(per_url.values())
    for reason, n in per_url.items():
        assert summary["counts"].get(reason) == n, (reason, summary, per_url)
    assert summary["counts"].get("domain_blacklist", 0) == per_url.get("domain_blacklist", 0) == summary["groomed"]
    assert len(harvested) + summary["skipped"] == 59, (len(harvested), summary)
    assert summary["loaded"] == 59 - per_url.get("score_filter", 0)        # loaded = everything past the 5/15 filter
    assert per_url["byok_gap"] == 3 and per_url["score_filter"] >= 27 + 4 + 2 + 1
    assert rescore and "candidates=" in rescore[0]

    # --- hive_content: method cellar, ids from hive_content_seq (not MAX(id)+1 = 9001) ---
    assert [c[2] for c in content if c[1] in by_url] == [c[2] for c in content]
    n = len(content)
    assert n == len(harvested) >= 10
    assert sorted(c[0] for c in content) == list(range(SEQ_START, SEQ_START + n)), [c[0] for c in content]
    assert Counter(c[2] for c in content if _host(c[1]) == "eur-lex.europa.eu") == \
        Counter({"cellar": sum(1 for r in rows if r["group"].startswith("eurlex") and
                               buckets[r["url"]] == "harvested:cellar")})
    assert all(c[3] >= 50 for c in content)
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute("SELECT last_value, is_called FROM hive_content_seq")
        assert cur.fetchone() == (SEQ_START + n - 1, True)
        cur.execute("SELECT id FROM hive_content WHERE mission_id = %s", (OTHER_M,))
        assert cur.fetchall() == [(DECOY_CONTENT_ID,)]

    # --- the 59 rows are the same rows: no insert, no delete, ids unchanged ---
    assert {u: v["id"] for u, v in after.items()} == {u: v["id"] for u, v in before.items()}

    # --- blacklisted domains stay blacklisted, with their reason; the exception is not an amnesty ---
    rep_after = _reputation(live_db)
    for dom in BLACKLISTED:
        assert rep_after[dom] == rep_before[dom] == (True, f"consecutive_bad={BLACKLISTED[dom]} (≥3)"), dom
    assert not any(rep_after.get(d, (False, ""))[0] for d in ("gov.pl", "www.gov.pl", "smart.gov.pl"))
