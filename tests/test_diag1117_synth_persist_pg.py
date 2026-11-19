"""Queen._persist_to_db must not touch the hive3_* tables missing in production.

Private socket PostgreSQL with live-shaped DDL (tests/test_repair1022_support.py):
hive_queen_assessments exists, hive3_mission_memory / hive3_calibration_log /
hive3_lessons do not (as in production). The connection wrapper records every
statement and still executes it for real, so the test sees exactly what hits
the database.
"""
import json
import re

import pytest

from test_repair1022_support import live_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.pg_harness

MISSION = "persist-m1"
MISSING = ("hive3_mission_memory", "hive3_calibration_log", "hive3_lessons")

ASSESSMENTS_DDL = """CREATE TABLE hive_queen_assessments (
    id text NOT NULL,
    mission_id text,
    query text,
    queen_json text,
    must_know_insight text,
    hausdorff_score real,
    confidence_raw real,
    confidence_calibrated real,
    bee_ops_count integer,
    rag_chunks_count integer,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT hive_queen_assessments_pkey PRIMARY KEY (id),
    CONSTRAINT hive_queen_assessments_mission_id_fkey
        FOREIGN KEY (mission_id) REFERENCES hive_missions(id)
)"""

BRIEF = (
    "[CONFIRMED] Northern solar capacity reached 120 megawatts in the 2024 reporting year.\n"
    "[PROBABLE] Southern wind capacity is expected to reach 80 megawatts by 2026 at the latest.\n"
    "[PRED] Capacity will exceed 150 megawatts by 2027."
)


class _Recorder:
    """Proxy over the real PgConnection: records SQL, executes it unchanged."""

    def __init__(self, inner, log):
        self._inner = inner
        self._log = log

    def execute(self, sql, params=None):
        self._log.append(sql)
        self._inner.execute(sql, params)
        return self

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _prepare(live_db):
    with live_db.conn, live_db.conn.cursor() as cur:
        cur.execute(ASSESSMENTS_DDL)
        cur.execute(
            "INSERT INTO hive_missions (id, topic, status, created_at) "
            "VALUES (%s, 'solar', 'running', NOW())",
            (MISSION,),
        )
        for name in MISSING:
            cur.execute("SELECT to_regclass(%s)", (name,))
            assert cur.fetchone()[0] is None, name


def _synthesis():
    return {
        "brief": BRIEF,
        "confidence_raw": 0.7,
        "metadata": {"hypotheses_used": 3, "facts_used": 4, "claims_used": 5,
                     "synthesized_at": "2026-11-19T00:00:00Z"},
        "evidence_hierarchy": "E1",
        "falsification": "none",
        "fuir_entries": [
            {"type": "FACT", "text": "Northern solar capacity reached 120 megawatts."},
            {"type": "PRED", "text": "Capacity will exceed 150 megawatts by 2027.",
             "pred_conf": 0.6},
            {"type": "PREDICTION", "text": "Wind will pass 100 megawatts."},
        ],
    }


def _run_persist(live_db, monkeypatch):
    from behive.engine.synth import Queen

    _prepare(live_db)
    seen = []
    q = Queen(MISSION)
    real = q._connect_rw
    monkeypatch.setattr(q, "_connect_rw", lambda: _Recorder(real(), seen))
    q._persist_to_db(_synthesis(), {"mission": {"topic": "solar"}}, report_text="report")
    return seen


def test_persist_writes_assessment_and_never_names_missing_hive3_tables(live_db, monkeypatch):
    seen = _run_persist(live_db, monkeypatch)

    named = [s for s in seen if re.search(r"hive3_", s)]
    assert named == [], f"statements naming a hive3 table: {named}"
    for name in MISSING:
        assert not any(name in s for s in seen), name

    live_db.conn.rollback()
    with live_db.conn.cursor() as cur:
        cur.execute(
            "SELECT query, must_know_insight, confidence_raw, confidence_calibrated, "
            "bee_ops_count, rag_chunks_count, queen_json "
            "FROM hive_queen_assessments WHERE mission_id = %s",
            (MISSION,),
        )
        rows = cur.fetchall()
    live_db.conn.rollback()

    assert len(rows) == 1
    query, must_know, raw, cal, bee_ops, rag, queen_json = rows[0]
    assert query == "solar"
    assert must_know.startswith("[CONFIRMED] Northern solar")
    assert raw == pytest.approx(0.7)
    assert cal == pytest.approx(0.7 * 0.85 + 0.05)
    assert (bee_ops, rag) == (3, 9)
    assert len(json.loads(queen_json)["fuir_entries"]) == 3
    assert any("hive_queen_assessments" in s for s in seen)


def test_queen_run_path_has_no_intelligence_closure_call():
    """The dead closure hook (relative path + wrong arity) is gone from synth.py."""
    import inspect

    from behive.engine import synth

    src = inspect.getsource(synth)
    assert "IntelligenceClosure" not in src
    assert "close_mission" not in src
