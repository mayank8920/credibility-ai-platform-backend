# =============================================================================
# main.py — Entry point for the Credibility Platform API
# Run locally with: uvicorn main:app --reload --port 8000
# =============================================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import logging

from app.routes import auth, verify, history, user, usage
from app.routes import claims          # ← NEW: global claim cache admin routes
from app.config import settings

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("credibility-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Credibility Platform API starting up…")
    logger.info(f"   Environment         : {settings.ENVIRONMENT}")
    logger.info(f"   CORS origins        : {settings.ALLOWED_ORIGINS}")
    logger.info(f"   Free daily limit    : {settings.DAILY_LIMIT_FREE} verifications/day")
    logger.info(f"   Claim cache enabled : {settings.CLAIM_CACHE_ENABLED}")
    logger.info(f"   Claim cache size    : {settings.CLAIM_CACHE_MEMORY_SIZE} entries")
    logger.info(f"   Claim cache TTL     : {settings.CLAIM_CACHE_TTL_SECONDS}s")
    yield
    logger.info("🛑 API shutting down.")


app = FastAPI(
    title       = "Credibility Verification Platform API",
    description = (
        "AI-powered social media fact-checking backend.\n\n"
        "Features:\n"
        "• JWT authentication via Supabase\n"
        "• Per-claim global cache with SHA-256 lookup\n"
        "• Daily usage limits per user\n"
        "• Five-judge scoring engine\n"
        "• Account credibility analysis"
    ),
    version  = "2.0.0",
    docs_url = "/docs",
    redoc_url= "/redoc",
    lifespan = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = settings.ALLOWED_ORIGINS,
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── Route groups ──────────────────────────────────────────────────────────────
app.include_router(auth.router,    prefix="/auth",    tags=["🔐 Authentication"])
app.include_router(verify.router,  prefix="/verify",  tags=["🔍 Verification"])
app.include_router(history.router, prefix="/history", tags=["📋 History"])
app.include_router(user.router,    prefix="/user",    tags=["👤 User"])
app.include_router(usage.router,   prefix="/usage",   tags=["📊 Usage Tracking"])
app.include_router(claims.router,  prefix="/claims",  tags=["🗄️ Global Claims"])  # ← NEW


@app.get("/", tags=["Health"])
async def root():
    return {
        "status":  "ok",
        "service": "Credibility Verification Platform",
        "version": "2.0.0",
        "features": [
            "global_claim_cache",
            "daily_usage_limits",
            "account_credibility",
            "five_judge_scoring",
        ],
        "docs": "/docs",
    }


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "healthy"}


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal server error occurred. Please try again."},
    )
