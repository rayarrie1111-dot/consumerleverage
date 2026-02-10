# CLAUDE.md — ConsumerLeverage

## Project Overview

ConsumerLeverage is an AI-powered credit repair dispute engine built as a SaaS MVP. It helps consumers dispute inaccurate credit report entries using automated FCRA violation detection and AI-generated dispute letters.

**Stack**: Python 3.10+ / FastAPI backend, React (Lovable.dev) frontend, Supabase (PostgreSQL + Auth), Anthropic Claude API, OpenAI embeddings, n8n workflow automation.

## Repository Structure

```
app/
├── main.py                 # FastAPI entry point, CORS, router registration
├── config.py               # Settings class (all env vars loaded here)
├── data/
│   └── fcra_sections.json  # Static FCRA section lookup table
├── models/
│   ├── database.py         # Supabase client singleton
│   └── schemas.py          # Pydantic models for all API + pipeline data
├── routers/
│   ├── reports.py          # PDF upload + credit report parsing endpoints
│   ├── disputes.py         # Core AI dispute analysis endpoints
│   ├── letters.py          # Letter CRUD + PDF export endpoints
│   └── webhooks.py         # n8n integration webhook handlers
└── services/
    ├── ai_engine.py        # 3-step Claude prompt chain (Steps A, B, C)
    ├── pdf_parser.py       # pdfplumber extraction + Claude JSON structuring
    ├── rag.py              # Vector + full-text search over FCRA chunks
    ├── citation_verifier.py # Post-AI legal citation validation
    ├── universal_truths.py # Deterministic cross-bureau consistency checks
    ├── chunker.py          # FCRA manual tokenization (tiktoken)
    ├── embeddings.py       # OpenAI embedding client wrapper
    ├── n8n_events.py       # Webhook event emitter for n8n workflows
    └── auth.py             # Supabase JWT verification

docs/
├── ARCHITECTURE.md         # System design + 3-step AI chain explanation
├── INTEGRATION_GUIDE.md    # Step-by-step setup (Supabase, backend, frontend)
└── FEATURE_PRIORITY.md     # MVP roadmap (Tier 1/2/3 features)

scripts/
└── ingest_fcra.py          # One-time FCRA manual ingestion into pgvector

supabase/migrations/
├── 000_core_schema.sql     # Main tables, auth, RLS policies
└── 001_fcra_vector_store.sql  # pgvector extension + RAG functions

n8n/                        # Automation workflow JSON templates
```

## Development Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the dev server
uvicorn app.main:app --reload

# Run in production mode
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Ingest FCRA manual into vector store (one-time setup)
python scripts/ingest_fcra.py --input <fcra-file> [--dry-run] [--clear]
```

## Environment Setup

Copy `.env.example` to `.env` and fill in all values. Required variables:

| Variable | Purpose |
|----------|---------|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service-role key (server-side only) |
| `ANTHROPIC_API_KEY` | Claude API key for AI engine |
| `OPENAI_API_KEY` | OpenAI API key for embeddings |
| `EMBEDDING_PROVIDER` | `openai` or `voyage` |
| `EMBEDDING_MODEL` | Model name (default: `text-embedding-3-small`) |
| `EMBEDDING_DIMENSIONS` | Vector dimensions (default: `1536`) |
| `RAG_TOP_K` | Number of RAG results to retrieve (default: `8`) |
| `RAG_MATCH_THRESHOLD` | Vector similarity cutoff (default: `0.7`) |
| `CHUNK_MAX_TOKENS` | Max tokens per FCRA chunk (default: `500`) |
| `CHUNK_OVERLAP_TOKENS` | Overlap between chunks (default: `50`) |
| `WEBHOOK_SECRET` | HMAC secret for n8n webhook authentication |
| `N8N_BASE_URL` | n8n instance URL (default: `http://localhost:5678`) |

## Architecture

### Core Data Flow

```
PDF Upload → pdfplumber extraction → Claude JSON structuring → Accounts
    ↓
Universal Truths check (deterministic pre-AI rule-based flags)
    ↓
Step A: RAG-grounded violation identification (Claude Opus)
    ↓
Step B: Citation verification against FCRA manual (Claude Opus)
    ↓
Step C: Dispute letter drafting from verified citations (Claude Sonnet)
    ↓
Store violations + letter in Supabase → Emit n8n webhook events
```

### 3-Step Claude Chain (`app/services/ai_engine.py`)

Each step is a **separate** Claude API call with a narrowly scoped system prompt. This isolates concerns and prevents hallucination:

- **Step A** — Violation Identification: Uses RAG-retrieved FCRA sections + Universal Truths flags. Model: `claude-opus-4-6`.
- **Step B** — Citation Grounding: Verifies each violation with verbatim statutory text. Model: `claude-opus-4-6`.
- **Step C** — Letter Drafting: Generates professional dispute letter from verified citations only. Model: `claude-sonnet-4-5-20250929`.

Every AI call is logged to `ai_audit_log` for compliance auditability.

