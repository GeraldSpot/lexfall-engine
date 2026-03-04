"""
LEXFALL API SERVER
==================
FastAPI backend that the Lexfall website talks to.

Endpoints:
  POST /api/session/start    — Pre-flight + start training
  POST /api/session/end      — Score + save results  
  GET  /api/employee/{id}    — Get employee profile + history
  GET  /api/dashboard/{org}  — Manager dashboard data
  POST /api/webhook/elevenlabs — ElevenLabs session-end callback
  POST /api/sync/{org}       — Trigger a data sync for an org
"""

import os
import json
import logging
from datetime import datetime
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from preflight import PreFlightEngine
from scoring import ScoringEngine

logger = logging.getLogger("lexfall.api")

# ── Database ──
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/lexfall")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

db_pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=20)
    logger.info("Database pool created")
    yield
    await db_pool.close()
    logger.info("Database pool closed")


app = FastAPI(title="Lexfall API", version="2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://lexfall.com", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request/Response Models ──

class StartSessionRequest(BaseModel):
    org_id: str
    employee_id: str
    scenario_type: str = "general"


class StartSessionResponse(BaseModel):
    session_id: str
    signed_url: str
    employee_name: str
    scenario: str
    status: str


class EndSessionRequest(BaseModel):
    session_id: str
    conversation_id: str      # From ElevenLabs
    employee_id: str
    org_id: str
    scenario_type: str
    duration_secs: int = 0


# ══════════════════════════════════════════
# POST /api/session/start
# ══════════════════════════════════════════
# This is the big one. Called when employee clicks "Start Training."
# Runs the entire pre-flight sequence.

@app.post("/api/session/start", response_model=StartSessionResponse)
async def start_session(req: StartSessionRequest):
    """
    Pre-flight launch sequence:
      1. Pull employee data from DB
      2. Send to Gemini for personalized briefing
      3. Inject briefing into ElevenLabs agent
      4. Return signed URL for frontend to connect
    """
    try:
        engine = PreFlightEngine(db_pool)
        result = await engine.launch(
            org_id=req.org_id,
            employee_id=req.employee_id,
            scenario_type=req.scenario_type,
        )
        return StartSessionResponse(**result)

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Pre-flight failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to start training session")


# ══════════════════════════════════════════
# POST /api/session/end
# ══════════════════════════════════════════
# Called when the conversation ends.
# Scores the session and saves results.

@app.post("/api/session/end")
async def end_session(req: EndSessionRequest):
    """
    Post-session pipeline:
      1. Get transcript from ElevenLabs
      2. Gemini scores the employee
      3. Update skill scores and employee profile
      4. Save everything to DB
    """
    try:
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
            "next_focus": evaluation.get("next_focus", ""),
        }

    except Exception as e:
        logger.error(f"Post-session processing failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to process session results")


# ══════════════════════════════════════════
# POST /api/webhook/elevenlabs
# ══════════════════════════════════════════
# ElevenLabs calls this when a conversation ends.
# Alternative to the frontend calling /session/end.

@app.post("/api/webhook/elevenlabs")
async def elevenlabs_webhook(request: Request):
    """
    Webhook handler for ElevenLabs conversation events.
    Automatically triggers scoring when a session ends.
    """
    body = await request.json()
    event_type = body.get("type", "")

    if event_type == "conversation.ended":
        conversation_id = body.get("conversation_id")
        # Look up the session by conversation_id
        async with db_pool.acquire() as conn:
            session = await conn.fetchrow("""
                SELECT session_id, employee_id, org_id, module_id
                FROM training_sessions
                WHERE status = 'active'
                ORDER BY started_at DESC
                LIMIT 1
            """)

        if session:
            # Trigger scoring in background
            # In production, use a task queue (Celery, etc.)
            engine = PreFlightEngine(db_pool)
            context = await engine.get_employee_context(
                session["org_id"], session["employee_id"]
            )
            scorer = ScoringEngine(db_pool)
            await scorer.process_completed_session(
                session_id=session["session_id"],
                conversation_id=conversation_id,
                employee_id=session["employee_id"],
                org_id=session["org_id"],
                employee_context=context,
                scenario_type=session["module_id"] or "general",
                api_key=ELEVENLABS_API_KEY,
            )

    return {"status": "ok"}


# ══════════════════════════════════════════
# GET /api/employee/{org_id}/{employee_id}
# ══════════════════════════════════════════
# Employee profile with full training history.

