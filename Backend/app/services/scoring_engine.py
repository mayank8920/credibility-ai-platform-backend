# =============================================================================
# app/services/scoring_engine.py
# =============================================================================
#
#   THE CREDIBILITY SCORING ENGINE
#   ───────────────────────────────
#   Plain-English explanation for non-technical founders:
#
#   Think of this file as a team of five specialist judges who each look at
#   the same piece of content from a different angle, give it a partial score,
#   and then a head judge combines those scores into one final verdict.
#
#   The five judges are:
#
#   1. CLAIM JUDGE        — How many facts actually check out?
#   2. SOURCE JUDGE       — Were the sources trustworthy? How many were there?
#   3. LANGUAGE JUDGE     — Does the writing use panic/urgency tactics?
#   4. FACT-CHECK JUDGE   — Has this been debunked before by a fact-checker?
#   5. FLAG INSPECTOR     — Issues warning labels based on what was found
#
#   Final score:  0 = completely false/unreliable
#                50 = mixed / can't tell
#               100 = fully verified and credible
#
# =============================================================================

from __future__ import annotations

import re
import math
import logging
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1 — FLAG DEFINITIONS
# =============================================================================
# Flags are warning labels attached to a result. Each flag has:
#   • A machine-readable code    (sent to frontend for conditional styling)
#   • A human-readable label     (shown in the UI)
#   • A severity level           (INFO / WARNING / DANGER)
#   • A score penalty it applies (some flags also reduce the score)
# =============================================================================

class FlagSeverity(str, Enum):
    INFO    = "INFO"      # Informational — no alarm, just context
    WARNING = "WARNING"   # Caution — something worth knowing
    DANGER  = "DANGER"    # High concern — strongly affects credibility


@dataclass(frozen=True)
class FlagDefinition:
    code: str
    label: str
    description: str        # Shown as a tooltip in the UI
    severity: FlagSeverity
    score_penalty: float    # Points deducted from the final score (0 = no penalty)


