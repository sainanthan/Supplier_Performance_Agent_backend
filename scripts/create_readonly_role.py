"""Create a read-only Postgres role for the agent's SQL tool and write READONLY_DATABASE_URL into .env.

Run from backend/ after load_data:   python -m scripts.create_readonly_role
The role can only SELECT from the `sp` schema. That role is the real safeguard against the
model writing or deleting data; the regex check in tools.py is a second layer.
"""
import re
import secrets
import string
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, quote

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import BACKEND_DIR, settings  # noqa: E402

ROLE = "agent_ro"
SAFE_ROLE = "agent_safe"     # enhancement 1.1: may read ONLY the approved KPI views + master data (no raw fact tables)
APPROVED = ["v_po_delivery", "v_defect_attribution", "v_incoming_quality", "v_ncr_supplier", "v_supply_disruption",
            "v_material_supplier_share", "v_schedule_delivery", "v_invoice_match",
            "dim_supplier", "dim_material", "dim_plant", "dim_defect_code", "dim_product", "dim_production_line",
            "work_calendar"]   # working-day calendar (not sensitive; needed by the inlined sp.working_days)


def main() -> None:
    alphabet = string.ascii_letters + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(32))  # Neon requires a strong password
    url = settings.db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    schema = settings.data_schema

    with psycopg.connect(url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL(verb + " ROLE {} WITH LOGIN PASSWORD {}").format(
            sql.Identifier(ROLE), sql.Literal(password)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(ROLE)))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(ROLE)))
        conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(
            sql.Identifier(schema), sql.Identifier(ROLE)))
        # Views call sp.cfg_num(), which reads public.app_config.
        conn.execute(sql.SQL("GRANT SELECT ON public.app_config, public.kpi_exclusions TO {}").format(sql.Identifier(ROLE)))
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(sql.Identifier(ROLE)))
        # Safe-query role: NOLOGIN; agent_ro switches to it (SET LOCAL ROLE) when feature 'safe_query_layer' is on.
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (SAFE_ROLE,)).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(SAFE_ROLE)))
        conn.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(sql.Identifier(schema), sql.Identifier(SAFE_ROLE)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(SAFE_ROLE)))
        for obj in APPROVED:
            conn.execute(sql.SQL("GRANT SELECT ON {}.{} TO {}").format(sql.Identifier(schema), sql.Identifier(obj), sql.Identifier(SAFE_ROLE)))
        conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(SAFE_ROLE), sql.Identifier(ROLE)))

    parts = urlsplit(url)
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    ro_url = urlunsplit((parts.scheme, f"{ROLE}:{quote(password)}@{host}", parts.path, parts.query, ""))

    env_path = BACKEND_DIR / ".env"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    line = f"READONLY_DATABASE_URL={ro_url}"
    if re.search(r"^READONLY_DATABASE_URL=.*$", text, flags=re.M):
        text = re.sub(r"^READONLY_DATABASE_URL=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    env_path.write_text(text, encoding="utf-8")
    print(f"Role '{ROLE}' ready. READONLY_DATABASE_URL written to {env_path}. Restart the backend.")


if __name__ == "__main__":
    main()
