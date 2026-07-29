"""Pipeline orchestrator — Queen-first planning, phase coordination, and full research execution."""

import logging
#!/usr/bin/env python3
"""
HIVE 2.0 — hive2.py
The ORCHESTRATOR: CLI entry point for the full HIVE intelligence pipeline.


log = logging.getLogger(__name__)
Usage:
  python3 hive2.py run "topic" [--think]   # full pipeline (Queen plans first)
  python3 hive2.py scout "topic"           # scout only (Queen plans first)
  python3 hive2.py plan "topic"            # Queen planning only — inspect the JSON
  python3 hive2.py harvest <mission_id>    # harvest existing mission
  python3 hive2.py process <mission_id>    # process existing mission
  python3 hive2.py synth <mission_id>      # synthesize existing mission
  python3 hive2.py status <mission_id>     # show mission status from DB
  python3 hive2.py list                    # list last 10 missions
"""

import sys
import os
import json
import time
import hashlib
import subprocess
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

# Event bus — best-effort wrapper (never raises, non-critical)
def _emit(mission_id: str, phase: str, event_type: str, data=None, **kw):
    """Emit a HIVE event. Silently swallows all errors."""
    try:
        from hive2_events import emit_event, ensure_events_table
        ensure_events_table(DB_PATH)
        emit_event(DB_PATH, mission_id, phase, event_type, data=data, **kw)
    except Exception as e:
        log.debug(f"Suppressed: {e}")

DB_PATH  = ''
PYTHON   = 'python3'
HIVE_DIR = Path(os.environ.get('BEHIVE_HOME', os.path.expanduser('~')))

# ---------------------------------------------------------------------------
# Inline DuckDB schema (sourced from hive2_schema.sql)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- ============================================================
-- HIVE 2.0 — DuckDB Schema (Ul / The Hive)
-- Shared state between all bee processes
-- ============================================================

-- MISJE (missions)
CREATE SEQUENCE IF NOT EXISTS hive_missions_seq START 1;
CREATE TABLE IF NOT EXISTS hive_missions (
    id VARCHAR PRIMARY KEY,
    topic VARCHAR NOT NULL,
    status VARCHAR DEFAULT 'scout',
    think_mode BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    scout_done_at TIMESTAMP,
    harvest_done_at TIMESTAMP,
    process_done_at TIMESTAMP,
    synth_done_at TIMESTAMP,
    sources_found INTEGER DEFAULT 0,
    sources_harvested INTEGER DEFAULT 0,
    sources_processed INTEGER DEFAULT 0,
    total_words INTEGER DEFAULT 0,
    total_entities INTEGER DEFAULT 0,
    total_facts INTEGER DEFAULT 0,
    config JSON
);

-- ŹRÓDŁA (nektar adresy — URLs znalezione przez zwiadowczynie)
CREATE SEQUENCE IF NOT EXISTS hive_sources_seq START 1;
CREATE TABLE IF NOT EXISTS hive_sources (
    id INTEGER DEFAULT nextval('hive_sources_seq') PRIMARY KEY,
    mission_id VARCHAR NOT NULL,
    url VARCHAR NOT NULL,
    domain VARCHAR,
    title VARCHAR,
    snippet VARCHAR,
    source_type VARCHAR,
    scout_method VARCHAR,
    language VARCHAR,
    score_total DOUBLE DEFAULT 0,
    score_relevance DOUBLE DEFAULT 0,
    score_freshness DOUBLE DEFAULT 0,
    score_authority DOUBLE DEFAULT 0,
    score_depth DOUBLE DEFAULT 0,
    status VARCHAR DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(mission_id, url)
);

-- NEKTAR (raw harvested content)
CREATE SEQUENCE IF NOT EXISTS hive_content_seq START 1;
CREATE TABLE IF NOT EXISTS hive_content (
    id INTEGER DEFAULT nextval('hive_content_seq') PRIMARY KEY,
    mission_id VARCHAR NOT NULL,
    url VARCHAR NOT NULL,
    domain VARCHAR,
    title VARCHAR,
    raw_text TEXT,
    word_count INTEGER DEFAULT 0,
    harvest_method VARCHAR,
    language VARCHAR,
    quality_score DOUBLE DEFAULT 0,
    author VARCHAR,
    published_date VARCHAR,
    keywords JSON,
    harvested_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(mission_id, url)
);

-- ENCJE (entities extracted by workers)
CREATE SEQUENCE IF NOT EXISTS hive_entities_seq START 1;
CREATE TABLE IF NOT EXISTS hive_entities (
    id INTEGER DEFAULT nextval('hive_entities_seq') PRIMARY KEY,
    mission_id VARCHAR NOT NULL,
    text VARCHAR NOT NULL,
    type VARCHAR NOT NULL,
    canonical_text VARCHAR,
    confidence DOUBLE DEFAULT 0,
    source_count INTEGER DEFAULT 0,
    sources JSON,
    first_seen VARCHAR,
    context VARCHAR,
    language VARCHAR
);

