# =============================================================================
# app/services/usage_service.py — Daily Usage Tracking + Verification History
# =============================================================================
#
# PLAIN ENGLISH — What this file does:
#   This is the "database librarian" for two tables:
#
#   1. verification_history  — saves every check ever run
#   2. usage_tracking        — counts how many checks a user runs per day
#
# TWO CLASSES:
#   VerificationHistoryService  → insert and read from verification_history
#   UsageTrackingService        → check limits and count from usage_tracking
#
# HOW IT'S USED:
#   The /verify route imports this file.
#   Before processing: calls UsageTrackingService.check_and_increment()
#   After processing:  calls VerificationHistoryService.save()
#
# WHERE THIS FILE LIVES:
#   credibility-backend/app/services/usage_service.py
# =============================================================================

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from supabase import create_client, Client
from app.config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# SUPABASE CLIENT (admin — bypasses Row Level Security)
# ─────────────────────────────────────────────────────────────────────────────
# We use the service_role key here because:
#   • We need to insert/update usage rows on behalf of ANY user
#   • RLS policies would block writes if we used the anon key
#
# IMPORTANT: This key must NEVER appear in frontend code.
# It lives only in the backend .env file.

_admin_client: Optional[Client] = None


def _get_client() -> Client:
    """Returns the shared admin Supabase client (created once, reused)."""
    global _admin_client
    if _admin_client is None:
        _admin_client = create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_ROLE_KEY,
        )
    return _admin_client


# =============================================================================
# CLASS 1: UsageTrackingService
# =============================================================================
#
# Manages the usage_tracking table.
# One row per (user_id, date). Auto-resets each calendar day.

