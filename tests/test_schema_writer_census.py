"""Offline AST census and private PostgreSQL replay; never load conftest.py.

Seams: SQL extraction/column admission (static, no imports of runtime code), and
real PostgreSQL INSERTs against verbatim live-shaped DDL (no provider calls).
Run with the authorized run_existing_pg_regressions.py socket-only PG16 harness.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

import psycopg2
from psycopg2.extensions import parse_dsn
from psycopg2.extras import execute_batch
import pytest

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path('/root/outputs/behive-repair-2026-10-22/census')
PHASE = os.environ.get('SCHEMA_CENSUS_PHASE', 'green')
AFFECTED = ('hive_gaps', 'hive_rag_eval')
WRITERS = ('gap_batch', 'gap_fallback', 'rag_eval')


def _tool():
    spec = importlib.util.spec_from_file_location("schema_census", ROOT / "tools/schema_census.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ast_census_extracts_insert_and_upsert_columns():
    source = '''
def save(con):
    con.execute("""
        INSERT INTO hive_gaps (mission_id, gap_query, gap_type, priority)
        VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING
    """, ["m", "q", "research", 5])
    con.execute("INSERT INTO hive_facts (id, mission_id) VALUES (nextval('hive_facts_seq'), ?)", ["m"])
'''
    writers = _tool().scan_source(source, "sample.py")["writers"]
    assert len(writers) == 2
    assert writers[0]["table"] == "hive_gaps"
    assert writers[0]["columns"] == ["mission_id", "gap_query", "gap_type", "priority"]
    assert writers[0]["id_supplied"] is False
    assert writers[0]["line"] == 4
    assert writers[1]["id_supplied"] is True
    assert writers[1]["values"][0] == "nextval('hive_facts_seq')"


def test_ast_census_resolves_module_constant_fstring_and_flags_dynamic():
    source = '''
RAG_EVAL_TABLE = "hive_rag_eval"
def save(con, id_sql):
    con.execute(f"INSERT INTO {RAG_EVAL_TABLE} (mission_id) VALUES (?)", ["m"])
    con.execute(f"INSERT INTO hive_claims (id, mission_id) VALUES ({id_sql}, %s)", ["m"])
'''
    writers = _tool().scan_source(source, "dynamic.py")["writers"]
    assert writers[0]["table"] == "hive_rag_eval"
    assert writers[0]["id_supplied"] is False
    assert writers[1]["id_supplied"] is True
    assert writers[1]["dynamic_expressions"] == ["id_sql"]


def test_ast_census_retains_positional_insert_and_copy():
    source = '''
def save(cur):
    cur.execute("INSERT INTO hive_queen_assessments VALUES (?, ?, CURRENT_TIMESTAMP)", ["id", "m"])
    cur.copy_expert("COPY hive_gaps (id, mission_id, gap_query) FROM STDIN", stream)
'''
    writers = _tool().scan_source(source, "positional.py")["writers"]
    assert writers[0]["columns"] is None
    assert writers[0]["positional"] is True
    assert writers[0]["values"] == ["?", "?", "CURRENT_TIMESTAMP"]
    assert writers[1]["operation"] == "COPY"
    assert writers[1]["columns"] == ["id", "mission_id", "gap_query"]


def test_ast_census_does_not_count_docstrings_or_readonly_copy():
    source = '''
def save(con):
    """INSERT INTO hive_gaps (mission_id) is an example, not SQL."""
    con.execute("COPY hive_gaps TO STDOUT")
    con.execute("SELECT * FROM hive_gaps")
'''
    assert _tool().scan_source(source, "no_writes.py")["writers"] == []


def test_ast_census_retains_unresolved_table_and_column_templates():
    source = '''
def insert_batch(cur, table, col_list, placeholders):
    sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
    cur.execute(sql)
'''
    writer, = _tool().scan_source(source, "template.py")["writers"]
    assert writer["table"] == "{table}"
    assert writer["columns"] is None
    assert writer["dynamic_expressions"] == ["table", "col_list", "placeholders"]


def test_ast_census_retains_nonliteral_sql_sinks_for_manual_review():
    source = '''
def wrapper(cur, sql, stream):
    cur.execute(sql, [1])
    cur.copy_from(stream, "hive_gaps", columns=("id", "mission_id"))
'''
    result = _tool().scan_source(source, "sinks.py")
    assert [(c['api'], c['line']) for c in result['nonliteral_sql_calls']] == [('execute', 3), ('copy_from', 4)]


def _private_dsn():
    """Hard fail instead of skipping or falling back to a production URL."""
    dsn = os.environ['CLAIM_TEST_PG_DSN']
    assert dsn == os.environ['BEHIVE_DB_URL'] == os.environ['DATABASE_URL']
    parts = parse_dsn(dsn)
    assert parts['host'].startswith('/tmp/behive-claim-pg-repair-')
    assert Path(parts['host']).is_dir()
    assert parts['port'] == '55438'
    assert parts['dbname'] == 'postgres'
    assert parts['user'] == 'postgres'
    return dsn


def _dump_object(dump, kind, name):
    pattern = rf'CREATE {kind} public\.{re.escape(name)}\b[\s\S]*?;'
    match = re.search(pattern, dump)
    assert match is not None, f'Object absent from live schema dump: {name}'
    return match.group(0)


@pytest.fixture
def pg():
    dsn = _private_dsn()
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = True
    tables = ('hive_gaps', 'hive_rag_eval', 'hive_facts', 'hive_claims')
    sequences = ('hive_facts_seq', 'hive_gaps_seq')
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT current_database(), inet_server_addr(), current_setting(\'server_version_num\')::int')
            row = cur.fetchone()
            assert row is not None
            database, tcp, version = row
            assert database == 'postgres' and tcp is None and 160000 <= version < 170000
            for table in tables:
                cur.execute(f'DROP TABLE IF EXISTS public.{table} CASCADE')
            for sequence in sequences:
                cur.execute(f'DROP SEQUENCE IF EXISTS public.{sequence} CASCADE')
            dump = (EVIDENCE / 'hive_schema_live_before.sql').read_text()
            for sequence in sequences:
                cur.execute(_dump_object(dump, 'SEQUENCE', sequence))
            for table in tables:
                cur.execute(_dump_object(dump, 'TABLE', table))
                for match in re.finditer(rf'ALTER TABLE ONLY public\.{table}\n[\s\S]*?;', dump):
                    # Keep the real primary/unique/check/FK declarations, not a
                    # permissive invented table. These four have no external FK.
                    cur.execute(match.group(0))
            # Private fixtures only. Put maxima beyond the fresh sequence START
            # and on all three shared consumers to exercise forward alignment.
            cur.execute("INSERT INTO public.hive_gaps (id, mission_id, gap_query) VALUES (70000, 'baseline', 'existing')")
            cur.execute("INSERT INTO public.hive_facts (id, mission_id) VALUES (80000, 'baseline')")
            cur.execute("INSERT INTO public.hive_claims (id, mission_id) VALUES (90000, 'baseline')")
        yield conn
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f'DROP TABLE IF EXISTS public.{table} CASCADE')
            for sequence in sequences:
                cur.execute(f'DROP SEQUENCE IF EXISTS public.{sequence} CASCADE')
        conn.close()


def _source_writer(label):
    filename = 'src/behive/engine/rag.py' if label == 'rag_eval' else 'src/behive/engine/process.py'
    source = (ROOT / filename).read_text()
    writers = _tool().scan_source(source, filename)['writers']
    if label == 'rag_eval':
        found = [w for w in writers if w['scope'] == 'evaluate_rag' and w['table'] == 'hive_rag_eval']
    else:
        sink = 'con.executemany' if label == 'gap_batch' else 'con.execute'
        found = [w for w in writers if w['scope'] == 'GapDrone.run' and w['sink'] == sink and w['table'] == 'hive_gaps']
    assert len(found) == 1
    writer = found[0]
    assert writer['id_supplied'] is False
    return writer


def _parameters(label):
    # Shapes and values follow GapDrone.run gap_rows (priority HIGH => 8) and
    # evaluate_rag's actual metrics for an empty context, top_k=10, latency=0.
    # These are labeled local input fixtures, not fabricated provider responses.
    if label == 'rag_eval':
        return ['census_rag_eval', 'Census local query', 0, 10, 0.0, 0, 0.0,
                0.0, 0.0, 0.0, False, 'P05:NO_CHUNKS']
    return ['census_' + label, 'Local gap: source coverage', 'research', 8]


def _write(conn, label):
    writer = _source_writer(label)
    sql = writer['sql'].replace('?', '%s')  # same PgConnection placeholder conversion
    params = _parameters(label)
    with conn.cursor() as cur:
        if label == 'gap_batch':
            execute_batch(cur, sql, [params])  # actual PgConnection.executemany backend
        else:
            cur.execute(sql, params)
        cur.execute(f"SELECT id FROM public.{writer['table']} WHERE mission_id = %s", (params[0],))
        rows = cur.fetchall()
        assert len(rows) == 1 and rows[0][0] is not None
        return rows[0][0]


def _apply(name, expect_success=True):
    path = EVIDENCE / name
    assert path.is_file()
    result = subprocess.run(['psql', '-X', '--no-password', '-v', 'ON_ERROR_STOP=1',
                             '--dbname', _private_dsn(), '-f', str(path)],
                            text=True, capture_output=True, timeout=15)
    if expect_success:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0
    return result


def _default(conn, table):
    with conn.cursor() as cur:
        cur.execute("SELECT column_default FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s AND column_name='id'", (table,))
        row = cur.fetchone()
        assert row is not None
        return row[0]


def _sequence(conn):
    with conn.cursor() as cur:
        cur.execute('SELECT last_value, is_called FROM public.hive_facts_seq')
        return cur.fetchone()


def _assert_not_null_failure(conn, label):
    with pytest.raises(psycopg2.errors.NotNullViolation) as failure:
        _write(conn, label)
    writer = _source_writer(label)
    assert failure.value.diag.table_name == writer['table']
    assert failure.value.diag.column_name == 'id'
    return {'sqlstate': failure.value.pgcode, 'table': failure.value.diag.table_name,
            'column': failure.value.diag.column_name}


@pytest.mark.parametrize('label', WRITERS)
def test_each_omitted_id_writer_before_up_after_up_and_after_down(pg, label):
    writer = _source_writer(label)
    assert _default(pg, writer['table']) is None
    if PHASE == 'red':
        # Run this node with SCHEMA_CENSUS_PHASE=red to retain a genuine failing
        # pre-migration execution, not merely pytest.raises-based RED evidence.
        _write(pg, label)
        return
    before = _assert_not_null_failure(pg, label)
    _apply('migration_up.sql')
    generated = _write(pg, label)
    assert _default(pg, writer['table']) is not None
    if label != 'rag_eval':
        assert generated > 90000
    else:
        import uuid
        assert str(uuid.UUID(generated)) == generated
    seq_after_write = _sequence(pg)
    _apply('migration_down.sql')
    assert all(_default(pg, table) is None for table in AFFECTED)
    # Reset only our local row; prove the identical writer fails again.
    with pg.cursor() as cur:
        cur.execute(f"DELETE FROM public.{writer['table']} WHERE mission_id=%s", (_parameters(label)[0],))
    after_down = _assert_not_null_failure(pg, label)
    assert _sequence(pg) == seq_after_write  # rollback never rewinds allocator
    evidence = {'writer': {k: writer[k] for k in ('file', 'line', 'scope', 'sink', 'sql_sha256')},
                'before': before, 'after_up_generated_id': generated,
                'after_down': after_down, 'sequence_after_write_and_down': seq_after_write}
    path = EVIDENCE / 'test_evidence' / (label + '.json')
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2) + '\n')


def test_migrations_are_idempotent_and_down_preserves_sequence(pg):
    _apply('migration_up.sql')
    first = {table: _default(pg, table) for table in AFFECTED}
    first_seq = _sequence(pg)
    _apply('migration_up.sql')
    assert {table: _default(pg, table) for table in AFFECTED} == first
    assert _sequence(pg) == first_seq
    _apply('migration_down.sql')
    _apply('migration_down.sql')
    assert all(_default(pg, table) is None for table in AFFECTED)
    assert _sequence(pg) == first_seq


def test_shared_gap_allocator_interoperates_with_every_explicit_gap_writer(pg):
    _apply('migration_up.sql')
    generated = [_write(pg, 'gap_batch')]
    tool = _tool()
    found = []
    for filename in ('src/behive/engine/orchestrator.py', 'src/behive/engine/prescout.py'):
        found.extend(w for w in tool.scan_source((ROOT / filename).read_text(), filename)['writers']
                     if w['table'] == 'hive_gaps' and w['id_supplied'])
    # 2 since 2026-11-13: prescout's quorum-expansion INSERT went through the removed
    # _ddb2/hive2 modules, never executed, and was deleted (ruff F821 fix).
    assert len(found) == 2
    with pg.cursor() as cur:
        for index, writer in enumerate(found):
            assert writer['values'][0] == "nextval('hive_facts_seq')"
            cur.execute(writer['sql'].replace('?', '%s'), ['explicit_gap_' + str(index), 'Local gap query'])
            cur.execute('SELECT id FROM public.hive_gaps WHERE mission_id=%s', ('explicit_gap_' + str(index),))
            generated.append(cur.fetchone()[0])
    generated.append(_write(pg, 'gap_fallback'))
    assert len(set(generated)) == 4
    assert generated == sorted(generated)
    assert "nextval('hive_facts_seq'::regclass)" == _default(pg, 'hive_gaps')
    with pg.cursor() as cur:
        cur.execute('SELECT last_value, is_called FROM public.hive_gaps_seq')
        assert cur.fetchone() == (34301, False)  # unused DDL allocator remains untouched


def test_forward_alignment_never_rewinds_a_sequence_already_ahead(pg):
    with pg.cursor() as cur:
        cur.execute("SELECT setval('public.hive_facts_seq', 200000, true)")
    _apply('migration_up.sql')
    assert _sequence(pg) == (200000, True)
    assert _write(pg, 'gap_batch') == 200001


def test_forward_alignment_handles_uncalled_sequence(pg):
    with pg.cursor() as cur:
        cur.execute("SELECT setval('public.hive_facts_seq', 90000, false)")
    _apply('migration_up.sql')
    assert _sequence(pg) == (90001, True)
    assert _write(pg, 'gap_batch') == 90002


@pytest.mark.parametrize('migration', ('migration_up.sql', 'migration_down.sql'))
def test_migration_refuses_unreviewed_defaults_atomically(pg, migration):
    with pg.cursor() as cur:
        cur.execute("ALTER TABLE public.hive_rag_eval ALTER COLUMN id SET DEFAULT 'installation-owned'")
    original = _sequence(pg)
    result = _apply(migration, expect_success=False)
    assert 'unreviewed default' in result.stderr
    assert _default(pg, 'hive_gaps') is None
    assert _default(pg, 'hive_rag_eval') == "'installation-owned'::text"
    assert _sequence(pg) == original


def test_stale_ddl_gap_allocator_can_collide_with_explicit_runtime_writer(pg):
    """Both sequences exist: blindly restoring CREATE TABLE's default is unsafe."""
    with pg.cursor() as cur:
        cur.execute("SELECT setval('public.hive_gaps_seq', 90000, true)")
        cur.execute("SELECT setval('public.hive_facts_seq', 90000, true)")
        cur.execute("ALTER TABLE public.hive_gaps ALTER COLUMN id SET DEFAULT nextval('public.hive_gaps_seq')")
    assert _write(pg, 'gap_batch') == 90001
    filename = 'src/behive/engine/orchestrator.py'
    explicit = next(w for w in _tool().scan_source((ROOT / filename).read_text(), filename)['writers']
                    if w['table'] == 'hive_gaps' and w['id_supplied'])
    with pg.cursor() as cur, pytest.raises(psycopg2.errors.UniqueViolation) as failure:
        cur.execute(explicit['sql'].replace('?', '%s'), ['explicit_collision_control', 'Local gap query'])
    assert failure.value.diag.constraint_name == 'hive_gaps_pkey'