### Universal Truths (`app/services/universal_truths.py`)

Deterministic checks that run **before** AI processing:

- **Accuracy**: Cross-bureau field matching (balance, date_opened, status, payment_history)
- **Completeness**: Required field presence validation
- **Verifiability**: Provenance data availability + 7-year FCRA reporting window check

Flags are injected into Step A as evidence anchors.

### RAG Pipeline

1. Query → OpenAI embeddings
2. Vector similarity search in pgvector (`match_fcra_chunks` SQL function)
3. Fallback to full-text search (`search_fcra_text`) if insufficient vector results
4. Chunks formatted into prompt context for Step A

## Database

**Supabase PostgreSQL** with Row-Level Security (RLS) enforcing user isolation.

### Core Tables

| Table | Purpose |
|-------|---------|
| `users` | Supabase Auth profiles |
| `credit_reports` | Uploaded PDF metadata + parsed data |
| `accounts` | Individual tradelines extracted from reports |
| `disputes` | Dispute records (status: draft → analyzing → ready → sent → responded) |
| `violations` | AI-identified FCRA violations |
| `letters` | Generated dispute letters (versioned) |
| `ai_audit_log` | Every AI call logged for compliance |
| `fcra_chunks` | FCRA manual sections stored as pgvector embeddings |

Migrations live in `supabase/migrations/` and are applied manually via SQL console.

## API Structure

All endpoints are prefixed with `/api/`. Authentication uses Supabase JWT tokens passed in the `Authorization` header.

| Router | Prefix | Key Endpoints |
|--------|--------|---------------|
| `reports` | `/api/reports` | `POST /upload`, `GET /{id}` |
| `disputes` | `/api/disputes` | `POST /analyze`, `GET /{id}` |
| `letters` | `/api/letters` | `GET /{id}`, `GET /{id}/pdf` |
| `webhooks` | `/api/webhooks` | `POST /dispute-status`, `POST /letter-mailed` |

Health check: `GET /health`

## Code Conventions

### Style

- **Type hints**: Used throughout. Python 3.10+ union syntax (`str | None` not `Optional[str]`).
- **Validation**: Pydantic v2 models for all API and internal data. Defined in `app/models/schemas.py`.
- **Enums**: `Bureau`, `DisputeStatus`, `LetterStatus` are `str, Enum` subclasses.
- **Logging**: `logging.getLogger(__name__)` in each module. Format: `%(asctime)s %(levelname)s [%(name)s] %(message)s`.
- **Docstrings**: Module-level docstrings with ASCII dividers (`───────`). Function docstrings where non-trivial.
- **Imports**: Standard library first, then third-party, then `app.*` internal imports.

### Project Patterns

- **Settings**: All configuration through `app.config.settings` singleton. Never hardcode secrets.
- **Auth dependency**: `get_current_user` FastAPI dependency for protected endpoints (`app/services/auth.py`).
- **Database access**: Through `app.models.database` Supabase client singleton.
- **Webhook security**: HMAC validation via `hmac.compare_digest()` in webhook handlers.
- **Error handling**: FastAPI `HTTPException` for API errors. Logging for internal errors.

### File Organization

- **Routers** handle HTTP request/response and call into services.
- **Services** contain all business logic. No direct HTTP concerns.
- **Models** define data shapes (Pydantic) and database clients.
- **Routers should not contain business logic** — delegate to services.

## Security Notes

- `.env` files are git-ignored. Never commit secrets.
- RLS policies enforce per-user data isolation at the database level.
- Webhook endpoints verify HMAC signatures before processing.
- Supabase service key is used server-side only (never exposed to frontend).
- Uploaded PDFs are stored in Supabase Storage, not in git (`.gitignore` excludes `uploads/` and `*.pdf`).

## Testing

No test suite exists yet. When adding tests:

- Use `pytest` with FastAPI's `TestClient` for API tests.
- Mock external services (Anthropic, OpenAI, Supabase) in unit tests.
- The `ingest_fcra.py` script supports `--dry-run` for testing chunking logic.
- Swagger UI available at `/docs` for manual API testing during development.

## n8n Workflow Automation

Workflow templates in `n8n/` handle post-processing automation:

- `dispute_ready_notification.json` — Email user when analysis completes
- `letter_mail_pipeline.json` — Batch PDF conversion + mail via Lob API
- `thirty_day_tracker.json` — Daily check for overdue dispute responses

Workflows are triggered by webhook events emitted from `app/services/n8n_events.py`.

## Dependencies

Key runtime dependencies (`requirements.txt`):

- `fastapi` / `uvicorn` — Web framework + ASGI server
- `anthropic` — Claude API client (AI engine)
- `openai` — Embedding generation
- `pdfplumber` — PDF text extraction
- `supabase` — PostgreSQL + Auth client
- `pydantic` — Schema validation
- `weasyprint` — HTML-to-PDF letter export
- `tiktoken` — Token counting for chunking
- `httpx` — HTTP client for webhooks
- `python-dotenv` — Environment variable loading
