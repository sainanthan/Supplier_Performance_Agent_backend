"""Supplier performance analytics: scorecards, trends, risk detection, exposure, sourcing.

All metrics come from the KPI views in schema `sp` (see app/sql/views.sql) and the
agreed weights/thresholds in public.app_config (key 'scorecard').
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from .db import engine, get_scorecard_config, read_engine

S = "sp"


# ---------------------------------------------------------------- helpers
def _clean(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


# ---------------------------------------------------------------- query layer (parallel record/replay)
import json as _json
import threading as _threading
from concurrent.futures import ThreadPoolExecutor as _Pool

from sqlalchemy.exc import DBAPIError as _DBAPIError

_TL = _threading.local()            # per-request record/replay state
_POOL = _Pool(max_workers=16, thread_name_prefix="sql")


class _NoneDict(dict):
    def __missing__(self, key):
        return None


class _Stub(list):
    """Empty result returned while recording; tolerates rows[0]['col'] access."""
    def __getitem__(self, i):
        return _NoneDict() if isinstance(i, int) else []


def _run(sql: str, params: dict, one: bool = False):
    for attempt in (1, 2):           # one retry: pre-ping is off, so a recycled connection may be stale
        try:
            with read_engine.connect() as c:
                res = c.execute(text(sql), params)
                if one:
                    return _clean(res.scalar())
                cols = list(res.keys())
                return [{k: _clean(v) for k, v in zip(cols, row)} for row in res.fetchall()]
        except _DBAPIError as e:
            if attempt == 2 or not e.connection_invalidated:
                raise


def _dispatch(sql: str, params: dict, one: bool):
    mode = getattr(_TL, "mode", None)
    key = (sql, one, _json.dumps(params, sort_keys=True, default=str))
    if mode == "record":
        _TL.recorded[key] = (sql, params, one)
        return None if one else _Stub()
    if mode == "replay" and key in _TL.results:
        return _TL.results[key]
    return _run(sql, params, one)


def query(sql: str, **params) -> list[dict]:
    return _dispatch(sql, params, False)


def scalar(sql: str, **params):
    return _dispatch(sql, params, True)


def _prefetched(fn):
    """Run fn once to collect its SQL, execute all statements in parallel, then run fn on the results.
    Queries whose SQL depends on earlier results simply execute live during the replay pass."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **k):
        if getattr(_TL, "mode", None):
            return fn(*a, **k)
        _TL.mode, _TL.recorded = "record", {}
        try:
            fn(*a, **k)
        except Exception:
            pass                       # recording is best-effort; replay falls back to live queries
        recorded, _TL.mode = _TL.recorded, None
        futs = {key: _POOL.submit(_run, sql, params, one) for key, (sql, params, one) in recorded.items()}
        _TL.mode, _TL.results = "replay", {key: f.result() for key, f in futs.items()}
        try:
            return fn(*a, **k)
        finally:
            _TL.mode, _TL.results = None, {}
    return wrapper


def _num(v, default=0.0) -> float:
    return default if v is None else float(v)


def _clamp(v: float, lo=0.0, hi=100.0) -> float:
    return max(lo, min(hi, v))


def _r(v, n=1):
    return None if v is None else round(float(v), n)


def _where(date_col: str | None, start=None, end=None, plant_id=None, supplier_id=None,
           extra: str | None = None) -> tuple[str, dict]:
    parts, p = ["TRUE"], {}
    if date_col and start:
        parts.append(f"{date_col} >= CAST(:start AS date)"); p["start"] = start
    if date_col and end:
        parts.append(f"{date_col} < CAST(:end AS date) + 1"); p["end"] = end
    if plant_id:
        parts.append("plant_id = :plant_id"); p["plant_id"] = plant_id
    ids = _sup_list(supplier_id)
    if ids is not None:                                  # one supplier, or a filtered set ("SUP-001,SUP-004")
        parts.append("supplier_id = ANY(CAST(:supplier_ids AS text[]))"); p["supplier_ids"] = ids
    if extra:
        parts.append(extra)
    return " AND ".join(parts), p


def _sup_list(supplier_id) -> list[str] | None:
    """None = no supplier filter. A list (possibly empty) = only these suppliers."""
    if supplier_id is None or supplier_id == "":
        return None
    if isinstance(supplier_id, (list, tuple, set)):
        return [str(x) for x in supplier_id]
    return [x.strip() for x in str(supplier_id).split(",") if x.strip()] or ["__none__"]


def as_of_date() -> str:
    return scalar(f"SELECT {S}.as_of_date()")


def data_coverage() -> dict:
    row = query(f"""
      SELECT (SELECT MIN(po_date) FROM {S}.fact_purchase_order_receipt)        AS po_from,
             (SELECT MAX(receipt_date) FROM {S}.fact_purchase_order_receipt)   AS receipts_to,
             (SELECT MIN(inspection_ts)::date FROM {S}.fact_quality_inspection) AS inspections_from,
             (SELECT MAX(inspection_ts)::date FROM {S}.fact_quality_inspection) AS inspections_to,
             {S}.as_of_date() AS as_of_date""")[0]
    return row


def suppliers() -> list[dict]:
    return query(f"SELECT * FROM {S}.dim_supplier ORDER BY supplier_id")


