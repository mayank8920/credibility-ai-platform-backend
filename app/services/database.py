# =============================================================================
# app/services/database.py — All database operations
# =============================================================================
# Plain-English:
#   This file is the ONLY place that talks to Supabase (the database).
#   All other files import functions from here — they never call Supabase directly.
#
#   This is called the "Service Layer" pattern.
#   Benefit: if you ever switch from Supabase to another database,
#            you only change THIS file — nothing else.
#
# THREE SERVICE CLASSES:
#   VerificationService  — save/read verification records
#   UsageLimitService    — check/increment daily usage limits
#   UserService          — read/update user profiles
#
# IMPORTANT — TWO SUPABASE CLIENTS:
#   supabase_admin  → uses SERVICE_ROLE_KEY → bypasses Row Level Security
#                     Used for: saving records, checking usage
#   supabase_user   → uses ANON_KEY          → respects Row Level Security
#                     Used for: reading user's own data (more secure)
#
# WHERE THIS FILE LIVES:
#   credibility-backend/app/services/database.py
# =============================================================================

from supabase import create_client, Client
from app.config import settings
from datetime import datetime, timezone, date
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# =============================================================================
# SUPABASE CLIENTS
# =============================================================================

def _make_admin_client() -> Client:
    """
    Admin client — uses SERVICE_ROLE_KEY.
    Full database access, bypasses all Row Level Security policies.
    NEVER expose this key to the browser.
    """
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY,
    )

def _make_anon_client() -> Client:
    """
    Anon client — uses ANON_KEY.
    Safe for user-scoped reads where RLS should be enforced.
    """
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_ANON_KEY,
    )

# Module-level singletons — created once, reused everywhere
_admin: Optional[Client] = None
_anon:  Optional[Client] = None

def get_admin() -> Client:
    global _admin
    if _admin is None:
        _admin = _make_admin_client()
    return _admin

def get_anon() -> Client:
    global _anon
    if _anon is None:
        _anon = _make_anon_client()
    return _anon


# =============================================================================
# VERIFICATION SERVICE
# =============================================================================

class VerificationService:
    """
    All database operations for verification records.

    The verification_history table stores one row per content check.
    Each row is linked to a user via user_id (their Supabase UUID).
    """

    def save(
        self,
        *,
        user_id:            str,       # ← from verified JWT (never from request body)
        input_text:         str,
        claims:             list,
        claims_total:       int,
        claims_verified:    int,
        claims_false:       int,
        claims_disputed:    int,
        claims_unverified:  int,
        credibility_score:  float,
        verdict:            str,
        verdict_label:      str,
        verdict_color:      str,
        summary:            str,
        flags:              list,
        confidence_level:   str,
        result_json:        dict,
        sources_consulted:  list,
        source_url:         Optional[str]  = None,
        content_type:       str            = "tweet",
        processing_time_ms: int            = 0,
        ip_hash:            str            = "",
    ) -> dict:
        """
        Save a verification result to the database.

        SECURITY NOTE:
            user_id must come from the verified JWT token (via get_current_user),
            NOT from the request body. This prevents users from saving records
            under another user's ID.

        Returns the saved record (including the database-generated `id`).
        """
        db = get_admin()   # admin client — needed to write to the table

        # Truncate the input text to fit the DB column (10,000 char limit)
        input_preview = input_text[:200] if len(input_text) > 200 else input_text

        record = {
            "user_id":              user_id,
            "input_text":           input_text[:10_000],
            "input_preview":        input_preview,
            "source_url":           source_url,
            "content_type":         content_type,
            "claims":               claims,              # stored as JSONB
            "claims_total":         claims_total,
            "claims_verified":      claims_verified,
            "claims_false":         claims_false,
            "claims_disputed":      claims_disputed,
            "claims_unverified":    claims_unverified,
            "credibility_score":    credibility_score,
            "verdict":              verdict,
            "verdict_label":        verdict_label,
            "verdict_color":        verdict_color,
            "summary":              summary,
            "flags":                flags,
            "confidence_level":     confidence_level,
            "result_json":          result_json,        # full engine output as JSONB
            "sources_consulted":    sources_consulted,
            "processing_time_ms":   processing_time_ms,
            "ip_hash":              ip_hash,
            "timestamp":            datetime.now(timezone.utc).isoformat(),
        }

        try:
            response = (
                db.table("verification_history")
                .insert(record)
                .execute()
            )
            saved = response.data[0] if response.data else record
            logger.info(
                f"[DB] Saved verification {saved.get('id')} "
                f"for user {user_id[:8]}… score={credibility_score}"
            )
            return saved
        except Exception as e:
            logger.error(f"[DB] Failed to save verification: {e}", exc_info=True)
            raise

    def get_by_id(self, verification_id: str, user_id: str) -> Optional[dict]:
        """
        Fetch a single verification by ID.
        user_id is checked to ensure users can only read their own records.
        """
        db = get_admin()
        response = (
            db.table("verification_history")
            .select("*")
            .eq("id", verification_id)
            .eq("user_id", user_id)        # ← security: user can't read others' records
            .single()
            .execute()
        )
        return response.data

    def get_history(
        self,
        user_id:  str,
        page:     int = 1,
        per_page: int = 20,
        verdict_filter: Optional[str] = None,
    ) -> dict:
        """
        Get paginated verification history for a user.
        Returns {items, total, page, page_size}.
        """
        db = get_admin()
        offset = (page - 1) * per_page

        query = (
            db.table("verification_history")
            .select(
                "id, credibility_score, verdict, verdict_label, verdict_color, "
                "content_type, input_preview, claims_total, claims_verified, "
                "flags, timestamp",
                count="exact",
            )
            .eq("user_id", user_id)
            .order("timestamp", desc=True)
            .range(offset, offset + per_page - 1)
        )

        if verdict_filter:
            query = query.eq("verdict", verdict_filter)

        response = query.execute()

        items = []
        for row in (response.data or []):
            items.append({
                "verification_id":   row["id"],
                "credibility_score": row.get("credibility_score") or 0.0,
                "verdict":           row.get("verdict") or "UNVERIFIED",
                "verdict_label":     row.get("verdict_label") or "Unverified",
                "verdict_color":     row.get("verdict_color") or "#6b7280",
                "content_type":      row.get("content_type") or "tweet",
                "content_preview":   (row.get("input_preview") or "")[:120],
                "claims_total":      row.get("claims_total") or 0,
                "claims_verified":   row.get("claims_verified") or 0,
                "flags":             row.get("flags") or [],
                "created_at":        row.get("timestamp") or "",
            })

        return {
            "items":     items,
            "total":     response.count or 0,
            "page":      page,
            "page_size": per_page,
        }


