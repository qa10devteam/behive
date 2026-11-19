"""Every direct psycopg2.connect of the test session goes through the guarded target (R-26).

tests/conftest.py guards the target of its own fixtures, but about 26 test files call
psycopg2.connect(host="localhost", port=5432, dbname="hive", ...) themselves. On the BeHive
host that address is the production database. The conftest therefore wraps psycopg2.connect
at session start: the legacy default target (dbname "hive" on a loopback host, a Unix socket
or no host) is redirected to BEHIVE_TEST_PG_DSN when it is set, and refused outside an
isolated network namespace otherwise. Other targets are left alone.

Offline: the underlying connect and _network_isolated are replaced; no database is touched.
"""
import psycopg2
import pytest
from psycopg2.extensions import parse_dsn

import conftest

OVERRIDE = "host=postgres port=5432 dbname=behive_test user=behive password=ci_secret_pw"
LEGACY_CALLS = [
    ((), dict(host="localhost", port=5432, dbname="hive", user="hive_app", password="legacy_pw")),
    ((), dict(host="127.0.0.1", port=5432, database="hive", user="hive_app")),
    (("host=localhost port=5432 dbname=hive user=hive_app password=legacy_pw",), {}),
    (("dbname=hive user=hive_app",), {}),
    (("postgresql://hive_app:legacy_pw@localhost:5432/hive",), {}),
    (("host=/var/run/postgresql dbname=hive user=hive_app",), {}),
]


@pytest.fixture
def real_connect(monkeypatch):
    """Stand-in for the underlying psycopg2.connect; records what reaches it."""
    calls = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return "connection"

    monkeypatch.setattr(conftest, "_REAL_PG_CONNECT", fake)
    monkeypatch.delenv("BEHIVE_TEST_PG_DSN", raising=False)
    for name in ("PGHOST", "PGDATABASE", "PGUSER"):
        monkeypatch.delenv(name, raising=False)
    fake.calls = calls
    return fake


def _params(call):
    args, kwargs = call
    return parse_dsn(args[0]) if args else kwargs


def test_wrapper_is_installed_for_the_session():
    assert psycopg2.connect is conftest._guarded_pg_connect


@pytest.mark.parametrize("args,kwargs", LEGACY_CALLS)
def test_legacy_default_is_redirected_to_the_override(monkeypatch, real_connect, args, kwargs):
    monkeypatch.setenv("BEHIVE_TEST_PG_DSN", OVERRIDE)
    monkeypatch.setattr(conftest, "_network_isolated", lambda: False)

    assert conftest._guarded_pg_connect(*args, **kwargs) == "connection"

    assert len(real_connect.calls) == 1
    (sent_args, sent_kwargs) = real_connect.calls[0]
    target = _params((sent_args, sent_kwargs)) if sent_args else None
    assert target is not None, "the redirected call passes one DSN string"
    assert (target["host"], target["dbname"], target["user"]) == ("postgres", "behive_test", "behive")
    assert target["password"] == "ci_secret_pw"


def test_redirect_keeps_connection_options_and_factories(monkeypatch, real_connect):
    monkeypatch.setenv("BEHIVE_TEST_PG_DSN", OVERRIDE)
    monkeypatch.setattr(conftest, "_network_isolated", lambda: False)
    factory = object()

    conftest._guarded_pg_connect(host="localhost", dbname="hive", user="hive_app", connect_timeout=3,
                                 cursor_factory=factory)

    (sent_args, sent_kwargs) = real_connect.calls[0]
    assert parse_dsn(sent_args[0])["connect_timeout"] == "3"
    assert sent_kwargs == {"cursor_factory": factory}


@pytest.mark.parametrize("args,kwargs", LEGACY_CALLS)
def test_legacy_default_is_refused_when_not_isolated_and_no_override(monkeypatch, real_connect, args, kwargs):
    monkeypatch.setattr(conftest, "_network_isolated", lambda: False)

    with pytest.raises(conftest.ProductionDatabaseRefused, match="isolated network namespace"):
        conftest._guarded_pg_connect(*args, **kwargs)

    assert real_connect.calls == []


def test_legacy_default_is_refused_for_a_bare_connect_when_pgdatabase_is_hive(monkeypatch, real_connect):
    monkeypatch.setenv("PGDATABASE", "hive")
    monkeypatch.setattr(conftest, "_network_isolated", lambda: False)

    with pytest.raises(conftest.ProductionDatabaseRefused):
        conftest._guarded_pg_connect()

    assert real_connect.calls == []


def test_legacy_default_passes_inside_an_isolated_namespace(monkeypatch, real_connect):
    """The PM harness: localhost:5432/hive is the private copy there."""
    monkeypatch.setattr(conftest, "_network_isolated", lambda: True)

    conftest._guarded_pg_connect(host="localhost", port=5432, dbname="hive", user="hive_app")

    assert real_connect.calls == [((), dict(host="localhost", port=5432, dbname="hive", user="hive_app"))]


@pytest.mark.parametrize("args,kwargs", [
    ((), dict(host="postgres", port=5432, dbname="behive_test", user="behive")),
    ((), dict(host="localhost", port=5432, dbname="behive_test", user="behive")),
    (("host=localhost port=5433 dbname=scratch user=x",), {}),
    (("host=db.example.test port=5432 dbname=hive user=x",), {}),
])
def test_other_targets_are_left_alone(monkeypatch, real_connect, args, kwargs):
    monkeypatch.setenv("BEHIVE_TEST_PG_DSN", OVERRIDE)
    monkeypatch.setattr(conftest, "_network_isolated", lambda: False)

    conftest._guarded_pg_connect(*args, **kwargs)

    assert real_connect.calls == [(args, kwargs)]


def test_a_call_that_already_targets_the_guarded_dsn_passes_unchanged(monkeypatch, real_connect):
    guarded = "host=localhost port=5433 dbname=hive user=hive_test password=pw"
    monkeypatch.setenv("BEHIVE_TEST_PG_DSN", guarded)
    monkeypatch.setattr(conftest, "_network_isolated", lambda: False)

    conftest._guarded_pg_connect(guarded)
    conftest._guarded_pg_connect(host="localhost", port=5433, dbname="hive", user="hive_test")

    assert real_connect.calls == [((guarded,), {}),
                                  ((), dict(host="localhost", port=5433, dbname="hive", user="hive_test"))]
