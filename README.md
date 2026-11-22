# Priority ERP → Supabase: a parallel database for your customer data

Read Priority once, keep a copy in your own Postgres, and point every tool at the copy.
Priority is called only to fetch what's new.

This kit copies the **CUSTOMERS** form from Priority's **public demo sandbox** into a Supabase
table, and keeps it up to date with a sync that runs **inside the database**: no servers, no Edge
Functions, no middleware.

## Files

| File | What it does |
|---|---|
| `01_schema_nopk.sql` | Creates the `erp` schema and the `erp.customers` table (one Priority customer per row, kept whole in `raw` jsonb). Row-level security on; the public API key gets no access. |
| `02_load_snapshot.py` | The backfill. Pulls customers from Priority (GET only) into `out/CUSTOMERS.json`, then inserts them through the Supabase Management API. Python standard library only. |
| `08_sync_new_customers.sql` | The daily sync: `http` extension, Priority login in Vault, `erp.sync_log`, and the `erp.sync_new_customers(days)` function, with a pg_cron schedule (commented). |
| `.env.example` | The variables the loader needs. Copy to `.env`. |

## Setup

**1. Credentials.** Copy `.env.example` to `.env` and fill in your Supabase project ref and a personal
access token (`sbp_…`, Dashboard → Account → Access Tokens). The Priority values are the sandbox's
published ones.

**2. Schema and backfill.**
```bash
python3 -I 02_load_snapshot.py schema              # run 01_schema_nopk.sql
python3 -I 02_load_snapshot.py pull CUSTOMERS      # Priority -> out/CUSTOMERS.json (+ CUSTOMERS.meta.json)
python3 -I 02_load_snapshot.py insert CUSTOMERS    # out/CUSTOMERS.json -> erp.customers
python3 -I 02_load_snapshot.py counts              # rows per table
```
The loader only accepts the public sandbox URL, skips any table that already has rows, and never deletes.

**3. The sync.** Paste `08_sync_new_customers.sql` into the Supabase SQL Editor and run it. Then:
```sql
select * from erp.sync_new_customers(1);   -- customers created yesterday or today
```

**4. Schedule it.** Enable `pg_cron` (Database → Extensions), then:
```sql
select cron.schedule('sync-new-customers-daily', '0 9 * * *',   -- 09:00 UTC every day
                     $cron$ select erp.sync_new_customers(1) $cron$);
```
Check the runs with `select * from erp.sync_log order by id desc limit 5;`.

**5. Read it over HTTPS (optional).** In Project Settings → Data API, add `erp` to the exposed schemas.
Then create a secret key (`sb_secret_…`, Project Settings → API Keys) and call:
```bash
curl -sS "https://<project-ref>.supabase.co/rest/v1/customers?select=custname,custdes&limit=3" \
  -H "apikey: sb_secret_..." -H "Accept-Profile: erp"
```
The secret key bypasses row-level security, so keep it on trusted servers only.

## Limits

- **New customers only.** The sync filters on `CREATEDDATE` and never updates a stored row, so edits
  made in Priority after a customer was copied are not picked up.
- **Sandbox only.** The scripts refuse any Priority URL other than the public demo. For a production
  tenant, use a personal access token of a **read-only** Priority user and mind your API transaction quota.
- Priority allows 100 API calls per minute and locks a user for 30 minutes after 3 failed logins in
  10 minutes. Every script here stops on the first 401 or 429 and never retries.

## License

[PolyForm Noncommercial 1.0.0](LICENSE). For commercial use, contact [Lemonade Operations](https://www.lemonade-operations.com/).
