"""Supplier Digital Twin: a calibrated, simulatable mirror of the inbound supply network.

MIRROR    Every supplier x material x plant flow is calibrated from history:
          - lead-time and fill-ratio distributions per supplier (received PO lines)
          - supplier share of each plant/material flow (received qty)
          - starting usable stock and safety stock per plant/material (latest inventory snapshot)
          - daily usage per plant/material (average receipts over the last 90 days = steady-state consumption)
          - open pipeline (PO lines not yet received) per supplier
SIMULATE  Monte Carlo over N runs x H days with common random numbers, so scenario vs baseline differences come
          from the scenario, not from noise. Scenarios: supplier delay, capacity drop, outage, lot rejection,
          demand change, regional shock (all overseas suppliers). Mitigations: dual-sourcing, safety-stock
          build, expediting, all with a planner response delay.
CONNECT   Results roll up to finished units, line hours and revenue at risk, open customer backlog and the factory
          twin (model TWIN-1.7) line utilisation.
VALIDATE  Backtest: the twin calibrated on older PO lines predicts each supplier's on-time rate on recent lines.

Simulations are decision support only: scenarios are saved as drafts and require approval (guide controls).
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from . import analytics as A

S = A.S
OPEN_CO = ("OPEN", "CONFIRMED", "IN_PRODUCTION", "PARTIALLY_SHIPPED")
WARMUP_DAYS = 60
BASE_LOT_REJECT = 0.005


# ---------------------------------------------------------------- calibration (cached, parallel)
def _calibration() -> dict:
    flows = A.query(f"""
      SELECT plant_id, material_id, supplier_id, SUM(received_qty) AS qty, SUM(receipt_value_inr) AS spend
      FROM {S}.v_po_delivery WHERE delivery_status = 'RECEIVED' AND NOT excluded
      GROUP BY plant_id, material_id, supplier_id""")
    dist = A.query(f"""
      SELECT supplier_id,
             array_agg(actual_lead_time_days) AS lead_times,
             array_agg(received_qty::float / NULLIF(ordered_qty, 0)) AS fills,
             array_agg(delay_days) AS delays,
             AVG(on_time::int) AS on_time_rate
      FROM {S}.v_po_delivery WHERE delivery_status = 'RECEIVED' AND NOT excluded GROUP BY supplier_id""")
    demand = A.query(f"""
      SELECT plant_id, material_id, SUM(received_qty) / 90.0 AS daily
      FROM {S}.v_po_delivery
      WHERE delivery_status = 'RECEIVED' AND NOT excluded AND receipt_date > {S}.as_of_date() - 90
      GROUP BY plant_id, material_id""")
    stock = A.query(f"""
      SELECT plant_id, material_id,
             SUM(on_hand_qty - allocated_qty - blocked_qty - quality_hold_qty) AS usable,
             SUM(quality_hold_qty) AS on_hold, SUM(in_transit_qty) AS in_transit, MAX(safety_stock_qty) AS safety,
             MAX(snapshot_date) AS snapshot_date
      FROM {S}.fact_inventory_daily_snapshot
      WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM {S}.fact_inventory_daily_snapshot)
      GROUP BY plant_id, material_id""")
    pipeline = A.query(f"""
      SELECT supplier_id, plant_id, material_id, COUNT(*) AS lines, SUM(ordered_qty) AS qty,
             COUNT(*) FILTER (WHERE delivery_status = 'PAST_DUE') AS past_due_lines
      FROM {S}.v_po_delivery WHERE delivery_status <> 'RECEIVED' AND NOT excluded
      GROUP BY supplier_id, plant_id, material_id""")
    backlog = A.query(f"""
      SELECT COUNT(*) AS orders, SUM(order_qty - shipped_qty) AS units,
             SUM((order_qty - shipped_qty) * unit_price_inr) AS value, AVG(unit_price_inr) AS avg_price
      FROM {S}.fact_customer_order WHERE order_status IN ('OPEN','CONFIRMED','IN_PRODUCTION','PARTIALLY_SHIPPED')""")
    factory = A.query(f"""
      WITH ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY line_id, scenario_name ORDER BY run_ts DESC) AS rn
        FROM {S}.fact_digital_twin_scenario)
      SELECT r.plant_id, r.line_id, l.line_type, l.nominal_capacity_per_hour, r.scenario_name, r.model_version,
             r.run_ts, r.demand_change_pct, r.capacity_change_pct, r.predicted_output_units,
             r.predicted_utilization_pct, r.actual_output_comparison_units, r.prediction_error_pct
      FROM ranked r JOIN {S}.dim_production_line l ON l.line_id = r.line_id WHERE rn = 1
      ORDER BY r.plant_id, r.line_id, r.scenario_name""")
    factory_fit = A.query(f"""
      SELECT model_version, COUNT(*) AS runs, AVG(prediction_error_pct) AS avg_error_pct
      FROM {S}.fact_digital_twin_scenario GROUP BY model_version""")
    backtest = A.query(f"""
      WITH a AS (SELECT {S}.as_of_date() - 45 AS split)
      SELECT supplier_id,
             AVG(on_time::int) FILTER (WHERE promised_date <  a.split)                   AS train_on_time,
             AVG(on_time::int) FILTER (WHERE promised_date >= a.split)                   AS test_on_time,
             COUNT(*)          FILTER (WHERE promised_date >= a.split)                   AS test_lines,
             AVG(actual_lead_time_days) FILTER (WHERE promised_date <  a.split AND delivery_status = 'RECEIVED') AS train_lt,
             AVG(actual_lead_time_days) FILTER (WHERE promised_date >= a.split AND delivery_status = 'RECEIVED') AS test_lt
      FROM {S}.v_po_delivery, a WHERE measurable GROUP BY supplier_id""")
    materials = A.query(f"SELECT material_id, material_name, material_category, standard_price_inr FROM {S}.dim_material")
    lines = A.query(f"SELECT * FROM {S}.dim_production_line")
    return {"flows": flows, "dist": dist, "demand": demand, "stock": stock, "pipeline": pipeline,
            "backlog": backlog[0] if backlog else {}, "factory": factory, "factory_fit": factory_fit,
            "backtest": backtest, "materials": materials, "lines": lines}


def calibration() -> dict:
    return _calibration_cached()


_calibration_cached = A._cached(A._prefetched(_calibration))


def _model() -> dict:
    """Turn calibration rows into simulation-ready structures."""
    c = calibration()
    sup = {s["supplier_id"]: s for s in A.suppliers()}
    cards = {r["supplier_id"]: r for r in A.scorecard()}
    dist = {}
    for d in c["dist"]:
        lts = [x for x in (d["lead_times"] or []) if x is not None and x >= 0] or [sup[d["supplier_id"]]["standard_lead_time_days"]]
        fills = [min(1.0, x) for x in (d["fills"] or []) if x is not None] or [0.97]
        dist[d["supplier_id"]] = {"lt": np.array(lts, float), "fill": np.array(fills, float),
                                  "on_time": float(d["on_time_rate"] or 0), "mean_fill": float(np.mean(fills))}
    shares = defaultdict(dict)
    for f in c["flows"]:
        shares[(f["plant_id"], f["material_id"])][f["supplier_id"]] = float(f["qty"] or 0)
    for k, v in shares.items():
        tot = sum(v.values()) or 1
        shares[k] = {s: q / tot for s, q in v.items()}
    demand = {(d["plant_id"], d["material_id"]): float(d["daily"] or 0) for d in c["demand"]}
    stock = {(s["plant_id"], s["material_id"]): s for s in c["stock"]}
    mats = {m["material_id"]: m for m in c["materials"]}
    return {"cal": c, "sup": sup, "cards": cards, "dist": dist, "shares": shares, "demand": demand,
            "stock": stock, "mats": mats}


# ---------------------------------------------------------------- mirror views
def network() -> dict:
    """Nodes (suppliers, materials, plants) and flow edges with live risk for the 3D twin."""
    m = _model()
    c = m["cal"]
    nodes, edges = [], []
    for sid, s in m["sup"].items():
        card = m["cards"].get(sid, {})
        nodes.append({"id": sid, "kind": "supplier", "label": s["supplier_name"], "country": s["country"],
                      "risk_tier": s["risk_tier"], "score": card.get("composite_score"), "grade": card.get("grade"),
                      "otif_pct": card.get("otif_pct"), "risk_score": card.get("risk_score")})
    mat_flow = defaultdict(float)
    sm_flow = defaultdict(float)
    for f in c["flows"]:
        sm_flow[(f["supplier_id"], f["material_id"])] += float(f["qty"] or 0)
        mat_flow[f["material_id"]] += float(f["qty"] or 0)
    for (sid, mid), q in sm_flow.items():
        edges.append({"source": sid, "target": mid, "kind": "supply", "qty": q,
                      "share_pct": round(100 * q / (mat_flow[mid] or 1), 1),
                      "on_time": round(100 * m["dist"].get(sid, {}).get("on_time", 0), 1)})
    plants = sorted({k[0] for k in m["demand"]})
    for mid, mt in m["mats"].items():
        covers = []
        for p in plants:
            d = m["demand"].get((p, mid), 0)
            st = m["stock"].get((p, mid), {})
            usable = float(st.get("usable") or 0)
            cover = usable / d if d else None
            covers.append(cover)
            edges.append({"source": mid, "target": p, "kind": "consume", "daily_demand": round(d),
                          "usable": usable, "safety": st.get("safety"), "days_cover": None if cover is None else round(cover, 1)})
        valid = [x for x in covers if x is not None]
        nodes.append({"id": mid, "kind": "material", "label": mt["material_name"], "category": mt["material_category"],
                      "min_days_cover": round(min(valid), 1) if valid else None})
    for p in plants:
        nodes.append({"id": p, "kind": "plant", "label": p,
                      "daily_units": round(np.median([m["demand"].get((p, mid), 0) for mid in m["mats"]]))})
    return {"nodes": nodes, "edges": edges, "snapshot_date": (c["stock"][0]["snapshot_date"] if c["stock"] else None)}


def supplier_state(supplier_id: str) -> dict:
    """The twin's live mirror of one supplier: calibrated behaviour, pipeline and fidelity."""
    m = _model()
    c = m["cal"]
    s = m["sup"].get(supplier_id)
    if not s:
        return {"error": f"unknown supplier {supplier_id}"}
    d = m["dist"].get(supplier_id, {})
    lt = d.get("lt", np.array([0.0]))
    pipe = [p for p in c["pipeline"] if p["supplier_id"] == supplier_id]
    bt = next((b for b in c["backtest"] if b["supplier_id"] == supplier_id), {})
    pred, act = bt.get("train_on_time"), bt.get("test_on_time")
    shares = sorted(((k, v[supplier_id]) for k, v in m["shares"].items() if supplier_id in v), key=lambda x: -x[1])
    return {
        "supplier_id": supplier_id, "supplier_name": s["supplier_name"], "country": s["country"],
        "risk_tier": s["risk_tier"], "standard_lead_time_days": s["standard_lead_time_days"],
        "scorecard": {k: m["cards"].get(supplier_id, {}).get(k) for k in
                      ("grade", "composite_score", "otif_pct", "incoming_ppm", "overdue_capa", "risk_rank")},
        "calibrated": {
            "lead_time_mean_days": round(float(lt.mean()), 1), "lead_time_p50_days": float(np.percentile(lt, 50)),
            "lead_time_p90_days": float(np.percentile(lt, 90)), "on_time_probability_pct": round(100 * d.get("on_time", 0), 1),
            "mean_fill_pct": round(100 * d.get("mean_fill", 0), 1), "samples": int(lt.size),
            "lead_time_histogram": np.histogram(lt, bins=8)[0].tolist(),
            "lead_time_bins": [round(x, 1) for x in np.histogram(lt, bins=8)[1].tolist()],
        },
        "pipeline": {"open_lines": sum(p["lines"] for p in pipe), "open_qty": sum(p["qty"] for p in pipe),
                     "past_due_lines": sum(p["past_due_lines"] for p in pipe)},
        "flow_shares": [{"plant_id": k[0], "material_id": k[1], "share_pct": round(100 * v, 1)} for k, v in shares[:12]],
        "fidelity": {
            "predicted_on_time_pct": None if pred is None else round(100 * pred, 1),
            "actual_on_time_pct": None if act is None else round(100 * act, 1),
            "abs_error_pts": None if pred is None or act is None else round(100 * abs(pred - act), 1),
            "test_lines": bt.get("test_lines"),
            "predicted_lead_time": None if bt.get("train_lt") is None else round(bt["train_lt"], 1),
            "actual_lead_time": None if bt.get("test_lt") is None else round(bt["test_lt"], 1),
        },
    }