# ---------------------------------------------------------------- scorecard
def scorecard(start=None, end=None, plant_id=None, supplier_id=None, weights: dict | None = None) -> list[dict]:
    cfg = get_scorecard_config()
    w = weights or cfg["weights"]

    wd, pd_ = _where("promised_date", start, end, plant_id, supplier_id)
    delivery = {r["supplier_id"]: r for r in query(f"""
      SELECT supplier_id,
        COUNT(*) FILTER (WHERE NOT excluded)                                         AS po_lines,
        COUNT(*) FILTER (WHERE measurable)                                           AS lines_measured,
        100.0 * AVG(otif::int)     FILTER (WHERE measurable)                         AS otif_pct,
        100.0 * AVG(on_time::int)  FILTER (WHERE measurable)                         AS on_time_pct,
        100.0 * AVG(in_full::int)  FILTER (WHERE measurable AND delivery_status = 'RECEIVED') AS in_full_pct,
        100.0 * AVG(lead_time_adherent::int) FILTER (WHERE measurable AND delivery_status = 'RECEIVED') AS lead_time_adherence_pct,
        AVG(lead_time_variance_days) FILTER (WHERE measurable AND delivery_status = 'RECEIVED') AS avg_lead_time_variance_days,
        AVG(delay_days) FILTER (WHERE measurable AND delivery_status = 'RECEIVED' AND NOT on_time) AS avg_delay_when_late_days,
        COUNT(*) FILTER (WHERE delivery_status = 'PAST_DUE' AND NOT excluded)        AS past_due_lines,
        SUM(received_qty) FILTER (WHERE delivery_status = 'RECEIVED' AND NOT excluded) AS received_qty,
        100.0 * SUM(received_qty) FILTER (WHERE delivery_status = 'RECEIVED' AND NOT excluded)
              / NULLIF(SUM(ordered_qty) FILTER (WHERE delivery_status = 'RECEIVED' AND NOT excluded), 0) AS fill_rate_pct,
        SUM(receipt_value_inr) FILTER (WHERE NOT excluded)                           AS spend_inr,
        100.0 * SUM(price_variance_inr) FILTER (WHERE NOT excluded)
              / NULLIF(SUM(standard_price_inr * received_qty) FILTER (WHERE NOT excluded), 0) AS price_variance_pct,
        COUNT(*) FILTER (WHERE excluded)                                             AS excluded_lines
      FROM {S}.v_po_delivery WHERE {wd} GROUP BY supplier_id""", **pd_)}

    wq, pq = _where("inspection_ts", start, end, plant_id, supplier_id)
    quality = {r["supplier_id"]: r for r in query(f"""
      SELECT supplier_id, SUM(attribution_weight) AS defects,
             SUM(attribution_weight) FILTER (WHERE severity = 'CRITICAL') AS critical_defects,
             COUNT(DISTINCT inspection_id) AS implicated_inspections
      FROM {S}.v_defect_attribution WHERE {wq} GROUP BY supplier_id""", **pq)}

    wn, pn = _where("opened_ts", start, end, plant_id, supplier_id)
    ncr = {r["supplier_id"]: r for r in query(f"""
      SELECT supplier_id,
        SUM(attribution_weight)                                              AS ncr_count,
        SUM(attribution_weight) FILTER (WHERE is_open)                       AS open_ncr,
        SUM(attribution_weight) FILTER (WHERE is_overdue)                    AS overdue_capa,
        SUM(attribution_weight) FILTER (WHERE capa_status IN ('CLOSED','VERIFIED')) AS closed_ncr,
        SUM(attribution_weight) FILTER (WHERE supplier_related)              AS supplier_related_ncr,
        AVG(open_age_days) FILTER (WHERE is_open)                            AS avg_open_age_days,
        MAX(open_age_days)                                                   AS max_open_age_days
      FROM {S}.v_ncr_supplier WHERE {wn} GROUP BY supplier_id""", **pn)}

    wr, pr = _where("request_date", start, end, None, supplier_id)
    rfq = {r["supplier_id"]: r for r in query(f"""
      SELECT supplier_id, COUNT(*) AS quotes, AVG(commercial_score) AS avg_commercial_score,
             100.0 * AVG((technical_compliance = 'COMPLIANT')::int) AS compliance_pct,
             100.0 * AVG((approval_status = 'APPROVED')::int)        AS quote_win_pct,
             AVG(lead_time_days) AS avg_quoted_lead_time_days
      FROM {S}.fact_rfq_quote WHERE {wr} GROUP BY supplier_id""", **pr)}

    # Direct incoming (GRN) inspection when that extract is loaded: preferred PPM source (A3/E2).
    wi, pi = _where("inspection_date", start, end, plant_id, supplier_id)
    incoming = {r["supplier_id"]: r for r in query(f"""
      SELECT supplier_id, SUM(inspected_qty) AS inspected, SUM(rejected_qty) AS rejected, COUNT(*) AS lots
      FROM {S}.v_incoming_quality WHERE {wi} GROUP BY supplier_id""", **pi)}

    # Supplier response time to NCRs (A4/E4): first portal response - NCR opened.
    resp = {r["supplier_id"]: r for r in query(f"""
      SELECT supplier_id, AVG(response_days) AS avg_response_days, COUNT(response_days) AS responded,
             COUNT(DISTINCT ncr_id) AS ncrs
      FROM {S}.v_ncr_supplier WHERE {wn} GROUP BY supplier_id""", **pn)}

    wdis, pdis = _where("start_ts", start, end, plant_id, supplier_id, "supplier_id IS NOT NULL")
    disruption = {r["supplier_id"]: r for r in query(f"""
      SELECT supplier_id, SUM(duration_min * attribution_weight) / 60.0 AS disruption_hours,
             SUM(impact_units * attribution_weight) AS disruption_units
      FROM {S}.v_supply_disruption WHERE {wdis} GROUP BY supplier_id""", **pdis)}

    sup_sql = f"SELECT * FROM {S}.dim_supplier" + (" WHERE supplier_id = :sid" if supplier_id else "")
    out = []
    for s in query(sup_sql, **({"sid": supplier_id} if supplier_id else {})):
        sid = s["supplier_id"]
        d, q, n, r, x = (delivery.get(sid, {}), quality.get(sid, {}), ncr.get(sid, {}),
                         rfq.get(sid, {}), disruption.get(sid, {}))
        received = _num(d.get("received_qty"))
        defects = _num(q.get("defects"))
        inc = incoming.get(sid)
        if inc and _num(inc.get("inspected")) > 0:
            ppm, ppm_method = _num(inc["rejected"]) / _num(inc["inspected"]) * 1e6, "INCOMING_INSPECTION"
        else:
            ppm = defects / received * 1e6 if received else None
            ppm_method = "GENEALOGY_ATTRIBUTED_PROXY"
        rs = resp.get(sid, {})

        otif = d.get("otif_pct")
        delivery_score = None
        if otif is not None:
            delivery_score = (0.6 * _num(otif) + 0.2 * _num(d.get("lead_time_adherence_pct"), 100)
                              + 0.2 * min(100.0, _num(d.get("fill_rate_pct"), 100)))
        quality_score = 100.0
        if ppm is not None:
            span = max(1.0, cfg["ppm_ceiling"] - cfg["ppm_target"])
            quality_score = 100.0 * (1 - _clamp((ppm - cfg["ppm_target"]) / span, 0, 1))
        ncr_n = _num(n.get("ncr_count"))
        if ncr_n:
            responsiveness_score = 100.0 * (0.5 * (1 - _num(n.get("overdue_capa")) / ncr_n)
                                            + 0.5 * (_num(n.get("closed_ncr")) / ncr_n))
        else:
            responsiveness_score = 100.0
        price_part = _clamp(100 - 10 * max(0.0, _num(d.get("price_variance_pct"))))
        commercial_score = (0.5 * price_part + 0.5 * _num(r["avg_commercial_score"])) if r else price_part

        parts = {"delivery": delivery_score, "quality": quality_score,
                 "responsiveness": responsiveness_score, "commercial": commercial_score}
        tot_w = sum(w[k] for k, v in parts.items() if v is not None) or 1
        composite = sum(w[k] * v for k, v in parts.items() if v is not None) / tot_w
        bands = cfg["grade_bands"]
        grade = "A" if composite >= bands["A"] else "B" if composite >= bands["B"] else "C" if composite >= bands["C"] else "D"

        rc = cfg["risk_score"]
        tier_pts = _num(rc["tier_points"].get(s["risk_tier"], 0))
        risk_score = _clamp(rc["composite_factor"] * (100 - composite) + tier_pts
                            + min(rc["capa_points_cap"], rc["capa_points_each"] * _num(n.get("overdue_capa")))
                            + min(rc["disruption_points_cap"], _num(x.get("disruption_hours"))))
        hb = cfg["health_bands"]
        health = "AT_RISK" if risk_score >= hb["risk"] else "WATCH" if risk_score >= hb["watch"] else "HEALTHY"

        out.append({
            "supplier_id": sid, "supplier_name": s["supplier_name"], "country": s["country"],
            "material_category": s["material_category"], "risk_tier": s["risk_tier"],
            "quality_certification": s["quality_certification"],
            "standard_lead_time_days": s["standard_lead_time_days"],
            "po_lines": d.get("po_lines", 0), "lines_measured": d.get("lines_measured", 0),
            "otif_pct": _r(otif), "on_time_pct": _r(d.get("on_time_pct")),
            "in_full_pct": _r(d.get("in_full_pct")), "fill_rate_pct": _r(d.get("fill_rate_pct")),
            "lead_time_adherence_pct": _r(d.get("lead_time_adherence_pct")),
            "avg_lead_time_variance_days": _r(d.get("avg_lead_time_variance_days")),
            "avg_delay_when_late_days": _r(d.get("avg_delay_when_late_days")),
            "past_due_lines": d.get("past_due_lines", 0), "excluded_lines": d.get("excluded_lines", 0),
            "received_qty": received, "spend_inr": _r(d.get("spend_inr"), 0),
            "price_variance_pct": _r(d.get("price_variance_pct"), 2),
            "defects": _r(defects, 2), "critical_defects": _r(q.get("critical_defects"), 2),
            "incoming_ppm": _r(ppm), "ppm_method": ppm_method,
            "avg_response_days": _r(rs.get("avg_response_days"), 1),
            "ncr_responded": rs.get("responded", 0), "ncr_awaiting_response": (rs.get("ncrs") or 0) - (rs.get("responded") or 0),
            "ncr_count": _r(ncr_n, 2), "open_ncr": _r(n.get("open_ncr"), 2),
            "overdue_capa": _r(n.get("overdue_capa"), 2),
            "avg_open_age_days": _r(n.get("avg_open_age_days")),
            "max_open_age_days": n.get("max_open_age_days"),
            "rfq_quotes": r.get("quotes", 0), "avg_commercial_score": _r(r.get("avg_commercial_score")),
            "rfq_compliance_pct": _r(r.get("compliance_pct")), "quote_win_pct": _r(r.get("quote_win_pct")),
            "disruption_hours": _r(x.get("disruption_hours"), 2),
            "scores": {k: _r(v) for k, v in parts.items()},
            "composite_score": _r(composite), "grade": grade, "risk_score": _r(risk_score), "health": health,
        })

    out.sort(key=lambda r: -r["composite_score"])
    for i, r in enumerate(out, 1):
        r["performance_rank"] = i
    for i, r in enumerate(sorted(out, key=lambda r: -r["risk_score"]), 1):
        r["risk_rank"] = i
    if getattr(_TL, "mode", None) != "record":
        run_id = log_run("scorecard", {"start": start, "end": end, "plant_id": plant_id, "supplier_id": supplier_id,
                                       "weights": weights}, out, snapshot=not (start or end or plant_id or supplier_id or weights))
        for r in out:
            r["run_id"] = run_id
    return out


