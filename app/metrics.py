"""Safe query layer (enhancement 1.1).

The agent (and the dashboards) ask for APPROVED METRICS, never write SQL. Every SQL fragment below is a fixed string
over the approved KPI views; callers only choose a metric id, dimensions and filter values from allow-lists, and
filter values are always bound parameters. Each result carries:
  - the metric definition (from this catalogue + the live config version),
  - a chart spec the UI can render,
  - a `source` reference that /api/metrics/{id}/records resolves to the exact underlying rows (audit trail).
"""
from __future__ import annotations

import datetime as dt
import re

from . import analytics as A

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-/]{0,39}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Sources: approved views only. date = the column periods are cut on.
SOURCES = {
    "delivery": {"view": "sp.v_po_delivery", "date": "promised_date", "dims": {"supplier", "plant", "material", "category"}},
    "receipts": {"view": "sp.v_po_delivery", "date": "receipt_date", "dims": {"supplier", "plant", "material", "category"}},
    "defects": {"view": "sp.v_defect_attribution", "date": "inspection_ts", "dims": {"supplier", "plant", "material", "defect"}},
    "ncr": {"view": "sp.v_ncr_supplier", "date": "opened_ts", "dims": {"supplier", "plant", "material", "defect"}},
    "disruption": {"view": "sp.v_supply_disruption", "date": "start_ts", "dims": {"supplier", "plant"}},
}
DIM_COL = {"supplier": "supplier_id", "plant": "plant_id", "material": "material_id", "category": "material_category",
           "defect": "defect_category"}
FILTER_COL = {"supplier_id": "supplier_id", "plant_id": "plant_id", "material_id": "material_id"}

# id: (label, unit, better, source, aggregate SQL, row condition, definition, record columns)
CATALOG: dict[str, dict] = {
    "otif_pct": dict(label="OTIF", unit="%", better="up", source="delivery", agg="100.0 * AVG(otif::int)", n="COUNT(*)",
                     cond="measurable", definition="Share of measurable PO lines received on time (by effective promise date) and in full.",
                     cols="po_id, po_line, supplier_id, material_id, plant_id, promised_date, receipt_date, ordered_qty, received_qty, on_time, in_full, otif"),
    "on_time_pct": dict(label="On-time", unit="%", better="up", source="delivery", agg="100.0 * AVG(on_time::int)", n="COUNT(*)",
                        cond="measurable", definition="Share of measurable PO lines received by the effective promise date.",
                        cols="po_id, po_line, supplier_id, material_id, plant_id, promised_date, receipt_date, delay_days, on_time"),
    "in_full_pct": dict(label="In-full", unit="%", better="up", source="delivery", agg="100.0 * AVG(in_full::int)", n="COUNT(*)",
                        cond="measurable AND delivery_status = 'RECEIVED'", definition="Share of received PO lines within the in-full / over-delivery tolerances.",
                        cols="po_id, po_line, supplier_id, material_id, ordered_qty, received_qty, in_full, over_delivery"),
    "lead_time_variance_days": dict(label="Lead-time variance", unit=" wd", better="down", source="receipts", agg="AVG(lead_time_variance_days)",
                                    n="COUNT(*)", cond="measurable AND delivery_status = 'RECEIVED'",
                                    definition="Actual lead time (working days, PO date to receipt) minus planned material lead time.",
                                    cols="po_id, po_line, supplier_id, material_id, po_date, receipt_date, actual_lead_time_wd, planned_lead_time_days, lead_time_variance_days"),
    "past_due_lines": dict(label="Past-due PO lines", unit="", better="down", source="delivery", agg="COUNT(*)", n="COUNT(*)",
                           cond="delivery_status = 'PAST_DUE' AND NOT excluded", definition="Open PO lines whose promise date is before the as-of date.",
                           cols="po_id, po_line, supplier_id, material_id, plant_id, promised_date, ordered_qty"),
    "spend_inr": dict(label="Spend", unit=" INR", better=None, source="receipts", agg="SUM(receipt_value_inr)", n="COUNT(*)",
                      cond="NOT excluded AND delivery_status = 'RECEIVED'", definition="Received quantity x PO unit price.",
                      cols="po_id, po_line, supplier_id, material_id, received_qty, unit_price_inr, receipt_value_inr"),
    "price_variance_pct": dict(label="Price variance", unit="%", better="down", source="receipts",
                               agg="100.0 * SUM(price_variance_inr) / NULLIF(SUM(standard_price_inr * received_qty), 0)", n="COUNT(*)",
                               cond="NOT excluded AND delivery_status = 'RECEIVED'", definition="(PO price - standard price) x qty / standard value.",
                               cols="po_id, po_line, supplier_id, material_id, unit_price_inr, standard_price_inr, received_qty, price_variance_inr"),
    "defects": dict(label="Attributed defects", unit="", better="down", source="defects", agg="SUM(attribution_weight)", n="COUNT(DISTINCT inspection_id)",
                    cond="TRUE", definition="Failed inspections attributed via genealogy, split equally across candidate suppliers.",
                    cols="inspection_id, inspection_ts, supplier_id, material_id, plant_id, defect_code, defect_name, severity, attribution_weight, confidence"),
    "ppm": dict(label="PPM (attributed)", unit="", better="down", source="defects", ratio=True,
                definition="Attributed defects / received units x 1,000,000 (genealogy estimate).",
                cols="inspection_id, inspection_ts, supplier_id, material_id, plant_id, defect_code, defect_name, attribution_weight, confidence"),
    "open_ncr": dict(label="Open NCR (weighted)", unit="", better="down", source="ncr", agg="SUM(attribution_weight)", n="COUNT(DISTINCT ncr_id)",
                     cond="is_open", definition="Open / in-progress NCRs implicating the supplier, weighted by number of implicated suppliers.",
                     cols="ncr_id, opened_ts, supplier_id, material_id, defect_name, capa_status, capa_due_date, open_age_days, attribution_weight"),
    "overdue_capa": dict(label="Overdue CAPA (weighted)", unit="", better="down", source="ncr", agg="SUM(attribution_weight)", n="COUNT(DISTINCT ncr_id)",
                         cond="is_overdue", definition="Open NCRs past their CAPA due date at the as-of date.",
                         cols="ncr_id, opened_ts, supplier_id, material_id, capa_due_date, days_overdue, attribution_weight"),
    "ncr_age_days": dict(label="Open NCR age", unit=" d", better="down", source="ncr", agg="AVG(open_age_days)", n="COUNT(DISTINCT ncr_id)",
                         cond="is_open", definition="Average age of open NCRs at the as-of date.",
                         cols="ncr_id, opened_ts, supplier_id, capa_status, open_age_days"),
    "response_days": dict(label="NCR response time", unit=" d", better="down", source="ncr", agg="AVG(response_days)", n="COUNT(response_days)",
                          cond="response_days IS NOT NULL", definition="NCR opened to the supplier's first portal response.",
                          cols="ncr_id, opened_ts, supplier_id, first_response_at, response_days"),
    "disruption_hours": dict(label="Disruption hours", unit=" h", better="down", source="disruption",
                             agg="SUM(duration_min * attribution_weight) / 60.0", n="COUNT(DISTINCT downtime_id)", cond="supplier_id IS NOT NULL",
                             definition="NO_MATERIAL stoppage hours on lines consuming a category the supplier delivered late.",
                             cols="downtime_id, supplier_id, plant_id, line_id, line_type, start_ts, duration_min, attribution_weight"),
}
GRAINS = {"week", "month"}


