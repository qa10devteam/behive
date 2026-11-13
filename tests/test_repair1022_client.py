"""A2: execute the client result SQL on the default-less live schema."""
import asyncio
import json

import pytest

from behive.client import BeHiveClient
from test_repair1022_support import live_db

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness


def test_client_loads_live_entities_and_eligible_claims_as_json(live_db, monkeypatch):
    monkeypatch.delenv('BEHIVE_API_URL', raising=False)
    with live_db.conn.cursor() as cur:
        cur.execute("""INSERT INTO hive_claims
            (id,mission_id,claim,confidence,quality_score,source_url,claim_type,is_garbage)
            VALUES (1,'m','Published solar capacity increased.',0.9,0.85,'https://evidence.example.test/report','fact',false),
                   (2,'m','Legacy eligibility is retained.',0.7,0.6,'https://evidence.example.test/legacy','intelligence',NULL),
                   (3,'m','Discarded extraction.',0.9,1.0,'https://evidence.example.test/garbage','fact',true),
                   (4,'foreign','Other mission.',0.9,1.0,'https://foreign.example.test/','fact',false)""")
        cur.execute("""INSERT INTO hive_entities (id,mission_id,value,entity_type,confidence)
            VALUES (1,'m','Solar Works','company',0.95),
                   (2,'m','Solar Array','technology',0.8),
                   (3,'foreign','Foreign entity','company',0.99)""")
    live_db.conn.commit()
    client = BeHiveClient(db_url=live_db.dsn)
    result = asyncio.run(client._load_results('Solar research', 'm', 2.5))
    assert result.status == 'done' and result.mission_id == 'm'
    assert [c.text for c in result.claims] == ['Published solar capacity increased.', 'Legacy eligibility is retained.']
    assert result.claims[0].source_url == 'https://evidence.example.test/report'
    assert result.claims[0].claim_type == 'fact'
    assert result.claims[0].confidence == pytest.approx(0.9)
    assert result.avg_quality == pytest.approx(0.725)
    assert [(e.name, e.entity_type) for e in result.entities] == [('Solar Works', 'company'), ('Solar Array', 'technology')]
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload['entities'][0]['name'] == 'Solar Works'
    assert payload['entities'][0]['type'] == 'company'
    assert payload['metrics']['total_claims'] == 2


def test_client_keeps_entity_limit_and_confidence_order(live_db, monkeypatch):
    monkeypatch.delenv('BEHIVE_API_URL', raising=False)
    with live_db.conn.cursor() as cur:
        cur.execute("""INSERT INTO hive_entities (id,mission_id,value,entity_type,confidence)
            SELECT i,'m','Entity ' || i,'company',i / 1000.0
            FROM generate_series(1,201) AS i""")
    live_db.conn.commit()
    result = asyncio.run(BeHiveClient(db_url=live_db.dsn)._load_results('Entities', 'm', 0.0))
    assert len(result.entities) == 200
    assert result.entities[0].name == 'Entity 201'
    assert result.entities[-1].name == 'Entity 2'
    assert result.claims == []