-- FAKTY NUMERYCZNE (numeric facts cross-validated)
CREATE SEQUENCE IF NOT EXISTS hive_facts_seq START 1;
CREATE TABLE IF NOT EXISTS hive_facts (
    id INTEGER DEFAULT nextval('hive_facts_seq') PRIMARY KEY,
    mission_id VARCHAR NOT NULL,
    value DOUBLE,
    unit VARCHAR,
    context VARCHAR,
    source_count INTEGER DEFAULT 1,
    sources JSON,
    domains JSON,
    confidence VARCHAR DEFAULT 'LOW',
    is_outlier BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- KLASTRY TEMATYCZNE (topic clusters)
CREATE SEQUENCE IF NOT EXISTS hive_clusters_seq START 1;
CREATE TABLE IF NOT EXISTS hive_clusters (
    id INTEGER DEFAULT nextval('hive_clusters_seq') PRIMARY KEY,
    mission_id VARCHAR NOT NULL,
    cluster_id INTEGER,
    topic_label VARCHAR,
    keywords JSON,
    document_count INTEGER DEFAULT 0,
    urls JSON,
    representative_text VARCHAR,
    created_at TIMESTAMP DEFAULT NOW()
);

-- CYTOWANIA (citations for final brief)
CREATE SEQUENCE IF NOT EXISTS hive_citations_seq START 1;
CREATE TABLE IF NOT EXISTS hive_citations (
    id INTEGER DEFAULT nextval('hive_citations_seq') PRIMARY KEY,
    mission_id VARCHAR NOT NULL,
    claim VARCHAR,
    source_url VARCHAR,
    source_domain VARCHAR,
    source_title VARCHAR,
    author VARCHAR,
    published_date VARCHAR,
    confidence DOUBLE DEFAULT 0,
    cross_validated BOOLEAN DEFAULT FALSE,
    supporting_sources INTEGER DEFAULT 1
);

-- GRAF WIEDZY (knowledge graph triples)
CREATE SEQUENCE IF NOT EXISTS hive_triples_seq START 1;
CREATE TABLE IF NOT EXISTS hive_triples (
    id INTEGER DEFAULT nextval('hive_triples_seq') PRIMARY KEY,
    mission_id VARCHAR NOT NULL,
    subject VARCHAR NOT NULL,
    predicate VARCHAR NOT NULL,
    object VARCHAR NOT NULL,
    confidence DOUBLE DEFAULT 0,
    source_count INTEGER DEFAULT 1,
    sources JSON
);

-- LUKI BADAWCZE (gaps for iterative research)
CREATE SEQUENCE IF NOT EXISTS hive_gaps_seq START 1;
CREATE TABLE IF NOT EXISTS hive_gaps (
    id INTEGER DEFAULT nextval('hive_gaps_seq') PRIMARY KEY,
    mission_id VARCHAR NOT NULL,
    gap_query VARCHAR NOT NULL,
    gap_type VARCHAR,
    priority INTEGER DEFAULT 5,
    resolved BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

# ---------------------------------------------------------------------------
# QueenPlanner — The intelligence brain that plans the entire scout swarm
# ---------------------------------------------------------------------------

class QueenPlanner:
    """
    The Queen analyzes the mission topic and generates a detailed
    200+ scout task plan optimized for maximum intelligence coverage.

    She plans EVERYTHING before any scout leaves the hive.
    """

    BEDROCK_MODEL  = 'eu.anthropic.claude-sonnet-4-6'
    BEDROCK_REGION = 'eu-central-1'

    # Task type classification — determines which source strategy Queen uses
    TASK_TYPES = {
        'scientific_research': {
            'triggers': [
                'research', 'paper', 'badania', 'naukow', 'arxiv', 'pubmed',
                'scientific', 'peer-review', 'hypothesis', 'experiment',
                'biology', 'physics', 'chemistry', 'nanotechnol', 'quantum',
                'algorithm', 'swarm intelligence', 'collective intelligence',
                'bee colony', 'ant colony', 'stigmergy', 'pheromone',
                'neural', 'DNA', 'molecular', 'self-replicat', 'emergent',
            ],
            'methods': ['ddg_academic', 'ddg_text', 'ddg_filetype', 'google_rss', 'ddg_site'],
            'domains': [
                'arxiv.org', 'scholar.google.com', 'pubmed.ncbi.nlm.nih.gov',
                'nature.com', 'science.org', 'pnas.org', 'cell.com',
                'frontiersin.org', 'mdpi.com', 'plos.org', 'ieee.org',
                'acm.org', 'springer.com', 'wiley.com', 'researchgate.net',
                'semanticscholar.org', 'ssrn.com', 'biorxiv.org',
            ],
        },
        'market_intelligence': {
            'triggers': [
                'market', 'rynek', 'startup', 'company', 'firma', 'revenue',
                'funding', 'valuation', 'competitor', 'industry', 'sector',
                'saas', 'fintech', 'ecommerce', 'b2b', 'growth',
            ],
            'methods': ['ddg_text', 'ddg_news', 'ddg_site', 'ddg_filetype', 'google_rss'],
            'domains': [
                'crunchbase.com', 'pitchbook.com', 'statista.com', 'reuters.com',
                'bloomberg.com', 'ft.com', 'techcrunch.com', 'forbes.com',
                'mckinsey.com', 'bcg.com', 'bain.com', 'gartner.com',
            ],
        },
        'geopolitical_osint': {
            'triggers': [
                'sanction', 'geopolit', 'conflict', 'war', 'military',
                'intelligence', 'espionage', 'influence', 'corruption',
                'money launder', 'aml', 'fraud', 'export control',
                'dual-use', 'weapon', 'compliance', 'oligarch',
            ],
            'methods': ['ddg_text', 'ddg_news', 'ddg_site', 'ddg_filetype', 'wayback_cdx', 'ddg_reddit'],
            'domains': [
                'opensanctions.org', 'occrp.org', 'bellingcat.com', 'icij.org',
                'chathamhouse.org', 'rusi.org', 'csis.org', 'rand.org',
                'sipri.org', 'iiss.org', 'carnegieendowment.org',
                'transparency.org', 'fatf-gafi.org', 'brookings.edu',
            ],
        },
        # ── NEW TASK TYPES ────────────────────────────────────────────────────
        'regulatory_analysis': {
            'triggers': [
                'regulat', 'law', 'prawo', 'przepis', 'ustawa', 'directive',
                'dyrektywa', 'compliance', 'gdpr', 'rodo', 'dsa', 'dma',
                'nis2', 'ai act', 'standard', 'norm', 'iso', 'iec',
                'licenc', 'zezwolen', 'koncesj', 'permit', 'approval',
                'legislation', 'statutory', 'regulatory', 'enforcement',
            ],
            'methods': ['ddg_site', 'ddg_filetype', 'ddg_text', 'google_rss', 'wayback_cdx'],
            'domains': [
                'eur-lex.europa.eu', 'ec.europa.eu', 'enisa.europa.eu',
                'ico.org.uk', 'bafin.de', 'cnb.cz', 'uodo.gov.pl',
                'edpb.europa.eu', 'cnil.fr', 'aepd.es',
                'federalregister.gov', 'law.cornell.edu', 'courtlistener.com',
                'iso.org', 'ietf.org', 'w3.org',
            ],
        },
        'competitive_intelligence': {
            'triggers': [
                'competitor', 'konkurencj', 'competitive', 'benchmarking',
                'market share', 'pricing', 'price', 'cena', 'cennik',
                'product comparison', 'feature matrix', 'versus', 'vs ',
                'alternative', 'substitute', 'positioning', 'differentiat',
                'leadership', 'rank', 'ranking', 'best in class',
            ],
            'methods': ['ddg_text', 'ddg_news', 'ddg_site', 'ddg_reddit', 'google_rss'],
            'domains': [
                'g2.com', 'capterra.com', 'trustpilot.com', 'gartner.com',
                'forrester.com', 'idc.com', 'similarweb.com', 'semrush.com',
                'linkedin.com/company', 'glassdoor.com', 'crunchbase.com',
                'github.com', 'stackshare.io', 'alternativeto.net',
            ],
        },
        'academic_deep_dive': {
            'triggers': [
                'meta-analiz', 'systematic review', 'literature review',
                'przegląd literatury', 'state of the art', 'sota',
                'survey', 'przegląd', 'survey paper', 'benchmar',
                'phd', 'dissertation', 'thesis', 'praca doktorska',
                'citation', 'impact factor', 'h-index', 'bibliometr',
            ],
            'methods': ['ddg_academic', 'ddg_filetype', 'ddg_site', 'ddg_text'],
            'domains': [
                'semanticscholar.org', 'arxiv.org', 'pubmed.ncbi.nlm.nih.gov',
                'scholar.google.com', 'researchgate.net', 'academia.edu',
                'core.ac.uk', 'unpaywall.org', 'opencitations.net',
                'crossref.org', 'doi.org', 'jstor.org',
                'acm.org', 'ieee.org', 'springer.com',
            ],
        },
        'social_media_analysis': {
            'triggers': [
                'social media', 'twitter', 'facebook', 'instagram', 'linkedin',
                'tiktok', 'youtube', 'reddit', 'telegram', 'discord',
                'influencer', 'viral', 'hashtag', 'engagement', 'reach',
                'sentiment', 'opinion mining', 'trend', 'buzz',
                'platform', 'content creator', 'digital marketing',
                'social listening', 'brand monitoring',
            ],
            'methods': ['ddg_text', 'ddg_news', 'ddg_reddit', 'google_rss', 'ddg_site'],
            'domains': [
                'datareportal.com', 'wearesocial.com', 'hootsuite.com',
                'sproutsocial.com', 'brandwatch.com', 'mention.com',
                'socialblade.com', 'similarweb.com', 'statista.com',
                'pew research.org', 'pewresearch.org', 'emarketer.com',
                'reuters.com', 'wired.com', 'theatlantic.com',
            ],
        },
    }

    def __init__(self, topic: str, think_mode: bool = False, scale: int = 200):
        self.topic      = topic
        self.think_mode = think_mode
        self.scale      = max(30, min(300, scale))  # Clamp 30-300
        self.task_type  = self._classify_task_type()

    # ------------------------------------------------------------------
    def _classify_task_type(self) -> str:
        """Classify mission into task type to select appropriate source strategy."""
        topic_lower = self.topic.lower()
        scores = {}
        for ttype, cfg in self.TASK_TYPES.items():
            score = sum(1 for t in cfg['triggers'] if t in topic_lower)
            scores[ttype] = score
        best = max(scores, key=scores.get)
        if scores[best] == 0:
            return 'geopolitical_osint'  # default fallback
        return best

    # ------------------------------------------------------------------
    def _detect_context(self) -> dict:
        """Detect geographic/linguistic context from topic."""
        topic_lower = self.topic.lower()
        ctx = {'langs': ['en', 'pl'], 'regions': [], 'domains': []}

        # Turkey
        if any(k in topic_lower for k in [
            'turcja', 'turkey', 'turkish', 'türkiye', 'ankara', 'istanbul', 'tr '
        ]):
            ctx['langs'].append('tr')
            ctx['regions'].append('tr')
            ctx['domains'].extend([
                'tuik.gov.tr', 'btk.gov.tr', 'sabah.com.tr', 'hurriyet.com.tr'
            ])

        # Georgia
        if any(k in topic_lower for k in ['gruzja', 'georgia', 'georgian', 'tbilisi']):
            ctx['langs'].append('ka')
            ctx['regions'].append('ge')
            ctx['domains'].extend(['geostat.ge', 'civil.ge', 'agenda.ge'])

        # Russia / Ukraine
        if any(k in topic_lower for k in [
            'rosja', 'russia', 'ukraine', 'ukraina', 'moscow'
        ]):
            ctx['langs'].append('ru')
            ctx['regions'].append('ru')

        # Romania
        if any(k in topic_lower for k in ['romania', 'rumunia', 'bucharest', 'bucuresti']):
            ctx['langs'].append('ro')
            ctx['regions'].append('ro')
            ctx['domains'].extend(['insse.ro', 'ancom.ro'])

        # Germany / DACH
        if any(k in topic_lower for k in ['germany', 'niemcy', 'deutschland', 'austria', 'schweiz']):
            ctx['langs'].append('de')
            ctx['regions'].append('de')
            ctx['domains'].extend(['destatis.de', 'bundesnetzagentur.de'])

        return ctx

    # ------------------------------------------------------------------
    def _bedrock_batch(self, batch_id: int, focus: str, ctx: dict, id_offset: int) -> list[dict]:
        """Single Bedrock call — Queen thinks deeply about ONE research axis."""
        import boto3 as _boto3
        client = _boto3.client('bedrock-runtime', region_name=self.BEDROCK_REGION)

        type_cfg = self.TASK_TYPES.get(self.task_type, self.TASK_TYPES['geopolitical_osint'])
        domains = type_cfg['domains']
        methods = type_cfg['methods']

        system_prompt = f"""You are the QUEEN — strategic director of a biomimetic intelligence swarm and a world-class research architect.

You are NOT a keyword generator. You are a PhD-level RESEARCH STRATEGIST with deep domain expertise.

CURRENT DATE: {datetime.utcnow().strftime("%Y-%m-%d")} UTC — prioritize sources and data from {datetime.utcnow().year - 1}–{datetime.utcnow().year}. Do NOT default to 2024/2025 if the current year is later.

Your mission: design 20 SURGICAL search operations that will collectively close specific knowledge gaps on a research axis.

TASK TYPE: {self.task_type}
KNOWN TASK TYPES: {', '.join(self.TASK_TYPES.keys())}

═══════════════════════════════════════════════════
MANDATORY THINKING PROTOCOL (execute before generating):
═══════════════════════════════════════════════════

STEP 1 — DECOMPOSE the research axis into 3-5 MECE sub-questions.
  Example for "Turkish B2B LinkedIn ROI":
  ├── Q1: What is LinkedIn penetration rate in Turkey? (quantitative baseline)
  ├── Q2: What CPC/CPL benchmarks exist for Turkish B2B campaigns? (cost data)
  ├── Q3: Which Turkish industries use LinkedIn most for B2B? (segmentation)
  ├── Q4: What case studies show measurable ROI? (evidence)
  └── Q5: What local alternatives compete with LinkedIn in Turkey? (alternatives)

STEP 2 — IDENTIFY the authoritative source per sub-question.
  NOT "statistics about topic" but "DataReportal Digital 2025 Turkey country report"
  NOT "LinkedIn marketing" but "LinkedIn Marketing Solutions Turkey Business Blog"
  NOT "B2B case study" but "HubSpot State of Marketing Turkey 2024 annual report"

STEP 3 — WRITE QUERIES a domain expert would type.
  BAD: "Turkey LinkedIn marketing statistics"
  GOOD: "site:datareportal.com Turkey 2025 digital LinkedIn users penetration"
  BAD: "B2B social media ROI"
  GOOD: "filetype:pdf \"LinkedIn\" \"cost per lead\" Turkey B2B benchmark 2024 2025"

STEP 4 — ASSIGN search method logically:
  - Official reports/PDFs → ddg_filetype (filetype:pdf)
  - Single trusted domain → ddg_site (requires "site" field)
  - Current news → ddg_news
  - Academic papers → ddg_academic
  - Community discussions → ddg_reddit
  - Standard web → ddg_text

STEP 5 — CHECK against memory block below.
  Do NOT re-generate tasks for queries already covered.
  DO generate tasks that directly answer listed FUIR gaps.

═══════════════════════════════════════════════════
QUALITY GATE — each task must pass ALL checks:
═══════════════════════════════════════════════════
✅ Query is specific enough that a human expert would type it exactly
✅ Expected output is concrete (numbers, names, dates — not "information about X")
✅ Method matches query type (no ddg_text for single-domain searches)
✅ Purpose directly answers one of the MECE sub-questions
✅ Not a duplicate of memory-covered queries

HIGH-VALUE DOMAINS for {self.task_type}:
{', '.join(domains)}"""

        # Extract competitor seed list + targeted queries from subject_profile (if present)
        subject_intel_block = ""
        if ctx.get("subject_profile"):
            subject_intel_block = f"""
SUBJECT INTELLIGENCE (use this to anchor your queries to SPECIFIC companies and sites):
{ctx["subject_profile"]}

MANDATORY RULES when subject intelligence is available:
- Use SPECIFIC company names from the COMPETITOR SEED LIST in your queries (not generic category names)
- Use site: operators targeting review platforms: site:clutch.co, site:g2.com, site:capterra.com, site:linkedin.com/company
- Use pracuj.pl / linkedin job postings to discover which companies USE which tools (job postings = customer intelligence)
- Polish-language queries for local competitors: search in Polish, use .pl domains
- DO NOT generate generic queries like "RPA automation market Poland" — generate "UiPath partner Polska wdrożenie opinie" instead
"""

        prompt = f"""MISSION TOPIC: {self.topic}
DETECTED LANGUAGES: {ctx['langs']}
RELEVANT DOMAINS: {ctx['domains']}

YOUR RESEARCH AXIS FOR THIS BATCH: {focus}

{ctx.get('memory_block', '')}
{subject_intel_block}

Generate EXACTLY 20 scout tasks as a JSON array. IDs start at {id_offset + 1}.

Each task MUST have:
{{
  "id": N,
  "query": "precise search query a domain expert would actually type",
  "region": "xx-xx",
  "method": "one of the available methods",
  "source": "category",
  "language": "en|pl|tr|etc",
  "priority": 1-10,
  "purpose": "WHAT SPECIFIC QUESTION does this task answer?",
  "expected_output": "WHAT will we learn from the results?"
}}

For ddg_site method, add: "site": "domain.com"

CRITICAL: The "purpose" and "expected_output" fields are MANDATORY. They prove you THOUGHT about why each task exists. A task without clear purpose is WORTHLESS.
{("CRITICAL: If FUIR directives are listed above — your tasks MUST directly address them. These are KNOWN GAPS that need filling." if ctx.get('memory_block') else "")}
{("CRITICAL: Do NOT generate tasks that duplicate already-covered queries listed above." if ctx.get('memory_block') else "")}

Return ONLY the JSON array."""

        resp = client.invoke_model(
            modelId=self.BEDROCK_MODEL,
            body=json.dumps({
                'anthropic_version': 'bedrock-2023-05-31',
                'max_tokens': 4096, 'temperature': 0.4,
                'system': system_prompt,
                'messages': [{'role': 'user', 'content': prompt}]
            })
        )
        text = json.loads(resp['body'].read())['content'][0]['text'].strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        tasks = json.loads(text)
        for i, t in enumerate(tasks):
            t['id'] = id_offset + i + 1
        return tasks

    def plan(self) -> list[dict]:
        """
        QUEEN STRATEGIC PLANNING — Two-phase intelligence:
        Phase 1: Decompose topic into 5 research axes (what to investigate)
        Phase 2: Generate 40 precision tasks per axis (how to investigate)
        """
        ctx = self._detect_context()
        type_cfg = self.TASK_TYPES.get(self.task_type, self.TASK_TYPES['geopolitical_osint'])

        # ═══ PHASE 0: MEMORY RECALL — Queen loads operational memory ═══
        log.debug(f'  👑 Queen loading operational memory...')
        try:
            from hive2_queen_memory import QueenMemory
            memory    = QueenMemory(self.topic)
            mem_ctx   = memory.recall()
            mem_block = mem_ctx.to_prompt_block()
            if not mem_ctx.is_empty():
                log.debug(f'  👑 Memory: {mem_ctx.total_missions} related missions | '
                      f'{len(mem_ctx.fuir_directives)} FUIR | '
                      f'{len(mem_ctx.intelligence_voids)} voids | '
                      f'confidence {mem_ctx.cluster_confidence:.0%}')
                ctx['memory_block'] = mem_block
                ctx['fuir']         = mem_ctx.fuir_directives
                ctx['known_gaps']   = mem_ctx.intelligence_voids
            else:
                log.debug(f'  👑 Memory: empty — fresh topic, no prior context')
                ctx['memory_block'] = ''
                ctx['fuir']         = []
                ctx['known_gaps']   = []
        except Exception as e:
            log.warning(f'  👑 Memory recall skipped: {e}')
            ctx['memory_block'] = ''
            ctx['fuir']         = []
            ctx['known_gaps']   = []

        # ═══ PHASE 0b: CALIBRATION — Queen loads calibration profile ═══
        try:
            from hive2_queen_calibrator import QueenCalibrator
            calibrator = QueenCalibrator()
            calib_block = calibrator.generate_calibration_block(self.topic)
            if calib_block and len(calib_block) > 100:
                # Attach calibration to memory_block (Queen widzi oba)
                existing_mem = ctx.get('memory_block', '')
                ctx['memory_block'] = (existing_mem + '\n\n' + calib_block).strip()
                log.debug(f'  👑 Calibration: profile loaded for this topic domain')
            else:
                log.debug(f'  👑 Calibration: no historical data for this domain yet')
        except Exception as e:
            log.warning(f'  👑 Calibration skipped: {e}')

        # ═══ PHASE 0c: SEMANTIC FACT MEMORY — Queen loads semantic facts ═══
        try:
            from hive2_queen_fact_mem import QueenFactMemory
            fact_mem = QueenFactMemory()
            fact_block = fact_mem.recall(self.topic, top_k=8)
            if fact_block:
                fact_stats = fact_mem.stats()
                log.debug(f'  👑 Semantic memory: {fact_stats["total_facts"]} facts indexed, '
                      f'{fact_stats["missions_covered"]} missions covered')
                existing_mem = ctx.get('memory_block', '')
                ctx['memory_block'] = (existing_mem + '\n\n' + fact_block).strip()
            else:
                log.debug(f'  👑 Semantic memory: empty — first run for this topic')
            fact_mem.close()
        except Exception as e:
            log.warning(f'  👑 Semantic memory skipped: {e}')

        # ═══ PHASE 0c.5: QMP PRIMER inject ─────────────────────────
        # _primer_context set by cmd_run before plan() is called.
        # Appended AFTER semantic memory so Queen sees it last (freshest context).
        _primer_ctx = getattr(self, '_primer_context', '')
        if _primer_ctx:
            existing_mem = ctx.get('memory_block', '')
            ctx['memory_block'] = (existing_mem + '\n\n' + _primer_ctx).strip()
            log.debug(f'  👑 QMP Primer: injected into Queen planning context')

        # ═══ PHASE 0d: SUBJECT RECON — Queen profiles subject before dispatching bees ═══
        try:
            subject_profile = self._recon_subject(ctx)
            if subject_profile:
                ctx['subject_profile'] = subject_profile
                log.debug(f'  👑 Subject recon: {len(subject_profile)} chars of context loaded')
            else:
                ctx['subject_profile'] = ''
                log.warning(f'  👑 Subject recon: no subject URL detected — skipping')
        except Exception as e:
            log.warning(f'  👑 Subject recon skipped: {e}')
            ctx['subject_profile'] = ''

        # ═══ PHASE 1: DECOMPOSITION — Queen thinks about WHAT to research ═══
        log.debug(f'  👑 Queen thinking · type={self.task_type} · decomposing into research axes...')

        axes = self._decompose_topic(ctx, type_cfg)
        if not axes or len(axes) < 3:
            axes = self._fallback_axes()

        log.debug(f'  👑 Identified {len(axes)} research axes:')
        for i, ax in enumerate(axes):
            log.debug(f'     {i+1}. {ax[:80]}')

        # ═══ PHASE 2: TASK GENERATION — scaled by self.scale ═══
        # Compute tasks_per_axis based on scale
        num_axes = len(axes)
        tasks_per_axis = max(10, self.scale // num_axes)
        # Round to nearest 10 for clean batching
        tasks_per_axis = ((tasks_per_axis + 9) // 10) * 10
        total_planned = num_axes * tasks_per_axis
        batch_size = 20  # LLM generates 20 tasks per call (fits 4096 tokens)
        batches_per_axis = max(1, -(-tasks_per_axis // batch_size))  # ceiling division
        log.debug(f'  👑 Generating {num_axes} × {tasks_per_axis} = {total_planned} precision tasks ({batches_per_axis}×{batch_size} per axis)...')

        BATCHES = []
        for i, ax in enumerate(axes):
            for b in range(batches_per_axis):
                BATCHES.append((i * batches_per_axis + b, i * tasks_per_axis + b * batch_size, ax))

        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            all_tasks: list[dict] = []

            with ThreadPoolExecutor(max_workers=5) as ex:
                futures = {
                    ex.submit(self._bedrock_batch, bid, focus, ctx, offset): bid
                    for bid, offset, focus in BATCHES
                }
                batch_results = {}
                for fut in as_completed(futures):
                    bid = futures[fut]
                    try:
                        batch_results[bid] = fut.result()
                        log.info(f'     ✓ Axis {bid+1}/{len(axes)}: {len(batch_results[bid])} tasks')
                    except Exception as e:
                        log.error(f'     ✗ Axis {bid+1}/{len(axes)} failed: {e} — retrying...')
                        import time as _time
                        _time.sleep(5)  # backoff before retry (429/throttle)
                        try:
                            bid2, offset2, focus2 = BATCHES[bid]
                            batch_results[bid] = self._bedrock_batch(bid2, focus2, ctx, offset2)
                            log.warning(f'     ↺ Axis {bid+1}/{len(axes)} retry OK: {len(batch_results[bid])} tasks')
                        except Exception as e2:
                            log.error(f'     ✗ Axis {bid+1}/{len(axes)} retry failed: {e2}')
                            batch_results[bid] = []

            for bid in sorted(batch_results):
                all_tasks.extend(batch_results[bid])

            if len(all_tasks) < max(20, self.scale // 4):
                raise ValueError(f'Too few tasks ({len(all_tasks)}) — falling back')

            log.debug(f'\n  👑 Queen planned {len(all_tasks)} scout tasks total')
            src_counts = Counter(t.get('source', '?') for t in all_tasks)
            for src, cnt in src_counts.most_common():
                log.debug(f'     {src}: {cnt}')
            return all_tasks

        except Exception as e:
            log.error(f'  ⚠️  Queen planning failed ({e}) — falling back to default plan')
            return self._fallback_plan()

    # ------------------------------------------------------------------
    def _recon_subject(self, ctx: dict) -> str:
        """
        PHASE 0d — Subject Recon.
        Wykrywa podmiot badania (URL lub nazwa firmy) z topiku,
        scrapuje jego stronę, i zwraca ustrukturyzowany profil
        który Queen dostaje przed generowaniem osi badawczych.

        Zwraca: string z profilem podmiotu (do ctx['subject_profile'])
                lub pusty string jeśli nie wykryto podmiotu.
        """
        import re, requests, boto3 as _boto3

        topic = self.topic

        # ── 1. Detect URL or company name in topic ─────────────────────
        # Szukaj: jawny URL, domena (xyz.io / xyz.com / xyz.pl), lub
        # wzorzec "dla X", "of X", "X competitive", "X sp. z o.o.", "X S.A."
        subject_url = None
        subject_name = None

        # Jawny URL
        url_match = re.search(r'https?://[^\s]+', topic)
        if url_match:
            subject_url = url_match.group(0)

        # Domain without protocol (np. "qa10.io", "example.com")
        if not subject_url:
            domain_match = re.search(
                r'\b([a-z0-9][a-z0-9\-]*\.[a-z]{2,6}(?:\.[a-z]{2})?)\b',
                topic, re.IGNORECASE
            )
            if domain_match:
                candidate = domain_match.group(1).lower()
                # Filter common words that are not domains
                skip = {'process', 'mining', 'market', 'rpa', 'poland', 'size', 'cagr'}
                if candidate.split('.')[0] not in skip:
                    subject_url = f'https://{candidate}'

        # Nazwa firmy (wzorce: "for QA10", "QA10 competitive", "QA10 sp.", itp.)
        if not subject_url:
            name_patterns = [
                r'(?:dla|for|of|about)\s+([A-Z][A-Za-z0-9\.\-]+)',
                r'^([A-Z][A-Za-z0-9]+)\s+(?:competitive|competitor|market|analysis)',
                r'([A-Z][A-Za-z0-9]+)\s+(?:sp\.|S\.A\.|GmbH|Ltd|Inc)',
                r'competitors\s+of\s+([A-Z][A-Za-z0-9\.\-]+)',
            ]
            for pat in name_patterns:
                m = re.search(pat, topic)
                if m:
                    subject_name = m.group(1)
                    break

        if not subject_url and not subject_name:
            return ''

        # ── 2. Scraping podmiotu ────────────────────────────────────────
        raw_text = ''
        scraped_url = subject_url or f'https://www.google.com/search?q={subject_name}'

        # Attempt 1: Jina Reader (najlepszy dla HTML→tekst)
        try:
            jina_url = f'https://r.jina.ai/{subject_url or ("https://" + subject_name + ".io")}'
            resp = requests.get(jina_url, timeout=15, headers={
                'User-Agent': 'Mozilla/5.0',
                'Accept': 'text/plain'
            })
            if resp.status_code == 200 and len(resp.text) > 200:
                raw_text = resp.text[:8000]
                scraped_url = jina_url
        except Exception as e:
            log.debug(f"Suppressed: {e}")

        # Attempt 2: direct requests
        if not raw_text and subject_url:
            try:
                resp = requests.get(subject_url, timeout=10, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                if resp.status_code == 200:
                    # Primitive HTML tag removal
                    text = re.sub(r'<[^>]+>', ' ', resp.text)
                    text = re.sub(r'\s+', ' ', text)
                    raw_text = text[:8000]
            except Exception as e:
                log.debug(f"Suppressed: {e}")

        if not raw_text:
            # Failed to harvest — do not block mission
            return ''

        # ── 3. Queen ekstrakcja profilu ─────────────────────────────────
        client = _boto3.client('bedrock-runtime', region_name=self.BEDROCK_REGION)
        prompt = f"""You scraped the following content from the subject's website.
Extract a structured intelligence profile AND generate a competitor seed list.

TOPIC: {topic}
SOURCE: {scraped_url}

SCRAPED CONTENT:
{raw_text[:6000]}

Extract ONLY what is explicitly present in the content above.
Return a structured profile in this exact format:

SUBJECT PROFILE
===============
Name: [company/organization name]
Website: [URL]
Core services: [bullet list — what they sell/do]
Technology stack: [tools, platforms, integrations mentioned — e.g. UiPath, Celonis, Power Automate, SAP, etc.]
Target customers: [segments, industries, company sizes — e.g. SME, enterprise, manufacturing, finance]
Geography: [markets served — e.g. Poland, CEE, DACH]
Positioning: [how they describe themselves — 1-2 sentences]
Key differentiators: [what makes them unique per their own claims]
Competitors mentioned: [any names explicitly mentioned on the site]
Pricing signals: [any pricing info]
Notable clients/cases: [if mentioned]

COMPETITOR SEED LIST
====================
Based on the subject's services, tech stack, and target market above, list 10-15 SPECIFIC companies
that likely compete with this subject. Include:
- Direct competitors (same product category, same market)
- Platform alternatives (e.g. if subject does RPA → UiPath, Automation Anywhere, Blue Prism)
- Local/regional players (Poland/CEE if applicable)
- Review site queries to find more (Clutch, G2, Capterra)

Format each as: COMPANY_NAME | domain.com | reason_they_compete
Example: UiPath | uipath.com | enterprise RPA platform, direct competitor in Polish market

TARGETED SEARCH QUERIES
========================
Generate 10 high-precision search queries to find competitor intelligence. Use:
- site: operators (site:clutch.co, site:g2.com, site:linkedin.com/company)
- Polish-language queries for local competitors
- Job posting queries (linkedin jobs reveal competitor org structures)
- Review queries

Format: one query per line, no numbering.

If a field has no evidence, write "not found".
Be factual for the profile. For competitor list — use domain knowledge about the industry."""

        try:
            resp = client.invoke_model(
                modelId=self.BEDROCK_MODEL,
                body=json.dumps({
                    'anthropic_version': 'bedrock-2023-05-31',
                    'max_tokens': 1500,
                    'temperature': 0.1,
                    'messages': [{'role': 'user', 'content': prompt}]
                })
            )
            profile = json.loads(resp['body'].read())['content'][0]['text'].strip()
            return f"\n\n═══ SUBJECT INTELLIGENCE PROFILE ═══\n{profile}\n═══════════════════════════════════\n"
        except Exception as e:
            log.debug(f"Exception in orchestrator.py: {e}")
            # Bedrock failed — return raw truncated text
            return f"\n\n═══ SUBJECT RAW CONTENT (scrape of {scraped_url}) ═══\n{raw_text[:2000]}\n═══════════════════════════════════\n"

    # ------------------------------------------------------------------
    def _decompose_topic(self, ctx: dict, type_cfg: dict) -> list[str]:
        """Phase 1: STORM-style multi-perspective decomposition.
        Instead of 5 generic axes, Queen generates 5 EXPERT PERSONAS — each with
        a distinct viewpoint, motivation, and targeted query strategy.
        This mirrors Stanford STORM's 'diverse perspectives' pattern (+25% breadth).
        """
        import boto3 as _boto3
        client = _boto3.client('bedrock-runtime', region_name=self.BEDROCK_REGION)

        memory_section = ''
        raw_mem = ctx.get('memory_block', '')
        if raw_mem:
            mem_lines = [l for l in raw_mem.split('\n')
                         if any(k in l for k in ['FUIR', 'GAP', 'MISSING', 'VOID', 'IV-', 'gap:'])]
            if mem_lines:
                memory_section = (
                    "\n\nCRITICAL — KNOWN INTELLIGENCE GAPS FROM PREVIOUS MISSIONS:\n"
                    + '\n'.join(mem_lines[:15])
                    + "\n\nYour personas MUST directly address these gaps."
                )

        competitor_block = ""
        if ctx.get('subject_profile'):
            competitor_block = (
                "\n\nSUBJECT INTELLIGENCE PROFILE (anchor ALL personas to this):\n"
                + ctx['subject_profile'][:1500]
            )

        prompt = f"""You are a senior research director using the STORM methodology to plan a comprehensive intelligence investigation.

TOPIC: {self.topic}
TASK TYPE: {self.task_type}
AVAILABLE SOURCE DOMAINS: {', '.join(type_cfg['domains'][:10])}{memory_section}{competitor_block}

STORM PATTERN: Generate 5 EXPERT PERSONAS — each representing a different type of investigator with a unique perspective, motivation, and information-seeking strategy. Each persona asks DIFFERENT questions from a different angle. This produces broader, less biased coverage.

RULES:
1. Each persona must have a DISTINCT viewpoint (buyer, analyst, employee, competitor, journalist)
2. Each persona targets DIFFERENT sources (review platforms, job sites, LinkedIn, news, forums)
3. For competitive intelligence: personas MUST reference specific company names, not generic categories
4. At least one persona must use site:clutch.co or site:g2.com
5. At least one persona must mine job postings (pracuj.pl, LinkedIn) to infer customer/tech adoption
6. Each axis description = persona identity (1 sentence) + what they seek (1 sentence) + 2-3 EXACT targeted queries

RETURN: JSON array of 5 strings. Each string = full persona axis description with embedded queries.

Example for competitive intelligence topic:
[
  "PERSONA: Polish CTO evaluating automation vendors for their company. SEEKS: honest peer reviews, implementation war stories, hidden costs, support quality. QUERIES: site:clutch.co 'UiPath' Poland review, site:g2.com 'Transition Technologies' rating, 'Britenet RPA wdrożenie opinia' reddit.com OR forum.pl",
  "PERSONA: Tech recruiter at headhunting firm mapping the automation market. SEEKS: which companies use which tools, team sizes, salaries, growth signals. QUERIES: site:pracuj.pl 'UiPath developer' OR 'Power Automate', site:linkedin.com/jobs 'RPA Polska' 2024, 'Automation Anywhere administrator' pracuj.pl",
  "PERSONA: Journalist writing about Polish IT outsourcing market consolidation. SEEKS: revenue figures, client logos, acquisitions, market share data. QUERIES: site:computerworld.pl automatyzacja procesów, 'Transition Technologies' OR 'Britenet' przychody 2023, site:prnewswire.com Poland automation",
  "PERSONA: Procurement officer at a mid-size Polish manufacturer comparing vendors. SEEKS: pricing tiers, pilot project costs, ROI case studies, contract terms. QUERIES: site:uipath.com/pricing Poland, 'koszt wdrożenia RPA' Polska case study, site:techbehemoths.com Poland automation",
  "PERSONA: Open-source advocate and startup founder looking for underdog alternatives. SEEKS: n8n vs UiPath comparisons, self-hosted RPA options, new entrants, ProductHunt launches. QUERIES: site:github.com 'RPA Poland' stars, n8n vs 'Power Automate' reddit 2024, site:producthunt.com automation 'Poland'"
]

Return ONLY the JSON array."""

        try:
            resp = client.invoke_model(
                modelId=self.BEDROCK_MODEL,
                body=json.dumps({
                    'anthropic_version': 'bedrock-2023-05-31',
                    'max_tokens': 2000, 'temperature': 0.6,
                    'messages': [{'role': 'user', 'content': prompt}]
                })
            )
            text = json.loads(resp['body'].read())['content'][0]['text'].strip()
            if text.startswith('```'):
                text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
            axes = json.loads(text)
            if isinstance(axes, list) and len(axes) >= 3:
                log.debug(f'  👑 STORM personas generated: {len(axes)} perspectives')
                return axes[:5]
        except Exception as e:
            log.error(f'  ⚠️  STORM decomposition failed: {e}')

        return self._fallback_axes()

    def _fallback_axes(self) -> list[str]:
        """Fallback axes when LLM decomposition fails."""
        return [
            f"Core definitions, foundational concepts, and seminal works about: {self.topic}",
            f"Current state of the art, latest research papers and breakthroughs in: {self.topic}",
            f"Practical applications, implementations, and case studies of: {self.topic}",
            f"Challenges, limitations, open problems, and criticisms of: {self.topic}",
            f"Future directions, speculative frontier, and cross-domain convergence for: {self.topic}",
        ]

    # ------------------------------------------------------------------
    def _fallback_plan(self) -> list[dict]:
        """50-task fallback if LLM is unavailable."""
        tasks: list[dict] = []
        region_pairs = [('pl-pl', 'pl'), ('en-us', 'en'), ('tr-tr', 'tr')]
        _cy = datetime.utcnow().year
        _py = _cy - 1

        base_queries = [
            self.topic,
            f'{self.topic} statistics {_py} {_cy}',
            f'{self.topic} market report analysis',
            f'{self.topic} raport dane statystyki',
            f'{self.topic} annual report',
            f'{self.topic} research study findings',
        ]

        for q in base_queries:
            for reg, lang in region_pairs:
                tasks.append({
                    'id': len(tasks) + 1,
                    'query': q,
                    'region': reg,
                    'method': 'ddg_text',
                    'source': 'ddg',
                    'language': lang,
                    'priority': 5,
                    'rationale': 'fallback base query',
                })

        # Authority sites
        for site in [
            'datareportal.com', 'statista.com', 'gsma.com',
            'itu.int', 'worldbank.org', 'imf.org',
        ]:
            tasks.append({
                'id': len(tasks) + 1,
                'query': self.topic,
                'method': 'ddg_site',
                'site': site,
                'source': 'primary_intel',
                'region': 'en-us',
                'language': 'en',
                'priority': 9,
                'rationale': f'authority site {site}',
            })

        # PDF reports
        tasks.append({
            'id': len(tasks) + 1,
            'query': f'{self.topic} filetype:pdf report 2024',
            'method': 'ddg_filetype',
            'source': 'document',
            'region': 'en-us',
            'language': 'en',
            'priority': 8,
            'rationale': 'PDF report search',
        })

        # News + RSS
        for lang in ['en', 'pl']:
            tasks.append({
                'id': len(tasks) + 1,
                'query': self.topic,
                'method': 'google_rss',
                'source': 'rss',
                'language': lang,
                'priority': 7,
                'rationale': f'Google RSS {lang}',
            })
            tasks.append({
                'id': len(tasks) + 1,
                'query': self.topic,
                'region': 'en-us',
                'method': 'ddg_news',
                'source': 'news',
                'language': lang,
                'priority': 8,
                'rationale': f'DDG news {lang}',
            })

        # Reddit
        tasks.append({
            'id': len(tasks) + 1,
            'query': f'{self.topic} site:reddit.com',
            'method': 'ddg_reddit',
            'source': 'reddit',
            'region': 'en-us',
            'language': 'en',
            'priority': 6,
            'rationale': 'community discussion',
        })

        # Wayback CDX
        tasks.append({
            'id': len(tasks) + 1,
            'query': self.topic,
            'method': 'wayback_cdx',
            'site': 'statista.com',
            'source': 'archive',
            'region': 'en-us',
            'language': 'en',
            'priority': 5,
            'rationale': 'archived Statista reports',
        })

        return tasks


# ---------------------------------------------------------------------------
# DuckDB/PostgreSQL helpers
# ---------------------------------------------------------------------------

# PostgreSQL backend (preferred)
try:
    import hive2_db as _hive_db
    _pg_available = (_hive_db.DB_BACKEND == "postgres")
except ImportError:
    _pg_available = False


def _get_duckdb():
    """Legacy — kept for backward compatibility but should never be called."""
    raise RuntimeError("DuckDB removed from HIVE pipeline. Use PostgreSQL (hive2_db.connect()).")


def _connect_db(read_only: bool = False):
    """Connect to the active database backend (PostgreSQL only)."""
    if _pg_available:
        return _hive_db.connect(read_only=read_only)
    raise RuntimeError("PostgreSQL backend not available. Set HIVE_DB_BACKEND=postgres and ensure hive2_pg.py is accessible.")


def _strip_sql_comments(sql: str) -> str:
    """Strip leading/inline -- comments from a SQL statement block."""
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue   # skip pure comment lines
        lines.append(line)
    return "\n".join(lines).strip()


def _init_db(con=None):
    """Ensure HIVE schema exists. PostgreSQL schema already created during migration."""
    if _pg_available:
        return
    raise RuntimeError("PostgreSQL required. DuckDB support removed.")
    con = _connect_db()
    try:
        raw_stmts = SCHEMA_SQL.split(";")
        statements = []
        for s in raw_stmts:
            cleaned = _strip_sql_comments(s)
            if cleaned:
                statements.append(cleaned)

        for stmt in statements:
            try:
                con.execute(stmt)
            except Exception as e:
                errmsg = str(e).lower()
                if "already exists" not in errmsg:
                    log.info(f"[HIVE] Schema warning: {e}")

        # ── Migrate hive_missions v1 → v2 (add missing columns) ─────────────
        existing_cols = {
            row[0].lower()
            for row in con.execute("DESCRIBE hive_missions").fetchall()
        }
        migrations = [
            ("status",            "VARCHAR DEFAULT 'scout'"),
            ("think_mode",        "BOOLEAN DEFAULT FALSE"),
            ("created_at",        "TIMESTAMP DEFAULT NOW()"),
            ("scout_done_at",     "TIMESTAMP"),
            ("harvest_done_at",   "TIMESTAMP"),
            ("process_done_at",   "TIMESTAMP"),
            ("synth_done_at",     "TIMESTAMP"),
            ("sources_processed", "INTEGER DEFAULT 0"),
            ("total_entities",    "INTEGER DEFAULT 0"),
            ("total_facts",       "INTEGER DEFAULT 0"),
            ("config",            "JSON"),
            # Quality metrics (v2.1 — aitmpl research-orchestrator pattern)
            ("quality_coverage",   "FLOAT"),
            ("quality_depth",      "FLOAT"),
            ("quality_confidence", "FLOAT"),
            ("specificity_score",  "FLOAT"),
        ]
        for col, col_def in migrations:
            if col not in existing_cols:
                try:
                    con.execute(f"ALTER TABLE hive_missions ADD COLUMN {col} {col_def}")
                except Exception as e:
                    log.info(f"[HIVE] Migration warning ({col}): {e}")

        # ── Migrate quality_metrics columns (hive_events infrastructure) ──────
        quality_cols = [
            ("quality_coverage",   "FLOAT DEFAULT NULL"),
            ("quality_depth",      "FLOAT DEFAULT NULL"),
            ("quality_confidence", "FLOAT DEFAULT NULL"),
        ]
        for col, col_def in quality_cols:
            if col not in existing_cols:
                try:
                    con.execute(f"ALTER TABLE hive_missions ADD COLUMN IF NOT EXISTS {col} {col_def}")
                except Exception as e:
                    log.info(f"[HIVE] Quality column migration ({col}): {e}")

        con.commit()
    finally:
        con.close()


def _get_mission(mission_id: str) -> dict | None:
    con = _connect_db(read_only=True)
    try:
        rows = con.execute(
            "SELECT * FROM hive_missions WHERE id = ?", [mission_id]
        ).fetchall()
        if not rows:
            return None
        desc = con.execute(
            "SELECT * FROM hive_missions WHERE id = ? LIMIT 0", [mission_id]
        ).description
        col_names = [d[0] for d in desc]
        return dict(zip(col_names, rows[0]))
    finally:
        con.close()


def _create_mission(mission_id: str, topic: str, think_mode: bool = False):
    con = _connect_db()
    try:
        con.execute(
            """INSERT INTO hive_missions(id, topic, status, think_mode, created_at)
               VALUES(?, ?, 'planning', ?, NOW())
               ON CONFLICT(id) DO NOTHING""",
            [mission_id, topic, think_mode]
        )
        con.commit()
    finally:
        con.close()


def _get_unresolved_gaps(mission_id: str) -> list:
    con = _connect_db(read_only=True)
    try:
        return con.execute(
            "SELECT gap_query, gap_type, priority FROM hive_gaps "
            "WHERE mission_id = ? AND resolved = FALSE ORDER BY priority DESC",
            [mission_id]
        ).fetchall()
    finally:
        con.close()

# ---------------------------------------------------------------------------
# Mission ID generator
# ---------------------------------------------------------------------------

def new_mission_id(topic: str) -> str:
    """Generate a unique mission identifier."""
    h = hashlib.md5(topic.encode()).hexdigest()[:6]
    return f"hive_{int(time.time())}_{h}"

# ---------------------------------------------------------------------------
# Phase runner
# ---------------------------------------------------------------------------

def run_phase(script: str, args: list[str], phase_name: str) -> float:
    """Run a HIVE bee script as subprocess, return elapsed seconds.
    
    Używa nohup + PID file żeby proces przeżył utratę sesji Hermes (tracker gubi PID po ~35min).
    Fazy synth (master_intelligence, synth*) używają detached double-fork żeby hive2.py
    mógł zakończyć się przed subproces otwiera DuckDB (cross-process write lock conflict).
    PID file: /tmp/hive_{phase_name}.pid
    """
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
        )
        # Tail log to stdout to maintain visibility
        subprocess.run(["grep", "-E", "⚙️|Process:|✅|❌|COMPLETE|ERROR", log_file], check=False)
        subprocess.run(["tail", "-5", log_file], check=False)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd)
    except FileNotFoundError:
        log.info(f"[HIVE] UWAGA: Skrypt {script_path} nie istnieje — faza pominięta.")
    except subprocess.CalledProcessError as e:
        log.info(f"[HIVE] BŁĄD w fazie {phase_name}: exit code {e.returncode}")
        raise
    elapsed = time.time() - t0
    log.info(f"✅  [{phase_name.upper()}] Zakończono w {elapsed:.1f}s")
    return elapsed

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _bar(phase: str, elapsed: float, width: int = 20) -> str:
    """Mini progress bar dla fazy."""
    icons = {
        'queen_plan': '👑', 'scout': '🔍', 'harvest': '🌾',
        'process': '⚙️ ', 'synth': '🔮', 'queen-synthesis': '🧠',
        'synth-fallback': '🆘', 'synth_final': '🍯',
    }
    icon = icons.get(phase, '🐝')
    filled = min(width, int(elapsed / 3))  # 1 block per 3s
    bar = '█' * filled + '░' * (width - filled)
    return f"  {icon} {phase:<18} [{bar}] {elapsed:6.1f}s"


def cmd_run(topic: str, think: bool = False, deep: bool = False, force: bool = False, scale: int = 200) -> None:
    """Full pipeline with Queen-first planning: plan → scout → harvest → process → synth."""
    _init_db(None)  # No-ops on PostgreSQL, creates tables on DuckDB legacy

    # ═══ PHASE -1: QUERY CLARIFICATION ═══════════════════════════
    try:
        from hive2_preprocessor import preprocess_query
        prep = preprocess_query(topic, interactive=True, force=force)
        if not prep.ok:
            log.info(f"\n  ❌  {prep.message}")
            sys.exit(1)
        topic = prep.refined_topic
        log.debug(f"  ✓  Query specificity: {prep.specificity_score:.2f}  →  '{topic}'")
        _specificity = prep.specificity_score
    except ImportError:
        _specificity = None

    mission_id = new_mission_id(topic)
    query = topic

    # Track active mission for SIGINT handler
    main._active_mission_id = mission_id
    log.debug(f'  🐝  HIVE 3.0  ·  {mission_id}')
    log.debug(f'  📋  {topic[:55]}{"…" if len(topic)>55 else ""}')
    flags = []
    if think: flags.append('🧠 think')
    if deep:  flags.append('🧬 deep')
    if flags: print(f'  {"  ".join(flags)}')
    log.debug(f'  🕐  {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC')
    log.debug(f'{"═"*62}\n')

    # ═══ PHASE 0: QUEEN PLANS ════════════════════════════════════
    log.debug('  ┌─ PHASE 0 · QUEEN PLANS SWARM ─────────────────────┐')
    t0 = time.time()

    # ── REC-05: QMP Primer inject (cross-mission memory) ─────────
    # Load primer from previous missions on similar topics.
    # Gives Queen access to trusted domains + gap queries sbefore full scout.
    try:
        from hive2_queen_primer import QueenPrimer
        _qp = QueenPrimer(mission_id)
        _primer = _qp.load_for_topic(topic, max_age_days=45)
        if _primer:
            _primer_block = _primer.to_prompt_block()
            log.debug(f"  🐝  QMP Primer: mission {_primer.mission_id[:20]}… "
                  f"q={_primer.primer_quality:.2f} depth={_primer.depth_modifier:.1f}x "
                  f"{len(_primer.top_domains)} trusted domains")
        else:
            _primer_block = ""
    except Exception as _pe:
        log.debug(f"Exception in orchestrator.py: {_pe}")
        _primer_block = ""

    planner = QueenPlanner(topic, think_mode=think, scale=scale)
    if _primer_block:
        planner._primer_context = _primer_block  # injected in _build_prompt / plan()
    plan = planner.plan()
    plan_file = f'/tmp/hive_plan_{mission_id}.json'
    with open(plan_file, 'w') as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    phase0_elapsed = time.time() - t0
    log.debug(f'  └─ {len(plan)} tasks planned · {phase0_elapsed:.1f}s ──────────────────┘\n')

    _create_mission(mission_id, topic, think_mode=think)

    # Save specificity score to mission record
    if _specificity is not None:
        try:
            con = _connect_db()
            con.execute(
                "UPDATE hive_missions SET specificity_score = ? WHERE id = ?",
                [_specificity, mission_id]
            )
            con.commit()
            con.close()
        except Exception as e:
            log.debug(f"Suppressed: {e}")

    _emit(mission_id, 'scout', 'started', data={'topic': topic, 'task_count': len(plan)})
    total_t0 = time.time()
    timings: dict[str, float] = {'queen_plan': phase0_elapsed}

    # ═══ PHASE 1: SCOUT ══════════════════════════════════════════
    log.debug('  ┌─ PHASE 1 · SCOUT SWARM ────────────────────────────┐')
    t1 = time.time()
    scout_script = str(HIVE_DIR / 'hive2_scout.py')
    scout_cmd = [PYTHON, scout_script, mission_id, '--plan', plan_file]
    sys.stdout.flush()
    try:
        result = subprocess.run(scout_cmd, check=False)
        if result.returncode != 0:
            log.info(f'  ⚠️  Scout exit {result.returncode} — continuing with partial results.')
    except FileNotFoundError:
        log.info(f'  ⚠️  hive2_scout.py not found — skipped.')
    timings['scout'] = time.time() - t1
    # Emit scout completed event
    try:
        # import duckdb as _ddb  # removed — using _connect_db()
        _con = _connect_db(read_only=True)
        _n_sources = _con.execute(
            "SELECT COUNT(*) FROM hive_sources WHERE mission_id=?", [mission_id]
        ).fetchone()[0]
        _con.close()
    except Exception:
        _n_sources = 0
    _emit(mission_id, 'scout', 'completed', data={'sources_found': _n_sources})
    log.info(f'  └─ done · {timings["scout"]:.1f}s ──────────────────────────────┘\n')

    # ═══ PHASE 1.5: DRONES (Trutnie) — rekon i penetracja barier ════════════
    log.debug('  ┌─ PHASE 1.5 · DRONES (Trutnie) ────────────────────────┐')
    log.debug('    🪲 Wall-breakers probing domains before harvest...')
    t_drone = time.time()
    try:
        from hive2_drones import extract_domains_from_sources, run_recon
        drone_domains = extract_domains_from_sources(mission_id)
        if drone_domains:
            drone_report = run_recon(mission_id, drone_domains)
            timings['drones'] = time.time() - t_drone
            pct = drone_report.get('coverage_pct', 0)
            blocked = drone_report.get('blocked', 0)
            log.info(f'    ✓ Recon complete: {pct}% accessible, {blocked} domains blocked')
        else:
            log.warning('    ⚠  No domains found in sources — skipping drone recon')
            timings['drones'] = 0.0
    except Exception as e:
        log.info(f'    ⚠  Drone recon error (non-fatal): {e}')
        timings['drones'] = 0.0
    log.info(f'  └─ done · {timings.get("drones", 0):.1f}s ──────────────────────────────┘\n')

    # ═══ PHASE 1.7: PRE-SCOUT (taniec + bee master + true scout) ════════════
    log.debug('  ┌─ PHASE 1.7 · PRE-SCOUT ───────────────────────────────┐')
    t_prescout = time.time()
    try:
        import asyncio as _aio
        import os as _os
        from hive2_prescout import run_prescout_pipeline
        _bm_mode   = "manual" if _os.environ.get("HIVE_BM_MANUAL") else "auto"
        _seeds_raw = _os.environ.get("HIVE_BM_SEEDS", "")
        _seed_urls = json.loads(_seeds_raw) if _seeds_raw else None
        _routing   = _aio.run(run_prescout_pipeline(
            mission_id, bm_mode=_bm_mode, seed_urls=_seed_urls
        ))
        timings['prescout'] = time.time() - t_prescout
        # Zapisz routing_map do pliku tymczasowego — HarvesterBee go odczyta
        _routing_path = f'/tmp/hive_routing_{mission_id}.json'
        with open(_routing_path, 'w') as _f:
            json.dump(_routing, _f, ensure_ascii=False)
        _approved_cnt = len(_routing)
        _skip_cnt     = _n_sources - _approved_cnt
        log.warning(f'    ✓ PreScout: {_approved_cnt} approved, ~{_skip_cnt} skipped before harvest')
        log.debug(f'    📋 Routing map: {_routing_path}')
    except Exception as _e:
        log.info(f'    ⚠  PreScout error (non-fatal): {_e}')
        timings['prescout'] = time.time() - t_prescout
    log.info(f'  └─ done · {timings.get("prescout", 0):.1f}s ─────────────────────────────┘\n')

    # ═══ PHASE 2: HARVEST ════════════════════════════════════════
    log.debug('  ┌─ PHASE 2 · HARVEST ────────────────────────────────┐')
    _emit(mission_id, 'harvest', 'started', data={'sources_to_harvest': _n_sources})
    try:
        timings['harvest'] = run_phase('hive2_harvest.py', [mission_id], 'harvest')
    except Exception as e:
        log.info(f'  ✗ Harvest failed: {e}')
        _emit(mission_id, 'harvest', 'error', data={'error': str(e)})
        raise
    # Emit harvest completed event
    try:
        # import duckdb as _ddb  # removed — using _connect_db()
        _con = _connect_db(read_only=True)
        _n_harvested = _con.execute(
            "SELECT COUNT(*) FROM hive_content WHERE mission_id=?", [mission_id]
        ).fetchone()[0]
        _con.close()
    except Exception:
        _n_harvested = 0
    _emit(mission_id, 'harvest', 'completed', data={'sources_harvested': _n_harvested})
    log.info(f'  └─ done · {timings["harvest"]:.1f}s ──────────────────────────────┘\n')

    # ═══ PHASE 3: PROCESS ════════════════════════════════════════
    log.debug('  ┌─ PHASE 3 · PROCESS ────────────────────────────────┐')
    _emit(mission_id, 'process', 'started')
    try:
        process_args = [mission_id]
        if deep:
            process_args.append("--deep")
        timings['process'] = run_phase('hive2_process.py', process_args, 'process')
    except Exception as e:
        log.info(f'  ✗ Process failed: {e}')
        _emit(mission_id, 'process', 'error', data={'error': str(e)})
        raise
    # Emit process completed event
    try:
        # import duckdb as _ddb  # removed — using _connect_db()
        _con = _connect_db(read_only=True)
        _n_docs = _con.execute(
            "SELECT COUNT(*) FROM hive_entities WHERE mission_id=?", [mission_id]
        ).fetchone()[0]
        _con.close()
    except Exception:
        _n_docs = 0
    _emit(mission_id, 'process', 'completed', data={'docs_processed': _n_docs})
    log.info(f'  └─ done · {timings["process"]:.1f}s ──────────────────────────────┘\n')

    # ═══ PHASE 3.5: FALSIFIER (claim dedup + cross-validation) ══
    log.debug('  ┌─ PHASE 3.5 · FALSIFIER ────────────────────────────┐')
    try:
        timings['falsifier'] = run_phase(
            'hive2_falsifier.py', [mission_id], 'falsifier'
        )
    except Exception as e_f:
        log.info(f'  ⚠️  Falsifier skipped: {e_f}')
        timings['falsifier'] = 0.0
    log.info(f'  └─ done · {timings.get("falsifier", 0):.1f}s ──────────────────────────────┘\n')

    # ═══ PHASE 3.7: STORM ITERATIVE GAP-FILL ════════════════════
    # STORM Pattern: after extraction, check what's missing → generate
    # follow-up queries → mini-harvest round 2 targeting the gaps.
    # Only runs if < 30 claims were collected (signal that harvest was thin).
    log.debug('  ┌─ PHASE 3.7 · STORM GAP-FILL ───────────────────────┐')
    try:
        # import duckdb as _ddb  # removed — using _connect_db()
        _con = _connect_db(read_only=True)
        _claim_count = _con.execute(
            "SELECT COUNT(*) FROM hive_claims WHERE mission_id=?", [mission_id]
        ).fetchone()[0]
        _con.close()
        if _claim_count < 30:
            log.warning(f'  ⚠️  Only {_claim_count} claims — triggering STORM gap-fill queries...')
            # Build gap-fill queries from open hive_gaps
            _con2 = _connect_db(read_only=True)
            _gaps = _con2.execute(
                "SELECT gap_query FROM hive_gaps WHERE mission_id=? AND resolved=FALSE ORDER BY priority DESC LIMIT 5",
                [mission_id]
            ).fetchall()
            _con2.close()
            if _gaps:
                import boto3 as _b3, json as _jj
                _bc = _b3.client('bedrock-runtime', region_name='eu-central-1')
                _gap_list = "\n".join(f"- {g[0]}" for g in _gaps)
                _gfp = f"""You are a research director. These intelligence gaps remain after initial research:
{_gap_list}

Generate 5 targeted follow-up search queries (site: operators where possible) to fill these specific gaps.
Return JSON array of 5 strings only."""
                _resp = _bc.invoke_model(
                    modelId='eu.anthropic.claude-haiku-4-5-20251001-v1:0',
                    body=_jj.dumps({'anthropic_version': 'bedrock-2023-05-31',
                                    'max_tokens': 800, 'temperature': 0.4,
                                    'messages': [{'role': 'user', 'content': _gfp}]})
                )
                _txt = _jj.loads(_resp['body'].read())['content'][0]['text'].strip()
                if _txt.startswith('```'):
                    _txt = _txt.split('\n', 1)[1].rsplit('```', 1)[0].strip()
                _fq = _jj.loads(_txt)
                if isinstance(_fq, list) and len(_fq) >= 1:
                    # ── REC-10: Spillover query log ─────────────────────────
                    # Save as gap_type='spillover' (distinguishable in analytics from gap_type=None)
                    _con3 = _connect_db()
                    for _q in _fq[:5]:
                        _con3.execute(
                            "INSERT INTO hive_gaps (id, mission_id, gap_query, gap_type, priority, resolved, created_at) "
                            "VALUES (nextval('hive_facts_seq'), ?, ?, 'spillover', 7, FALSE, CURRENT_TIMESTAMP)",
                            [mission_id, f"[STORM-SPILLOVER] {_q}"]
                        )
                    _con3.close()
                    log.info(f'  ✓ STORM: {len(_fq)} spillover queries logged (gap_type=spillover)')
                    # Run mini-scout + harvest for follow-up queries
                    import subprocess as _sp
                    for _fqi, _fqq in enumerate(_fq[:3]):  # max 3 to avoid timeout
                        try:
                            _sp.run(
                                ['python3', 'hive2_scout.py', mission_id, '--followup', _fqq],
                                timeout=60, capture_output=True
                            )
                        except Exception as e:
                            log.debug(f"Suppressed: {e}")
                    log.info(f'  ✓ STORM gap-fill scout done — re-running process...')
                    run_phase('hive2_process.py', [mission_id], 'gap-fill-process')
                    # Mark original gaps as resolved after successful fill
                    _con_res = _connect_db()
                    _con_res.execute(
                        "UPDATE hive_gaps SET resolved = TRUE WHERE mission_id = ? AND resolved = FALSE AND gap_type != 'spillover'",
                        [mission_id]
                    )
                    _con_res.close()
                    log.info(f'  ✓ Original gaps marked resolved')
        else:
            log.info(f'  ✓ {_claim_count} claims collected — gap-fill not needed')
    except Exception as e_gf:
        log.warning(f'  ⚠️  Gap-fill skipped: {e_gf}')
    log.info(f'  └─ done ──────────────────────────────────────────────┘\n')

    # ═══ PHASE 3.9: HARVEST QUALITY GATE ════════════════════════
    # Before Queen synthesises, audit data quality.
    # If >60% sources are errors/paywalls, inject DATA QUALITY WARNING.
    log.debug('  ┌─ PHASE 3.9 · HARVEST QUALITY GATE ────────────────┐')
    _quality_warning = ""
    try:
        # duckdb import removed — _connect_db() handles routing
        _qcon = _connect_db(read_only=True)
        _total_src = _qcon.execute(
            "SELECT COUNT(*) FROM hive_sources WHERE mission_id=?", [mission_id]
        ).fetchone()[0]
        _success_src = _qcon.execute(
            "SELECT COUNT(*) FROM hive_content WHERE mission_id=? AND word_count >= 100",
            [mission_id]
        ).fetchone()[0]
        _total_claims = _qcon.execute(
            "SELECT COUNT(*) FROM hive_claims WHERE mission_id=?", [mission_id]
        ).fetchone()[0]
        # Unique domains
        _unique_domains = _qcon.execute(
            "SELECT COUNT(DISTINCT domain) FROM hive_sources WHERE mission_id=?",
            [mission_id]
        ).fetchone()[0]
        # Median word count
        _median_wc_row = _qcon.execute(
            "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY word_count) "
            "FROM hive_content WHERE mission_id=?", [mission_id]
        ).fetchone()
        _median_wc = int(_median_wc_row[0] or 0) if _median_wc_row else 0
        _qcon.close()

        _success_pct = (_success_src / _total_src * 100) if _total_src > 0 else 0
        log.info(f"  📊  Quality: {_success_src}/{_total_src} sources full content "
              f"({_success_pct:.0f}%), {_unique_domains} unique domains, "
              f"median wc={_median_wc}, {_total_claims} claims")

        if _success_pct < 40 or _unique_domains < 5 or _total_claims < 10:
            _quality_warning = (
                f"\n\n⚠️  **DATA QUALITY WARNING**\n"
                f"- Full content: {_success_src}/{_total_src} sources ({_success_pct:.0f}%) — "
                f"{'majority are paywalls/errors' if _success_pct < 40 else 'acceptable'}\n"
                f"- Unique domains: {_unique_domains} "
                f"{'(low diversity — echo chamber risk)' if _unique_domains < 5 else ''}\n"
                f"- Claims extracted: {_total_claims} "
                f"{'(insufficient for confident synthesis)' if _total_claims < 10 else ''}\n"
                f"- Median word count per source: {_median_wc} words\n"
                f"**Treat findings as preliminary. Manual verification recommended.**\n"
            )
            # Persist warning to DB so synth picks it up
            _wcon = _connect_db()
            _wcon.execute(
                "INSERT INTO hive_gaps (id, mission_id, gap_query, gap_type, priority, resolved, created_at) "
                "VALUES (nextval('hive_facts_seq'), ?, ?, 'quality_warning', 10, FALSE, CURRENT_TIMESTAMP)",
                [mission_id, f"[QUALITY-WARNING] {_quality_warning}"]
            )
            _wcon.close()
            log.debug(f"  ⚠️  Quality gate FAILED — warning injected into report")
        else:
            log.debug(f"  ✅  Quality gate passed")
    except Exception as e_qg:
        log.debug(f"  ⚠️  Quality gate skipped: {e_qg}")
    log.info(f'  └─ done ──────────────────────────────────────────────┘\n')

    # ═══ PHASE 4: QUEEN MASTER INTELLIGENCE ══════════════════════
    log.debug('  ┌─ PHASE 4 · QUEEN SYNTHESIS ────────────────────────┐')
    _emit(mission_id, 'synth', 'started')
    import gc as _gc
    _gc.collect()
    try:
        timings['synth'] = run_phase(
            'hive2_master_intelligence.py', [mission_id, query], 'queen-synthesis'
        )
    except Exception as e:
        log.info(f'  ⚠️  Queen MI failed: {e} — fallback synth')
        _gc.collect()
        try:
            timings['synth'] = run_phase('hive2_synth.py', [mission_id], 'synth-fallback')
        except Exception as e2:
            log.info(f'  ✗ Fallback synth failed: {e2}')
            _emit(mission_id, 'synth', 'error', data={'error': str(e2)})
            # Mark interrupted instead of leaving status hanging — resumable
            try:
                _con_int = _connect_db()
                _con_int.execute(
                    "UPDATE hive_missions SET status='interrupted' WHERE id=? AND status NOT IN ('done','cancelled')",
                    [mission_id]
                )
                _con_int.commit()
                _con_int.close()
            except Exception as e:
                log.debug(f"Suppressed: {e}")
            # Don't raise — log and continue (pipeline can still write partial result)
            log.info(f'  ⚠️  Synth failed — mission marked interrupted, resumable via: hive2 resume {mission_id}')
    _emit(mission_id, 'synth', 'completed')
    log.info(f'  └─ done · {timings.get("synth", 0):.1f}s ──────────────────────────────┘\n')

    # ── REC-05b: QMP Primer flush ─────────────────────────────────
    # After synthesis, save this mission as primer for similar topics in future.
    try:
        from hive2_queen_primer import QueenPrimer
        import hive2_db as _hive_db_qp
        _con_qp = _hive_db_qp.connect(read_only=True)
        _qp_row = _con_qp.execute(
            "SELECT COALESCE(quality_confidence, 0.5) FROM hive_missions WHERE id=?",
            [mission_id]
        ).fetchone()
        _con_qp.close()
        _qp_quality = _qp_row[0] if _qp_row else 0.5
        QueenPrimer(mission_id).save(topic, quality_score=_qp_quality)
        log.info(f'  🐝  QMP Primer: saved (q={_qp_quality:.2f}) for future missions')
    except Exception as _pfe:
        log.debug(f"Exception in orchestrator.py: {_pfe}")
        pass  # non-critical

    # ═══ PHASE 5: MASTER INTELLIGENCE SUMMARY (cross-mission) ════
    try:
        import subprocess as _subp
        _subp.Popen(
            [PYTHON, str(HIVE_DIR / 'hive2_intel_summary.py'), 'update', mission_id],
            stdout=open('/tmp/hive_intel_summary.log', 'a'),
            stderr=subprocess.STDOUT,
            env={**os.environ, 'PATH': f'{os.environ.get("PATH","")}'}
        )
        log.debug('  🧠  Master Intelligence Summary update triggered (background)')
    except Exception as _e:
        log.info(f'  ⚠️  Intel summary update skipped: {_e}')

    # ═══ PHASE 5b: KNOWLEDGE GRAPH REBUILD (background) ════
    try:
        import subprocess as _subp2
        _subp2.Popen(
            [PYTHON, str(HIVE_DIR / 'hive2_graph_engine.py'), 'build'],
            stdout=open('/tmp/hive_graph_build.log', 'a'),
            stderr=subprocess.STDOUT,
            env={**os.environ, 'PATH': f'{os.environ.get("PATH","")}'}
        )
        log.debug('  🕸️   Knowledge Graph rebuild triggered (background)')
    except Exception as _e:
        log.info(f'  ⚠️  Graph rebuild skipped: {_e}')

    # ═══ THINK MODE ═══════════════════════════════════════════════
    if think:
        log.debug(f'\n  🧠  THINK MODE — scanning research gaps …')
        gaps = _get_unresolved_gaps(mission_id)
        gaps_to_process = gaps[:5]
        log.debug(f'      Found {len(gaps)} gaps, processing {len(gaps_to_process)}.')
        for i, (gap_query, gap_type, priority) in enumerate(gaps_to_process, 1):
            log.debug(f'\n  🔍  Gap {i}/{len(gaps_to_process)}: {gap_query}')
            gap_mission_id = new_mission_id(gap_query)
            _create_mission(gap_mission_id, gap_query)
            log.debug(f'  👑  Queen planning gap scout …')
            gap_planner = QueenPlanner(gap_query)
            gap_plan = gap_planner.plan()
            gap_plan_file = f'/tmp/hive_plan_{gap_mission_id}.json'
            with open(gap_plan_file, 'w') as gf:
                json.dump(gap_plan, gf, ensure_ascii=False)
            try:
                gap_scout_cmd = [PYTHON, str(HIVE_DIR / 'hive2_scout.py'),
                                 gap_mission_id, '--plan', gap_plan_file]
                subprocess.run(gap_scout_cmd, check=False)
                run_phase('hive2_harvest.py', [gap_mission_id], f'gap-{i}-harvest')
                run_phase('hive2_process.py', [gap_mission_id], f'gap-{i}-process')
                # Mark gap as resolved after successful processing
                _gcon = _connect_db()
                _gcon.execute(
                    "UPDATE hive_gaps SET resolved = TRUE WHERE mission_id = ? AND gap_query = ?",
                    [mission_id, gap_query]
                )
                _gcon.close()
                log.info(f'  ✓  Gap {i} marked resolved: {gap_query[:60]}')
            except Exception as e:
                log.info(f'  ⚠️  Gap {i} failed: {e} — continuing.')

        log.debug(f'\n  🔮  THINK MODE — final synthesis after gap fill …')
        try:
            timings['synth_final'] = run_phase('hive2_synth.py', [mission_id], 'synth-final')
        except Exception as e:
            log.info(f'  ✗ Final synth failed: {e}')

    total_elapsed = time.time() - total_t0
    log.debug(f'\n{"═"*62}')
    log.info(f'  🍯  MISSION COMPLETE  ·  {mission_id}')
    log.debug(f'  ⏱️   Total: {total_elapsed:.1f}s\n')
    for phase, t in timings.items():
        log.debug(_bar(phase, t))
    log.debug(f'\n{"═"*62}\n')
    _emit(mission_id, 'done', 'done', data={'total_elapsed': total_elapsed, 'phases': list(timings.keys())})


def cmd_scout(topic: str) -> None:
    """Scout only — Queen plans first, then runs hive2_scout.py."""
    # duckdb removed — PG is primary
    _init_db(None)

    mission_id = new_mission_id(topic)
    _create_mission(mission_id, topic)

    log.debug(f'[HIVE] Scout: mission_id={mission_id}, topic={topic}')

    # Queen plans the swarm
    log.debug('\n╔═══ QUEEN PLANS SCOUT SWARM ═══╗')
    t0 = time.time()
    planner = QueenPlanner(topic)
    plan = planner.plan()
    plan_file = f'/tmp/hive_plan_{mission_id}.json'
    with open(plan_file, 'w') as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    log.debug(f'╚═══ Plan ready: {len(plan)} tasks → {plan_file} ({time.time()-t0:.1f}s) ═══╝\n')

    # Launch scout with plan
    scout_script = str(HIVE_DIR / 'hive2_scout.py')
    scout_cmd = [PYTHON, scout_script, mission_id, '--plan', plan_file]
    log.debug(f"🐝  [SCOUT] {' '.join(scout_cmd)}")
    sys.stdout.flush()
    try:
        subprocess.run(scout_cmd, check=False)
    except FileNotFoundError:
        log.info(f'[HIVE] UWAGA: hive2_scout.py nie istnieje — faza pominięta.')

    log.debug(f'\n[HIVE] Mission ID do dalszego użycia: {mission_id}')


def cmd_plan(topic: str, output_file: str | None = None) -> None:
    """
    Queen planning only — generate and print the scout task JSON.
    Useful for inspecting what the Queen would plan for a given topic.
    Optionally saves to output_file.
    """
    log.debug(f'\n👑  QUEEN PLANNING — Topic: {topic}\n')
    t0 = time.time()
    planner = QueenPlanner(topic)
    plan = planner.plan()
    elapsed = time.time() - t0

    dest = output_file or f'/tmp/hive_plan_inspect_{int(time.time())}.json'
    with open(dest, 'w') as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    log.info(f'\n✅  {len(plan)} tasks planned in {elapsed:.1f}s → {dest}')

    # Print summary table
    log.debug(f'\n{"─"*50}')
    log.debug(f'  {"SOURCE":<20} {"COUNT":>5}  {"AVG_PRIORITY":>12}')
    log.debug(f'{"─"*50}')
    from collections import defaultdict
    by_src: dict[str, list] = defaultdict(list)
    for t in plan:
        by_src[t.get('source', '?')].append(t.get('priority', 5))
    for src, prios in sorted(by_src.items(), key=lambda x: -len(x[1])):
        avg = sum(prios) / len(prios)
        log.debug(f'  {src:<20} {len(prios):>5}  {avg:>12.1f}')
    log.debug(f'{"─"*50}\n')

    # Also print pretty JSON
    log.debug(json.dumps(plan, ensure_ascii=False, indent=2))


def cmd_harvest(mission_id: str) -> None:
    """Harvest existing mission."""
    # duckdb removed — PG is primary
    _init_db(None)
    if not _get_mission(mission_id):
        log.info(f"[HIVE] Misja '{mission_id}' nie znaleziona w DB.")
        sys.exit(1)
    run_phase('hive2_harvest.py', [mission_id], 'harvest')


# ── P3.7: --resume — checkpoint-based recovery ────────────────────────────────
_PHASE_ORDER = ['scout', 'harvest', 'process', 'synth', 'done']

def cmd_resume(mission_id: str) -> None:
    """Resume mission from last completed phase (checkpoint)."""
    # duckdb removed — PG is primary
    _init_db(None)
    m = _get_mission(mission_id)
    if not m:
        log.info(f"[HIVE] Misja '{mission_id}' nie znaleziona w DB.")
        sys.exit(1)

    # Ustal aktualny status z DB
    con = _connect_db(read_only=True)
    row = con.execute("SELECT status, phase FROM hive_missions WHERE id=?", [mission_id]).fetchone()
    con.close()
    current_status = (row[0] or row[1] or 'scout').lower() if row else 'scout'

    log.info(f"[HIVE] Resume misji {mission_id} — status: {current_status}")

    # Check data state in DB (determines start point regardless of status)
    con2 = _connect_db(read_only=True)
    harvested = con2.execute(
        "SELECT COUNT(*) FROM hive_sources WHERE mission_id=?", [mission_id]
    ).fetchone()[0]
    # sources_harvested = sources with full content (status done in hive_content)
    sources_harvested = con2.execute(
        "SELECT COUNT(*) FROM hive_content WHERE mission_id=? AND word_count > 0", [mission_id]
    ).fetchone()[0]
    processed = con2.execute(
        "SELECT COUNT(*) FROM hive_entities WHERE mission_id=?", [mission_id]
    ).fetchone()[0]
    # has_synth = synthesis in DB OR status already done (FIX: do not rely solely on synthesis)
    synth_row = con2.execute(
        "SELECT synthesis, status FROM hive_missions WHERE id=?", [mission_id]
    ).fetchone()
    has_synth = bool(
        synth_row and (
            (synth_row[0] and len(synth_row[0]) > 500) or  # synthesis text w DB
            (synth_row[1] == 'done')                        # status done
        )
    )
    con2.close()

    log.info(f"[HIVE]   sources znalezione:  {harvested}")
    log.info(f"[HIVE]   sources z treścią:   {sources_harvested}")
    log.info(f"[HIVE]   entities przetworz:  {processed}")
    log.info(f"[HIVE]   synteza gotowa:      {has_synth}")

    # Dla cancelled/interrupted — unlock status so pipeline can continue
    if current_status in ('cancelled', 'interrupted'):
        con_rw = _connect_db()
        # Cofnij do odpowiedniej fazy na podstawie danych
        if processed > 0:
            resume_status = 'process'
        elif sources_harvested > 0:
            resume_status = 'harvest'
        else:
            resume_status = 'scout'
        con_rw.execute(
            "UPDATE hive_missions SET status=?, cancelled_at=NULL WHERE id=?",
            [resume_status, mission_id]
        )
        con_rw.commit()
        con_rw.close()
        log.info(f"[HIVE] ↩ Cancelled → {resume_status} (odblokowuję misję)")

    # Wybierz punkt startowy na podstawie danych (nie statusu)
    if has_synth:
        log.info(f"[HIVE] ✅ Synteza gotowa — nic do wznowienia.")
        return
    elif processed > 0:
        log.info(f"[HIVE] ⚡ Wznawiamy od synth (process done, {processed} entities)")
        cmd_synth(mission_id)
        run_phase('hive2_falsifier.py', ['--mission', mission_id], 'falsify')
    elif sources_harvested > 0:
        log.info(f"[HIVE] ⚡ Wznawiamy od process (harvest done, {sources_harvested} źródeł z treścią)")
        cmd_process(mission_id)
    elif harvested > 0:
        log.info(f"[HIVE] ⚡ Wznawiamy od harvest (znalezione: {harvested} sources, brak treści)")
        cmd_harvest(mission_id)
        cmd_process(mission_id)
    else:
        log.info(f"[HIVE] ⚡ Wznawiamy od harvest (brak danych)")
        cmd_harvest(mission_id)
        cmd_process(mission_id)


def cmd_process(mission_id: str) -> None:
    """Process existing mission."""
    # duckdb removed — PG is primary
    _init_db(None)
    if not _get_mission(mission_id):
        log.info(f"[HIVE] Misja '{mission_id}' nie znaleziona w DB.")
        sys.exit(1)
    run_phase('hive2_process.py', [mission_id], 'process')

    # ── P2.4: Auto-pipe process → synth → falsify ─────────────────────────────
    log.info(f"\n[HIVE] ⚡ Auto-pipe: process → synth → falsify")
    cmd_synth(mission_id)

    log.info(f"\n[HIVE] ⚡ Auto-pipe: uruchamiam falsifikację")
    try:
        run_phase('hive2_falsifier.py', ['--mission', mission_id], 'falsify')
    except Exception as e:
        log.info(f"[HIVE] Falsifikacja failed ({e}) — raport już zapisany")


def cmd_synth(mission_id: str) -> None:
    """Synthesize existing mission."""
    # duckdb removed — PG is primary
    _init_db(None)
    if not _get_mission(mission_id):
        log.info(f"[HIVE] Misja '{mission_id}' nie znaleziona w DB.")
        sys.exit(1)
    # Pobierz query misji z DB
    con = _connect_db(read_only=True)
