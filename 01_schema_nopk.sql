-- Priority sandbox mirror: the erp schema and the customers table. No primary key yet.
-- Run once in the Supabase SQL editor, or:  python3 -I 02_load_snapshot.py schema
--
-- Design:
--   * One row per Priority customer, kept whole in raw (jsonb): all of the form's fields, exactly as the
--     API returned them. A new Priority field never breaks the load; it simply appears inside raw.
--   * A few columns are generated from raw so they can be filtered, sorted and indexed.
--   * No primary key or unique constraint yet. The key, from the sandbox $metadata, is CUSTNAME.
--     A plain (non-unique) index keeps lookups fast in the meantime.
--   * Re-running a load would duplicate rows, so 02_load_snapshot.py refuses to load into a non-empty table.
--   * Locked down: no grants for anon/authenticated, RLS on. service_role (the secret key) can read.
--
-- Safe to re-run: every statement is "if not exists". Nothing here drops or deletes.

-- A separate schema keeps the ERP mirror apart from everything else in the project.
create schema if not exists erp;

create table if not exists erp.customers (
  raw        jsonb not null,                                            -- the full Priority record
  custname   text generated always as (raw->>'CUSTNAME') stored,        -- customer number (Priority's key)
  custdes    text generated always as (raw->>'CUSTDES')  stored,        -- customer name
  loaded_at  timestamptz not null default now()                        -- when the row arrived in Supabase
);

-- Fast lookups by customer number.
create index if not exists customers_custname_idx on erp.customers (custname);

-- RLS on: anything that isn't the owner or service_role sees nothing unless a policy allows it.
alter table erp.customers enable row level security;

-- Grants: nothing for anon/authenticated (the publishable key reads nothing). service_role may read.
revoke all on schema erp from public, anon, authenticated;
revoke all on all tables in schema erp from public, anon, authenticated;
revoke all on all functions in schema erp from public, anon, authenticated;
grant usage on schema erp to service_role;
grant select on all tables in schema erp to service_role;
alter default privileges in schema erp revoke all on tables from public, anon, authenticated;
alter default privileges in schema erp revoke execute on functions from public, anon, authenticated;
