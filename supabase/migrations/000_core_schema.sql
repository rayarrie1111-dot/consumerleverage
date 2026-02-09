-- ─────────────────────────────────────────────
-- ConsumerLeverage Core Schema
-- Run this BEFORE 001_fcra_vector_store.sql
-- ─────────────────────────────────────────────

-- Users (managed by Supabase Auth, this stores profile data)
create table if not exists users (
  id uuid primary key references auth.users(id) on delete cascade,
  email text unique not null,
  full_name text not null,
  address text,
  city_state_zip text,
  dob date,
  ssn_last4 text,
  created_at timestamptz default now()
);

-- Uploaded credit reports
create table if not exists credit_reports (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  bureau text not null check (bureau in ('equifax', 'experian', 'transunion')),
  raw_file_path text not null,
  parsed_data jsonb,
  status text default 'pending' check (status in ('pending', 'parsing', 'parsed', 'failed')),
  uploaded_at timestamptz default now()
);

create index if not exists credit_reports_user_idx on credit_reports(user_id);

-- Individual accounts extracted from reports
create table if not exists accounts (
  id uuid primary key default gen_random_uuid(),
  report_id uuid not null references credit_reports(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  creditor text not null,
  account_number_partial text,
  status text,
  balance numeric,
  date_opened date,
  date_reported date,
  payment_history text,
  remarks text,
  original_creditor text,
  bureau text not null check (bureau in ('equifax', 'experian', 'transunion')),
  created_at timestamptz default now()
);

create index if not exists accounts_user_idx on accounts(user_id);
create index if not exists accounts_report_idx on accounts(report_id);

-- Disputes initiated by user
create table if not exists disputes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  status text default 'draft' check (status in ('draft', 'analyzing', 'ready', 'sent', 'responded')),
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists disputes_user_idx on disputes(user_id);

-- Violations found by AI
create table if not exists violations (
  id uuid primary key default gen_random_uuid(),
  dispute_id uuid not null references disputes(id) on delete cascade,
  account_id uuid not null references accounts(id) on delete cascade,
  violation_type text not null,
  fcra_section text not null,
  universal_truth text check (universal_truth in ('accuracy', 'completeness', 'verifiability')),
  data_point text,
  description text,
  citation_text text,
  verified boolean default false,
  ai_confidence numeric,
  created_at timestamptz default now()
);

create index if not exists violations_dispute_idx on violations(dispute_id);

-- Generated dispute letters
create table if not exists letters (
  id uuid primary key default gen_random_uuid(),
  dispute_id uuid not null references disputes(id) on delete cascade,
  bureau text not null check (bureau in ('equifax', 'experian', 'transunion')),
  content text not null,
  version int default 1,
  status text default 'draft' check (status in ('draft', 'approved', 'sent')),
  pdf_path text,
  created_at timestamptz default now()
);

create index if not exists letters_dispute_idx on letters(dispute_id);

-- Audit log for every AI call
create table if not exists ai_audit_log (
  id uuid primary key default gen_random_uuid(),
  dispute_id uuid references disputes(id) on delete set null,
  step text not null check (step in ('parse', 'identify', 'cite', 'draft')),
  input_data jsonb,
  output_data jsonb,
  model text,
  tokens_used int,
  created_at timestamptz default now()
);

create index if not exists audit_dispute_idx on ai_audit_log(dispute_id);

-- ─────────────────────────────────────────────
-- Row Level Security (RLS)
-- Users can only access their own data
-- ─────────────────────────────────────────────

alter table users enable row level security;
alter table credit_reports enable row level security;
alter table accounts enable row level security;
alter table disputes enable row level security;
alter table violations enable row level security;
alter table letters enable row level security;

-- Users can read/update their own profile
create policy "Users can view own profile"
  on users for select using (auth.uid() = id);
create policy "Users can update own profile"
  on users for update using (auth.uid() = id);

-- Credit reports belong to user
create policy "Users can view own reports"
  on credit_reports for select using (auth.uid() = user_id);
create policy "Users can insert own reports"
  on credit_reports for insert with check (auth.uid() = user_id);

-- Accounts belong to user
create policy "Users can view own accounts"
  on accounts for select using (auth.uid() = user_id);
create policy "Users can insert own accounts"
  on accounts for insert with check (auth.uid() = user_id);

-- Disputes belong to user
create policy "Users can view own disputes"
  on disputes for select using (auth.uid() = user_id);
create policy "Users can insert own disputes"
  on disputes for insert with check (auth.uid() = user_id);
create policy "Users can update own disputes"
  on disputes for update using (auth.uid() = user_id);

-- Violations: user access through dispute → user chain
create policy "Users can view own violations"
  on violations for select using (
    exists (select 1 from disputes d where d.id = dispute_id and d.user_id = auth.uid())
  );
create policy "Users can insert own violations"
  on violations for insert with check (
    exists (select 1 from disputes d where d.id = dispute_id and d.user_id = auth.uid())
  );

-- Letters: user access through dispute → user chain
create policy "Users can view own letters"
  on letters for select using (
    exists (select 1 from disputes d where d.id = dispute_id and d.user_id = auth.uid())
  );
create policy "Users can insert own letters"
  on letters for insert with check (
    exists (select 1 from disputes d where d.id = dispute_id and d.user_id = auth.uid())
  );
create policy "Users can update own letters"
  on letters for update using (
    exists (select 1 from disputes d where d.id = dispute_id and d.user_id = auth.uid())
  );

-- Audit log: no direct user access (service role only)
-- No RLS policies = only service role key can read/write