# ── Master flag registry ─────────────────────────────────────────────────────
FLAGS: dict[str, FlagDefinition] = {

    # ── Source-related flags ─────────────────────────────────
    "NO_CREDIBLE_SOURCE": FlagDefinition(
        code="NO_CREDIBLE_SOURCE",
        label="No Credible Source",
        description="No articles from trusted news outlets were found supporting these claims.",
        severity=FlagSeverity.DANGER,
        score_penalty=18.0,
    ),
    "LOW_SOURCE_COUNT": FlagDefinition(
        code="LOW_SOURCE_COUNT",
        label="Few Sources",
        description="Only one or two news sources were found — not enough for strong confidence.",
        severity=FlagSeverity.WARNING,
        score_penalty=8.0,
    ),
    "SINGLE_SOURCE": FlagDefinition(
        code="SINGLE_SOURCE",
        label="Single Source Only",
        description="All evidence comes from a single outlet. Independent corroboration is lacking.",
        severity=FlagSeverity.WARNING,
        score_penalty=5.0,
    ),

    # ── Language-related flags ───────────────────────────────
    "URGENCY_LANGUAGE": FlagDefinition(
        code="URGENCY_LANGUAGE",
        label="Urgency Language Detected",
        description="The content uses words designed to create panic or urgency (e.g. 'BREAKING', 'MUST SHARE').",
        severity=FlagSeverity.WARNING,
        score_penalty=10.0,
    ),
    "EMOTIONAL_MANIPULATION": FlagDefinition(
        code="EMOTIONAL_MANIPULATION",
        label="Emotional Manipulation",
        description="Heavy use of emotionally charged or fear-inducing language detected.",
        severity=FlagSeverity.WARNING,
        score_penalty=8.0,
    ),
    "CLICKBAIT_HEADLINE": FlagDefinition(
        code="CLICKBAIT_HEADLINE",
        label="Clickbait Pattern",
        description="The content matches common clickbait structures designed to mislead before reading.",
        severity=FlagSeverity.WARNING,
        score_penalty=6.0,
    ),
    "ALL_CAPS_ABUSE": FlagDefinition(
        code="ALL_CAPS_ABUSE",
        label="Excessive Capitalisation",
        description="Large portions of the text are in ALL CAPS, a common tactic in misleading content.",
        severity=FlagSeverity.INFO,
        score_penalty=4.0,
    ),

    # ── Claim-related flags ──────────────────────────────────
    "UNVERIFIED_CLAIMS": FlagDefinition(
        code="UNVERIFIED_CLAIMS",
        label="Unverified Claims",
        description="The majority of factual claims could not be verified against news sources.",
        severity=FlagSeverity.WARNING,
        score_penalty=7.0,
    ),
    "CONTRADICTED_CLAIMS": FlagDefinition(
        code="CONTRADICTED_CLAIMS",
        label="Contradicted by Sources",
        description="One or more claims were directly contradicted by news reporting.",
        severity=FlagSeverity.DANGER,
        score_penalty=15.0,
    ),
    "VAGUE_CLAIMS": FlagDefinition(
        code="VAGUE_CLAIMS",
        label="Vague or Unverifiable Claims",
        description="Some claims are too vague to verify — they use language like 'some say' or 'experts believe' without specifics.",
        severity=FlagSeverity.INFO,
        score_penalty=4.0,
    ),

    # ── Fact-check flags ─────────────────────────────────────
    "FACT_CHECK_FAILED": FlagDefinition(
        code="FACT_CHECK_FAILED",
        label="Previously Debunked",
        description="This content or very similar content has been fact-checked and rated false by a reputable fact-checker.",
        severity=FlagSeverity.DANGER,
        score_penalty=25.0,
    ),
    "FACT_CHECK_DISPUTED": FlagDefinition(
        code="FACT_CHECK_DISPUTED",
        label="Disputed by Fact-Checkers",
        description="A fact-checking organisation has rated this claim as partially false or contested.",
        severity=FlagSeverity.WARNING,
        score_penalty=12.0,
    ),
    "FACT_CHECK_VERIFIED": FlagDefinition(
        code="FACT_CHECK_VERIFIED",
        label="Independently Fact-Checked ✓",
        description="A reputable fact-checker has independently verified this content.",
        severity=FlagSeverity.INFO,
        score_penalty=0.0,     # bonus flag — no penalty, handled in score
    ),

    # ── Rumour / misinformation pattern flags ────────────────
    "POSSIBLE_RUMOR": FlagDefinition(
        code="POSSIBLE_RUMOR",
        label="Possible Rumour",
        description="The content has characteristics common to viral rumours: no sources, urgency language, and unverifiable claims.",
        severity=FlagSeverity.DANGER,
        score_penalty=15.0,
    ),
    "VIRAL_MISINFORMATION_PATTERN": FlagDefinition(
        code="VIRAL_MISINFORMATION_PATTERN",
        label="Viral Misinformation Pattern",
        description="The structure and language match patterns commonly seen in viral misinformation.",
        severity=FlagSeverity.DANGER,
        score_penalty=12.0,
    ),
    "ANONYMOUS_SOURCE": FlagDefinition(
        code="ANONYMOUS_SOURCE",
        label="Anonymous Source",
        description="Claims are attributed to unnamed or anonymous sources ('sources say', 'insiders report').",
        severity=FlagSeverity.WARNING,
        score_penalty=6.0,
    ),

    # ── Positive / trust flags ───────────────────────────────
    "HIGHLY_CREDIBLE": FlagDefinition(
        code="HIGHLY_CREDIBLE",
        label="Highly Credible",
        description="Multiple Tier-1 news sources corroborate the claims in this content.",
        severity=FlagSeverity.INFO,
        score_penalty=0.0,
    ),
    "OFFICIAL_SOURCE": FlagDefinition(
        code="OFFICIAL_SOURCE",
        label="Official Source Referenced",
        description="Content references official organisations (government, WHO, CDC, NASA, etc.).",
        severity=FlagSeverity.INFO,
        score_penalty=0.0,
    ),
}


# =============================================================================
# SECTION 2 — INPUT / OUTPUT DATA STRUCTURES
# =============================================================================

@dataclass
class ClaimInput:
    """A single claim + the news search result for that claim."""
    text: str
    status: str                          # VERIFIED / DISPUTED / UNVERIFIED / FALSE
    confidence: float                    # 0–100 from the news evaluator
    news_articles_found: int = 0
    tier1_source_hit: bool = False
    source_names: List[str] = field(default_factory=list)
    supporting_headlines: List[str] = field(default_factory=list)
    fact_check_status: Optional[str] = None   # VERIFIED / DISPUTED / DEBUNKED / None


@dataclass
class ScoringInput:
    """
    Everything the scoring engine needs.
    The caller builds this object and passes it to `compute_score()`.
    """
    original_content: str                           # Full text being analysed
    claims: List[ClaimInput]                        # Evaluated claims
    credible_source_count: int        = 0           # Tier-1 sources found total
    total_source_count: int           = 0           # All sources (any tier)
    fact_check_matches: List[str]     = field(default_factory=list)  # e.g. ["DEBUNKED", "VERIFIED"]
    content_type: str                 = "tweet"     # tweet / article / post


@dataclass
class SubScore:
    """Intermediate score from one of the five judges."""
    judge_name: str
    raw_score: float      # 0–100 from this judge alone
    weight: float         # How much this judge's score counts toward the final
    contribution: float   # raw_score × weight (pre-computed for transparency)
    notes: str            # Explanation for this sub-score


