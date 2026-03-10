# ============================================================
# app/services/supabase_service.py — All database operations
# ============================================================
# This is the ONLY file that talks to Supabase directly.
# Every other file calls functions from here.
# Think of it as your database "librarian."
# ============================================================

from supabase import create_client, Client
from app.config import settings
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# ── Create the Supabase client (one shared instance) ─────────
# service_key has full admin access — NEVER expose it to users
_supabase: Client = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_SERVICE_KEY,
)


# ─────────────────────────────────────────────────────────────
# AUTH OPERATIONS
# ─────────────────────────────────────────────────────────────

def signup_with_email(email: str, password: str, full_name: str | None) -> dict:
    """
    Creates a new Supabase Auth user (email + password).
    Also creates a matching row in the public.profiles table.
    Returns the session dict on success.
    """
    try:
        response = _supabase.auth.sign_up({
            "email": email,
            "password": password,
            "options": {
                "data": {"full_name": full_name or ""}
            },
        })

        if response.user is None:
            raise ValueError("Signup failed — Supabase returned no user object.")

        # Upsert a profile row so our app has a place to store extra fields
        _supabase.table("profiles").upsert({
            "id": response.user.id,
            "email": email,
            "full_name": full_name or "",
        }).execute()

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
    """
    Signs in an existing user with email + password.
    Returns access_token and basic user info.
    """
    try:
        response = _supabase.auth.sign_in_with_password({
            "email": email,
            "password": password,
        })

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
    """
    Exchanges a Google OAuth authorisation code for a Supabase session.
    Supabase handles all the Google OAuth complexity for us.
    """
    try:
        response = _supabase.auth.exchange_code_for_session({"auth_code": code})

        if response.user is None or response.session is None:
            raise ValueError("OAuth exchange failed.")

        # Ensure profile row exists
        _supabase.table("profiles").upsert({
            "id": response.user.id,
            "email": response.user.email,
            "full_name": response.user.user_metadata.get("full_name", ""),
            "avatar_url": response.user.user_metadata.get("avatar_url", ""),
        }).execute()

        return {
            "access_token": response.session.access_token,
            "user_id": response.user.id,
            "email": response.user.email,
            "full_name": response.user.user_metadata.get("full_name"),
        }

    except Exception as e:
        logger.error(f"exchange_oauth_code error: {e}")
        raise


# ─────────────────────────────────────────────────────────────
# VERIFICATION OPERATIONS
# ─────────────────────────────────────────────────────────────

def save_verification(
    user_id: str | None,
    original_content: str,
    source_url: str | None,
    content_type: str,
    score: float,
    verdict: str,
    verdict_label: str,
    verdict_color: str,
    summary: str,
    claim_results: list,
    sources_consulted: list,
) -> dict:
    """
    Saves a completed verification result to the database.
    Returns the saved row including its auto-generated ID.
    """
    counts = _count_claim_statuses(claim_results)

    record = {
        "user_id": user_id,
        "original_content": original_content[:5000],   # safety trim
        "content_preview": original_content[:120],
        "source_url": source_url,
        "content_type": content_type,
        "credibility_score": round(score, 2),
        "verdict": verdict,
        "verdict_label": verdict_label,
        "verdict_color": verdict_color,
        "summary": summary,
        "claims_total": counts["total"],
        "claims_verified": counts["verified"],
        "claims_false": counts["false"],
        "claims_disputed": counts["disputed"],
        "claims_unverified": counts["unverified"],
        "claim_results": claim_results,       # stored as JSONB
        "sources_consulted": sources_consulted,
    }

    result = _supabase.table("verifications").insert(record).execute()

    # Increment the user's verification counter
    if user_id:
        _increment_profile_counter(user_id)

    return result.data[0]


def get_verification_by_id(verification_id: str, user_id: str | None = None) -> dict | None:
    """Fetch a single verification. Optionally checks ownership."""
    query = _supabase.table("verifications").select("*").eq("id", verification_id)
    if user_id:
        query = query.eq("user_id", user_id)
    result = query.maybe_single().execute()
    return result.data


def get_user_history(user_id: str, page: int = 1, page_size: int = 20) -> dict:
    """
    Returns paginated verification history for a user.
    Newest results appear first.
    """
    offset = (page - 1) * page_size

    # Fetch rows (only the columns needed for the history list view)
    result = (
        _supabase.table("verifications")
        .select(
            "id, credibility_score, verdict, verdict_label, verdict_color, "
            "content_type, content_preview, claims_total, claims_verified, created_at"
        )
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .range(offset, offset + page_size - 1)
        .execute()
    )

    # Count total rows for pagination
    count_result = (
        _supabase.table("verifications")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .execute()
    )

    return {
        "items": result.data,
        "total": count_result.count or 0,
        "page": page,
        "page_size": page_size,
    }


# ─────────────────────────────────────────────────────────────
# USER PROFILE OPERATIONS
# ─────────────────────────────────────────────────────────────

def get_profile(user_id: str) -> dict | None:
    """Fetch a user's profile row."""
    result = (
        _supabase.table("profiles")
        .select("*")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    return result.data


def update_profile(user_id: str, updates: dict) -> dict:
    """Update mutable profile fields (full_name, avatar_url)."""
    result = (
        _supabase.table("profiles")
        .update(updates)
        .eq("id", user_id)
        .execute()
    )
    return result.data[0]


# ─────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────

def _count_claim_statuses(claim_results: list) -> dict:
    counts = {"total": len(claim_results), "verified": 0, "false": 0, "disputed": 0, "unverified": 0}
    for c in claim_results:
        status = (c.get("status") or "UNVERIFIED").upper()
        if status == "VERIFIED":
            counts["verified"] += 1
        elif status == "FALSE":
            counts["false"] += 1
        elif status == "DISPUTED":
            counts["disputed"] += 1
        else:
            counts["unverified"] += 1
    return counts


def _increment_profile_counter(user_id: str) -> None:
    """Increments total_verifications on the user's profile row."""
    try:
        _supabase.rpc("increment_verifications", {"uid": user_id}).execute()
    except Exception:
        pass   # non-critical — don't fail the whole request
