"""Stage 1 loader over HTTPS only: no psql, no database password.

Two steps, with JSON files in between:
  pull    Priority PUBLIC DEMO sandbox (GET only)  ->  out/FORM.json      (+ out/FORM.meta.json)
  insert  out/FORM.json  ->  Supabase erp.<table>, through the Management API
          (POST /v1/projects/{ref}/database/query) with a personal access token.
Every Priority page and every Supabase insert is timed. Inserts go in batches of BATCH_ROWS (100), so each
batch's time is directly "seconds per 100 rows". Timings are printed and appended to out/timings.csv.

Usage (from this directory):
  time python3 -I 02_load_snapshot.py schema                  # run 01_schema_nopk.sql (erp.customers)
  time python3 -I 02_load_snapshot.py pull                    # CUSTOMERS, newest MAX_ROWS (400)
  time python3 -I 02_load_snapshot.py insert                  # load the saved JSON file
  time python3 -I 02_load_snapshot.py load                    # pull + insert in one go
  time python3 -I 02_load_snapshot.py pull CUSTOMERS          # any command also takes form names
  time MAX_ROWS=1000 python3 -I 02_load_snapshot.py pull      # bigger cap
  time python3 -I 02_load_snapshot.py counts                  # row count per table

To add another form, create its table in 01_schema_nopk.sql and add a line to FORMS below.

Knobs (env vars): MAX_ROWS=400, PAGE=100 (records per Priority request), BATCH_ROWS=100 (rows per insert),
SLEEP=0.7 (seconds between Priority requests).

Reads .env (next to this file):
  PRIORITY_SANDBOX_URL, PRIORITY_SANDBOX_USER, PRIORITY_SANDBOX_PASS
  SUPABASE_PROJECT_TOKEN   personal access token (sbp_...), Dashboard > Account > Access Tokens
  SUPABASE_PROJECT_REF     <project-ref>, from your project's URL https://<project-ref>.supabase.co

Safety:
  * Only the public demo sandbox URL is accepted (a production Priority is billed per call and holds PII).
  * Priority: GET only; stops on the first 401/429.
  * Supabase: CREATE ... IF NOT EXISTS and INSERT only. A screen whose table already has rows is skipped,
    so nothing is duplicated and nothing is deleted. To reload a screen, empty its table yourself.
"""

from base64 import b64encode
from datetime import datetime, timezone
from json import dump, dumps, load, loads
from os import environ
from pathlib import Path
from secrets import token_hex
from sys import argv, exit, stderr
from time import perf_counter, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
TIMINGS = OUT / "timings.csv"
DEMO_URL = "https://t.eu.priority-connect.online/odata/Priority/tabbtd38.ini/usdemo"
MGMT_API = "https://api.supabase.com/v1/projects/{ref}/database/query"
USER_AGENT = "priority-sandbox-loader/1.0"

MAX_ROWS = int(environ.get("MAX_ROWS", "400"))      # per screen
PAGE = int(environ.get("PAGE", "100"))              # records per Priority request (Priority's cap is 2,000)
BATCH_ROWS = int(environ.get("BATCH_ROWS", "100"))  # rows per Supabase insert request
SLEEP = float(environ.get("SLEEP", "0.7"))          # Priority allows 100 calls/min per tenant
MAX_BATCH_BYTES = 1_500_000                         # safety ceiling per insert request (API limit is ~2 MB)

# screen, table, key fields for a stable $orderby (from the sandbox $metadata), lines subform to $expand.
# A document form would look like: ("ORDERS", "erp.orders", ["ORDNAME"], "ORDERITEMS_SUBFORM")
FORMS = [
    ("CUSTOMERS", "erp.customers", ["CUSTNAME"], None),
]


def die(msg, code=1):
    print(msg, file=stderr)
    exit(code)


def read_env():
    """KEY=VALUE lines from .env, optional surrounding quotes, # comments ignored. Real env vars win."""
    env = {}
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    env.update({k: v for k, v in environ.items() if k.startswith(("PRIORITY_SANDBOX_", "SUPABASE_"))})
    return env


def need(env, *names):
    missing = [n for n in names if not env.get(n)]
    if missing:
        die("Missing in .env: " + ", ".join(missing))


def log_timing(phase, form, rows, kb, seconds):
    """Append one line to out/timings.csv: when, phase, form, rows, payload KB, seconds."""
    OUT.mkdir(exist_ok=True)
    new = not TIMINGS.exists()
    with TIMINGS.open("a", encoding="utf-8") as f:
        if new:
            f.write("at_utc,phase,form,rows,kb,seconds\n")
        at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        f.write(f"{at},{phase},{form},{rows},{kb:.1f},{seconds:.3f}\n")


