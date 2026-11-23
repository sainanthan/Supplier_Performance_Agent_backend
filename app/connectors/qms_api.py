"""QMS REST connector skeleton (C1/C3) - NOT YET TESTED against a live system.

Pulls incoming-inspection lots and NCRs into the CSV layout used by scripts/load_data.py
(fact_incoming_inspection.csv, fact_ncr_capa.csv). Configure in backend/.env:
    QMS_BASE_URL=https://qms.example.com/api      QMS_TOKEN=...

Expected endpoints (adapt the field mapping in MAP_* to your QMS):
    GET /inspection-lots?type=INCOMING&from=YYYY-MM-DD    GET /ncrs?from=YYYY-MM-DD

Run:  python -m app.connectors.qms_api --since 2026-08-01 --out data/data
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

import httpx

MAP_LOT = {"inspection_lot_id": "lotId", "grn_id": "grnNumber", "po_id": "poNumber", "po_line": "poItem",
           "supplier_id": "vendorCode", "material_id": "materialCode", "material_lot_id": "batch", "plant_id": "plant",
           "inspection_date": "inspectedOn", "inspected_qty": "sampleQty", "accepted_qty": "acceptedQty",
           "rejected_qty": "rejectedQty", "defect_code": "defectCode", "usage_decision": "usageDecision"}


def _pages(path: str, since: str):
    base, tok = os.environ["QMS_BASE_URL"].rstrip("/"), os.environ["QMS_TOKEN"]
    page = 1
    with httpx.Client(timeout=60, headers={"Authorization": f"Bearer {tok}"}) as c:
        while True:
            r = c.get(f"{base}{path}", params={"from": since, "page": page, "pageSize": 1000})
            r.raise_for_status()
            rows = r.json().get("items", [])
            if not rows:
                return
            yield from rows
            page += 1


def extract(since: str, out: Path) -> None:
    n = 0
    with (out / "fact_incoming_inspection.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(MAP_LOT)
        for lot in _pages("/inspection-lots?type=INCOMING", since):
            w.writerow([lot.get(src, "") for src in MAP_LOT.values()])
            n += 1
    print(f"extracted {n} incoming inspection lots -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--out", default="data/data")
    a = ap.parse_args()
    extract(a.since, Path(a.out))
