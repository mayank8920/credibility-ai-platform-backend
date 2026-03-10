# =============================================================================
# app/services/account_credibility.py
# =============================================================================
# ACCOUNT CREDIBILITY ANALYSIS ENGINE
# ─────────────────────────────────────
# Plain-English:
#   Before we score WHAT someone said, we also look at WHO said it.
#   A claim from Reuters.com should be trusted more than a claim from
#   a brand-new Twitter account with 12 followers.
#
#   This module analyses the ACCOUNT or SOURCE that posted the content
#   and produces:
#     • account_credibility_score  (0–100)
#     • flags  (e.g. LOW_FOLLOWER_COUNT, NEW_ACCOUNT, UNVERIFIED_SOURCE)
#
#   This score is then blended with the content score by the verify route.
#
# WHAT IT ANALYSES:
#   1. Account age       — new accounts are less trusted
#   2. Follower count    — very low = suspicious
#   3. Verification badge — verified accounts get a bonus
#   4. Domain reputation  — known news sites score higher
#   5. Historical signals — patterns in the submitted URL/username
#
# NOTE ON DATA SOURCES:
#   In MVP mode (no Twitter/social API keys), this module works from:
#     • The URL/domain of the content (extracted from source_url)
#     • The username/handle if present in the text
#     • Optional metadata the frontend can send (follower_count, etc.)
#   With Twitter API keys (Phase 2), live data replaces estimates.
#
# WHERE THIS FILE LIVES:
#   credibility-backend/app/services/account_credibility.py
# =============================================================================

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1 — FLAG DEFINITIONS FOR ACCOUNT ANALYSIS
# =============================================================================

ACCOUNT_FLAGS = {
    # ── Danger flags ─────────────────────────────────────────────────────────
    "NEW_ACCOUNT": {
        "label":       "New Account",
        "description": "This account was created recently. New accounts are more commonly used for coordinated misinformation.",
        "severity":    "DANGER",
        "penalty":     20.0,
    },
    "NO_FOLLOWER_DATA": {
        "label":       "No Follower Data",
        "description": "Could not verify follower count for this account.",
        "severity":    "WARNING",
        "penalty":     5.0,
    },
    "LOW_FOLLOWER_COUNT": {
        "label":       "Low Follower Count",
        "description": "Account has very few followers, reducing its credibility signal.",
        "severity":    "DANGER",
        "penalty":     15.0,
    },
    "UNVERIFIED_SOURCE": {
        "label":       "Unverified Source",
        "description": "The account or domain is not verified by the platform or any trusted registry.",
        "severity":    "WARNING",
        "penalty":     10.0,
    },
    "ANONYMOUS_ACCOUNT": {
        "label":       "Anonymous Account",
        "description": "The account appears to be anonymous with no identifiable owner.",
        "severity":    "DANGER",
        "penalty":     18.0,
    },
    "SUSPICIOUS_DOMAIN": {
        "label":       "Suspicious Domain",
        "description": "The domain matches patterns associated with misinformation sites.",
        "severity":    "DANGER",
        "penalty":     25.0,
    },
    "KNOWN_MISINFORMATION_HISTORY": {
        "label":       "Known Misinformation History",
        "description": "This domain or account has a documented history of sharing false information.",
        "severity":    "DANGER",
        "penalty":     30.0,
    },

    # ── Warning flags ─────────────────────────────────────────────────────────
    "NO_PROFILE_PICTURE": {
        "label":       "No Profile Picture",
        "description": "Accounts without profile pictures are statistically more likely to be bots.",
        "severity":    "WARNING",
        "penalty":     8.0,
    },
    "GENERIC_USERNAME": {
        "label":       "Generic Username",
        "description": "Username matches patterns of bot or auto-generated accounts (e.g. User123456).",
        "severity":    "WARNING",
        "penalty":     6.0,
    },
    "NO_BIO": {
        "label":       "No Bio / Description",
        "description": "Account has no bio, which is common for bot or spam accounts.",
        "severity":    "WARNING",
        "penalty":     4.0,
    },
    "DOMAIN_AGE_UNKNOWN": {
        "label":       "Domain Age Unknown",
        "description": "Cannot verify how long this domain has existed.",
        "severity":    "WARNING",
        "penalty":     5.0,
    },
    "PARODY_ACCOUNT": {
        "label":       "Parody Account",
        "description": "Account appears to be a parody. Content may not reflect real events.",
        "severity":    "WARNING",
        "penalty":     12.0,
    },

    # ── Positive / trust flags ────────────────────────────────────────────────
    "VERIFIED_ACCOUNT": {
        "label":       "Verified Account ✓",
        "description": "This account has a verification badge from the platform.",
        "severity":    "INFO",
        "penalty":     0.0,    # no penalty — handled as a bonus
    },
    "ESTABLISHED_DOMAIN": {
        "label":       "Established Domain",
        "description": "This domain has a long track record and is widely recognized.",
        "severity":    "INFO",
        "penalty":     0.0,
    },
    "TIER1_NEWS_SOURCE": {
        "label":       "Tier-1 News Source",
        "description": "Content comes from a globally recognised, editorially independent news outlet.",
        "severity":    "INFO",
        "penalty":     0.0,
    },
    "HIGH_FOLLOWER_COUNT": {
        "label":       "High Follower Count",
        "description": "Account has a substantial following, increasing its credibility signal.",
        "severity":    "INFO",
        "penalty":     0.0,
    },
}


