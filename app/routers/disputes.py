"""
Disputes Router
───────────────
API endpoints for running the AI dispute engine and managing results.
"""

import logging

from fastapi import APIRouter, HTTPException

from app.models.schemas import (
    Account,
    AnalyzeRequest,
    AnalyzeResponse,
    Bureau,
    DisputeResult,
    DisputeStatus,
)
from app.models.database import get_supabase
from app.services.ai_engine import CreditRepairEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/disputes", tags=["disputes"])


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze_disputes(request: AnalyzeRequest):
    """
    Run the full AI dispute pipeline on selected accounts.

    1. Fetches account data from the database
    2. Creates a dispute record
    3. Runs Step A → B → C for each account × bureau combination
    4. Stores violations and letters in the database
    5. Returns the complete results
    """
    supabase = get_supabase()

    # Create dispute record
    dispute = supabase.table("disputes").insert({
        "user_id": None,  # TODO: wire in auth
        "status": DisputeStatus.ANALYZING.value,
    }).execute()

    if not dispute.data:
        raise HTTPException(status_code=500, detail="Failed to create dispute record")

    dispute_id = dispute.data[0]["id"]

    # Fetch accounts
    accounts_query = (
        supabase.table("accounts")
        .select("*")
        .in_("id", request.account_ids)
        .execute()
    )

    if not accounts_query.data:
        raise HTTPException(status_code=404, detail="No accounts found for the given IDs")

    # Run the engine
    engine = CreditRepairEngine(dispute_id=dispute_id)
    results: list[DisputeResult] = []
    total_violations = 0
    total_letters = 0

    for account_row in accounts_query.data:
        account = Account(
            id=account_row["id"],
            creditor=account_row["creditor"],
            account_number_partial=account_row.get("account_number_partial"),
            status=account_row.get("status"),
            balance=account_row.get("balance"),
            date_opened=account_row.get("date_opened"),
            date_reported=account_row.get("date_reported"),
            payment_history=account_row.get("payment_history"),
            remarks=account_row.get("remarks"),
        )

        for bureau in request.bureaus:
            result = await engine.generate_dispute(account, bureau)
            results.append(result)

            # Store violations in DB
            for violation in result.violations:
                if violation.verified:
                    total_violations += 1
                supabase.table("violations").insert({
                    "dispute_id": dispute_id,
                    "account_id": account_row["id"],
                    "violation_type": violation.violation_type,
                    "fcra_section": violation.fcra_section,
                    "citation_text": violation.citation_text,
                    "verified": violation.verified,
                    "ai_confidence": violation.confidence,
                }).execute()

            # Store letter in DB
            if result.letter_content:
                total_letters += 1
                supabase.table("letters").insert({
                    "dispute_id": dispute_id,
                    "bureau": bureau.value,
                    "content": result.letter_content,
                    "status": "draft",
                }).execute()

    # Update dispute status
    final_status = DisputeStatus.READY if total_letters > 0 else DisputeStatus.DRAFT
    supabase.table("disputes").update({
        "status": final_status.value,
    }).eq("id", dispute_id).execute()

    return AnalyzeResponse(
        dispute_id=dispute_id,
        status=final_status,
        results=results,
        total_violations=total_violations,
        total_letters=total_letters,
    )


@router.get("/status/{dispute_id}")
async def get_dispute_status(dispute_id: str):
    """Get the current status of a dispute and its results."""
    supabase = get_supabase()

    dispute = (
        supabase.table("disputes")
        .select("*")
        .eq("id", dispute_id)
        .single()
        .execute()
    )

    if not dispute.data:
        raise HTTPException(status_code=404, detail="Dispute not found")

    violations = (
        supabase.table("violations")
        .select("*")
        .eq("dispute_id", dispute_id)
        .execute()
    )

    letters = (
        supabase.table("letters")
        .select("id, bureau, status, version, created_at")
        .eq("dispute_id", dispute_id)
        .execute()
    )

    return {
        "dispute": dispute.data,
        "violations": violations.data or [],
        "letters": letters.data or [],
    }
