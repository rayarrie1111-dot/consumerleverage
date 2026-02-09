"""
Pydantic Schemas
────────────────
Data models for every stage of the dispute pipeline.
Used for API request/response validation and type safety
between the AI engine steps.
"""

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


# ─── Enums ────────────────────────────────────────────────────────

class Bureau(str, Enum):
    EQUIFAX = "equifax"
    EXPERIAN = "experian"
    TRANSUNION = "transunion"


class DisputeStatus(str, Enum):
    DRAFT = "draft"
    ANALYZING = "analyzing"
    READY = "ready"
    SENT = "sent"
    RESPONDED = "responded"


class LetterStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    SENT = "sent"


# ─── Account data (parsed from credit report) ────────────────────

class Account(BaseModel):
    id: str | None = None
    creditor: str
    account_number_partial: str | None = None
    status: str | None = None
    balance: float | None = None
    date_opened: date | None = None
    date_reported: date | None = None
    payment_history: str | None = None
    remarks: str | None = None
    original_creditor: str | None = None
    bureau: Bureau | None = None

    def to_description(self) -> str:
        """Generate a natural language summary for RAG queries."""
        parts = [f"Creditor: {self.creditor}"]
        if self.status:
            parts.append(f"Status: {self.status}")
        if self.balance is not None:
            parts.append(f"Balance: ${self.balance:,.2f}")
        if self.payment_history:
            parts.append(f"Payment history: {self.payment_history}")
        if self.remarks:
            parts.append(f"Remarks: {self.remarks}")
        if self.original_creditor:
            parts.append(f"Original creditor: {self.original_creditor}")
        return " | ".join(parts)


# ─── Violation (output of Step A + B) ────────────────────────────

class Violation(BaseModel):
    violation_type: str = Field(description="Category: e.g. 'inaccurate reporting', 'obsolete data'")
    fcra_section: str = Field(description="e.g. '15 U.S.C. § 1681e(b)'")
    description: str = Field(description="Plain language explanation of the violation")
    universal_truth: str | None = Field(default=None, description="Which truth failed: accuracy, completeness, or verifiability")
    data_point: str | None = Field(default=None, description="The specific field that triggered the violation")
    citation_text: str | None = Field(default=None, description="Verbatim statutory text from FCRA")
    verified: bool = False
    flag: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


# ─── Universal Truths flags (pre-AI rule-based checks) ───────────

class UniversalTruthFlag(BaseModel):
    """A discrepancy found by the deterministic Universal Truths checker."""
    truth: str = Field(description="accuracy | completeness | verifiability")
    field: str = Field(description="The data field that failed the check")
    account_creditor: str
    bureau: Bureau | None = None
    details: str = Field(description="Human-readable description of the discrepancy")
    mismatched_values: dict | None = Field(default=None, description="Bureau → value for cross-bureau mismatches")


class UniversalTruthsReport(BaseModel):
    """Full report from the Universal Truths checker."""
    flags: list[UniversalTruthFlag] = []
    accounts_checked: int = 0
    bureaus_compared: list[str] = []
    accuracy_flags: int = 0
    completeness_flags: int = 0
    verifiability_flags: int = 0


# ─── AI engine step outputs ──────────────────────────────────────

class StepAOutput(BaseModel):
    """Output of Step A: Violation Identification."""
    violations: list[Violation] = []
    account_summary: str = ""
    fcra_sections_consulted: list[str] = []
    universal_truths_flags: list[UniversalTruthFlag] = []


class StepBOutput(BaseModel):
    """Output of Step B: Legal Citation Grounding."""
    violations: list[Violation] = []
    citations_verified: int = 0
    citations_failed: int = 0


class StepCOutput(BaseModel):
    """Output of Step C: Letter Drafting."""
    letter_content: str
    bureau: Bureau
    violations_addressed: int = 0


# ─── Full dispute result ─────────────────────────────────────────

class DisputeResult(BaseModel):
    """Complete result for one account across one bureau."""
    account: Account
    bureau: Bureau
    violations: list[Violation] = []
    letter_content: str | None = None
    verification_summary: dict = {}
    steps_completed: list[str] = []
    error: str | None = None


# ─── API request/response models ─────────────────────────────────

class AnalyzeRequest(BaseModel):
    report_id: str
    account_ids: list[str]
    bureaus: list[Bureau]


class AnalyzeResponse(BaseModel):
    dispute_id: str
    status: DisputeStatus
    results: list[DisputeResult]
    total_violations: int = 0
    total_letters: int = 0


class LetterUpdateRequest(BaseModel):
    content: str


class LetterResponse(BaseModel):
    id: str
    dispute_id: str
    bureau: Bureau
    content: str
    version: int
    status: LetterStatus
    created_at: datetime | None = None


# ─── Audit log entry ─────────────────────────────────────────────

class AuditEntry(BaseModel):
    dispute_id: str | None = None
    step: str  # "parse", "identify", "cite", "draft"
    input_data: dict
    output_data: dict
    model: str
    tokens_used: int = 0