class MetricError(ValueError):
    pass


def catalog() -> list[dict]:
    return [{"id": k, "label": m["label"], "unit": m["unit"], "better": m["better"], "definition": m["definition"],
             "dimensions": sorted(SOURCES[m["source"]]["dims"])} for k, m in CATALOG.items()]


def _validate(metric: str, group_by: list[str], filters: dict) -> tuple:
    if metric not in CATALOG:
        raise MetricError(f"Unknown metric '{metric}'. Approved metrics: {', '.join(CATALOG)}")
    m = CATALOG[metric]
    src = SOURCES[m["source"]]
    dims, grain = [], None
    for g in group_by or []:
        g = g.strip().lower()
        if g in GRAINS:
            grain = g
        elif g in src["dims"]:
            dims.append(g)
        elif g:
            raise MetricError(f"'{g}' is not an allowed dimension for {metric}. Allowed: {', '.join(sorted(src['dims'] | GRAINS))}")
    if len(dims) > 1:
        raise MetricError("Group by at most one dimension plus a time grain")
    if m.get("ratio") and set(dims) - {"supplier", "plant", "material"}:
        raise MetricError("PPM can be grouped by supplier, plant or material only")
    clean = {}
    for k, v in (filters or {}).items():
        if v in (None, ""):
            continue
        if k in FILTER_COL:
            if not ID_RE.match(str(v)):
                raise MetricError(f"Invalid {k}")
            if k == "material_id" and "material" not in src["dims"]:
                raise MetricError(f"{metric} cannot be filtered by material")
            clean[k] = str(v)
        elif k in ("start", "end"):
            if not DATE_RE.match(str(v)):
                raise MetricError(f"{k} must be YYYY-MM-DD")
            clean[k] = str(v)
        else:
            raise MetricError(f"Unknown filter '{k}'. Allowed: supplier_id, plant_id, material_id, start, end")
    return m, src, dims, grain, clean


