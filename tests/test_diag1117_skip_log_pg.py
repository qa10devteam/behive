"""Skip log on the real boundary: private PostgreSQL, real HarvesterBee.run().

One mission with rows above and below the 5/15 thresholds, a blacklisted domain,
a drone SKIP, a PreScout skip and a cached URL. Nothing touches the network:
every loaded source ends in a skip. The INFO log must name every skipped URL once
with its reason, the summary must add up, and nobody new is skipped.
"""
import asyncio
import logging

import pytest

from test_repair1022_support import live_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.pg_harness

LOGGER = "hive2.harvest"
M = "skiplog-mission"

SEED = "https://seed.example.test/seed"            # operator_seed, relevance 0: loaded, cached
DRONE = "https://drone.example.test/page"          # passes 5/15, drone SKIP
PRESCOUT = "https://scout.example.test/page"       # passes 5/15, PreScout skip
LOW_REL = "https://low.example.test/relevance"     # relevance 3 < 5
LOW_TOTAL = "https://low.example.test/total"       # total 10 < 15
BLACK = "https://bad.example.test/page"            # passes 5/15, blacklisted domain
DONE = "https://done.example.test/page"            # status done: not pending, not a skip
FOREIGN = "https://foreign.example.test/page"      # other mission

SKIPPED = {
    LOW_REL: "score_filter",
    LOW_TOTAL: "score_filter",
    BLACK: "domain_blacklist",
    DRONE: "drone_skip",
    PRESCOUT: "prescout_skip",
    SEED: "cache_skip",
}


def _seed(conn):
    rows = [
        (1, M, SEED, "operator_seed", 0, 0, "pending"),
        (2, M, DRONE, "web", 30, 8, "scouted"),
        (3, M, PRESCOUT, "web", 31, 8, "scouted"),
        (4, M, LOW_REL, "web", 40, 3, "scouted"),
        (5, M, LOW_TOTAL, "web", 10, 9, "scouted"),
        (6, M, BLACK, "web", 25, 7, "scouted"),
        (7, M, DONE, "web", 80, 20, "done"),
        (8, "other-mission", FOREIGN, "web", 1, 1, "scouted"),
    ]
    with conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(
                "INSERT INTO hive_sources (id, mission_id, url, source_type, score_total, score_relevance, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                r,
            )


def _reputation(live_db, monkeypatch):
    from behive.engine import domain_reputation

    monkeypatch.setattr(domain_reputation, "_global_rep", None)
    rep = domain_reputation.get_reputation()  # creates the table in the private schema
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hive_domain_reputation (domain, blacklisted, blacklist_reason) "
            "VALUES ('bad.example.test', TRUE, 'captcha wall')"
        )
    return rep


def _info(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]


def test_every_skipped_url_has_an_info_line_and_summary_adds_up(live_db, monkeypatch, caplog):
    from behive.engine import harvest

    _seed(live_db.conn)
    _reputation(live_db, monkeypatch)
    monkeypatch.setattr(
        harvest,
        "_get_drone_strategy",
        lambda mission, dom: (
            {"strategy": "SKIP", "block_type": "captcha"} if dom == "drone.example.test" else {"strategy": "DIRECT"}
        ),
    )
    harvest.clear_url_cache()
    harvest._cache_url(SEED)
    caplog.set_level(logging.INFO, logger=LOGGER)

    bee = harvest.HarvesterBee(M)
    bee._prescout_skip.add(PRESCOUT)
    try:
        asyncio.run(bee.run())
    finally:
        harvest.clear_url_cache()

    info = _info(caplog)
    skip_lines = [m for m in info if m.startswith("skip mission=")]
    # one line per skipped URL, each with its fixed reason code
    assert len(skip_lines) == len(SKIPPED) == 6
    for url, reason in SKIPPED.items():
        mine = [m for m in skip_lines if f"url={url} " in m + " "]
        assert len(mine) == 1, (url, skip_lines)
        assert mine[0].startswith(f"skip mission={M} reason={reason} url={url}")
    assert any("reason=score_filter url=" + LOW_REL in m and "relevance=3 total=40" in m for m in skip_lines)
    assert any("reason=score_filter url=" + LOW_TOTAL in m and "relevance=9 total=10" in m for m in skip_lines)
    assert any("reason=domain_blacklist url=" + BLACK in m and "captcha wall" in m for m in skip_lines)
    # rows that are not skips never show up as skips
    assert not any(DONE in m or FOREIGN in m for m in skip_lines)
    # filtered and groomed URLs are never harvested
    for url in (LOW_REL, LOW_TOTAL, BLACK):
        assert f"Harvesting: {url}" not in info
    # the loaded ones still are (skip decisions stay where they were)
    for url in (SEED, DRONE, PRESCOUT):
        assert f"Harvesting: {url}" in info

    summaries = [m for m in info if m.startswith("skip_summary")]
    assert summaries == [
        f"skip_summary mission={M} loaded=4 groomed=1 skipped=6 score_filter=2 "
        "domain_blacklist=1 prescout_skip=1 drone_skip=1 cache_skip=1"
    ]
    assert bee._skip_counts == {
        "score_filter": 2, "domain_blacklist": 1, "prescout_skip": 1, "drone_skip": 1, "cache_skip": 1,
    }
    assert sum(bee._skip_counts.values()) == len(skip_lines)


def test_fallback_rows_are_loaded_not_logged_as_skips(live_db, monkeypatch, caplog):
    """When nothing passes 5/15 the existing top-100 fallback loads the rows: they are not skips."""
    from behive.engine import harvest

    with live_db.conn, live_db.conn.cursor() as cur:
        for i, (rel, tot) in enumerate([(3, 40), (9, 10)], start=1):
            cur.execute(
                "INSERT INTO hive_sources (id, mission_id, url, source_type, score_total, score_relevance, status) "
                "VALUES (%s,'fb-mission',%s,'web',%s,%s,'scouted')",
                (i, f"https://fb.example.test/{i}", tot, rel),
            )
    caplog.set_level(logging.INFO, logger=LOGGER)
    bee = harvest.HarvesterBee("fb-mission")
    try:
        sources = bee._load_pending_sources()
        bee._log_score_filter_skips(sources)
    finally:
        bee.con.close()
    assert sorted(s["url"] for s in sources) == ["https://fb.example.test/1", "https://fb.example.test/2"]
    assert not [m for m in _info(caplog) if m.startswith("skip ")]
    assert bee._skip_counts == {}
