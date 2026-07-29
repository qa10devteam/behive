"""Legacy import shims: hive2_* → behive.engine.*

This module installs shims into sys.modules so that legacy imports like:
    from hive2_drones import get_strategy
    import hive2_db

resolve to the actual behive.engine.* modules.

Call install_shims() early in application startup (e.g. in behive.cli or behive.server).
"""

import sys
import importlib

# Mapping: legacy module name → new module path
SHIM_MAP = {
    "hive2": "behive.engine.orchestrator",
    "hive2_db": "behive.engine.db",
    "hive2_pg": "behive.engine.db",
    "hive2_drones": "behive.engine.drones",
    "hive2_scout": "behive.engine.scout",
    "hive2_harvest": "behive.engine.harvest",
    "hive2_process": "behive.engine.process",
    "hive2_synth": "behive.engine.synth",
    "hive2_events": "behive.engine.events",
    "hive2_content_quality": "behive.engine.quality",
    "hive2_domain_reputation": "behive.engine.domain_reputation",
    "hive2_bionic_quorum": "behive.engine.bionic_quorum",
    "hive2_preprocessor": "behive.engine.preprocessor",
    "hive2_prescout": "behive.engine.prescout",
    "hive2_queen_primer": "behive.engine.queen_primer",
    "hive2_queen_memory": "behive.engine.queen_memory",
    "hive2_queen_calibrator": "behive.engine.queen_calibrator",
    "hive2_queen_fact_mem": "behive.engine.queen_fact_mem",
    "hive2_guardrail": "behive.engine.guardrail",
    "hive_api_scout": "behive.engine.api_scout",
}


def install_shims():
    """Install all legacy import shims into sys.modules (lazy — only on first use)."""
    for legacy_name, new_path in SHIM_MAP.items():
        if legacy_name not in sys.modules:
            try:
                mod = importlib.import_module(new_path)
                sys.modules[legacy_name] = mod
            except (ImportError, Exception):
                pass  # Module not available or has unmet deps — skip silently


# Also handle hive2_ops.* imports (process.py uses tiered operations)
import types


def install_ops_shim():
    """Install the hive2_ops package and submodules as empty shims."""
    # Create the top-level hive2_ops package
    if "hive2_ops" not in sys.modules:
        pkg = types.ModuleType("hive2_ops")
        pkg.__path__ = []  # Make it a package
        pkg.__package__ = "hive2_ops"
        sys.modules["hive2_ops"] = pkg

    # Pre-register all known tier submodules
    tier_names = [
        "tier1_recon", "tier2_harvest", "tier3_analysis", "tier4_validation",
        "tier5_synthesis", "tier6_warriors", "tier7_queen", "tier8_neural",
        "tier9_immune", "tier10_mycelium", "tier11_evolution", "tier12_quantum",
        "tier13_stigmergy", "tier14_resonance",
    ]
    for tier in tier_names:
        full_name = f"hive2_ops.{tier}"
        if full_name not in sys.modules:
            mod = types.ModuleType(full_name)
            mod.__package__ = "hive2_ops"
            # Each tier module exports a *_OPERATIONS list
            op_var = tier.upper().replace("TIER", "TIER") + "_OPERATIONS"
            # e.g. TIER1_OPERATIONS = []
            setattr(mod, f"TIER{tier.split('tier')[1].split('_')[0]}_OPERATIONS", [])
            sys.modules[full_name] = mod
            setattr(sys.modules["hive2_ops"], tier, mod)
