# =============================================================================
# app/services/supabase_service.py — FIXED VERSION
# =============================================================================
# FIXES APPLIED:
#   1. SUPABASE_SERVICE_KEY → SUPABASE_SERVICE_ROLE_KEY (wrong config key)
#   2. "profiles" table     → "users"                   (table doesn't exist)
#   3. "verifications" table → "verification_history"   (table doesn't exist)
#   4. Removed duplicate auth operations (supabase handles login natively)
#
# This file now only contains operations NOT already covered by database.py.
# For verification saves and history reads, import from database.py instead.
# =============================================================================

from supabase import create_client, Client
from app.config import settings
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# ── Create the Supabase admin client ─────────────────────────────────────────
# FIXED: was settings.SUPABASE_SERVICE_KEY → now settings.SUPABASE_SERVICE_ROLE_KEY
_supabase: Client = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_SERVICE_ROLE_KEY,   # ← FIXED
)


# =============================================================================
# AUTH OPERATIONS
# =============================================================================

def signup_with_email(email: str, password: str, full_name: str | None) -> dict:
    """
    Creates a new Supabase Auth user.
    NOTE: The database trigger (handle_new_user) automatically creates
    a matching row in public.users — no manual insert needed here.
    """
    try:
        response = _supabase.auth.sign_up({
            "email": email,
            "password": password,
            "options": {"data": {"full_name": full_name or ""}},
        })
        if response.user is None:
            raise ValueError("Signup failed — Supabase returned no user object.")

        return {
            "user_id": response.user.id,
            "email": response.user.email,
            "full_name": full_name,
            "access_token": response.session.access_token if response.session else None,
        }
    except Exception as e:
        logger.error(f"signup_with_email error: {e}")
        raise


def login_with_email(email: str, password: str) -> dict:
    try:
        response = _supabase.auth.sign_in_with_password({"email": email, "password": password})
        if response.user is None or response.session is None:
            raise ValueError("Login failed — invalid credentials.")
        return {
            "access_token": response.session.access_token,
            "user_id": response.user.id,
            "email": response.user.email,
            "full_name": response.user.user_metadata.get("full_name"),
        }
    except Exception as e:
        logger.error(f"login_with_email error: {e}")
        raise


def exchange_oauth_code(code: str) -> dict:
    """Exchanges a Google OAuth code for a Supabase session."""
    try:
        response = _supabase.auth.exchange_code_for_session({"auth_code": code})
        if response.user is None or response.session is None:
            raise ValueError("OAuth exchange failed.")
        return {
            "access_token": response.session.access_token,
            "user_id": response.user.id,
            "email": response.user.email,
            "full_name": response.user.user_metadata.get("full_name"),
        }
    except Exception as e:
        logger.error(f"exchange_oauth_code error: {e}")
        raise


# =============================================================================
# PROFILE OPERATIONS  (using correct table name: public.users)
# =============================================================================

def get_profile(user_id: str) -> dict | None:
    """Fetch a user's profile row from public.users (FIXED: was 'profiles')."""
    result = (
        _supabase.table("users")         # ← FIXED: was "profiles"
        .select("*")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    return result.data


def update_profile(user_id: str, updates: dict) -> dict:
    """Update mutable profile fields. Only full_name and avatar_url are allowed."""
    ALLOWED = {"full_name", "avatar_url"}
    safe = {k: v for k, v in updates.items() if k in ALLOWED}
    result = (
        _supabase.table("users")         # ← FIXED: was "profiles"
        .update(safe)
        .eq("id", user_id)
        .execute()
    )
    return result.data[0] if result.data else {}
