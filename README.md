# Credibility AI Platform — Backend API

> AI-powered social media fact-checking engine. Verifies claims against live news sources, scores credibility across five dimensions, and returns a detailed analysis report.

---

## What This Does

A user pastes a tweet, article, or social media post into the frontend. This backend:

1. Receives the extracted claims from the frontend
2. Checks a global claim cache — if the claim was verified before, returns instantly
3. Searches live news sources (NewsAPI + GNews) in parallel for new claims
4. Runs a five-judge scoring engine to produce a 0–100 credibility score
5. Analyses the account/source credibility if metadata is provided
6. Saves the result to the user's history
7. Returns a complete credibility report to the frontend

---

## Tech Stack

| Layer | Technology |
|---|---|
| Framework | FastAPI (Python 3.11) |
| Auth | Supabase JWT (PyJWT) |
| Database | Supabase (PostgreSQL) |
| News Search | NewsAPI + GNews (parallel) |
| AI Embeddings | sentence-transformers (local, free) |
| Semantic Search | pgvector (cosine similarity) |
| Deployment | Koyeb |

---

## Project Structure

```
credibility-backend/
│
├── main.py                        # App entry point, route registration, CORS
├── requirements.txt               # Python dependencies
├── Dockerfile                     # Container config for Koyeb deployment
├── render.yaml                    # Render.com deployment config (alternative)
│
└── app/
    ├── config.py                  # All settings loaded from .env
    │
    ├── models/
    │   └── schemas.py             # Request/response Pydantic models
    │
    ├── routes/
    │   ├── auth.py                # POST /auth/signup, POST /auth/login
    │   ├── verify.py              # POST /verify/ (core feature)
    │   ├── history.py             # GET /history/, GET /history/:id
    │   ├── user.py                # GET /user/, PATCH /user/, GET /user/usage
    │   ├── usage.py               # GET /usage/today
    │   └── claims.py              # GET /claims/stats
    │
    ├── middleware/
    │   ├── auth.py                # JWT verification dependency
    │   └── rate_limit.py          # Daily usage limit enforcement
    │
    └── services/
        ├── database.py            # All Supabase database operations
        ├── news_service.py        # NewsAPI + GNews parallel search
        ├── scoring_engine.py      # Five-judge credibility scoring
        ├── claim_cache.py         # Global claim cache (memory + database)
        ├── embedding_service.py   # AI embeddings for semantic search
        ├── account_credibility.py # Source/account trust analysis
        ├── usage_service.py       # Daily usage tracking
        └── supabase_service.py    # Auth operations
```

---

## API Endpoints

### Health
```
GET  /          → Service info
GET  /health    → Health check
GET  /docs      → Interactive API documentation
```

### Authentication
```
POST /auth/signup   → Create account with email + password
POST /auth/login    → Login with email + password
```

### Verification (Core Feature)
```
POST /verify/       → Verify content credibility (requires auth)
```

### History
```
GET  /history/          → Get paginated verification history
GET  /history/:id       → Get single verification by ID
```

### User
```
GET   /user/        → Get current user profile
PATCH /user/        → Update profile (name, avatar)
GET   /user/usage   → Get daily usage status
GET   /user/stats   → Get aggregate stats
```

### Usage
```
GET /usage/today    → Today's verification count and limit
```

### Cache
```
GET /claims/stats   → Global claim cache statistics
```

---

## Verification Pipeline

When `POST /verify/` is called, this 10-step pipeline runs:

```
1. Verify JWT token → identify user
2. Look up user plan → set daily limit (free=10, pro=100)
3. Check daily usage → block with HTTP 429 if limit reached
4. For each claim (all processed in parallel):
   a. Check memory cache      < 1ms
   b. Check database cache    ~20ms
   c. Semantic similarity     ~30ms  ← finds same claim with different wording
   d. Live news search        ~1s    ← only if all cache layers miss
5. Evaluate each claim: VERIFIED / FALSE / DISPUTED / UNVERIFIED
6. Run 5-judge scoring engine → 0-100 score + verdict
7. Optionally blend account credibility score (15% weight)
8. Save result to verification_history table
9. Store new claims in global_claims cache (background task)
10. Return complete credibility report
```

