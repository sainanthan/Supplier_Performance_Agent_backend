"""Load the supplier-performance CSV datasets into Neon (schema `sp`) and build the KPI views.

Run from the backend/ folder:
    python -m scripts.load_data            # load everything (drops & reloads the sp tables)
    python -m scripts.load_data --views    # only rebuild views (no data reload)
"""
import argparse
import csv
import re
import sys
import time
from pathlib import Path

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import BACKEND_DIR, settings  # noqa: E402
from app.db import init_db  # noqa: E402

SCHEMA = settings.data_schema

# Core files from the requirement + supporting tables used for attribution and exposure.
TABLES = {
    "dim_supplier": "Supplier master and risk profile",
    "dim_material": "Purchased material master (standard price, lead time)",
    "dim_plant": "Plants",
    "dim_defect_code": "Defect code catalogue",
    "dim_product": "Finished products",
    "fact_purchase_order_receipt": "PO lines: promised vs actual receipt dates and quantities",
    "fact_quality_inspection": "Inspection outcomes (PASS/FAIL, defect code)",
    "fact_ncr_capa": "Non-conformance reports and corrective actions",
    "fact_rfq_quote": "Supplier quotes (commercial performance)",
    "fact_product_genealogy_event": "Serial/lot genealogy linking production orders to supplier lots",
    "fact_bom_component": "Bill of materials (which products use which materials)",
    "fact_production_order": "Production orders (line/customer exposure)",
    "fact_downtime_event": "Downtime incl. NO_MATERIAL stoppages (supply disruption)",
    # Digital twin inputs
    "fact_inventory_daily_snapshot": "Daily inventory by plant/material/lot (twin starting stock)",
    "fact_customer_order": "Customer orders (twin backlog exposure)",
    "fact_digital_twin_scenario": "Factory twin runs (model TWIN-1.7): predicted vs actual line output",
    "dim_production_line": "Production lines and nominal capacity",
    "dim_shift_calendar": "Shift calendar: working days for lead-time / delay in working days",
}

# Optional ERP/QMS extracts. Loaded when the CSV exists; otherwise the empty table from views.sql is kept
# and the KPIs fall back to the PO-receipt / production-inspection proxies.
OPTIONAL = {
    "fact_grn": "Goods receipts (split deliveries per PO line)",
    "fact_incoming_inspection": "Incoming (GRN) inspection lots: direct supplier rejection",
    "fact_schedule_line": "Scheduling-agreement delivery lines",
    "fact_invoice": "Supplier invoices (three-way match)",
    "fact_inbound_milestone": "Inbound shipment milestones / ASN events",
}

# Declared primary keys: a duplicate is a hard load failure, not a silent skip.
PKS = {
    "dim_supplier": ["supplier_id"], "dim_material": ["material_id"], "dim_plant": ["plant_id"],
    "dim_defect_code": ["defect_code"], "dim_product": ["product_id"],
    "fact_purchase_order_receipt": ["po_id", "po_line"], "fact_quality_inspection": ["inspection_id"],
    "fact_ncr_capa": ["ncr_id"], "dim_production_line": ["line_id"],
    "dim_shift_calendar": ["calendar_date", "shift_id"], "fact_grn": ["grn_id"],
    "fact_incoming_inspection": ["inspection_lot_id"], "fact_invoice": ["invoice_id"],
}

INT_RE = re.compile(r"^-?\d+$")
NUM_RE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?$")


def infer_type(values: list[str]) -> str:
    vals = [v for v in values if v != ""]
    if not vals:
        return "TEXT"
    if all(v in ("True", "False", "true", "false") for v in vals):
        return "BOOLEAN"
    if all(INT_RE.match(v) for v in vals):
        return "BIGINT" if any(abs(int(v)) > 2_000_000_000 for v in vals) else "INTEGER"
    if all(NUM_RE.match(v) for v in vals):
        return "DOUBLE PRECISION"
    if all(DATE_RE.match(v) for v in vals):
        return "DATE"
    if all(TS_RE.match(v) for v in vals):
        return "TIMESTAMP"
    return "TEXT"