# =============================================================================
# SECTION 2 — DOMAIN DATABASES
# =============================================================================
# These lists are used for pattern matching when no live API data is available.

# Tier-1: globally trusted news and institutional sources
TIER1_DOMAINS = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk",
    "nytimes.com", "theguardian.com", "washingtonpost.com",
    "bloomberg.com", "npr.org", "pbs.org", "abc.net.au",
    "ft.com", "economist.com", "theatlantic.com",
    "who.int", "cdc.gov", "nasa.gov", "nih.gov",
    "gov.uk", "europa.eu", "un.org",
    "politifact.com", "factcheck.org", "snopes.com", "fullfact.org",
    "nature.com", "science.org", "pubmed.ncbi.nlm.nih.gov",
    "ap.org", "afp.com",
}

# Tier-2: generally reliable regional and digital outlets
TIER2_DOMAINS = {
    "cnn.com", "foxnews.com", "msnbc.com", "nbcnews.com", "cbsnews.com",
    "usatoday.com", "newsweek.com", "time.com", "forbes.com",
    "businessinsider.com", "huffpost.com", "vox.com",
    "independent.co.uk", "telegraph.co.uk", "dailymail.co.uk",
    "aljazeera.com", "dw.com", "france24.com", "rfi.fr",
    "thehindu.com", "hindustantimes.com", "indiatoday.in",
    "smh.com.au", "news.com.au", "theage.com.au",
    "globo.com", "lemonde.fr", "spiegel.de",
}

# Known misinformation / unreliable domains
# Source: consolidated from MBFC, NewsGuard, and academic research
KNOWN_MISINFORMATION_DOMAINS = {
    "infowars.com", "naturalnews.com", "beforeitsnews.com",
    "worldnewsdailyreport.com", "nationalreport.net",
    "empirenews.net", "theonion.com",   # satire — parody, not malicious
    "clickhole.com",                    # satire
    "superstation95.com", "yournewswire.com",
    "newspunch.com", "bigleaguepolitics.com",
    "westernjournal.com", "thegatewaypundit.com",
    "zerohedge.com",
    "globalresearch.ca", "veteranstoday.com",
    "sputniknews.com", "rt.com",        # state-sponsored propaganda
    "oann.com",
}

# Domains that are satire — not malicious but content isn't real news
SATIRE_DOMAINS = {
    "theonion.com", "clickhole.com", "babylonbee.com",
    "thebeaverton.com", "newsthump.com", "thespoof.com",
    "waterfordwhispersnews.com",
}

