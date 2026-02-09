"""
Credit Repair AI Engine
───────────────────────
Orchestrates the 3-step Claude prompt chain:

  Step A: Identify FCRA violations (RAG-grounded)
  Step B: Ground each violation with verbatim legal citations
  Step C: Draft the dispute letter from verified citations only

Each step is a separate Claude API call with a narrowly scoped system
prompt. This prevents hallucination and keeps each step auditable.

Every AI call is logged to the ai_audit_log table for compliance.
"""

import json
import logging
import time

import anthropic

from app.config import settings
from app.models.schemas import (
    Account,
    AuditEntry,
    Bureau,
    DisputeResult,
    StepAOutput,
    StepBOutput,
    StepCOutput,
    Violation,
)
from app.services.rag import get_relevant_fcra_sections, format_context_for_prompt
from app.services.citation_verifier import (
    verify_violations,
    filter_verified_only,
    verification_summary,
)
from app.services.universal_truths import (
    check_single_bureau,
    format_flags_for_prompt,
    UniversalTruthFlag,
)
from app.models.database import get_supabase

logger = logging.getLogger(__name__)

# ─── Claude client ────────────────────────────────────────────────

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


# Model selection: Opus for analysis accuracy, Sonnet for letter drafting speed
MODEL_ANALYSIS = "claude-opus-4-6"
MODEL_DRAFTING = "claude-sonnet-4-5-20250929"


# ─── Audit logging ────────────────────────────────────────────────

def _log_audit(entry: AuditEntry) -> None:
    """Store an AI call in the audit log for compliance and debugging."""
    try:
        supabase = get_supabase()
        supabase.table("ai_audit_log").insert({
            "dispute_id": entry.dispute_id,
            "step": entry.step,
            "input_data": entry.input_data,
            "output_data": entry.output_data,
            "model": entry.model,
            "tokens_used": entry.tokens_used,
        }).execute()
    except Exception as e:
        logger.warning(f"Failed to write audit log: {e}")


# ─── System prompts ──────────────────────────────────────────────

STEP_A_SYSTEM = """You are a Senior FCRA Compliance Auditor. Your sole objective is to identify discrepancies in a consumer's credit report by cross-referencing account data against the "Universal Truths" of the Fair Credit Reporting Act.

THE UNIVERSAL TRUTHS:

1. ACCURACY (15 U.S.C. § 1681e(b)):
   Every data point — balance, date, status, payment history — must be 100% correct across all bureaus. Any discrepancy is a potential violation.

2. COMPLETENESS (15 U.S.C. § 1681e(b), § 1681i):
   No required fields should be missing or logically inconsistent. A credit report that omits material information is incomplete and therefore inaccurate.

3. VERIFIABILITY (15 U.S.C. § 1681i, Section 611):
   If a furnisher cannot prove the data is correct with documentation, it must be deleted. Missing original creditor on a collection account, missing account numbers, and absent payment histories all fail the verifiability test.

YOUR TASK:
Analyze the provided account_data and the fcra_context (RAG chunks). Identify every technical violation. Cross-reference any pre-identified discrepancies provided below with the FCRA reference material to determine which statutory sections apply.

CRITICAL RULES:
- ONLY cite violations supported by the FCRA reference material provided below.
- If no violation exists, return {{"violations": []}}.
- Do NOT use your general knowledge of FCRA — ONLY use the reference material below.
- Do NOT provide conversational filler. Output structured JSON only.
- Tag each violation with which Universal Truth it falls under.

Return ONLY valid JSON matching this exact schema:
{{
  "violations": [
    {{
      "violation_type": "category (e.g. 'inaccurate reporting', 'incomplete data', 'unverifiable account', 'obsolete information', 'failure to investigate')",
      "fcra_section": "15 U.S.C. § XXXX",
      "universal_truth": "accuracy | completeness | verifiability",
      "data_point": "the specific field that triggered this violation (e.g. 'balance', 'date_opened', 'original_creditor')",
      "description": "plain language explanation of the violation, referencing the specific data discrepancy",
      "confidence": 0.0 to 1.0
    }}
  ]
}}

{universal_truths_flags}

FCRA REFERENCE MATERIAL:
{fcra_context}"""

STEP_B_SYSTEM = """You are a legal citation engine for the Fair Credit Reporting Act (FCRA).

Your task: For each identified violation, provide the EXACT verbatim statutory text from the FCRA reference material.

CRITICAL RULES:
- Quote the statute VERBATIM from the reference material provided below.
- Include the full section number, subsection letter/number, and exact wording.
- If the cited section does NOT appear in the reference material, set citation_text to "CITATION NOT FOUND".
- Do NOT paraphrase, summarize, or fabricate statutory language.
- Do NOT use your general knowledge — ONLY quote from the reference material below.

Return ONLY valid JSON matching this exact schema:
{{
  "violations": [
    {{
      "violation_type": "...",
      "fcra_section": "15 U.S.C. § XXXX",
      "description": "...",
      "citation_text": "exact verbatim quote from the statute or 'CITATION NOT FOUND'",
      "confidence": 0.0 to 1.0
    }}
  ]
}}

FCRA REFERENCE MATERIAL:
{fcra_context}"""

