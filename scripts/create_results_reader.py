"""Create (or rotate) a READ-ONLY login for sharing the agent's results with someone outside the project.

    python -m scripts.create_results_reader          # create / rotate; prints the connection string ONCE
    python -m scripts.create_results_reader --drop   # remove the login (access revoked immediately)

Grants SELECT only, on:
  public  agent outputs: scorecard_snapshots, calc_runs, alert_events, alert_notices, corrective_actions, predictions,
          twin_scenarios, disruption_events, report_exports, recon_results, dq_results, job_runs, kpi_exclusions, config_versions
  sp      KPI views (v_*) and master data (dim_*)
Never granted: app_users (password hashes), integration.* (encrypted secrets, source data), chat history (conversations,
messages, ai_usage), notifications, feature flags. Sessions are read-only with a 60 s statement timeout.
"""
from __future__ import annotations

import argparse
import secrets as pysecrets
from urllib.parse import urlparse

from sqlalchemy import text

from app.config import settings
from app.db import engine

ROLE = "results_reader"
PUBLIC_OUTPUTS = ["scorecard_snapshots", "calc_runs", "alert_events", "alert_notices", "corrective_actions", "predictions", "twin_scenarios",
                  "disruption_events", "report_exports", "recon_results", "dq_results", "job_runs", "kpi_exclusions", "config_versions"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", action="store_true")
    ap.add_argument("--password", help="use this password instead of a generated one (a strong random one is recommended)")
    a = ap.parse_args()
    u = urlparse(settings.database_url)
    db = u.path.lstrip("/").split("?")[0]
    with engine.begin() as c:
        exists = c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": ROLE}).fetchone()
        if a.drop:
            if exists:
                c.execute(text(f"REASSIGN OWNED BY {ROLE} TO CURRENT_USER"))
                c.execute(text(f"DROP OWNED BY {ROLE}"))
                c.execute(text(f"DROP ROLE {ROLE}"))
            print(f"{ROLE} removed")
            return
        pw = a.password or pysecrets.token_urlsafe(24)
        from psycopg import sql as psql
        attrs = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT " if not exists else ""
        stmt = psql.SQL(f"{'ALTER' if exists else 'CREATE'} ROLE {ROLE} WITH {attrs}PASSWORD {{}}").format(psql.Literal(pw))
        try:
            with c.connection.driver_connection.cursor() as cur:
                cur.execute(stmt)
        except Exception as e:  # noqa: BLE001
            hint = " - the database requires a stronger password" if "password" in str(e).lower() and ("strong" in str(e).lower() or "entropy" in str(e).lower()) else ""
            raise RuntimeError(f"could not set up {ROLE}: {type(e).__name__}{hint} (statement withheld - it contains a password)") from None
        c.execute(text(f"ALTER ROLE {ROLE} SET default_transaction_read_only = on"))
        c.execute(text(f"ALTER ROLE {ROLE} SET statement_timeout = '60s'"))
        c.execute(text(f'GRANT CONNECT ON DATABASE "{db}" TO {ROLE}'))
        c.execute(text(f"GRANT USAGE ON SCHEMA public, sp TO {ROLE}"))
        have = {r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
        for t in PUBLIC_OUTPUTS:
            if t in have:
                c.execute(text(f'GRANT SELECT ON public."{t}" TO {ROLE}'))
        for (t,) in c.execute(text("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                                      WHERE n.nspname = 'sp' AND ((c.relkind IN ('v','m') AND c.relname LIKE 'v\\_%') OR (c.relkind = 'r' AND c.relname LIKE 'dim\\_%'))""")).fetchall():
            c.execute(text(f'GRANT SELECT ON sp."{t}" TO {ROLE}'))
    print(f"postgresql://{ROLE}:{pw}@{u.hostname}:{u.port or 5432}/{db}?sslmode=require&channel_binding=require")


if __name__ == "__main__":
    main()
