-- ═══════════════════════════════════════════════════════════════════
-- BeHive PostgreSQL Schema — Full Production
-- Runs automatically on first docker compose up
-- ═══════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ─── Core Tables ─────────────────────────────────────────────────

-- Missions (research sessions)
CREATE TABLE IF NOT EXISTS hive_missions (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    phase TEXT DEFAULT 'scout',
    depth INTEGER DEFAULT 3,
    sources_found INTEGER DEFAULT 0,
    sources_scored INTEGER DEFAULT 0,
    sources_harvested INTEGER DEFAULT 0,
    sources_processed INTEGER DEFAULT 0,
    total_words INTEGER DEFAULT 0,
    total_files INTEGER DEFAULT 0,
    total_entities INTEGER DEFAULT 0,
    total_facts INTEGER DEFAULT 0,
    synthesis TEXT,
    think_mode BOOLEAN DEFAULT FALSE,
    config JSONB DEFAULT '{}'::jsonb,
    quality_coverage REAL,
    quality_depth REAL,
    quality_confidence REAL,
    specificity_score REAL,
    quality_metrics JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    scout_done_at TIMESTAMPTZ,
    harvest_done_at TIMESTAMPTZ,
    process_done_at TIMESTAMPTZ,
    synth_done_at TIMESTAMPTZ
);

-- Sources (discovered URLs)
CREATE TABLE IF NOT EXISTS hive_sources (
    id SERIAL PRIMARY KEY,
    mission_id TEXT REFERENCES hive_missions(id),
    url TEXT NOT NULL,
    domain TEXT,
    title TEXT,
    snippet TEXT,
    source_type TEXT,
    discovered_at TIMESTAMPTZ DEFAULT NOW(),
    score_relevance INTEGER DEFAULT 0,
    score_freshness INTEGER DEFAULT 0,
    score_density INTEGER DEFAULT 0,
    score_authority INTEGER DEFAULT 0,
    score_accessibility INTEGER DEFAULT 0,
    score_uniqueness INTEGER DEFAULT 0,
    score_depth INTEGER DEFAULT 0,
    score_total INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    harvest_method TEXT
);

CREATE INDEX IF NOT EXISTS idx_sources_mission ON hive_sources(mission_id);
CREATE INDEX IF NOT EXISTS idx_sources_domain ON hive_sources(domain);

