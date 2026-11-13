"""Exact mission+URL provenance with an actual isolated PostgreSQL fixture."""
import importlib
from pathlib import Path
import sys
from test_repair1022_support import live_db
import pytest

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def mod():
    assert (ROOT/'tools/eval/claims_eval.py').exists(), 'claims evaluator missing'
    return importlib.import_module('tools.eval.claims_eval')


def test_provenance_exact_and_whitespace_not_paraphrase():
    m=mod()
    assert m.provenance_check('A has 20 tanks.', ['A has 20 tanks.'])['exact'] is True
    assert m.provenance_check('A has 20 tanks.', ['A has\n20 tanks.'])['whitespace_normalized'] is True
    assert m.provenance_check('A owns 20 tanks.', ['A has 20 tanks.'])['exact'] is False
    assert m.provenance_check('', ['anything'])['exact'] is False


def test_reader_proves_readonly_and_same_mission_url(live_db):
    m=mod()
    with live_db.conn.cursor() as c:
        c.execute("INSERT INTO hive_missions(id,topic) VALUES ('m','tank telemetry'),('other','other')")
        c.execute("INSERT INTO hive_claims(id,mission_id,claim,source_url,claim_type) VALUES (1,'m','A has 20 tanks.','https://a','fact'),(2,'m','B has 50 tanks.','https://b','fact'),(3,'m','Different wording','https://a','fact'),(4,'m','Foreign mission','https://z','fact')")
        c.execute("INSERT INTO hive_content(id,mission_id,url,raw_text) VALUES (1,'m','https://a','Company A has 20 tanks. with telemetry'),(2,'m','https://c','B has 50 tanks.'),(3,'other','https://z','Foreign mission')")
    live_db.conn.commit()
    result=m.load_mission('m',dsn=live_db.dsn)
    assert result['topic']=='tank telemetry'
    assert result['read_only_verified'] is True
    assert result['isolation']=='repeatable read'
    assert len(result['items'])==4
    assert result['provenance_summary']=={'claims':4,'source_url_present':4,'same_url_content_present':2,'exact_text_present':1,'whitespace_text_present':1,'unmatched':3}
    assert result['items'][1]['provenance']['same_url_content_present'] is False
    assert result['items'][3]['provenance']['same_url_content_present'] is False
    with live_db.conn.cursor() as c:
        c.execute('SELECT count(*) FROM hive_claims')
        assert c.fetchone()[0]==4
