-- ============================================================
-- LEXFALL DATABASE SCHEMA
-- ============================================================
-- This is the Lexfall-side database. Retailers don't need to
-- change THEIR database. They just need to map their fields
-- to ours via the adapter layer.
--
-- Supports: PostgreSQL (primary), MySQL, SQLite for dev
-- ============================================================

-- ── ORGANIZATIONS (the retailer/client) ──
CREATE TABLE IF NOT EXISTS organizations (
    org_id          VARCHAR(50) PRIMARY KEY,
    org_name        VARCHAR(255) NOT NULL,           -- "Walmart", "Target", etc.
    industry        VARCHAR(100),                     -- "Retail", "Grocery", "QSR"
    integration_type VARCHAR(20) NOT NULL DEFAULT 'csv',  -- 'rest_api', 'direct_db', 'csv'
    api_endpoint    TEXT,                              -- For REST API clients
    api_auth_type   VARCHAR(20),                      -- 'oauth2', 'api_key', 'bearer'
    db_connection   TEXT,                              -- Encrypted connection string for direct DB
    sftp_path       VARCHAR(255),                     -- For CSV clients
    sync_frequency  VARCHAR(20) DEFAULT 'daily',      -- 'realtime', 'hourly', 'daily'
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    active          BOOLEAN DEFAULT TRUE
);

