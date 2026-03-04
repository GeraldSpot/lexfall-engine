"""
LEXFALL PRE-FLIGHT ENGINE
=========================
The core pipeline that runs BEFORE every training session:
  DB → Gemini Briefing → ElevenLabs Agent → Ready to talk

This is the heart of Lexfall.
"""

import os
import json
import logging
from datetime import datetime
from typing import Optional

import google.generativeai as genai
import httpx

logger = logging.getLogger("lexfall.preflight")

# ── Config ──
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID")

genai.configure(api_key=GEMINI_API_KEY)


class PreFlightEngine:
    """
    Orchestrates the entire pre-flight sequence for a training session.
    
    Usage:
        engine = PreFlightEngine(db_pool)
        session = await engine.launch("walmart", "EMP-12345", "de_escalation")
        # session.signed_url → give to frontend to start ElevenLabs widget
    """

    def __init__(self, db_pool):
        """db_pool: asyncpg connection pool to Lexfall's database."""
        self.db = db_pool
        self.gemini = genai.GenerativeModel("gemini-1.5-flash")

    # ──────────────────────────────────────────
    # STEP 1: Pull employee data + history
    # ──────────────────────────────────────────
    async def get_employee_context(self, org_id: str, employee_id: str) -> dict:
        """
        Pull everything we know about this employee.
        This is all from LEXFALL'S database (already synced from retailer).
        """
        async with self.db.acquire() as conn:
            # Employee profile
            employee = await conn.fetchrow("""
                SELECT * FROM employees 
                WHERE employee_id = $1 AND org_id = $2
            """, employee_id, org_id)

            if not employee:
                raise ValueError(f"Employee {employee_id} not found for org {org_id}")

            # Last 10 training sessions
            sessions = await conn.fetch("""
                SELECT module_id, score, passed, score_breakdown,
                       strengths, improvements, started_at
                FROM training_sessions
                WHERE employee_id = $1 AND org_id = $2
                  AND status = 'completed'
                ORDER BY started_at DESC
                LIMIT 10
            """, employee_id, org_id)

            # Skill scores
            skills = await conn.fetch("""
                SELECT skill_name, current_score, trend, sessions_rated
                FROM employee_skills
                WHERE employee_id = $1 AND org_id = $2
                ORDER BY current_score ASC
            """, employee_id, org_id)

            # Build context object
            context = {
                "employee": {
                    "name": employee["name"],
                    "job_title": employee["job_title"],
                    "department": employee["department"],
                    "store_location": employee["store_location"],
                    "hire_date": str(employee["hire_date"]) if employee["hire_date"] else None,
                    "overall_score": float(employee["overall_score"]),
                    "sessions_completed": employee["sessions_total"],
                    "manager_notes": employee["manager_notes"] or "",
                },
                "recent_sessions": [
                    {
                        "module": s["module_id"],
                        "score": float(s["score"]) if s["score"] else None,
                        "passed": s["passed"],
                        "strengths": s["strengths"] or [],
                        "improvements": s["improvements"] or [],
                        "date": str(s["started_at"].date()),
                    }
                    for s in sessions
                ],
                "skills": [
                    {
                        "skill": s["skill_name"],
                        "score": float(s["current_score"]),
                        "trend": s["trend"],
                        "times_rated": s["sessions_rated"],
                    }
                    for s in skills
                ],
                "weakest_skills": [
                    s["skill_name"] for s in skills[:3]  # Bottom 3
                ],
                "strongest_skills": [
                    s["skill_name"] for s in sorted(skills, key=lambda x: x["current_score"], reverse=True)[:3]
                ],
            }

            logger.info(
                f"[PREFLIGHT] Context loaded for {employee['name']} "
                f"({employee['job_title']}) — {employee['sessions_total']} sessions, "
                f"avg score: {employee['overall_score']}"
            )

            return context

    # ──────────────────────────────────────────
    # STEP 2: Gemini builds the briefing
    # ──────────────────────────────────────────
    async def build_briefing(self, context: dict, scenario_type: str) -> str:
        """
        Send employee context to Gemini. Get back a structured briefing
        that becomes the ElevenLabs agent's instruction prompt.
        
        Gemini is the strategist. It reads the employee's file and writes
        the playbook. It never talks to the employee.
        """

        prompt = f"""You are the training intelligence engine for Lexfall, an AI-powered 
enterprise training platform for frontline workers.

YOUR ROLE: Analyze this employee's data and write an instruction prompt for 
a voice AI agent that is about to train them. You are NOT talking to the employee.
You are briefing the AI trainer.

═══ EMPLOYEE DATA ═══
{json.dumps(context["employee"], indent=2)}

═══ RECENT TRAINING SESSIONS ═══
{json.dumps(context["recent_sessions"], indent=2)}

═══ SKILL SCORES ═══
{json.dumps(context["skills"], indent=2)}

═══ WEAKEST AREAS ═══
{json.dumps(context["weakest_skills"])}

═══ STRONGEST AREAS ═══  
{json.dumps(context["strongest_skills"])}

═══ SCENARIO TYPE ═══
{scenario_type}

Write the instruction prompt for the voice AI agent. Include:

1. EMPLOYEE CONTEXT — Brief summary of who this person is, their experience 
   level, and current standing. Use their first name.

2. SESSION OBJECTIVE — What specific skill(s) to focus on this session, 
   based on their weakest areas and what they haven't practiced recently.

3. SCENARIO — A detailed, realistic situation for them to role-play that 
   targets their weak areas. Make it specific to their job title and department.
   Include:
   - The setup (who is the customer, what's the situation)
   - What makes it challenging (ties to their weak areas)
   - What a good response looks like vs a poor response

4. SCORING CRITERIA — Rate the employee on these dimensions (each 0-100):
   - communication: Clarity, active listening, verbal skills
   - empathy: Understanding the other person's perspective
   - resolution: Actually solving the problem effectively
   - professionalism: Tone, composure, staying appropriate
   - knowledge: Job-specific knowledge applied correctly

5. ADAPTIVE DIFFICULTY — Based on their score history:
   - Below 60 avg: Be encouraging, guide more, simpler scenarios
   - 60-80 avg: Moderate difficulty, some curveballs
   - Above 80 avg: Push them hard, add complications, be a difficult customer

6. CONVERSATION STYLE — 
   - Be warm but professional
   - Use their first name naturally
   - If this is their first session, welcome them and explain how this works
   - If they're returning, reference their progress
   - NEVER break character during the role-play
   - After the role-play, drop the character and give honest feedback

7. RED FLAGS — If the employee says anything inappropriate, discriminatory,
   or unsafe, immediately end the role-play and address it directly.

Keep under 600 words. Direct instructions to the agent only."""

        response = self.gemini.generate_content(prompt)
        briefing = response.text

        logger.info(f"[PREFLIGHT] Gemini briefing generated ({len(briefing)} chars)")
        return briefing

    # ──────────────────────────────────────────
    # STEP 3: Build the ElevenLabs session config
    # ──────────────────────────────────────────
    def build_agent_config(self, briefing: str, context: dict, scenario_type: str) -> dict:
        """
        Create the ElevenLabs Conversational AI override config.
        This gets sent when starting the conversation, injecting
        Gemini's briefing as the agent's instruction prompt.
        """
        first_name = context["employee"]["name"].split()[0]
        sessions = context["employee"]["sessions_completed"]

        # Dynamic first message based on whether they're new or returning
        if sessions == 0:
            first_message = (
                f"Hey {first_name}! Welcome to your first training session. "
                f"I'm your AI training partner. Here's how this works — I'm going to "
                f"put you in a realistic scenario based on your role, and we'll practice "
                f"together. There's no wrong answers here, this is about building skills. "
                f"Ready to jump in?"
            )
        elif sessions < 5:
            first_message = (
                f"Hey {first_name}, good to see you back! You've been making solid progress. "
                f"I've got a new scenario ready for you today. Let's pick up where we left off."
            )
        else:
            first_message = (
                f"Hey {first_name}! Ready for today's session? I've got something "
                f"that should challenge you based on how far you've come."
            )

        config = {
            "agent": {
                "prompt": {
                    "prompt": briefing
                },
                "first_message": first_message,
                "language": context["employee"].get("preferred_lang", "en"),
            }
        }

        logger.info(f"[PREFLIGHT] Agent armed for {first_name} — scenario: {scenario_type}")
        return config

    # ──────────────────────────────────────────
    # STEP 4: Create the session and get signed URL
    # ──────────────────────────────────────────
    async def create_elevenlabs_session(self, config: dict) -> str:
        """
        Call ElevenLabs API to create a conversation session
        with our overrides. Returns a signed URL that the
        frontend uses to connect the employee.
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/convai/conversation",
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "agent_id": ELEVENLABS_AGENT_ID,
                    "conversation_config_override": config,
                },
                timeout=15.0,
            )
            response.raise_for_status()
            data = response.json()

        signed_url = data.get("signed_url", data.get("conversation_id"))
        logger.info(f"[PREFLIGHT] ElevenLabs session created")
        return signed_url

    # ──────────────────────────────────────────
    # MAIN: The full pre-flight launch sequence
    # ──────────────────────────────────────────
    async def launch(self, org_id: str, employee_id: str,
                     scenario_type: str = "general") -> dict:
        """
        THE MAIN ENTRY POINT.
        Called when an employee clicks "Start Training."
        
        Returns everything the frontend needs to begin.
        """
        logger.info(f"[PREFLIGHT] ══ Launch sequence for {employee_id} ══")

        # Step 1: Get employee context
        context = await self.get_employee_context(org_id, employee_id)

        # Step 2: Gemini builds the briefing
        briefing = await self.build_briefing(context, scenario_type)

        # Step 3: Build agent config
        agent_config = self.build_agent_config(briefing, context, scenario_type)

        # Step 4: Create ElevenLabs session
        signed_url = await self.create_elevenlabs_session(agent_config)

        # Step 5: Create session record in our DB
        session_id = f"sess_{org_id}_{employee_id}_{int(datetime.now().timestamp())}"
        async with self.db.acquire() as conn:
            await conn.execute("""
                INSERT INTO training_sessions 
                    (session_id, employee_id, org_id, module_id,
                     gemini_briefing, agent_prompt, started_at, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'active')
            """,
                session_id, employee_id, org_id, scenario_type,
                briefing, json.dumps(agent_config), datetime.now()
            )

        logger.info(f"[PREFLIGHT] ══ Ready. Session: {session_id} ══")

        return {
            "session_id": session_id,
            "signed_url": signed_url,
            "employee_name": context["employee"]["name"],
            "scenario": scenario_type,
            "status": "ready",
        }
