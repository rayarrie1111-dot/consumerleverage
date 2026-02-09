# ConsumerLeverage — Credit Repair SaaS MVP Architecture

## System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        FRONTEND (Lovable.dev)                       │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐ │
│  │  User Auth    │  │  Dispute     │  │  Letter Previewer         │ │
│  │  Dashboard    │  │  Center      │  │  (PDF/DOCX export)        │ │
│  └──────┬───────┘  └──────┬───────┘  └────────────┬──────────────┘ │
│         │                 │                        │                │
└─────────┼─────────────────┼────────────────────────┼────────────────┘
          │                 │                        │
          ▼                 ▼                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     API GATEWAY / BACKEND (Cursor)                   │
│                     Python (FastAPI) or Node.js                      │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐ │
│  │  PDF Parser   │  │  AI Engine   │  │  Letter Generator         │ │
│  │  (ingestion)  │  │  (Claude)    │  │  (template + AI merge)    │ │
│  └──────┬───────┘  └──────┬───────┘  └────────────┬──────────────┘ │
│         │                 │                        │                │
└─────────┼─────────────────┼────────────────────────┼────────────────┘
          │                 │                        │
          ▼                 ▼                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        DATA LAYER                                    │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐ │
│  │  Supabase     │  │  FCRA Manual │  │  File Storage             │ │
│  │  (Postgres)   │  │  Vector DB   │  │  (Supabase Storage /S3)   │ │
│  └──────────────┘  └──────────────┘  └───────────────────────────┘ │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 1. Data Ingestion Pipeline

### Credit Report Parsing (PDF → Structured Data)

**Recommended approach: Hybrid parsing**

```
PDF Upload → pdfplumber/PyMuPDF (extract text) → Claude (structure extraction) → JSON
```

1. **Raw text extraction** — Use `pdfplumber` (Python) to pull raw text from credit
   report PDFs. Credit reports from Equifax, Experian, and TransUnion each have
   different layouts, so raw text is the safest first step.

2. **AI-assisted structuring** — Send the raw text to Claude with a tightly scoped
   prompt that extracts structured fields:

   ```json
   {
     "bureau": "Equifax",
     "accounts": [
       {
         "creditor": "Capital One",
         "account_number_partial": "xxxx1234",
         "status": "Collection",
         "balance": 2340,
         "date_opened": "2021-03-15",
         "date_reported": "2024-11-01",
         "payment_history": "30-60-90 late",
         "remarks": "Charged off"
       }
     ],
     "inquiries": [...],
     "personal_info": {...}
   }
   ```

3. **Validation layer** — Run schema validation (Pydantic in Python / Zod in Node)
   on the extracted JSON before it enters the system. Reject malformed data early.

### Why not OCR-only?
Credit report PDFs are usually text-based (not scanned images), so `pdfplumber`
handles them well. Add Tesseract OCR as a fallback only if you encounter
image-based PDFs.

---

## 2. AI Logic Layer — Prompt-Chaining Strategy

### The Three-Step Chain

This is the core innovation. Each step is a **separate Claude API call** with a
narrowly scoped system prompt. This prevents hallucination and keeps each step
auditable.

#### Step A: Violation Identification

```
System prompt:
"You are a credit report analyst. Given the following account data and the
FCRA reference material below, identify ALL potential violations.

RULES:
- Only cite violations found in the FCRA reference material provided.
- If no violation exists, say 'No violation found.'
- Do not infer or assume violations not supported by the reference text.
- Return structured JSON."

User prompt:
{account_data_json}

Context (injected via RAG):
{relevant_fcra_sections}  ← retrieved from vector DB
```

**Output:** Array of violation objects with section references.

#### Step B: Legal Citation & Basis

```
System prompt:
"You are a legal citation engine. Given the identified violations and the
FCRA reference text, provide the EXACT statutory text for each violation.

RULES:
- Quote the statute verbatim from the provided reference material.
- Include section number, subsection, and exact wording.
- If the reference material does not contain the cited section, respond
  with 'CITATION NOT FOUND' — do not fabricate."

User prompt:
{violations_from_step_a}

Context (injected via RAG):
{relevant_fcra_sections}
```

**Output:** Each violation now has a verified, verbatim legal citation.

#### Step C: Letter Drafting

```
System prompt:
"You are a consumer rights dispute letter writer. Using the violations and
legal citations provided, draft a professional dispute letter addressed
to the specified credit bureau.

RULES:
- Use a firm but professional tone.
- Reference each violation with its exact legal citation.
- Include consumer identifying information placeholders.
- Follow the dispute letter template structure provided.
- Do not add legal claims not supported by the citations given."

User prompt:
{violations_with_citations_from_step_b}
{letter_template}
{consumer_info_placeholders}
```

