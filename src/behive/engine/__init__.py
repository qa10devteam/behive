"""
BeHive Engine — core research pipeline.

Architecture:
    orchestrator.py  — full pipeline orchestration (scout→harvest→process→synth)
    scout.py         — query generation, API routing, source discovery
    harvest.py       — URL fetching via 8-layer stealth drone cascade
    process.py       — claim extraction, LLM-powered analysis, quality scoring
    synth.py         — report synthesis, cross-referencing, deduplication
    drones.py        — 8-layer evasion stack
    api_scout.py     — 70+ API source router (125 endpoints)
    quality.py       — content quality scoring (0.0-1.0)
    db.py            — PostgreSQL connection management
    events.py        — event bus for real-time progress tracking

Optional (graceful degradation if deps missing):
    bionic_quorum.py     — multi-LLM consensus verification
    domain_reputation.py — source credibility scoring
    preprocessor.py      — query understanding and expansion
    prescout.py          — pre-flight reconnaissance
    queen_primer.py      — research planning and strategy
    queen_memory.py      — cross-mission knowledge persistence
    queen_calibrator.py  — quality threshold calibration
    queen_fact_mem.py    — fact deduplication memory
    guardrail.py         — output safety and validation

Usage:
    # Install compat shims before importing engine modules
    from behive.compat.shims import install_ops_shim, install_shims
    install_ops_shim()
    install_shims()

    # Then import what you need
    from behive.engine.db import get_pg_connection
    from behive.engine.orchestrator import run_mission
"""

__all__ = [
    "orchestrator",
    "scout",
    "harvest",
    "process",
    "synth",
    "drones",
    "api_scout",
    "quality",
    "db",
    "events",
]
