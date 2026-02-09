# ConsumerLeverage — Step-by-Step Integration Guide

## Phase 1: Foundation (Do This First)

### Step 1.1 — Set Up Supabase Project

1. Create a new Supabase project at supabase.com
2. Enable the `pgvector` extension:
   ```sql
   create extension if not exists vector;
   ```
3. Run the database schema from `ARCHITECTURE.md` Section 6
4. Enable Supabase Auth (email/password to start)
5. Create a Storage bucket called `credit-reports` (private)
6. Create a Storage bucket called `dispute-letters` (private)
7. Save your Supabase URL and keys — you'll need them in both Lovable and Cursor

### Step 1.2 — Set Up Cursor Backend (Python/FastAPI)

Create the project structure in Cursor:

```
consumerleverage-backend/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app entry point
│   ├── config.py               # Environment variables
│   ├── routers/
│   │   ├── auth.py             # Auth endpoints (proxy to Supabase)
│   │   ├── reports.py          # PDF upload + parsing
│   │   ├── disputes.py         # Dispute analysis + AI chain
│   │   └── letters.py          # Letter CRUD + PDF export
│   ├── services/
│   │   ├── pdf_parser.py       # pdfplumber extraction
│   │   ├── ai_engine.py        # Claude API calls (3-step chain)
│   │   ├── rag.py              # Vector search for FCRA sections
│   │   ├── citation_verifier.py # Post-AI citation validation
│   │   ├── universal_truths.py # Cross-bureau consistency checks
│   │   └── letter_export.py    # PDF generation
│   ├── models/
│   │   ├── schemas.py          # Pydantic models for request/response
│   │   └── database.py         # Supabase client setup
│   └── data/
│       └── fcra_sections.json  # Valid FCRA section lookup table
├── scripts/
│   └── ingest_fcra.py          # One-time script to chunk + embed FCRA manual
├── requirements.txt
├── .env
└── Dockerfile
```

Install core dependencies:
```bash
pip install fastapi uvicorn anthropic pdfplumber supabase python-dotenv pydantic weasyprint
```

### Step 1.3 — Set Up Lovable Frontend

In Lovable.dev:
1. Create a new project connected to your Supabase instance
2. Lovable has native Supabase integration — use it for auth
3. Set up the API base URL pointing to your Cursor backend
4. Create these initial pages:
   - `/login` — Auth page
   - `/dashboard` — Overview
   - `/upload` — Report upload
   - `/disputes` — Dispute Center
   - `/letters/:id` — Letter previewer

---

## Phase 2: Data Ingestion Pipeline

### Step 2.1 — PDF Upload Endpoint (Cursor)

```python
# app/routers/reports.py
from fastapi import APIRouter, UploadFile, File, Depends
from app.services.pdf_parser import parse_credit_report
from app.models.database import get_supabase

router = APIRouter(prefix="/api/reports", tags=["reports"])

@router.post("/upload")
async def upload_report(
    file: UploadFile = File(...),
    bureau: str = "equifax",
    user_id: str = Depends(get_current_user)
):
    # 1. Save raw file to Supabase Storage
    file_bytes = await file.read()
    storage_path = f"{user_id}/{bureau}/{file.filename}"
    supabase = get_supabase()
    supabase.storage.from_("credit-reports").upload(storage_path, file_bytes)

    # 2. Parse the PDF
    parsed_data = await parse_credit_report(file_bytes, bureau)

    # 3. Store structured data in DB
    report = supabase.table("credit_reports").insert({
        "user_id": user_id,
        "bureau": bureau,
        "raw_file_path": storage_path,
        "parsed_data": parsed_data
    }).execute()

    # 4. Store individual accounts
    for account in parsed_data["accounts"]:
        supabase.table("accounts").insert({
            "report_id": report.data[0]["id"],
            **account
        }).execute()

    return {"report_id": report.data[0]["id"], "accounts": parsed_data["accounts"]}
```

### Step 2.2 — PDF Parser Service (Cursor)