**Output:** Ready-to-send dispute letter.

### Why separate calls instead of one big prompt?

| Concern | Single prompt | Chained prompts |
|---------|--------------|-----------------|
| Hallucination risk | High — model mixes tasks | Low — each step is constrained |
| Debugging | Hard to trace errors | Can audit each step independently |
| Cost control | One large call | Can cache Step A results, skip B/C if no violations |
| Accuracy | Drifts on long outputs | Each output is short and focused |

---

## 3. Preventing Hallucinated Laws (Critical Question)

### Strategy: RAG + Grounding + Verification

1. **Retrieval-Augmented Generation (RAG)**
   - Chunk your FCRA manual into ~500-token sections.
   - Embed each chunk using a model like `voyage-3` or OpenAI `text-embedding-3-small`.
   - Store in a vector database (Supabase `pgvector` extension works perfectly here).
   - At query time, retrieve the top-K most relevant chunks and inject them
     directly into the system prompt as context.
   - **The model can only cite what's in the context window.**

2. **Closed-book constraint**
   - Every system prompt includes: *"Only reference laws and sections found in the
     FCRA reference material provided below. Do not use external knowledge."*

3. **Post-generation verification**
   - After Step B, run a simple string-match check: does the cited section number
     (e.g., "15 U.S.C. § 1681e(b)") actually appear in your FCRA manual chunks?
   - If not → flag for human review, do not include in letter.

4. **Citation index**
   - Build a lookup table of all valid FCRA section numbers from your manual.
   - Any section number in the AI output that isn't in this table is auto-rejected.

```python
VALID_FCRA_SECTIONS = {
    "15 U.S.C. § 1681",
    "15 U.S.C. § 1681a",
    "15 U.S.C. § 1681b",
    "15 U.S.C. § 1681c",
    "15 U.S.C. § 1681e(b)",
    "15 U.S.C. § 1681i",
    "15 U.S.C. § 1681s-2",
    # ... complete list from your manual
}

def verify_citations(ai_output: dict) -> dict:
    for violation in ai_output["violations"]:
        section = violation["section"]
        if section not in VALID_FCRA_SECTIONS:
            violation["status"] = "UNVERIFIED"
            violation["flag"] = "Citation not found in FCRA manual"
    return ai_output
```

---

## 4. Frontend → Backend Data Flow

### Passing "Negative Account" Data from Lovable to Backend

**Recommended flow:**

```
Lovable UI                         Backend (Cursor)
──────────                         ────────────────
1. User uploads PDF          →     POST /api/reports/upload
                                   (multipart/form-data)

2. Backend parses, returns   ←     { accounts: [...], id: "rpt_123" }
   structured accounts

3. User reviews accounts     →     (displayed in Dispute Center UI)
   and selects which to
   dispute

4. User clicks "Analyze"     →     POST /api/disputes/analyze
                                   {
                                     report_id: "rpt_123",
                                     account_ids: ["acc_1", "acc_3"],
                                     bureaus: ["equifax", "experian"]
                                   }

5. Backend runs 3-step       ←     { violations: [...], letters: [...] }
   AI chain, returns results

6. User previews/edits       →     PUT /api/letters/{id}
   letter in Lovable                (optional edits)

7. User exports final        →     GET /api/letters/{id}/pdf
   letter as PDF
```

**Key design decisions:**
- The frontend never sends raw unstructured text to the AI. The backend always
  structures data first.
- Account selection happens on the frontend so the user has control over what
  gets disputed.
- The backend returns both the violations AND the draft letters so the UI can
  show the reasoning alongside the letter.

---

## 5. "Universal Truths" Automation (Accuracy, Completeness, Verifiability)

### Cross-Bureau Consistency Check

Every reported account must pass three checks across all bureaus:

| Check | What it means | How to automate |
|-------|--------------|-----------------|
| **Accuracy** | Is the data factually correct? | Compare fields across bureau reports. Flag mismatches (e.g., balance on Equifax ≠ balance on Experian). |
| **Completeness** | Is all required info present? | Check for null/empty fields that should be populated (date opened, payment history, account status). |
| **Verifiability** | Can the bureau prove this data? | Flag accounts with no original creditor listed, missing account numbers, or "information unavailable" markers. |

