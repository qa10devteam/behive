-- BeHive PostgreSQL schema initialization
-- Runs automatically on first docker compose up

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Missions
CREATE TABLE IF NOT EXISTS hive_missions (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    phase TEXT DEFAULT 'scout',
    scale INTEGER DEFAULT 30,
    depth INTEGER DEFAULT 3,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    total_claims INTEGER DEFAULT 0,
    avg_quality REAL DEFAULT 0.0,
    synthesis TEXT,
    error TEXT,
    metadata JSONB DEFAULT '{}'::jsonb
);

-- Claims
CREATE TABLE IF NOT EXISTS hive_claims (
    id SERIAL PRIMARY KEY,
    mission_id TEXT REFERENCES hive_missions(id),
    claim TEXT NOT NULL,
    evidence TEXT,
    source_url TEXT,
    claim_type TEXT DEFAULT 'fact',
    confidence REAL DEFAULT 0.5,
    quality_score REAL DEFAULT 0.0,
    extracted_at TIMESTAMPTZ DEFAULT NOW(),
    verified BOOLEAN DEFAULT FALSE,
    is_garbage BOOLEAN DEFAULT FALSE,
    source_tier INTEGER DEFAULT 3
);

CREATE INDEX IF NOT EXISTS idx_claims_mission ON hive_claims(mission_id);
CREATE INDEX IF NOT EXISTS idx_claims_quality ON hive_claims(quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_claims_text_trgm ON hive_claims USING gin(claim gin_trgm_ops);

-- Events (for SSE streaming)
CREATE SEQUENCE IF NOT EXISTS hive_events_seq;
CREATE TABLE IF NOT EXISTS hive_events (
    id INTEGER PRIMARY KEY DEFAULT nextval('hive_events_seq'),
    mission_id TEXT,
    event_type TEXT NOT NULL,
    data JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_mission ON hive_events(mission_id, created_at);

-- Claim feedback
CREATE TABLE IF NOT EXISTS claim_feedback (
    id SERIAL PRIMARY KEY,
    claim_id INTEGER REFERENCES hive_claims(id),
    mission_id TEXT,
    operation TEXT,
    useful BOOLEAN,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
