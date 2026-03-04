"""
Initialize the Lexfall database.
Run this once after setting up PostgreSQL on Railway.

Usage:
  python init_db.py                    # Uses DATABASE_URL from environment
  python init_db.py --seed             # Also inserts demo data for testing
"""

import asyncio
import os
import sys
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

SCHEMA = """
-- Organizations
CREATE TABLE IF NOT EXISTS organizations (
    org_id          VARCHAR(50) PRIMARY KEY,
    org_name        VARCHAR(255) NOT NULL,
    industry        VARCHAR(100),
    integration_type VARCHAR(20) NOT NULL DEFAULT 'csv',
    api_endpoint    TEXT,
    api_auth_type   VARCHAR(20),
    db_connection   TEXT,
    sftp_path       VARCHAR(255),
    sync_frequency  VARCHAR(20) DEFAULT 'daily',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    active          BOOLEAN DEFAULT TRUE
);

-- Employees
CREATE TABLE IF NOT EXISTS employees (
    employee_id     VARCHAR(100) NOT NULL,
    org_id          VARCHAR(50) NOT NULL REFERENCES organizations(org_id),
    external_id     VARCHAR(100),
    name            VARCHAR(255) NOT NULL,
    job_title       VARCHAR(255),
    department      VARCHAR(255),
    store_location  VARCHAR(255),
    hire_date       DATE,
    first_training  DATE,
    preferred_lang  VARCHAR(10) DEFAULT 'en',
    manager_name    VARCHAR(255),
    manager_notes   TEXT,
    overall_score   DECIMAL(5,2) DEFAULT 0,
    sessions_total  INTEGER DEFAULT 0,
    last_session    TIMESTAMP,
    active          BOOLEAN DEFAULT TRUE,
    synced_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (employee_id, org_id)
);

CREATE INDEX IF NOT EXISTS idx_employees_org ON employees(org_id);
CREATE INDEX IF NOT EXISTS idx_employees_dept ON employees(org_id, department);
CREATE INDEX IF NOT EXISTS idx_employees_external ON employees(org_id, external_id);

-- Training Modules
CREATE TABLE IF NOT EXISTS training_modules (
    module_id       VARCHAR(50) PRIMARY KEY,
    org_id          VARCHAR(50) REFERENCES organizations(org_id),
    module_name     VARCHAR(255) NOT NULL,
    description     TEXT,
    category        VARCHAR(100),
    difficulty      VARCHAR(20) DEFAULT 'medium',
    passing_score   INTEGER DEFAULT 75,
    estimated_mins  INTEGER DEFAULT 10,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Training Sessions
CREATE TABLE IF NOT EXISTS training_sessions (
    session_id      VARCHAR(50) PRIMARY KEY,
    employee_id     VARCHAR(100) NOT NULL,
    org_id          VARCHAR(50) NOT NULL,
    module_id       VARCHAR(50) REFERENCES training_modules(module_id),
    gemini_briefing TEXT,
    agent_prompt    TEXT,
    started_at      TIMESTAMP NOT NULL,
    ended_at        TIMESTAMP,
    duration_secs   INTEGER,
    status          VARCHAR(20) DEFAULT 'active',
    score           DECIMAL(5,2),
    passed          BOOLEAN,
    score_breakdown JSONB,
    strengths       TEXT[],
    improvements    TEXT[],
    agent_notes     TEXT,
    transcript      JSONB,
    FOREIGN KEY (employee_id, org_id) REFERENCES employees(employee_id, org_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_employee ON training_sessions(employee_id, org_id);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON training_sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_module ON training_sessions(module_id);

-- Employee Skills
CREATE TABLE IF NOT EXISTS employee_skills (
    employee_id     VARCHAR(100) NOT NULL,
    org_id          VARCHAR(50) NOT NULL,
    skill_name      VARCHAR(100) NOT NULL,
    current_score   DECIMAL(5,2) DEFAULT 0,
    trend           VARCHAR(10) DEFAULT 'stable',
    sessions_rated  INTEGER DEFAULT 0,
    last_updated    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (employee_id, org_id, skill_name),
    FOREIGN KEY (employee_id, org_id) REFERENCES employees(employee_id, org_id)
);

-- Field Mappings
CREATE TABLE IF NOT EXISTS field_mappings (
    org_id          VARCHAR(50) NOT NULL REFERENCES organizations(org_id),
    lexfall_field   VARCHAR(100) NOT NULL,
    client_field    VARCHAR(255) NOT NULL,
    transform       VARCHAR(50),
    transform_args  JSONB,
    PRIMARY KEY (org_id, lexfall_field)
);

-- Sync Log
CREATE TABLE IF NOT EXISTS sync_log (
    sync_id         SERIAL PRIMARY KEY,
    org_id          VARCHAR(50) NOT NULL REFERENCES organizations(org_id),
    sync_type       VARCHAR(20) NOT NULL,
    source          VARCHAR(20) NOT NULL,
    records_synced  INTEGER DEFAULT 0,
    records_failed  INTEGER DEFAULT 0,
    errors          JSONB,
    started_at      TIMESTAMP NOT NULL,
    completed_at    TIMESTAMP,
    status          VARCHAR(20) DEFAULT 'running'
);
"""