```python
def universal_truths_check(accounts_by_bureau: dict) -> list[dict]:
    """
    accounts_by_bureau = {
      "equifax":   [account1, account2, ...],
      "experian":  [account1, account2, ...],
      "transunion": [account1, account2, ...]
    }
    """
    flags = []

    # Group accounts across bureaus by creditor + partial account number
    merged = merge_accounts_across_bureaus(accounts_by_bureau)

    for account_group in merged:
        # ACCURACY: Compare fields across bureaus
        if not all_match(account_group, "balance"):
            flags.append({
                "type": "accuracy",
                "field": "balance",
                "account": account_group["creditor"],
                "details": get_mismatches(account_group, "balance")
            })

        if not all_match(account_group, "date_opened"):
            flags.append({
                "type": "accuracy",
                "field": "date_opened",
                "account": account_group["creditor"],
                "details": get_mismatches(account_group, "date_opened")
            })

        # COMPLETENESS: Check for missing required fields
        required = ["balance", "date_opened", "payment_history", "status"]
        for bureau, account in account_group["bureau_data"].items():
            for field in required:
                if not account.get(field):
                    flags.append({
                        "type": "completeness",
                        "bureau": bureau,
                        "field": field,
                        "account": account_group["creditor"]
                    })

        # VERIFIABILITY: Flag accounts missing provenance
        for bureau, account in account_group["bureau_data"].items():
            if not account.get("original_creditor"):
                flags.append({
                    "type": "verifiability",
                    "bureau": bureau,
                    "account": account_group["creditor"],
                    "reason": "No original creditor listed"
                })

    return flags
```

These flags feed directly into **Step A** of the AI chain as additional context,
making violation identification even more targeted.

---

## 6. Database Schema (Supabase / Postgres)

```sql
-- Users
create table users (
  id uuid primary key default gen_random_uuid(),
  email text unique not null,
  full_name text not null,
  address text,
  ssn_last4 text,
  created_at timestamptz default now()
);

-- Uploaded credit reports
create table credit_reports (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id),
  bureau text not null check (bureau in ('equifax', 'experian', 'transunion')),
  raw_file_path text not null,         -- path in Supabase Storage
  parsed_data jsonb,                    -- structured account data
  uploaded_at timestamptz default now()
);

-- Individual accounts extracted from reports
create table accounts (
  id uuid primary key default gen_random_uuid(),
  report_id uuid references credit_reports(id),
  creditor text not null,
  account_number_partial text,
  status text,
  balance numeric,
  date_opened date,
  date_reported date,
  payment_history text,
  remarks text
);

-- Disputes initiated by user
create table disputes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id),
  status text default 'draft' check (status in ('draft', 'analyzing', 'ready', 'sent', 'responded')),
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- Violations found by AI
create table violations (
  id uuid primary key default gen_random_uuid(),
  dispute_id uuid references disputes(id),
  account_id uuid references accounts(id),
  violation_type text not null,
  fcra_section text not null,
  citation_text text,
  verified boolean default false,       -- passed citation verification
  ai_confidence numeric
);

-- Generated dispute letters
create table letters (
  id uuid primary key default gen_random_uuid(),
  dispute_id uuid references disputes(id),
  bureau text not null,
  content text not null,
  version int default 1,
  status text default 'draft' check (status in ('draft', 'approved', 'sent')),
  pdf_path text,                        -- exported PDF in storage
  created_at timestamptz default now()
);

-- Audit log for every AI call (compliance + debugging)
create table ai_audit_log (
  id uuid primary key default gen_random_uuid(),
  dispute_id uuid references disputes(id),
  step text not null check (step in ('parse', 'identify', 'cite', 'draft')),
  input_data jsonb,
  output_data jsonb,
  model text,
  tokens_used int,
  created_at timestamptz default now()
);
```

---

## 7. Tech Stack Summary

| Layer | Tool | Purpose |
|-------|------|---------|
| Frontend | Lovable.dev | Dashboard, Dispute Center, Letter Previewer |
| Backend | Cursor (Python/FastAPI) | PDF parsing, API orchestration, AI chain |
| AI | Claude API (Anthropic) | Violation ID, citation, letter drafting |
| Database | Supabase (Postgres) | Users, reports, disputes, letters |
| Vector DB | Supabase pgvector | FCRA manual embeddings for RAG |
| File Storage | Supabase Storage | PDF uploads, generated letters |
| Auth | Supabase Auth | User accounts, JWT tokens |
| PDF Export | WeasyPrint or Puppeteer | Convert letters to PDF |