```python
# app/services/pdf_parser.py
import pdfplumber
import io
import anthropic

client = anthropic.Anthropic()

async def parse_credit_report(file_bytes: bytes, bureau: str) -> dict:
    # Step 1: Extract raw text
    raw_text = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            raw_text += page.extract_text() + "\n"

    # Step 2: Use Claude to structure the data
    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",  # Use Sonnet for parsing (fast + cheap)
        max_tokens=4096,
        system=f"""You are a credit report parser for {bureau} reports.
Extract ALL accounts, inquiries, and personal information into structured JSON.

Return ONLY valid JSON matching this schema:
{{
  "personal_info": {{"name": "", "address": "", "ssn_last4": ""}},
  "accounts": [
    {{
      "creditor": "",
      "account_number_partial": "",
      "status": "",
      "balance": 0,
      "date_opened": "",
      "date_reported": "",
      "payment_history": "",
      "remarks": "",
      "original_creditor": ""
    }}
  ],
  "inquiries": [
    {{"inquirer": "", "date": "", "type": ""}}
  ]
}}""",
        messages=[{"role": "user", "content": f"Parse this {bureau} credit report:\n\n{raw_text}"}]
    )

    import json
    return json.loads(response.content[0].text)
```

### Step 2.3 — Upload UI (Lovable)

In Lovable, create an upload component that:
1. Accepts PDF files (drag-and-drop or file picker)
2. Lets user select the bureau (Equifax / Experian / TransUnion)
3. Calls `POST /api/reports/upload` with multipart form data
4. Shows a loading spinner during parsing
5. On success, navigates to the Dispute Center with the parsed accounts displayed

---

## Phase 3: FCRA Knowledge Base (RAG Setup)

### Step 3.1 — Ingest the FCRA Manual (One-Time Script)

```python
# scripts/ingest_fcra.py
"""
Run once to chunk your FCRA manual and store embeddings in Supabase pgvector.
"""
import anthropic
from supabase import create_client

# First, create the embeddings table in Supabase:
# create table fcra_chunks (
#   id uuid primary key default gen_random_uuid(),
#   section_number text,
#   content text not null,
#   embedding vector(1024)
# );
# create index on fcra_chunks using ivfflat (embedding vector_cosine_ops);

def chunk_fcra_manual(filepath: str) -> list[dict]:
    """Split FCRA manual into sections by statute number."""
    with open(filepath) as f:
        text = f.read()

    chunks = []
    # Split by section headers like "§ 1681a" or "Section 604"
    # Adjust regex based on your manual's format
    import re
    sections = re.split(r'(§\s*\d+\w*)', text)

    current_section = ""
    for i, part in enumerate(sections):
        if part.startswith("§"):
            current_section = part.strip()
        else:
            if current_section and part.strip():
                # Further chunk if section is too long (~500 tokens)
                words = part.split()
                for j in range(0, len(words), 400):
                    chunk_text = " ".join(words[j:j+400])
                    chunks.append({
                        "section_number": current_section,
                        "content": f"{current_section}\n{chunk_text}"
                    })

    return chunks

def embed_and_store(chunks: list[dict]):
    """Embed each chunk and store in Supabase."""
    client = anthropic.Anthropic()
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    for chunk in chunks:
        # Use Voyage or OpenAI embeddings (Claude doesn't have an embedding model)
        # Example with a generic embedding call:
        embedding = get_embedding(chunk["content"])  # implement with your embedding provider

        supabase.table("fcra_chunks").insert({
            "section_number": chunk["section_number"],
            "content": chunk["content"],
            "embedding": embedding
        }).execute()

if __name__ == "__main__":
    chunks = chunk_fcra_manual("path/to/fcra_manual.pdf")
    embed_and_store(chunks)
    print(f"Ingested {len(chunks)} FCRA chunks")
```

### Step 3.2 — RAG Retrieval Service (Cursor)

```python
# app/services/rag.py
from app.models.database import get_supabase

async def get_relevant_fcra_sections(query: str, top_k: int = 5) -> list[str]:
    """Retrieve the most relevant FCRA sections for a given account/violation."""
    query_embedding = get_embedding(query)  # same embedding provider as ingestion
    supabase = get_supabase()

    # Supabase pgvector similarity search
    result = supabase.rpc("match_fcra_chunks", {
        "query_embedding": query_embedding,
        "match_threshold": 0.7,
        "match_count": top_k
    }).execute()

    return [row["content"] for row in result.data]
```

