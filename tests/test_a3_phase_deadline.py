"""Harmless real subprocess verifies the deadline exported by run_phase."""
import json
import os
from pathlib import Path
import sys
import time
import uuid

import pytest

from behive.engine import orchestrator


@pytest.mark.parametrize('script,timeout,override,effective', [
    ('hive2_process.py', 600, None, 600),
    ('hive2_synth.py', 600, None, 900),
    ('hive2_process.py', 5, None, 5),
    ('hive2_process.py', 600, '7', 7),
    ('hive2_synth.py', 600, '8', 8),
])
def test_run_phase_exports_effective_wall_clock_deadline(monkeypatch, tmp_path, script, timeout, override, effective):
    tag = 'a3_deadline_' + uuid.uuid4().hex
    log = Path(f'/tmp/hive_process_{tag}_proc.log')
    pid = Path(f'/tmp/hive_process_{tag}.pid')
    # An inherited deadline is stale: every phase must get its own budget.
    monkeypatch.setenv('BEHIVE_PHASE_DEADLINE', '1')
    if override is None:
        monkeypatch.delenv('BEHIVE_PHASE_TIMEOUT', raising=False)
    else:
        monkeypatch.setenv('BEHIVE_PHASE_TIMEOUT', override)
    monkeypatch.setattr(orchestrator, '_resolve_cmd', lambda _: [sys.executable, '-c',
        'import json,os,time;print(json.dumps(dict(deadline=os.getenv("BEHIVE_PHASE_DEADLINE"),'
        'now=time.time(),parent=os.getenv("HIVE_PARENT_PID"))))'])
    before = time.time()
    try:
        orchestrator.run_phase(script, [tag], 'process', timeout=timeout)
        after = time.time()
        data = json.loads(log.read_text().splitlines()[0])
        deadline = float(data['deadline'])
        # Since 2026-11-13 (diag task 2) the child's deadline precedes the parent's
        # kill by a grace of min(max(15 s, 10%), 60 s, 25%): the phase ends itself.
        exported = effective - min(max(15.0, 0.1 * effective), 60.0, effective / 4)
        assert before + exported <= deadline <= after + exported
        assert before <= data['now'] <= after
        assert data['parent'] == str(os.getpid())
        assert 'EXIT:0' in log.read_text() and not pid.exists()
    finally:
        log.unlink(missing_ok=True)
        pid.unlink(missing_ok=True)
