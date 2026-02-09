"""
Reports Router
──────────────
Endpoints for uploading and managing credit report PDFs.
"""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from app.models.schemas import Account, Bureau
from app.models.database import get_supabase
from app.services.auth import get_current_user
from app.services.pdf_parser import parse_credit_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reports", tags=["reports"])

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


@router.post("/upload")
async def upload_report(
    file: UploadFile = File(...),
    bureau: Bureau = Query(..., description="Which bureau issued this report"),
    user_id: str = Depends(get_current_user),
):
    """
    Upload a credit report PDF.

    1. Validates the file
    2. Stores raw PDF in Supabase Storage
    3. Parses accounts using Claude
    4. Stores structured data in the database

    Returns the report ID and parsed accounts.
    """
    # Validate file type
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type: {file.content_type}. Only PDF files are accepted.",
        )

    # Read file bytes
    file_bytes = await file.read()

    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File exceeds 20 MB limit")

    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="File is empty")

    supabase = get_supabase()

    # 1. Store raw PDF in Supabase Storage
    storage_path = f"{user_id}/{bureau.value}/{file.filename}"
    try:
        supabase.storage.from_("credit-reports").upload(
            storage_path,
            file_bytes,
            {"content-type": "application/pdf"},
        )
    except Exception as e:
        logger.error(f"Storage upload failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to store uploaded file")

    # 2. Create credit_reports record
    report = supabase.table("credit_reports").insert({
        "user_id": user_id,
        "bureau": bureau.value,
        "raw_file_path": storage_path,
        "status": "parsing",
    }).execute()

    if not report.data:
        raise HTTPException(status_code=500, detail="Failed to create report record")

    report_id = report.data[0]["id"]

    # 3. Parse the PDF
    try:
        parsed = await parse_credit_report(file_bytes, bureau.value)

        # 4. Store parsed data on the report
        supabase.table("credit_reports").update({
            "parsed_data": {
                "personal_info": parsed.personal_info.model_dump(),
                "accounts_count": len(parsed.accounts),
                "inquiries_count": len(parsed.inquiries),
            },
            "status": "parsed",
        }).eq("id", report_id).execute()

        # 5. Store individual accounts
        stored_accounts = []
        for account in parsed.accounts:
            account_row = supabase.table("accounts").insert({
                "report_id": report_id,
                "user_id": user_id,
                "creditor": account.creditor,
                "account_number_partial": account.account_number_partial,
                "status": account.status,
                "balance": float(account.balance) if account.balance is not None else None,
                "date_opened": str(account.date_opened) if account.date_opened else None,
                "date_reported": str(account.date_reported) if account.date_reported else None,
                "payment_history": account.payment_history,
                "remarks": account.remarks,
                "original_creditor": account.original_creditor,
                "bureau": bureau.value,
            }).execute()

            if account_row.data:
                stored = account_row.data[0]
                stored["id"] = stored["id"]
                stored_accounts.append(stored)

    except Exception as e:
        logger.error(f"PDF parsing failed: {e}", exc_info=True)
        supabase.table("credit_reports").update({
            "status": "failed",
        }).eq("id", report_id).execute()
        raise HTTPException(status_code=422, detail=f"Failed to parse credit report: {str(e)}")

    return {
        "report_id": report_id,
        "bureau": bureau.value,
        "accounts_count": len(stored_accounts),
        "accounts": stored_accounts,
        "personal_info": parsed.personal_info.model_dump(),
    }


@router.get("/{report_id}")
async def get_report(
    report_id: str,
    user_id: str = Depends(get_current_user),
):
    """Get a report and its accounts."""
    supabase = get_supabase()

    report = (
        supabase.table("credit_reports")
        .select("*")
        .eq("id", report_id)
        .eq("user_id", user_id)
        .single()
        .execute()
    )

    if not report.data:
        raise HTTPException(status_code=404, detail="Report not found")

    accounts = (
        supabase.table("accounts")
        .select("*")
        .eq("report_id", report_id)
        .execute()
    )

    return {
        "report": report.data,
        "accounts": accounts.data or [],
    }


@router.get("/")
async def list_reports(
    user_id: str = Depends(get_current_user),
):
    """List all reports for the current user."""
    supabase = get_supabase()

    reports = (
        supabase.table("credit_reports")
        .select("id, bureau, status, uploaded_at, parsed_data")
        .eq("user_id", user_id)
        .order("uploaded_at", desc=True)
        .execute()
    )

    return {"reports": reports.data or []}
