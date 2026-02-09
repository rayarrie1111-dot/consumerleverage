"""
Credit Report PDF Parser
────────────────────────
Extracts structured account data from credit report PDFs.

Pipeline:
  1. pdfplumber extracts raw text from the PDF
  2. Claude (Sonnet) structures the raw text into JSON
  3. Pydantic validates the output schema

Handles all three bureau formats (Equifax, Experian, TransUnion).
"""

import io
import json
import logging

import anthropic
import pdfplumber
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.models.schemas import Account, AuditEntry, Bureau

logger = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


# ─── Output schema for the parser ────────────────────────────────

class PersonalInfo(BaseModel):
    name: str | None = None
    address: str | None = None
    city_state_zip: str | None = None
    ssn_last4: str | None = None
    dob: str | None = None


class Inquiry(BaseModel):
    inquirer: str
    date: str | None = None
    type: str | None = None


class ParsedReport(BaseModel):
    personal_info: PersonalInfo = PersonalInfo()
    accounts: list[Account] = []
    inquiries: list[Inquiry] = []


# ─── Text extraction ─────────────────────────────────────────────

def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract all text from a credit report PDF."""
    text_parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)

    raw_text = "\n\n".join(text_parts)

    if not raw_text.strip():
        raise ValueError("No text could be extracted from the PDF. The file may be image-based or corrupted.")

    return raw_text


# ─── AI-assisted structuring ─────────────────────────────────────

PARSER_SYSTEM = """You are a credit report parser. Extract ALL account information from the credit report text into structured JSON.

You must handle reports from Equifax, Experian, and TransUnion — each has a different layout.

Return ONLY valid JSON matching this exact schema:
{{
  "personal_info": {{
    "name": "consumer full name or null",
    "address": "street address or null",
    "city_state_zip": "city, state zip or null",
    "ssn_last4": "last 4 digits or null",
    "dob": "date of birth or null"
  }},
  "accounts": [
    {{
      "creditor": "creditor/company name (REQUIRED)",
      "account_number_partial": "partial account number if shown, or null",
      "status": "Open, Closed, Collection, Charged Off, Paid, Late, Current, etc.",
      "balance": 0.00,
      "date_opened": "YYYY-MM-DD or null",
      "date_reported": "YYYY-MM-DD or null",
      "payment_history": "description of payment pattern, e.g. '30-60-90 late' or 'Current/Never Late'",
      "remarks": "any remarks, comments, or special notes on the account",
      "original_creditor": "original creditor name if this is a collection/transferred account, or null"
    }}
  ],
  "inquiries": [
    {{
      "inquirer": "company name",
      "date": "YYYY-MM-DD or null",
      "type": "hard or soft"
    }}
  ]
}}

RULES:
- Extract EVERY account listed on the report. Do not skip any.
- Use null for fields that are not present or cannot be determined.
- For balance, use 0 if the balance is shown as $0 or paid off.
- Normalize dates to YYYY-MM-DD format where possible.
- For status, use the exact wording from the report.
- If a field value is unclear, include it as-is rather than guessing.
- Do NOT add accounts that don't appear in the report.
- Do NOT provide conversational text. Output ONLY the JSON."""


async def parse_credit_report(
    file_bytes: bytes,
    bureau: str,
    dispute_id: str | None = None,
) -> ParsedReport:
    """
    Parse a credit report PDF into structured data.

    Args:
        file_bytes: Raw PDF file content.
        bureau: Which bureau this report is from.
        dispute_id: Optional dispute ID for audit logging.

    Returns:
        ParsedReport with personal info, accounts, and inquiries.
    """
    # 1. Extract raw text
    raw_text = extract_text_from_pdf(file_bytes)
    logger.info(f"Extracted {len(raw_text)} chars from {bureau} PDF")

    # 2. Truncate if too long (Claude's context handles ~200k tokens but
    #    credit reports are rarely over 50 pages)
    max_chars = 150_000
    if len(raw_text) > max_chars:
        logger.warning(f"Report text truncated from {len(raw_text)} to {max_chars} chars")
        raw_text = raw_text[:max_chars]

    # 3. Send to Claude for structuring
    client = _get_client()
    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=8192,
        system=PARSER_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Parse this {bureau.title()} credit report:\n\n{raw_text}",
        }],
    )

    raw_response = response.content[0].text
    tokens_used = response.usage.input_tokens + response.usage.output_tokens

    # 4. Parse JSON response
    parsed_json = _parse_json_response(raw_response)

    # 5. Validate with Pydantic
    try:
        report = ParsedReport(**parsed_json)
    except ValidationError as e:
        logger.error(f"Validation failed for parsed report: {e}")
        # Try to salvage accounts even if other fields fail
        report = ParsedReport(
            accounts=[
                Account(**a) for a in parsed_json.get("accounts", [])
                if "creditor" in a
            ]
        )

    # 6. Tag each account with the bureau
    for account in report.accounts:
        account.bureau = Bureau(bureau)

    logger.info(f"Parsed {len(report.accounts)} accounts from {bureau} report")

    # 7. Audit log
    if dispute_id:
        from app.models.database import get_supabase
        try:
            get_supabase().table("ai_audit_log").insert({
                "dispute_id": dispute_id,
                "step": "parse",
                "input_data": {"bureau": bureau, "text_length": len(raw_text)},
                "output_data": {"accounts_found": len(report.accounts)},
                "model": "claude-sonnet-4-5-20250929",
                "tokens_used": tokens_used,
            }).execute()
        except Exception as e:
            logger.warning(f"Failed to write parse audit log: {e}")

    return report


def _parse_json_response(text: str) -> dict:
    """Parse JSON from Claude's response, handling markdown fences."""
    cleaned = text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse report JSON: {e}\nRaw: {text[:500]}")
        return {"personal_info": {}, "accounts": [], "inquiries": []}
