"""A4: capture the generated script, execute its actual import bootstrap.

Only external phase/scout dispatch is intercepted. The import probe executes in
another real interpreter from an unrelated cwd with no useful PYTHONPATH; it
stops before scout_apis can issue any remote request.
"""
import ast
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from behive.engine import api_scout, drones, orchestrator, prescout
from test_repair1022_support import live_db

# Needs the private socket-only PostgreSQL harness (CLAIM_TEST_PG_DSN); the main
# suite skips it with a reason (tests/conftest.py), the PG harness runs it.
pytestmark = pytest.mark.pg_harness


class HarvestDispatch(BaseException):
    pass


@pytest.mark.parametrize('inherited_pythonpath', ['', 'unrelated-relative-directory'])
def test_api_scout_generated_bootstrap_imports_parent_package(live_db, monkeypatch, tmp_path, inherited_pythonpath):
    mission = 'imports_' + uuid.uuid4().hex
    with live_db.conn.cursor() as cur:
        cur.execute("INSERT INTO hive_sources (id,mission_id,url,status,score_total,score_relevance) VALUES (1,%s,'https://scout.example.test/report','scouted',50,10)", (mission,))
    live_db.conn.commit()
    monkeypatch.delenv('HIVE_BM_SEEDS', raising=False)
    monkeypatch.setenv('PYTHONPATH', inherited_pythonpath)
    monkeypatch.setenv('BEHIVE_SCOUT_RETRIES', '1')
    monkeypatch.setattr(drones, 'run_recon', lambda *a, **k: {})
    async def failing_heads(urls):
        raise RuntimeError('controlled HEAD transport failure')
    monkeypatch.setattr(prescout, 'head_sweep', failing_heads)
    real_run = subprocess.run
    observed = {}
    def phase_subprocess(cmd, **kwargs):
        if len(cmd) > 1 and str(cmd[1]).endswith('.py'):
            source = Path(cmd[1]).read_text()
            parsed = ast.parse(source)
            topic_assignment = next(node for node in parsed.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'topic' for t in node.targets))
            bootstrap = '\n'.join(source.splitlines()[:topic_assignment.lineno - 1])
            probe = tmp_path / 'api_import_probe.py'
            probe.write_text(bootstrap + '\nimport behive.engine.api_scout as api_module\nprint(api_module.__file__)\n')
            observed['source'] = source
            observed['executable'] = cmd[0]
            observed['probe'] = real_run([cmd[0], str(probe)], capture_output=True, text=True, timeout=10, cwd=tmp_path, env=kwargs['env'])
        return subprocess.CompletedProcess(cmd, 0, stdout='[]', stderr='')
    monkeypatch.setattr(orchestrator.subprocess, 'run', phase_subprocess)
    def stop_at_harvest(*a, **k):
        raise HarvestDispatch()
    monkeypatch.setattr(orchestrator, 'run_phase', stop_at_harvest)
    try:
        with pytest.raises(HarvestDispatch):
            orchestrator._run_pipeline_phases(mission, 'Import-only research', [], {}, time.time(), False, False, 1, 'Import-only research', None, 'unused.json')
        assert observed['executable'] == sys.executable
        assert observed['probe'].returncode == 0, observed['probe'].stderr
        assert Path(observed['probe'].stdout.strip()).resolve() == Path(api_scout.__file__).resolve()
        assert '/home/ubuntu/behive-pkg/src' not in observed['source']
    finally:
        Path(f'/tmp/hive_api_scout_{mission}.py').unlink(missing_ok=True)
        Path(f'/tmp/hive_routing_{mission}.json').unlink(missing_ok=True)