def selected(wanted):
    unknown = set(wanted) - {f[0] for f in FORMS}
    if unknown:
        die("Unknown screen(s): " + ", ".join(sorted(unknown)))
    return [f for f in FORMS if not wanted or f[0] in wanted]


# ---------- Supabase (Management API) ----------

def run_sql(env, sql):
    """Run SQL on the project as the postgres role. Returns the rows of the last statement."""
    req = Request(
        MGMT_API.format(ref=env["SUPABASE_PROJECT_REF"]),
        data=dumps({"query": sql}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + env["SUPABASE_PROJECT_TOKEN"],
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(req, timeout=120) as res:
            body = res.read().decode("utf-8")
    except HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:800]
        if e.code in (401, 403):
            die(f"Supabase HTTP {e.code}: check SUPABASE_PROJECT_TOKEN / SUPABASE_PROJECT_REF. {detail}", 3)
        die(f"Supabase HTTP {e.code}: {detail}")
    except URLError as e:
        die(f"Supabase unreachable: {e.reason}")
    return loads(body) if body else []


def table_count(env, table):
    rows = run_sql(env, f"select count(*)::int as n from {table}")
    return int(rows[0]["n"])


def insert_batch(env, table, records_json):
    """records_json: list of JSON strings (one Priority record each). Inserted as raw jsonb."""
    payload = "[" + ",".join(records_json) + "]"
    tag = "j" + token_hex(8)
    if f"${tag}$" in payload:   # practically impossible; refuse rather than break the quoting
        die("Dollar-quote tag collision; run again.")
    run_sql(env, f"insert into {table} (raw) select value from jsonb_array_elements(${tag}${payload}${tag}$::jsonb)")


# ---------- Priority (GET only) ----------

def prio_get(env, path, params):
    query = "&".join(f"{k}={quote(v, safe=',')}" for k, v in params)   # keep $top etc. literal
    auth = b64encode(f"{env['PRIORITY_SANDBOX_USER']}:{env['PRIORITY_SANDBOX_PASS']}".encode("utf-8")).decode("ascii")
    req = Request(
        f"{env['PRIORITY_SANDBOX_URL'].rstrip('/')}/{path}?{query}",
        method="GET",
        headers={"Authorization": "Basic " + auth, "Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urlopen(req, timeout=180) as res:
            return res.read()
    except HTTPError as e:
        if e.code in (401, 429):
            die(f"HTTP {e.code} from Priority on {path}: stopping. Do not retry.", 3)
        die(f"Priority HTTP {e.code} on {path}: {e.read().decode('utf-8', 'replace')[:500]}")
    except URLError as e:
        die(f"Priority unreachable: {e.reason}")


def pull_form(env, form, keys, subform):
    """Newest-first pull, up to MAX_ROWS. Writes out/FORM.json (array) and out/FORM.meta.json."""
    orderby = ",".join(f"{k} desc" for k in keys)
    records, skip, complete, secs = [], 0, False, 0.0
    while len(records) < MAX_ROWS:
        top = min(PAGE, MAX_ROWS - len(records))
        params = [("$top", str(top)), ("$skip", str(skip)), ("$orderby", orderby)]
        if subform:
            params.append(("$expand", subform))
        t0 = perf_counter()
        body = prio_get(env, form, params)
        dt = perf_counter() - t0
        secs += dt
        page = loads(body.decode("utf-8")).get("value", [])
        for rec in page:
            rec.pop("@odata.etag", None)
        records.extend(page)
        log_timing("pull", form, len(page), len(body) / 1024, dt)
        print(f"  {form}: +{len(page)} (total {len(records)})  {dt:.2f}s  {len(body) / 1024:.0f} KB", file=stderr)
        if len(page) < top:
            complete = True
            break
        skip += len(page)
        sleep(SLEEP)
    OUT.mkdir(exist_ok=True)
    with (OUT / f"{form}.json").open("w", encoding="utf-8") as f:
        dump(records, f, ensure_ascii=False, indent=1)
    meta = {"form": form, "rows": len(records), "complete": complete, "max_rows": MAX_ROWS,
            "pulled_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "priority_seconds": round(secs, 3)}
    with (OUT / f"{form}.meta.json").open("w", encoding="utf-8") as f:
        dump(meta, f, indent=1)
    return meta


def insert_form(env, form, table):
    """Insert out/FORM.json into table in BATCH_ROWS batches, timing each. Returns a summary string."""
    src = OUT / f"{form}.json"
    if not src.exists():
        return f"{form}: no {src.name} (run pull first)"
    existing = table_count(env, table)
    if existing:
        return f"{form}: skipped ({table} already has {existing} rows)"
    with src.open(encoding="utf-8") as f:
        records = [dumps(r, ensure_ascii=False, separators=(",", ":")) for r in load(f)]
    meta_file = OUT / f"{form}.meta.json"
    complete = loads(meta_file.read_text(encoding="utf-8")).get("complete") if meta_file.exists() else None

    # batches of BATCH_ROWS rows, split further only if a batch would pass MAX_BATCH_BYTES
    batches, cur, size = [], [], 0
    for rec in records:
        if cur and (len(cur) >= BATCH_ROWS or size + len(rec) > MAX_BATCH_BYTES):
            batches.append(cur)
            cur, size = [], 0
        cur.append(rec)
        size += len(rec) + 1
    if cur:
        batches.append(cur)

    inserted, times = 0, []
    for i, batch in enumerate(batches, 1):
        kb = sum(len(r) + 1 for r in batch) / 1024
        t0 = perf_counter()
        try:
            insert_batch(env, table, batch)
        except SystemExit:
            print(f"{form}: insert failed after {inserted} rows. {table} now holds a partial load; "
                  f"empty it yourself before re-running this screen.", file=stderr)
            raise
        dt = perf_counter() - t0
        inserted += len(batch)
        times.append((len(batch), dt))
        log_timing("insert", form, len(batch), kb, dt)
        print(f"  {form}: batch {i}/{len(batches)}  {len(batch)} rows  {kb:.0f} KB  {dt:.2f}s", file=stderr)

    total = sum(t for _, t in times)
    per100 = (total / inserted * 100) if inserted else 0.0
    state = {True: "whole screen", False: "capped", None: "?"}[complete]
    return (f"{form:10} {inserted:5} rows ({state:12})  insert {total:6.2f}s total  "
            f"{per100:5.2f}s per 100 rows  (batches {min(t for _, t in times):.2f}-{max(t for _, t in times):.2f}s)"
            if times else f"{form}: 0 rows in {src.name}")


# ---------- commands ----------

def check_priority_env(env):
    need(env, "PRIORITY_SANDBOX_URL", "PRIORITY_SANDBOX_USER", "PRIORITY_SANDBOX_PASS")
    if env["PRIORITY_SANDBOX_URL"].rstrip("/") != DEMO_URL:
        die(f"PRIORITY_SANDBOX_URL is not the public demo sandbox ({DEMO_URL}). Refusing.", 2)


def cmd_schema(env):
    need(env, "SUPABASE_PROJECT_TOKEN", "SUPABASE_PROJECT_REF")
    run_sql(env, (HERE / "01_schema_nopk.sql").read_text(encoding="utf-8"))
    print("Schema applied (erp schema + erp.customers).")


def cmd_pull(env, wanted):
    check_priority_env(env)
    lines = []
    for form, _table, keys, subform in selected(wanted):
        print(f"== pull {form}", file=stderr)
        m = pull_form(env, form, keys, subform)
        state = "whole screen" if m["complete"] else "capped"
        lines.append(f"{form:10} {m['rows']:5} rows ({state:12})  Priority {m['priority_seconds']:6.2f}s  -> out/{form}.json")
    print("\nPull summary:")
    print("\n".join(lines))


def cmd_insert(env, wanted):
    need(env, "SUPABASE_PROJECT_TOKEN", "SUPABASE_PROJECT_REF")
    lines = []
    for form, table, _keys, _subform in selected(wanted):
        print(f"== insert {form}", file=stderr)
        lines.append(insert_form(env, form, table))
    print("\nInsert summary:")
    print("\n".join(lines))
    print(f"\nPer-request timings: {TIMINGS}")


def cmd_load(env, wanted):
    need(env, "SUPABASE_PROJECT_TOKEN", "SUPABASE_PROJECT_REF")
    check_priority_env(env)
    # Pull only the screens whose tables are empty, so a skipped screen costs no Priority calls.
    todo = []
    for form, table, _keys, _subform in selected(wanted):
        n = table_count(env, table)
        if n:
            print(f"== {form}: {table} already has {n} rows, skipping", file=stderr)
        else:
            todo.append(form)
    if todo:
        cmd_pull(env, todo)
        cmd_insert(env, todo)


def cmd_counts(env):
    need(env, "SUPABASE_PROJECT_TOKEN", "SUPABASE_PROJECT_REF")
    sql = " union all ".join(f"select '{t}' as tbl, count(*)::int as n from {t}" for _, t, _, _ in FORMS)
    for row in run_sql(env, sql):
        print(f"{row['tbl']:15} {row['n']}")


def main():
    commands = ("schema", "pull", "insert", "load", "counts")
    if len(argv) < 2 or argv[1] not in commands:
        die(__doc__)
    env = read_env()
    cmd, wanted = argv[1], argv[2:]
    if cmd == "schema":
        cmd_schema(env)
    elif cmd == "pull":
        cmd_pull(env, wanted)
    elif cmd == "insert":
        cmd_insert(env, wanted)
    elif cmd == "load":
        cmd_load(env, wanted)
    else:
        cmd_counts(env)


if __name__ == "__main__":
    main()