# Suspicious domain patterns (regex)
SUSPICIOUS_DOMAIN_PATTERNS = [
    r"breaking[\-_]?news",
    r"real[\-_]?truth",
    r"truth[\-_]?news",
    r"patriots?[\-_]?(news|daily|report)",
    r"conservative[\-_]?(news|daily)",
    r"liberal[\-_]?(news|daily)",
    r"(news|report)[\-_]?24",
    r"worldstar",
    r"viral(news|truth|facts)",
    r"\d{4,}(news|report)",     # random numbers in domain
]

# Bot/auto-generated username patterns
BOT_USERNAME_PATTERNS = [
    r"^[a-z]+\d{5,}$",          # letters followed by 5+ numbers: user12345
    r"^[A-Z][a-z]+\d{4,}$",    # Name followed by numbers: Alice1234
    r"^(user|account|profile)\d+$",
    r"^[a-z]{2,4}\d{6,}$",     # short prefix + many numbers
    r"^[A-Za-z]+_[A-Za-z]+\d{4,}$",  # Name_Surname1234
]

# Parody account signals in username/bio
PARODY_SIGNALS = [
    "parody", "satire", "not the real", "unofficial",
    "fan account", "parody account",
]


# =============================================================================
# SECTION 3 — INPUT / OUTPUT DATA STRUCTURES
# =============================================================================

@dataclass
class AccountInput:
    """
    All known information about the account/source that posted the content.

    The frontend can provide as much or as little as it knows.
    The more data provided, the more accurate the analysis.
    Fields not provided are estimated or given a neutral score.
    """
    # ── From URL ──────────────────────────────────────────────────────────────
    source_url:          Optional[str]  = None    # e.g. "https://twitter.com/user/status/123"
    domain:              Optional[str]  = None    # e.g. "reuters.com" (auto-extracted from URL)

    # ── From Twitter/social API (optional — Phase 2) ─────────────────────────
    username:            Optional[str]  = None    # e.g. "@BreakingNews"
    display_name:        Optional[str]  = None    # e.g. "Breaking News"
    is_verified:         bool           = False   # Blue/gold checkmark
    follower_count:      Optional[int]  = None    # Number of followers
    following_count:     Optional[int]  = None
    account_age_days:    Optional[int]  = None    # How old is the account?
    total_posts:         Optional[int]  = None    # Total tweets/posts
    has_profile_picture: bool           = True    # False = likely bot
    has_bio:             bool           = True    # False = likely bot
    bio_text:            Optional[str]  = None    # Account description

    # ── Content type ──────────────────────────────────────────────────────────
    content_type: str = "tweet"     # tweet | article | post | other


@dataclass
class AccountCredibilityResult:
    """
    Output from the account credibility analysis.
    This is included in the final VerifyResponse.
    """
    account_credibility_score: float           # 0–100
    flags:                     list[str]       # Flag codes
    flag_details:              list[dict]      # Full flag objects
    domain_tier:               str             # TIER1 | TIER2 | UNKNOWN | SUSPICIOUS
    source_type:               str             # news_outlet | social_media | blog | unknown
    analysis_note:             str             # Plain-English summary
    data_completeness:         str             # FULL | PARTIAL | MINIMAL


# =============================================================================
# SECTION 4 — MAIN ANALYSIS FUNCTION
# =============================================================================

