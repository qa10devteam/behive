"""SR bridge: only useful claims, and only from missions that have a product.

Private socket PostgreSQL, live-shaped DDL (tests/test_repair1022_support.py).
The HTTP boundary is patched (requests.post / requests.get on sr_bridge) so
nothing leaves the process. One done mission with a report and a mix of useful,
regex_fallback and garbage claims; one done mission without a report; one empty
done mission. A useful claim of 15 characters is not sent. A plain list
from _fetch_mission_claims (no product flag) sends nothing and records nothing.
"""
import psycopg2
import pytest

from test_repair1022_support import live_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.pg_harness

PRODUCT = "sr-product"
NO_REPORT = "sr-no-report"
EMPTY = "sr-empty"
PRODUCT_URL = "https://src.example.test/product"
NO_REPORT_URL = "https://src.example.test/no-report"

USEFUL_HI = "Solar capacity in the northern region reached 120 megawatts during the 2024 reporting year."
USEFUL_LO = "Wind capacity in the southern region reached 80 megawatts during the 2024 reporting year."
USEFUL_SHORT = "Solar is 120 MW"  # 15 characters: useful, under the SR length limit
REGEX = "Number of Pages: 158 www.example.test report 2024 extracted by the deterministic fallback."
GARBAGE = "This garbage claim about solar capacity must never be sent to Singularity Room."
UNSOURCED = "Unsourced capacity claim with no hive_sources or hive_content row for this mission."
BLOB = "{\"claim\": \"json blob long enough that only the blob rule should drop it from SR\"}"
NO_REPORT_CLAIM = "Hydro capacity in the western region reached 40 megawatts during the 2024 reporting year."


def _seed(conn):
    with conn, conn.cursor() as cur:
        cur.execute("CREATE SEQUENCE hive_claims_id_seq START 1")
        cur.execute("CREATE SEQUENCE hive_content_seq START 1")
        cur.execute(
            "INSERT INTO hive_missions (id, topic, status, synthesis, created_at) VALUES "
            "(%s, 'solar', 'done', %s, NOW()), "
            "(%s, 'solar', 'done', NULL, NOW() - INTERVAL '1 hour'), "
            "(%s, 'solar', 'done', '   ', NOW() - INTERVAL '2 hours')",
            (PRODUCT, "Report: northern solar capacity reached 120 MW in 2024.", NO_REPORT, EMPTY),
        )
        cur.execute(
            "INSERT INTO hive_sources (id, mission_id, url) VALUES (nextval('hive_sources_id_seq'), %s, %s)",
            (PRODUCT, PRODUCT_URL),
        )
        cur.execute(
            "INSERT INTO hive_content (id, mission_id, url, raw_text) "
            "VALUES (nextval('hive_content_seq'), %s, %s, %s)",
            (NO_REPORT, NO_REPORT_URL, "Documented hydro capacity report for 2024."),
        )
        claims = [
            (PRODUCT, USEFUL_HI, PRODUCT_URL, "fact", 0.8, 0.9, False),
            (PRODUCT, USEFUL_LO, PRODUCT_URL, "fact", 0.6, 0.6, False),
            (PRODUCT, USEFUL_SHORT, PRODUCT_URL, "fact", 0.99, 0.99, False),
            (PRODUCT, REGEX, PRODUCT_URL, "regex_fallback", 0.99, 0.99, False),
            (PRODUCT, GARBAGE, PRODUCT_URL, "fact", 0.99, 0.98, True),
            (PRODUCT, UNSOURCED, "https://orphan.example.test/capacity", "fact", 0.9, 0.95, False),
            (PRODUCT, BLOB, PRODUCT_URL, "fact", 0.9, 0.97, False),
            (NO_REPORT, NO_REPORT_CLAIM, NO_REPORT_URL, "fact", 0.9, 0.9, False),
        ]
        for row in claims:
            if row[1] == USEFUL_SHORT:
                assert len(row[1]) == 15
            else:
                assert len(row[1]) > 20, row[1]
            cur.execute(
                "INSERT INTO hive_claims (id, mission_id, claim, source_url, claim_type, "
                "confidence, quality_score, is_garbage) "
                "VALUES (nextval('hive_claims_id_seq'), %s, %s, %s, %s, %s, %s, %s)",
                row,
            )


def _log(conn):
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('sr_bridge_log')")
        if cur.fetchone()[0] is None:
            conn.rollback()
            return []
        cur.execute("SELECT mission_id, claims_sent FROM sr_bridge_log ORDER BY mission_id")
        rows = cur.fetchall()
    conn.rollback()
    return rows


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self.text = ""
        self._payload = payload

    def json(self):
        return self._payload


