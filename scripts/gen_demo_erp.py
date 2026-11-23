"""Generate the SAP-like `demo_erp` source schema in Neon + a READ-ONLY login role, and register it as a DEV PostgreSQL connection.

    python -m scripts.gen_demo_erp            # (re)build tables, create/rotate the reader role, register / update the connection
    python -m scripts.gen_demo_erp --drop     # remove schema demo_erp and the reader role (the connection is soft-deleted)

What it builds (SAP technical names, lower case; business IDs kept from sp.* so every row traces back to the KPI data):
  lfa1  vendor master            <- sp.dim_supplier            (phone / e-mail / tax no. are SYNTHETIC placeholders)
  mara  material master          <- sp.dim_material
  mbew  material valuation       <- sp.dim_material (standard price STPRS)
  ekko  PO header                <- sp.fact_purchase_order_receipt
  ekpo  PO item (NETPR / NETWR)  <- sp.fact_purchase_order_receipt; NETPR kept at full precision (real SAP: CURR 11,2 per PEINH) and
                                    ZZPOSTAT = customer Z-field carrying the PO status (SAP derives status; Z-fields are common)
  eket  schedule line (EINDT)    <- promised date
  ekbe  PO history (GR, VGABE 1) <- receipts
  mseg  material doc (BWART 101) <- receipts
  qals  inspection lot           <- receipts; usage decisions / rejections are SYNTHETIC (seeded) - sp has no incoming-inspection rows,
                                    so mapping QALS into the KPI layer (Phase 3) needs explicit approval.
Every table has AEDAT / CPUDT change dates for incremental (watermark) loads.

Security: the reader role gets CONNECT + USAGE + SELECT on demo_erp only, default_transaction_read_only=on. Its random password goes
straight into the vault (never printed, never in a file); re-running rotates it.
"""
from __future__ import annotations

import argparse
import random
import secrets as pysecrets
from urllib.parse import urlparse

from sqlalchemy import text

from app.config import settings
from app.db import engine

SCHEMA, ROLE, CONN_NAME = "demo_erp", "demo_erp_reader", "demo_erp - SAP-like source (Neon, read-only)"

DDL = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};
COMMENT ON SCHEMA {SCHEMA} IS 'DEMO source system (SAP-like) generated from sp.* for Data Integration demos and tests';
CREATE TABLE {SCHEMA}.lfa1 (lifnr varchar(20) PRIMARY KEY, name1 text NOT NULL, land1 varchar(3), ktokk varchar(4), sperr char(1),
  telf1 text, smtp_addr text, stcd3 text, zterm varchar(20), erdat date, aedat timestamptz);
CREATE TABLE {SCHEMA}.mara (matnr varchar(40) PRIMARY KEY, maktx text, matkl text, meins varchar(3), mhdhb integer, aedat timestamptz);
CREATE TABLE {SCHEMA}.mbew (matnr varchar(40), bwkey varchar(8), stprs numeric(15,2), peinh integer, aedat timestamptz, PRIMARY KEY (matnr, bwkey));
CREATE TABLE {SCHEMA}.ekko (ebeln varchar(20) PRIMARY KEY, bukrs varchar(4), bsart varchar(4), lifnr varchar(20), bedat date, waers varchar(5),
  inco1 varchar(3), aedat timestamptz);
CREATE TABLE {SCHEMA}.ekpo (ebeln varchar(20), ebelp integer, matnr varchar(40), werks varchar(12), menge numeric(13,3), meins varchar(3),
  netpr double precision, peinh integer, netwr numeric(17,2), elikz char(1), loekz char(1), zzpostat varchar(10), aedat timestamptz,
  PRIMARY KEY (ebeln, ebelp));
CREATE TABLE {SCHEMA}.eket (ebeln varchar(20), ebelp integer, etenr integer, eindt date, menge numeric(13,3), wemng numeric(13,3), aedat timestamptz,
  PRIMARY KEY (ebeln, ebelp, etenr));
CREATE TABLE {SCHEMA}.ekbe (ebeln varchar(20), ebelp integer, vgabe char(1), gjahr integer, belnr varchar(20), buzei integer, bwart varchar(3),
  budat date, menge numeric(13,3), dmbtr numeric(17,2), waers varchar(5), cpudt timestamptz, PRIMARY KEY (ebeln, ebelp, vgabe, gjahr, belnr, buzei));
CREATE TABLE {SCHEMA}.mseg (mblnr varchar(20), mjahr integer, zeile integer, bwart varchar(3), matnr varchar(40), werks varchar(12), lifnr varchar(20),
  menge numeric(13,3), ebeln varchar(20), ebelp integer, budat_mkpf date, cpudt_mkpf timestamptz, PRIMARY KEY (mblnr, mjahr, zeile));
