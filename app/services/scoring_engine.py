# =============================================================================
# app/services/scoring_engine.py
# =============================================================================
#
#   THE CREDIBILITY SCORING ENGINE
#   ───────────────────────────────
#   Five specialist judges each score the content from a different angle,
#   then a head judge combines their scores into one final verdict.
#
#   The five judges are:
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
# CHANGES IN THIS VERSION:
#   FIX F-01: VERDICT_MAP codes renamed to match frontend VERDICT_CONFIG keys
#             VERIFIED    → credible
#             MOSTLY_TRUE → mostly_credible
#             QUESTIONABLE→ questionable      (already lowercase)
#             MISLEADING  → likely_false
#             FALSE       → false             (already lowercase)
#
#   FIX F-02: FlagSeverity enum renamed to match frontend FLAG_SEVERITY_COLORS
#             INFO    → LOW
#             WARNING → MEDIUM
#             DANGER  → HIGH
# =============================================================================

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1 — FLAG DEFINITIONS
# =============================================================================

# FIX F-02: Renamed to match FLAG_SEVERITY_COLORS keys in frontend utils.ts
class FlagSeverity(str, Enum):
    LOW    = "LOW"      # Informational — no alarm, just context
    MEDIUM = "MEDIUM"   # Caution — something worth knowing
    HIGH   = "HIGH"     # High concern — strongly affects credibility


@dataclass(frozen=True)
class FlagDefinition:
    code: str
    label: str
    description: str
    severity: FlagSeverity
    score_penalty: float


