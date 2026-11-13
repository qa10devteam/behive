"""Regex fallback claims must respect the process relevance decision (2026-10-23).

Live mission hive_1792744015_96a356: the only harvested page (an education journal at
pressto.amu.edu.pl) was rejected by RelevanceFilter ("0 pass / 1 skip"), yet
claims_fallback.salvage_claims_from_content wrote 3 regex claims from it and the mission
ended `done`. Fixtures are read-only captures of production hive_content.
"""
import json
from pathlib import Path

from behive.engine import claims_fallback
from test_repair1022_support import live_db
import pytest

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness

FIX = json.loads((Path(__file__).parent / "fixtures/relevance_gate/captured_content.json").read_text())
ROWS = {("pressto" if "pressto" in r["url"] else "telemetria24"): r for r in FIX["rows"]}
# Salvage reads the longest documents first; make the rejected page come first so a
# missing gate cannot pass by luck (control, not captured data).
PRESSTO_FIRST = dict(ROWS["pressto"], _order_words=10**6)
TOPIC = "Systemy telemetrii zbiorników paliw i chemikaliów w Polsce: dostawcy i wdrożenia"


def _seed(conn, mission, rows, config=None):
    with conn.cursor() as cur:
        # Live installation: default-less hive_claims ids come from hive_facts_seq.
        cur.execute("CREATE SEQUENCE IF NOT EXISTS hive_facts_seq START 1000")
        cur.execute("INSERT INTO hive_missions (id, topic, status, config) VALUES (%s, %s, 'process', %s)",
                    (mission, TOPIC, json.dumps(config) if config is not None else None))
        for i, r in enumerate(rows, 1):
            cur.execute("INSERT INTO hive_content (id, mission_id, url, title, raw_text, word_count) VALUES (%s,%s,%s,%s,%s,%s)",
                        (i, mission, r["url"], r["title"], r["raw_text"], r.get("_order_words", r["word_count"])))
    conn.commit()


def _claim_urls(conn, mission):
    with conn.cursor() as cur:
        cur.execute("SELECT source_url FROM hive_claims WHERE mission_id=%s", (mission,))
        return [r[0] for r in cur.fetchall()]


def test_gate_rejecting_every_document_yields_no_salvage(live_db):
    mission = "gate_none"
    _seed(live_db.conn, mission, [ROWS["pressto"]],
          {"relevance_gate": {"version": 1, "accepted_urls": [], "considered": 1}})
    assert claims_fallback.salvage_claims_from_content(mission) == 0
    assert _claim_urls(live_db.conn, mission) == []


def test_gate_limits_salvage_to_accepted_documents(live_db):
    mission = "gate_some"
    _seed(live_db.conn, mission, [PRESSTO_FIRST, ROWS["telemetria24"]],
          {"keep": "other config", "relevance_gate": {"version": 1, "accepted_urls": [ROWS["telemetria24"]["url"]],
                                                      "considered": 2}})
    assert claims_fallback.salvage_claims_from_content(mission) > 0
    assert set(_claim_urls(live_db.conn, mission)) == {ROWS["telemetria24"]["url"]}


def test_without_gate_salvage_requires_topic_keyword_relevance(live_db):
    """Legacy missions or a process crash before Phase A1: no recorded gate."""
    mission = "gate_absent"
    _seed(live_db.conn, mission, [PRESSTO_FIRST, ROWS["telemetria24"]])
    assert claims_fallback.salvage_claims_from_content(mission) > 0
    assert set(_claim_urls(live_db.conn, mission)) == {ROWS["telemetria24"]["url"]}


def test_without_gate_off_topic_only_mission_gets_nothing(live_db):
    mission = "gate_absent_offtopic"
    _seed(live_db.conn, mission, [ROWS["pressto"]])
    assert claims_fallback.salvage_claims_from_content(mission) == 0


def test_process_records_gate_and_preserves_other_config(live_db):
    from behive.engine.process import ProcessingDrones
    mission = "gate_record"
    _seed(live_db.conn, mission, [], {"keep": 1})
    drones = ProcessingDrones.__new__(ProcessingDrones)
    drones.mission_id = mission
    docs = [{"url": ROWS["pressto"]["url"]}, {"url": ROWS["telemetria24"]["url"]}, {"url": ""}]
    drones._record_relevance_gate(docs, [docs[1]])
    with live_db.conn.cursor() as cur:
        cur.execute("SELECT config FROM hive_missions WHERE id=%s", (mission,))
        config = cur.fetchone()[0]
    assert config["keep"] == 1
    assert config["relevance_gate"]["accepted_urls"] == [ROWS["telemetria24"]["url"]]
    assert config["relevance_gate"]["considered"] == 3
    assert config["relevance_gate"]["version"] == 1


def test_gate_round_trip_on_legacy_json_config_column(live_db):
    """orchestrator's legacy DDL declares hive_missions.config as json, not jsonb."""
    from behive.engine.process import ProcessingDrones
    with live_db.conn.cursor() as cur:
        cur.execute("ALTER TABLE hive_missions ALTER COLUMN config TYPE json USING config::json")
    live_db.conn.commit()
    mission = "gate_json"
    _seed(live_db.conn, mission, [PRESSTO_FIRST, ROWS["telemetria24"]], {"keep": True})
    drones = ProcessingDrones.__new__(ProcessingDrones)
    drones.mission_id = mission
    drones._record_relevance_gate([{"url": ROWS["pressto"]["url"]}, {"url": ROWS["telemetria24"]["url"]}],
                                  [{"url": ROWS["telemetria24"]["url"]}])
    assert claims_fallback.salvage_claims_from_content(mission) > 0
    assert set(_claim_urls(live_db.conn, mission)) == {ROWS["telemetria24"]["url"]}
