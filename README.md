# Lexfall Engine — Full System Architecture

## Overview

This is the complete backend engine for Lexfall's AI training platform.
Every training session follows this flow:

```
Employee clicks "Start Training"
         │
         ▼
┌─── PRE-FLIGHT ────────────────────────────────────────────┐
│                                                            │
│  1. server.py receives POST /api/session/start             │
│  2. data_adapter.py pulls employee from retailer's DB      │
│  3. preflight.py sends employee data to Gemini             │
│  4. Gemini writes personalized training briefing           │
│  5. agent_prompts.py assembles full agent prompt           │
│  6. Briefing injected into ElevenLabs agent via overrides  │
│  7. Signed URL returned to frontend                        │
│                                                            │
└─── Agent says "Hey Sarah!" ───────────────────────────────┘
         │
         ▼
┌─── LIVE SESSION ──────────────────────────────────────────┐
│                                                            │
│  ElevenLabs handles:                                       │
│    - Speech-to-text (employee's voice → text)              │
│    - Text-to-speech (agent's text → voice)                 │
│  Claude handles:                                           │
│    - Real-time reasoning and response generation           │
│    - Staying in character during role-play                  │
│    - Adaptive difficulty based on employee responses        │
│                                                            │
└─── Employee finishes session ─────────────────────────────┘
         │
         ▼
┌─── POST-SESSION ──────────────────────────────────────────┐
│                                                            │
│  1. ElevenLabs webhook hits POST /api/webhook/elevenlabs   │
│  2. scoring.py pulls transcript from ElevenLabs            │
│  3. Transcript sent to Gemini for evaluation               │
│  4. Gemini returns scores per skill (0-100)                │
│  5. Employee's skill scores updated (weighted avg)          │
│  6. Employee's overall profile updated                      │
│  7. Full session saved: transcript, scores, feedback        │
│                                                            │
└─── Next session will be even smarter ─────────────────────┘
```


## File Structure

```
lexfall-engine/
│
├── schema.sql           # Database tables for Lexfall
│                         # - organizations (the clients)
│                         # - employees (synced from client)
│                         # - training_sessions (every conversation)
│                         # - employee_skills (running skill scores)
│                         # - field_mappings (client → Lexfall translation)
│                         # - training_modules (available scenarios)
│                         # - sync_log (data import tracking)
│
├── data_adapter.py      # Universal data adapter
│                         # Normalizes ANY retailer's data format into Lexfall's
│                         # Supports: REST API, Direct DB, CSV/SFTP
│                         # FieldMapper handles field name translation
│                         # AdapterFactory auto-selects the right adapter
│
├── preflight.py         # Pre-flight engine (THE CORE)
│                         # Step 1: Pull employee context from DB
│                         # Step 2: Send to Gemini → get briefing
│                         # Step 3: Build ElevenLabs session config
│                         # Step 4: Create session, return signed URL
│
├── scoring.py           # Post-session scoring engine
│                         # Step 1: Get transcript from ElevenLabs
│                         # Step 2: Gemini evaluates performance
│                         # Step 3: Update skill scores (weighted avg)
│                         # Step 4: Update employee profile
│                         # Step 5: Save full session results
│
├── agent_prompts.py     # ElevenLabs agent prompt templates
│                         # Base prompt (agent identity)
│                         # Scenario overlays (de-escalation, safety, etc.)
│                         # Prompt builder (assembles final prompt)
│                         # Recommended ElevenLabs dashboard config
│
├── server.py            # FastAPI backend
│                         # POST /api/session/start (pre-flight)
│                         # POST /api/session/end (scoring)
│                         # POST /api/webhook/elevenlabs (auto-scoring)
│                         # GET  /api/employee/{org}/{id} (profile)
│                         # GET  /api/dashboard/{org} (manager view)
│                         # GET  /api/scenarios/{org} (available modules)
│
└── README.md            # This file
```


## How Retailers Connect Their Database

The key insight: **retailers don't change anything on their end.**

During onboarding, we ask: "What do you call your employee ID field?"
They say: "WIN" (Walmart) or "TM_ID" (Target) or "emp_number" (generic).

We store that mapping ONCE in the `field_mappings` table:

```
Lexfall Field    →  Their Field
─────────────────────────────────
employee_id      →  WIN
name             →  associate_name
job_title        →  job_code_desc
department       →  dept_name
hire_date        →  original_hire_dt
store_location   →  facility_nbr
```

After that, every sync auto-translates. The `data_adapter.py` FieldMapper
handles it. New employees show up automatically. Field names don't matter
anymore.

### Three integration paths:

**Option A: REST API** — They expose `GET /employees/{id}`, we call it.
They control access. Most secure. Enterprise preferred.

**Option B: Direct DB** — They give us read-only credentials. We query
their table directly. Fastest setup. Works with PostgreSQL, MySQL,
Firebase, MongoDB, DynamoDB, SQL Server.

**Option C: CSV Upload** — They upload a spreadsheet nightly. We import
it. Zero technical changes on their end. Best for pilot programs.


## Scoring System

Every session generates scores across 5 dimensions (0-100):

| Dimension       | What It Measures                                    |
|-----------------|-----------------------------------------------------|
| Communication   | Clarity, listening, professional language            |
| Empathy         | Understanding feelings, validating experiences       |
| Resolution      | Actually solving the problem, offering solutions     |
| Professionalism | Staying calm, appropriate tone, composure            |
| Knowledge       | Job-specific knowledge, accurate information         |

Scores update via **weighted moving average** (30% new, 70% history)
so a single bad day doesn't tank someone's profile, but consistent
improvement is reflected quickly.

Employee profiles track:
- Per-skill scores with trend direction (improving/declining/stable)
- Overall average across all sessions
- Session count and recency
- Weakest areas (auto-fed into next session's Gemini briefing)

Manager dashboard shows:
- Org-wide averages by department
- Weakest skills across the organization
- Employees needing attention (low scores, no recent sessions)
- Recent session feed with pass/fail


## Environment Variables

```env
DATABASE_URL=postgresql://user:pass@host:5432/lexfall
GEMINI_API_KEY=your_gemini_api_key
ELEVENLABS_API_KEY=your_elevenlabs_api_key
ELEVENLABS_AGENT_ID=your_agent_id
```


## Deployment

Recommended: Deploy on Railway, Render, or Fly.io with a managed
PostgreSQL database. The FastAPI server is stateless and scales
horizontally.

```bash
pip install fastapi uvicorn asyncpg google-generativeai httpx
uvicorn server:app --host 0.0.0.0 --port 8000
```


## What's Next

- [ ] Admin portal for retailers to manage field mappings via UI
- [ ] Real-time session monitoring (manager can watch live scores)
- [ ] Multi-language support (agent speaks employee's preferred language)
- [ ] Batch reporting API (weekly/monthly PDF reports)
- [ ] SSO integration (Okta, Azure AD, Google Workspace)
