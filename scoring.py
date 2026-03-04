"""
LEXFALL SCORING ENGINE
======================
Runs AFTER every training session.

Takes the conversation transcript, sends it to Gemini for evaluation,
calculates scores, updates the employee's profile, and saves everything
back to the database.

This is what makes each session smarter than the last.
"""

import json
import logging
from datetime import datetime
from typing import Optional

import google.generativeai as genai

logger = logging.getLogger("lexfall.scoring")


class ScoringEngine:
    """
    Post-session processing pipeline:
      1. Get transcript from ElevenLabs
      2. Send to Gemini for scoring and analysis
      3. Update employee skills and overall score
      4. Save everything to the database
      5. Determine what the next session should focus on
    """

    def __init__(self, db_pool):
        self.db = db_pool
        self.gemini = genai.GenerativeModel("gemini-1.5-flash")

    # ──────────────────────────────────────────
    # STEP 1: Get the transcript from ElevenLabs
    # ──────────────────────────────────────────
    async def get_transcript(self, conversation_id: str, api_key: str) -> list[dict]:
        """
        Pull the full conversation transcript from ElevenLabs.
        Returns a list of turns: [{"role": "agent"|"user", "text": "...", "timestamp": "..."}]
        """
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://api.elevenlabs.io/v1/convai/conversation/{conversation_id}",
                headers={"xi-api-key": api_key},
                timeout=15.0,
            )
            response.raise_for_status()
            data = response.json()

        transcript = []
        for turn in data.get("transcript", []):
            transcript.append({
                "role": turn.get("role", "unknown"),
                "text": turn.get("message", ""),
                "timestamp": turn.get("time_in_call_secs", 0),
            })

        return transcript

    # ──────────────────────────────────────────
    # STEP 2: Gemini scores the session
    # ──────────────────────────────────────────
    async def evaluate_session(self, transcript: list[dict],
                                employee_context: dict,
                                scenario_type: str) -> dict:
        """
        Send the transcript to Gemini for scoring and analysis.
        Returns structured scores, feedback, and recommendations.
        """

        prompt = f"""You are the scoring engine for Lexfall, an AI employee training platform.

Analyze this training conversation transcript and score the employee's performance.

═══ EMPLOYEE CONTEXT ═══
Name: {employee_context['employee']['name']}
Job Title: {employee_context['employee']['job_title']}
Department: {employee_context['employee']['department']}
Sessions Completed: {employee_context['employee']['sessions_completed']}
Current Average Score: {employee_context['employee']['overall_score']}

═══ SCENARIO TYPE ═══
{scenario_type}

═══ TRANSCRIPT ═══
{json.dumps(transcript, indent=2)}

Score the employee on each dimension (0-100):

1. communication — Clarity, active listening, professional language, letting the 
   other person speak, not interrupting, asking good questions
2. empathy — Understanding the customer/person's feelings, acknowledging frustration,
   showing genuine concern, validating their experience
3. resolution — Actually solving or addressing the core problem, offering concrete 
   solutions, following through, knowing when to escalate
4. professionalism — Staying calm under pressure, appropriate tone, not getting 
   defensive, maintaining composure even when challenged
5. knowledge — Applying relevant job knowledge, knowing policies and procedures,
   giving accurate information, not making things up

RESPOND IN EXACT JSON FORMAT (no markdown, no backticks):
{{
    "scores": {{
        "communication": <0-100>,
        "empathy": <0-100>,
        "resolution": <0-100>,
        "professionalism": <0-100>,
        "knowledge": <0-100>
    }},
    "overall_score": <0-100 weighted average>,
    "passed": <true if overall >= 75>,
    "strengths": ["specific thing they did well", "another strength"],
    "improvements": ["specific thing to improve", "another area"],
    "summary": "2-3 sentence assessment of how they did overall",
    "next_focus": "what the next session should prioritize",
    "notable_moments": [
        {{"timestamp": <seconds>, "type": "positive|negative", "note": "what happened"}}
    ]
}}"""

        response = self.gemini.generate_content(prompt)
        raw_text = response.text.strip()

        # Clean up potential markdown wrapping
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1]
        if raw_text.endswith("```"):
            raw_text = raw_text.rsplit("```", 1)[0]
        raw_text = raw_text.strip()

        try:
            evaluation = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.error(f"[SCORING] Failed to parse Gemini response: {raw_text[:200]}")
            # Fallback: generate basic scores
            evaluation = {
                "scores": {
                    "communication": 70,
                    "empathy": 70,
                    "resolution": 70,
                    "professionalism": 70,
                    "knowledge": 70,
                },
                "overall_score": 70,
                "passed": False,
                "strengths": ["Completed the session"],
                "improvements": ["Score could not be parsed — manual review recommended"],
                "summary": "Session completed but automated scoring encountered an error.",
                "next_focus": "Repeat this scenario type",
                "notable_moments": [],
            }

        logger.info(
            f"[SCORING] {employee_context['employee']['name']} scored "
            f"{evaluation['overall_score']} — {'PASSED' if evaluation['passed'] else 'NEEDS WORK'}"
        )

        return evaluation

    # ──────────────────────────────────────────
    # STEP 3: Update employee skills
    # ──────────────────────────────────────────
    async def update_skills(self, employee_id: str, org_id: str,
                             scores: dict) -> None:
        """
        Update the running skill scores for this employee.
        Uses a weighted moving average so recent sessions matter more.
        """
        WEIGHT_NEW = 0.3  # 30% weight on new score, 70% on history

        async with self.db.acquire() as conn:
            for skill_name, new_score in scores.items():
                # Get existing skill record
                existing = await conn.fetchrow("""
                    SELECT current_score, sessions_rated 
                    FROM employee_skills
                    WHERE employee_id = $1 AND org_id = $2 AND skill_name = $3
                """, employee_id, org_id, skill_name)

                if existing:
                    old_score = float(existing["current_score"])
                    times_rated = existing["sessions_rated"]

                    # Weighted moving average
                    updated_score = (old_score * (1 - WEIGHT_NEW)) + (new_score * WEIGHT_NEW)

                    # Determine trend
                    if new_score > old_score + 3:
                        trend = "improving"
                    elif new_score < old_score - 3:
                        trend = "declining"
                    else:
                        trend = "stable"

                    await conn.execute("""
                        UPDATE employee_skills
                        SET current_score = $1, trend = $2, 
                            sessions_rated = $3, last_updated = $4
                        WHERE employee_id = $5 AND org_id = $6 AND skill_name = $7
                    """, updated_score, trend, times_rated + 1,
                        datetime.now(), employee_id, org_id, skill_name)

                else:
                    # First time scoring this skill
                    await conn.execute("""
                        INSERT INTO employee_skills 
                            (employee_id, org_id, skill_name, current_score,
                             trend, sessions_rated, last_updated)
                        VALUES ($1, $2, $3, $4, 'stable', 1, $5)
                    """, employee_id, org_id, skill_name, new_score, datetime.now())

        logger.info(f"[SCORING] Updated {len(scores)} skill scores for {employee_id}")

    # ──────────────────────────────────────────
    # STEP 4: Update the employee's overall profile
    # ──────────────────────────────────────────
    async def update_employee_profile(self, employee_id: str, org_id: str,
                                       overall_score: float,
                                       improvements: list[str]) -> None:
        """Update the employee's running average and session count."""
        async with self.db.acquire() as conn:
            employee = await conn.fetchrow("""
                SELECT overall_score, sessions_total FROM employees
                WHERE employee_id = $1 AND org_id = $2
            """, employee_id, org_id)

            old_avg = float(employee["overall_score"])
            total = employee["sessions_total"]

            # Running average
            if total == 0:
                new_avg = overall_score
            else:
                new_avg = ((old_avg * total) + overall_score) / (total + 1)

            await conn.execute("""
                UPDATE employees
                SET overall_score = $1,
                    sessions_total = sessions_total + 1,
                    last_session = $2
                WHERE employee_id = $3 AND org_id = $4
            """, round(new_avg, 2), datetime.now(), employee_id, org_id)

        logger.info(
            f"[SCORING] {employee_id} profile updated — "
            f"avg: {old_avg:.1f} → {new_avg:.1f}, sessions: {total + 1}"
        )

    # ──────────────────────────────────────────
    # STEP 5: Save full session results
    # ──────────────────────────────────────────
    async def save_session_results(self, session_id: str,
                                    transcript: list[dict],
                                    evaluation: dict,
                                    duration_secs: int) -> None:
        """Save everything about this session to the database."""
        async with self.db.acquire() as conn:
            await conn.execute("""
                UPDATE training_sessions
                SET ended_at = $1,
                    duration_secs = $2,
                    status = 'completed',
                    score = $3,
                    passed = $4,
                    score_breakdown = $5,
                    strengths = $6,
                    improvements = $7,
                    agent_notes = $8,
                    transcript = $9
                WHERE session_id = $10
            """,
                datetime.now(),
                duration_secs,
                evaluation["overall_score"],
                evaluation["passed"],
                json.dumps(evaluation["scores"]),
                evaluation.get("strengths", []),
                evaluation.get("improvements", []),
                evaluation.get("summary", ""),
                json.dumps(transcript),
                session_id,
            )

        logger.info(f"[SCORING] Session {session_id} saved — score: {evaluation['overall_score']}")

    # ──────────────────────────────────────────
    # MAIN: Full post-session pipeline
    # ──────────────────────────────────────────
    async def process_completed_session(
        self,
        session_id: str,
        conversation_id: str,
        employee_id: str,
        org_id: str,
        employee_context: dict,
        scenario_type: str,
        api_key: str,
        duration_secs: int = 0,
    ) -> dict:
        """
        THE MAIN ENTRY POINT for post-session processing.
        Called when ElevenLabs signals the conversation has ended.
        
        Returns the full evaluation results.
        """
        logger.info(f"[SCORING] ══ Processing session {session_id} ══")

        # 1. Get transcript
        transcript = await self.get_transcript(conversation_id, api_key)
        logger.info(f"[SCORING] Transcript: {len(transcript)} turns")

        # 2. Gemini evaluates
        evaluation = await self.evaluate_session(
            transcript, employee_context, scenario_type
        )

        # 3. Update individual skill scores
        await self.update_skills(employee_id, org_id, evaluation["scores"])

        # 4. Update employee profile
        await self.update_employee_profile(
            employee_id, org_id,
            evaluation["overall_score"],
            evaluation.get("improvements", [])
        )

        # 5. Save full session
        await self.save_session_results(
            session_id, transcript, evaluation, duration_secs
        )

        logger.info(f"[SCORING] ══ Complete. Score: {evaluation['overall_score']} ══")

        return evaluation