def overview(start=None, end=None, plant_id=None, suppliers=None) -> dict:
    cards = scorecard(start, end, plant_id)
    keep = _sup_list(suppliers)
    if keep is not None:
        cards = [c for c in cards if c["supplier_id"] in keep]
    wd, pd_ = _where("promised_date", start, end, plant_id, suppliers)
    tot = query(f"""
      SELECT 100.0 * AVG(otif::int) FILTER (WHERE measurable) AS otif_pct,
             SUM(received_qty) FILTER (WHERE delivery_status='RECEIVED' AND NOT excluded) AS received_qty,
             SUM(receipt_value_inr) FILTER (WHERE NOT excluded) AS spend_inr,
             AVG(lead_time_variance_days) FILTER (WHERE measurable AND delivery_status='RECEIVED') AS lt_var,
             COUNT(*) FILTER (WHERE delivery_status='PAST_DUE' AND NOT excluded) AS past_due,
             100.0 * SUM(received_qty) FILTER (WHERE delivery_status='RECEIVED' AND NOT excluded)
               / NULLIF(SUM(ordered_qty) FILTER (WHERE delivery_status='RECEIVED' AND NOT excluded), 0) AS fill_rate
      FROM {S}.v_po_delivery WHERE {wd}""", **pd_)[0]
    wi, pi = _where("inspection_ts", start, end, plant_id)
    qa = query(f"""SELECT 100.0 * AVG((result = 'PASS')::int) AS pass_pct
                   FROM {S}.fact_quality_inspection WHERE {wi}""", **pi)[0]
    defects = sum(_num(c["defects"]) for c in cards)
    received = _num(tot["received_qty"])
    return {
        "as_of_date": as_of_date(),
        "suppliers": len(cards),
        "spend_inr": _r(tot["spend_inr"], 0),
        "otif_pct": _r(tot["otif_pct"]),
        "incoming_ppm": _r(defects / received * 1e6) if received else None,
        "avg_lead_time_variance_days": _r(tot["lt_var"]),
        "past_due_lines": tot["past_due"],
        "open_ncr": _r(sum(_num(c["open_ncr"]) for c in cards), 0),
        "overdue_capa": _r(sum(_num(c["overdue_capa"]) for c in cards), 0),
        "disruption_hours": _r(sum(_num(c["disruption_hours"]) for c in cards), 1),
        "avg_composite_score": _r(sum(c["composite_score"] for c in cards) / max(1, len(cards))),
        "grade_mix": {g: sum(1 for c in cards if c["grade"] == g) for g in "ABCD"},
        "quality_acceptance_pct": _r(qa["pass_pct"]),        # inspections passed / inspections
        "fill_rate_pct": _r(tot["fill_rate"]),                 # received qty / ordered qty (received lines)
        "ppm_method": "INCOMING_INSPECTION" if any(c.get("ppm_method") == "INCOMING_INSPECTION" for c in cards) else "GENEALOGY_ATTRIBUTED_PROXY",
        "health_mix": {h: sum(1 for c in cards if c.get("health") == h) for h in ("HEALTHY", "WATCH", "AT_RISK")},
        "avg_response_days": _r(sum(_num(c["avg_response_days"]) for c in cards if c.get("avg_response_days") is not None)
                                / max(1, sum(1 for c in cards if c.get("avg_response_days") is not None)), 1)
                             if any(c.get("avg_response_days") is not None for c in cards) else None,
        "run_id": cards[0].get("run_id") if cards else None,
    }


