"""
Letters Router
──────────────
CRUD endpoints for dispute letters: view, edit, export as PDF.
"""

import io
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.models.schemas import LetterResponse, LetterStatus, LetterUpdateRequest
from app.models.database import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/letters", tags=["letters"])


@router.get("/{letter_id}", response_model=LetterResponse)
async def get_letter(letter_id: str):
    """Get a specific dispute letter by ID."""
    supabase = get_supabase()
    result = (
        supabase.table("letters")
        .select("*")
        .eq("id", letter_id)
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Letter not found")

    return result.data


@router.put("/{letter_id}", response_model=LetterResponse)
async def update_letter(letter_id: str, update: LetterUpdateRequest):
    """Update a letter's content (user edits in the previewer)."""
    supabase = get_supabase()

    # Fetch current letter to increment version
    current = (
        supabase.table("letters")
        .select("version")
        .eq("id", letter_id)
        .single()
        .execute()
    )

    if not current.data:
        raise HTTPException(status_code=404, detail="Letter not found")

    new_version = current.data["version"] + 1

    result = (
        supabase.table("letters")
        .update({
            "content": update.content,
            "version": new_version,
        })
        .eq("id", letter_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to update letter")

    return result.data[0]


@router.get("/{letter_id}/pdf")
async def export_letter_pdf(letter_id: str):
    """Export a dispute letter as a PDF file."""
    supabase = get_supabase()
    result = (
        supabase.table("letters")
        .select("content, bureau, dispute_id")
        .eq("id", letter_id)
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Letter not found")

    letter = result.data

    # Convert letter content to simple HTML for PDF rendering
    html_content = _letter_to_html(letter["content"])

    try:
        from weasyprint import HTML
        pdf_bytes = HTML(string=html_content).write_pdf()
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="PDF export requires WeasyPrint. Install with: pip install weasyprint"
        )
    except Exception as e:
        logger.error(f"PDF generation failed: {e}")
        raise HTTPException(status_code=500, detail="PDF generation failed")

    filename = f"dispute-{letter['bureau']}-{letter_id[:8]}.pdf"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _letter_to_html(content: str) -> str:
    """Convert plain text letter to styled HTML for PDF rendering."""
    # Escape HTML entities
    escaped = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Convert newlines to <br>
    body = escaped.replace("\n", "<br>\n")

    return f"""<!DOCTYPE html>
<html>
<head>
<style>
    body {{
        font-family: 'Times New Roman', Times, serif;
        font-size: 12pt;
        line-height: 1.6;
        margin: 1in;
        color: #000;
    }}
</style>
</head>
<body>
{body}
</body>
</html>"""