def analyse_account(inp: AccountInput) -> AccountCredibilityResult:
    """
    THE MAIN FUNCTION — call this from the verify route.

    Takes an AccountInput (whatever data we have about the source),
    runs all signal checks, and returns an AccountCredibilityResult.

    Example:
        result = analyse_account(AccountInput(
            source_url="https://infowars.com/article/123",
            is_verified=False,
            follower_count=500,
        ))
        # result.account_credibility_score → ~18
        # result.flags → ["SUSPICIOUS_DOMAIN", "KNOWN_MISINFORMATION_HISTORY"]
    """
    logger.info(f"[AccountCredibility] Analysing: url={inp.source_url} user={inp.username}")

    triggered_flags: list[str] = []
    score = 50.0    # neutral baseline

    # ── Auto-extract domain from URL if not provided ──────────────────────
    domain = inp.domain
    if not domain and inp.source_url:
        domain = _extract_domain(inp.source_url)

    # ── Determine source type ─────────────────────────────────────────────
    source_type = _detect_source_type(inp.source_url, inp.content_type)

    # ── Signal 1: Domain reputation ───────────────────────────────────────
    domain_tier, domain_flags, domain_adj = _analyse_domain(domain)
    triggered_flags.extend(domain_flags)
    score += domain_adj

    # ── Signal 2: Verification status ─────────────────────────────────────
    if inp.is_verified:
        score += 15.0
        triggered_flags.append("VERIFIED_ACCOUNT")
    elif source_type in ("social_media",) and domain_tier == "UNKNOWN":
        score -= 5.0
        triggered_flags.append("UNVERIFIED_SOURCE")

    # ── Signal 3: Follower / audience size ────────────────────────────────
    if inp.follower_count is not None:
        follower_flags, follower_adj = _analyse_follower_count(inp.follower_count)
        triggered_flags.extend(follower_flags)
        score += follower_adj
    elif source_type == "social_media":
        # No follower data for a social media account — suspicious
        triggered_flags.append("NO_FOLLOWER_DATA")
        score -= 5.0

    # ── Signal 4: Account age ─────────────────────────────────────────────
    if inp.account_age_days is not None:
        age_flags, age_adj = _analyse_account_age(inp.account_age_days)
        triggered_flags.extend(age_flags)
        score += age_adj

    # ── Signal 5: Profile completeness signals ────────────────────────────
    if not inp.has_profile_picture:
        triggered_flags.append("NO_PROFILE_PICTURE")
        score -= 8.0

    if not inp.has_bio:
        triggered_flags.append("NO_BIO")
        score -= 4.0

    # ── Signal 6: Username pattern analysis ──────────────────────────────
    if inp.username:
        username_flags, username_adj = _analyse_username(inp.username, inp.bio_text)
        triggered_flags.extend(username_flags)
        score += username_adj

    # ── Clamp score to 0–100 ──────────────────────────────────────────────
    score = max(0.0, min(100.0, score))

    # ── Determine data completeness ───────────────────────────────────────
    data_completeness = _compute_data_completeness(inp)

    # ── Build flag detail objects ─────────────────────────────────────────
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_flags = [f for f in triggered_flags if not (f in seen or seen.add(f))]   # type: ignore

    flag_details = [
        {
            "code":         code,
            "label":        ACCOUNT_FLAGS[code]["label"],
            "description":  ACCOUNT_FLAGS[code]["description"],
            "severity":     ACCOUNT_FLAGS[code]["severity"],
            "score_penalty": ACCOUNT_FLAGS[code]["penalty"],
        }
        for code in unique_flags
        if code in ACCOUNT_FLAGS
    ]

    # ── Write analysis note ───────────────────────────────────────────────
    analysis_note = _write_analysis_note(
        score, domain, domain_tier, source_type, unique_flags
    )

    logger.info(
        f"[AccountCredibility] Done. score={score:.1f} "
        f"tier={domain_tier} flags={unique_flags}"
    )

    return AccountCredibilityResult(
        account_credibility_score = round(score, 1),
        flags                     = unique_flags,
        flag_details              = flag_details,
        domain_tier               = domain_tier,
        source_type               = source_type,
        analysis_note             = analysis_note,
        data_completeness         = data_completeness,
    )


# =============================================================================
# SECTION 5 — SIGNAL ANALYSERS (helpers)
# =============================================================================