@dataclass
class ScoringResult:
    """The final output of the entire scoring engine."""
    credibility_score: float              # 0–100 final score
    verdict: str                          # VERIFIED / MOSTLY_TRUE / QUESTIONABLE / MISLEADING / FALSE
    verdict_label: str                    # "Mostly True"
    verdict_color: str                    # "#65a30d" (hex for frontend badge)
    flags: List[str]                      # Flag codes e.g. ["URGENCY_LANGUAGE", "NO_CREDIBLE_SOURCE"]
    flag_details: List[dict]              # Full flag objects for display
    summary: str                          # 2–3 sentence plain-English summary
    sub_scores: List[SubScore]            # Per-judge breakdown (for /explain endpoint)
    penalty_total: float                  # Total points deducted by flags
    confidence_level: str                 # HIGH / MEDIUM / LOW — how sure is the engine?
    claims_breakdown: dict                # Counts: {verified, false, disputed, unverified}


# =============================================================================
# SECTION 3 — LANGUAGE ANALYSIS
# =============================================================================
# These dictionaries power the Language Judge.
# We scan the original text for these patterns using regex.
# =============================================================================

# Words/phrases that signal manufactured urgency or panic
URGENCY_PATTERNS = [
    r"\bBREAKING\b",
    r"\bURGENT\b",
    r"\bALERT\b",
    r"\bWARNING\b",
    r"\bEMERGENCY\b",
    r"\bMUST\s+(?:SHARE|READ|WATCH|SEE)\b",
    r"\bSHARE\s+(?:THIS|NOW|BEFORE)\b",
    r"\bPLEASE\s+SHARE\b",
    r"\bGO\s+VIRAL\b",
    r"\bSPREAD\s+THE\s+WORD\b",
    r"\bWORLD\s+NEEDS\s+TO\s+KNOW\b",
    r"\bTHEY\s+DON'?T\s+WANT\s+YOU\s+TO\s+KNOW\b",
    r"\bMAINSTREAM\s+MEDIA\s+WON'?T\s+TELL\b",
    r"\bSILENCED\b",
    r"\bCENSORED\b",
    r"\bWAKE\s+UP\b",
    r"\bSHEEPLE\b",
    r"\bFAKE\s+NEWS\b",  # when used as a dismissal tactic
    r"\bDEEP\s+STATE\b",
    r"\bCOVER.?UP\b",
    r"\bCONSPIRACY\b",
    r"!!+",              # Multiple exclamation marks
    r"\?\?+",            # Multiple question marks
    r"\bLAST\s+CHANCE\b",
    r"\bACT\s+NOW\b",
    r"\bBEFORE\s+IT'?S\s+DELETED\b",
    r"\bBEFORE\s+THEY\s+TAKE\s+IT\s+DOWN\b",
]

# Emotional manipulation: fear / anger / outrage language
EMOTIONAL_PATTERNS = [
    r"\boutrage(?:d|ous)?\b",
    r"\bscandal(?:ous)?\b",
    r"\bshock(?:ing|ed)?\b",
    r"\bhorr(?:ifying|ible|endous)\b",
    r"\bdisgusting\b",
    r"\bsickening\b",
    r"\bdevastating\b",
    r"\bterr(?:ifying|ible)\b",
    r"\brage\b",
    r"\bfury\b",
    r"\bfurious\b",
    r"\btreason\b",
    r"\btraitor\b",
    r"\bevil\b",
    r"\bcriminal(?:s)?\b",
    r"\bpedophil\b",
    r"\bgenocide\b",
    r"\bdestroy(?:ing|ed)?\b",
    r"\bcollaps(?:ing|e)?\b",
]

# Clickbait structural patterns
CLICKBAIT_PATTERNS = [
    r"\bYou Won'?t Believe\b",
    r"\bWhat Happens Next\b",
    r"\bThis Is Why\b",
    r"\bThe Truth About\b",
    r"\bSecret(?:s)? (?:They|The)\b",
    r"\bHere'?s What\b",
    r"\bEveryone Is Talking\b",
    r"\bGoes Viral\b",
    r"\bBreaks The Internet\b",
    r"\bMind.?Blowing\b",
    r"\bLife.?Changing\b",
    r"\bNumber \d+ Will Shock You\b",
    r"\bDoctors Hate\b",
    r"\bOne Weird Trick\b",
    r"\bThis Simple Trick\b",
]

# Vague attribution — "someone said something" without naming anyone
VAGUE_ATTRIBUTION_PATTERNS = [
    r"\b(?:some|many|most)\s+(?:people|experts|scientists|doctors|officials)\s+(?:say|claim|believe|think|warn)\b",
    r"\bsources\s+(?:say|claim|report|reveal)\b",
    r"\binsiders?\s+(?:say|claim|report|reveal)\b",
    r"\bit\s+is\s+(?:said|reported|claimed|believed)\b",
    r"\baccording\s+to\s+(?:some|many|reports?)\b",
    r"\bword\s+(?:has|is)\s+(?:it|out)\b",
    r"\bthey\s+(?:say|claim|don'?t\s+want)\b",
    r"\beveryone\s+(?:knows?|is\s+saying)\b",
    r"\bI\s+heard\b",
    r"\ba\s+friend\s+(?:told|said)\b",
]

