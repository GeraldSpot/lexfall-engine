"""
LEXFALL API SERVER v2 — Railway-ready
"""

import os
import json
import logging
import sys
import traceback
from datetime import datetime
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

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

DATABASE_URL = os.getenv("DATABASE_URL")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 8000))

db_pool = None

logger.info("=" * 50)
logger.info("LEXFALL ENGINE STARTING")
logger.info("=" * 50)
logger.info(f"DATABASE_URL set: {bool(DATABASE_URL)}")
logger.info(f"ELEVENLABS_API_KEY set: {bool(ELEVENLABS_API_KEY)}")
logger.info(f"ELEVENLABS_AGENT_ID set: {bool(ELEVENLABS_AGENT_ID)}")
logger.info(f"GEMINI_API_KEY set: {bool(GEMINI_API_KEY)}")
logger.info(f"PORT: {PORT}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    try:
        logger.info("Connecting to database...")
        if not DATABASE_URL:
            logger.error("DATABASE_URL is not set!")
        else:
            db_url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
            db_pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10, timeout=30)
            logger.info("Database connected successfully!")
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        logger.error(traceback.format_exc())
        logger.info("Server starting without database.")
    yield
    if db_pool:
        await db_pool.close()
        logger.info("Database pool closed.")


app = FastAPI(title="Lexfall API", version="2.0", lifespan=lifespan)

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
        "database": "connected" if db_pool else "not connected",
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/")
async def root():
    return {"service": "Lexfall Engine", "version": "2.0", "status": "running"}


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
            api_key=ELEVENLABS_API_KEY,
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
