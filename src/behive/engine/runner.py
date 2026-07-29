"""Phase runner — executes HIVE bee scripts as subprocesses with timeout.

Extracted from orchestrator.py for maintainability.
Handles detached synth processes (double-fork), timeout enforcement, and PID tracking.
"""
import os
import sys
import time
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

HIVE_DIR = Path(os.environ.get("HIVE_DIR", str(Path.home())))
PYTHON = sys.executable

def run_phase(script: str, args: list[str], phase_name: str, 
              timeout: int = 600) -> float:
    """Run a HIVE bee script as subprocess, return elapsed seconds.
    
    Args:
        timeout: Max seconds for this phase (default 600 = 10 min).
                 Synth phases get 900s. Override via BEHIVE_PHASE_TIMEOUT env.
    """
    # Allow env override for timeout
    timeout = int(os.environ.get("BEHIVE_PHASE_TIMEOUT", str(timeout)))
    script_path = str(HIVE_DIR / script)
    pid_file = f"/tmp/hive_{phase_name.replace('/', '_')}.pid"
    log_file = f"/tmp/hive_{phase_name.replace('/', '_')}_proc.log"
    cmd = [PYTHON, script_path] + args
    t0 = time.time()
    log.debug(f"\n{'='*60}")
    log.debug(f"🐝  [{phase_name.upper()}] Uruchamiam: {' '.join(cmd)}")
    log.debug(f"    PID file: {pid_file} | log: {log_file}")
    log.debug(f"{'='*60}")
    sys.stdout.flush()

    # Fazy synth: detached double-fork — hive2.py does not wait, process lives on its own
    # This allows hive2.py to exit and release the DB write lock.
    is_synth = any(k in script for k in ('master_intelligence', 'synth'))
    if is_synth:
        import shlex as _shlex
        _safe_cmd = ' '.join(_shlex.quote(str(a)) for a in cmd)
        _env_str = f"HIVE_PARENT_PID={os.getpid()}"
        # Double-fork detach: setsid + nohup, hive2.py nie robi wait
        # EXIT: marker dopisany przez bash trap — polling w hive2.py go czeka
        detach_cmd = (
            f"export {_env_str} && "
            f"setsid bash -c 'nohup {_safe_cmd} > {log_file} 2>&1; echo \"EXIT:$?\" >> {log_file}' &"
            f" echo $! > {pid_file}"
        )
        subprocess.run(["bash", "-c", detach_cmd], check=False)
        child_pid_str = ""
        try:
            with open(pid_file) as _pf:
                child_pid_str = _pf.read().strip()
        except Exception as e:
            log.debug(f"Suppressed: {e}")
        log.debug(f"  🔮 Synth detached — PID {child_pid_str}, waiting for completion…")
        sys.stdout.flush()
        # Czekamy przez polling log file na EXIT: marker (max 300s)
        _deadline = time.time() + 900  # Pass1~80s + Pass2~65s + Pass3~170s = ~315s — 900s generous margin
        _exit_code = None
        while time.time() < _deadline:
            time.sleep(3)
            try:
                with open(log_file) as _lf:
                    _content = _lf.read()
                if "EXIT:" in _content:
                    import re as _re
                    _m = _re.search(r"EXIT:(\d+)", _content)
                    _exit_code = int(_m.group(1)) if _m else 0
                    break
            except Exception as e:
                log.debug(f"Suppressed: {e}")
        # Show last lines of log
        subprocess.run(["tail", "-20", log_file], check=False)
        elapsed = time.time() - t0
        
        # If deadline exceeded and no EXIT marker → kill orphan and raise timeout
        if _exit_code is None:
            log.warning(f"  ⚠️  [{phase_name.upper()}] TIMEOUT after {elapsed:.0f}s — killing PID {child_pid_str}")
            try:
                if child_pid_str:
                    os.kill(int(child_pid_str), 9)
            except (ProcessLookupError, ValueError):
                pass
            raise subprocess.TimeoutExpired(cmd, timeout)
        
        log.info(f"✅  [{phase_name.upper()}] Zakończono w {elapsed:.1f}s (exit={_exit_code})")
        if _exit_code and _exit_code != 0:
            raise subprocess.CalledProcessError(_exit_code, cmd)
        return elapsed

    try:
        # Launch via nohup shell — survives session close
        # HIVE_PARENT_PID=os.getpid() — child can wait for parent death (DuckDB lock)
        import shlex as _shlex
        _safe_cmd = ' '.join(_shlex.quote(str(a)) for a in cmd)
        # Pass env vars to subprocess (HIVE_USE_BATCH, HIVE_BM_MANUAL, etc.)
        _extra_env = ""
        for _evar in ("HIVE_USE_BATCH", "HIVE_BM_MANUAL", "HIVE_BM_SEEDS"):
            _evalue = os.environ.get(_evar)
            if _evalue is not None:
                _extra_env += f"export {_evar}={_shlex.quote(_evalue)} && "
        nohup_cmd = (
            f"export HIVE_PARENT_PID={os.getpid()} && "
            f"{_extra_env}"
            f"nohup {_safe_cmd} > {log_file} 2>&1 & "
            f"echo $! > {pid_file} && "
            f"CHILD_PID=$(cat {pid_file}) && "
            f"wait $CHILD_PID; echo \"EXIT:$?\" >> {log_file}"
        )
        result = subprocess.run(
            ["bash", "-c", nohup_cmd],
            check=False,
            timeout=timeout,
        )
        # Tail log to stdout to maintain visibility
        subprocess.run(["grep", "-E", "⚙️|Process:|✅|❌|COMPLETE|ERROR", log_file], check=False)
        subprocess.run(["tail", "-5", log_file], check=False)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd)
    except FileNotFoundError:
        log.info(f"[HIVE] UWAGA: Skrypt {script_path} nie istnieje — faza pominięta.")
    except subprocess.TimeoutExpired:
        log.warning(f"[HIVE] ⏱️ TIMEOUT: Phase {phase_name} exceeded {timeout}s — killing.")
        # Try to kill the child process
        try:
            with open(pid_file) as _pf:
                _pid = int(_pf.read().strip())
            os.kill(_pid, 9)
        except Exception:
            pass
        raise RuntimeError(f"Phase {phase_name} timed out after {timeout}s")
    except subprocess.CalledProcessError as e:
        log.info(f"[HIVE] BŁĄD w fazie {phase_name}: exit code {e.returncode}")
        raise
    elapsed = time.time() - t0
    log.info(f"✅  [{phase_name.upper()}] Zakończono w {elapsed:.1f}s")
    return elapsed

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

