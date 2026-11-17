"""Offline tests for behive.sr_bridge: no PG, no network, no SR, no subprocess."""
import logging
import subprocess
from types import SimpleNamespace

import pytest
import requests

from behive import sr_bridge


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


CLAIMS = [
    {"claim": "claim one " * 5, "confidence": 0.8, "source": "behive:m1"},
    {"claim": "claim two " * 5, "confidence": 0.7, "source": "behive:m1"},
]


@pytest.fixture
def env(monkeypatch):
    """Patch DB helpers + HTTP; forbid subprocesses. Returns a recorder."""
    rec = SimpleNamespace()
    rec.marked = []
    rec.posts = []
    rec.gets = []
    rec.health = FakeResponse(200, {
        "occupied": 12371,
        "encoder": {"attached": True, "warmed": True, "n": 12371, "dim": 384, "model": "m"},
    })
    rec.already = False
    rec.claims = list(CLAIMS)

    def no_subprocess(*a, **k):
        raise AssertionError("subprocess must not be started by the bridge")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)

    monkeypatch.setattr(sr_bridge, "_ensure_bridge_table", lambda: None)
    monkeypatch.setattr(sr_bridge, "_was_already_bridged", lambda mid: rec.already)

    class _ProductClaims(list):
        from_product = True

    monkeypatch.setattr(sr_bridge, "_fetch_mission_claims", lambda mid: _ProductClaims(rec.claims))
    monkeypatch.setattr(sr_bridge, "_mark_bridged",
                        lambda mid, n, resp: rec.marked.append((mid, n, resp)))
    monkeypatch.setattr(sr_bridge.time, "sleep", lambda s: None)

    def fake_post(url, **kw):
        rec.posts.append((url, kw))
        if "reload-encoder" in url:
            raise AssertionError("reload-encoder must not be called")
        return FakeResponse(200, {"ingested": len(kw["json"]["claims"])})

    def fake_get(url, **kw):
        rec.gets.append((url, kw))
        if isinstance(rec.health, Exception):
            raise rec.health
        return rec.health

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(sr_bridge.requests, "post", fake_post)
    monkeypatch.setattr(sr_bridge.requests, "get", fake_get)
    return rec


# ─── encoder verification via bridge_mission ───────────────────────────────

def test_happy_path_verifies_encoder_without_subprocess(env):
    result = sr_bridge.bridge_mission("m1")
    assert result["claims_sent"] == 2
    assert result["encoder"]["ok"] is True
    assert result["encoder"] == {"ok": True, "attached": True, "n": 12371, "occupied": 12371}
    assert len(env.marked) == 1
    mid, sent, resp = env.marked[0]
    assert (mid, sent) == ("m1", 2)
    assert resp["encoder"]["ok"] is True
    assert len(env.gets) == 1
    assert env.gets[0][1]["timeout"] == 10
    assert all("reload-encoder" not in u for u, _ in env.posts)


def test_mismatch_is_not_ok_warns_but_still_bridges(env, caplog):
    env.health = FakeResponse(200, {
        "occupied": 100, "encoder": {"attached": True, "n": 90},
    })
    with caplog.at_level(logging.WARNING, logger="behive-sr-bridge"):
        result = sr_bridge.bridge_mission("m1")
    assert result["encoder"]["ok"] is False
    assert result["encoder"]["n"] == 90 and result["encoder"]["occupied"] == 100
    assert len(env.marked) == 1
    assert env.marked[0][2]["encoder"]["ok"] is False
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_not_attached_is_not_ok(env):
    env.health = FakeResponse(200, {"occupied": 5, "encoder": {"attached": False, "n": 5}})
    assert sr_bridge._verify_encoder()["ok"] is False


def test_health_connection_error_never_raises(env, caplog):
    env.health = requests.exceptions.ConnectionError("down")
    with caplog.at_level(logging.WARNING, logger="behive-sr-bridge"):
        result = sr_bridge.bridge_mission("m1")
    assert result["encoder"] == {"ok": False, "error": "ConnectionError"}
    assert env.marked[0][2]["encoder"]["ok"] is False
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_health_http_500(env):
    env.health = FakeResponse(500, None, "boom")
    result = sr_bridge.bridge_mission("m1")
    assert result["encoder"] == {"ok": False, "error": "http_500"}
    assert len(env.marked) == 1


def test_health_bad_json(env):
    env.health = FakeResponse(200, ValueError("bad json"))
    assert sr_bridge._verify_encoder() == {"ok": False, "error": "ValueError"}


def test_health_missing_fields(env):
    env.health = FakeResponse(200, {})
    out = sr_bridge._verify_encoder()
    assert out["ok"] is False


def test_already_bridged_skips_health(env):
    env.already = True
    assert sr_bridge.bridge_mission("m1") is None
    assert env.gets == [] and env.posts == [] and env.marked == []


def test_no_claims_skips_health(env):
    env.claims = []
    assert sr_bridge.bridge_mission("m1") is None
    assert env.gets == [] and env.posts == []
    assert env.marked == [("m1", 0, {"skipped": "no_claims"})]


def test_health_url_default():
    assert sr_bridge.SR_HEALTH_URL.endswith("/health")


def test_module_has_no_subprocess_dependency():
    assert not hasattr(sr_bridge, "subprocess")
    assert not hasattr(sr_bridge, "_sync_encoder")


# ─── _fetch_mission_claims filtering ───────────────────────────────────────

class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self):
        return self.cur

    def close(self):
        pass


def test_fetch_skips_json_blobs_and_low_quality(monkeypatch):
    rows = [
        ("A good factual claim about something real", 0.9, 0.9, "u", "t"),
        ('{"json": "blob that is long enough to pass length"}', 0.9, 0.9, "u", "t"),
        ('  [1, 2, 3, "list blob long enough to pass length"]', 0.9, 0.9, "u", "t"),
        ("Low quality claim that is long enough here", 0.9, 0.3, "u", "t"),
        ("Low combined confidence claim long enough", 0.1, 0.45, "u", "t"),
        ("Claim with null scores uses defaults okay", None, None, "u", "t"),
    ]
    monkeypatch.setattr(sr_bridge, "_get_db", lambda: FakeConn(rows))
    out = sr_bridge._fetch_mission_claims("mX")
    texts = [c["claim"] for c in out]
    assert texts == ["A good factual claim about something real",
                     "Claim with null scores uses defaults okay"]
    assert out[0]["source"] == "behive:mX"
    assert out[0]["confidence"] == 0.9
    assert out[1]["confidence"] == 0.6


# ─── CLI ───────────────────────────────────────────────────────────────────

def test_main_all(monkeypatch, capsys):
    monkeypatch.setattr(sr_bridge, "bridge_all_pending", lambda: [
        {"mission_id": "a", "claims_sent": 1, "claims_fetched": 2}])
    sr_bridge.main(["--all"])
    out = capsys.readouterr().out
    assert "Bridged 1 missions" in out and "a: 1/2 claims" in out


def test_main_single_mission(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(sr_bridge, "bridge_mission",
                        lambda m: seen.append(m) or {"mission_id": m, "encoder": {"ok": True}})
    sr_bridge.main(["m42"])
    assert seen == ["m42"]
    assert '"mission_id": "m42"' in capsys.readouterr().out


def test_main_single_mission_none(monkeypatch, capsys):
    monkeypatch.setattr(sr_bridge, "bridge_mission", lambda m: None)
    sr_bridge.main(["m42"])
    assert "Already bridged or no claims" in capsys.readouterr().out


def test_main_usage(capsys):
    sr_bridge.main([])
    assert "Usage" in capsys.readouterr().out