def plain_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def load_table(cur, name: str, path: Path, comment: str) -> int:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header, body = rows[0], rows[1:]
    types = [infer_type([r[i] for r in body]) for i in range(len(header))]
    cols_sql = ", ".join(f'"{c}" {t}' for c, t in zip(header, types))

    cur.execute(f'DROP TABLE IF EXISTS {SCHEMA}."{name}" CASCADE')
    cur.execute(f'CREATE TABLE {SCHEMA}."{name}" ({cols_sql})')
    with path.open("rb") as f, cur.copy(
        f"COPY {SCHEMA}.\"{name}\" FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')"
    ) as cp:
        while chunk := f.read(1 << 20):
            cp.write(chunk)

    # Declared primary key (fail loudly on duplicates); else first *_id column when unique.
    first = header[0]
    pk = PKS.get(name)
    if pk:
        idx = [header.index(c) for c in pk]
        keys = [tuple(r[i] for i in idx) for r in body]
        if len(set(keys)) != len(keys):
            dup = next(k for k in keys if keys.count(k) > 1)
            raise SystemExit(f"PRIMARY KEY violation in {name} {pk}: duplicate {dup} - fix the extract before loading")
        cur.execute(f'ALTER TABLE {SCHEMA}."{name}" ADD PRIMARY KEY ({", ".join(chr(34) + c + chr(34) for c in pk)})')
    elif first.endswith("_id") and len({r[0] for r in body}) == len(body):
        cur.execute(f'ALTER TABLE {SCHEMA}."{name}" ADD PRIMARY KEY ("{first}")')
    for col in ("supplier_id", "material_id", "production_order_id", "inspection_id", "plant_id"):
        if col in header and col != first:
            cur.execute(f'CREATE INDEX ON {SCHEMA}."{name}" ("{col}")')
    cur.execute(sql.SQL("COMMENT ON TABLE {}.{} IS {}").format(
        sql.Identifier(SCHEMA), sql.Identifier(name), sql.Literal(comment)))
    return len(body)


def _owned_by_connector(cur, table: str) -> str | None:
    """Data Integration: a table owned by a connector must not be overwritten by a CSV reload."""
    try:
        cur.execute("SELECT owner FROM integration.table_ownership WHERE canonical_table = %s", (table,))
        r = cur.fetchone()
        return r[0] if r and r[0] != "CSV_LOADER" else None
    except Exception:
        cur.connection.rollback() if False else None
        return None


def build_views(cur) -> None:
    sql = (BACKEND_DIR / "app" / "sql" / "views.sql").read_text(encoding="utf-8")
    cur.execute(sql)


# Data-quality rules (G2). Each returns the number of offending rows; results go to public.dq_results.
DQ_RULES = [
    ("po_receipt_before_po_date", "ERROR", "SELECT COUNT(*) FROM sp.fact_purchase_order_receipt WHERE receipt_date < po_date"),
    ("po_promise_before_po_date", "ERROR", "SELECT COUNT(*) FROM sp.fact_purchase_order_receipt WHERE promised_date < po_date"),
    ("po_non_positive_qty", "ERROR", "SELECT COUNT(*) FROM sp.fact_purchase_order_receipt WHERE ordered_qty <= 0"),
    ("po_negative_received", "ERROR", "SELECT COUNT(*) FROM sp.fact_purchase_order_receipt WHERE received_qty < 0"),
    ("po_over_receipt_gt_50pct", "WARN", "SELECT COUNT(*) FROM sp.fact_purchase_order_receipt WHERE received_qty > 1.5 * ordered_qty"),
    ("po_orphan_supplier", "ERROR", "SELECT COUNT(*) FROM sp.fact_purchase_order_receipt p LEFT JOIN sp.dim_supplier s USING (supplier_id) WHERE s.supplier_id IS NULL"),
    ("po_orphan_material", "ERROR", "SELECT COUNT(*) FROM sp.fact_purchase_order_receipt p LEFT JOIN sp.dim_material m USING (material_id) WHERE m.material_id IS NULL"),
    ("po_null_promise", "ERROR", "SELECT COUNT(*) FROM sp.fact_purchase_order_receipt WHERE promised_date IS NULL"),
    ("insp_fail_without_defect", "WARN", "SELECT COUNT(*) FROM sp.fact_quality_inspection WHERE result = 'FAIL' AND (defect_code IS NULL OR defect_code = '')"),
    ("insp_unknown_defect_code", "ERROR", "SELECT COUNT(*) FROM sp.fact_quality_inspection q LEFT JOIN sp.dim_defect_code d USING (defect_code) WHERE q.result='FAIL' AND d.defect_code IS NULL"),
    ("insp_fail_unattributed", "WARN", "SELECT COUNT(*) FROM sp.fact_quality_inspection q WHERE q.result='FAIL' AND NOT EXISTS (SELECT 1 FROM sp.v_defect_attribution a WHERE a.inspection_id=q.inspection_id)"),
    ("ncr_orphan_inspection", "ERROR", "SELECT COUNT(*) FROM sp.fact_ncr_capa n LEFT JOIN sp.fact_quality_inspection q USING (inspection_id) WHERE q.inspection_id IS NULL"),
    ("ncr_due_before_open", "WARN", "SELECT COUNT(*) FROM sp.fact_ncr_capa WHERE capa_due_date < opened_ts::date"),
    ("material_null_lead_time", "WARN", "SELECT COUNT(*) FROM sp.dim_material WHERE lead_time_days IS NULL"),
    ("calendar_gaps_in_po_window", "WARN", "SELECT COUNT(*) FROM sp.work_calendar w WHERE NOT w.from_shift_calendar AND w.d BETWEEN (SELECT MIN(po_date) FROM sp.fact_purchase_order_receipt) AND (SELECT MAX(receipt_date) FROM sp.fact_purchase_order_receipt)"),
]