# Official/authoritative source references — boosts credibility
OFFICIAL_SOURCE_PATTERNS = [
    r"\baccording\s+to\s+(?:the\s+)?(?:WHO|CDC|FDA|NHS|NASA|UN|EU|FBI|CIA|DOJ|Pentagon)\b",
    r"\b(?:WHO|CDC|FDA|NHS|NASA)\s+(?:confirmed|announced|stated|said|reported)\b",
    r"\bofficial\s+(?:statement|report|data|figures?|announcement)\b",
    r"\bpeer.?reviewed\b",
    r"\bpublished\s+in\s+(?:the\s+)?(?:Nature|Science|Lancet|NEJM|JAMA|BMJ)\b",
    r"\bgovernment\s+(?:data|report|official|spokesperson)\b",
    r"\bpress\s+conference\b",
    r"\bofficial\s+spokesperson\b",
]

# Fact-check site references in the text itself
FACT_CHECK_SITE_PATTERNS = [
    r"\bsnopes\.com\b",
    r"\bpolitifact\.com\b",
    r"\bfactcheck\.org\b",
    r"\bfullfact\.org\b",
    r"\bafp\s+fact\s+check\b",
    r"\breuters\s+fact\s+check\b",
    r"\bap\s+fact\s+check\b",
    r"\bfact.?check(?:ed|ing)?\b",
    r"\bdebunked\b",
    r"\brated\s+(?:false|true|mostly\s+false|mostly\s+true|half\s+true)\b",
]


# =============================================================================
# SECTION 4 — THE FIVE JUDGES (Sub-engines)
# =============================================================================

class ClaimJudge:
    """
    JUDGE 1: Evaluates the quality and proportion of verified claims.

    Scoring logic:
      - Start with 100 points (assumes everything is true)
      - Deduct per false claim   (heaviest penalty — certainty of falsehood)
      - Deduct per disputed      (medium penalty — uncertainty)
      - Deduct per unverified    (light penalty — absence of proof)
      - Add per verified         (bonus — positive evidence found)
      - Weight by confidence     (high-confidence results move the score more)
    """

    WEIGHTS = {
        "VERIFIED":   +20.0,   # Points earned per verified claim
        "DISPUTED":   -15.0,   # Points lost per disputed claim
        "UNVERIFIED":  -8.0,   # Points lost per unverified claim
        "FALSE":      -30.0,   # Points lost per false claim (largest impact)
    }

    def judge(self, claims: List[ClaimInput]) -> SubScore:
        if not claims:
            return SubScore("ClaimJudge", 50.0, 0.40, 20.0, "No claims to evaluate.")

        score = 50.0  # neutral baseline
        breakdown_lines = []

        for claim in claims:
            status = claim.status.upper()
            confidence_factor = claim.confidence / 100.0   # 0.0 – 1.0

            # Weight = base points × confidence × per-claim share
            per_claim_share = 1.0 / len(claims)
            adjustment = (
                self.WEIGHTS.get(status, 0.0)
                * confidence_factor
                * per_claim_share
                * 2.5        # scale factor to use the full 0–100 range
            )
            score += adjustment
            breakdown_lines.append(
                f'  • "{claim.text[:50]}..." → {status} ({claim.confidence:.0f}% conf) → {adjustment:+.1f} pts'
            )

        score = max(0.0, min(100.0, score))

        verified_count  = sum(1 for c in claims if c.status == "VERIFIED")
        false_count     = sum(1 for c in claims if c.status == "FALSE")
        total           = len(claims)

        notes = (
            f"{verified_count}/{total} claims verified, {false_count} false. "
            f"Raw claim score: {score:.1f}/100."
        )

        weight = 0.40   # Claims are the biggest factor (40% of final score)
        return SubScore("ClaimJudge", score, weight, score * weight, notes)


