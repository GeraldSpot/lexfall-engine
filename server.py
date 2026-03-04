"""
LEXFALL API SERVER v3 — Railway-ready
"""

import os
import json
import logging
import sys
import traceback
from datetime import datetime
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("lexfall")

db_pool = None
CONFIG = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, CONFIG

    CONFIG["DATABASE_URL"] = os.environ.get("DATABASE_URL", "")
    CONFIG["ELEVENLABS_API_KEY"] = os.environ.get("ELEVENLABS_API_KEY", "")
    CONFIG["ELEVENLABS_AGENT_ID"] = os.environ.get("ELEVENLABS_AGENT_ID", "")
    CONFIG["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "")

    logger.info("=" * 50)
    logger.info("LEXFALL ENGINE STARTING v3")
    logger.info("=" * 50)
    logger.info(f"DATABASE_URL set: {bool(CONFIG['DATABASE_URL'])}")
    logger.info(f"DATABASE_URL starts with: {CONFIG['DATABASE_URL'][:20]}..." if CONFIG["DATABASE_URL"] else "DATABASE_URL is empty")
    logger.info(f"ELEVENLABS_API_KEY set: {bool(CONFIG['ELEVENLABS_API_KEY'])}")
    logger.info(f"ELEVENLABS_AGENT_ID set: {bool(CONFIG['ELEVENLABS_AGENT_ID'])}")
    logger.info(f"GEMINI_API_KEY set: {bool(CONFIG['GEMINI_API_KEY'])}")

    try:
        if not CONFIG["DATABASE_URL"]:
            logger.error("DATABASE_URL is not set!")
        else:
            db_url = CONFIG["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
            logger.info(f"Connecting to: {db_url[:40]}...")
            db_pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10, timeout=30)
            logger.info("Database connected successfully!")
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        logger.error(traceback.format_exc())

    yield

    if db_pool:
        await db_pool.close()


app = FastAPI(title="Lexfall API", version="3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartSessionRequest(BaseModel):
    org_id: str
    employee_id: str
    scenario_type: str = "general"


class EndSessionRequest(BaseModel):
    session_id: str
    conversation_id: str
    employee_id: str
    org_id: str
    scenario_type: str
    duration_secs: int = 0


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "3.0",
        "database": "connected" if db_pool else "not connected",
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/")
async def root():
    return {"service": "Lexfall Engine", "version": "3.0", "status": "running"}


@app.get("/debug/env")
async def debug_env():
    return {
        "DATABASE_URL_set": bool(os.environ.get("DATABASE_URL")),
        "ELEVENLABS_API_KEY_set": bool(os.environ.get("ELEVENLABS_API_KEY")),
        "GEMINI_API_KEY_set": bool(os.environ.get("GEMINI_API_KEY")),
        "env_keys_with_DB": [k for k in os.environ.keys() if "DATABASE" in k.upper() or "PG" in k.upper() or "POSTGRES" in k.upper()],
        "total_env_vars": len(os.environ),
    }


@app.get("/api/init-db")
async def init_database():
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database not connected")
    try:
        async with db_pool.acquire() as conn:
            # Create tables
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS organizations (
                    org_id VARCHAR(50) PRIMARY KEY,
                    org_name VARCHAR(255) NOT NULL,
                    industry VARCHAR(100),
                    integration_type VARCHAR(20) NOT NULL DEFAULT 'csv',
                    api_endpoint TEXT,
                    api_auth_type VARCHAR(20),
                    db_connection TEXT,
                    sftp_path VARCHAR(255),
                    sync_frequency VARCHAR(20) DEFAULT 'daily',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    active BOOLEAN DEFAULT TRUE
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS employees (
                    employee_id VARCHAR(100) NOT NULL,
                    org_id VARCHAR(50) NOT NULL REFERENCES organizations(org_id),
                    external_id VARCHAR(100),
                    name VARCHAR(255) NOT NULL,
                    job_title VARCHAR(255),
                    department VARCHAR(255),
                    store_location VARCHAR(255),
                    hire_date DATE,
                    first_training DATE,
                    preferred_lang VARCHAR(10) DEFAULT 'en',
                    manager_name VARCHAR(255),
                    manager_notes TEXT,
                    overall_score DECIMAL(5,2) DEFAULT 0,
                    sessions_total INTEGER DEFAULT 0,
                    last_session TIMESTAMP,
                    active BOOLEAN DEFAULT TRUE,
                    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (employee_id, org_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS training_modules (
                    module_id VARCHAR(50) PRIMARY KEY,
                    org_id VARCHAR(50) REFERENCES organizations(org_id),
                    module_name VARCHAR(255) NOT NULL,
                    description TEXT,
                    category VARCHAR(100),
                    difficulty VARCHAR(20) DEFAULT 'medium',
                    passing_score INTEGER DEFAULT 75,
                    estimated_mins INTEGER DEFAULT 10,
                    active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS training_sessions (
                    session_id VARCHAR(50) PRIMARY KEY,
                    employee_id VARCHAR(100) NOT NULL,
                    org_id VARCHAR(50) NOT NULL,
                    module_id VARCHAR(50),
                    gemini_briefing TEXT,
                    agent_prompt TEXT,
                    started_at TIMESTAMP NOT NULL,
                    ended_at TIMESTAMP,
                    duration_secs INTEGER,
                    status VARCHAR(20) DEFAULT 'active',
                    score DECIMAL(5,2),
                    passed BOOLEAN,
                    score_breakdown JSONB,
                    strengths TEXT[],
                    improvements TEXT[],
                    agent_notes TEXT,
                    transcript JSONB,
                    FOREIGN KEY (employee_id, org_id) REFERENCES employees(employee_id, org_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS employee_skills (
                    employee_id VARCHAR(100) NOT NULL,
                    org_id VARCHAR(50) NOT NULL,
                    skill_name VARCHAR(100) NOT NULL,
                    current_score DECIMAL(5,2) DEFAULT 0,
                    trend VARCHAR(10) DEFAULT 'stable',
                    sessions_rated INTEGER DEFAULT 0,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (employee_id, org_id, skill_name),
                    FOREIGN KEY (employee_id, org_id) REFERENCES employees(employee_id, org_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS field_mappings (
                    org_id VARCHAR(50) NOT NULL REFERENCES organizations(org_id),
                    lexfall_field VARCHAR(100) NOT NULL,
                    client_field VARCHAR(255) NOT NULL,
                    transform VARCHAR(50),
                    transform_args JSONB,
                    PRIMARY KEY (org_id, lexfall_field)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_log (
                    sync_id SERIAL PRIMARY KEY,
                    org_id VARCHAR(50) NOT NULL REFERENCES organizations(org_id),
                    sync_type VARCHAR(20) NOT NULL,
                    source VARCHAR(20) NOT NULL,
                    records_synced INTEGER DEFAULT 0,
                    records_failed INTEGER DEFAULT 0,
                    errors JSONB,
                    started_at TIMESTAMP NOT NULL,
                    completed_at TIMESTAMP,
                    status VARCHAR(20) DEFAULT 'running'
                )
            """)
            logger.info("Tables created.")

            # Seed demo data
            await conn.execute("""
                INSERT INTO organizations (org_id, org_name, industry, integration_type)
                VALUES ('lexfall_demo', 'Lexfall Demo', 'Retail', 'csv')
                ON CONFLICT (org_id) DO NOTHING
            """)
            await conn.execute("""
                INSERT INTO employees (employee_id, org_id, external_id, name, job_title, department, store_location, hire_date, overall_score, sessions_total, manager_notes)
                VALUES
                    ('demo_sarah', 'lexfall_demo', 'S001', 'Sarah Johnson', 'Deli Associate', 'Deli/Bakery', 'Store 1234', '2024-03-15', 72, 3, 'Strong on technical skills but struggles with difficult customer interactions. Focus on empathy and staying calm.'),
                    ('demo_marcus', 'lexfall_demo', 'M002', 'Marcus Williams', 'Produce Associate', 'Produce', 'Store 1234', '2023-11-01', 85, 7, 'Excellent customer skills. Push him on product knowledge and upselling.'),
                    ('demo_ashley', 'lexfall_demo', 'A003', 'Ashley Chen', 'Cashier', 'Front End', 'Store 1234', '2024-06-20', 0, 0, 'Brand new hire. Start with onboarding basics.'),
                    ('demo_james', 'lexfall_demo', 'J004', 'James Rodriguez', 'Meat Cutter', 'Meat', 'Store 1234', '2022-08-10', 91, 12, 'Veteran associate. Ready for leadership training scenarios.')
                ON CONFLICT (employee_id, org_id) DO NOTHING
            """)
            await conn.execute("""
                INSERT INTO training_modules (module_id, org_id, module_name, description, category, difficulty)
                VALUES
                    ('customer_de-escalation', 'lexfall_demo', 'Customer De-escalation', 'Practice calming upset customers and resolving complaints', 'customer_service', 'adaptive'),
                    ('food_safety', 'lexfall_demo', 'Food Safety Fundamentals', 'Temperature control, cross-contamination, and handling procedures', 'safety', 'medium'),
                    ('onboarding', 'lexfall_demo', 'New Employee Onboarding', 'Welcome session for brand new hires', 'onboarding', 'easy'),
                    ('upselling', 'lexfall_demo', 'Upselling and Customer Engagement', 'Natural product suggestions and customer engagement', 'sales', 'medium'),
                    ('compliance', 'lexfall_demo', 'Compliance and Policy Review', 'Break policies, dress code, incident reporting', 'compliance', 'medium')
                ON CONFLICT (module_id) DO NOTHING
            """)
            await conn.execute("""
                INSERT INTO training_modules (module_id, module_name, description, category, difficulty)
                VALUES ('general', 'General Skills Assessment', 'Well-rounded scenario testing multiple skills', 'general', 'adaptive')
                ON CONFLICT (module_id) DO NOTHING
            """)
            await conn.execute("""
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
                ON CONFLICT (employee_id, org_id, skill_name) DO NOTHING
            """)
            logger.info("Demo data seeded.")

        return {
            "status": "ok",
            "message": "Database initialized with demo data",
            "employees": ["demo_sarah", "demo_marcus", "demo_ashley", "demo_james"],
        }
    except Exception as e:
        logger.error(f"Init failed: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/session/start")
async def start_session(req: StartSessionRequest):
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database not connected")
    try:
        from preflight import PreFlightEngine
        engine = PreFlightEngine(db_pool)
        result = await engine.launch(
            org_id=req.org_id,
            employee_id=req.employee_id,
            scenario_type=req.scenario_type,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Pre-flight failed: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to start training session")


@app.post("/api/session/end")
async def end_session(req: EndSessionRequest):
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database not connected")
    try:
        from preflight import PreFlightEngine
        from scoring import ScoringEngine
        engine = PreFlightEngine(db_pool)
        context = await engine.get_employee_context(req.org_id, req.employee_id)
        scorer = ScoringEngine(db_pool)
        evaluation = await scorer.process_completed_session(
            session_id=req.session_id,
            conversation_id=req.conversation_id,
            employee_id=req.employee_id,
            org_id=req.org_id,
            employee_context=context,
            scenario_type=req.scenario_type,
            api_key=CONFIG.get("ELEVENLABS_API_KEY", ""),
            duration_secs=req.duration_secs,
        )
        return {
            "session_id": req.session_id,
            "score": evaluation["overall_score"],
            "passed": evaluation["passed"],
            "scores": evaluation["scores"],
            "strengths": evaluation.get("strengths", []),
            "improvements": evaluation.get("improvements", []),
            "summary": evaluation.get("summary", ""),
        }
    except Exception as e:
        logger.error(f"Scoring failed: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to process session")


@app.post("/api/webhook/elevenlabs")
async def elevenlabs_webhook(request: Request):
    body = await request.json()
    logger.info(f"Webhook received: {body.get('type', 'unknown')}")
    return {"status": "ok"}


@app.get("/api/employee/{org_id}/{employee_id}")
async def get_employee(org_id: str, employee_id: str):
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database not connected")
    async with db_pool.acquire() as conn:
        employee = await conn.fetchrow(
            "SELECT * FROM employees WHERE employee_id = $1 AND org_id = $2",
            employee_id, org_id
        )
        if not employee:
            raise HTTPException(status_code=404, detail="Employee not found")
        skills = await conn.fetch(
            "SELECT skill_name, current_score, trend FROM employee_skills WHERE employee_id = $1 AND org_id = $2 ORDER BY current_score ASC",
            employee_id, org_id
        )
        sessions = await conn.fetch(
            "SELECT session_id, module_id, score, passed, started_at FROM training_sessions WHERE employee_id = $1 AND org_id = $2 AND status = 'completed' ORDER BY started_at DESC LIMIT 20",
            employee_id, org_id
        )
    return {
        "employee": dict(employee),
        "skills": [dict(s) for s in skills],
        "sessions": [dict(s) for s in sessions],
    }


@app.get("/api/dashboard/{org_id}")
async def get_dashboard(org_id: str):
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database not connected")
    async with db_pool.acquire() as conn:
        stats = await conn.fetchrow(
            "SELECT COUNT(*) as total_employees, AVG(overall_score) as avg_score, SUM(sessions_total) as total_sessions FROM employees WHERE org_id = $1 AND active = TRUE",
            org_id
        )
    return {"overview": dict(stats)}


@app.get("/api/scenarios/{org_id}")
async def get_scenarios(org_id: str):
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database not connected")
    async with db_pool.acquire() as conn:
        modules = await conn.fetch(
            "SELECT module_id, module_name, description, category, difficulty, passing_score, estimated_mins FROM training_modules WHERE (org_id = $1 OR org_id IS NULL) AND active = TRUE ORDER BY category, module_name",
            org_id
        )
    return {"scenarios": [dict(m) for m in modules]}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