def _where(m: dict, src: dict, f: dict, extra: str | None = None) -> tuple[str, dict]:
    parts, p = [m.get("cond", "TRUE")], {}
    for k, col in FILTER_COL.items():
        if k in f:
            parts.append(f"{col} = :{k}"); p[k] = f[k]
    if "start" in f:
        parts.append(f"{src['date']} >= CAST(:start AS date)"); p["start"] = f["start"]
    if "end" in f:
        parts.append(f"{src['date']} < CAST(:end AS date) + 1"); p["end"] = f["end"]
    if extra:
        parts.append(extra)
    return " AND ".join(f"({x})" for x in parts), p


def _sql(metric: str, m: dict, src: dict, dims: list[str], grain: str | None, f: dict) -> tuple[str, dict]:
    keys = [f"date_trunc('{grain}', {src['date']})::date AS period"] if grain else []
    keys += [f"{DIM_COL[d]} AS {d}" for d in dims]
    gb = ", ".join(str(i + 1) for i in range(len(keys)))
    if m.get("ratio"):        # PPM = defects / received units, both cut by the same keys
        dkeys = keys
        rkeys = ([f"date_trunc('{grain}', receipt_date)::date AS period"] if grain else []) + [f"{DIM_COL[d]} AS {d}" for d in dims]
        wd, pd_ = _where({"cond": "TRUE"}, SOURCES["defects"], f)
        wr, pr = _where({"cond": "delivery_status = 'RECEIVED' AND NOT excluded"}, SOURCES["receipts"], f)
        join = " AND ".join(f"d.{k} = r.{k}" for k in (["period"] if grain else []) + dims)
        sel = ", ".join(f"COALESCE(d.{k}, r.{k}) AS {k}" for k in (["period"] if grain else []) + dims)
        sql = f"""WITH d AS (SELECT {', '.join(dkeys + ['SUM(attribution_weight) AS defects']) } FROM sp.v_defect_attribution WHERE {wd}{' GROUP BY ' + gb if gb else ''}),
                       r AS (SELECT {', '.join(rkeys + ['SUM(received_qty) AS units']) } FROM sp.v_po_delivery WHERE {wr}{' GROUP BY ' + gb if gb else ''})
                  SELECT {sel + ', ' if sel else ''}1e6 * COALESCE(d.defects, 0) / NULLIF(r.units, 0) AS value, r.units AS n
                  FROM d {("FULL JOIN r ON " + join) if join else "CROSS JOIN r"}"""
        return sql, {**pd_, **pr}
    w, p = _where(m, src, f)
    sql = f"SELECT {', '.join(keys + [m['agg'] + ' AS value', m['n'] + ' AS n'])} FROM {src['view']} WHERE {w}" + (f" GROUP BY {gb}" if gb else "")
    return sql, p


def _names() -> dict:
    return {r["supplier_id"]: r["supplier_name"] for r in A._run("SELECT supplier_id, supplier_name FROM sp.dim_supplier", {})}


def query(metric: str, group_by: list[str] | None = None, filters: dict | None = None, log: bool = True) -> dict:
    m, src, dims, grain, f = _validate(metric, group_by or [], filters or {})
    sql, params = _sql(metric, m, src, dims, grain, f)
    rows = A._run(sql, params)
    names = _names() if "supplier" in dims else {}
    for r in rows:
        r["value"] = None if r["value"] is None else round(float(r["value"]), 2)
        if "supplier" in r:
            r["supplier_name"] = names.get(r["supplier"])
    rows.sort(key=lambda r: (str(r.get("period") or ""), str(r.get(dims[0]) if dims else "")))
    out = {"metric": metric, "label": m["label"], "unit": m["unit"], "better": m["better"], "definition": m["definition"],
           "group_by": ([grain] if grain else []) + dims, "filters": f, "rows": rows, "as_of_date": str(A.as_of_date()),
           "chart": _chart(m, rows, dims, grain), "source": {"metric": metric, "filters": f}}
    if log:
        out["run_id"] = A.log_run("metric_query", {"metric": metric, "group_by": out["group_by"], **f}, rows)
    return out