class SourceJudge:
    """
    JUDGE 2: Evaluates how many credible news sources corroborate the content.

    Scoring logic:
      Tier-1 sources (Reuters, BBC, AP, gov sites):   +20 each, capped at 3
      Tier-2 sources (regional papers, digital outlets): +8 each, capped at 3
      Only 1 source total:                             penalty applied (SINGLE_SOURCE flag)
      0 credible sources:                              near-zero score (NO_CREDIBLE_SOURCE flag)

    The source score is then scaled 0–100.
    """

    def judge(self, credible_count: int, total_count: int) -> SubScore:
        if credible_count == 0 and total_count == 0:
            return SubScore(
                "SourceJudge", 5.0, 0.25, 1.25,
                "Zero sources found. Cannot corroborate any claims."
            )

        # Build score from source counts
        tier1_contribution  = min(credible_count, 3) * 20.0   # max +60
        other_contribution  = min(max(total_count - credible_count, 0), 3) * 8.0   # max +24

        # Bonus for having a rich set of independent sources
        diversity_bonus = 8.0 if total_count >= 5 else (4.0 if total_count >= 3 else 0.0)

        raw = tier1_contribution + other_contribution + diversity_bonus
        score = min(raw, 100.0)

        # If only one total source, apply a penalty regardless of tier
        if total_count == 1:
            score = max(score - 15.0, 10.0)

        notes = (
            f"{credible_count} Tier-1 source(s) found out of {total_count} total. "
            f"Source quality score: {score:.1f}/100."
        )

        weight = 0.25   # Sources account for 25% of the final score
        return SubScore("SourceJudge", score, weight, score * weight, notes)


class LanguageJudge:
    """
    JUDGE 3: Analyses the writing style for misinformation red flags.

    Scoring logic:
      Start at 100 (assume neutral language).
      Deduct for each urgency phrase found.
      Deduct for each emotional manipulation phrase.
      Deduct for each clickbait pattern.
      Deduct for excessive ALL-CAPS usage.
      Add small bonus for official source references in text.

    High language score = neutral, fact-based writing.
    Low language score  = manipulative, sensational writing.
    """

    def judge(self, text: str) -> tuple[SubScore, List[str]]:
        """Returns (SubScore, list_of_triggered_flag_codes)."""
        if not text or not text.strip():
            return SubScore("LanguageJudge", 50.0, 0.15, 7.5, "No text to analyse."), []

        score = 100.0
        triggered_flags: List[str] = []
        upper_text = text.upper()
        details = []

        # ── Urgency language ───────────────────────────────────
        urgency_hits = sum(
            1 for p in URGENCY_PATTERNS
            if re.search(p, upper_text, re.IGNORECASE)
        )
        if urgency_hits > 0:
            deduction = min(urgency_hits * 8.0, 30.0)
            score -= deduction
            triggered_flags.append("URGENCY_LANGUAGE")
            details.append(f"Urgency patterns: {urgency_hits} hits → -{deduction:.0f} pts")

        # ── Emotional manipulation ─────────────────────────────
        emotional_hits = sum(
            1 for p in EMOTIONAL_PATTERNS
            if re.search(p, text, re.IGNORECASE)
        )
        if emotional_hits >= 3:
            deduction = min(emotional_hits * 4.0, 20.0)
            score -= deduction
            triggered_flags.append("EMOTIONAL_MANIPULATION")
            details.append(f"Emotional language: {emotional_hits} hits → -{deduction:.0f} pts")

        # ── Clickbait patterns ─────────────────────────────────
        clickbait_hits = sum(
            1 for p in CLICKBAIT_PATTERNS
            if re.search(p, text, re.IGNORECASE)
        )
        if clickbait_hits > 0:
            deduction = min(clickbait_hits * 7.0, 20.0)
            score -= deduction
            triggered_flags.append("CLICKBAIT_HEADLINE")
            details.append(f"Clickbait patterns: {clickbait_hits} hits → -{deduction:.0f} pts")

        # ── ALL CAPS abuse ─────────────────────────────────────
        words = text.split()
        caps_words = [w for w in words if w.isupper() and len(w) >= 3]
        caps_ratio = len(caps_words) / max(len(words), 1)
        if caps_ratio > 0.20:   # More than 20% of words are all-caps
            score -= 12.0
            triggered_flags.append("ALL_CAPS_ABUSE")
            details.append(f"ALL CAPS ratio: {caps_ratio:.0%} → -12 pts")

        # ── Vague attribution ──────────────────────────────────
        vague_hits = sum(
            1 for p in VAGUE_ATTRIBUTION_PATTERNS
            if re.search(p, text, re.IGNORECASE)
        )
        if vague_hits >= 2:
            score -= min(vague_hits * 5.0, 15.0)
            triggered_flags.append("VAGUE_CLAIMS")
            details.append(f"Vague attribution: {vague_hits} hits → penalty applied")

        # ── Anonymous source patterns ──────────────────────────
        anon_hits = sum(
            1 for p in [r"\bsources?\s+say\b", r"\binsiders?\b", r"\baccording\s+to\s+sources\b"]
            if re.search(p, text, re.IGNORECASE)
        )
        if anon_hits >= 1:
            score -= min(anon_hits * 5.0, 12.0)
            triggered_flags.append("ANONYMOUS_SOURCE")

        # ── Official source bonus ──────────────────────────────
        official_hits = sum(
            1 for p in OFFICIAL_SOURCE_PATTERNS
            if re.search(p, text, re.IGNORECASE)
        )
        if official_hits >= 1:
            score += min(official_hits * 5.0, 15.0)
            triggered_flags.append("OFFICIAL_SOURCE")
            details.append(f"Official source references: {official_hits} → +{min(official_hits * 5, 15)} pts")

        score = max(0.0, min(100.0, score))

        notes = "; ".join(details) if details else "Language appears neutral."
        weight = 0.15   # Language style is 15% of the final score
        return SubScore("LanguageJudge", score, weight, score * weight, notes), triggered_flags