# ---------------------------------------------------------------- trends
def trends(supplier_id=None, grain="week", plant_id=None) -> list[dict]:
    grain = "month" if grain == "month" else "week"
    wd, pd_ = _where(None, plant_id=plant_id, supplier_id=supplier_id, extra="measurable")
    deliv = query(f"""
      SELECT date_trunc('{grain}', promised_date)::date AS period,
             100.0 * AVG(otif::int) AS otif_pct, 100.0 * AVG(on_time::int) AS on_time_pct,
             AVG(GREATEST(delay_days, 0)) AS avg_delay_days,
             AVG(lead_time_variance_days) AS avg_lead_time_variance_days, COUNT(*) AS lines
      FROM {S}.v_po_delivery WHERE {wd} GROUP BY 1""", **pd_)
    wr, pr = _where(None, plant_id=plant_id, supplier_id=supplier_id,
                    extra="delivery_status = 'RECEIVED' AND NOT excluded")
    recv = {r["period"]: r["qty"] for r in query(f"""
      SELECT date_trunc('{grain}', receipt_date)::date AS period, SUM(received_qty) AS qty
      FROM {S}.v_po_delivery WHERE {wr} GROUP BY 1""", **pr)}
    wq, pq = _where(None, plant_id=plant_id, supplier_id=supplier_id)
    defs = {r["period"]: r for r in query(f"""
      SELECT date_trunc('{grain}', inspection_ts)::date AS period, SUM(attribution_weight) AS defects,
             SUM(attribution_weight) FILTER (WHERE severity='CRITICAL') AS critical
      FROM {S}.v_defect_attribution WHERE {wq} GROUP BY 1""", **pq)}

    # Only periods that have started by the as-of date: lines promised for later weeks but delivered early would otherwise
    # create sparse "future" periods (1 line -> 0 % or 100 % OTIF) at the end of every chart.
    cut = str(as_of_date())[:10]
    periods = sorted(p for p in ({r["period"] for r in deliv} | set(defs)) if str(p)[:10] <= cut)
    dmap = {r["period"]: r for r in deliv}
    # PPM is unstable when a period has very little receipt volume; require >= 25% of the median period volume.
    vols = sorted(_num(v) for v in recv.values() if v)
    min_qty = 0.25 * vols[len(vols) // 2] if vols else 0
    cfg = get_scorecard_config()
    min_lines, min_units = cfg.get("trend_min_lines", 5), cfg.get("trend_min_received_units", 20000)
    out = []
    for p in periods:
        d, q = dmap.get(p, {}), defs.get(p, {})
        qty = _num(recv.get(p))
        if qty < min_qty:
            qty_for_ppm = 0
        else:
            qty_for_ppm = qty
        if (d.get("lines") or 0) < min_lines:          # too few lines for a rate: show a gap, not a 0 % / 100 % spike
            d = {"lines": d.get("lines", 0)}
        out.append({
            "period": p, "lines": d.get("lines", 0), "otif_pct": _r(d.get("otif_pct")),
            "on_time_pct": _r(d.get("on_time_pct")), "avg_delay_days": _r(d.get("avg_delay_days")),
            "avg_lead_time_variance_days": _r(d.get("avg_lead_time_variance_days")),
            "received_qty": qty, "defects": _r(q.get("defects"), 2),
            "critical_defects": _r(q.get("critical"), 2),
            "ppm": _r(_num(q.get("defects")) / qty_for_ppm * 1e6) if qty_for_ppm else None,
            "low_volume": (d.get("lines", 0) or 0) < min_lines or qty < min_units,
        })
    # Significance of the change vs the previous period (two-proportion z-test), so noise is not read as a trend (B3).
    prev = None
    for r in out:
        r["otif_change_significant"] = r["ppm_change_significant"] = None
        if prev and r["lines"] and prev["lines"] and r["otif_pct"] is not None and prev["otif_pct"] is not None:
            r["otif_change_significant"] = significant_change(prev["otif_pct"] / 100, prev["lines"],
                                                              r["otif_pct"] / 100, r["lines"], cfg.get("trend_alpha", 0.1))
        if prev and r["ppm"] is not None and prev["ppm"] is not None:
            r["ppm_change_significant"] = significant_change(prev["ppm"] / 1e6, prev["received_qty"],
                                                             r["ppm"] / 1e6, r["received_qty"], cfg.get("trend_alpha", 0.1))
        prev = r
    return out


def significant_change(p1: float, n1: float, p2: float, n2: float, alpha: float = 0.1) -> bool:
    """Two-proportion z-test (two-sided). True when the change is unlikely to be noise at level alpha."""
    import math
    if not n1 or not n2:
        return False
    pooled = (p1 * n1 + p2 * n2) / (n1 + n2)
    se = math.sqrt(max(pooled * (1 - pooled), 1e-12) * (1 / n1 + 1 / n2))
    z = abs(p2 - p1) / se if se else 0
    pval = math.erfc(z / math.sqrt(2))
    return pval < alpha


# ---------------------------------------------------------------- risk detection
def risk_alerts() -> list[dict]:
    cfg = get_scorecard_config()
    win = int(cfg["trend_window_days"])
    alerts: list[dict] = []
    names = {s["supplier_id"]: s for s in suppliers()}

    def add(kind, sev, sid, title, detail, action, metric=None):
        alerts.append({"id": f"{kind}-{sid or 'ALL'}-{len(alerts)}", "type": kind, "severity": sev,
                       "supplier_id": sid, "supplier_name": names.get(sid, {}).get("supplier_name"),
                       "title": title, "detail": detail, "recommended_action": action, "metric": metric})

    # 1. Deteriorating OTIF: last N days vs the 2N days before (by promise date).
    for r in query(f"""
      WITH a AS (SELECT {S}.as_of_date() AS d)
      SELECT supplier_id,
        100.0 * AVG(otif::int) FILTER (WHERE promised_date >  a.d - :w)                         AS recent,
        COUNT(*)              FILTER (WHERE promised_date >  a.d - :w)                         AS n_recent,
        100.0 * AVG(otif::int) FILTER (WHERE promised_date <= a.d - :w AND promised_date > a.d - 3 * :w) AS prior,
        COUNT(*)              FILTER (WHERE promised_date <= a.d - :w AND promised_date > a.d - 3 * :w) AS n_prior
      FROM {S}.v_po_delivery, a WHERE measurable GROUP BY supplier_id""", w=win):
        if r["recent"] is None or r["prior"] is None or r["n_recent"] < cfg.get("trend_min_lines", 5):
            continue
        drop = r["prior"] - r["recent"]
        sig = significant_change(r["prior"] / 100, r["n_prior"], r["recent"] / 100, r["n_recent"], cfg.get("trend_alpha", 0.1))
        if drop >= cfg["otif_drop_alert_pts"] and sig:
            sev = "HIGH" if drop >= 15 else "MEDIUM"
            add("OTIF_DETERIORATION", sev, r["supplier_id"],
                f"OTIF down {drop:.1f} pts in the last {win} days",
                f"OTIF {r['recent']:.1f}% (n={r['n_recent']}) vs {r['prior']:.1f}% previously (n={r['n_prior']}).",
                "Request a delivery recovery plan and confirm promise dates on open POs.",
                {"recent": _r(r["recent"]), "prior": _r(r["prior"])})

    # 2. Rising defect rate: recent window vs prior, normalised by received volume and z-tested.
    vols_by_sup = {r["supplier_id"]: r for r in query(f"""
      WITH a AS (SELECT {S}.as_of_date() AS d)
      SELECT supplier_id, SUM(received_qty) FILTER (WHERE receipt_date > a.d - :w) AS recent,
             SUM(received_qty) FILTER (WHERE receipt_date <= a.d - :w AND receipt_date > a.d - 3 * :w) AS prior
      FROM {S}.v_po_delivery, a WHERE delivery_status = 'RECEIVED' AND NOT excluded GROUP BY supplier_id""", w=win)}
    for r in query(f"""
      WITH a AS (SELECT LEAST({S}.as_of_date(), (SELECT MAX(inspection_ts)::date FROM {S}.fact_quality_inspection)) AS d)
      SELECT supplier_id,
        SUM(attribution_weight) FILTER (WHERE inspection_ts::date >  a.d - :w) * 7.0 / :w            AS recent_wk,
        SUM(attribution_weight) FILTER (WHERE inspection_ts::date <= a.d - :w
                                          AND inspection_ts::date > a.d - 3 * :w) * 7.0 / (2 * :w)  AS prior_wk
      FROM {S}.v_defect_attribution, a GROUP BY supplier_id""", w=win):
        rec, pri = _num(r["recent_wk"]), _num(r["prior_wk"])
        vol = vols_by_sup.get(r["supplier_id"], {})
        v_rec, v_pri = _num(vol.get("recent")), _num(vol.get("prior"))
        # volume-normalised: compare defect RATES (per received unit) when receipt volume is known
        if v_rec >= cfg.get("trend_min_received_units", 20000) and v_pri > 0:
            rate_rec, rate_pri = rec * win / 7.0 / v_rec, pri * 2 * win / 7.0 / v_pri
            if not significant_change(rate_pri, v_pri, rate_rec, v_rec, cfg.get("trend_alpha", 0.1)):
                continue
            rec_cmp, pri_cmp = rate_rec, rate_pri
        else:
            rec_cmp, pri_cmp = rec, pri
        if pri_cmp > 0 and rec >= 1 and (rec_cmp - pri_cmp) / pri_cmp * 100 >= cfg["ppm_rise_alert_pct"]:
            rise = (rec_cmp - pri_cmp) / pri_cmp * 100
            add("QUALITY_DETERIORATION", "HIGH" if rise >= 60 else "MEDIUM", r["supplier_id"],
                f"Attributed defects up {rise:.0f}% (per week)",
                f"{rec:.2f} defects/week in the last {win} days vs {pri:.2f} before.",
                "Raise a supplier quality alert; request 8D and tighten incoming inspection on new lots.",
                {"recent_per_week": _r(rec, 2), "prior_per_week": _r(pri, 2)})

    # 3. Repeated delivery failures: latest N measurable lines all late.
    for r in query(f"""
      WITH x AS (SELECT supplier_id, on_time, po_id, promised_date,
                        ROW_NUMBER() OVER (PARTITION BY supplier_id ORDER BY promised_date DESC, po_id) rn
                 FROM {S}.v_po_delivery WHERE measurable)
      SELECT supplier_id, COUNT(*) AS late_streak, STRING_AGG(po_id, ', ' ORDER BY rn) AS pos
      FROM x WHERE rn <= 3 AND NOT on_time GROUP BY supplier_id HAVING COUNT(*) = 3"""):
        add("REPEATED_LATE_DELIVERY", "HIGH", r["supplier_id"], "Last 3 deliveries were all late",
            f"Consecutive late PO lines: {r['pos']}.",
            "Escalate to supplier management; review capacity and expedite open orders.")

    # 4. Repeated quality failures: same defect code with attributed weight >= threshold in window.
    for r in query(f"""
      WITH a AS (SELECT LEAST({S}.as_of_date(), (SELECT MAX(inspection_ts)::date FROM {S}.fact_quality_inspection)) AS d)
      SELECT supplier_id, defect_code, defect_name, SUM(attribution_weight) AS w,
             COUNT(DISTINCT inspection_id) AS n, STRING_AGG(DISTINCT material_id, ', ') AS mats
      FROM {S}.v_defect_attribution, a WHERE inspection_ts::date > a.d - :w
      GROUP BY supplier_id, defect_code, defect_name HAVING SUM(attribution_weight) >= :t
      ORDER BY w DESC""", w=win, t=cfg.get("repeat_defect_min_weight", 2.5)):
        add("REPEATED_DEFECT", "HIGH" if r["w"] >= 3.5 else "MEDIUM", r["supplier_id"],
            f"Repeat defect: {r['defect_name']}",
            f"{r['w']:.1f} attributed defects across {r['n']} failed inspections in {win} days "
            f"(materials {r['mats']}).",
            "Quarantine suspect lots, request root-cause analysis, and verify the CAPA effectiveness.",
            {"attributed": _r(r["w"], 2), "inspections": r["n"], "defect_code": r["defect_code"]})

    # 5. Overdue CAPA.
    for r in query(f"""
      SELECT supplier_id, SUM(attribution_weight) AS overdue, MAX(open_age_days) AS max_age,
             COUNT(DISTINCT ncr_id) AS ncrs
      FROM {S}.v_ncr_supplier WHERE is_overdue GROUP BY supplier_id ORDER BY 2 DESC"""):
        sev = "HIGH" if _num(r["overdue"]) >= 5 or _num(r["max_age"]) >= 60 else "MEDIUM"
        add("OVERDUE_CAPA", sev, r["supplier_id"], f"{_num(r['overdue']):.1f} overdue CAPA (attributed)",
            f"{r['ncrs']} NCRs involving this supplier are past their CAPA due date; oldest open {r['max_age']} days.",
            "Chase supplier response; escalate CAPAs older than 30 days to the quality owner.",
            {"overdue": _r(r["overdue"], 2), "max_age_days": r["max_age"]})

    # 6. Concentration risk.
    thr = cfg["concentration_share_pct"]
    for r in query(f"""
      SELECT * FROM {S}.v_material_supplier_share WHERE share_pct >= :t
      ORDER BY share_pct DESC""", t=thr):
        sev = "HIGH" if r["risk_tier"] == "HIGH" else "MEDIUM" if r["risk_tier"] == "MEDIUM" else "LOW"
        add("CONCENTRATION", sev, r["supplier_id"],
            f"{r['share_pct']:.0f}% of {r['material_name']} spend",
            f"Single-supplier share above {thr:.0f}% for {r['material_id']} (supplier risk tier {r['risk_tier']}).",
            "Assess dual-sourcing and safety stock for this material (sourcing decision stays with procurement).",
            {"share_pct": _r(r["share_pct"]), "material_id": r["material_id"]})

    # 7. Past-due open deliveries (supply risk now).
    for r in query(f"""
      SELECT supplier_id, COUNT(*) AS lines, SUM(ordered_qty * unit_price_inr) AS value,
             MAX({S}.as_of_date() - promised_date) AS max_days, STRING_AGG(DISTINCT material_id, ', ') AS mats
      FROM {S}.v_po_delivery WHERE delivery_status = 'PAST_DUE' AND NOT excluded
      GROUP BY supplier_id ORDER BY value DESC"""):
        sev = "CRITICAL" if r["max_days"] >= 14 else "HIGH" if r["max_days"] >= 7 else "MEDIUM"
        add("PAST_DUE_SUPPLY", sev, r["supplier_id"], f"{r['lines']} PO lines past due",
            f"₹{_num(r['value']):,.0f} outstanding; oldest {r['max_days']} days late; materials {r['mats']}.",
            "Expedite and confirm new delivery dates; check line coverage for affected materials.",
            {"lines": r["lines"], "value_inr": _r(r["value"], 0)})

    # 8. Supply disruption hours.
    for r in query(f"""
      SELECT supplier_id, SUM(duration_min * attribution_weight) / 60.0 AS hours,
             SUM(impact_units * attribution_weight) AS units
      FROM {S}.v_supply_disruption WHERE supplier_id IS NOT NULL GROUP BY supplier_id
      HAVING SUM(duration_min * attribution_weight) / 60.0 >= 1 ORDER BY hours DESC"""):
        add("SUPPLY_DISRUPTION", "HIGH" if r["hours"] >= 4 else "MEDIUM", r["supplier_id"],
            f"{r['hours']:.1f} h of NO_MATERIAL line stoppage",
            f"Line stoppages while this supplier's deliveries were late; ~{_num(r['units']):,.0f} units of output lost.",
            "Review safety stock and supplier capacity; agree a buffer or consignment stock.",
            {"hours": _r(r["hours"], 2)})

    rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    alerts.sort(key=lambda a: (rank[a["severity"]], a["type"]))
    return alerts


# ---------------------------------------------------------------- exposure / issues / sourcing
def exposure(supplier_id: str) -> dict:
    quality = query(f"""
      SELECT a.line_id, a.plant_id, COUNT(DISTINCT a.inspection_id) AS failed_inspections,
             SUM(a.attribution_weight) AS attributed_defects,
             COUNT(DISTINCT a.production_order_id) AS production_orders,
             COUNT(DISTINCT po.customer_order_id) AS customer_orders,
             STRING_AGG(DISTINCT a.product_id, ', ') AS products
      FROM {S}.v_defect_attribution a
      LEFT JOIN {S}.fact_production_order po ON po.production_order_id = a.production_order_id
      WHERE a.supplier_id = :s GROUP BY a.line_id, a.plant_id ORDER BY attributed_defects DESC""", s=supplier_id)
    past_due = query(f"""
      SELECT po_id, po_line, material_id, material_name, plant_id, promised_date, receipt_date,
             ordered_qty, received_qty, {S}.as_of_date() - promised_date AS days_late,
             ordered_qty * unit_price_inr AS value_inr
      FROM {S}.v_po_delivery WHERE supplier_id = :s AND delivery_status = 'PAST_DUE' AND NOT excluded
      ORDER BY days_late DESC""", s=supplier_id)
    mats = sorted({p["material_id"] for p in past_due})
    at_risk_orders = []
    if mats:
        at_risk_orders = query(f"""
          SELECT DISTINCT o.production_order_id, o.plant_id, o.product_id, o.customer_order_id,
                 o.order_qty, o.due_date, o.priority, o.order_status
          FROM {S}.fact_production_order o
          JOIN {S}.fact_bom_component b ON b.product_id = o.product_id
          WHERE b.material_id = ANY(:mats) AND o.order_status NOT IN ('CLOSED','COMPLETED','CANCELLED')
            AND o.plant_id IN (SELECT plant_id FROM {S}.v_po_delivery
                               WHERE supplier_id = :s AND delivery_status = 'PAST_DUE')
          ORDER BY o.due_date""", mats=mats, s=supplier_id)
    share = query(f"""SELECT material_id, material_name, spend_inr, share_pct
                      FROM {S}.v_material_supplier_share WHERE supplier_id = :s ORDER BY share_pct DESC""",
                  s=supplier_id)
    return {
        "supplier_id": supplier_id,
        "quality_exposure_by_line": quality,
        "past_due_lines": past_due,
        "materials_past_due": mats,
        "open_production_orders_at_risk": at_risk_orders[:50],
        "at_risk_production_orders": len(at_risk_orders),
        "at_risk_order_qty": sum(_num(o["order_qty"]) for o in at_risk_orders),
        "at_risk_customer_orders": len({o["customer_order_id"] for o in at_risk_orders}),
        "spend_share_by_material": share,
    }


def supplier_issues(supplier_id: str, only_open: bool = True) -> list[dict]:
    cond = "AND is_open" if only_open else ""
    return query(f"""
      SELECT ncr_id, opened_ts, defect_code, defect_name, defect_severity, root_cause_category,
             containment_action, owner_id, capa_due_date, capa_status, is_overdue, open_age_days,
             material_id, plant_id, line_id, product_id, production_order_id,
             ROUND(attribution_weight::numeric, 2) AS attribution_weight, supplier_related
      FROM {S}.v_ncr_supplier WHERE supplier_id = :s {cond}
      ORDER BY is_overdue DESC, open_age_days DESC NULLS LAST, opened_ts DESC""", s=supplier_id)


def material_sourcing(material_id: str) -> list[dict]:
    rows = query(f"""
      WITH d AS (
        SELECT supplier_id, supplier_name, risk_tier, COUNT(*) FILTER (WHERE measurable) AS lines,
               100.0 * AVG(otif::int) FILTER (WHERE measurable) AS otif_pct,
               AVG(unit_price_inr) AS avg_price_inr, MAX(standard_price_inr) AS standard_price_inr,
               AVG(actual_lead_time_days) FILTER (WHERE delivery_status='RECEIVED') AS avg_lead_time_days,
               SUM(received_qty) FILTER (WHERE delivery_status='RECEIVED' AND NOT excluded) AS received_qty
        FROM {S}.v_po_delivery WHERE material_id = :m GROUP BY supplier_id, supplier_name, risk_tier),
      q AS (SELECT supplier_id, SUM(attribution_weight) AS defects FROM {S}.v_defect_attribution
            WHERE material_id = :m GROUP BY supplier_id),
      r AS (SELECT DISTINCT ON (supplier_id) supplier_id, quoted_unit_price_inr, lead_time_days AS quoted_lead_time_days,
                   technical_compliance, commercial_score, approval_status, request_date
            FROM {S}.fact_rfq_quote WHERE material_id = :m ORDER BY supplier_id, request_date DESC),
      sh AS (SELECT supplier_id, share_pct FROM {S}.v_material_supplier_share WHERE material_id = :m)
      SELECT d.*, q.defects, 1e6 * q.defects / NULLIF(d.received_qty, 0) AS ppm, sh.share_pct,
             r.quoted_unit_price_inr, r.quoted_lead_time_days, r.technical_compliance,
             r.commercial_score, r.approval_status AS latest_quote_status
      FROM d LEFT JOIN q USING (supplier_id) LEFT JOIN r USING (supplier_id) LEFT JOIN sh USING (supplier_id)
      ORDER BY otif_pct DESC NULLS LAST""", m=material_id)
    for r in rows:
        for k in ("otif_pct", "avg_price_inr", "avg_lead_time_days", "ppm", "share_pct", "defects"):
            r[k] = _r(r.get(k), 2)
    return rows


def defect_breakdown(supplier_id=None) -> list[dict]:
    w, p = _where(None, supplier_id=supplier_id)
    return query(f"""
      SELECT defect_code, defect_name, defect_category, material_id,
             ROUND(SUM(attribution_weight)::numeric, 2) AS defects,
             COUNT(DISTINCT inspection_id) AS inspections
      FROM {S}.v_defect_attribution WHERE {w}
      GROUP BY defect_code, defect_name, defect_category, material_id ORDER BY defects DESC""", **p)


# ---------------------------------------------------------------- premium: pulse & what-if
def kpi_pulse(plant_id=None, weeks: int = 12, suppliers=None) -> dict:
    """Weekly sparkline series + change of the last 4 weeks vs the 4 weeks before, per headline KPI.
    With `suppliers`, every series is limited to those suppliers (NCRs and supplier-caused downtime too)."""
    rows = [r for r in trends(suppliers or None, "week", plant_id) if r["lines"] or r["defects"]][-weeks:]

    def series(key):
        return [{"period": r["period"], "v": r[key]} for r in rows]

    def delta(key):
        vals = [r[key] for r in rows if r[key] is not None]
        if len(vals) < 8:
            return None
        cur, prev = sum(vals[-4:]) / 4, sum(vals[-8:-4]) / 4
        return _r(cur - prev, 1)

    wn, pn = _where(None, supplier_id=suppliers)
    ncr = query(f"""
      SELECT date_trunc('week', opened_ts)::date AS period, COUNT(DISTINCT ncr_id) AS n
      FROM {S}.v_ncr_supplier WHERE {wn} GROUP BY 1 ORDER BY 1""", **pn)[-weeks:]
    if suppliers:                                        # downtime attributed to the selected suppliers
        wdd, pdd = _where(None, plant_id=plant_id, supplier_id=suppliers)
        dis = query(f"""
      SELECT date_trunc('week', start_ts)::date AS period, SUM(duration_min * attribution_weight) / 60.0 AS h
      FROM {S}.v_supply_disruption WHERE {wdd} GROUP BY 1 ORDER BY 1""", **pdd)[-weeks:]
    else:
        dis = query(f"""
      SELECT date_trunc('week', start_ts)::date AS period, SUM(duration_min) / 60.0 AS h
      FROM {S}.fact_downtime_event WHERE reason_code = 'NO_MATERIAL'
      {"AND plant_id = :p" if plant_id else ""} GROUP BY 1 ORDER BY 1""", **({"p": plant_id} if plant_id else {}))[-weeks:]
    return {
        "otif_pct": {"series": series("otif_pct"), "delta": delta("otif_pct"), "good": "up"},
        "incoming_ppm": {"series": series("ppm"), "delta": delta("ppm"), "good": "down"},
        "avg_lead_time_variance_days": {"series": series("avg_lead_time_variance_days"),
                                        "delta": delta("avg_lead_time_variance_days"), "good": "down"},
        "ncr_opened": {"series": [{"period": r["period"], "v": r["n"]} for r in ncr], "delta": None, "good": "down"},
        "disruption_hours": {"series": [{"period": r["period"], "v": _r(r["h"], 1)} for r in dis], "delta": None,
                             "good": "down"},
    }


def simulate(weights: dict, start=None, end=None, plant_id=None) -> list[dict]:
    """Re-rank suppliers with trial weights (nothing is saved) and show rank movement."""
    base = {r["supplier_id"]: r for r in scorecard(start, end, plant_id)}
    sim = scorecard(start, end, plant_id, weights=weights)
    keep = ("supplier_id", "supplier_name", "grade", "composite_score", "performance_rank", "scores", "risk_tier")
    out = []
    for r in sim:
        b = base[r["supplier_id"]]
        out.append({**{k: r[k] for k in keep},
                    "baseline_score": b["composite_score"], "baseline_rank": b["performance_rank"],
                    "baseline_grade": b["grade"], "rank_change": b["performance_rank"] - r["performance_rank"]})
    return out


# ---------------------------------------------------------------- governance: run log, snapshots, evidence (F4/F5)
CODE_VERSION = "2.0.0"


def log_run(kind: str, params: dict, rows: list | None = None, snapshot: bool = False, trigger: str = "on-demand") -> str | None:
    """Record a calculation run (config version + hash, data cut-off, code version) and optionally freeze rows."""
    import uuid
    from .db import CalcRun, ScorecardSnapshot, SessionLocal, config_hash, current_config_version
    try:
        run_id = str(uuid.uuid4())
        cfg = get_scorecard_config()
        with SessionLocal() as db:
            db.add(CalcRun(id=run_id, kind=kind, trigger=trigger, params={k: v for k, v in params.items() if v is not None},
                           config_version=current_config_version()[0], config_hash=config_hash(cfg), code_version=CODE_VERSION,
                           as_of_date=str(_run(f"SELECT {S}.as_of_date()", {}, True)), row_count=len(rows or [])))
            if snapshot and rows:
                for r in rows:
                    db.add(ScorecardSnapshot(run_id=run_id, supplier_id=r["supplier_id"], payload=r))
            db.commit()
        return run_id
    except Exception:
        return None


def runs(limit: int = 50) -> list[dict]:
    return _run("""SELECT r.id, r.kind, r.trigger, r.params, r.config_version, r.config_hash, r.code_version, r.as_of_date,
                          r.row_count, r.status, r.created_at, (SELECT COUNT(*) FROM public.scorecard_snapshots s WHERE s.run_id = r.id) AS snapshot_rows
                   FROM public.calc_runs r ORDER BY r.created_at DESC LIMIT :n""", {"n": limit})


def snapshot(run_id: str, supplier_id: str | None = None) -> list[dict]:
    rows = _run("SELECT supplier_id, payload FROM public.scorecard_snapshots WHERE run_id = :r"
                + (" AND supplier_id = :s" if supplier_id else ""), {"r": run_id, **({"s": supplier_id} if supplier_id else {})})
    return [r["payload"] if isinstance(r["payload"], dict) else _json.loads(r["payload"]) for r in rows]


EVIDENCE = {
    "otif": ("On-time & in-full: received by the effective (approved) promise date, within -tolerance/+over-delivery tolerance, not early.",
             """SELECT po_id, po_line, material_id, plant_id, po_date, original_promised_date, promised_date, promise_changed,
                       first_receipt_date, receipt_date, n_receipts, ordered_qty, received_qty, qty_by_promise, delay_days,
                       delay_working_days, on_time, in_full, otif, early_delivery, over_delivery, delivery_status
                FROM sp.v_po_delivery WHERE supplier_id = :s AND measurable ORDER BY promised_date DESC"""),
    "lead_time": ("Lead-time variance = actual lead time (working days, PO date to last receipt) - planned material lead time.",
                  """SELECT po_id, po_line, material_id, po_date, receipt_date, actual_lead_time_days, actual_lead_time_wd,
                            planned_lead_time_days, lead_time_basis, lead_time_variance_days, lead_time_adherent
                     FROM sp.v_po_delivery WHERE supplier_id = :s AND measurable AND delivery_status = 'RECEIVED' ORDER BY po_date DESC"""),
    "ppm": ("PPM = attributed defects / received units x 1e6 (genealogy proxy) unless incoming inspection is loaded.",
            """SELECT inspection_id, inspection_ts, plant_id, line_id, production_order_id, defect_code, defect_name, severity,
                      material_id, material_lots, n_candidates, ROUND(attribution_weight::numeric, 3) AS attribution_weight,
                      ROUND(confidence::numeric, 3) AS confidence, attribution_method
               FROM sp.v_defect_attribution WHERE supplier_id = :s ORDER BY inspection_ts DESC"""),
    "ncr": ("NCR/CAPA involving the supplier (weighted by the number of implicated suppliers); overdue = open past due date.",
            """SELECT ncr_id, opened_ts, defect_code, defect_name, capa_status, capa_due_date, is_open, is_overdue, open_age_days,
                      days_overdue, material_id, ROUND(attribution_weight::numeric, 3) AS attribution_weight, first_response_at,
                      ROUND(response_days::numeric, 1) AS response_days
               FROM sp.v_ncr_supplier WHERE supplier_id = :s ORDER BY opened_ts DESC"""),
    "price": ("Price variance = (PO unit price - standard price) x received qty.",
              """SELECT po_id, po_line, material_id, unit_price_inr, standard_price_inr, received_qty, ROUND(price_variance_inr::numeric, 0) AS price_variance_inr
                 FROM sp.v_po_delivery WHERE supplier_id = :s AND NOT excluded ORDER BY ABS(price_variance_inr) DESC"""),
    "disruption": ("NO_MATERIAL stoppages on lines that consume a material this supplier delivered late at that plant.",
                   """SELECT downtime_id, plant_id, line_id, line_type, start_ts, duration_min, impact_units,
                             ROUND(attribution_weight::numeric, 3) AS attribution_weight
                      FROM sp.v_supply_disruption WHERE supplier_id = :s ORDER BY start_ts DESC"""),
}


def evidence(supplier_id: str, metric: str, limit: int = 500) -> dict:
    """The rows behind a supplier metric, with its definition and the active config version (F4)."""
    from .db import config_hash, current_config_version
    if metric not in EVIDENCE:
        raise KeyError(metric)
    definition, sql_ = EVIDENCE[metric]
    rows = _run(sql_ + " LIMIT :lim", {"s": supplier_id, "lim": limit})
    return {"supplier_id": supplier_id, "metric": metric, "definition": definition, "rows": rows, "row_count": len(rows),
            "as_of_date": str(_run(f"SELECT {S}.as_of_date()", {}, True)), "config_version": current_config_version()[0],
            "config_hash": config_hash(get_scorecard_config()), "code_version": CODE_VERSION}


def dq_results(limit_loads: int = 5) -> list[dict]:
    return _run("""SELECT load_id, table_name, rule, severity, failed_rows, created_at FROM public.dq_results
                   WHERE load_id IN (SELECT load_id FROM public.dq_results GROUP BY load_id ORDER BY MAX(created_at) DESC LIMIT :n)
                   ORDER BY created_at DESC, rule""", {"n": limit_loads})


# ---------------------------------------------------------------- caching (stale-while-revalidate + disk)
# Business data only changes when scripts/load_data.py runs. Results are fresh for CACHE_TTL seconds; after that the
# last value is served instantly while a background thread refreshes it. The cache is persisted to disk so a backend
# restart is instant. Cleared on weight/exclusion changes and on POST /api/cache/clear.
import copy as _copy
import functools as _functools
import os as _os
import time as _time

from .config import BACKEND_DIR as _BACKEND_DIR

CACHE_TTL = 300
_CACHE_FILE = _BACKEND_DIR / ".cache" / "analytics.json"
_CACHE: dict = {}
_LOCK = _threading.Lock()
_INFLIGHT: set = set()
_SAVE_PENDING = [False]
# Results computed by older code must not survive a code change: the disk cache is stamped with a hash of this module.
import hashlib as _hashlib
try:
    _CODE_VER = _hashlib.sha1(open(__file__, "rb").read()).hexdigest()[:12]
except Exception:  # pragma: no cover
    _CODE_VER = "dev"


def _state_save() -> tuple:
    return getattr(_TL, "mode", None), getattr(_TL, "recorded", {}), getattr(_TL, "results", {})


def _state_restore(st) -> None:
    _TL.mode, _TL.recorded, _TL.results = st


def _save_soon() -> None:
    if _SAVE_PENDING[0]:
        return
    _SAVE_PENDING[0] = True

    def _save():
        _time.sleep(2)
        _SAVE_PENDING[0] = False
        try:
            with _LOCK:
                data = [[list(k), v[0], v[1]] for k, v in _CACHE.items()]
            _CACHE_FILE.parent.mkdir(exist_ok=True)
            tmp = _CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(_json.dumps({"v": _CODE_VER, "items": data}, default=str), encoding="utf-8")
            _os.replace(tmp, _CACHE_FILE)
        except Exception:
            pass
    _threading.Thread(target=_save, daemon=True).start()


def _load_disk_cache() -> None:
    try:
        raw = _json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("v") != _CODE_VER:
            return                                  # written by older code: start clean (warm_cache refills it)
        for k, ts, v in raw["items"]:
            _CACHE[tuple(k)] = (0.0, v)      # timestamp 0 = stale: served instantly, refreshed in background
    except Exception:
        pass


def _cached(fn):
    @_functools.wraps(fn)
    def wrapper(*a, **k):
        key = (fn.__name__, _json.dumps([a, k], default=str, sort_keys=True))
        now = _time.time()
        with _LOCK:
            hit = _CACHE.get(key)

        def compute():
            st = _state_save()
            _state_restore((None, {}, {}))   # nested calls run with their own record/replay state
            try:
                val = fn(*a, **k)
            finally:
                _state_restore(st)
            with _LOCK:
                _CACHE[key] = (_time.time(), val)
            _save_soon()
            return val

        if hit:
            if now - hit[0] > CACHE_TTL and key not in _INFLIGHT:
                _INFLIGHT.add(key)

                def refresh():
                    try:
                        compute()
                    except Exception:
                        pass
                    finally:
                        _INFLIGHT.discard(key)
                _threading.Thread(target=refresh, daemon=True).start()
            return _copy.deepcopy(hit[1])
        return _copy.deepcopy(compute())
    return wrapper


def clear_cache() -> None:
    from .db import invalidate_scorecard_config
    invalidate_scorecard_config()                       # config just changed (weights / rules): re-read it too
    with _LOCK:
        _CACHE.clear()
    try:
        _CACHE_FILE.unlink()
    except Exception:
        pass
    # weights / exclusions changed: recompute the main pages and every supplier in the background, so the next visitor
    # does not pay the cold cost (skipped under pytest, which clears the cache often)
    if not _os.environ.get("PYTEST_CURRENT_TEST"):
        _threading.Thread(target=lambda: warm_cache(), daemon=True).start()


def concentration_matrix() -> list[dict]:
    return query(f"SELECT * FROM {S}.v_material_supplier_share ORDER BY material_id, share_pct DESC")


_WARMING = _threading.Lock()


def warm_cache() -> None:
    """Pre-compute the main pages in parallel so first loads are fast. Single-flight: a second call while one runs is skipped."""
    if not _WARMING.acquire(blocking=False):
        return
    try:
        _warm_cache()
    finally:
        _WARMING.release()


def _warm_cache() -> None:
    jobs = [lambda: overview(), lambda: risk_alerts(), lambda: kpi_pulse(), lambda: trends(None, "week"),
            lambda: trends(None, "month"), lambda: data_coverage(), lambda: concentration_matrix(),
            lambda: defect_breakdown(None), lambda: scorecard()]
    with _Pool(max_workers=len(jobs)) as p:
        for f in [p.submit(j) for j in jobs]:
            try:
                f.result()
            except Exception:
                pass
    # Phase 2 (after the main pages): each supplier's drawer / review-pack data, so opening any supplier is instant too.
    try:
        sids = [r["supplier_id"] for r in scorecard()]
    except Exception:
        return
    per = [f for sid in sids for f in (lambda s=sid: scorecard(None, None, None, s), lambda s=sid: trends(s, "week"),
                                       lambda s=sid: trends(s, "month"), lambda s=sid: supplier_issues(s),
                                       lambda s=sid: exposure(s), lambda s=sid: defect_breakdown(s))]
    with _Pool(max_workers=6) as p:                  # modest: leave pool connections free for users
        for f in [p.submit(j) for j in per]:
            try:
                f.result()
            except Exception:
                pass


for _name in ("scorecard", "overview", "trends", "risk_alerts", "exposure", "kpi_pulse"):
    globals()[_name] = _prefetched(globals()[_name])
for _name in ("as_of_date", "data_coverage", "suppliers", "scorecard", "overview", "trends", "risk_alerts", "exposure",
              "supplier_issues", "material_sourcing", "defect_breakdown", "kpi_pulse", "concentration_matrix",
              "get_scorecard_config"):
    globals()[_name] = _cached(globals()[_name])
_load_disk_cache()