def compare_periods(metric: str, grain: str = "month", dim: str = "supplier", filters: dict | None = None) -> dict:
    """Current period (containing the as-of date) vs the previous one, per dimension value.
    Reports change and direction (worse/better) using the metric's 'better' direction; it does NOT rank for sourcing."""
    grain = grain if grain in GRAINS else "month"
    res = query(metric, [grain, dim], filters, log=False)
    periods = sorted({r["period"] for r in res["rows"] if r.get("period")})
    as_of = dt.date.fromisoformat(res["as_of_date"])
    cur = max((p for p in periods if p <= as_of.isoformat()), default=None)
    prev = max((p for p in periods if cur and p < cur), default=None)
    by = {}
    for r in res["rows"]:
        by.setdefault(r[dim], {})[r["period"]] = r
    better = res["better"]
    rows = []
    for k, d in by.items():
        c, p = d.get(cur), d.get(prev)
        if not c or not p or c["value"] is None or p["value"] is None:
            continue
        ch = round(c["value"] - p["value"], 2)
        worse = (ch < 0) if better == "up" else (ch > 0) if better == "down" else None
        rows.append({dim: k, "supplier_name": c.get("supplier_name"), "previous": p["value"], "current": c["value"], "change": ch,
                     "n_previous": p.get("n"), "n_current": c.get("n"), "direction": None if worse is None else "worse" if worse else ("better" if ch else "flat")})
    rows.sort(key=lambda r: (r["direction"] != "worse", -abs(r["change"])))
    label = lambda r: r.get("supplier_name") or r[dim]
    out = {"metric": metric, "label": res["label"], "unit": res["unit"], "definition": res["definition"], "grain": grain,
           "current_period": cur, "previous_period": prev, "rows": rows, "as_of_date": res["as_of_date"],
           "note": "Current period may be partial (it contains the as-of date). Change = current - previous.",
           "chart": {"type": "bar", "title": f"{res['label']} change: {prev} → {cur}", "x": "name", "series": [{"key": "change", "label": "Change"}],
                     "data": [{"name": label(r), "change": r["change"], "id": r[dim]} for r in rows[:15]], "unit": res["unit"]},
           "source": {"metric": metric, "filters": {k: v for k, v in {**(filters or {}), "start": prev if prev else None, "end": as_of.isoformat() if prev else None}.items() if v}}}
    out["run_id"] = A.log_run("metric_compare", {"metric": metric, "grain": grain, "dim": dim, **(filters or {})}, rows)
    return out


def _chart(m: dict, rows: list, dims: list[str], grain: str | None) -> dict:
    if grain and dims:          # one line per dimension value (top 6 by volume)
        d = dims[0]
        tot = {}
        for r in rows:
            tot[r[d]] = tot.get(r[d], 0) + float(r.get("n") or 0)
        keep = [k for k, _ in sorted(tot.items(), key=lambda x: -x[1])[:6]]
        data = {}
        for r in rows:
            if r[d] in keep:
                data.setdefault(r["period"], {"x": r["period"]})[r[d]] = r["value"]
        name = lambda k: next((r.get("supplier_name") for r in rows if r[d] == k and r.get("supplier_name")), k)
        return {"type": "line", "title": m["label"], "x": "x", "unit": m["unit"], "data": [data[k] for k in sorted(data)],
                "series": [{"key": k, "label": name(k)} for k in keep]}
    if grain:
        return {"type": "line", "title": m["label"], "x": "x", "unit": m["unit"], "series": [{"key": "value", "label": m["label"]}],
                "data": [{"x": r["period"], "value": r["value"]} for r in rows]}
    if dims:
        d = dims[0]
        return {"type": "bar", "title": m["label"], "x": "x", "unit": m["unit"], "series": [{"key": "value", "label": m["label"]}],
                "data": [{"x": r.get("supplier_name") or r[d], "value": r["value"], "id": r[d]} for r in rows[:20]]}
    return {"type": "kpi", "title": m["label"], "unit": m["unit"], "value": rows[0]["value"] if rows else None}


def records(metric: str, filters: dict | None = None, limit: int = 500) -> dict:
    """The exact rows behind a metric result (same view, same row condition, same filters)."""
    m, src, _dims, _grain, f = _validate(metric, [], filters or {})
    view = SOURCES[m["source"]]["view"]
    cond_m = m if not m.get("ratio") else {"cond": "TRUE"}
    w, p = _where(cond_m, src, f)
    rows = A._run(f"SELECT {m['cols']} FROM {view} WHERE {w} ORDER BY {src['date']} DESC LIMIT {int(min(limit, 5000))}", p)
    total = A._run(f"SELECT COUNT(*) FROM {view} WHERE {w}", p, True)
    return {"metric": metric, "label": m["label"], "definition": m["definition"], "view": view, "condition": m.get("cond", "TRUE"),
            "filters": f, "row_count": total, "rows": rows,
            "note": "PPM denominator = received units from sp.v_po_delivery with the same filters." if m.get("ratio") else None}
