"""SAP S/4HANA OData connector skeleton (C1/C2) - NOT YET TESTED against a live system.

Pulls purchase-order schedule lines and goods receipts into the CSV layout that scripts/load_data.py loads, so the
KPI views work unchanged. Configure in backend/.env:
    SAP_ODATA_BASE=https://<host>/sap/opu/odata/sap      SAP_USER=...   SAP_PASSWORD=...   SAP_CLIENT=100

Services used (standard S/4HANA APIs):
    API_PURCHASEORDER_PROCESS_SRV   A_PurchaseOrderItem, A_PurchaseOrderScheduleLine
    API_MATERIAL_DOCUMENT_SRV       A_MaterialDocumentItem (GoodsMovementType 101 = GR)
    API_SCHED_AGRMT_PROCESS_SRV     A_SchAgrmtSchLine (scheduling agreements)

Run:  python -m app.connectors.sap_odata --since 2026-08-01 --out data/data
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

import httpx


class SapODataClient:
    def __init__(self) -> None:
        self.base = os.environ["SAP_ODATA_BASE"].rstrip("/")
        self.auth = (os.environ["SAP_USER"], os.environ["SAP_PASSWORD"])
        self.params = {"sap-client": os.environ.get("SAP_CLIENT", "100"), "$format": "json"}

    def get_all(self, service: str, entity: str, flt: str | None = None, select: str | None = None):
        url = f"{self.base}/{service}/{entity}"
        params = {**self.params, "$top": 5000}
        if flt:
            params["$filter"] = flt
        if select:
            params["$select"] = select
        with httpx.Client(auth=self.auth, timeout=120) as c:
            while url:
                r = c.get(url, params=params)
                r.raise_for_status()
                d = r.json()["d"]
                yield from d.get("results", [])
                url, params = d.get("__next"), None


def _sap_date(v: str | None) -> str:
    """'/Date(1754006400000)/' -> 'YYYY-MM-DD'."""
    import datetime as dt
    if not v:
        return ""
    ms = int(v.strip("/").replace("Date(", "").replace(")", "").split("+")[0])
    return dt.datetime.utcfromtimestamp(ms / 1000).date().isoformat()


def extract(since: str, out: Path) -> None:
    sap = SapODataClient()
    flt = f"CreationDate ge datetime'{since}T00:00:00'"
    items = {(i["PurchaseOrder"], int(i["PurchaseOrderItem"])): i
             for i in sap.get_all("API_PURCHASEORDER_PROCESS_SRV", "A_PurchaseOrderItem")}
    sched = list(sap.get_all("API_PURCHASEORDER_PROCESS_SRV", "A_PurchaseOrderScheduleLine", flt))
    grs = list(sap.get_all("API_MATERIAL_DOCUMENT_SRV", "A_MaterialDocumentItem", "GoodsMovementType eq '101'"))
    with (out / "fact_grn.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["grn_id", "po_id", "po_line", "receipt_date", "received_qty", "accepted_qty", "rejected_qty", "plant_id"])
        for g in grs:
            w.writerow([f"{g['MaterialDocument']}-{g['MaterialDocumentItem']}", g["PurchaseOrder"], int(g["PurchaseOrderItem"]),
                        _sap_date(g["PostingDate"]), g["QuantityInEntryUnit"], g["QuantityInEntryUnit"], 0, g["Plant"]])
    print(f"extracted {len(items)} PO items, {len(sched)} schedule lines, {len(grs)} goods receipts -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--out", default="data/data")
    a = ap.parse_args()
    extract(a.since, Path(a.out))
