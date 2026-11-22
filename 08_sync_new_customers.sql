-- Fetch NEW customers from Priority's PUBLIC DEMO sandbox into erp.customers, entirely inside Supabase.
-- Needs 01_schema_nopk.sql first (the erp schema and erp.customers). Everything else is set up here.
--
-- How it works:
--   * erp.sync_new_customers(days) asks Priority (GET only) for customers whose CREATEDDATE is on or after
--     the start of the day N days ago. CREATEDDATE is a date without time, hence days, not hours:
--     days = 0 -> created today (UTC), days = 1 -> since yesterday, ...
--   * The database makes the HTTP call itself, with the http extension (libcurl inside Postgres).
--   * Each returned customer is inserted into erp.customers only if its CUSTNAME isn't stored yet.
--     Nothing is updated or deleted.
--   * Every run is recorded in erp.sync_log (form = 'CUSTOMERS').
--   * Priority side: GET only. A 401/429 stops the run.
--
-- Run this whole file once in the Supabase SQL Editor. Safe to re-run (nothing is dropped or deleted).
-- Then:  select * from erp.sync_new_customers(1);   -- created yesterday or today
--        select * from erp.sync_new_customers(7);   -- created in the last week

-- 1. The http extension (synchronous HTTP client inside Postgres).
create extension if not exists http with schema extensions;

-- 2. Sandbox credentials in Vault (encrypted). These are Priority's published demo values.
--    Each secret is created only if it doesn't exist yet.
do $$
begin
  if not exists (select 1 from vault.secrets where name = 'priority_sandbox_url') then
    perform vault.create_secret('https://t.eu.priority-connect.online/odata/Priority/tabbtd38.ini/usdemo',
                                'priority_sandbox_url', 'Priority public demo OData root');
  end if;
  if not exists (select 1 from vault.secrets where name = 'priority_sandbox_user') then
    perform vault.create_secret('apidemo', 'priority_sandbox_user', 'Priority public demo user');
  end if;
  if not exists (select 1 from vault.secrets where name = 'priority_sandbox_pass') then
    perform vault.create_secret('123', 'priority_sandbox_pass', 'Priority public demo password');
  end if;
end $$;

-- 3. One row per sync run: the window asked for, rows fetched/inserted, duration, status and error.
create table if not exists erp.sync_log (
  id           bigint generated always as identity,
  form         text not null,
  since        timestamptz not null,
  started_at   timestamptz not null default clock_timestamp(),
  finished_at  timestamptz,
  fetched      int,
  inserted     int,
  seconds      numeric,
  status       text not null default 'running',   -- running | ok | error
  error        text
);
alter table erp.sync_log enable row level security;
revoke all on erp.sync_log from public, anon, authenticated;
grant select on erp.sync_log to service_role;

-- 4. The sync function.
create or replace function erp.sync_new_customers(p_days int default 1)
returns table (since timestamptz, fetched int, inserted int, seconds numeric, status text, error text)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_since    timestamptz := date_trunc('day', now() at time zone 'UTC') at time zone 'UTC' - make_interval(days => p_days);
  v_filter   text;
  v_url      text;
  v_auth     text;
  v_page     int := 100;
  v_skip     int := 0;
  v_resp     extensions.http_response;
  v_docs     jsonb;
  v_got      int;
  v_ins      int;
  v_fetched  int := 0;
  v_inserted int := 0;
  v_t0       timestamptz := clock_timestamp();
  v_log      bigint;