STEP_C_SYSTEM = """You are a consumer rights dispute letter writer specializing in FCRA-based credit report disputes.

Your task: Draft a professional dispute letter to the specified credit bureau using ONLY the verified violations and citations provided.

LETTER REQUIREMENTS:
- Address the letter to the credit bureau's dispute department.
- Use a firm, professional, and factual tone.
- Reference each violation with its exact FCRA legal citation.
- State the specific relief requested (investigation, correction, deletion).
- Include a 30-day deadline for response per FCRA requirements.
- Use these placeholders for consumer info:
    {{CONSUMER_NAME}}, {{CONSUMER_ADDRESS}}, {{CONSUMER_CITY_STATE_ZIP}},
    {{SSN_LAST4}}, {{DOB}}, {{CURRENT_DATE}}

CRITICAL RULES:
- Do NOT add legal claims, violations, or citations beyond what is provided.
- Do NOT reference laws other than FCRA unless the violations explicitly cite them.
- Do NOT include legal advice or attorney disclaimers — this is a consumer self-help letter.
- Keep the letter concise and focused on the specific account and violations.

BUREAU DISPUTE ADDRESSES:
- Equifax: P.O. Box 740256, Atlanta, GA 30374-0256
- Experian: P.O. Box 4500, Allen, TX 75013
- TransUnion: P.O. Box 2000, Chester, PA 19016"""


# ─── The Engine ───────────────────────────────────────────────────