def fidelity() -> dict:
    c = calibration()
    rows = []
    for b in c["backtest"]:
        if b["train_on_time"] is None or b["test_on_time"] is None:
            continue
        rows.append({"supplier_id": b["supplier_id"], "predicted": round(100 * b["train_on_time"], 1),
                     "actual": round(100 * b["test_on_time"], 1),
                     "abs_error": round(100 * abs(b["train_on_time"] - b["test_on_time"]), 1), "test_lines": b["test_lines"]})
    mae = sum(r["abs_error"] for r in rows) / max(1, len(rows))
    ff = c["factory_fit"][0] if c["factory_fit"] else {}
    return {"supply_twin": {"on_time_mae_pts": round(mae, 1), "fidelity_pct": round(100 - mae, 1), "suppliers": rows,
                            "method": "calibrated on PO lines promised >45 days before as-of; tested on later lines"},
            "factory_twin": {"model_version": ff.get("model_version"), "runs": ff.get("runs"),
                             "avg_prediction_error_pct": None if ff.get("avg_error_pct") is None else round(ff["avg_error_pct"], 2),
                             "fidelity_pct": None if ff.get("avg_error_pct") is None else round(100 - ff["avg_error_pct"], 1)}}


# ---------------------------------------------------------------- simulation
DEFAULTS = {"scope": None, "delay_days": 0, "capacity_pct": 100, "outage_days": 0, "lot_reject_pct": 0,
            "demand_pct": 0, "horizon_days": 60, "dual_source_pct": 0, "safety_boost_days": 0, "expedite_days": 0,
            "response_days": 5, "runs": 200, "plant_id": None, "seed": 7}