def test_integer_capacity_failure_rolls_back_both_defaults_without_sequence_change(pg):
    with pg.cursor() as cur:
        cur.execute("SELECT setval('public.hive_facts_seq', 2147483647, true)")
    original = _sequence(pg)
    result = _apply('migration_up.sql', expect_success=False)
    assert 'integer ID capacity exhausted' in result.stderr
    assert all(_default(pg, table) is None for table in AFFECTED)
    assert _sequence(pg) == original


def test_live_required_column_census_and_final_migration_writer_coverage():
    tool = _tool()
    metadata = json.loads((EVIDENCE / 'live_metadata.json').read_text())
    required = tool.required_columns(metadata)
    assert len([c for c in required if c['column_name'] == 'id']) == 29
    assert len([c for c in required if c['column_name'] != 'id']) == 68
    scanned = tool.scan_repository(ROOT, metadata)
    assert scanned['parse_errors'] == []
    assert not [w for w in scanned['writers']
                if any(c != 'id' for c in w['omitted_required_columns'] or [])]
    omitted = {(w['table'], w['scope'], w['sink']) for w in scanned['writers']
               if w['omitted_required_columns']}
    assert omitted == {('hive_gaps', 'GapDrone.run', 'con.executemany'),
                       ('hive_gaps', 'GapDrone.run', 'con.execute'),
                       ('hive_rag_eval', 'evaluate_rag', 'con.execute')}, (
        'Re-scan concurrent edits: QueenPrimer must supply its UUID; any new omission requires review', omitted)
    dynamic = {(w['file'], w['scope']) for w in scanned['writers'] if '{' in w['table']}
    assert dynamic == {('src/behive/engine/db.py', 'insert_batch'),
                       ('src/behive/engine/db.py', 'upsert_batch')}
    assert not [call for call in scanned['nonliteral_sql_calls']
                if call['api'] in ('insert_batch', 'upsert_batch', 'copy', 'copy_from', 'copy_expert',
                                   'copy_records_to_table', 'copy_to_table')]


def test_migration_changes_only_the_two_proven_id_defaults(pg):
    def columns():
        with pg.cursor() as cur:
            cur.execute("SELECT table_name, column_name, data_type, is_nullable, column_default "
                        "FROM information_schema.columns WHERE table_schema='public' ORDER BY 1, 2")
            return {(row[0], row[1]): row[2:] for row in cur.fetchall()}
    before = columns()
    _apply('migration_up.sql')
    after = columns()
    assert before.keys() == after.keys()
    changed = [key for key in before if before[key] != after[key]]
    assert set(changed) == {(table, 'id') for table in AFFECTED}
    for key in changed:
        assert before[key][:2] == after[key][:2]
        assert before[key][2] is None and after[key][2] is not None
    _apply('migration_down.sql')
    assert columns() == before
