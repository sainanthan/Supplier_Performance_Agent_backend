"""Role-based dashboards (enhancement 1.2).

Every tile is built from the approved metric catalogue (app/metrics.py) or existing analytics, and carries a `source`
that the UI resolves to the exact records (/api/metrics/{id}/records or /api/evidence). No new KPI logic lives here.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import analytics as A
from . import metrics as MX

ROLES_FOR = {"procurement": ("ADMIN", "PROCUREMENT", "VIEWER"), "quality": ("ADMIN", "QUALITY", "VIEWER"),
             "management": ("ADMIN", "PROCUREMENT", "QUALITY", "VIEWER")}
DEFAULT_FOR = {"PROCUREMENT": "procurement", "QUALITY": "quality", "ADMIN": "management", "VIEWER": "management"}


def _tile(metric: str, f: dict) -> dict:
    r = MX.query(metric, [], f, log=False)
    return {"metric": metric, "label": r["label"], "unit": r["unit"], "better": r["better"], "definition": r["definition"],
            "value": r["rows"][0]["value"] if r["rows"] else None, "n": r["rows"][0].get("n") if r["rows"] else None,
            "source": r["source"]}


def _chart(metric: str, group_by: list[str], f: dict) -> dict:
    r = MX.query(metric, group_by, f, log=False)
    return {"metric": metric, "label": r["label"], "definition": r["definition"], "chart": r["chart"], "source": r["source"]}


SPEC = {
    "procurement": (("otif_pct", "on_time_pct", "in_full_pct", "lead_time_variance_days", "past_due_lines", "price_variance_pct"),
                    (("otif_pct", ["month"]), ("past_due_lines", ["supplier"]), ("lead_time_variance_days", ["supplier"])), "otif_pct"),
    "quality": (("ppm", "defects", "open_ncr", "overdue_capa", "ncr_age_days", "response_days"),
                (("ppm", ["month"]), ("defects", ["defect"]), ("overdue_capa", ["supplier"])), "ppm"),
}


def _parallel(jobs: dict) -> dict:
    """Each view query costs ~1 s on Neon; run a dashboard's queries concurrently."""
    with ThreadPoolExecutor(max_workers=min(10, len(jobs))) as ex:
        futs = {k: ex.submit(f) for k, f in jobs.items()}
        return {k: f.result() for k, f in futs.items()}


def build(kind: str, plant_id: str | None = None, start: str | None = None, end: str | None = None) -> dict:
    f = {"plant_id": plant_id, "start": start, "end": end}
    if kind in SPEC:
        tiles, charts, cmp_metric = SPEC[kind]
        jobs = {f"t{i}": (lambda m=m: _tile(m, f)) for i, m in enumerate(tiles)}
        jobs.update({f"c{i}": (lambda m=m, g=g: _chart(m, g, f)) for i, (m, g) in enumerate(charts)})
        jobs["changes"] = lambda: MX.compare_periods(cmp_metric, "month", "supplier", {"plant_id": plant_id})
        jobs["alerts"] = A.risk_alerts
        r = _parallel(jobs)
        out = {"tiles": [r[f"t{i}"] for i in range(len(tiles))], "charts": [r[f"c{i}"] for i in range(len(charts))], "changes": r["changes"]}
        if kind == "procurement":
            out["pending_promise_changes"] = A._run("SELECT COUNT(*) FROM public.promise_changes WHERE status='PROPOSED'", {}, True)
        else:
            out["repeat_defects"] = [a for a in r["alerts"] if a["type"] in ("REPEATED_DEFECT", "QUALITY_DETERIORATION")][:8]
    elif kind == "management":
        r = _parallel({"cards": lambda: A.scorecard(start, end, plant_id), "alerts": A.risk_alerts,
                       "t0": lambda: _tile("otif_pct", f), "t1": lambda: _tile("ppm", f), "t2": lambda: _tile("disruption_hours", f),
                       "t3": lambda: _tile("overdue_capa", f), "c0": lambda: _chart("disruption_hours", ["month"], f),
                       "c1": lambda: _chart("otif_pct", ["month", "plant"], f)})
        cards = r["cards"]
        alerts = [a for a in r["alerts"] if a["severity"] in ("CRITICAL", "HIGH")]
        risky = [c for c in cards if c.get("health") == "AT_RISK" or (c.get("past_due_lines") or 0) > 0]
        exp = _parallel({c["supplier_id"]: (lambda sid=c["supplier_id"]: A.exposure(sid)) for c in risky}) if risky else {}
        exposure = []
        for c in risky:
            if True:
                e = exp[c["supplier_id"]]
                exposure.append({"supplier_id": c["supplier_id"], "supplier_name": c["supplier_name"], "health": c["health"],
                                 "past_due_lines": len(e["past_due_lines"]), "at_risk_production_orders": e["at_risk_production_orders"],
                                 "at_risk_customer_orders": e["at_risk_customer_orders"], "at_risk_order_qty": e["at_risk_order_qty"],
                                 "source": {"metric": "past_due_lines", "filters": {"supplier_id": c["supplier_id"]}}})
        exposure.sort(key=lambda x: -x["at_risk_order_qty"])
        out = {
            "tiles": [r["t0"], r["t1"], r["t2"], r["t3"]],
            "health_mix": {h: sum(1 for c in cards if c.get("health") == h) for h in ("HEALTHY", "WATCH", "AT_RISK")},
            "top_risks": alerts[:10], "high_alerts": len(alerts), "exposure": exposure[:10],
            "charts": [r["c0"], r["c1"]],
        }
    else:
        raise KeyError(kind)
    out.update(kind=kind, as_of_date=str(A.as_of_date()), filters={k: v for k, v in f.items() if v})
    out["run_id"] = A.log_run(f"dashboard_{kind}", out["filters"], out.get("tiles"))
    return out


# Same stale-while-revalidate cache as the other analytics (cleared by POST /api/cache/clear and config changes).
build = A._cached(build)
