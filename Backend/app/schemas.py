# =============================================================================
# app/models/schemas.py  — Request/response shapes (updated with account cred.)
# =============================================================================

from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import List, Optional, Literal
from datetime import datetime


# ─────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: Optional[str] = None

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    full_name: Optional[str] = None

class GoogleOAuthRequest(BaseModel):
    code: str


# ─────────────────────────────────────────────────────────────
# ACCOUNT METADATA (sent by frontend, optional)
# ─────────────────────────────────────────────────────────────

class AccountMetadata(BaseModel):
    """
    Optional account/source info the frontend can send.
    The more fields provided, the better the account credibility analysis.

    For tweets: the frontend can extract username, follower_count, is_verified
    from the pasted URL or content using Twitter API or Puter.js.

    For articles: the source_url is usually enough to identify the domain.
    """
    username:            Optional[str]  = None
    display_name:        Optional[str]  = None
    is_verified:         bool           = False
    follower_count:      Optional[int]  = Field(default=None, ge=0)
    following_count:     Optional[int]  = Field(default=None, ge=0)
    account_age_days:    Optional[int]  = Field(default=None, ge=0)
    total_posts:         Optional[int]  = Field(default=None, ge=0)
    has_profile_picture: bool           = True
    has_bio:             bool           = True
    bio_text:            Optional[str]  = Field(default=None, max_length=500)

    model_config = {
        "json_schema_extra": {
            "example": {
                "username":         "@RealUser123",
                "is_verified":      False,
                "follower_count":   250,
                "account_age_days": 45,
                "has_profile_picture": True,
                "has_bio":          False,
            }
        }
    }


# ─────────────────────────────────────────────────────────────
# CLAIMS
# ─────────────────────────────────────────────────────────────

class ClaimInput(BaseModel):
    text: str = Field(min_length=5, max_length=1000)
    original_context: Optional[str] = None
    fact_check_status: Optional[Literal["VERIFIED", "DISPUTED", "DEBUNKED"]] = None


# ─────────────────────────────────────────────────────────────
# VERIFY REQUEST
# ─────────────────────────────────────────────────────────────

class VerifyRequest(BaseModel):
    """
    What the frontend sends to POST /verify/

    Required:
      original_content  — the text being checked
      claims            — extracted by Puter.js on the frontend

    Optional:
      source_url        — where the content came from
      content_type      — tweet | article | post | other
      account_metadata  — info about the account that posted it
    """
    original_content: str = Field(min_length=10, max_length=10_000)
    claims:           List[ClaimInput] = Field(min_length=1, max_length=20)
    source_url:       Optional[str] = None
    content_type:     Literal["tweet", "article", "post", "other"] = "tweet"
    account_metadata: Optional[AccountMetadata] = None

    @field_validator("original_content")
    @classmethod
    def strip_content(cls, v: str) -> str:
        return v.strip()

    model_config = {
        "json_schema_extra": {
            "example": {
                "original_content": "BREAKING!! Scientists CONFIRM vaccines cause autism — SHARE BEFORE DELETED!!",
                "claims": [{"text": "Scientists confirmed vaccines cause autism"}],
                "content_type": "tweet",
                "account_metadata": {
                    "username": "@BreakingNewsBot123",
                    "is_verified": False,
                    "follower_count": 52,
                    "account_age_days": 12,
                }
            }
        }
    }


# ─────────────────────────────────────────────────────────────
# VERIFY RESPONSE
# ─────────────────────────────────────────────────────────────

class FlagDetail(BaseModel):
    code:          str
    label:         str
    description:   str
    severity:      Literal["INFO", "WARNING", "DANGER"]
    score_penalty: float


class ClaimResult(BaseModel):
    text:               str
    status:             Literal["VERIFIED", "DISPUTED", "UNVERIFIED", "FALSE"]
    confidence:         float = Field(ge=0, le=100)
    evidence_summary:   str
    supporting_articles: List[str] = []
    sources_checked:    List[str]  = []
    fact_check_status:  Optional[str] = None


class SubScoreDetail(BaseModel):
    judge_name:   str
    raw_score:    float
    weight:       float
    contribution: float
    notes:        str


class AccountCredibilityDetail(BaseModel):
    """Full account analysis result — included in VerifyResponse."""
    account_credibility_score: float
    flags:             List[str]
    flag_details:      List[FlagDetail]
    domain_tier:       str
    source_type:       str
    analysis_note:     str
    data_completeness: str


class VerifyResponse(BaseModel):
    """
    Complete response from POST /verify.
    Everything the frontend needs to display the full analysis.
    """
    verification_id:    str

    # ── Overall score ─────────────────────────────────────────────────────────
    credibility_score:  float = Field(ge=0, le=100)
    verdict:            str
    verdict_label:      str
    verdict_color:      str

    # ── Content flags ─────────────────────────────────────────────────────────
    flags:              List[str]
    flag_details:       List[FlagDetail]

    # ── Summary ───────────────────────────────────────────────────────────────
    summary:            str
    confidence_level:   str     # HIGH | MEDIUM | LOW

    # ── Claims ────────────────────────────────────────────────────────────────
    claims_breakdown:   dict    # {total, verified, false, disputed, unverified}
    claim_results:      List[ClaimResult]

    # ── Account credibility ───────────────────────────────────────────────────
    account_credibility: Optional[AccountCredibilityDetail] = None

    # ── Sources ───────────────────────────────────────────────────────────────
    sources_consulted:  List[str]

    # ── Score breakdown (for transparency) ────────────────────────────────────
    penalty_total:      float
    sub_scores:         List[SubScoreDetail]

    # ── Rate limit info ───────────────────────────────────────────────────────
    usage: Optional[dict] = None   # {used, limit, remaining, plan}

    created_at:         str


# ─────────────────────────────────────────────────────────────
# HISTORY
# ─────────────────────────────────────────────────────────────

class HistoryItem(BaseModel):
    verification_id:   str
    credibility_score: float
    verdict:           str
    verdict_label:     str
    verdict_color:     str
    content_type:      str
    content_preview:   str
    claims_total:      int
    claims_verified:   int
    flags:             List[str] = []
    account_credibility_score: Optional[float] = None
    created_at:        str

class HistoryResponse(BaseModel):
    items:     List[HistoryItem]
    total:     int
    page:      int
    page_size: int


# ─────────────────────────────────────────────────────────────
# USER
# ─────────────────────────────────────────────────────────────

class UserProfile(BaseModel):
    user_id:              str
    email:                str
    full_name:            Optional[str] = None
    avatar_url:           Optional[str] = None
    plan:                 str = "free"
    total_verifications:  int = 0
    joined_at:            str

class UpdateProfileRequest(BaseModel):
    full_name:  Optional[str] = Field(default=None, max_length=120)
    avatar_url: Optional[str] = None
