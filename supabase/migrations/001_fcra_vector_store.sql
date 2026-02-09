-- Enable pgvector extension
create extension if not exists vector;

-- ─────────────────────────────────────────────
-- FCRA chunks table: stores embedded manual sections
-- ─────────────────────────────────────────────
create table if not exists fcra_chunks (
  id uuid primary key default gen_random_uuid(),
  section_number text not null,          -- e.g. "15 U.S.C. § 1681e(b)"
  section_title text,                    -- e.g. "Compliance procedures"
  content text not null,                 -- the actual chunk text
  chunk_index int not null default 0,    -- position within the section (for ordering)
  token_count int,                       -- approx token count for this chunk
  embedding vector(1536),               -- matches text-embedding-3-small dimensions
  metadata jsonb default '{}'::jsonb,    -- flexible extra data
  created_at timestamptz default now()
);

-- Index for fast similarity search
create index if not exists fcra_chunks_embedding_idx
  on fcra_chunks
  using ivfflat (embedding vector_cosine_ops)
  with (lists = 50);

-- Index for section lookups
create index if not exists fcra_chunks_section_idx
  on fcra_chunks (section_number);

-- ─────────────────────────────────────────────
-- Similarity search function
-- Called by the RAG service to find relevant FCRA sections
-- ─────────────────────────────────────────────
create or replace function match_fcra_chunks(
  query_embedding vector(1536),
  match_threshold float default 0.7,
  match_count int default 8
)
returns table (
  id uuid,
  section_number text,
  section_title text,
  content text,
  chunk_index int,
  similarity float
)
language sql stable
as $$
  select
    fc.id,
    fc.section_number,
    fc.section_title,
    fc.content,
    fc.chunk_index,
    1 - (fc.embedding <=> query_embedding) as similarity
  from fcra_chunks fc
  where 1 - (fc.embedding <=> query_embedding) > match_threshold
  order by similarity desc
  limit match_count;
$$;

-- ─────────────────────────────────────────────
-- Full-text search fallback (when vector search returns too few results)
-- ─────────────────────────────────────────────
alter table fcra_chunks add column if not exists fts tsvector
  generated always as (to_tsvector('english', content)) stored;

create index if not exists fcra_chunks_fts_idx
  on fcra_chunks using gin (fts);

create or replace function search_fcra_text(
  search_query text,
  result_limit int default 10
)
returns table (
  id uuid,
  section_number text,
  section_title text,
  content text,
  rank float
)
language sql stable
as $$
  select
    fc.id,
    fc.section_number,
    fc.section_title,
    fc.content,
    ts_rank(fc.fts, plainto_tsquery('english', search_query)) as rank
  from fcra_chunks fc
  where fc.fts @@ plainto_tsquery('english', search_query)
  order by rank desc
  limit result_limit;
$$;
