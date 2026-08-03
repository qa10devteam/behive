#!/usr/bin/env python3
"""Coverage-guided test amplifier.

Strategy: 
1. Run existing tests, get per-file miss ranges
2. For each missed range, read the source code
3. Generate a test that FORCES that code path to execute
4. Verify it doesn't raise (no try/except BaseException)

Targets modules with highest ROI.
"""
import subprocess
import re
import json
import sys
import os

sys.path.insert(0, "src")

PYTEST = "/home/ubuntu/stealth-research-env/bin/pytest"
TARGET_MODULES = [
    "orchestrator",  # 964 stmts, many in QueenPlanner/cmd_*
    "process",       # 1225 stmts, RelevanceFilter/extraction
    "rag",           # 907 stmts, chunk/retrieve  
    "synth",         # 945 stmts, multi_role/Queen
    "prescout",      # 646 stmts, classify/route/filter
    "harvest",       # 705 stmts, extraction
    "scout",         # 325 stmts, SwarmScout
    "master_intelligence",  # 584 stmts
]


def get_missed_lines(module: str) -> list[tuple[int, int]]:
    """Get uncovered line ranges for a module."""
    cmd = f"{PYTEST} tests/test_80_*.py --cov=behive.engine.{module} --cov-report=term-missing -q --tb=no --timeout=10 2>&1"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    for line in result.stdout.split('\n'):
        if f"{module}.py" in line:
            # Extract miss ranges like "24-25, 30-35, 47-66"
            parts = line.split()
            if len(parts) >= 4:
                miss_str = " ".join(parts[4:])  # Everything after Cover%
                ranges = []
                for r in miss_str.split(","):
                    r = r.strip()
                    if "-" in r:
                        start, end = r.split("-")
                        ranges.append((int(start), int(end)))
                    elif r.isdigit():
                        ranges.append((int(r), int(r)))
                return ranges
    return []


def read_source_lines(module: str, start: int, end: int) -> str:
    """Read source code lines."""
    path = f"src/behive/engine/{module}.py"
    with open(path) as f:
        lines = f.readlines()
    return "".join(lines[start-1:end])


if __name__ == "__main__":
    print("Analyzing coverage gaps...")
    for mod in TARGET_MODULES:
        ranges = get_missed_lines(mod)
        total_missed = sum(end - start + 1 for start, end in ranges)
        if ranges:
            print(f"\n{mod}: {total_missed} lines missed in {len(ranges)} ranges")
            # Show first 5 ranges (largest first)
            sorted_ranges = sorted(ranges, key=lambda x: x[1]-x[0], reverse=True)[:5]
            for start, end in sorted_ranges:
                size = end - start + 1
                snippet = read_source_lines(mod, start, min(start+3, end))
                first_line = snippet.split('\n')[0].strip()
                print(f"  L{start}-{end} ({size}L): {first_line[:70]}")
