# ConsumerLeverage — MVP Feature Priority List

## Tier 1: Must-Have for Launch (Weeks 1–4)

### P0 — Core Loop (No product without these)

| # | Feature | Owner | Effort |
|---|---------|-------|--------|
| 1 | **User auth + account creation** | Lovable (Supabase Auth) | S |
| 2 | **PDF upload endpoint** | Cursor (FastAPI) | S |
| 3 | **Credit report parser** (PDF → structured JSON) | Cursor (pdfplumber + Claude) | L |
| 4 | **FCRA manual ingestion** (chunk + embed + store in pgvector) | Cursor | M |
| 5 | **Step A: Violation identification** (Claude + RAG) | Cursor | L |
| 6 | **Step B: Legal citation grounding** (Claude + verification) | Cursor | M |
| 7 | **Step C: Letter generation** (Claude + templates) | Cursor | M |
| 8 | **Citation verification layer** (post-AI validation) | Cursor | S |
| 9 | **Dispute Center UI** (view accounts, select for dispute, trigger analysis) | Lovable | L |
| 10 | **Letter previewer** (view generated letter, basic edit) | Lovable | M |
| 11 | **PDF export** (download dispute letter as PDF) | Cursor (WeasyPrint) | S |
| 12 | **Database schema + migrations** | Cursor (Supabase) | M |

### Definition of "MVP Done"
A user can:
1. Sign up and log in
2. Upload a credit report PDF
3. See their accounts parsed and displayed
4. Select accounts to dispute
5. Get AI-identified violations with real FCRA citations
6. Preview and download a dispute letter as PDF

---

## Tier 2: Should-Have for Early Users (Weeks 5–8)

| # | Feature | Owner | Effort |
|---|---------|-------|--------|
| 13 | **Multi-bureau support** (upload all 3, cross-compare) | Cursor + Lovable | L |
| 14 | **Universal Truths automation** (accuracy/completeness/verifiability checks) | Cursor | M |
| 15 | **Letter history + versioning** | Supabase + Lovable | M |
| 16 | **Dispute status tracking** (draft → sent → responded) | Lovable | S |
| 17 | **User dashboard** (summary stats, active disputes, timeline) | Lovable | M |
| 18 | **AI audit log** (store every AI call for compliance) | Cursor | S |
| 19 | **Error handling + retry logic** for AI calls | Cursor | S |

---

## Tier 3: Nice-to-Have for Growth (Weeks 9–12)

| # | Feature | Owner | Effort |
|---|---------|-------|--------|
| 20 | **Bulk dispute mode** (auto-dispute all flagged accounts) | Cursor + Lovable | M |
| 21 | **Bureau-specific letter templates** (tailored formatting per bureau) | Cursor | M |
| 22 | **Email/mail integration** (send letters directly) | Cursor (SendGrid / Lob API) | L |
| 23 | **Progress notifications** (email alerts on dispute status changes) | Cursor | S |
| 24 | **Credit score simulator** (estimate impact of successful disputes) | Cursor + Lovable | L |
| 25 | **Stripe billing integration** (subscription or per-letter pricing) | Cursor + Lovable | M |
| 26 | **Admin panel** (monitor users, review flagged citations) | Lovable | L |

---

## Effort Key

| Size | Meaning |
|------|---------|
| S | < 1 day of focused work |
| M | 1–3 days |
| L | 3–5 days |

---

## Critical Path

The longest dependency chain determines your real timeline:

```
User Auth (1) → PDF Upload (2) → Parser (3) → FCRA Embeddings (4)
                                      ↓
                              Dispute Center UI (9)
                                      ↓
                           Violation ID / Step A (5)
                                      ↓
                           Citation / Step B (6) → Verification (8)
                                      ↓
                           Letter Gen / Step C (7)
                                      ↓
                           Letter Previewer (10) → PDF Export (11)
```

**Parallelizable work:**
- Auth (1) and FCRA ingestion (4) can happen simultaneously
- Lovable UI work (9, 10) can happen in parallel with backend AI work (5, 6, 7)
- Database schema (12) should be done first to unblock both frontend and backend