---

## Scoring Engine — Five Judges

| Judge | What It Scores | Weight |
|---|---|---|
| Claims Judge | Are the specific facts true? | 40% |
| Source Judge | Are the sources credible? | 25% |
| Language Judge | Does the text use panic/urgency tactics? | 15% |
| Fact-Check Judge | Has this been debunked before? | 12% |
| Patterns Judge | Does this look like misinformation? | 8% |

**Verdict scale:**

| Score | Verdict |
|---|---|
| 80–100 | ✅ Verified |
| 65–79 | 🟡 Mostly True |
| 45–64 | 🟠 Questionable |
| 25–44 | 🔴 Misleading |
| 0–24 | ❌ False / Fabricated |

---

## Claim Cache — How It Works

Every verified claim is stored in a global cache shared across all users. When two users submit the same claim, the second gets an instant result instead of waiting for a live news search.

**Four layers:**

```
Layer 1: Python memory cache     < 1ms    (exact match)
Layer 2: Supabase hash lookup    ~20ms    (exact match)
Layer 2b: Semantic search        ~30ms    (meaning match via pgvector)
Layer 3: Live news search        ~1000ms  (only on full cache miss)
```

The semantic search layer means `"Vaccines cause autism"` and `"Autism linked to vaccination"` are treated as the same claim and share the same cached result.

---

## Database Tables

| Table | Purpose |
|---|---|
| `public.users` | User profiles linked to Supabase Auth |
| `public.verification_history` | Every verification ever run |
| `public.usage_limits` | Daily quota tracking per user |
| `public.global_claims` | Shared claim cache with embeddings |

---

## Environment Variables

Create a `.env` file in the root folder:

```env
# App
ENVIRONMENT=development
SECRET_KEY=your-random-secret-key-here

# Supabase (Settings → API in Supabase dashboard)
SUPABASE_URL=https://YOUR_PROJECT_ID.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_JWT_SECRET=your-jwt-secret

# News APIs (free tier: 100 req/day each)
# Get keys at newsapi.org and gnews.io
NEWSAPI_KEY=your-newsapi-key
GNEWS_KEY=your-gnews-key

# CORS — set to your frontend URL
ALLOWED_ORIGINS_STR=http://localhost:3000

# Optional: OpenAI for embeddings (leave blank to use free local model)
OPENAI_API_KEY=
```

---

## Local Development

**Requirements:** Python 3.11+

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/credibility-backend.git
cd credibility-backend

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate      # Mac/Linux
venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create .env file and fill in your values
cp .env.example .env

# 5. Start the server
uvicorn main:app --reload --port 8000
```

Server runs at: `http://localhost:8000`
API docs at: `http://localhost:8000/docs`

---

## Deployment

This backend is deployed on **Koyeb** (free tier, no cold starts).

**Required environment variables in Koyeb dashboard:**

```
ENVIRONMENT              = production
SUPABASE_URL             = https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY = your-service-role-key
SUPABASE_ANON_KEY        = your-anon-key
SUPABASE_JWT_SECRET      = your-jwt-secret
NEWSAPI_KEY              = your-newsapi-key
GNEWS_KEY                = your-gnews-key
ALLOWED_ORIGINS_STR      = https://your-frontend.vercel.app
SECRET_KEY               = your-random-secret
```

---

## Security

- All protected endpoints require a valid Supabase JWT in the `Authorization: Bearer` header
- `user_id` is always extracted from the verified JWT — never trusted from the request body
- Service role key is server-side only — never exposed to the browser
- Row Level Security (RLS) enabled on all Supabase tables
- Daily rate limiting uses atomic database locks to prevent bypass
- IP addresses are SHA-256 hashed before storage (privacy)
- GDPR erasure function built into the database schema

---

## Rate Limits

| Plan | Verifications per day |
|---|---|
| Free | 10 |
| Pro | 100 |
| Enterprise | Unlimited |

Limits reset at midnight UTC. When the limit is reached, the API returns `HTTP 429` with a clear error message and the reset time.

---

## License

Private — All rights reserved.
