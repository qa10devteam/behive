"""A3: real primer SQL and JSON(B) roundtrips, never a DB double."""
import uuid

from psycopg2.extras import Json
import pytest

from behive.engine import queen_primer
from test_repair1022_support import live_db

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness


@pytest.mark.parametrize('schema_kind', ['live_defaultless_jsonb', 'declared_json'])
def test_primer_save_load_roundtrip_has_explicit_text_id(live_db, schema_kind):
    if schema_kind == 'declared_json':
        with live_db.conn.cursor() as cur:
            cur.execute('DROP TABLE hive_queen_primers')
            cur.execute(queen_primer._SCHEMA_SQL)
        live_db.conn.commit()
    with live_db.conn.cursor() as cur:
        cur.execute("INSERT INTO hive_content (id,mission_id,url,raw_text) VALUES (1,'prior','https://energy.example.test/report',%s)", ('Documented solar storage research. ' * 40,))
        cur.execute("INSERT INTO hive_entities (id,mission_id,value,entity_type) VALUES (1,'prior','Solar Works','company')")
        cur.execute("INSERT INTO hive_gaps (id,mission_id,gap_query,gap_type,priority) VALUES (1,'prior','Solar storage capacity outlook','coverage',8)")
    live_db.conn.commit()
    queen_primer.QueenPrimer('prior').save('Solar storage market', quality_score=0.8)
    with live_db.conn.cursor() as cur:
        cur.execute('SELECT id,top_domains FROM hive_queen_primers')
        row = cur.fetchone()
        assert row is not None, 'Saving must not rely on a missing production id default'
        assert str(uuid.UUID(row[0])) == row[0]
        assert row[1] == ['energy.example.test']
    primer = queen_primer.QueenPrimer('next').load_for_topic('Solar storage market')
    assert primer is not None, 'Load must accept psycopg2-decoded JSON and JSONB arrays'
    assert primer.mission_id == 'prior'
    assert primer.top_domains == ['energy.example.test']
    assert primer.recommended_queries == ['Solar storage capacity outlook']
    assert primer.key_entities == ['Solar Works']
    assert primer.depth_modifier == pytest.approx(0.85)
    assert primer.primer_quality == pytest.approx(0.8)
    assert 'energy.example.test' in primer.to_prompt_block()


@pytest.mark.parametrize('max_age,expected_mission', [(7, None), (30, 'recent'), (60, 'old')])
def test_primer_load_uses_requested_age_and_excludes_current_mission(live_db, max_age, expected_mission):
    with live_db.conn.cursor() as cur:
        for mission, days, keywords in [
            ('next', 0, ['solar', 'storage', 'market']),
            ('unrelated', 0, ['aviation']),
            ('recent', 10, ['solar', 'market']),
            ('old', 40, ['solar', 'storage', 'market']),
        ]:
            cur.execute("""INSERT INTO hive_queen_primers
                (id,mission_id,topic,topic_keywords,top_domains,recommended_queries,key_entities,depth_modifier,primer_quality,created_at)
                VALUES (%s,%s,'Solar storage market',%s,%s,%s,%s,1.0,0.8,NOW() - %s * INTERVAL '1 day')""",
                (str(uuid.uuid4()), mission, Json(keywords), Json(['energy.example.test']), Json([]), Json([]), days))
    live_db.conn.commit()
    primer = queen_primer.QueenPrimer('next').load_for_topic('Solar storage market', max_age_days=max_age)
    if expected_mission is None:
        assert primer is None
    else:
        assert primer is not None, 'Matching live JSONB primer must load with the supplied age limit'
        assert primer.mission_id == expected_mission