-- ── EMPLOYEES (synced from retailer's system) ──
CREATE TABLE IF NOT EXISTS employees (
    employee_id     VARCHAR(100) NOT NULL,
    org_id          VARCHAR(50) NOT NULL REFERENCES organizations(org_id),
    external_id     VARCHAR(100),                     -- Their ID in the retailer's system
    name            VARCHAR(255) NOT NULL,
    job_title       VARCHAR(255),
    department      VARCHAR(255),
    store_location  VARCHAR(255),                     -- Store #, location, region
    hire_date       DATE,
    first_training  DATE,                             -- First Lexfall session
    preferred_lang  VARCHAR(10) DEFAULT 'en',
    manager_name    VARCHAR(255),
    manager_notes   TEXT,                              -- Free text from their manager
    overall_score   DECIMAL(5,2) DEFAULT 0,           -- Running avg 0-100
    sessions_total  INTEGER DEFAULT 0,
    last_session    TIMESTAMP,
    active          BOOLEAN DEFAULT TRUE,
    synced_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (employee_id, org_id)
);

CREATE INDEX idx_employees_org ON employees(org_id);
CREATE INDEX idx_employees_dept ON employees(org_id, department);
CREATE INDEX idx_employees_external ON employees(org_id, external_id);

-- ── TRAINING MODULES (what scenarios are available) ──
CREATE TABLE IF NOT EXISTS training_modules (
    module_id       VARCHAR(50) PRIMARY KEY,
    org_id          VARCHAR(50) REFERENCES organizations(org_id),
    module_name     VARCHAR(255) NOT NULL,
    description     TEXT,
    category        VARCHAR(100),                     -- "customer_service", "safety", "compliance"
    difficulty      VARCHAR(20) DEFAULT 'medium',     -- "easy", "medium", "hard", "adaptive"
    passing_score   INTEGER DEFAULT 75,
    estimated_mins  INTEGER DEFAULT 10,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── TRAINING SESSIONS (every conversation) ──
CREATE TABLE IF NOT EXISTS training_sessions (
    session_id      VARCHAR(50) PRIMARY KEY,
    employee_id     VARCHAR(100) NOT NULL,
    org_id          VARCHAR(50) NOT NULL,
    module_id       VARCHAR(50) REFERENCES training_modules(module_id),
    
    -- Pre-flight data
    gemini_briefing TEXT,                              -- The full briefing Gemini generated
    agent_prompt    TEXT,                              -- What was injected into ElevenLabs
    
    -- Session data
    started_at      TIMESTAMP NOT NULL,
    ended_at        TIMESTAMP,
    duration_secs   INTEGER,
    status          VARCHAR(20) DEFAULT 'active',     -- 'active', 'completed', 'abandoned', 'error'
    
    -- Scoring
    score           DECIMAL(5,2),                     -- 0-100
    passed          BOOLEAN,
    
    -- Detailed scores (JSON for flexibility)
    score_breakdown JSONB,
    -- Example: {
    --   "communication": 82,
    --   "empathy": 65,
    --   "resolution": 78,
    --   "professionalism": 90,
    --   "response_time": 85
    -- }
    
    -- AI assessment
    strengths       TEXT[],                            -- What they did well
    improvements    TEXT[],                            -- What to work on
    agent_notes     TEXT,                              -- AI-generated session summary
    
    -- Transcript
    transcript      JSONB,                            -- Full conversation as array of turns
    -- Example: [
    --   {"role": "agent", "text": "Hey Sarah!", "timestamp": "00:00"},
    --   {"role": "employee", "text": "Hi!", "timestamp": "00:02"},
    --   ...
    -- ]
    
    FOREIGN KEY (employee_id, org_id) REFERENCES employees(employee_id, org_id)
);

CREATE INDEX idx_sessions_employee ON training_sessions(employee_id, org_id);
CREATE INDEX idx_sessions_date ON training_sessions(started_at DESC);
CREATE INDEX idx_sessions_module ON training_sessions(module_id);

-- ── EMPLOYEE SKILLS TRACKING (running skill scores) ──
CREATE TABLE IF NOT EXISTS employee_skills (
    employee_id     VARCHAR(100) NOT NULL,
    org_id          VARCHAR(50) NOT NULL,
    skill_name      VARCHAR(100) NOT NULL,            -- "de-escalation", "empathy", "product_knowledge"
    current_score   DECIMAL(5,2) DEFAULT 0,
    trend           VARCHAR(10) DEFAULT 'stable',     -- 'improving', 'declining', 'stable'
    sessions_rated  INTEGER DEFAULT 0,
    last_updated    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (employee_id, org_id, skill_name),
    FOREIGN KEY (employee_id, org_id) REFERENCES employees(employee_id, org_id)
);

-- ── FIELD MAPPINGS (how each client's fields map to ours) ──
-- This is the key to making onboarding easy.
-- Retailer says "our employee name field is called 'associate_full_name'"
-- We store that mapping here so the adapter knows how to translate.
CREATE TABLE IF NOT EXISTS field_mappings (
    org_id          VARCHAR(50) NOT NULL REFERENCES organizations(org_id),
    lexfall_field   VARCHAR(100) NOT NULL,             -- Our field name
    client_field    VARCHAR(255) NOT NULL,             -- Their field name
    transform       VARCHAR(50),                       -- 'none', 'uppercase', 'date_format', 'concat'
    transform_args  JSONB,                             -- Args for transform if needed
    PRIMARY KEY (org_id, lexfall_field)
);

-- Example mappings for Walmart:
-- INSERT INTO field_mappings VALUES 
--   ('walmart', 'employee_id', 'WIN', 'none', NULL),
--   ('walmart', 'name', 'associate_name', 'none', NULL),
--   ('walmart', 'job_title', 'job_code_desc', 'none', NULL),
--   ('walmart', 'department', 'dept_name', 'none', NULL),
--   ('walmart', 'hire_date', 'original_hire_dt', 'date_format', '{"from": "MM/DD/YYYY"}'),
--   ('walmart', 'store_location', 'facility_nbr', 'none', NULL);

-- ── SYNC LOG (track data imports) ──
CREATE TABLE IF NOT EXISTS sync_log (
    sync_id         SERIAL PRIMARY KEY,
    org_id          VARCHAR(50) NOT NULL REFERENCES organizations(org_id),
    sync_type       VARCHAR(20) NOT NULL,              -- 'full', 'incremental', 'manual'
    source          VARCHAR(20) NOT NULL,              -- 'api', 'db', 'csv'
    records_synced  INTEGER DEFAULT 0,
    records_failed  INTEGER DEFAULT 0,
    errors          JSONB,
    started_at      TIMESTAMP NOT NULL,
    completed_at    TIMESTAMP,
    status          VARCHAR(20) DEFAULT 'running'      -- 'running', 'completed', 'failed'
);