class CreditRepairEngine:
    """
    Orchestrates the full dispute generation pipeline.

    Usage:
        engine = CreditRepairEngine()
        result = await engine.generate_dispute(account, bureau="equifax")
    """

    def __init__(self, dispute_id: str | None = None):
        self.dispute_id = dispute_id
        self.client = _get_client()

    async def generate_dispute(
        self,
        account: Account,
        bureau: Bureau,
        universal_truths_flags: list[UniversalTruthFlag] | None = None,
    ) -> DisputeResult:
        """
        Run the full 3-step pipeline for a single account and bureau.

        Args:
            account: The parsed credit report account data.
            bureau: Which bureau to address the dispute to.
            universal_truths_flags: Pre-computed flags from the cross-bureau
                Universal Truths checker. If None, single-bureau checks run
                automatically inside Step A.

        Returns a DisputeResult with violations, letter, and audit metadata.
        """
        result = DisputeResult(
            account=account,
            bureau=bureau,
        )

        try:
            # ── Step A: Identify violations (with Universal Truths context) ──
            step_a = await self._step_a_identify(account, universal_truths_flags)
            result.steps_completed.append("identify")

            if not step_a.violations:
                result.verification_summary = {"total_violations": 0}
                return result

            # ── Step B: Ground citations ──
            step_b = await self._step_b_cite(step_a, account)
            result.steps_completed.append("cite")

            # ── Verify citations ──
            verified = verify_violations(
                [v.model_dump() for v in step_b.violations]
            )
            valid_only = filter_verified_only(verified)
            result.violations = [Violation(**v) for v in verified]
            result.verification_summary = verification_summary(verified)

            if not valid_only:
                # All citations failed verification — no letter
                logger.warning(
                    f"All citations failed verification for {account.creditor}"
                )
                return result

            # ── Step C: Draft letter ──
            step_c = await self._step_c_draft(valid_only, account, bureau)
            result.letter_content = step_c.letter_content
            result.steps_completed.append("draft")

        except Exception as e:
            logger.error(f"Engine error for {account.creditor}: {e}", exc_info=True)
            result.error = str(e)

        return result

    # ─── Step A: Violation Identification ─────────────────────────

    async def _step_a_identify(
        self,
        account: Account,
        precomputed_flags: list[UniversalTruthFlag] | None = None,
    ) -> StepAOutput:
        """
        Identify potential FCRA violations for an account.

        If precomputed_flags (from the Universal Truths checker) are provided,
        they are injected into the system prompt as concrete evidence for the
        auditor to cross-reference against FCRA statute.
        """
        description = account.to_description()

        # Run single-bureau Universal Truths checks if no flags provided
        if precomputed_flags is None:
            bureau_name = account.bureau.value if account.bureau else "unknown"
            precomputed_flags = check_single_bureau([account], bureau_name)

        # Filter flags relevant to this specific account
        account_flags = [
            f for f in precomputed_flags
            if f.account_creditor.strip().lower() == account.creditor.strip().lower()
        ]
        truths_text = format_flags_for_prompt(account_flags)

        # RAG: retrieve relevant FCRA sections
        # Include Universal Truth findings in the RAG query for better section retrieval
        rag_query = description
        if account_flags:
            truth_types = set(f.truth for f in account_flags)
            rag_query += f" FCRA violations: {' '.join(truth_types)}"
            # Add specific fields that failed checks for targeted retrieval
            flagged_fields = set(f.field for f in account_flags)
            rag_query += f" disputed fields: {' '.join(flagged_fields)}"

        chunks = await get_relevant_fcra_sections(rag_query)
        fcra_context = format_context_for_prompt(chunks)
        sections_consulted = [c.section_number for c in chunks]

        system_prompt = STEP_A_SYSTEM.format(
            fcra_context=fcra_context,
            universal_truths_flags=truths_text,
        )
        user_prompt = (
            f"Analyze this credit report account for FCRA violations:\n\n"
            f"{json.dumps(account.model_dump(mode='json', exclude_none=True), indent=2)}"
        )

        response = self.client.messages.create(
            model=MODEL_ANALYSIS,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = response.content[0].text
        tokens_used = response.usage.input_tokens + response.usage.output_tokens

        # Parse response
        parsed = self._parse_json(raw_text)
        violations = [
            Violation(**v)
            for v in parsed.get("violations", [])
        ]

        # Audit log
        _log_audit(AuditEntry(
            dispute_id=self.dispute_id,
            step="identify",
            input_data={"account": account.model_dump(mode="json", exclude_none=True)},
            output_data=parsed,
            model=MODEL_ANALYSIS,
            tokens_used=tokens_used,
        ))

        return StepAOutput(
            violations=violations,
            account_summary=description,
            fcra_sections_consulted=sections_consulted,
            universal_truths_flags=account_flags,
        )

    # ─── Step B: Legal Citation Grounding ─────────────────────────

    async def _step_b_cite(
        self,
        step_a: StepAOutput,
        account: Account,
    ) -> StepBOutput:
        """Ground each violation with verbatim FCRA statutory text."""

        # Targeted RAG: search specifically for the cited sections
        section_query = " ".join(
            v.fcra_section for v in step_a.violations
        )
        combined_query = f"{account.to_description()} {section_query}"
        chunks = await get_relevant_fcra_sections(combined_query, top_k=12)
        fcra_context = format_context_for_prompt(chunks)

        system_prompt = STEP_B_SYSTEM.format(fcra_context=fcra_context)
        user_prompt = (
            f"Provide exact FCRA statutory citations for these violations:\n\n"
            f"{json.dumps([v.model_dump(exclude_none=True) for v in step_a.violations], indent=2)}"
        )

        response = self.client.messages.create(
            model=MODEL_ANALYSIS,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = response.content[0].text
        tokens_used = response.usage.input_tokens + response.usage.output_tokens

        parsed = self._parse_json(raw_text)
        violations = [
            Violation(**v)
            for v in parsed.get("violations", [])
        ]

        # Count how many citations came back vs "CITATION NOT FOUND"
        cited = sum(
            1 for v in violations
            if v.citation_text and v.citation_text != "CITATION NOT FOUND"
        )
        failed = len(violations) - cited

        _log_audit(AuditEntry(
            dispute_id=self.dispute_id,
            step="cite",
            input_data={"violations_in": [v.model_dump() for v in step_a.violations]},
            output_data=parsed,
            model=MODEL_ANALYSIS,
            tokens_used=tokens_used,
        ))

        return StepBOutput(
            violations=violations,
            citations_verified=cited,
            citations_failed=failed,
        )

    # ─── Step C: Letter Drafting ──────────────────────────────────

    async def _step_c_draft(
        self,
        verified_violations: list[dict],
        account: Account,
        bureau: Bureau,
    ) -> StepCOutput:
        """Draft the dispute letter from verified violations only."""

        user_prompt = (
            f"Draft a dispute letter to {bureau.value.title()} for this account.\n\n"
            f"ACCOUNT:\n{json.dumps(account.model_dump(mode='json', exclude_none=True), indent=2)}\n\n"
            f"VERIFIED VIOLATIONS AND CITATIONS:\n{json.dumps(verified_violations, indent=2)}"
        )

        response = self.client.messages.create(
            model=MODEL_DRAFTING,
            max_tokens=4096,
            system=STEP_C_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )

        letter_content = response.content[0].text
        tokens_used = response.usage.input_tokens + response.usage.output_tokens

        _log_audit(AuditEntry(
            dispute_id=self.dispute_id,
            step="draft",
            input_data={
                "bureau": bureau.value,
                "violations_count": len(verified_violations),
            },
            output_data={"letter_length": len(letter_content)},
            model=MODEL_DRAFTING,
            tokens_used=tokens_used,
        ))

        return StepCOutput(
            letter_content=letter_content,
            bureau=bureau,
            violations_addressed=len(verified_violations),
        )

    # ─── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _parse_json(text: str) -> dict:
        """
        Parse JSON from Claude's response, handling markdown code fences.
        """
        cleaned = text.strip()

        # Strip markdown code fences if present
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            # Remove first line (```json) and last line (```)
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI JSON response: {e}\nRaw: {text[:500]}")
            return {"violations": []}