CREATE TABLE {SCHEMA}.qals (prueflos varchar(20) PRIMARY KEY, art varchar(8), ebeln varchar(20), ebelp integer, mblnr varchar(20), mjahr integer,
  lifnr varchar(20), matnr varchar(40), werk varchar(12), losmenge numeric(13,3), lmenge01 numeric(13,3), lmenge04 numeric(13,3), vcode varchar(4),
  vdatum date, enstehdat date, aedat timestamptz, synthetic boolean DEFAULT true);
"""


def build(rng: random.Random) -> dict:
    with engine.begin() as c:
        for stmt in [s for s in DDL.split(";") if s.strip()]:
            c.execute(text(stmt))
        n = {}
        n["lfa1"] = c.execute(text(f"""INSERT INTO {SCHEMA}.lfa1 SELECT supplier_id, supplier_name, CASE country WHEN 'India' THEN 'IN' WHEN 'China' THEN 'CN'
              WHEN 'Taiwan' THEN 'TW' WHEN 'Japan' THEN 'JP' WHEN 'Vietnam' THEN 'VN' WHEN 'Singapore' THEN 'SG' WHEN 'South Korea' THEN 'KR'
              ELSE left(upper(country), 2) END, 'ZSUP',
              CASE WHEN approved_status ILIKE 'block%' THEN 'X' ELSE NULL END, '+00 000 000 ' || right(supplier_id, 3),
              lower(regexp_replace(supplier_id, '[^A-Za-z0-9]', '', 'g')) || '@supplier.example', 'DEMO-TAX-' || right(supplier_id, 3),
              payment_terms, DATE '2024-01-01', now() FROM sp.dim_supplier""")).rowcount
        n["mara"] = c.execute(text(f"""INSERT INTO {SCHEMA}.mara SELECT material_id, material_name, material_category, left(uom, 3), shelf_life_days, now()
              FROM sp.dim_material""")).rowcount
        n["mbew"] = c.execute(text(f"""INSERT INTO {SCHEMA}.mbew SELECT DISTINCT m.material_id, p.plant_id, m.standard_price_inr, 1, now()
              FROM sp.dim_material m JOIN (SELECT DISTINCT material_id, plant_id FROM sp.fact_purchase_order_receipt) p ON p.material_id = m.material_id""")).rowcount
        n["ekko"] = c.execute(text(f"""INSERT INTO {SCHEMA}.ekko SELECT DISTINCT ON (po_id) po_id, 'IN01', 'NB', supplier_id, po_date::date, currency,
              left(incoterm, 3), po_date::timestamptz + interval '1 hour' FROM sp.fact_purchase_order_receipt ORDER BY po_id, po_line""")).rowcount
        n["ekpo"] = c.execute(text(f"""INSERT INTO {SCHEMA}.ekpo SELECT po_id, po_line * 10, f.material_id, plant_id, ordered_qty, COALESCE(m.uom, 'EA'), unit_price_inr, 1,
              round((ordered_qty * unit_price_inr)::numeric, 2), CASE WHEN po_status = 'CLOSED' THEN 'X' END,
              CASE WHEN po_status = 'CANCELLED' THEN 'L' END, po_status, COALESCE(receipt_date::timestamptz, po_date::timestamptz) + interval '2 hour'
              FROM sp.fact_purchase_order_receipt f LEFT JOIN sp.dim_material m ON m.material_id = f.material_id""")).rowcount
        n["eket"] = c.execute(text(f"""INSERT INTO {SCHEMA}.eket SELECT po_id, po_line * 10, 1, promised_date::date, ordered_qty, COALESCE(received_qty, 0),
              COALESCE(receipt_date::timestamptz, po_date::timestamptz) + interval '2 hour' FROM sp.fact_purchase_order_receipt""")).rowcount
        rec = c.execute(text("""SELECT po_id, po_line, supplier_id, material_id, plant_id, receipt_date::date, received_qty, unit_price_inr, currency
                                FROM sp.fact_purchase_order_receipt WHERE receipt_date IS NOT NULL AND received_qty > 0 ORDER BY po_id, po_line""")).fetchall()
        ekbe, mseg, qals = [], [], []
        for i, (po, ln, sup, mat, plant, rd, qty, price, cur) in enumerate(rec, 1):
            mblnr, yr, ts = f"50{i:08d}", rd.year, f"{rd.isoformat()} 10:00:00+05:30"
            ekbe.append({"e": po, "p": ln * 10, "g": yr, "b": mblnr, "d": rd, "m": qty, "a": round(float(qty) * float(price), 2), "w": cur, "t": ts})
            mseg.append({"b": mblnr, "y": yr, "mat": mat, "wk": plant, "l": sup, "m": qty, "e": po, "p": ln * 10, "d": rd, "t": ts})
            rej = 0 if rng.random() > 0.08 else max(1, int(float(qty) * rng.uniform(0.002, 0.03)))
            qals.append({"q": f"0100{i:08d}", "e": po, "p": ln * 10, "b": mblnr, "y": yr, "l": sup, "mat": mat, "wk": plant, "m": qty,
                         "ok": float(qty) - rej, "rj": rej, "v": "A" if rej == 0 else ("A2" if rej < float(qty) * 0.01 else "R"), "d": rd, "t": ts})
        c.execute(text(f"""INSERT INTO {SCHEMA}.ekbe VALUES (:e, :p, '1', :g, :b, 1, '101', :d, :m, :a, :w, CAST(:t AS timestamptz))"""), ekbe)
        c.execute(text(f"""INSERT INTO {SCHEMA}.mseg VALUES (:b, :y, 1, '101', :mat, :wk, :l, :m, :e, :p, :d, CAST(:t AS timestamptz))"""), mseg)
        c.execute(text(f"""INSERT INTO {SCHEMA}.qals VALUES (:q, '01', :e, :p, :b, :y, :l, :mat, :wk, :m, :ok, :rj, :v, :d, :d,
                           CAST(:t AS timestamptz), true)"""), qals)
        n.update(ekbe=len(ekbe), mseg=len(mseg), qals=len(qals))
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        for t in ("lfa1", "mara", "mbew", "ekko", "ekpo", "eket", "ekbe", "mseg", "qals"):
            c.execute(text(f"ANALYZE {SCHEMA}.{t}"))                        # row estimates for schema discovery
    return n


def scram_verifier(password: str, iterations: int = 4096) -> str:
    """PostgreSQL SCRAM-SHA-256 verifier computed client-side (RFC 5802 / 7677)."""
    import base64
    import hashlib
    import hmac
    salt = pysecrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b(salt)}${b(stored_key)}:{b(server_key)}"


def ensure_reader() -> str:
    """Create or rotate the read-only role. Returns the NEW password to the caller only (goes straight into the vault)."""
    pw = pysecrets.token_urlsafe(32)
    db = urlparse(settings.database_url).path.lstrip("/").split("?")[0]
    with engine.begin() as c:
        exists = c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": ROLE}).fetchone()
        verb = "ALTER" if exists else "CREATE"
        # Self-managed Postgres: only a SCRAM verifier is sent, so the plain password never reaches the server or its logs.
        # Neon's control plane only accepts plaintext (it hashes it itself), so there the password is sent as a quoted literal
        # over TLS on a raw driver cursor; errors are re-raised WITHOUT the statement so the password can never be echoed.
        from psycopg import sql as psql
        neon = "neon.tech" in (urlparse(settings.database_url).hostname or "")
        attrs = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT " if verb == "CREATE" else ""     # rotation: password only
        stmt = psql.SQL(f"{verb} ROLE {ROLE} WITH {attrs}PASSWORD {{}}").format(
            psql.Literal(pw if neon else scram_verifier(pw)))
        try:
            with c.connection.driver_connection.cursor() as cur:
                cur.execute(stmt)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"could not {verb.lower()} role {ROLE}: {type(e).__name__} (statement withheld - it contains a secret)") from None
        c.execute(text(f"ALTER ROLE {ROLE} SET default_transaction_read_only = on"))
        c.execute(text(f"ALTER ROLE {ROLE} SET statement_timeout = '60s'"))
        c.execute(text(f'GRANT CONNECT ON DATABASE "{db}" TO {ROLE}'))
        c.execute(text(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {ROLE}"))
        c.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {ROLE}"))
    return pw


def register(pw: str) -> str:
    from app.integration import service as S
    from app.integration.secrets import vault
    u = urlparse(settings.database_url)
    cfg = {"host": u.hostname, "port": u.port or 5432, "database": u.path.lstrip("/"), "username": ROLE, "schema": SCHEMA,
           "ssl": True, "tls_verify": True, "query_timeout_s": 30}
    existing = [c for c in S.list_connections() if c["name"] == CONN_NAME]
    if existing:
        cid = existing[0]["id"]
        ref = S.get_connection(cid, with_secret_ref=True)["secret_ref"]
        vault().replace(ref, {**vault().get(ref), "password": pw}, "gen_demo_erp")       # rotate in place
        S.audit("gen_demo_erp", "secret_rotated", cid, {"secret_fields": ["password"]})
        return cid
    return S.create_connection("postgresql", CONN_NAME, "DEV", cfg, {"password": pw}, "gen_demo_erp")["id"]


def drop():
    from app.integration import service as S
    with engine.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        if c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": ROLE}).fetchone():
            db = urlparse(settings.database_url).path.lstrip("/").split("?")[0]
            c.execute(text(f'REVOKE CONNECT ON DATABASE "{db}" FROM {ROLE}'))
            c.execute(text(f"DROP ROLE {ROLE}"))
    for c in S.list_connections():
        if c["name"] == CONN_NAME:
            S.delete_connection(c["id"], "gen_demo_erp")
    print("demo_erp schema, reader role and connection removed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    if a.drop:
        return drop()
    counts = build(random.Random(a.seed))
    print("demo_erp rows:", counts)
    pw = ensure_reader()
    cid = register(pw)
    del pw
    print(f"read-only role {ROLE} ready (password stored in the vault only) - connection {cid}")


if __name__ == "__main__":
    main()
