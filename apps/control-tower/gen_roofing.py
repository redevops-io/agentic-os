#!/usr/bin/env python3
"""Generate a realistic Summit Roofing Co. dataset as roofing.sql (Postgres).

Deterministic (seeded), current through TODAY, with roofing-real seasonality (spring/summer
peak, winter trough, two storm spikes), a service mix with distinct margins, lead sources with
distinct conversion + margin, regions, crews, and an invoices table for AR aging. Loaded into the
control-tower-db Postgres on first boot (docker-entrypoint-initdb.d) and queried live by Metabase.

    python3 gen_roofing.py > roofing.sql
"""
from __future__ import annotations

import random
from datetime import date, timedelta

random.seed(1704)
TODAY = date(2026, 7, 4)
START = date(2024, 8, 1)                     # ~23 months of history

REGIONS = ["North Metro", "West Suburbs", "Downtown", "East County", "Lakeside"]
CREWS = ["Crew A", "Crew B", "Crew C", "Crew D"]
# lead source -> (share of quotes, win-rate, margin multiplier). Storm chases high volume/low margin;
# referrals & repeat customers convert best and hold margin.
LEAD_SOURCES = {
    "Referral":       (0.24, 0.58, 1.08),
    "Repeat Customer":(0.14, 0.70, 1.12),
    "Google Ads":     (0.22, 0.40, 0.96),
    "Storm Response": (0.20, 0.52, 0.90),
    "Nextdoor":       (0.10, 0.46, 1.00),
    "Home Show":      (0.10, 0.44, 1.02),
}
# service -> (share, price range, material%, labor%). Margin = 1 - material% - labor%.
SERVICES = {
    "Roof Replacement": (0.34, (8000, 28000), 0.40, 0.30),
    "Storm Damage":     (0.18, (6000, 34000), 0.42, 0.29),
    "Roof Repair":      (0.24, (450, 3800),   0.30, 0.34),
    "Gutter Install":   (0.12, (700, 4200),   0.38, 0.30),
    "Skylight":         (0.06, (1400, 5200),  0.44, 0.28),
    "Inspection":       (0.06, (0, 450),      0.05, 0.60),   # loss-leader / lead-gen
}
# month -> demand weight (1=Jan). Roofing peaks Apr-Sep; Nov-Feb slow.
SEASON = {1: 0.55, 2: 0.60, 3: 0.85, 4: 1.15, 5: 1.30, 6: 1.35,
          7: 1.30, 8: 1.20, 9: 1.10, 10: 0.95, 11: 0.70, 12: 0.55}
# storm spikes (extra Storm Response volume) in these year-months
STORMS = {(2025, 5), (2025, 6), (2026, 4)}

FIRST = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda", "David",
         "Barbara", "William", "Susan", "Richard", "Karen", "Joseph", "Nancy", "Tom", "Lisa",
         "Dan", "Betty", "Paul", "Sandra", "Mark", "Ashley", "Greg", "Donna", "Kyle", "Carol"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez",
        "Martinez", "Hernandez", "Lopez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson",
        "Martin", "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Nguyen", "Hill"]


def weighted(d):
    keys = list(d.keys())
    weights = [d[k][0] if isinstance(d[k], tuple) else d[k] for k in keys]
    return random.choices(keys, weights=weights, k=1)[0]


