"""
Webhooks Router
───────────────
Endpoints designed for n8n (or any automation platform) to:
  1. Receive events FROM our system (n8n listens)
  2. Trigger actions IN our system (n8n calls)

These use a shared secret for auth instead of user JWT,
since n8n acts as a service-to-service caller.
"""

import hashlib
import hmac
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.config import settings
from app.models.schemas import Bureau, DisputeStatus
from app.models.database import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


# ─── Webhook auth (shared secret, not user JWT) ──────────────────

async def verify_webhook_secret(
    x_webhook_secret: str | None = Header(None),
) -> None:
    """Verify the n8n webhook secret matches our configured secret."""
    expected = settings.WEBHOOK_SECRET
    if not expected:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")
    if not x_webhook_secret or not hmac.compare_digest(x_webhook_secret, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")


# ─── Models ───────────────────────────────────────────────────────

class DisputeStatusUpdate(BaseModel):
    dispute_id: str
    new_status: DisputeStatus
    notes: str | None = None


class LetterMailedEvent(BaseModel):
    letter_id: str
    tracking_number: str | None = None
    mailed_at: str | None = None
    carrier: str | None = None


class BureauResponseEvent(BaseModel):
    dispute_id: str
    bureau: Bureau
    response_type: str  # "verified", "deleted", "updated", "no_response"
    details: str | None = None
    received_at: str | None = None


class UserNotification(BaseModel):
    user_id: str
    type: str  # "dispute_ready", "letter_mailed", "bureau_responded", "reminder"
    title: str
    body: str
    data: dict | None = None


# ─── n8n → Our System (n8n triggers these) ───────────────────────

@router.post("/dispute-status")
async def update_dispute_status(
    event: DisputeStatusUpdate,
    _: None = Depends(verify_webhook_secret),
):
    """
    n8n calls this to update a dispute's status.
    Use case: n8n tracks the 30-day clock after a letter is sent
    and moves disputes to 'responded' or flags overdue ones.
    """
    supabase = get_supabase()

    result = supabase.table("disputes").update({
        "status": event.new_status.value,
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", event.dispute_id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Dispute not found")

    return {"updated": True, "dispute_id": event.dispute_id, "status": event.new_status.value}


@router.post("/letter-mailed")
async def mark_letter_mailed(
    event: LetterMailedEvent,
    _: None = Depends(verify_webhook_secret),
):
    """
    n8n calls this after sending a letter via mail API (e.g. Lob).
    Updates the letter status and stores tracking info.
    """
    supabase = get_supabase()

    result = supabase.table("letters").update({
        "status": "sent",
    }).eq("id", event.letter_id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Letter not found")

    # Also update the parent dispute status
    letter = result.data[0]
    supabase.table("disputes").update({
        "status": "sent",
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", letter["dispute_id"]).execute()

    return {"updated": True, "letter_id": event.letter_id}


@router.post("/bureau-response")
async def log_bureau_response(
    event: BureauResponseEvent,
    _: None = Depends(verify_webhook_secret),
):
    """
    n8n calls this when a bureau responds to a dispute.
    Could be triggered by email parsing, manual entry, or a timer.
    """
    supabase = get_supabase()

    supabase.table("disputes").update({
        "status": "responded",
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", event.dispute_id).execute()

    return {
        "logged": True,
        "dispute_id": event.dispute_id,
        "bureau": event.bureau.value,
        "response_type": event.response_type,
    }


# ─── Our System → n8n (our system fires these, n8n listens) ──────
# These are called internally when events happen. n8n picks them up
# via its webhook trigger node pointing at these endpoints.

@router.get("/pending-letters")
async def get_pending_letters(
    _: None = Depends(verify_webhook_secret),
):
    """
    n8n polls this to find letters that are approved and ready to send.
    n8n workflow: poll every hour → find approved letters → send via Lob → mark as sent
    """
    supabase = get_supabase()

    letters = (
        supabase.table("letters")
        .select("id, dispute_id, bureau, content, created_at")
        .eq("status", "approved")
        .execute()
    )

    return {"letters": letters.data or [], "count": len(letters.data or [])}


@router.get("/overdue-disputes")
async def get_overdue_disputes(
    days: int = 30,
    _: None = Depends(verify_webhook_secret),
):
    """
    n8n polls this to find disputes where the bureau hasn't responded
    within the FCRA-required timeframe (30 days).
    n8n workflow: poll daily → find overdue → send reminder or escalation email
    """
    supabase = get_supabase()
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    disputes = (
        supabase.table("disputes")
        .select("id, user_id, status, updated_at")
        .eq("status", "sent")
        .lt("updated_at", cutoff)
        .execute()
    )

    return {"overdue_disputes": disputes.data or [], "count": len(disputes.data or [])}


@router.get("/dispute-summary/{dispute_id}")
async def get_dispute_summary_for_n8n(
    dispute_id: str,
    _: None = Depends(verify_webhook_secret),
):
    """
    n8n calls this to get a full dispute summary for notification emails.
    Returns dispute + violations + letters in one call.
    """
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
        .select("violation_type, fcra_section, universal_truth, verified, description")
        .eq("dispute_id", dispute_id)
        .eq("verified", True)
        .execute()
    )

    letters = (
        supabase.table("letters")
        .select("id, bureau, status, version")
        .eq("dispute_id", dispute_id)
        .execute()
    )

    # Get user info for the email
    user = (
        supabase.table("users")
        .select("email, full_name")
        .eq("id", dispute.data["user_id"])
        .single()
        .execute()
    )

    return {
        "dispute": dispute.data,
        "violations": violations.data or [],
        "letters": letters.data or [],
        "user": user.data if user.data else {},
    }