def _extract_domain(url: str) -> str:
    """
    Extracts the domain from a URL.
    'https://www.reuters.com/article/123' → 'reuters.com'
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        # Remove www. prefix
        if domain.startswith("www."):
            domain = domain[4:]
        # Remove port if present
        domain = domain.split(":")[0]
        return domain
    except Exception:
        return ""


def _detect_source_type(url: Optional[str], content_type: str) -> str:
    """
    Determines what kind of source this is.
    Returns: 'news_outlet' | 'social_media' | 'blog' | 'unknown'
    """
    if not url:
        return "social_media" if content_type == "tweet" else "unknown"

    domain = _extract_domain(url)

    SOCIAL_DOMAINS = {"twitter.com", "x.com", "facebook.com", "instagram.com",
                      "tiktok.com", "reddit.com", "linkedin.com", "threads.net"}

    BLOG_PLATFORMS  = {"medium.com", "substack.com", "wordpress.com",
                       "blogspot.com", "tumblr.com"}

    if domain in TIER1_DOMAINS or domain in TIER2_DOMAINS:
        return "news_outlet"
    if domain in SOCIAL_DOMAINS:
        return "social_media"
    if domain in BLOG_PLATFORMS or "blog" in domain:
        return "blog"
    return "unknown"


def _analyse_domain(domain: Optional[str]) -> tuple[str, list[str], float]:
    """
    Scores the domain.
    Returns: (tier, flags, score_adjustment)
    """
    if not domain:
        return "UNKNOWN", ["DOMAIN_AGE_UNKNOWN"], -5.0

    domain_lower = domain.lower()

    # Check satire first (before misinformation — satire isn't malicious)
    if domain_lower in SATIRE_DOMAINS:
        return "SUSPICIOUS", ["PARODY_ACCOUNT"], -12.0

    # Check known misinformation sites
    if domain_lower in KNOWN_MISINFORMATION_DOMAINS:
        return "SUSPICIOUS", ["SUSPICIOUS_DOMAIN", "KNOWN_MISINFORMATION_HISTORY"], -40.0

    # Check suspicious patterns
    for pattern in SUSPICIOUS_DOMAIN_PATTERNS:
        if re.search(pattern, domain_lower):
            return "SUSPICIOUS", ["SUSPICIOUS_DOMAIN"], -20.0

    # Tier-1: globally trusted sources
    if domain_lower in TIER1_DOMAINS:
        return "TIER1", ["TIER1_NEWS_SOURCE", "ESTABLISHED_DOMAIN"], +35.0

    # Tier-2: generally reliable sources
    if domain_lower in TIER2_DOMAINS:
        return "TIER2", ["ESTABLISHED_DOMAIN"], +15.0

    # Unknown domain — neutral, slight penalty for lack of recognition
    return "UNKNOWN", [], -3.0


def _analyse_follower_count(followers: int) -> tuple[list[str], float]:
    """
    Scores the account's follower count.

    Thresholds based on research into influence and account authenticity:
      < 100      → very suspicious (bot or brand-new account)
      100–999    → low (minor personal account)
      1K–9.9K    → moderate (small but legitimate)
      10K–99K    → good (established voice)
      100K+      → high (major account)
    """
    if followers < 50:
        return ["LOW_FOLLOWER_COUNT"], -20.0
    elif followers < 500:
        return ["LOW_FOLLOWER_COUNT"], -12.0
    elif followers < 1_000:
        return [], -5.0
    elif followers < 10_000:
        return [], 0.0     # neutral
    elif followers < 100_000:
        return [], +8.0
    else:
        return ["HIGH_FOLLOWER_COUNT"], +18.0


def _analyse_account_age(age_days: int) -> tuple[list[str], float]:
    """
    Scores how long the account has existed.

    Thresholds:
      < 30 days  → brand new — high risk
      30–180 days → young account — moderate risk
      180–365 days → established — slight bonus
      365+ days  → mature — full bonus
    """
    if age_days < 30:
        return ["NEW_ACCOUNT"], -25.0
    elif age_days < 90:
        return ["NEW_ACCOUNT"], -15.0
    elif age_days < 180:
        return [], -5.0
    elif age_days < 365:
        return [], +5.0
    else:
        return [], +10.0


def _analyse_username(username: str, bio: Optional[str]) -> tuple[list[str], float]:
    """
    Checks username and bio for bot/parody patterns.
    """
    flags: list[str] = []
    adj = 0.0

    clean_username = username.lstrip("@").strip()

    # Check for bot-like username patterns
    for pattern in BOT_USERNAME_PATTERNS:
        if re.match(pattern, clean_username):
            flags.append("GENERIC_USERNAME")
            adj -= 6.0
            break

    # Check for parody signals in username or bio
    combined_text = (clean_username + " " + (bio or "")).lower()
    if any(signal in combined_text for signal in PARODY_SIGNALS):
        flags.append("PARODY_ACCOUNT")
        adj -= 12.0

    return flags, adj


def _compute_data_completeness(inp: AccountInput) -> str:
    """
    Returns how much data we have about the account.
    FULL = we have live API data
    PARTIAL = we have some signals (URL, username)
    MINIMAL = we have very little (no URL, no username)
    """
    score = 0
    if inp.source_url:         score += 2
    if inp.username:           score += 1
    if inp.follower_count is not None: score += 2
    if inp.account_age_days is not None: score += 2
    if inp.is_verified:        score += 1
    if inp.bio_text:           score += 1

    if score >= 7: return "FULL"
    if score >= 3: return "PARTIAL"
    return "MINIMAL"


def _write_analysis_note(
    score: float,
    domain: Optional[str],
    domain_tier: str,
    source_type: str,
    flags: list[str],
) -> str:
    """Writes a plain-English explanation of the account analysis."""
    parts = []

    if domain_tier == "TIER1":
        parts.append(f"Content from Tier-1 trusted source ({domain}) significantly boosts credibility.")
    elif domain_tier == "TIER2":
        parts.append(f"Content from recognised outlet ({domain}) adds credibility.")
    elif domain_tier == "SUSPICIOUS":
        if "KNOWN_MISINFORMATION_HISTORY" in flags:
            parts.append(f"⚠️ Domain ({domain}) has a documented history of publishing false information.")
        else:
            parts.append(f"⚠️ Domain ({domain}) matches patterns of unreliable sources.")
    else:
        parts.append(f"Source domain ({domain or 'unknown'}) could not be verified in trusted source lists.")

    danger_flags = [f for f in flags if ACCOUNT_FLAGS.get(f, {}).get("severity") == "DANGER"]
    if danger_flags:
        flag_labels = [ACCOUNT_FLAGS[f]["label"] for f in danger_flags[:2]]
        parts.append(f"Account concerns: {', '.join(flag_labels)}.")

    if score >= 70:
        parts.append("Overall account credibility is HIGH.")
    elif score >= 45:
        parts.append("Overall account credibility is MODERATE.")
    else:
        parts.append("Overall account credibility is LOW — treat content with extra caution.")

    return " ".join(parts)


# =============================================================================
# SECTION 6 — BLEND WITH CONTENT SCORE
# =============================================================================

def blend_scores(
    content_score: float,
    account_score: float,
    weight: float = 0.15,
) -> float:
    """
    Blends the account credibility score into the content credibility score.

    Formula:
      final = (content_score × (1 - weight)) + (account_score × weight)

    Default weight = 0.15 (15% from account, 85% from content).
    This means:
      • A great account can add up to ~7.5 points
      • A terrible account can reduce by up to ~7.5 points
      • Content analysis is always the dominant factor

    You can increase the weight to make the account source matter more.
    Set to 0.0 to disable account scoring entirely.
    """
    if weight <= 0:
        return content_score

    blended = (content_score * (1 - weight)) + (account_score * weight)
    return max(0.0, min(100.0, round(blended, 1)))