@app.get("/api/employee/{org_id}/{employee_id}")
async def get_employee(org_id: str, employee_id: str):
    """Get employee profile, skills, and session history."""
    async with db_pool.acquire() as conn:
        employee = await conn.fetchrow("""
            SELECT * FROM employees
            WHERE employee_id = $1 AND org_id = $2
        """, employee_id, org_id)

        if not employee:
            raise HTTPException(status_code=404, detail="Employee not found")

        skills = await conn.fetch("""
            SELECT skill_name, current_score, trend, sessions_rated
            FROM employee_skills
            WHERE employee_id = $1 AND org_id = $2
            ORDER BY current_score ASC
        """, employee_id, org_id)

        sessions = await conn.fetch("""
            SELECT session_id, module_id, score, passed,
                   score_breakdown, strengths, improvements,
                   agent_notes, started_at, duration_secs
            FROM training_sessions
            WHERE employee_id = $1 AND org_id = $2
              AND status = 'completed'
            ORDER BY started_at DESC
            LIMIT 20
        """, employee_id, org_id)

    return {
        "employee": dict(employee),
        "skills": [dict(s) for s in skills],
        "sessions": [dict(s) for s in sessions],
    }


# ══════════════════════════════════════════
# GET /api/dashboard/{org_id}
# ══════════════════════════════════════════
# Manager dashboard: org-wide stats.

@app.get("/api/dashboard/{org_id}")
async def get_dashboard(org_id: str, department: str = None):
    """Org-wide training analytics for managers."""
    async with db_pool.acquire() as conn:
        # Overall stats
        stats = await conn.fetchrow("""
            SELECT 
                COUNT(*) as total_employees,
                AVG(overall_score) as avg_score,
                SUM(sessions_total) as total_sessions
            FROM employees
            WHERE org_id = $1 AND active = TRUE
        """, org_id)

        # Department breakdown
        dept_stats = await conn.fetch("""
            SELECT 
                department,
                COUNT(*) as employee_count,
                AVG(overall_score) as avg_score,
                SUM(sessions_total) as total_sessions
            FROM employees
            WHERE org_id = $1 AND active = TRUE
            GROUP BY department
            ORDER BY avg_score ASC
        """, org_id)

        # Org-wide weakest skills
        weak_skills = await conn.fetch("""
            SELECT 
                skill_name,
                AVG(current_score) as avg_score,
                COUNT(*) as employees_rated
            FROM employee_skills
            WHERE org_id = $1
            GROUP BY skill_name
            ORDER BY avg_score ASC
            LIMIT 5
        """, org_id)

        # Recent sessions
        recent = await conn.fetch("""
            SELECT 
                ts.session_id, ts.employee_id, e.name, e.department,
                ts.module_id, ts.score, ts.passed, ts.started_at
            FROM training_sessions ts
            JOIN employees e ON ts.employee_id = e.employee_id AND ts.org_id = e.org_id
            WHERE ts.org_id = $1 AND ts.status = 'completed'
            ORDER BY ts.started_at DESC
            LIMIT 25
        """, org_id)

        # Employees needing attention (low scores, declining trends)
        attention = await conn.fetch("""
            SELECT 
                e.employee_id, e.name, e.department, e.job_title,
                e.overall_score, e.sessions_total, e.last_session
            FROM employees e
            WHERE e.org_id = $1 AND e.active = TRUE
              AND (e.overall_score < 65 OR e.sessions_total = 0)
            ORDER BY e.overall_score ASC
            LIMIT 10
        """, org_id)

    return {
        "overview": dict(stats),
        "departments": [dict(d) for d in dept_stats],
        "weakest_skills": [dict(s) for s in weak_skills],
        "recent_sessions": [dict(r) for r in recent],
        "needs_attention": [dict(a) for a in attention],
    }


# ══════════════════════════════════════════
# Available training scenarios
# ══════════════════════════════════════════

@app.get("/api/scenarios/{org_id}")
async def get_scenarios(org_id: str):
    """List available training scenarios for this org."""
    async with db_pool.acquire() as conn:
        modules = await conn.fetch("""
            SELECT module_id, module_name, description, category,
                   difficulty, passing_score, estimated_mins
            FROM training_modules
            WHERE (org_id = $1 OR org_id IS NULL) AND active = TRUE
            ORDER BY category, module_name
        """, org_id)

    return {"scenarios": [dict(m) for m in modules]}


# ══════════════════════════════════════════
# Health check
# ══════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
