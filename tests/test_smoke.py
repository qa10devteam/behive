"""
BeHive Smoke Tests — verifies install → import → serve → research path.

Run: pytest tests/ -v
"""

import ast
import os
import sys
import subprocess

import pytest


# ─── Test 1: Package imports cleanly ──────────────────────────────────────────

def test_import_behive():
    """behive package imports without error."""
    import behive
    assert hasattr(behive, "__version__")
    assert behive.__version__ == "0.2.2"


def test_import_client():
    """Client module imports cleanly."""
    from behive.client import BeHiveClient
    client = BeHiveClient(api_url="http://localhost:8091")
    assert client.api_url == "http://localhost:8091"


def test_import_engine_modules():
    """All engine modules parse without syntax errors."""
    engine_dir = os.path.join(os.path.dirname(__file__), "..", "src", "behive", "engine")
    engine_dir = os.path.abspath(engine_dir)

    errors = []
    for f in os.listdir(engine_dir):
        if f.endswith(".py"):
            path = os.path.join(engine_dir, f)
            with open(path) as fh:
                try:
                    ast.parse(fh.read())
                except SyntaxError as e:
                    errors.append(f"{f}: line {e.lineno}: {e.msg}")

    assert len(errors) == 0, f"Syntax errors in engine: {errors}"


# ─── Test 2: Compat shims work ───────────────────────────────────────────────

def test_compat_shims_install():
    """Legacy import shims install and DB_BACKEND is accessible."""
    from behive.compat.shims import install_ops_shim, install_shims
    install_ops_shim()
    install_shims()

    import hive2_db
    assert hive2_db.DB_BACKEND == "postgres"


def test_compat_ops_shim():
    """hive2_ops.* shim returns empty operations lists."""
    from behive.compat.shims import install_ops_shim
    install_ops_shim()

    from hive2_ops.tier1_recon import TIER1_OPERATIONS
    assert TIER1_OPERATIONS == []


# ─── Test 3: CLI entry point works ───────────────────────────────────────────

def test_cli_version():
    """'behive version' returns version string."""
    result = subprocess.run(
        ["behive", "version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "0.2.2" in result.stdout


def test_cli_help():
    """'behive --help' shows available commands."""
    result = subprocess.run(
        ["behive", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "serve" in result.stdout
    assert "research" in result.stdout


# ─── Test 4: No secrets in source ────────────────────────────────────────────

def test_no_hardcoded_secrets():
    """No passwords or private keys in source code."""
    src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
    src_dir = os.path.abspath(src_dir)

    forbidden = ["hive_2026_prod", "sk-ant-", "ghp_", "/home/ubuntu"]
    violations = []

    for root, dirs, files in os.walk(src_dir):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                with open(path) as fh:
                    content = fh.read()
                    for secret in forbidden:
                        if secret in content:
                            violations.append(f"{path}: contains '{secret}'")

    assert len(violations) == 0, f"Secrets found: {violations}"


# ─── Test 5: Server integration (requires DB) ────────────────────────────────

@pytest.mark.skipif(
    not os.environ.get("BEHIVE_INTEGRATION_TESTS"),
    reason="Set BEHIVE_INTEGRATION_TESTS=1 to run server tests (requires PostgreSQL)"
)
def test_server_starts():
    """Server starts and /health responds."""
    import urllib.request
    import time
    import signal

    proc = subprocess.Popen(
        ["behive", "serve", "--port", "19091", "--mcp-port", "19090"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        time.sleep(3)  # Wait for startup
        resp = urllib.request.urlopen("http://localhost:19091/health", timeout=5)
        assert resp.status == 200
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
