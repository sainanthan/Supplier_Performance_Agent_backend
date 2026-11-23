"""Load the SYNTHETIC demo extracts (data/sample_synthetic/) into their optional tables, for demos only.

The real dataset has no goods receipts, incoming inspection, schedule agreements or invoices. This fills those tables from
the clearly labelled SYNTHETIC_*.csv files (generated from the real PO lines by scripts/gen_sample_data.py), into the
EXISTING tables (columns and lineage kept). Every row keeps _source_system =
'SYNTHETIC:<file>', so it can always be told apart and removed again.

    python -m scripts.load_synthetic            # load (replaces earlier SYNTHETIC rows only)
    python -m scripts.load_synthetic --clear    # remove every SYNTHETIC row again
Inbound milestones are loaded by scripts/gen_milestones.py --load (same labelling).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402

DIR = Path(__file__).resolve().parents[1] / "data" / "sample_synthetic"
TABLES = ["fact_grn", "fact_incoming_inspection", "fact_schedule_line", "fact_invoice"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear", action="store_true")
    a = ap.parse_args()
    with engine.begin() as c:
        for t in TABLES:
            cols = set(c.execute(text("SELECT column_name FROM information_schema.columns WHERE table_schema='sp' AND table_name=:t"), {"t": t}).scalars())
            tag = "_source_system" if "_source_system" in cols else "source_system"
            n = c.execute(text(f"DELETE FROM sp.{t} WHERE {tag} LIKE 'SYNTHETIC%'")).rowcount
            if a.clear:
                print(f"  removed {n:>5} SYNTHETIC rows from sp.{t}")
                continue
            path = DIR / f"SYNTHETIC_{t}.csv"
            with path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            keep = [k for k in rows[0] if k in cols]
            drop = [k for k in rows[0] if k not in cols]
            extra = ", _source_system" if "_source_system" in cols else ""
            sql = text(f"INSERT INTO sp.{t} ({', '.join(keep)}{extra}) VALUES ({', '.join(':' + k for k in keep)}{', :_src' if extra else ''})")
            c.execute(sql, [{**{k: (r[k] if r[k] != "" else None) for k in keep}, "_src": f"SYNTHETIC:{path.name}"} for r in rows])
            print(f"  loaded {len(rows):>5} SYNTHETIC rows into sp.{t}" + (f"  (columns not in table, skipped: {', '.join(drop)})" if drop else ""))
    print("Done. Clear the app cache (Refresh button) to see the new figures.")


if __name__ == "__main__":
    main()
