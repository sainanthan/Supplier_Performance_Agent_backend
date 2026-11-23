"""Independent reconciliation (G5): recompute key KPIs straight from the source CSVs in plain Python - no SQL views -
and compare with what the app serves. Results are stored in public.recon_results and shown in Settings.

    python -m scripts.reconcile           (from backend/)
"""
from __future__ import annotations

import csv
import datetime as dt
import sys
import uuid
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import analytics as A  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import ReconResult, SessionLocal, get_scorecard_config  # noqa: E402

D = Path(settings.data_dir)


def _csv(name: str) -> list[dict]:
    with (D / f"{name}.csv").open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _d(s: str) -> dt.date | None:
    return dt.date.fromisoformat(s[:10]) if s else None


def source_kpis() -> dict:
    cfg = get_scorecard_config()
    tol_u, tol_o = cfg["in_full_tolerance_pct"] / 100, cfg["over_delivery_tolerance_pct"] / 100
    early = cfg.get("early_window_days")
    insp = _csv("fact_quality_inspection")
    ncr = _csv("fact_ncr_capa")
    as_of = max(max(_d(r["inspection_ts"]) for r in insp), max(_d(r["opened_ts"]) for r in ncr))
    excl = {(r["po_id"], r["po_line"]) for r in A._run(
        "SELECT po_id, po_line FROM public.kpi_exclusions WHERE entity_type='PO_LINE' AND revoked_at IS NULL", {})}
    promise = {(r["po_id"], r["po_line"]): (_d(str(r["new_promise"])), _d(str(r["approved_at"])))
               for r in A._run("""SELECT DISTINCT ON (po_id, po_line) po_id, po_line, new_promise, approved_at FROM public.promise_changes
                                  WHERE status='APPROVED' ORDER BY po_id, po_line, approved_at DESC""", {})}
    per = defaultdict(lambda: {"n": 0, "otif": 0, "spend": 0.0, "lines": 0})
    for r in _csv("fact_purchase_order_receipt"):
        key = (r["po_id"], int(r["po_line"]))
        if key in excl:
            continue
        p = per[r["supplier_id"]]
        rcpt, prom = _d(r["receipt_date"]), _d(r["promised_date"])
        if key in promise and promise[key][1] and promise[key][1] <= prom:
            prom = promise[key][0]
        oq, rq = float(r["ordered_qty"]), float(r["received_qty"] or 0)
        p["lines"] += 1
        p["spend"] += rq * float(r["unit_price_inr"])
        if (rcpt and rcpt <= as_of) or prom < as_of:
            p["n"] += 1
            in_full = oq * (1 - tol_u) <= rq <= oq * (1 + tol_o)
            not_early = not (early is not None and rcpt and rcpt < prom - dt.timedelta(days=int(early)))
            if rcpt and rcpt <= prom and in_full and not_early:
                p["otif"] += 1
    return {"as_of": as_of, "per": per, "fails": sum(1 for r in insp if r["result"] == "FAIL"), "ncr": len(ncr)}


def reconcile() -> dict:
    A.clear_cache()
    src = source_kpis()
    app = {c["supplier_id"]: c for c in A.scorecard()}
    rid, rows = str(uuid.uuid4()), []

    def add(name, sid, s, a, tol):
        diff = None if s is None or a is None else round(float(a) - float(s), 4)
        rows.append(ReconResult(recon_id=rid, check_name=name, supplier_id=sid, source_value=s, app_value=a, diff=diff,
                                passed=diff is not None and abs(diff) <= tol))

    add("as_of_date (days since 2000-01-01)", None, (src["as_of"] - dt.date(2000, 1, 1)).days,
        (dt.date.fromisoformat(str(A.as_of_date())) - dt.date(2000, 1, 1)).days, 0)
    for sid, p in sorted(src["per"].items()):
        c = app.get(sid, {})
        add("OTIF % (measurable lines)", sid, round(100 * p["otif"] / p["n"], 1) if p["n"] else None, c.get("otif_pct"), 0.11)
        add("Measurable PO lines", sid, p["n"], c.get("lines_measured"), 0)
        add("Spend INR", sid, round(p["spend"]), c.get("spend_inr"), max(1.0, 0.0001 * p["spend"]))
    attributed = float(A._run("SELECT COUNT(DISTINCT inspection_id) FROM sp.v_defect_attribution", {}, True) or 0)
    unattributed = float(A._run("""SELECT COUNT(*) FROM sp.fact_quality_inspection q WHERE result='FAIL' AND NOT EXISTS
                                   (SELECT 1 FROM sp.v_defect_attribution a WHERE a.inspection_id=q.inspection_id)""", {}, True) or 0)
    excluded = float(A._run("SELECT COUNT(*) FROM public.kpi_exclusions WHERE entity_type='INSPECTION' AND revoked_at IS NULL", {}, True) or 0)
    add("Failed inspections = attributed + unattributed + excluded", None, src["fails"], attributed + unattributed + excluded, 0)
    add("Attributed defects (sum of weights) = attributed inspections", None, attributed,
        round(float(A._run("SELECT SUM(attribution_weight) FROM sp.v_defect_attribution", {}, True) or 0), 4), 0.001)
    with SessionLocal() as s:
        s.add_all(rows)
        s.commit()
    failed = [r for r in rows if not r.passed]
    return {"recon_id": rid, "checks": len(rows), "failed": len(failed),
            "failures": [{"check": r.check_name, "supplier_id": r.supplier_id, "source": r.source_value, "app": r.app_value}
                         for r in failed][:20]}


if __name__ == "__main__":
    out = reconcile()
    print(f"recon {out['recon_id']}: {out['checks']} checks, {out['failed']} failed")
    for f in out["failures"]:
        print("  FAIL", f)