class FactCheckJudge:
    """
    JUDGE 4: Checks whether any of the claims have been independently fact-checked.

    This judge receives a list of fact-check statuses — one per claim.
    Statuses come from the news search service which may find fact-check articles.

    Scoring:
      VERIFIED  (by fact-checker) → +30 bonus
      DISPUTED  (by fact-checker) → -20 penalty
      DEBUNKED  (by fact-checker) → -40 penalty (worst case)
      None / no match             →  neutral (0 adjustment)
    """

    STATUS_ADJUSTMENTS = {
        "VERIFIED":  +30.0,
        "DISPUTED":  -20.0,
        "DEBUNKED":  -40.0,
    }

    def judge(self, fact_check_matches: List[str]) -> tuple[SubScore, List[str]]:
        """Returns (SubScore, list_of_triggered_flag_codes)."""
        if not fact_check_matches:
            return SubScore(
                "FactCheckJudge", 50.0, 0.20, 10.0,
                "No fact-check matches found. Neither confirms nor denies credibility."
            ), []

        total_adjustment = 0.0
        triggered_flags: List[str] = []

        debunked_count  = fact_check_matches.count("DEBUNKED")
        disputed_count  = fact_check_matches.count("DISPUTED")
        verified_count  = fact_check_matches.count("VERIFIED")

        for status in fact_check_matches:
            total_adjustment += self.STATUS_ADJUSTMENTS.get(status.upper(), 0.0)

        # Map final adjustment to 0–100 score
        # +30 per verified claim → score above 50
        # -40 per debunked claim → score near 0
        score = 50.0 + total_adjustment
        score = max(0.0, min(100.0, score))

        if debunked_count > 0:
            triggered_flags.append("FACT_CHECK_FAILED")
        if disputed_count > 0:
            triggered_flags.append("FACT_CHECK_DISPUTED")
        if verified_count > 0 and debunked_count == 0:
            triggered_flags.append("FACT_CHECK_VERIFIED")

        notes = (
            f"Fact-check results — verified: {verified_count}, "
            f"disputed: {disputed_count}, debunked: {debunked_count}. "
            f"Fact-check score: {score:.1f}/100."
        )

        weight = 0.20   # Fact-checks are 20% of the final score
        return SubScore("FactCheckJudge", score, weight, score * weight, notes), triggered_flags


class FlagInspector:
    """
    JUDGE 5: Issues compound warning flags that arise from *combinations* of signals.

    This judge doesn't look at individual signals — it looks at the pattern.
    Example: Low source count alone is a warning, but low source count
    PLUS urgency language PLUS unverified claims = POSSIBLE_RUMOR.
    """

    def inspect(
        self,
        claims: List[ClaimInput],
        credible_source_count: int,
        total_source_count: int,
        existing_flags: List[str],
    ) -> List[str]:
        """Returns a list of additional compound flag codes."""
        compound_flags: List[str] = []

        verified_count   = sum(1 for c in claims if c.status == "VERIFIED")
        false_count      = sum(1 for c in claims if c.status == "FALSE")
        unverified_count = sum(1 for c in claims if c.status == "UNVERIFIED")
        total_claims     = len(claims)

        has_urgency          = "URGENCY_LANGUAGE" in existing_flags
        has_emotional        = "EMOTIONAL_MANIPULATION" in existing_flags
        has_no_source        = "NO_CREDIBLE_SOURCE" in existing_flags
        has_fact_fail        = "FACT_CHECK_FAILED" in existing_flags
        has_vague            = "VAGUE_CLAIMS" in existing_flags

        # ── POSSIBLE_RUMOR ─────────────────────────────────────
        # = No credible sources + urgency language + most claims unverified
        rumor_signals = sum([
            has_no_source,
            has_urgency,
            total_source_count == 0,
            (unverified_count / max(total_claims, 1)) >= 0.6,
        ])
        if rumor_signals >= 3:
            compound_flags.append("POSSIBLE_RUMOR")

        # ── VIRAL_MISINFORMATION_PATTERN ──────────────────────
        # = Clickbait + urgency + emotional + any false claims
        viral_signals = sum([
            "CLICKBAIT_HEADLINE" in existing_flags,
            has_urgency,
            has_emotional,
            false_count > 0,
        ])
        if viral_signals >= 3:
            compound_flags.append("VIRAL_MISINFORMATION_PATTERN")

        # ── CONTRADICTED_CLAIMS ────────────────────────────────
        if false_count > 0:
            compound_flags.append("CONTRADICTED_CLAIMS")

        # ── UNVERIFIED_CLAIMS ──────────────────────────────────
        if total_claims > 0 and (unverified_count / total_claims) >= 0.5:
            compound_flags.append("UNVERIFIED_CLAIMS")

        # ── NO_CREDIBLE_SOURCE ─────────────────────────────────
        if credible_source_count == 0 and total_source_count == 0:
            compound_flags.append("NO_CREDIBLE_SOURCE")
        elif total_source_count == 1:
            compound_flags.append("SINGLE_SOURCE")
        elif total_source_count <= 2 and credible_source_count == 0:
            compound_flags.append("LOW_SOURCE_COUNT")

        # ── HIGHLY_CREDIBLE ────────────────────────────────────
        # Positive flag: many verified claims from trusted sources
        if (
            credible_source_count >= 3
            and verified_count == total_claims
            and total_claims > 0
            and not has_fact_fail
        ):
            compound_flags.append("HIGHLY_CREDIBLE")

        return compound_flags


