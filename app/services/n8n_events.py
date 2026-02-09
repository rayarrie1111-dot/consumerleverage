"""
n8n Event Emitter
─────────────────
Fires events to n8n webhook trigger nodes.

When something happens in our system (dispute ready, letter generated, etc.),
we POST to n8n's webhook URL so it can kick off automations.

n8n receives these as "Webhook" trigger nodes configured to listen on
specific paths like /webhook/dispute-ready.
"""

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Timeout for calls to n8n (don't block the main request)
N8N_TIMEOUT = 10.0


async def emit_event(event_name: str, payload: dict[str, Any]) -> bool:
    """
    Fire an event to n8n's webhook trigger.

    Args:
        event_name: Maps to the n8n webhook path, e.g. "dispute-ready"
                    → POST {N8N_BASE_URL}/webhook/{event_name}
        payload: JSON data n8n will receive in the webhook body.

    Returns:
        True if n8n acknowledged, False if it failed (non-blocking).
    """
    if not settings.N8N_BASE_URL:
        logger.debug(f"n8n not configured, skipping event: {event_name}")
        return False

    url = f"{settings.N8N_BASE_URL}/webhook/{event_name}"

    try:
        async with httpx.AsyncClient(timeout=N8N_TIMEOUT) as client:
            response = await client.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Secret": settings.WEBHOOK_SECRET,
                },
            )

        if response.status_code == 200:
            logger.info(f"n8n event '{event_name}' delivered successfully")
            return True
        else:
            logger.warning(
                f"n8n event '{event_name}' returned {response.status_code}: "
                f"{response.text[:200]}"
            )
            return False

    except httpx.TimeoutException:
        logger.warning(f"n8n event '{event_name}' timed out (non-blocking)")
        return False
    except httpx.ConnectError:
        logger.debug(f"n8n not reachable at {url} (is n8n running?)")
        return False
    except Exception as e:
        logger.warning(f"n8n event '{event_name}' failed: {e}")
        return False


# ─── Typed event helpers ──────────────────────────────────────────

async def emit_dispute_ready(
    dispute_id: str,
    user_id: str,
    total_violations: int,
    total_letters: int,
) -> bool:
    """Fired when the AI pipeline finishes and letters are ready for review."""
    return await emit_event("dispute-ready", {
        "dispute_id": dispute_id,
        "user_id": user_id,
        "total_violations": total_violations,
        "total_letters": total_letters,
    })


async def emit_letter_approved(
    letter_id: str,
    dispute_id: str,
    bureau: str,
    user_id: str,
) -> bool:
    """Fired when a user approves a letter for sending."""
    return await emit_event("letter-approved", {
        "letter_id": letter_id,
        "dispute_id": dispute_id,
        "bureau": bureau,
        "user_id": user_id,
    })


async def emit_report_uploaded(
    report_id: str,
    user_id: str,
    bureau: str,
    accounts_count: int,
) -> bool:
    """Fired when a credit report is successfully parsed."""
    return await emit_event("report-uploaded", {
        "report_id": report_id,
        "user_id": user_id,
        "bureau": bureau,
        "accounts_count": accounts_count,
    })


async def emit_user_signup(
    user_id: str,
    email: str,
) -> bool:
    """Fired when a new user signs up (for onboarding email flow)."""
    return await emit_event("user-signup", {
        "user_id": user_id,
        "email": email,
    })