class UsageTrackingService:
    """
    Handles daily verification limits.

    The usage_tracking table has one row per (user_id, date).
    The UNIQUE constraint on (user_id, date) means the database
    automatically creates a fresh row on a new day — no cron job needed.

    Key method: check_and_increment()
      → reads today's count, checks against limit, increments if allowed
      → all in one database transaction (prevents race conditions)
    """

    # ── MAIN METHOD: called before every verification ─────────────────────────

    def check_and_increment(
        self,
        user_id:     str,
        daily_limit: int = 10,
    ) -> dict:
        """
        Check if the user is under their daily limit.
        If yes: increment their count and return {"allowed": True, ...}
        If no:  return {"allowed": False, ...} — do NOT increment

        This calls the increment_usage_count() SQL function which uses
        a row-level lock to prevent two simultaneous requests from
        both slipping through when the count is at 9.

        Args:
            user_id:     The user's Supabase UUID (from the verified JWT)
            daily_limit: Maximum verifications per day for this user's plan

        Returns:
            {
              "allowed":      True/False,
              "search_count": 7,          ← their count AFTER this call (if allowed)
              "daily_limit":  10,
              "remaining":    3,
              "date":         "2025-01-14",
              "resets_at":    "2025-01-15T00:00:00+00:00"
            }
        """
        db = _get_client()

        try:
            # Call the atomic SQL function we defined in the schema.
            # It does: check count → if under limit, increment → return bool.
            # Using a database function prevents race conditions that
            # pure Python code would be vulnerable to.
            response = db.rpc(
                "increment_usage_count",
                {
                    "p_user_id":    user_id,
                    "p_daily_limit": daily_limit,
                },
            ).execute()

            allowed = bool(response.data)   # True = allowed, False = blocked

            # Read the current count so we can include it in the response
            current = self.get_today_count(user_id)
            count   = current.get("search_count", 0)

            if not allowed:
                # Limit reached — count is at or above the limit
                logger.info(
                    f"[UsageTracking] LIMIT REACHED: "
                    f"user={user_id[:8]}... count={count}/{daily_limit}"
                )
            else:
                logger.info(
                    f"[UsageTracking] Allowed: "
                    f"user={user_id[:8]}... count={count}/{daily_limit}"
                )

            return {
                "allowed":      allowed,
                "search_count": count,
                "daily_limit":  daily_limit,
                "remaining":    max(0, daily_limit - count),
                "date":         date.today().isoformat(),
                "resets_at":    _next_midnight_utc(),
            }

        except Exception as exc:
            # If the database call fails, we fail OPEN (allow the request)
            # so database issues don't lock out users.
            # Log the error so you can investigate later.
            logger.error(
                f"[UsageTracking] check_and_increment failed for user "
                f"{user_id[:8]}...: {exc}",
                exc_info=True,
            )
            return {
                "allowed":      True,    # fail open — don't block users due to DB issues
                "search_count": 0,
                "daily_limit":  daily_limit,
                "remaining":    daily_limit,
                "date":         date.today().isoformat(),
                "resets_at":    _next_midnight_utc(),
                "error":        "usage_check_failed",
            }

    # ── READ: today's count for a user ───────────────────────────────────────

    def get_today_count(self, user_id: str) -> dict:
        """
        Read the current day's usage row for this user.

        Returns:
            {
              "search_count": 7,
              "date": "2025-01-14",
              "updated_at": "2025-01-14T10:30:00+00:00"
            }
        Or an empty dict {} if no row exists yet (user hasn't verified today).
        """
        db = _get_client()
        today = date.today().isoformat()

        try:
            response = (
                db.table("usage_tracking")
                .select("search_count, date, updated_at")
                .eq("user_id", user_id)
                .eq("date", today)
                .maybe_single()    # returns None instead of raising if no row
                .execute()
            )

            if response.data:
                return {
                    "search_count": response.data["search_count"],
                    "date":         response.data["date"],
                    "updated_at":   response.data.get("updated_at"),
                }
            # No row yet = user hasn't run any verifications today
            return {"search_count": 0, "date": today}

        except Exception as exc:
            logger.error(f"[UsageTracking] get_today_count failed: {exc}")
            return {"search_count": 0, "date": today}

    # ── READ: full status object (for the frontend usage bar) ────────────────

    def get_status(self, user_id: str, daily_limit: int = 10) -> dict:
        """
        Build the full usage status object.
        This is what the /verify response includes so the frontend
        can display the "X of 10 verifications used today" indicator.

        Returns:
            {
              "search_count":  7,
              "daily_limit":   10,
              "remaining":     3,
              "date":          "2025-01-14",
              "resets_at":     "2025-01-15T00:00:00+00:00",
              "limit_reached": false
            }
        """
        today_data   = self.get_today_count(user_id)
        search_count = today_data.get("search_count", 0)
        remaining    = max(0, daily_limit - search_count)

        return {
            "search_count":  search_count,
            "daily_limit":   daily_limit,
            "remaining":     remaining,
            "date":          date.today().isoformat(),
            "resets_at":     _next_midnight_utc(),
            "limit_reached": search_count >= daily_limit,
        }

    # ── READ: history of daily counts (for analytics dashboard) ──────────────

    def get_history(
        self,
        user_id:  str,
        days:     int = 30,
    ) -> list[dict]:
        """
        Return the last N days of usage counts for a user.
        Useful for building a usage history chart in the dashboard.

        Returns a list of dicts sorted newest-first:
            [
              {"date": "2025-01-14", "search_count": 7},
              {"date": "2025-01-13", "search_count": 10},
              ...
            ]
        """
        db = _get_client()

        try:
            response = (
                db.table("usage_tracking")
                .select("date, search_count, updated_at")
                .eq("user_id", user_id)
                .order("date", desc=True)
                .limit(days)
                .execute()
            )
            return response.data or []
        except Exception as exc:
            logger.error(f"[UsageTracking] get_history failed: {exc}")
            return []


# =============================================================================
# CLASS 2: VerificationHistoryService
# =============================================================================
#
# Manages the verification_history table.
# One row per verification ever run, stored permanently.