def test_sr_receives_only_useful_claims_from_product_missions(live_db, monkeypatch):
    from behive import sr_bridge
    from behive.hygiene import SQL_USEFUL_CLAIM

    _seed(live_db.conn)
    posted = []
    gets = []

    def fake_post(url, **kwargs):
        posted.append((url, kwargs))
        return _Resp(200, {"ingested": len(kwargs["json"]["claims"])})

    def fake_get(url, **kwargs):
        gets.append((url, kwargs))
        return _Resp(200, {"occupied": 1, "encoder": {"attached": True, "n": 1}})

    monkeypatch.setattr(sr_bridge, "_get_db", lambda: psycopg2.connect(live_db.dsn))
    monkeypatch.setattr(sr_bridge.requests, "post", fake_post)
    monkeypatch.setattr(sr_bridge.requests, "get", fake_get)

    live_db.conn.rollback()
    with live_db.conn.cursor() as cur:
        cur.execute(
            f"SELECT length(c.claim) FROM hive_claims c "
            f"WHERE c.mission_id = %s AND c.claim = %s AND {SQL_USEFUL_CLAIM}",
            (PRODUCT, USEFUL_SHORT),
        )
        stored = cur.fetchone()
    live_db.conn.rollback()
    assert stored == (15,)

    fetched = sr_bridge._fetch_mission_claims(PRODUCT)
    assert [c["claim"] for c in fetched] == [USEFUL_HI, USEFUL_LO]
    assert USEFUL_SHORT not in [c["claim"] for c in fetched]
    assert all(len(c["claim"]) > 20 for c in fetched)
    assert fetched[0]["source"] == f"behive:{PRODUCT}"
    assert fetched[0]["confidence"] == 0.85
    assert fetched[1]["confidence"] == 0.6
    assert sr_bridge._fetch_mission_claims(NO_REPORT) == []
    assert sr_bridge._fetch_mission_claims(EMPTY) == []

    assert sr_bridge.bridge_mission(NO_REPORT) is None
    assert sr_bridge.bridge_mission(EMPTY) is None
    assert posted == [] and gets == []
    assert _log(live_db.conn) == []

    results = sr_bridge.bridge_all_pending()
    assert [r["mission_id"] for r in results] == [PRODUCT]
    assert results[0]["claims_fetched"] == 2 and results[0]["claims_sent"] == 2
    assert len(posted) == 1
    assert posted[0][1]["json"]["claims"] == [
        {"claim": USEFUL_HI, "confidence": 0.85, "source": f"behive:{PRODUCT}"},
        {"claim": USEFUL_LO, "confidence": 0.6, "source": f"behive:{PRODUCT}"},
    ]
    sent = " ".join(c["claim"] for c in posted[0][1]["json"]["claims"])
    assert REGEX not in sent and GARBAGE not in sent and UNSOURCED not in sent and NO_REPORT_CLAIM not in sent
    assert USEFUL_SHORT not in sent
    assert _log(live_db.conn) == [(PRODUCT, 2)]

    posted.clear()
    gets.clear()
    assert sr_bridge.bridge_mission(PRODUCT) is None  # already bridged: no second send
    assert sr_bridge.bridge_all_pending() == []
    assert posted == [] and gets == []
    assert _log(live_db.conn) == [(PRODUCT, 2)]


def test_plain_list_without_product_flag_sends_nothing(live_db, monkeypatch):
    """A bare list from _fetch_mission_claims is not a product: send nothing, record nothing."""
    from behive import sr_bridge

    posted = []

    def fake_post(url, **kwargs):
        posted.append((url, kwargs))
        return _Resp(200, {"ingested": len(kwargs["json"]["claims"])})

    plain = [{"claim": USEFUL_HI, "confidence": 0.9, "source": "behive:sr-plain-list"}]
    assert getattr(plain, "from_product", None) is None

    monkeypatch.setattr(sr_bridge, "_get_db", lambda: psycopg2.connect(live_db.dsn))
    monkeypatch.setattr(sr_bridge.requests, "post", fake_post)
    monkeypatch.setattr(
        sr_bridge.requests, "get",
        lambda *a, **k: _Resp(200, {"occupied": 1, "encoder": {"attached": True, "n": 1}}),
    )
    monkeypatch.setattr(sr_bridge, "_fetch_mission_claims", lambda mid: list(plain))

    assert sr_bridge.bridge_mission("sr-plain-list") is None
    assert posted == []
    assert _log(live_db.conn) == []