def run_dq(cur, load_id: str) -> list[tuple]:
    """Execute every DQ rule; store results under one load_id. Returns (rule, severity, failed, status)."""
    out = []
    for name, sev, q in DQ_RULES:
        cur.execute("SAVEPOINT dq")
        try:
            cur.execute(q)
            n = int(cur.fetchone()[0])
            cur.execute("RELEASE SAVEPOINT dq")
        except Exception as e:  # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT dq")
            n, sev = -1, "ERROR"
            print(f"  !! DQ rule {name} failed to run: {e}")
        status = "PASS" if n == 0 else sev
        cur.execute("INSERT INTO public.dq_results (load_id, table_name, rule, severity, failed_rows, total_rows, sample, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,0,'[]',now())", (load_id, name.split("_")[0], name, status, n))
        out.append((name, sev, n, status))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--views", action="store_true", help="only rebuild views")
    ap.add_argument("--only", nargs="*", help="load only these tables (then rebuild views)")
    args = ap.parse_args()

    print("Creating application tables (conversations, actions, config)...")
    init_db()

    data_dir = Path(settings.data_dir)
    with psycopg.connect(plain_url(settings.db_url), autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
            if not args.views:
                for name, comment in TABLES.items():
                    if args.only and name not in args.only:
                        continue
                    path = data_dir / f"{name}.csv"
                    if not path.exists():
                        print(f"  !! missing {path}, skipped")
                        continue
                    owner = _owned_by_connector(cur, name)
                    if owner:
                        print(f"  -- {name} is owned by connection {owner} (Data Integration): CSV reload skipped")
                        continue
                    t = time.time()
                    n = load_table(cur, name, path, comment)
                    from app.integration.canonical import ensure_lineage
                    ensure_lineage(cur, name, SCHEMA, f"CSV:{path.name}")
                    print(f"  loaded {SCHEMA}.{name:<32} {n:>7} rows  ({time.time() - t:.1f}s)")
            for name, comment in OPTIONAL.items():
                path = data_dir / f"{name}.csv"
                if not args.views and path.exists() and (not args.only or name in args.only):
                    n = load_table(cur, name, path, comment)
                    print(f"  loaded optional {SCHEMA}.{name:<23} {n:>7} rows")
            print("Building KPI views...")
            build_views(cur)
            cur.execute("ANALYZE")
            print("Data-quality checks...")
            import uuid
            res = run_dq(cur, str(uuid.uuid4()))
            for name, sev, n, st in res:
                print(f"  {st:<5} {name:<32} {n}")
            if any(st == "ERROR" for *_, st in res):
                print("  !! DQ ERROR rules failed - KPIs are computed but flagged; see GET /api/dq")
        conn.commit()
    print("Done.")


if __name__ == "__main__":
    main()
