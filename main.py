# =============================================================================
# main.py — Entry point for the Credibility Platform API
# Run locally with: uvicorn main:app --reload --port 8000
# =============================================================================

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import ResponseValidationError
from contextlib import asynccontextmanager
import logging
import traceback

from app.routes import auth, verify, history, user, usage
from app.routes import claims
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
app.include_router(claims.router,  prefix="/claims",  tags=["🗄️ Global Claims"])


@app.get("/", tags=["Health"])
async def root():
    return {
        "status":  "ok",
        "service": "Credibility Verification Platform",
        "version": "2.0.0",
        "docs":    "/docs",
    }


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "healthy"}


# =============================================================================
# EXCEPTION HANDLERS
# =============================================================================
#
# IMPORTANT: FastAPI 0.100+ has a separate internal exception type called
# ResponseValidationError that fires when Pydantic fails to serialize the
# RESPONSE (not the request). This bypasses the generic Exception handler
# below AND bypasses CORSMiddleware — causing 500s with no CORS headers
# and no log output.
#
# We must register a handler for it explicitly so that:
#   1. The actual error is logged to Railway
#   2. The response goes through CORSMiddleware (CORS headers are added)
#
# =============================================================================

@app.exception_handler(ResponseValidationError)
async def response_validation_error_handler(request: Request, exc: ResponseValidationError):
    """
    Catches Pydantic response serialization failures.
    These happen when a route returns data that doesn't match its response model.
    Without this handler, FastAPI returns 500 with no CORS headers and no logs.
    """
    logger.error(
        f"[ResponseValidationError] Route: {request.method} {request.url.path}\n"
        f"Error: {exc}\n"
        f"{traceback.format_exc()}"
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Response serialization failed. Check Railway logs for details.",
            "path": str(request.url.path),
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Catches all other unhandled exceptions.
    Logs the full traceback so Railway logs show the exact error.
    """
    logger.error(
        f"[UnhandledException] Route: {request.method} {request.url.path}\n"
        f"Error: {type(exc).__name__}: {exc}\n"
        f"{traceback.format_exc()}"
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal server error occurred. Please try again."},
    )