Create the matching function in Supabase SQL:
```sql
create or replace function match_fcra_chunks(
  query_embedding vector(1024),
  match_threshold float,
  match_count int
)
returns table (id uuid, section_number text, content text, similarity float)
language sql stable
as $$
  select
    id,
    section_number,
    content,
    1 - (embedding <=> query_embedding) as similarity
  from fcra_chunks
  where 1 - (embedding <=> query_embedding) > match_threshold
  order by similarity desc
  limit match_count;
$$;
```

---

## Phase 4: AI Engine (The 3-Step Chain)

### Step 4.1 — Build the AI Engine (Cursor)

```python
# app/services/ai_engine.py
import anthropic
from app.services.rag import get_relevant_fcra_sections
from app.services.citation_verifier import verify_citations

client = anthropic.Anthropic()

async def analyze_account(account: dict, bureau: str) -> dict:
    """Run the full 3-step AI chain on a single account."""

    # Retrieve relevant FCRA sections for this account
    account_summary = f"{account['creditor']} - {account['status']} - {account['remarks']}"
    fcra_context = await get_relevant_fcra_sections(account_summary)
    fcra_text = "\n\n---\n\n".join(fcra_context)

    # ─── STEP A: Identify Violations ───
    step_a = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        system=f"""You are a credit report analyst specializing in FCRA violations.
Analyze the account data and identify ALL potential violations.

RULES:
- ONLY cite violations found in the FCRA reference material below.
- If no violation exists, return {{"violations": []}}.
- Do not infer violations not supported by the reference text.
- Return valid JSON only.

FCRA REFERENCE MATERIAL:
{fcra_text}""",
        messages=[{"role": "user", "content": f"Analyze this account for FCRA violations:\n{account}"}]
    )

    import json
    violations = json.loads(step_a.content[0].text)

    if not violations.get("violations"):
        return {"violations": [], "citations": [], "letter": None}

    # ─── STEP B: Ground Citations ───
    step_b = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        system=f"""You are a legal citation engine for FCRA law.
For each violation, provide the EXACT statutory text.

RULES:
- Quote the statute VERBATIM from the reference material.
- Include section number, subsection, and exact wording.
- If the section is not in the reference material, respond with "CITATION NOT FOUND".

FCRA REFERENCE MATERIAL:
{fcra_text}""",
        messages=[{"role": "user", "content": f"Provide exact citations for these violations:\n{json.dumps(violations)}"}]
    )

    citations = json.loads(step_b.content[0].text)

    # ─── VERIFICATION: Check citations against known sections ───
    verified = verify_citations(citations)

    # ─── STEP C: Draft Letter ───
    step_c = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system="""You are a consumer rights dispute letter writer.
Draft a professional dispute letter using the violations and citations provided.

RULES:
- Use a firm but professional tone.
- Reference each violation with its exact legal citation.
- Use {{CONSUMER_NAME}}, {{CONSUMER_ADDRESS}}, {{SSN_LAST4}} as placeholders.
- Follow standard dispute letter format.
- Do not add claims not supported by the provided citations.""",
        messages=[{"role": "user", "content": f"""Draft a dispute letter to {bureau} for this account:

Account: {json.dumps(account)}
Verified violations and citations: {json.dumps(verified)}"""}]
    )

    letter = step_c.content[0].text

    return {
        "violations": verified["violations"],
        "letter": letter
    }
```

### Step 4.2 — Dispute Endpoint (Cursor)