def _affected(scope, sup: dict) -> set:
    if not scope or scope == "NONE":
        return set()
    if scope == "ALL":
        return set(sup)
    if scope == "OVERSEAS":
        return {k for k, v in sup.items() if v["country"] != "India"}
    return {scope} if scope in sup else set()


def _simulate_pm(m, plant, mid, p, affected, scenario: bool, seed: int):
    """Simulate one plant/material flow. Returns (inventory trajectories, shortage matrix, D)."""
    rng = np.random.default_rng(seed)
    R, H, W = p["runs"], p["horizon_days"], WARMUP_DAYS
    D = m["demand"].get((plant, mid), 0.0)
    shares = m["shares"].get((plant, mid), {})
    st = m["stock"].get((plant, mid), {})
    inv = np.full(R, max(0.0, float(st.get("usable") or 0)))
    safety = float(st.get("safety") or 0)
    if D <= 0 or not shares:
        return np.repeat(inv[:, None], H, 1), np.zeros((R, H)), D, safety
    days = np.arange(-W, H)
    arrivals = np.zeros((R, H))
    react = p["response_days"]
    on = scenario
    aff = affected if on else set()
    unaffected = [s for s in shares if s not in aff]
    # Dual-sourcing (F3): the twin does NOT pick a supplier. Moved volume is spread over the suppliers that already
    # supply this material, pro-rata to their current share - the sourcing decision stays with procurement.
    unaff_share = sum(shares[u] for u in unaffected if m["dist"].get(u))
    demand_mult = 1 + p["demand_pct"] / 100 if on else 1.0

    for sid, w in sorted(shares.items()):
        dist = m["dist"].get(sid)
        if not dist:
            continue
        q = np.full(days.size, D * w / max(0.5, dist["mean_fill"]))
        if on:
            q = np.where(days >= react, q * demand_mult, q)              # planner follows demand after reacting
            if sid in aff and p["dual_source_pct"]:
                q = np.where(days >= react, q * (1 - p["dual_source_pct"] / 100), q)
            if sid in unaffected and p["dual_source_pct"] and aff and unaff_share > 0:
                moved = sum(D * shares[a] for a in aff if a in shares) * p["dual_source_pct"] / 100 * (w / unaff_share)
                q = np.where(days >= react, q + moved * demand_mult / max(0.5, dist["mean_fill"]), q)
            if p["safety_boost_days"] and (sid in unaffected or not unaffected):
                base = sum(shares[u] for u in (unaffected or shares))
                extra = D * p["safety_boost_days"] / 7 * (w / base) / max(0.5, dist["mean_fill"])
                q = np.where((days >= 0) & (days < 7), q + extra, q)
        lt = rng.choice(dist["lt"], size=(R, days.size))
        fill = rng.choice(dist["fill"], size=(R, days.size))
        reject = rng.random((R, days.size))
        arr = days[None, :] + np.rint(lt).astype(int)
        qty = q[None, :] * fill
        reject_p = BASE_LOT_REJECT
        if sid in aff:
            in_window = arr >= 0
            if p["delay_days"]:
                arr = np.where(in_window, arr + int(p["delay_days"]), arr)
            if p["outage_days"]:
                od = int(p["outage_days"])
                hit = (arr >= 0) & (arr < od)
                arr = np.where(hit, od + rng.integers(0, 4, size=arr.shape), arr)
                placed = (days >= 0) & (days < od)
                arr = np.where(placed[None, :], np.maximum(arr, od + np.rint(lt).astype(int)), arr)
            if p["capacity_pct"] < 100:
                qty = np.where(arr >= 0, qty * p["capacity_pct"] / 100, qty)
            reject_p = max(reject_p, p["lot_reject_pct"] / 100)
        elif on and p["expedite_days"]:
            arr = np.where(days[None, :] >= react, np.maximum(days[None, :] + 1, arr - int(p["expedite_days"])), arr)
        qty = np.where(reject < reject_p, 0.0, qty)
        ok = (arr >= 0) & (arr < H)
        r_idx = np.broadcast_to(np.arange(R)[:, None], arr.shape)
        np.add.at(arrivals, (r_idx[ok], arr[ok]), qty[ok])

    noise = rng.lognormal(mean=-0.0112, sigma=0.15, size=(R, H))
    dem = D * noise * demand_mult
    traj = np.empty((R, H))
    short = np.empty((R, H))
    for t in range(H):
        inv = inv + arrivals[:, t]
        cons = np.minimum(inv, dem[:, t])
        short[:, t] = dem[:, t] - cons
        inv = inv - cons
        traj[:, t] = inv
    return traj, short, D, safety