SEED_DATA = """
-- Demo organization
INSERT INTO organizations (org_id, org_name, industry, integration_type)
VALUES ('lexfall_demo', 'Lexfall Demo', 'Retail', 'csv')
ON CONFLICT (org_id) DO NOTHING;

-- Demo employees
INSERT INTO employees (employee_id, org_id, external_id, name, job_title, department, store_location, hire_date, overall_score, sessions_total, manager_notes)
VALUES 
    ('demo_sarah', 'lexfall_demo', 'S001', 'Sarah Johnson', 'Deli Associate', 'Deli/Bakery', 'Store 1234', '2024-03-15', 72, 3, 'Strong on technical skills but struggles with difficult customer interactions. Focus on empathy and staying calm.'),
    ('demo_marcus', 'lexfall_demo', 'M002', 'Marcus Williams', 'Produce Associate', 'Produce', 'Store 1234', '2023-11-01', 85, 7, 'Excellent customer skills. Push him on product knowledge and upselling.'),
    ('demo_ashley', 'lexfall_demo', 'A003', 'Ashley Chen', 'Cashier', 'Front End', 'Store 1234', '2024-06-20', 0, 0, 'Brand new hire. Start with onboarding basics.'),
    ('demo_james', 'lexfall_demo', 'J004', 'James Rodriguez', 'Meat Cutter', 'Meat', 'Store 1234', '2022-08-10', 91, 12, 'Veteran associate. Ready for leadership training scenarios.')
ON CONFLICT (employee_id, org_id) DO NOTHING;

-- Training modules
INSERT INTO training_modules (module_id, org_id, module_name, description, category, difficulty)
VALUES
    ('customer_de-escalation', 'lexfall_demo', 'Customer De-escalation', 'Practice calming upset customers and resolving complaints', 'customer_service', 'adaptive'),
    ('food_safety', 'lexfall_demo', 'Food Safety Fundamentals', 'Temperature control, cross-contamination, and handling procedures', 'safety', 'medium'),
    ('onboarding', 'lexfall_demo', 'New Employee Onboarding', 'Welcome session for brand new hires', 'onboarding', 'easy'),
    ('upselling', 'lexfall_demo', 'Upselling & Customer Engagement', 'Natural product suggestions and customer engagement', 'sales', 'medium'),
    ('compliance', 'lexfall_demo', 'Compliance & Policy Review', 'Break policies, dress code, incident reporting', 'compliance', 'medium'),
    ('general', NULL, 'General Skills Assessment', 'Well-rounded scenario testing multiple skills', 'general', 'adaptive')
ON CONFLICT (module_id) DO NOTHING;

-- Skill scores for existing employees
INSERT INTO employee_skills (employee_id, org_id, skill_name, current_score, trend, sessions_rated)
VALUES
    ('demo_sarah', 'lexfall_demo', 'communication', 75, 'improving', 3),
    ('demo_sarah', 'lexfall_demo', 'empathy', 62, 'stable', 3),
    ('demo_sarah', 'lexfall_demo', 'resolution', 70, 'improving', 3),
    ('demo_sarah', 'lexfall_demo', 'professionalism', 82, 'stable', 3),
    ('demo_sarah', 'lexfall_demo', 'knowledge', 78, 'improving', 3),
    ('demo_marcus', 'lexfall_demo', 'communication', 88, 'stable', 7),
    ('demo_marcus', 'lexfall_demo', 'empathy', 90, 'improving', 7),
    ('demo_marcus', 'lexfall_demo', 'resolution', 85, 'stable', 7),
    ('demo_marcus', 'lexfall_demo', 'professionalism', 87, 'stable', 7),
    ('demo_marcus', 'lexfall_demo', 'knowledge', 76, 'declining', 7),
    ('demo_james', 'lexfall_demo', 'communication', 92, 'stable', 12),
    ('demo_james', 'lexfall_demo', 'empathy', 88, 'improving', 12),
    ('demo_james', 'lexfall_demo', 'resolution', 94, 'stable', 12),
    ('demo_james', 'lexfall_demo', 'professionalism', 93, 'stable', 12),
    ('demo_james', 'lexfall_demo', 'knowledge', 90, 'stable', 12)
ON CONFLICT (employee_id, org_id, skill_name) DO NOTHING;
"""


async def main():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set.")
        print("Set it in .env or as an environment variable.")
        sys.exit(1)

    print(f"Connecting to database...")
    conn = await asyncpg.connect(DATABASE_URL)

    try:
        print("Creating tables...")
        await conn.execute(SCHEMA)
        print("Schema created successfully.")

        if "--seed" in sys.argv:
            print("Inserting demo data...")
            await conn.execute(SEED_DATA)
            print("Demo data inserted.")
            print()
            print("Demo employees available:")
            print("  demo_sarah   — Deli Associate, score 72, needs de-escalation help")
            print("  demo_marcus  — Produce Associate, score 85, push on knowledge")
            print("  demo_ashley  — Cashier, score 0, brand new hire")
            print("  demo_james   — Meat Cutter, score 91, ready for advanced")
            print()
            print("Use org_id: lexfall_demo")

        print()
        print("Database ready.")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