-- Content (harvested page content)
CREATE TABLE IF NOT EXISTS hive_content (
    id SERIAL PRIMARY KEY,
    mission_id TEXT REFERENCES hive_missions(id),
    url TEXT NOT NULL,
    domain TEXT,
    title TEXT,
    raw_text TEXT,
    word_count INTEGER DEFAULT 0,
    harvest_method TEXT,
    language TEXT,
    quality_score DOUBLE PRECISION DEFAULT 0.0,
    author TEXT,
    published_date TEXT,
    keywords JSONB,
    harvested_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_content_mission ON hive_content(mission_id);
CREATE INDEX IF NOT EXISTS idx_content_quality ON hive_content(quality_score DESC);

-- Claims (extracted facts with scores)
CREATE TABLE IF NOT EXISTS hive_claims (
    id SERIAL PRIMARY KEY,
    mission_id TEXT REFERENCES hive_missions(id),
    claim TEXT NOT NULL,
    evidence TEXT,
    source_url TEXT,
    claim_type TEXT DEFAULT 'fact',
    confidence REAL DEFAULT 0.5,
    quality_score DOUBLE PRECISION DEFAULT 0.0,
    quality_details JSONB,
    source_operation TEXT,
    source_tier INTEGER DEFAULT 3,
    extracted_at TIMESTAMPTZ DEFAULT NOW(),
    verified BOOLEAN DEFAULT FALSE,
    evidence_flags TEXT,
    is_garbage BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_claims_mission ON hive_claims(mission_id);
CREATE INDEX IF NOT EXISTS idx_claims_quality ON hive_claims(quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_claims_text_trgm ON hive_claims USING gin(claim gin_trgm_ops);

-- Entities (extracted named entities)
CREATE TABLE IF NOT EXISTS hive_entities (
    id SERIAL PRIMARY KEY,
    mission_id TEXT REFERENCES hive_missions(id),
    source_url TEXT,
    entity_type TEXT,
    value TEXT NOT NULL,
    context TEXT,
    confidence REAL DEFAULT 0.5,
    extracted_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entities_mission ON hive_entities(mission_id);
CREATE INDEX IF NOT EXISTS idx_entities_type ON hive_entities(entity_type);

-- Triples (subject-predicate-object relationships)
CREATE TABLE IF NOT EXISTS hive_triples (
    id SERIAL PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES hive_missions(id),
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence DOUBLE PRECISION DEFAULT 0.5,
    source_count INTEGER DEFAULT 1,
    sources JSONB
);

CREATE INDEX IF NOT EXISTS idx_triples_mission ON hive_triples(mission_id);

-- Gaps (identified knowledge gaps for follow-up research)
CREATE TABLE IF NOT EXISTS hive_gaps (
    id SERIAL PRIMARY KEY,
    mission_id TEXT REFERENCES hive_missions(id),
    gap_query TEXT NOT NULL,
    gap_type TEXT,
    priority INTEGER DEFAULT 5,
    resolved BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Intelligence Tables ─────────────────────────────────────────

-- Events (real-time progress / SSE streaming)
CREATE SEQUENCE IF NOT EXISTS hive_events_seq;
CREATE TABLE IF NOT EXISTS hive_events (
    id INTEGER PRIMARY KEY DEFAULT nextval('hive_events_seq'),
    mission_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    event_type TEXT NOT NULL,
    data JSONB,
    quality_coverage REAL,
    quality_depth REAL,
    quality_confidence REAL,
    ts TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_mission ON hive_events(mission_id, ts);

-- Domain reputation (track which domains yield high-quality content)
CREATE TABLE IF NOT EXISTS hive_domain_reputation (
    domain TEXT PRIMARY KEY,
    avg_quality REAL DEFAULT 0.0,
    harvest_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    last_harvest_at TIMESTAMPTZ,
    last_bad_at TIMESTAMPTZ,
    consecutive_bad INTEGER DEFAULT 0,
    blacklisted BOOLEAN DEFAULT FALSE,
    blacklist_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Drone map (stealth strategy per domain)
CREATE TABLE IF NOT EXISTS drone_map (
    mission_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    strategy TEXT,
    block_type TEXT,
    working_ua TEXT,
    cffi_target TEXT,
    primp_target TEXT,
    robots_json TEXT,
    delay_ms INTEGER,
    via_jina BOOLEAN DEFAULT FALSE,
    via_wayback BOOLEAN DEFAULT FALSE,
    via_archive_ph BOOLEAN DEFAULT FALSE,
    via_nodriver BOOLEAN DEFAULT FALSE,
    via_patchright BOOLEAN DEFAULT FALSE,
    probe_status INTEGER,
    probe_ms INTEGER,
    notes_json TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (mission_id, domain)
);

-- Clusters (topic clustering of content)
CREATE TABLE IF NOT EXISTS hive_clusters (
    id SERIAL PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES hive_missions(id),
    cluster_id INTEGER,
    topic_label TEXT,
    keywords JSONB,
    document_count INTEGER DEFAULT 0,
    urls JSONB,
    representative_text TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    centroid_score DOUBLE PRECISION DEFAULT 0.0
);

-- ─── Queen / Planning Tables ─────────────────────────────────────

-- Queen primers (research strategy plans)
CREATE TABLE IF NOT EXISTS hive_queen_primers (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES hive_missions(id),
    topic TEXT NOT NULL,
    topic_keywords JSONB,
    top_domains JSONB,
    recommended_queries JSONB,
    key_entities JSONB,
    depth_modifier REAL DEFAULT 1.0,
    primer_quality REAL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Queen facts (persistent knowledge across missions)
CREATE TABLE IF NOT EXISTS hive_queen_facts (
    id TEXT PRIMARY KEY,
    fact_text TEXT NOT NULL,
    source_type TEXT,
    mission_id TEXT,
    agent_id TEXT,
    run_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    linked_ids TEXT
);

-- Numerical facts (verified data points)
CREATE TABLE IF NOT EXISTS hive_facts (
    id SERIAL PRIMARY KEY,
    mission_id TEXT REFERENCES hive_missions(id),
    value DOUBLE PRECISION,
    unit TEXT,
    context TEXT,
    source_count INTEGER DEFAULT 1,
    sources JSONB,
    domains JSONB,
    confidence TEXT,
    is_outlier BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Feedback / Learning Tables ──────────────────────────────────

-- Claim feedback (user ratings for quality calibration)
CREATE TABLE IF NOT EXISTS hive_claim_feedback (
    id SERIAL PRIMARY KEY,
    claim_id TEXT NOT NULL,
    mission_id TEXT,
    rating SMALLINT NOT NULL,
    context TEXT,
    source_operation TEXT,
    source_tier INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ─── Indexes for Performance ─────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_sources_status ON hive_sources(status);
CREATE INDEX IF NOT EXISTS idx_content_url ON hive_content(url);
CREATE INDEX IF NOT EXISTS idx_entities_value ON hive_entities(value);
CREATE INDEX IF NOT EXISTS idx_facts_mission ON hive_facts(mission_id);
CREATE INDEX IF NOT EXISTS idx_clusters_mission ON hive_clusters(mission_id);

-- ─── Bee Results ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hive_bee_results (
    id              TEXT PRIMARY KEY,
    mission_id      TEXT REFERENCES hive_missions(id),
    doc_id          TEXT,
    operation_id    TEXT,
    operation_name  TEXT,
    tier            INTEGER,
    relevance_score REAL,
    output_json     TEXT,
    confidence      REAL,
    processing_ms   INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bee_results_mission ON hive_bee_results(mission_id);

-- ─── Citations ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hive_citations (
    id                  SERIAL PRIMARY KEY,
    mission_id          TEXT REFERENCES hive_missions(id),
    claim               TEXT,
    source_url          TEXT,
    source_domain       TEXT,
    source_title        TEXT,
    author              TEXT,
    published_date      TEXT,
    confidence          DOUBLE PRECISION,
    cross_validated     BOOLEAN DEFAULT FALSE,
    supporting_sources  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_citations_mission ON hive_citations(mission_id);

-- ─── Queen Assessments ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hive_queen_assessments (
    id                      TEXT PRIMARY KEY,
    mission_id              TEXT REFERENCES hive_missions(id),
    query                   TEXT,
    queen_json              TEXT,
    must_know_insight       TEXT,
    hausdorff_score         REAL,
    confidence_raw          REAL,
    confidence_calibrated   REAL,
    bee_ops_count           INTEGER,
    rag_chunks_count        INTEGER,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_queen_assessments_mission ON hive_queen_assessments(mission_id);
