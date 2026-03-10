# =============================================================================
# app/routes/auth.py — Login and signup routes
# =============================================================================

from fastapi import APIRouter, HTTPException, status
from app.models.schemas import SignupRequest, LoginRequest, AuthResponse
from app.services.supabase_service import signup_with_email, login_with_email
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/signup", response_model=AuthResponse, summary="Sign up with email")
async def signup(payload: SignupRequest):
    try:
        result = signup_with_email(
            email     = payload.email,
            password  = payload.password,
            full_name = payload.full_name,
        )
        return AuthResponse(
            access_token = result["access_token"] or "",
            user_id      = result["user_id"],
            email        = result["email"],
            full_name    = result.get("full_name"),
        )
    except Exception as e:
        logger.error(f"Signup failed: {e}")
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = str(e),
        )


@router.post("/login", response_model=AuthResponse, summary="Login with email")
async def login(payload: LoginRequest):
    try:
        result = login_with_email(
            email    = payload.email,
            password = payload.password,
        )
        return AuthResponse(
            access_token = result["access_token"],
            user_id      = result["user_id"],
            email        = result["email"],
            full_name    = result.get("full_name"),
        )
    except Exception as e:
        logger.error(f"Login failed: {e}")
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Invalid email or password.",
        )
```

---

## After Both Fixes — Your Final Structure Should Be
```
credibility-backend/
├── main.py                ← ROOT level (not inside app/)
├── requirements.txt
├── Dockerfile
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── models/
│   │   ├── __init__.py
│   │   └── schemas.py
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── auth.py        ← just created
│   │   ├── verify.py
│   │   ├── history.py
│   │   ├── user.py
│   │   ├── usage.py
│   │   └── claims.py
│   ├── middleware/
│   │   ├── __init__.py
│   │   └── auth.py        ← JWT verification (already exists)
│   └── services/
│       ├── __init__.py
│       ├── database.py
│       ├── news_service.py
│       ├── scoring_engine.py
│       ├── claim_cache.py
│       ├── embedding_service.py
│       ├── account_credibility.py
│       ├── usage_service.py
│       └── supabase_service.py