# =============================================================================
# SECTION 5 — THE HEAD JUDGE (Final Aggregator)
# =============================================================================

VERDICT_MAP: List[tuple] = [
    (82, "VERIFIED",     "Verified",      "#16a34a"),   # green-600
    (65, "MOSTLY_TRUE",  "Mostly True",   "#65a30d"),   # lime-600
    (48, "QUESTIONABLE", "Questionable",  "#ca8a04"),   # yellow-600
    (28, "MISLEADING",   "Misleading",    "#ea580c"),   # orange-600
    (0,  "FALSE",        "False / Fabricated", "#dc2626"),   # red-600
]


def compute_score(inp: ScoringInput) -> ScoringResult:
    """
    THE MAIN FUNCTION — call this from your FastAPI route.

    Takes a ScoringInput, runs all five judges, aggregates results,
    applies flag penalties, and returns a complete ScoringResult.

    Example usage:
        result = compute_score(scoring_input)
        print(result.credibility_score)   # → 22.0
        print(result.flags)               # → ["NO_CREDIBLE_SOURCE", "URGENCY_LANGUAGE", ...]
    """
    logger.info(
        f"[ScoringEngine] Starting. claims={len(inp.claims)}, "
        f"sources={inp.total_source_count} (credible={inp.credible_source_count}), "
        f"fact_checks={len(inp.fact_check_matches)}"
    )

    # ── Run the five judges ────────────────────────────────────
    claim_sub    = ClaimJudge().judge(inp.claims)
    source_sub   = SourceJudge().judge(inp.credible_source_count, inp.total_source_count)
    lang_sub, lang_flags     = LanguageJudge().judge(inp.original_content)
    fc_sub,   fc_flags       = FactCheckJudge().judge(inp.fact_check_matches)

    # Collect all flags found so far for compound detection
    preliminary_flags = lang_flags + fc_flags
    compound_flags = FlagInspector().inspect(
        inp.claims,
        inp.credible_source_count,
        inp.total_source_count,
        preliminary_flags,
    )

    all_flag_codes: List[str] = list(dict.fromkeys(
        preliminary_flags + compound_flags
    ))   # deduplicated, order preserved

    # ── Weighted aggregate ────────────────────────────────────
    # Weights: Claims 40%  |  Sources 25%  |  Language 15%  |  FactChecks 20%
    # These add to exactly 1.0.
    weighted_score = (
        claim_sub.contribution
        + source_sub.contribution
        + lang_sub.contribution
        + fc_sub.contribution
    )

    # ── Apply flag penalties ───────────────────────────────────
    # Flags can deduct additional points from the weighted score.
    # This creates the "double hit": a flag is raised AND the score drops.
    penalty_total = 0.0
    for code in all_flag_codes:
        flag_def = FLAGS.get(code)
        if flag_def and flag_def.score_penalty > 0:
            # Scale penalty so multiple flags don't completely annihilate the score
            # Each flag's penalty is halved if more than 3 flags are present
            scaling = 0.6 if len(all_flag_codes) > 3 else 1.0
            penalty_total += flag_def.score_penalty * scaling

    final_score = max(0.0, min(100.0, weighted_score - penalty_total))

    # ── Map score → verdict ────────────────────────────────────
    verdict, verdict_label, verdict_color = _score_to_verdict(final_score)

    # ── Build full flag detail objects (for UI display) ────────
    flag_details = []
    for code in all_flag_codes:
        fdef = FLAGS.get(code)
        if fdef:
            flag_details.append({
                "code":         fdef.code,
                "label":        fdef.label,
                "description":  fdef.description,
                "severity":     fdef.severity.value,
                "score_penalty": fdef.score_penalty,
            })

    # ── Confidence level ───────────────────────────────────────
    confidence_level = _compute_confidence_level(inp, all_flag_codes)

    # ── Claims breakdown ───────────────────────────────────────
    claims_breakdown = {
        "total":      len(inp.claims),
        "verified":   sum(1 for c in inp.claims if c.status == "VERIFIED"),
        "false":      sum(1 for c in inp.claims if c.status == "FALSE"),
        "disputed":   sum(1 for c in inp.claims if c.status == "DISPUTED"),
        "unverified": sum(1 for c in inp.claims if c.status == "UNVERIFIED"),
    }

    # ── Generate summary ───────────────────────────────────────
    summary = _generate_summary(final_score, verdict_label, claims_breakdown, all_flag_codes)

    logger.info(
        f"[ScoringEngine] Done. score={final_score:.1f}, verdict={verdict}, "
        f"flags={all_flag_codes}, penalty={penalty_total:.1f}"
    )

    return ScoringResult(
        credibility_score=round(final_score, 1),
        verdict=verdict,
        verdict_label=verdict_label,
        verdict_color=verdict_color,
        flags=all_flag_codes,
        flag_details=flag_details,
        summary=summary,
        sub_scores=[claim_sub, source_sub, lang_sub, fc_sub],
        penalty_total=round(penalty_total, 1),
        confidence_level=confidence_level,
        claims_breakdown=claims_breakdown,
    )


