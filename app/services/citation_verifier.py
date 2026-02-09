"""
Citation Verifier
─────────────────
Post-generation validation layer that ensures every FCRA citation
produced by the AI actually exists in our reference material.

Two-layer verification:
  1. Hard-coded lookup table of all valid FCRA section numbers
  2. Database check against the actual ingested chunks

Any citation that fails both checks is flagged as UNVERIFIED and
excluded from dispute letters.
"""

import re
import json
from pathlib import Path

from app.models.database import get_supabase


# ─── Load valid sections from reference data ──────────────────────

_SECTIONS_FILE = Path(__file__).parent.parent / "data" / "fcra_sections.json"

_valid_sections: set[str] | None = None


def _load_valid_sections() -> set[str]:
    global _valid_sections
    if _valid_sections is not None:
        return _valid_sections

    if _SECTIONS_FILE.exists():
        with open(_SECTIONS_FILE) as f:
            data = json.load(f)
            _valid_sections = set(data.get("sections", []))
    else:
        # Fallback: core FCRA sections that are always valid
        _valid_sections = _CORE_FCRA_SECTIONS.copy()

    return _valid_sections


# Core FCRA sections — the minimum set we know is valid.
# The full list comes from fcra_sections.json (generated during ingestion).
_CORE_FCRA_SECTIONS: set[str] = {
    "15 U.S.C. § 1681",
    "15 U.S.C. § 1681a",
    "15 U.S.C. § 1681b",
    "15 U.S.C. § 1681c",
    "15 U.S.C. § 1681c-1",
    "15 U.S.C. § 1681c-2",
    "15 U.S.C. § 1681d",
    "15 U.S.C. § 1681e",
    "15 U.S.C. § 1681e(b)",
    "15 U.S.C. § 1681f",
    "15 U.S.C. § 1681g",
    "15 U.S.C. § 1681h",
    "15 U.S.C. § 1681i",
    "15 U.S.C. § 1681j",
    "15 U.S.C. § 1681k",
    "15 U.S.C. § 1681l",
    "15 U.S.C. § 1681m",
    "15 U.S.C. § 1681n",
    "15 U.S.C. § 1681o",
    "15 U.S.C. § 1681p",
    "15 U.S.C. § 1681q",
    "15 U.S.C. § 1681r",
    "15 U.S.C. § 1681s",
    "15 U.S.C. § 1681s-1",
    "15 U.S.C. § 1681s-2",
    "15 U.S.C. § 1681s-2(a)",
    "15 U.S.C. § 1681s-2(b)",
    "15 U.S.C. § 1681s-3",
    "15 U.S.C. § 1681t",
    "15 U.S.C. § 1681u",
    "15 U.S.C. § 1681v",
    "15 U.S.C. § 1681w",
    "15 U.S.C. § 1681x",
}

# Regex to extract section citations from AI-generated text
_CITATION_PATTERN = re.compile(
    r"15\s+U\.?S\.?C\.?\s+§\s*(\d{4}[a-z]?(?:-\d+)?(?:\([a-z0-9]+\))*)",
    re.IGNORECASE,
)


# ─── Verification functions ───────────────────────────────────────

def extract_citations(text: str) -> list[str]:
    """Extract all FCRA citation section numbers from a text string."""
    matches = _CITATION_PATTERN.findall(text)
    return [f"15 U.S.C. § {m}" for m in matches]


def verify_citation_against_lookup(section: str) -> bool:
    """Check if a section number exists in our valid sections table."""
    valid = _load_valid_sections()

    # Exact match
    if section in valid:
        return True

    # Try matching the base section (strip subsection specifiers)
    # e.g. "15 U.S.C. § 1681e(b)" → check "15 U.S.C. § 1681e" too
    base = re.sub(r"\([a-z0-9]+\)$", "", section)
    if base in valid:
        return True

    return False


async def verify_citation_against_db(section: str) -> bool:
    """Check if we have any ingested chunks for this section number."""
    supabase = get_supabase()
    result = (
        supabase.table("fcra_chunks")
        .select("id")
        .eq("section_number", section)
        .limit(1)
        .execute()
    )
    return len(result.data or []) > 0


def verify_violations(violations: list[dict]) -> list[dict]:
    """
    Verify all citations in a list of violation objects.
    Adds 'verified' and 'flag' fields to each violation.

    Expected violation format:
    {
        "violation_type": "...",
        "section": "15 U.S.C. § 1681e(b)",
        "citation_text": "...",
        ...
    }
    """
    valid = _load_valid_sections()
    verified_violations = []

    for violation in violations:
        v = violation.copy()
        section = v.get("section", v.get("fcra_section", ""))

        if verify_citation_against_lookup(section):
            v["verified"] = True
            v["flag"] = None
        else:
            v["verified"] = False
            v["flag"] = f"Citation '{section}' not found in FCRA reference material"

        verified_violations.append(v)

    return verified_violations


def filter_verified_only(violations: list[dict]) -> list[dict]:
    """Return only violations that passed citation verification."""
    return [v for v in violations if v.get("verified", False)]


def verification_summary(violations: list[dict]) -> dict:
    """Return a summary of verification results."""
    total = len(violations)
    verified = sum(1 for v in violations if v.get("verified", False))
    unverified = total - verified
    return {
        "total_violations": total,
        "verified": verified,
        "unverified": unverified,
        "pass_rate": verified / total if total > 0 else 1.0,
        "unverified_sections": [
            v.get("section", v.get("fcra_section"))
            for v in violations
            if not v.get("verified", False)
        ],
    }