begin
  if p_days is null or p_days < 0 then
    raise exception 'p_days must be >= 0';
  end if;

  select s.decrypted_secret into v_url from vault.decrypted_secrets s where s.name = 'priority_sandbox_url';
  select 'Basic ' || replace(encode(convert_to(u.decrypted_secret || ':' || p.decrypted_secret, 'UTF8'), 'base64'), E'\n', '')
    into v_auth
    from vault.decrypted_secrets u, vault.decrypted_secrets p
   where u.name = 'priority_sandbox_user' and p.name = 'priority_sandbox_pass';
  if v_url is null or v_auth is null then
    raise exception 'Vault secrets priority_sandbox_url / _user / _pass are missing (run this whole file, including step 2)';
  end if;
  if rtrim(v_url, '/') <> 'https://t.eu.priority-connect.online/odata/Priority/tabbtd38.ini/usdemo' then
    raise exception 'priority_sandbox_url is not the public demo sandbox. Refusing.';
  end if;

  v_filter := 'CREATEDDATE ge ' || to_char(v_since at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"');

  insert into erp.sync_log (form, since) values ('CUSTOMERS', v_since) returning id into v_log;

  begin
    perform extensions.http_set_curlopt('CURLOPT_TIMEOUT', '170');   -- Priority drops requests after 3 min

    loop
      v_resp := extensions.http((
        'GET',
        rtrim(v_url, '/') || '/CUSTOMERS'
          || '?$filter='  || extensions.urlencode(v_filter::varchar)
          || '&$top='     || v_page
          || '&$skip='    || v_skip
          || '&$orderby=' || extensions.urlencode('CUSTNAME desc'::varchar),
        array[extensions.http_header('Authorization', v_auth),
              extensions.http_header('Accept', 'application/json')],
        null, null)::extensions.http_request);

      if v_resp.status in (401, 429) then
        raise exception 'HTTP % from Priority: stopping. Do not retry.', v_resp.status;
      elsif v_resp.status <> 200 then
        raise exception 'Priority HTTP %: %', v_resp.status, left(v_resp.content, 500);
      end if;

      v_docs := coalesce(v_resp.content::jsonb -> 'value', '[]'::jsonb);
      v_got  := jsonb_array_length(v_docs);
      v_fetched := v_fetched + v_got;

      -- Only customers we don't have yet (by CUSTNAME).
      insert into erp.customers (raw)
      select d.doc
        from (select e.value - '@odata.etag' as doc from jsonb_array_elements(v_docs) as e) as d
       where not exists (select 1 from erp.customers c where c.custname = d.doc->>'CUSTNAME');
      get diagnostics v_ins = row_count;
      v_inserted := v_inserted + v_ins;

      exit when v_got < v_page;
      v_skip := v_skip + v_got;
      perform pg_sleep(0.7);   -- Priority allows 100 calls/min
    end loop;

    update erp.sync_log l
       set finished_at = clock_timestamp(), fetched = v_fetched, inserted = v_inserted,
           seconds = round(extract(epoch from clock_timestamp() - v_t0)::numeric, 2), status = 'ok'
     where l.id = v_log;

  exception when others then
    -- Everything inserted by this run is rolled back; the log row keeps the error.
    update erp.sync_log l
       set finished_at = clock_timestamp(), fetched = v_fetched, inserted = 0,
           seconds = round(extract(epoch from clock_timestamp() - v_t0)::numeric, 2),
           status = 'error', error = left(sqlerrm, 1000)
     where l.id = v_log;
  end;

  return query
    select l.since, l.fetched, l.inserted, l.seconds, l.status, l.error from erp.sync_log l where l.id = v_log;
end $$;

-- erp is exposed through the Data API, and functions are executable by PUBLIC by default.
-- Lock it so only you (SQL Editor) and pg_cron can run it.
revoke all on function erp.sync_new_customers(int) from public, anon, authenticated, service_role;

-- 5. Optional: run it every day. Enable pg_cron first (Database > Extensions, or the line below).
--    Cron times are UTC: '0 9 * * *' is 09:00 UTC every day. A 1-day window on a daily job overlaps
--    on purpose; customers already stored are skipped.
--   create extension if not exists pg_cron;
--   select cron.schedule('sync-new-customers-daily', '0 9 * * *', $cron$ select erp.sync_new_customers(1) $cron$);
-- Change the time:  select cron.alter_job(job_id := (select jobid from cron.job where jobname = 'sync-new-customers-daily'),
--                                         schedule := '0 9 * * *');
-- Check:            select * from cron.job_run_details order by start_time desc limit 5;
-- Stop:             select cron.unschedule('sync-new-customers-daily');

-- See what the last runs brought in:
--   select * from erp.sync_log where form = 'CUSTOMERS' order by id desc limit 5;
--   select custname, custdes, raw->>'CREATEDDATE' as created, loaded_at
--     from erp.customers order by loaded_at desc, custname limit 20;
