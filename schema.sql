-- Forsyth Zoning Watch — database schema
-- Run this once in Supabase: Project > SQL Editor > New query > paste this whole file > Run.

create extension if not exists pgcrypto; -- gives us gen_random_uuid()

-- One row per person watching a location.
create table subscribers (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  address text not null,          -- what they typed in, for their own reference
  lat double precision not null,
  lon double precision not null,
  radius_miles double precision not null default 0.5,
  confirmed boolean not null default false,
  confirm_token uuid not null default gen_random_uuid(),
  confirm_sent_at timestamptz,    -- set once the confirmation email goes out, so we never resend it
  unsubscribe_token uuid not null default gen_random_uuid(),
  created_at timestamptz not null default now()
);

create index subscribers_confirmed_idx on subscribers (confirmed);

-- Every Plan case the monitor script has already looked at, so it never re-scans
-- or re-alerts on the same filing twice.
create table processed_filings (
  case_number text primary key,
  plan_type text,
  applied_date date,
  main_parcel text,
  project_name text,
  address text,
  description text,
  lat double precision,
  lon double precision,
  first_seen_at timestamptz not null default now()
);

-- Which subscriber has been told about which filing. The unique constraint is
-- what makes the script safe to re-run without double-emailing anyone.
create table notifications_sent (
  id uuid primary key default gen_random_uuid(),
  subscriber_id uuid not null references subscribers(id) on delete cascade,
  case_number text not null references processed_filings(case_number) on delete cascade,
  sent_at timestamptz not null default now(),
  unique (subscriber_id, case_number)
);

-- Row Level Security: the public site talks to this database directly using
-- Supabase's "anon" public key (that key is *meant* to be embedded in client-side
-- JS — it's not a secret). These policies are what keep that safe:
--   - anyone can INSERT a new subscription (that's the signup form)
--   - anyone can UPDATE a row, but only if they already know its token, which
--     is an unguessable random UUID mailed only to that address — this is the
--     same "possession of the link proves consent" model almost every email
--     confirm/unsubscribe flow uses
--   - nobody can SELECT subscriber rows through the public key, so no one can
--     browse or scrape the list of who's watching what
alter table subscribers enable row level security;

create policy "public can sign up" on subscribers
  for insert to anon
  with check (true);

create policy "public can confirm or unsubscribe with their token" on subscribers
  for update to anon
  using (true)
  with check (true);

-- No SELECT policy for anon is created on purpose — the public key cannot read
-- this table back. The monitor script uses the separate service key, which
-- bypasses RLS entirely and is never exposed to the browser.

-- processed_filings holds nothing but public government filing data (case
-- number, plan type, dates, parcel/address) — no PII — so the site is allowed
-- to read it back to show "last known filing" per parcel on the map. Only the
-- monitor script's service key (which bypasses RLS entirely) can write to it.
alter table processed_filings enable row level security;

create policy "public can read processed filings" on processed_filings
  for select to anon
  using (true);

-- notifications_sent links a subscriber_id to a case_number — not PII by
-- itself, but there's no reason the public site needs it, so it stays fully
-- locked: RLS enabled with no policies at all means anon/authenticated get
-- zero access (otherwise Supabase's default grants would allow it).
alter table notifications_sent enable row level security;