```python
# app/routers/disputes.py
from fastapi import APIRouter, Depends
from app.services.ai_engine import analyze_account
from app.models.database import get_supabase

router = APIRouter(prefix="/api/disputes", tags=["disputes"])

@router.post("/analyze")
async def analyze_disputes(
    report_id: str,
    account_ids: list[str],
    bureaus: list[str],
    user_id: str = Depends(get_current_user)
):
    supabase = get_supabase()

    # Create dispute record
    dispute = supabase.table("disputes").insert({
        "user_id": user_id,
        "status": "analyzing"
    }).execute()
    dispute_id = dispute.data[0]["id"]

    results = []
    for account_id in account_ids:
        # Fetch account data
        account = supabase.table("accounts").select("*").eq("id", account_id).single().execute()

        for bureau in bureaus:
            # Run 3-step AI chain
            analysis = await analyze_account(account.data, bureau)

            # Store violations
            for violation in analysis["violations"]:
                supabase.table("violations").insert({
                    "dispute_id": dispute_id,
                    "account_id": account_id,
                    **violation
                }).execute()

            # Store letter
            if analysis["letter"]:
                supabase.table("letters").insert({
                    "dispute_id": dispute_id,
                    "bureau": bureau,
                    "content": analysis["letter"],
                    "status": "draft"
                }).execute()

            results.append(analysis)

    # Update dispute status
    supabase.table("disputes").update({"status": "ready"}).eq("id", dispute_id).execute()

    return {"dispute_id": dispute_id, "results": results}
```

---

## Phase 5: Frontend Integration (Lovable)

### Step 5.1 — Dispute Center Page

Build in Lovable with these components:

1. **Account List Component**
   - Fetches parsed accounts from `/api/reports/{id}`
   - Displays each account as a card (creditor, balance, status, remarks)
   - Checkbox selection for disputing
   - Color-coding: red for collections, yellow for late payments, green for good standing

2. **Analyze Button**
   - Sends selected account IDs to `POST /api/disputes/analyze`
   - Shows progress indicator (consider WebSocket or polling for real-time status)
   - On completion, navigates to results view

3. **Results View**
   - Shows each violation found with its FCRA citation
   - "Verified" badge for citations that passed verification
   - Link to view/edit the generated letter

### Step 5.2 — Letter Previewer Page

1. **Letter Display**
   - Renders the letter content in a professional format
   - Highlights legal citations in bold
   - Shows which violations this letter addresses

2. **Edit Mode**
   - Toggle to edit the letter text directly
   - Save changes via `PUT /api/letters/{id}`

3. **Export Actions**
   - "Download PDF" button → `GET /api/letters/{id}/pdf`
   - "Print" button
   - (Future) "Send via Mail" button

### Step 5.3 — Connect Lovable to Backend

In Lovable, set up API calls using their built-in fetch or axios integration:

```typescript
// Example: Lovable API service
const API_BASE = process.env.VITE_API_URL;

export async function uploadReport(file: File, bureau: string) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("bureau", bureau);

  const response = await fetch(`${API_BASE}/api/reports/upload`, {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: formData,
  });

  return response.json();
}

export async function analyzeDisputes(reportId: string, accountIds: string[], bureaus: string[]) {
  const response = await fetch(`${API_BASE}/api/disputes/analyze`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${getToken()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      report_id: reportId,
      account_ids: accountIds,
      bureaus: bureaus,
    }),
  });

  return response.json();
}
```

---

## Phase 6: Testing & Hardening

### Step 6.1 — Test with Real Credit Reports

1. Get sample credit reports (your own or sanitized test data)
2. Upload each bureau's report through the full pipeline
3. Manually verify:
   - Are accounts parsed correctly?
   - Are violations legitimate?
   - Do citations match the actual FCRA text?
   - Is the letter professional and accurate?

### Step 6.2 — Edge Cases to Handle

- PDF with scanned images (OCR fallback)
- Empty or corrupted PDFs
- Accounts that appear on multiple bureaus with different data
- Reports with no negative accounts (happy path — no disputes needed)
- Claude API rate limits (implement retry with exponential backoff)
- Very long credit reports (token limits — implement pagination)

### Step 6.3 — Security Checklist

- [ ] Supabase Row-Level Security (RLS) enabled — users can only see their own data
- [ ] API authentication on every endpoint
- [ ] PII (SSN, addresses) encrypted at rest
- [ ] PDF uploads scanned for malware
- [ ] Rate limiting on AI endpoints (prevent abuse)
- [ ] HTTPS everywhere
- [ ] AI audit log captures all API calls for compliance