# ── Master flag registry ─────────────────────────────────────────────────────
FLAGS: dict[str, FlagDefinition] = {

    # ── Source-related flags ─────────────────────────────────────────────────
    "NO_CREDIBLE_SOURCE": FlagDefinition(
        code="NO_CREDIBLE_SOURCE",
        label="No Credible Source",
        description="No articles from trusted news outlets were found supporting these claims.",
        severity=FlagSeverity.HIGH,
        score_penalty=18.0,
    ),
    "LOW_SOURCE_COUNT": FlagDefinition(
        code="LOW_SOURCE_COUNT",
        label="Few Sources",
        description="Only one or two news sources were found — not enough for strong confidence.",
        severity=FlagSeverity.MEDIUM,
        score_penalty=8.0,
    ),
    "SINGLE_SOURCE": FlagDefinition(
        code="SINGLE_SOURCE",
        label="Single Source Only",
        description="All evidence comes from a single outlet. Independent corroboration is lacking.",
        severity=FlagSeverity.MEDIUM,
        score_penalty=5.0,
    ),

    # ── Language-related flags ───────────────────────────────────────────────
    "URGENCY_LANGUAGE": FlagDefinition(
        code="URGENCY_LANGUAGE",
        label="Urgency Language Detected",
        description="The content uses words designed to create panic or urgency (e.g. 'BREAKING', 'MUST SHARE').",
        severity=FlagSeverity.MEDIUM,
        score_penalty=10.0,
    ),
    "EMOTIONAL_MANIPULATION": FlagDefinition(
        code="EMOTIONAL_MANIPULATION",
        label="Emotional Manipulation",
        description="Heavy use of emotionally charged or fear-inducing language detected.",
        severity=FlagSeverity.MEDIUM,
        score_penalty=8.0,
    ),
    "CLICKBAIT_HEADLINE": FlagDefinition(
        code="CLICKBAIT_HEADLINE",
        label="Clickbait Pattern",
        description="The content matches common clickbait structures designed to mislead before reading.",
        severity=FlagSeverity.MEDIUM,
        score_penalty=6.0,
    ),
    "ALL_CAPS_ABUSE": FlagDefinition(
        code="ALL_CAPS_ABUSE",
        label="Excessive Capitalisation",
        description="Large portions of the text are in ALL CAPS, a common tactic in misleading content.",
        severity=FlagSeverity.LOW,
        score_penalty=4.0,
    ),

    # ── Claim-related flags ──────────────────────────────────────────────────
    "UNVERIFIED_CLAIMS": FlagDefinition(
        code="UNVERIFIED_CLAIMS",
        label="Unverified Claims",
        description="The majority of factual claims could not be verified against news sources.",
        severity=FlagSeverity.MEDIUM,
        score_penalty=7.0,
    ),
    "CONTRADICTED_CLAIMS": FlagDefinition(
        code="CONTRADICTED_CLAIMS",
        label="Contradicted by Sources",
        description="One or more claims were directly contradicted by news reporting.",
        severity=FlagSeverity.HIGH,
        score_penalty=15.0,
    ),
    "VAGUE_CLAIMS": FlagDefinition(
        code="VAGUE_CLAIMS",
        label="Vague or Unverifiable Claims",
        description="Some claims are too vague to verify — they use language like 'some say' or 'experts believe' without specifics.",
        severity=FlagSeverity.LOW,
        score_penalty=4.0,
    ),

    # ── Fact-check flags ─────────────────────────────────────────────────────
    "FACT_CHECK_FAILED": FlagDefinition(
        code="FACT_CHECK_FAILED",
        label="Previously Debunked",
        description="This content or very similar content has been fact-checked and rated false by a reputable fact-checker.",
        severity=FlagSeverity.HIGH,
        score_penalty=25.0,
    ),
    "FACT_CHECK_DISPUTED": FlagDefinition(
        code="FACT_CHECK_DISPUTED",
        label="Disputed by Fact-Checkers",
        description="A fact-checking organisation has rated this claim as partially false or contested.",
        severity=FlagSeverity.MEDIUM,
        score_penalty=12.0,
    ),
    "FACT_CHECK_VERIFIED": FlagDefinition(
        code="FACT_CHECK_VERIFIED",
        label="Independently Fact-Checked ✓",
        description="A reputable fact-checker has independently verified this content.",
        severity=FlagSeverity.LOW,
        score_penalty=0.0,
    ),

    # ── Rumour / misinformation pattern flags ────────────────────────────────
    "POSSIBLE_RUMOR": FlagDefinition(
        code="POSSIBLE_RUMOR",
        label="Possible Rumour",
        description="The content has characteristics common to viral rumours: no sources, urgency language, and unverifiable claims.",
        severity=FlagSeverity.HIGH,
        score_penalty=15.0,
    ),
    "VIRAL_MISINFORMATION_PATTERN": FlagDefinition(
        code="VIRAL_MISINFORMATION_PATTERN",
        label="Viral Misinformation Pattern",
        description="The structure and language match patterns commonly seen in viral misinformation.",
        severity=FlagSeverity.HIGH,
        score_penalty=12.0,
    ),
    "ANONYMOUS_SOURCE": FlagDefinition(
        code="ANONYMOUS_SOURCE",
        label="Anonymous Source",
        description="Claims are attributed to unnamed or anonymous sources ('sources say', 'insiders report').",
        severity=FlagSeverity.MEDIUM,
        score_penalty=6.0,
    ),

    # ── Positive / trust flags ───────────────────────────────────────────────
    "HIGHLY_CREDIBLE": FlagDefinition(
        code="HIGHLY_CREDIBLE",
        label="Highly Credible",
        description="Multiple Tier-1 news sources corroborate the claims in this content.",
        severity=FlagSeverity.LOW,
        score_penalty=0.0,
    ),
    "OFFICIAL_SOURCE": FlagDefinition(
        code="OFFICIAL_SOURCE",
        label="Official Source Referenced",
        description="Content references official organisations (government, WHO, CDC, NASA, etc.).",
        severity=FlagSeverity.LOW,
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
    """Everything the scoring engine needs."""
    original_content: str
    claims: List[ClaimInput]
    credible_source_count: int        = 0
    total_source_count: int           = 0
    fact_check_matches: List[str]     = field(default_factory=list)
    content_type: str                 = "tweet"


@dataclass
class SubScore:
    """Intermediate score from one of the five judges."""
    judge_name: str
    raw_score: float
    weight: float
    contribution: float    # raw_score × weight
    notes: str


@dataclass
class ScoringResult:
    """The final output of the entire scoring engine."""
    credibility_score: float
    verdict: str                 # FIX F-01: now uses frontend vocabulary
    verdict_label: str
    verdict_color: str
    flags: List[str]
    flag_details: List[dict]
    summary: str
    sub_scores: List[SubScore]
    penalty_total: float
    confidence_level: str        # HIGH / MEDIUM / LOW
    claims_breakdown: dict


# =============================================================================
# SECTION 3 — LANGUAGE ANALYSIS PATTERNS
# =============================================================================

URGENCY_PATTERNS = [
    r"\bBREAKING\b", r"\bURGENT\b", r"\bALERT\b", r"\bWARNING\b",
    r"\bEMERGENCY\b", r"\bMUST\s+(?:SHARE|READ|WATCH|SEE)\b",
    r"\bSHARE\s+(?:THIS|NOW|BEFORE)\b", r"\bPLEASE\s+SHARE\b",
    r"\bGO\s+VIRAL\b", r"\bSPREAD\s+THE\s+WORD\b",
    r"\bWORLD\s+NEEDS\s+TO\s+KNOW\b",
    r"\bTHEY\s+DON'?T\s+WANT\s+YOU\s+TO\s+KNOW\b",
    r"\bMAINSTREAM\s+MEDIA\s+WON'?T\s+TELL\b",
    r"\bSILENCED\b", r"\bCENSORED\b", r"\bWAKE\s+UP\b",
    r"\bSHEEPLE\b", r"\bFAKE\s+NEWS\b", r"\bDEEP\s+STATE\b",
    r"\bCOVER.?UP\b", r"\bCONSPIRACY\b",
    r"!!+", r"\?\?+",
    r"\bLAST\s+CHANCE\b", r"\bACT\s+NOW\b",
    r"\bBEFORE\s+IT'?S\s+DELETED\b",
    r"\bBEFORE\s+THEY\s+TAKE\s+IT\s+DOWN\b",
]

EMOTIONAL_PATTERNS = [
    r"\boutrage(?:d|ous)?\b", r"\bscandal(?:ous)?\b",
    r"\bshock(?:ing|ed)?\b", r"\bhorr(?:ifying|ible|endous)\b",
    r"\bdisgusting\b", r"\bsickening\b", r"\bdevastating\b",
    r"\bterr(?:ifying|ible)\b", r"\brage\b", r"\bfury\b",
    r"\bfurious\b", r"\btreason\b", r"\btraitor\b", r"\bevil\b",
    r"\bcriminal(?:s)?\b", r"\bpedophil\b", r"\bgenocide\b",
    r"\bdestroy(?:ing|ed)?\b", r"\bcollaps(?:ing|e)?\b",
]

CLICKBAIT_PATTERNS = [
    r"\bYou Won'?t Believe\b", r"\bWhat Happens Next\b",
    r"\bThis Is Why\b", r"\bThe Truth About\b",
    r"\bSecret(?:s)? (?:They|The)\b", r"\bHere'?s What\b",
    r"\bEveryone Is Talking\b", r"\bGoes Viral\b",
    r"\bBreaks The Internet\b", r"\bMind.?Blowing\b",
    r"\bLife.?Changing\b", r"\bNumber \d+ Will Shock You\b",
    r"\bDoctors Hate\b", r"\bOne Weird Trick\b",
    r"\bThis Simple Trick\b",
]

VAGUE_ATTRIBUTION_PATTERNS = [
    r"\b(?:some|many|most)\s+(?:people|experts|scientists|doctors|officials)\s+(?:say|claim|believe|think|warn)\b",
    r"\bsources\s+(?:say|claim|report|reveal)\b",
    r"\binsiders?\s+(?:say|claim|report|reveal)\b",
    r"\bit\s+is\s+(?:said|reported|claimed|believed)\b",
    r"\baccording\s+to\s+(?:some|many|reports?)\b",
    r"\bword\s+(?:has|is)\s+(?:it|out)\b",
    r"\bthey\s+(?:say|claim|don'?t\s+want)\b",
    r"\beveryone\s+(?:knows?|is\s+saying)\b",
    r"\bI\s+heard\b", r"\ba\s+friend\s+(?:told|said)\b",
]

OFFICIAL_SOURCE_PATTERNS = [
    r"\baccording\s+to\s+(?:the\s+)?(?:WHO|CDC|FDA|NHS|NASA|UN|EU|FBI|CIA|DOJ|Pentagon)\b",
    r"\b(?:WHO|CDC|FDA|NHS|NASA)\s+(?:confirmed|announced|stated|said|reported)\b",
    r"\bofficial\s+(?:statement|report|data|figures?|announcement)\b",
    r"\bpeer.?reviewed\b",
    r"\bpublished\s+in\s+(?:the\s+)?(?:Nature|Science|Lancet|NEJM|JAMA|BMJ)\b",
    r"\bgovernment\s+(?:data|report|official|spokesperson)\b",
    r"\bpress\s+conference\b", r"\bofficial\s+spokesperson\b",
]


# =============================================================================
# SECTION 4 — THE FIVE JUDGES
# =============================================================================

class ClaimJudge:
    """JUDGE 1: Evaluates the quality and proportion of verified claims."""

    WEIGHTS = {
        "VERIFIED":   +20.0,
        "DISPUTED":   -15.0,
        "UNVERIFIED":  -8.0,
        "FALSE":      -30.0,
    }

    def judge(self, claims: List[ClaimInput]) -> SubScore:
        if not claims:
            return SubScore("ClaimJudge", 50.0, 0.40, 20.0, "No claims to evaluate.")

        score = 50.0
        breakdown_lines = []

        for claim in claims:
            status = claim.status.upper()
            confidence_factor = claim.confidence / 100.0
            per_claim_share = 1.0 / len(claims)
            adjustment = (
                self.WEIGHTS.get(status, 0.0)
                * confidence_factor
                * per_claim_share
                * 2.5
            )
            score += adjustment
            breakdown_lines.append(
                f'  • "{claim.text[:50]}..." → {status} ({claim.confidence:.0f}% conf) → {adjustment:+.1f} pts'
            )

        score = max(0.0, min(100.0, score))

        verified_count = sum(1 for c in claims if c.status == "VERIFIED")
        false_count    = sum(1 for c in claims if c.status == "FALSE")
        total          = len(claims)

        notes = (
            f"{verified_count}/{total} claims verified, {false_count} false. "
            f"Raw claim score: {score:.1f}/100."
        )
        weight = 0.40
        return SubScore("ClaimJudge", score, weight, score * weight, notes)


class SourceJudge:
    """JUDGE 2: Evaluates how many credible news sources corroborate the content."""

    def judge(self, credible_count: int, total_count: int) -> SubScore:
        if credible_count == 0 and total_count == 0:
            return SubScore(
                "SourceJudge", 5.0, 0.25, 1.25,
                "Zero sources found. Cannot corroborate any claims."
            )

        tier1_contribution  = min(credible_count, 3) * 20.0
        other_contribution  = min(max(total_count - credible_count, 0), 3) * 8.0
        diversity_bonus     = 8.0 if total_count >= 5 else (4.0 if total_count >= 3 else 0.0)

        raw   = tier1_contribution + other_contribution + diversity_bonus
        score = min(raw, 100.0)

        if total_count == 1:
            score = max(score - 15.0, 10.0)

        notes = (
            f"{credible_count} Tier-1 source(s) found out of {total_count} total. "
            f"Source quality score: {score:.1f}/100."
        )
        weight = 0.25
        return SubScore("SourceJudge", score, weight, score * weight, notes)


class LanguageJudge:
    """JUDGE 3: Analyses the writing style for misinformation red flags."""

    def judge(self, text: str) -> tuple[SubScore, List[str]]:
        if not text or not text.strip():
            return SubScore("LanguageJudge", 50.0, 0.15, 7.5, "No text to analyse."), []

        score = 100.0
        triggered_flags: List[str] = []
        upper_text = text.upper()
        details = []

        urgency_hits = sum(
            1 for p in URGENCY_PATTERNS if re.search(p, upper_text, re.IGNORECASE)
        )
        if urgency_hits > 0:
            deduction = min(urgency_hits * 8.0, 30.0)
            score -= deduction
            triggered_flags.append("URGENCY_LANGUAGE")
            details.append(f"Urgency patterns: {urgency_hits} hits → -{deduction:.0f} pts")

        emotional_hits = sum(
            1 for p in EMOTIONAL_PATTERNS if re.search(p, text, re.IGNORECASE)
        )
        if emotional_hits >= 3:
            deduction = min(emotional_hits * 4.0, 20.0)
            score -= deduction
            triggered_flags.append("EMOTIONAL_MANIPULATION")
            details.append(f"Emotional language: {emotional_hits} hits → -{deduction:.0f} pts")

        clickbait_hits = sum(
            1 for p in CLICKBAIT_PATTERNS if re.search(p, text, re.IGNORECASE)
        )
        if clickbait_hits > 0:
            deduction = min(clickbait_hits * 7.0, 20.0)
            score -= deduction
            triggered_flags.append("CLICKBAIT_HEADLINE")
            details.append(f"Clickbait patterns: {clickbait_hits} hits → -{deduction:.0f} pts")

        words = text.split()
        caps_words = [w for w in words if w.isupper() and len(w) >= 3]
        caps_ratio = len(caps_words) / max(len(words), 1)
        if caps_ratio > 0.20:
            score -= 12.0
            triggered_flags.append("ALL_CAPS_ABUSE")
            details.append(f"ALL CAPS ratio: {caps_ratio:.0%} → -12 pts")

        vague_hits = sum(
            1 for p in VAGUE_ATTRIBUTION_PATTERNS if re.search(p, text, re.IGNORECASE)
        )
        if vague_hits >= 2:
            score -= min(vague_hits * 5.0, 15.0)
            triggered_flags.append("VAGUE_CLAIMS")
            details.append(f"Vague attribution: {vague_hits} hits → penalty applied")

        anon_hits = sum(
            1 for p in [r"\bsources?\s+say\b", r"\binsiders?\b", r"\baccording\s+to\s+sources\b"]
            if re.search(p, text, re.IGNORECASE)
        )
        if anon_hits >= 1:
            score -= min(anon_hits * 5.0, 12.0)
            triggered_flags.append("ANONYMOUS_SOURCE")

        official_hits = sum(
            1 for p in OFFICIAL_SOURCE_PATTERNS if re.search(p, text, re.IGNORECASE)
        )
        if official_hits >= 1:
            score += min(official_hits * 5.0, 15.0)
            triggered_flags.append("OFFICIAL_SOURCE")
            details.append(f"Official source references: {official_hits} → +{min(official_hits * 5, 15)} pts")

        score = max(0.0, min(100.0, score))

        notes  = "; ".join(details) if details else "Language appears neutral."
        weight = 0.15
        return SubScore("LanguageJudge", score, weight, score * weight, notes), triggered_flags


class FactCheckJudge:
    """JUDGE 4: Checks whether any claims have been independently fact-checked."""

    STATUS_ADJUSTMENTS = {
        "VERIFIED":  +30.0,
        "DISPUTED":  -20.0,
        "DEBUNKED":  -40.0,
    }

    def judge(self, fact_check_matches: List[str]) -> tuple[SubScore, List[str]]:
        if not fact_check_matches:
            return SubScore(
                "FactCheckJudge", 50.0, 0.20, 10.0,
                "No fact-check matches found. Neither confirms nor denies credibility."
            ), []

        total_adjustment = 0.0
        triggered_flags: List[str] = []

        debunked_count = fact_check_matches.count("DEBUNKED")
        disputed_count = fact_check_matches.count("DISPUTED")
        verified_count = fact_check_matches.count("VERIFIED")

        for status in fact_check_matches:
            total_adjustment += self.STATUS_ADJUSTMENTS.get(status.upper(), 0.0)

        score = max(0.0, min(100.0, 50.0 + total_adjustment))

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
        weight = 0.20
        return SubScore("FactCheckJudge", score, weight, score * weight, notes), triggered_flags


class FlagInspector:
    """JUDGE 5: Issues compound warning flags from combinations of signals."""

    def inspect(
        self,
        claims: List[ClaimInput],
        credible_source_count: int,
        total_source_count: int,
        existing_flags: List[str],
    ) -> List[str]:
        compound_flags: List[str] = []

        verified_count   = sum(1 for c in claims if c.status == "VERIFIED")
        false_count      = sum(1 for c in claims if c.status == "FALSE")
        unverified_count = sum(1 for c in claims if c.status == "UNVERIFIED")
        total_claims     = len(claims)

        has_urgency    = "URGENCY_LANGUAGE" in existing_flags
        has_emotional  = "EMOTIONAL_MANIPULATION" in existing_flags
        has_no_source  = "NO_CREDIBLE_SOURCE" in existing_flags
        has_fact_fail  = "FACT_CHECK_FAILED" in existing_flags

        # POSSIBLE_RUMOR: no credible sources + urgency + mostly unverified
        rumor_signals = sum([
            has_no_source,
            has_urgency,
            total_source_count == 0,
            (unverified_count / max(total_claims, 1)) >= 0.6,
        ])
        if rumor_signals >= 3:
            compound_flags.append("POSSIBLE_RUMOR")

        # VIRAL_MISINFORMATION_PATTERN: clickbait + urgency + emotional + false claims
        viral_signals = sum([
            "CLICKBAIT_HEADLINE" in existing_flags,
            has_urgency,
            has_emotional,
            false_count > 0,
        ])
        if viral_signals >= 3:
            compound_flags.append("VIRAL_MISINFORMATION_PATTERN")

        if false_count > 0:
            compound_flags.append("CONTRADICTED_CLAIMS")

        if total_claims > 0 and (unverified_count / total_claims) >= 0.5:
            compound_flags.append("UNVERIFIED_CLAIMS")

        if credible_source_count == 0 and total_source_count == 0:
            compound_flags.append("NO_CREDIBLE_SOURCE")
        elif total_source_count == 1:
            compound_flags.append("SINGLE_SOURCE")
        elif total_source_count <= 2 and credible_source_count == 0:
            compound_flags.append("LOW_SOURCE_COUNT")

        # Positive flag
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

# FIX F-01: Verdict codes now match frontend VERDICT_CONFIG keys exactly.
# Scores: credible ≥82, mostly_credible ≥65, questionable ≥48, likely_false ≥28, false ≥0
VERDICT_MAP: List[tuple] = [
    (82, "credible",        "Credible",          "#14b8a6"),  # teal-500
    (65, "mostly_credible", "Mostly Credible",   "#22c55e"),  # green-500
    (48, "questionable",    "Questionable",      "#f59e0b"),  # amber-500
    (28, "likely_false",    "Likely False",      "#f97316"),  # orange-500
    (0,  "false",           "False / Fabricated","#ef4444"),  # red-500
]


def compute_score(inp: ScoringInput) -> ScoringResult:
    """
    THE MAIN FUNCTION — call this from your FastAPI route.

    Takes a ScoringInput, runs all five judges, aggregates results,
    applies flag penalties, and returns a complete ScoringResult.
    """
    logger.info(
        f"[ScoringEngine] Starting. claims={len(inp.claims)}, "
        f"sources={inp.total_source_count} (credible={inp.credible_source_count}), "
        f"fact_checks={len(inp.fact_check_matches)}"
    )

    claim_sub                    = ClaimJudge().judge(inp.claims)
    source_sub                   = SourceJudge().judge(inp.credible_source_count, inp.total_source_count)
    lang_sub,   lang_flags       = LanguageJudge().judge(inp.original_content)
    fc_sub,     fc_flags         = FactCheckJudge().judge(inp.fact_check_matches)

    preliminary_flags = lang_flags + fc_flags
    compound_flags    = FlagInspector().inspect(
        inp.claims,
        inp.credible_source_count,
        inp.total_source_count,
        preliminary_flags,
    )

    all_flag_codes: List[str] = list(dict.fromkeys(
        preliminary_flags + compound_flags
    ))

    weighted_score = (
        claim_sub.contribution
        + source_sub.contribution
        + lang_sub.contribution
        + fc_sub.contribution
    )

    penalty_total = 0.0
    for code in all_flag_codes:
        flag_def = FLAGS.get(code)
        if flag_def and flag_def.score_penalty > 0:
            scaling        = 0.6 if len(all_flag_codes) > 3 else 1.0
            penalty_total += flag_def.score_penalty * scaling

    final_score = max(0.0, min(100.0, weighted_score - penalty_total))

    verdict, verdict_label, verdict_color = _score_to_verdict(final_score)

    flag_details = []
    for code in all_flag_codes:
        fdef = FLAGS.get(code)
        if fdef:
            flag_details.append({
                "code":          fdef.code,
                "label":         fdef.label,
                "description":   fdef.description,
                "severity":      fdef.severity.value,
                "score_penalty": fdef.score_penalty,
            })

    confidence_level = _compute_confidence_level(inp, all_flag_codes)

    claims_breakdown = {
        "total":      len(inp.claims),
        "verified":   sum(1 for c in inp.claims if c.status == "VERIFIED"),
        "false":      sum(1 for c in inp.claims if c.status == "FALSE"),
        "disputed":   sum(1 for c in inp.claims if c.status == "DISPUTED"),
        "unverified": sum(1 for c in inp.claims if c.status == "UNVERIFIED"),
    }

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
    return "false", "False / Fabricated", "#ef4444"


def _compute_confidence_level(inp: ScoringInput, flags: List[str]) -> str:
    score = 0

    if len(inp.claims) >= 3:
        score += 2
    elif len(inp.claims) >= 1:
        score += 1

    if inp.total_source_count >= 5:
        score += 3
    elif inp.total_source_count >= 3:
        score += 2
    elif inp.total_source_count >= 1:
        score += 1

    if inp.fact_check_matches:
        score += 3

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
    total     = breakdown["total"]
    verified  = breakdown["verified"]
    false_cnt = breakdown["false"]
    disputed  = breakdown["disputed"]

    parts = []

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

    if total > 0:
        parts.append(
            f"Of {total} factual claim(s) checked: "
            f"{verified} verified, {false_cnt} false, {disputed} disputed."
        )

    danger_flags  = [f for f in flags if FLAGS.get(f) and FLAGS[f].severity == FlagSeverity.HIGH]
    warning_flags = [f for f in flags if FLAGS.get(f) and FLAGS[f].severity == FlagSeverity.MEDIUM]

    if danger_flags:
        labels = [FLAGS[f].label for f in danger_flags[:2]]
        parts.append(f"Key concerns: {', '.join(labels)}.")
    elif warning_flags:
        labels = [FLAGS[f].label for f in warning_flags[:2]]
        parts.append(f"Caution: {', '.join(labels)}.")

    return " ".join(parts)
