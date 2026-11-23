"""Synthetic sample-data generator (clearly labelled, NEVER loaded automatically).

Writes CSVs for the extracts the real dataset does not have (goods receipts with split deliveries, incoming inspection,
invoices, schedule-agreement lines) into data/sample_synthetic/ - a folder the loader does not read. Every file name is
prefixed SYNTHETIC_ and every row carries source_system=SYNTHETIC, so they can never be confused with real data.

    python -m scripts.gen_sample_data --seed 7          # generate
    # to try them in a SCRATCH database only: copy without the prefix into data/data and run load_data

Rows are derived from the real PO lines so keys join (po_id/po_line/supplier/material/plant).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # backend/ (data lives in backend/data)
SRC = ROOT / "data" / "data" / "fact_purchase_order_receipt.csv"
OUT = ROOT / "data" / "sample_synthetic"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--split-share", type=float, default=0.25, help="share of PO lines received in 2-3 GRNs")
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    OUT.mkdir(parents=True, exist_ok=True)
    pos = [r for r in csv.DictReader(SRC.open(encoding="utf-8")) if r["receipt_date"]]
    grn, insp, inv, sched = [], [], [], []
    QUALITY = {}                                        # per-supplier incoming-quality factor (synthetic)
    for i, p in enumerate(pos):
        qty, rd = float(p["received_qty"]), dt.date.fromisoformat(p["receipt_date"])
        parts = rnd.choice([2, 3]) if rnd.random() < a.split_share else 1
        cuts = sorted(rnd.uniform(0.2, 0.8) for _ in range(parts - 1))
        shares = [b - a_ for a_, b in zip([0] + cuts, cuts + [1])]
        for j, sh in enumerate(shares):
            d = rd - dt.timedelta(days=(parts - 1 - j) * rnd.randint(1, 4))
            q = round(qty * sh)
            rej = round(q * rnd.choice([0, 0, 0, 0.0005, 0.002]))
            gid = f"SYN-GRN-{i:05d}-{j}"
            grn.append([gid, p["po_id"], p["po_line"], d, q, q - rej, rej, p["plant_id"], "SYNTHETIC"])
            # incoming inspection samples 5 % of the lot; a defective part is found in about 2 % of lots (x a per-supplier
            # quality factor), which gives realistic incoming PPM in the tens to low hundreds, in line with the real data
            s = max(1, round(q * 0.05))
            found = 1 if rnd.random() < 0.02 * QUALITY.setdefault(p["supplier_id"], rnd.uniform(0.4, 2.6)) else 0
            insp.append([f"SYN-IL-{i:05d}-{j}", gid, p["po_id"], p["po_line"], p["supplier_id"], p["material_id"], f"SYN-LOT-{i:05d}{j}",
                         p["plant_id"], d, s, s - found, found, "D001" if found else "", "ACCEPT_WITH_DEVIATION" if found else "ACCEPT", "SYNTHETIC"])
        price = float(p["unit_price_inr"]) * rnd.choice([1, 1, 1, 1.01, 0.995])
        inv.append([f"SYN-INV-{i:05d}", p["po_id"], p["po_line"], p["supplier_id"], rd + dt.timedelta(days=rnd.randint(1, 10)),
                    qty if rnd.random() > 0.05 else round(qty * 1.02), round(price, 4), "INR", "SYNTHETIC"])
        if rnd.random() < 0.3:
            sched.append([f"SYN-SA-{p['supplier_id']}", i, p["supplier_id"], p["material_id"], p["plant_id"], p["promised_date"],
                          p["ordered_qty"], p["received_qty"], p["receipt_date"], "SYNTHETIC"])
    files = {
        "SYNTHETIC_fact_grn.csv": (["grn_id", "po_id", "po_line", "receipt_date", "received_qty", "accepted_qty", "rejected_qty", "plant_id", "source_system"], grn),
        "SYNTHETIC_fact_incoming_inspection.csv": (["inspection_lot_id", "grn_id", "po_id", "po_line", "supplier_id", "material_id", "material_lot_id", "plant_id",
                                                    "inspection_date", "inspected_qty", "accepted_qty", "rejected_qty", "defect_code", "usage_decision", "source_system"], insp),
        "SYNTHETIC_fact_invoice.csv": (["invoice_id", "po_id", "po_line", "supplier_id", "invoice_date", "invoiced_qty", "invoice_unit_price_inr", "currency", "source_system"], inv),
        "SYNTHETIC_fact_schedule_line.csv": (["agreement_id", "schedule_line", "supplier_id", "material_id", "plant_id", "delivery_date", "scheduled_qty",
                                              "received_qty", "receipt_date", "source_system"], sched),
    }
    for name, (hdr, rows) in files.items():
        with (OUT / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(hdr)
            w.writerows(rows)
        print(f"  {name:<44} {len(rows):>6} rows")
    print(f"Synthetic sample data written to {OUT} (not loaded).")


if __name__ == "__main__":
    main()
