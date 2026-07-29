"""Queen Planner — LLM-driven mission planning for BeHive research pipeline.

Extracted from orchestrator.py to reduce god-function size.
The Queen analyzes a topic and generates 200+ parallel scout tasks.
"""

import os
import json
import time
import logging
import traceback
from collections import Counter

log = logging.getLogger(__name__)

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
        try:
            import boto3 as _boto3
        except ImportError:
            from behive.engine.bedrock_compat import get_bedrock_compat_client as _bcc
            class _boto3:
                @staticmethod
                def client(*a, **kw):
                    return _bcc(stage="scout")
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
        try:
            import boto3 as _boto3
        except ImportError:
            from behive.engine.bedrock_compat import get_bedrock_compat_client as _bcc
            class _boto3:
                @staticmethod
                def client(*a, **kw):
                    return _bcc(stage="scout")
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

# PostgreSQL backend (preferred) — checked lazily to avoid import-order issues
_pg_available = None  # Will be set on first use
_hive_db = None
