"""
Auth Service
────────────
Supabase JWT verification for protecting API endpoints.

Extracts the user ID from the Authorization header's Bearer token
by verifying it against Supabase's JWT secret.
"""

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Header

from app.models.database import get_supabase

logger = logging.getLogger(__name__)


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """
    Extract and verify the user ID from the Supabase JWT.

    Returns the user's UUID string.

    Usage in routes:
        @router.post("/something")
        async def endpoint(user_id: str = Depends(get_current_user)):
            ...
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    # Extract token from "Bearer <token>"
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header format")

    token = parts[1]

    try:
        supabase = get_supabase()
        user_response = supabase.auth.get_user(token)

        if not user_response or not user_response.user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        return user_response.user.id

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Auth verification failed: {e}")
        raise HTTPException(status_code=401, detail="Authentication failed")
