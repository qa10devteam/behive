"""
BeHive — Research engine that builds structured knowledge graphs.

Usage:
    from behive import research
    
    result = await research("NVIDIA GPU market 2025", depth=3)
    print(result.claims)  # Structured facts with quality scores
    print(result.entities)  # Named entities with types
    print(result.report)  # Synthesized markdown report
"""

try:
    from importlib.metadata import version as _get_version
    __version__ = _get_version("behive")
except Exception:
    __version__ = "0.5.1"

from behive.client import research, BeHiveClient
from behive.models import ResearchResult, Claim, Entity

__all__ = [
    "research",
    "BeHiveClient", 
    "ResearchResult",
    "Claim",
    "Entity",
]