# =============================================================================
# USAGE LIMIT SERVICE
# =============================================================================

class UsageLimitService:
    """
    Manages daily verification limits per user.

    The usage_limits table has one row per (user_id, date).
    At the start of each new day the row is created fresh (or updated).

    check_and_increment() is the key method — it atomically:
      1. Reads current count
      2. Checks against the plan limit
      3. If allowed: increments count and returns {"allowed": True, ...}
      4. If blocked:  returns {"allowed": False, ...}
    """

    def check_and_increment(self, user_id: str) -> Optional[dict]:
        """
        Check if user is under their daily limit. If yes, increment and allow.
        If no, return not-allowed status.

        Uses the Supabase RPC function check_and_increment_usage() which
        handles the atomic read-check-increment in the database.
        This prevents race conditions (two requests at the same time).
        """
        db = get_admin()
        try:
            response = db.rpc(
                "check_and_increment_usage",
                {"p_user_id": user_id},
            ).execute()

            # The RPC returns a boolean: True = allowed, False = blocked
            if isinstance(response.data, bool):
                allowed = response.data
                status  = self.get_status(user_id)
                return {
                    "allowed":   allowed,
                    "used":      status.get("used",      0),
                    "limit":     status.get("limit",     10),
                    "remaining": status.get("remaining", 0),
                    "plan":      status.get("plan",      "free"),
                    "reset_at":  status.get("reset_at",  ""),
                }
            return None
        except Exception as e:
            logger.error(f"[DB] check_and_increment failed for {user_id}: {e}")
            return None   # fail open

    def get_status(self, user_id: str) -> dict:
        """
        Returns the user's current daily usage status.
        Called after check_and_increment to build the response.
        """
        db = get_admin()
        try:
            response = db.rpc(
                "get_usage_status",
                {"p_user_id": user_id},
            ).execute()

            if response.data and isinstance(response.data, dict):
                return response.data

            # Fallback: read the usage_limits table directly
            today = date.today().isoformat()
            row = (
                db.table("usage_limits")
                .select("requests_count, daily_limit, plan_snapshot, first_request_at")
                .eq("user_id", user_id)
                .eq("date", today)
                .maybe_single()
                .execute()
            )

            if row.data:
                used  = row.data["requests_count"]
                limit = row.data["daily_limit"]
                return {
                    "used":      used,
                    "limit":     limit,
                    "remaining": max(0, limit - used),
                    "allowed":   used < limit,
                    "plan":      row.data.get("plan_snapshot", "free"),
                    "reset_at":  _next_midnight_utc(),
                }

        except Exception as e:
            logger.error(f"[DB] get_usage_status failed for {user_id}: {e}")

        return {"used": 0, "limit": 10, "remaining": 10, "allowed": True, "plan": "free"}


# =============================================================================
# USER SERVICE
# =============================================================================

class UserService:
    """Read and update user profiles in the public.users table."""

    def get_by_id(self, user_id: str) -> Optional[dict]:
        db = get_admin()
        try:
            response = (
                db.table("users")
                .select("*")
                .eq("id", user_id)
                .maybe_single()
                .execute()
            )
            return response.data
        except Exception as e:
            logger.error(f"[DB] get_by_id failed: {e}")
            return None

    def update_profile(self, user_id: str, updates: dict) -> Optional[dict]:
        """Update allowed profile fields. Never lets callers update plan or user_id."""
        ALLOWED_FIELDS = {"full_name", "avatar_url", "phone"}
        safe_updates = {k: v for k, v in updates.items() if k in ALLOWED_FIELDS}

        if not safe_updates:
            return None

        db = get_admin()
        try:
            response = (
                db.table("users")
                .update(safe_updates)
                .eq("id", user_id)
                .execute()
            )
            return response.data[0] if response.data else None
        except Exception as e:
            logger.error(f"[DB] update_profile failed: {e}")
            return None

    def get_stats(self, user_id: str) -> dict:
        """Returns aggregated stats for the user's dashboard."""
        db = get_admin()
        try:
            response = (
                db.table("user_stats")   # view defined in schema
                .select("*")
                .eq("id", user_id)
                .maybe_single()
                .execute()
            )
            return response.data or {}
        except Exception as e:
            logger.error(f"[DB] get_stats failed: {e}")
            return {}


# =============================================================================
# MODULE-LEVEL SINGLETONS
# =============================================================================
# Import these in your routes:
#   from app.services.database import verifications_db, usage_db, users_db

verifications_db = VerificationService()
usage_db         = UsageLimitService()
users_db         = UserService()


# =============================================================================
# HELPER
# =============================================================================

def _next_midnight_utc() -> str:
    from datetime import timedelta
    now   = datetime.now(timezone.utc)
    reset = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return reset.isoformat()