def simulate(params: dict) -> dict:
    p = {**DEFAULTS, **{k: v for k, v in (params or {}).items() if v is not None}}
    p["runs"] = int(min(500, max(50, p["runs"])))
    p["horizon_days"] = int(min(120, max(14, p["horizon_days"])))
    m = _model()
    c = m["cal"]
    affected = _affected(p["scope"], m["sup"])
    plants = sorted({k[0] for k in m["demand"]})
    if p["plant_id"]:
        plants = [x for x in plants if x == p["plant_id"]]
    H, R = p["horizon_days"], p["runs"]

    rows, traj_store = [], {}
    plant_lost = {pl: [np.zeros(R), np.zeros(R)] for pl in plants}          # [baseline, scenario] finished units lost
    plant_daily = {}
    for pl in plants:
        plant_daily[pl] = float(np.median([m["demand"].get((pl, mid), 0) for mid in m["mats"] if m["demand"].get((pl, mid))] or [0]))
        for i, mid in enumerate(sorted(m["mats"])):
            seed = p["seed"] * 1000 + i * 10 + plants.index(pl)
            tb, sb, D, safety = _simulate_pm(m, pl, mid, p, affected, False, seed)
            ts, ss, _, _ = _simulate_pm(m, pl, mid, p, affected, True, seed)
            if D <= 0:
                continue
            short_b, short_s = sb.sum(1), ss.sum(1)
            plant_lost[pl][0] = np.maximum(plant_lost[pl][0], short_b)        # 1 unit of each component per phone
            plant_lost[pl][1] = np.maximum(plant_lost[pl][1], short_s)
            # A stockout "event" = at least half a day of production lost over the horizon (filters daily noise);
            # its first day = first day the shortfall exceeds 5% of daily demand.
            ev_s, ev_b = short_s > 0.5 * D, short_b > 0.5 * D
            day_short = ss > 0.05 * D
            first = np.where(ev_s & day_short.any(1), day_short.argmax(1), np.nan)
            share_aff = sum(v for k, v in m["shares"].get((pl, mid), {}).items() if k in affected)
            row = {
                "plant_id": pl, "material_id": mid, "material_name": m["mats"][mid]["material_name"],
                "daily_demand": round(D), "start_usable": round(float(tb[:, 0].mean() + D)),
                "start_days_cover": round(float(m["stock"].get((pl, mid), {}).get("usable") or 0) / D, 1),
                "affected_share_pct": round(100 * share_aff, 1),
                "stockout_prob_pct": round(100 * float(ev_s.mean()), 1),
                "baseline_stockout_prob_pct": round(100 * float(ev_b.mean()), 1),
                "expected_short_units": round(float(short_s.mean())),
                "baseline_short_units": round(float(short_b.mean())),
                "first_stockout_day_p50": None if np.isnan(first).all() else round(float(np.nanmedian(first))),
                "below_safety_prob_pct": round(100 * float((ts < safety).any(1).mean()), 1) if safety else None,
                "min_inventory_p50": round(float(np.percentile(ts.min(1), 50))),
            }
            rows.append(row)
            traj_store[(pl, mid)] = (tb, ts, safety, D)

    rows.sort(key=lambda r: (-(r["stockout_prob_pct"] - r["baseline_stockout_prob_pct"]), -r["expected_short_units"]))
    top = rows[:4]
    trajectories = []
    for r in top:
        tb, ts, safety, D = traj_store[(r["plant_id"], r["material_id"])]
        trajectories.append({
            "plant_id": r["plant_id"], "material_id": r["material_id"], "material_name": r["material_name"],
            "safety_stock": safety, "daily_demand": round(D),
            "series": [{"day": t, "p10": round(float(np.percentile(ts[:, t], 10))), "p50": round(float(np.percentile(ts[:, t], 50))),
                        "p90": round(float(np.percentile(ts[:, t], 90))), "baseline_p50": round(float(np.percentile(tb[:, t], 50)))}
                       for t in range(H)],
        })

    bl = c["backlog"] or {}
    avg_price = float(bl.get("avg_price") or 0)
    tot_daily = sum(plant_daily.values()) or 1
    lines = c["lines"]
    plant_out = []
    for pl in plants:
        lb, ls = plant_lost[pl]
        fal = [l for l in lines if l["plant_id"] == pl and l["line_type"] == "Final Assembly"]
        cap = sum(l["nominal_capacity_per_hour"] for l in fal) or 300
        planned = plant_daily[pl] * H * (1 + (p["demand_pct"] / 100))
        lost_frac = float(np.median(ls)) / planned if planned else 0
        backlog_units = float(bl.get("units") or 0) * plant_daily[pl] / tot_daily
        backlog_value = float(bl.get("value") or 0) * plant_daily[pl] / tot_daily
        f_lines = []
        for f in c["factory"]:
            if f["plant_id"] == pl and f["scenario_name"] == "Baseline":
                f_lines.append({"line_id": f["line_id"], "line_type": f["line_type"], "model_version": f["model_version"],
                                "predicted_utilization_pct": f["predicted_utilization_pct"],
                                "supply_constrained_utilization_pct": round(f["predicted_utilization_pct"] * (1 - lost_frac), 1),
                                "predicted_output_units": f["predicted_output_units"],
                                "prediction_error_pct": f["prediction_error_pct"]})
        plant_out.append({
            "plant_id": pl, "daily_units_planned": round(plant_daily[pl]),
            "units_at_risk_p50": round(float(np.median(ls))), "units_at_risk_p90": round(float(np.percentile(ls, 90))),
            "baseline_units_at_risk_p50": round(float(np.median(lb))),
            "revenue_at_risk_inr": round(float(np.median(ls)) * avg_price),
            "baseline_revenue_at_risk_inr": round(float(np.median(lb)) * avg_price),
            "line_hours_lost_p50": round(float(np.median(ls)) / cap, 1),
            "output_loss_pct": round(100 * lost_frac, 1),
            "backlog_units": round(backlog_units), "backlog_value_inr": round(backlog_value),
            "backlog_at_risk_inr": round(backlog_value * min(1.0, float(np.median(ls)) / backlog_units)) if backlog_units else 0,
            "factory_twin_lines": f_lines,
        })

    def tot(k):
        return sum(x[k] for x in plant_out)

    at_risk = [r for r in rows if r["stockout_prob_pct"] >= 20]
    summary = {
        "affected_suppliers": sorted(affected),
        "materials_at_risk": len(at_risk),
        "baseline_materials_at_risk": len([r for r in rows if r["baseline_stockout_prob_pct"] >= 20]),
        "max_stockout_prob_pct": max((r["stockout_prob_pct"] for r in rows), default=0),
        "units_at_risk_p50": tot("units_at_risk_p50"), "baseline_units_at_risk_p50": tot("baseline_units_at_risk_p50"),
        "revenue_at_risk_inr": tot("revenue_at_risk_inr"), "baseline_revenue_at_risk_inr": tot("baseline_revenue_at_risk_inr"),
        "line_hours_lost_p50": round(tot("line_hours_lost_p50"), 1),
        "backlog_at_risk_inr": tot("backlog_at_risk_inr"),
        # earliest stockout among flows the SCENARIO makes materially worse (+10 pts vs baseline)
        "earliest_stockout_day": min((r["first_stockout_day_p50"] for r in rows if r["first_stockout_day_p50"] is not None
                                      and r["stockout_prob_pct"] >= 50
                                      and r["stockout_prob_pct"] - r["baseline_stockout_prob_pct"] >= 10), default=None),
    }
    summary["incremental_revenue_at_risk_inr"] = summary["revenue_at_risk_inr"] - summary["baseline_revenue_at_risk_inr"]
    return {"params": p, "summary": summary, "plants": plant_out, "materials": rows, "trajectories": trajectories,
            "assumptions": [
                "Daily usage per plant/material = average receipts over the last 90 days (steady-state calibration).",
                "Each finished phone uses one unit of each BOM component, so plant output lost = largest component shortfall.",
                "Replenishment follows each supplier's historical share; lead times and fill ratios are sampled from that "
                "supplier's received PO lines.",
                f"Customer orders are not linked to plants in the data; open backlog is split by plant share of demand.",
                f"{p['runs']} Monte Carlo runs, common random numbers for baseline vs scenario; planner reacts after "
                f"{p['response_days']} days.",
                "Decision support only: approve a scenario before acting on it.",
            ]}


PRESETS = [
    {"id": "outage14", "name": "Supplier outage, 14 days", "params": {"outage_days": 14}, "needs_supplier": True},
    {"id": "delay10", "name": "Shipping delay +10 days", "params": {"delay_days": 10}, "needs_supplier": True},
    {"id": "capacity50", "name": "Capacity drop to 50%", "params": {"capacity_pct": 50}, "needs_supplier": True},
    {"id": "quality", "name": "Quality excursion: 30% lots rejected", "params": {"lot_reject_pct": 30}, "needs_supplier": True},
    {"id": "port", "name": "Port congestion: overseas +7 days", "params": {"scope": "OVERSEAS", "delay_days": 7}},
    {"id": "demand15", "name": "Demand surge +15%", "params": {"scope": "NONE", "demand_pct": 15}},
    {"id": "mitigated", "name": "Outage 14 d + 50% dual-source + 5 d buffer",
     "params": {"outage_days": 14, "dual_source_pct": 50, "safety_boost_days": 5}, "needs_supplier": True},
]


def _cached_network():
    return network()


network_cached = A._cached(network)
fidelity_cached = A._cached(fidelity)
supplier_state_cached = A._cached(supplier_state)