class VerificationHistoryService:
    """
    Saves and retrieves verification records.

    Every successful verification gets a row in this table.
    This is the permanent record — it's never deleted.
    """

    # ── WRITE: save a new verification result ────────────────────────────────

    def save(
        self,
        *,
        user_id:                    str,
        input_text:                 str,
        claims:                     list,
        credibility_score:          float,
        result_json:                dict,
        account_credibility_score:  Optional[float] = None,
    ) -> dict:
        """
        Insert a new row into verification_history.

        SECURITY: user_id must come from the verified JWT token,
        NOT from the request body. The caller (verify.py) is responsible
        for this — user_id = current_user["sub"].

        Args:
            user_id:                   UUID from verified Supabase JWT
            input_text:                Raw text the user pasted (max 10,000 chars)
            claims:                    List of claim dicts from Puter.js
            credibility_score:         Final content score (0-100)
            account_credibility_score: Source trust score (0-100) or None
            result_json:               Complete engine output dict

        Returns:
            The saved row dict (includes the auto-generated id and timestamp)

        Raises:
            Exception: if the database insert fails
        """
        db  = _get_client()
        now = datetime.now(timezone.utc).isoformat()

        record = {
            "user_id":                   user_id,
            "input_text":                input_text[:10_000],   # hard cap
            "claims":                    claims,                 # stored as JSONB
            "credibility_score":         round(credibility_score, 2),
            "account_credibility_score": round(account_credibility_score, 2)
                                         if account_credibility_score is not None
                                         else None,
            "result_json":               result_json,           # stored as JSONB
            "timestamp":                 now,
        }

        try:
            response = (
                db.table("verification_history")
                .insert(record)
                .execute()
            )

            saved = response.data[0] if response.data else record
            logger.info(
                f"[VerificationHistory] Saved id={saved.get('id', 'unknown')} "
                f"user={user_id[:8]}... score={credibility_score:.1f}"
            )
            return saved

        except Exception as exc:
            logger.error(
                f"[VerificationHistory] save() failed for user "
                f"{user_id[:8]}...: {exc}",
                exc_info=True,
            )
            raise   # re-raise so the caller can handle it

    # ── READ: get a user's verification history ───────────────────────────────

    def get_by_user(
        self,
        user_id:  str,
        page:     int = 1,
        per_page: int = 20,
        date_filter: Optional[str] = None,   # "YYYY-MM-DD"
    ) -> dict:
        """
        Fetch paginated verification history for a user.

        Args:
            user_id:     The user's UUID
            page:        Page number (starts at 1)
            per_page:    Results per page (max 100)
            date_filter: Optional date string "YYYY-MM-DD" to filter by day

        Returns:
            {
              "items": [...],
              "total": 42,
              "page":  1,
              "per_page": 20
            }
        """
        db     = _get_client()
        offset = (page - 1) * per_page

        try:
            query = (
                db.table("verification_history")
                .select(
                    "id, input_text, credibility_score, account_credibility_score, "
                    "result_json, claims, timestamp",
                    count="exact",
                )
                .eq("user_id", user_id)
                .order("timestamp", desc=True)
                .range(offset, offset + per_page - 1)
            )

            # Optional: filter to a specific calendar date
            if date_filter:
                # timestamp is a timestamptz; filter rows where the date matches
                query = (
                    query
                    .gte("timestamp", f"{date_filter}T00:00:00+00:00")
                    .lt("timestamp",  f"{date_filter}T23:59:59+00:00")
                )

            response = query.execute()

            return {
                "items":    response.data or [],
                "total":    response.count or 0,
                "page":     page,
                "per_page": per_page,
            }

        except Exception as exc:
            logger.error(f"[VerificationHistory] get_by_user failed: {exc}")
            return {"items": [], "total": 0, "page": page, "per_page": per_page}

    # ── READ: get a single record by ID ──────────────────────────────────────

    def get_by_id(self, record_id: str, user_id: str) -> Optional[dict]:
        """
        Fetch a single verification record.
        user_id check ensures users can only read their own records.
        """
        db = _get_client()

        try:
            response = (
                db.table("verification_history")
                .select("*")
                .eq("id",      record_id)
                .eq("user_id", user_id)      # security: must be the owner
                .maybe_single()
                .execute()
            )
            return response.data
        except Exception as exc:
            logger.error(f"[VerificationHistory] get_by_id failed: {exc}")
            return None

    # ── READ: get all history for a specific date ─────────────────────────────

    def get_by_date(self, user_id: str, target_date: str) -> list[dict]:
        """
        Fetch all verifications a user ran on a specific date.

        Args:
            user_id:     The user's UUID
            target_date: Date string "YYYY-MM-DD"

        Returns:
            List of verification records for that date
        """
        db = _get_client()

        try:
            response = (
                db.table("verification_history")
                .select(
                    "id, input_text, credibility_score, "
                    "account_credibility_score, result_json, timestamp"
                )
                .eq("user_id", user_id)
                .gte("timestamp", f"{target_date}T00:00:00+00:00")
                .lt("timestamp",  f"{target_date}T23:59:59+00:00")
                .order("timestamp", desc=True)
                .execute()
            )
            return response.data or []
        except Exception as exc:
            logger.error(f"[VerificationHistory] get_by_date failed: {exc}")
            return []


# =============================================================================
# MODULE-LEVEL SINGLETONS
# =============================================================================
# Import these in your routes — don't instantiate the classes yourself.
#
# Usage:
#   from app.services.usage_service import usage_tracker, verification_history
#
#   # Check limit
#   status = usage_tracker.check_and_increment(user_id, daily_limit=10)
#
#   # Save result
#   verification_history.save(user_id=user_id, input_text=..., ...)

usage_tracker        = UsageTrackingService()
verification_history = VerificationHistoryService()


# =============================================================================
# HELPERS
# =============================================================================

def _next_midnight_utc() -> str:
    """Returns the next midnight UTC as an ISO 8601 string."""
    from datetime import timedelta
    now   = datetime.now(timezone.utc)
    reset = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return reset.isoformat()