def month_iter(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m, y = 1, y + 1


rows_customers, rows_jobs, rows_invoices = [], [], []
cust_id = job_id = inv_id = 0
customer_pool: list[int] = []

for (y, m) in month_iter(START, TODAY):
    base = 44 * SEASON[m]                                # quotes this month
    if (y, m) in STORMS:
        base *= 1.7
    n_quotes = max(3, int(random.gauss(base, base * 0.15)))
    for _ in range(n_quotes):
        day = min(28, max(1, int(random.gauss(15, 8))))
        quote_date = date(y, m, day)
        if quote_date > TODAY:
            continue
        # customer: ~30% repeat from the pool, else new
        if customer_pool and random.random() < 0.30:
            c_id = random.choice(customer_pool)
            lead = "Repeat Customer"
        else:
            cust_id += 1
            c_id = cust_id
            name = f"{random.choice(FIRST)} {random.choice(LAST)}"
            region = random.choice(REGIONS)
            seg = random.choices(["Residential", "Commercial", "Property Mgmt"],
                                 weights=[0.72, 0.16, 0.12])[0]
            rows_customers.append((c_id, name, region, seg, quote_date))
            customer_pool.append(c_id)
            lead = weighted(LEAD_SOURCES)
        # storms push Storm Response leads/services
        if (y, m) in STORMS and random.random() < 0.5:
            lead, service = "Storm Response", "Storm Damage"
        else:
            service = weighted(SERVICES)
        region = random.choice(REGIONS)
        crew = random.choice(CREWS)
        share, (lo, hi), mat_pct, lab_pct = SERVICES[service]
        quoted = round(random.uniform(lo, hi), 2) if hi > 0 else round(random.uniform(0, 450), 2)
        _, win, margin_mult = LEAD_SOURCES[lead]
        won = random.random() < win
        job_id += 1
        # material/labor with a little noise; margin scaled by lead-source quality
        mat = round(quoted * mat_pct * random.uniform(0.92, 1.08), 2)
        lab = round(quoted * lab_pct * random.uniform(0.92, 1.08) / max(0.6, margin_mult), 2)
        if not won:
            status = "Lost"
            invoiced = 0.0
            completed = None
            scheduled = None
        else:
            # completed vs still in the pipeline, based on how long ago the quote was
            age = (TODAY - quote_date).days
            sched = quote_date + timedelta(days=random.randint(7, 45))
            if sched > TODAY:
                status, completed, scheduled, invoiced = "Scheduled", None, sched, 0.0
            elif age < 20 and random.random() < 0.5:
                status, completed, scheduled, invoiced = "In Progress", None, sched, 0.0
            else:
                comp = sched + timedelta(days=random.randint(1, 10))
                if comp > TODAY:
                    status, completed, scheduled, invoiced = "Scheduled", None, sched, 0.0
                else:
                    status, completed, scheduled = "Completed", comp, sched
                    invoiced = round(quoted * random.uniform(0.97, 1.06), 2)  # change orders
        rows_jobs.append((job_id, c_id, service, region, crew, lead, status,
                          round(quoted, 2), round(invoiced, 2), mat, lab,
                          quote_date, scheduled, completed))
        # invoice for completed jobs → AR aging. Payment likelihood rises with age: old invoices
        # are almost all collected; only recent ones (and a small late-payer tail) stay Open.
        if status == "Completed":
            inv_id += 1
            issued = completed
            due = issued + timedelta(days=30)
            inv_age = (TODAY - issued).days
            if inv_age <= 25:
                open_prob = 0.60           # not due yet — mostly still open
            elif inv_age <= 45:
                open_prob = 0.30
            elif inv_age <= 90:
                open_prob = 0.12           # late payers
            else:
                open_prob = 0.03           # stragglers / bad debt
            if random.random() >= open_prob:
                # paid: on time-ish, but never in the future
                paid = min(TODAY, issued + timedelta(days=random.randint(5, 38)))
                rows_invoices.append((inv_id, job_id, round(invoiced, 2), issued, due, paid, "Paid"))
            else:
                rows_invoices.append((inv_id, job_id, round(invoiced, 2), issued, due, None, "Open"))


def sql_str(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, date):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def emit(table, cols, rows):
    print(f"\n-- {len(rows)} {table}")
    for chunk_start in range(0, len(rows), 500):
        chunk = rows[chunk_start:chunk_start + 500]
        print(f"INSERT INTO {table} ({', '.join(cols)}) VALUES")
        vals = ",\n".join("(" + ", ".join(sql_str(v) for v in r) + ")" for r in chunk)
        print(vals + ";")


print("-- Summit Roofing Co. — generated by gen_roofing.py (deterministic). DO NOT EDIT BY HAND.")
print("""
DROP TABLE IF EXISTS invoices CASCADE;
DROP TABLE IF EXISTS jobs CASCADE;
DROP TABLE IF EXISTS customers CASCADE;

CREATE TABLE customers (
  id integer PRIMARY KEY, name text, region text, segment text, created_at date);

CREATE TABLE jobs (
  id integer PRIMARY KEY, customer_id integer REFERENCES customers(id),
  service_type text, region text, crew text, lead_source text, status text,
  quoted_amount numeric(12,2), invoiced_amount numeric(12,2),
  material_cost numeric(12,2), labor_cost numeric(12,2),
  quote_date date, scheduled_date date, completed_date date);

CREATE TABLE invoices (
  id integer PRIMARY KEY, job_id integer REFERENCES jobs(id),
  amount numeric(12,2), issued_date date, due_date date, paid_date date, status text);
""")
emit("customers", ["id", "name", "region", "segment", "created_at"], rows_customers)
emit("jobs", ["id", "customer_id", "service_type", "region", "crew", "lead_source", "status",
              "quoted_amount", "invoiced_amount", "material_cost", "labor_cost",
              "quote_date", "scheduled_date", "completed_date"], rows_jobs)
emit("invoices", ["id", "job_id", "amount", "issued_date", "due_date", "paid_date", "status"],
     rows_invoices)
print("\nCREATE INDEX idx_jobs_completed ON jobs(completed_date);")
print("CREATE INDEX idx_jobs_quote ON jobs(quote_date);")
print("CREATE INDEX idx_inv_status ON invoices(status);")

import sys  # noqa: E402
print(f"-- summary: {len(rows_customers)} customers, {len(rows_jobs)} jobs, "
      f"{len(rows_invoices)} invoices", file=sys.stderr)