# =============================================================================
# SECTION 6 — HELPERS
# =============================================================================

def _score_to_verdict(score: float) -> tuple[str, str, str]:
    for threshold, verdict, label, color in VERDICT_MAP:
        if score >= threshold:
            return verdict, label, color
    return "FALSE", "False / Fabricated", "#dc2626"


def _compute_confidence_level(inp: ScoringInput, flags: List[str]) -> str:
    """
    How confident is the engine in its own score?

    HIGH:   Lots of claims, multiple sources, some fact-checks
    MEDIUM: Some claims and sources but gaps
    LOW:    Very little evidence to go on
    """
    score = 0

    # More claims = more data to work with
    if len(inp.claims) >= 3:
        score += 2
    elif len(inp.claims) >= 1:
        score += 1

    # More sources = more corroboration
    if inp.total_source_count >= 5:
        score += 3
    elif inp.total_source_count >= 3:
        score += 2
    elif inp.total_source_count >= 1:
        score += 1

    # Fact-check matches dramatically increase confidence
    if inp.fact_check_matches:
        score += 3

    # Flags indicating very little evidence lower confidence
    if "NO_CREDIBLE_SOURCE" in flags:
        score -= 2
    if "UNVERIFIED_CLAIMS" in flags:
        score -= 1

    if score >= 6:
        return "HIGH"
    elif score >= 3:
        return "MEDIUM"
    else:
        return "LOW"


def _generate_summary(
    score: float,
    verdict_label: str,
    breakdown: dict,
    flags: List[str],
) -> str:
    """Writes a 2–3 sentence plain-English summary."""
    total     = breakdown["total"]
    verified  = breakdown["verified"]
    false_cnt = breakdown["false"]
    disputed  = breakdown["disputed"]

    parts = []

    # Sentence 1: Overall verdict
    if score >= 80:
        parts.append(
            f"This content scored {score:.0f}/100 and is rated {verdict_label.upper()} — "
            f"the majority of claims were corroborated by credible news sources."
        )
    elif score >= 60:
        parts.append(
            f"This content scored {score:.0f}/100 and is rated {verdict_label.upper()} — "
            f"most claims appear accurate but some could not be fully verified."
        )
    elif score >= 40:
        parts.append(
            f"This content scored {score:.0f}/100 and is rated {verdict_label.upper()} — "
            f"significant concerns were found including unverified or disputed claims."
        )
    else:
        parts.append(
            f"This content scored {score:.0f}/100 and is rated {verdict_label.upper()} — "
            f"multiple claims appear inaccurate or are directly contradicted by news reporting."
        )

    # Sentence 2: Claims breakdown
    if total > 0:
        parts.append(
            f"Of {total} factual claim(s) checked: "
            f"{verified} verified, {false_cnt} false, {disputed} disputed."
        )

    # Sentence 3: Key flags
    danger_flags = [f for f in flags if FLAGS.get(f) and FLAGS[f].severity == FlagSeverity.DANGER]
    warning_flags = [f for f in flags if FLAGS.get(f) and FLAGS[f].severity == FlagSeverity.WARNING]

    if danger_flags:
        labels = [FLAGS[f].label for f in danger_flags[:2]]
        parts.append(f"Key concerns: {', '.join(labels)}.")
    elif warning_flags:
        labels = [FLAGS[f].label for f in warning_flags[:2]]
        parts.append(f"Caution: {', '.join(labels)}.")

    return " ".join(parts)
