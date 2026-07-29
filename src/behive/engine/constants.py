"""
Shared constants for the BeHive engine.

Centralizes magic strings and config values used across multiple modules.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline stages (used in config, logging, events)
# ═══════════════════════════════════════════════════════════════════════════════
STAGE_SCOUT = "scout"
STAGE_HARVEST = "harvest"
STAGE_PROCESS = "process"
STAGE_SYNTH = "synth"
ALL_STAGES = (STAGE_SCOUT, STAGE_HARVEST, STAGE_PROCESS, STAGE_SYNTH)

# ═══════════════════════════════════════════════════════════════════════════════
# AWS / Bedrock constants
# ═══════════════════════════════════════════════════════════════════════════════
BEDROCK_SERVICE = "bedrock-runtime"
BEDROCK_API_VERSION = "bedrock-2023-05-31"
BEDROCK_ANTHROPIC_VERSION = "bedrock-2023-05-31"

# ═══════════════════════════════════════════════════════════════════════════════
# DB sequences and table names
# ═══════════════════════════════════════════════════════════════════════════════
SEQ_FACTS = "hive_facts_seq"
TABLE_MISSIONS = "hive_missions"
TABLE_SOURCES = "hive_sources"
TABLE_CONTENT = "hive_content"
TABLE_CLAIMS = "hive_claims"
TABLE_ENTITIES = "hive_entities"
TABLE_FACTS = "hive_facts"
TABLE_EVENTS = "hive_events"
TABLE_CLUSTERS = "hive_clusters"

# ═══════════════════════════════════════════════════════════════════════════════
# Scout source types
# ═══════════════════════════════════════════════════════════════════════════════
SOURCE_GOOGLE_RSS = "google_rss"
SOURCE_DDG_FILETYPE = "ddg_filetype"
SOURCE_API = "api"
SOURCE_DIRECT = "direct"

# ═══════════════════════════════════════════════════════════════════════════════
# Process / Quality thresholds
# ═══════════════════════════════════════════════════════════════════════════════
DEFAULT_QUALITY_THRESHOLD = 0.45
DEFAULT_CONFIDENCE_THRESHOLD = 0.30
MAX_TOKENS_HAIKU = 4096
MAX_TOKENS_SONNET = 8192

# ═══════════════════════════════════════════════════════════════════════════════
# Queen internal keys
# ═══════════════════════════════════════════════════════════════════════════════
KEY_MEMORY_BLOCK = "memory_block"
KEY_SUBJECT_PROFILE = "subject_profile"
KEY_QUALITY_SCORE = "quality_score"
KEY_MAX_TOKENS = "max_tokens"
