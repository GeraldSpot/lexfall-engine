# Lexfall on Squarespace — Complete Setup Guide

## The Architecture

```
┌─────────────────────────────────────┐
│         SQUARESPACE (Frontend)       │
│                                      │
│  Your Lexfall website                │
│  Marketing pages, pricing, etc.      │
│  Training page has embedded widget   │
│                                      │
│  Employee clicks "Start Training"    │
│           │                          │
└───────────┼──────────────────────────┘
            │  API call (fetch)
            ▼
┌─────────────────────────────────────┐
│     RAILWAY / RENDER (Backend)       │
│                                      │
│  server.py (FastAPI)                 │
│  preflight.py                        │
│  scoring.py                          │
│  data_adapter.py                     │
│                                      │
│  This is where the magic happens.    │
│  Squarespace can't run Python,       │
│  so the backend lives here.          │
│           │                          │
└───────────┼──────────────────────────┘
            │  API calls
            ▼
┌──────────────────────────────────────────────┐
│              EXTERNAL SERVICES                │
│                                               │
│  PostgreSQL (database — Railway or Supabase)  │
│  Google Gemini (builds training briefings)    │
│  ElevenLabs (voice AI conversation)           │
│  Claude (reasoning during conversation)       │
│                                               │
└──────────────────────────────────────────────┘
```

## Step-by-Step Setup

### 1. Deploy the Backend (Railway — Free Tier)

Railway gives you a server + database for free (with limits).

```bash
# In your lexfall-engine folder:
railway init
railway add --plugin postgresql

# Set environment variables
railway variables set GEMINI_API_KEY=your_key
railway variables set ELEVENLABS_API_KEY=your_key
railway variables set ELEVENLABS_AGENT_ID=your_agent_id

# Deploy
railway up
```

After deploy, Railway gives you a URL like:
`https://lexfall-api-production.up.railway.app`

That's your LEXFALL_API_URL.

### 2. Set Up the Database

SSH into your Railway deployment or use their dashboard:

```bash
# Run the schema
psql $DATABASE_URL < schema.sql
```

### 3. Configure the ElevenLabs Agent

In the ElevenLabs dashboard:
1. Create a new Conversational AI agent
2. Set the base prompt from `agent_prompts.py` (BASE_AGENT_PROMPT)
3. Choose a voice (Rachel, Drew, or Aria work well)
4. Set the LLM to Claude Sonnet
5. Set the webhook URL to:
   `https://your-railway-url.railway.app/api/webhook/elevenlabs`
6. Copy the Agent ID — you need it for the env variable

### 4. Add the Widget to Squarespace

**Option A: On a specific page (recommended)**
1. Go to your training page in Squarespace
2. Click Edit → Add Section → Code
3. Paste the entire contents of `squarespace-widget.html`
4. Change these two lines at the top of the <script>:

```javascript
const LEXFALL_API_URL = "https://your-railway-url.railway.app";
const LEXFALL_ORG_ID = "your_org_id";
```

5. Save and publish

**Option B: Site-wide (via Code Injection)**
1. Settings → Advanced → Code Injection
2. Paste in the Footer section
3. The widget will appear wherever you place a div with id="lexfall-widget"

### 5. Add Your First Client (Yourself for Testing)

Insert a test organization and employee into your database:

```sql
-- Add your org
INSERT INTO organizations (org_id, org_name, industry, integration_type)
VALUES ('lexfall_demo', 'Lexfall Demo', 'Retail', 'csv');

-- Add a test employee
INSERT INTO employees (employee_id, org_id, external_id, name, job_title, department, store_location, overall_score)
VALUES ('lexfall_demo_001', 'lexfall_demo', '001', 'Sarah Johnson', 'Deli Associate', 'Deli/Bakery', 'Store 1234', 0);

-- Add a training module
INSERT INTO training_modules (module_id, org_id, module_name, category, difficulty)
VALUES ('customer_de-escalation', 'lexfall_demo', 'Customer De-escalation', 'customer_service', 'adaptive');
```

Now go to your training page and enter `lexfall_demo_001` as the employee ID.
The full pre-flight will run and the agent will greet Sarah by name.

## CORS Note

Make sure your `server.py` allows your Squarespace domain:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.lexfall.com",        # Your Squarespace domain
        "https://lexfall.com",
        "http://localhost:3000",           # For local dev
    ],
    ...
)
```

## Cost Breakdown (Starting Out)

| Service        | Free Tier                    | When You'd Pay       |
|----------------|------------------------------|----------------------|
| Squarespace    | $16-33/mo (you already have) | —                    |
| Railway        | Free (500 hrs/mo)            | $5/mo for always-on  |
| PostgreSQL     | Free on Railway (1GB)        | $5/mo for more       |
| ElevenLabs     | Free tier (limited mins)     | $5-22/mo for more    |
| Gemini API     | Free tier (generous)         | Pay-per-use at scale |
| Claude API     | Via ElevenLabs               | Included in EL plan  |

**Total to launch: ~$16-33/mo** (just your Squarespace plan)
Everything else fits in free tiers for demo/pilot usage.
