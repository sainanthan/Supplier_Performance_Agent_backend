"""Plant digital twin (T4): what a plant has, what is arriving, and which lines are exposed - at any day of the timeline.

Deterministic, from existing data only (no new KPI logic):
  stock        sp.fact_inventory_daily_snapshot at the selected date (latest snapshot on/before it)
               usable = on hand - allocated - blocked - quality hold (same definition as the what-if twin)
  daily usage  90-day average receipts per plant/material (consumption proxy - same as the Risk Radar what-if)
  days cover   usable / daily usage; status vs APPROVED config: critical < plant_cover_critical_days or below
               safety stock, watch < plant_cover_watch_days
  inbound      tracker shipments to the plant arriving in the next 24 h / 72 h (estimated, milestone or GPS position)
  lines        sp.dim_production_line; materials a line consumes via sp.line_material_category (documented mapping)
  exposure     a line is exposed when a material it consumes is critical, or an inbound shipment of that material is
               delayed / at risk AND the plant would run out before that shipment's ETA
  orders       open production orders at the plant that use the material (BOM) and the customer orders behind them
Every figure has a records query (records()) so the UI can show the exact source rows.
"""
from __future__ import annotations

import datetime as dt

from . import analytics as A

S = A.S


def _cfg() -> dict:
    c = A.get_scorecard_config()
    return {"critical": float(c.get("plant_cover_critical_days", 2)), "watch": float(c.get("plant_cover_watch_days", 5)),
            "buffer": float(c.get("lane_at_risk_buffer_days", 2))}


def _snap_date(plant_id: str, date: str | None) -> str | None:
    return A._run(f"""SELECT MAX(snapshot_date) FROM {S}.fact_inventory_daily_snapshot
                      WHERE plant_id = :p AND (CAST(:d AS date) IS NULL OR snapshot_date <= CAST(:d AS date))""",
                  {"p": plant_id, "d": date}, True)


def view(plant_id: str, date: str | None = None) -> dict:
    from . import tracker as TR
    from .features import is_on
    cfg = _cfg()
    plant = A._run(f"SELECT * FROM {S}.dim_plant WHERE plant_id = :p", {"p": plant_id})
    if not plant:
        raise LookupError("plant not found")
    snap = _snap_date(plant_id, date)
    the_day = dt.date.fromisoformat(str(date or snap or A.as_of_date()))
    stock = A._run(f"""
      SELECT i.material_id, m.material_name, m.material_category, m.standard_price_inr,
             SUM(i.on_hand_qty) AS on_hand, SUM(i.allocated_qty) AS allocated, SUM(i.blocked_qty) AS blocked,
             SUM(i.quality_hold_qty) AS quality_hold, SUM(i.in_transit_qty) AS in_transit_erp, MAX(i.safety_stock_qty) AS safety,
             SUM(i.on_hand_qty - i.allocated_qty - i.blocked_qty - i.quality_hold_qty) AS usable,
             jsonb_object_agg(i.warehouse_id || '|' || i.lot_id, i.on_hand_qty) AS by_lot
      FROM {S}.fact_inventory_daily_snapshot i JOIN {S}.dim_material m USING (material_id)
      WHERE i.plant_id = :p AND i.snapshot_date = :d
      GROUP BY i.material_id, m.material_name, m.material_category, m.standard_price_inr""", {"p": plant_id, "d": snap})
    wh = A._run(f"""SELECT warehouse_id, COUNT(DISTINCT material_id) AS materials, SUM(on_hand_qty) AS units,
                           SUM(on_hand_qty * unit_cost_inr) AS value_inr, SUM(quality_hold_qty) AS on_hold
                    FROM {S}.fact_inventory_daily_snapshot WHERE plant_id = :p AND snapshot_date = :d
                    GROUP BY warehouse_id ORDER BY warehouse_id""", {"p": plant_id, "d": snap})
    usage = {r["material_id"]: float(r["daily"] or 0) for r in A._run(f"""
      SELECT material_id, SUM(received_qty) / 90.0 AS daily FROM {S}.v_po_delivery
      WHERE plant_id = :p AND delivery_status = 'RECEIVED' AND NOT excluded
        AND receipt_date > CAST(:d AS date) - 90 AND receipt_date <= CAST(:d AS date)
      GROUP BY material_id""", {"p": plant_id, "d": the_day.isoformat()})}
    lines = A._run(f"""SELECT l.*, COALESCE(json_agg(lm.material_category) FILTER (WHERE lm.material_category IS NOT NULL), '[]') AS categories
                       FROM {S}.dim_production_line l LEFT JOIN {S}.line_material_category lm ON lm.line_type = l.line_type
                       WHERE l.plant_id = :p GROUP BY l.line_id, l.plant_id, l.line_type, l.nominal_capacity_per_hour, l.takt_time_sec, l.area""",
                   {"p": plant_id})
    downtime = {r["line_id"]: r for r in A._run(f"""
      SELECT line_id, SUM(duration_min) FILTER (WHERE reason_code = 'NO_MATERIAL') / 60.0 AS no_material_h,
             SUM(duration_min) / 60.0 AS all_h, COUNT(*) AS events
      FROM {S}.fact_downtime_event WHERE plant_id = :p AND start_ts::date > CAST(:d AS date) - 30 AND start_ts::date <= CAST(:d AS date)
      GROUP BY line_id""", {"p": plant_id, "d": the_day.isoformat()})}
    orders = A._run(f"""
      SELECT b.material_id, COUNT(DISTINCT o.production_order_id) AS prod_orders, SUM(o.order_qty * b.component_qty * (1 + b.scrap_factor)) AS req_qty,
             COUNT(DISTINCT o.customer_order_id) AS customer_orders
      FROM {S}.fact_production_order o JOIN {S}.fact_bom_component b ON b.product_id = o.product_id
      WHERE o.plant_id = :p AND o.order_status IN ('RELEASED', 'IN_PROGRESS')
      GROUP BY b.material_id""", {"p": plant_id})
    orders = {r["material_id"]: r for r in orders}

    # inbound from the tracker (same shipments / positions as the map)
    net = TR.tracker()
    start = dt.date.fromisoformat(net["start"])
    t0 = (the_day - start).days + 0.999                       # end of the selected day
    t_ref = (the_day - start).days                            # start of the selected day: ETA tomorrow = 1 day ahead
    lanes = {l["id"]: l for l in net["lanes"]}
    sup = {n["id"]: n for n in net["nodes"] if n["kind"] == "supplier"}
    inbound = []
    for s in net["shipments"]:
        if s["plant_id"] != plant_id or s["t_eta"] <= t0 or s["t_eta"] > t0 + 3:
            continue
        status = "late" if t0 > s["t_promised"] else "risk" if s["t_eta"] > s["t_promised"] - cfg["buffer"] else "ok"
        inbound.append({"id": s["id"], "po_id": s["po_id"], "supplier_id": s["supplier_id"], "supplier_name": sup.get(s["supplier_id"], {}).get("name"),
                        "material_id": s["material_id"], "material_name": net["materials"].get(s["material_id"]), "qty": s["qty"],
                        "eta": (start + dt.timedelta(days=s["t_eta"])).isoformat(), "days_ahead": round(s["t_eta"] - t_ref, 1),
                        "within_24h": s["t_eta"] <= t0 + 1, "status": status, "mode": lanes[s["lane"]]["mode"],
                        "pos_source": s.get("pos_source", "ESTIMATED"), "demo": bool(s.get("demo"))})
    # all open (not yet received) shipments per material: the next arrival decides whether the plant runs out first
    nxt = {}
    for s in net["shipments"]:
        if s["plant_id"] == plant_id and s["t_eta"] > t0:
            cur = nxt.get(s["material_id"])
            if not cur or s["t_eta"] < cur["t_eta"]:
                nxt[s["material_id"]] = s

    cat_lines = {}
    for l in lines:
        for c in l["categories"]:
            cat_lines.setdefault(c, []).append(l["line_id"])
    mats = []
    for r in stock:
        mid, d = r["material_id"], usage.get(r["material_id"], 0.0)
        usable = float(r["usable"] or 0)
        cover = usable / d if d > 0 else None
        below_safety = r["safety"] is not None and usable < float(r["safety"])
        status = "critical" if (cover is not None and cover < cfg["critical"]) or below_safety else "watch" if cover is not None and cover < cfg["watch"] else "ok"
        n = nxt.get(mid)
        eta_days = round(n["t_eta"] - t_ref, 2) if n else None
        runs_out_first = bool(cover is not None and eta_days is not None and cover < eta_days)
        o = orders.get(mid, {})
        mats.append({"material_id": mid, "material_name": r["material_name"], "category": r["material_category"],
                     "on_hand": r["on_hand"], "allocated": r["allocated"], "blocked": r["blocked"], "quality_hold": r["quality_hold"],
                     "usable": usable, "safety": r["safety"], "daily_usage": round(d, 1), "days_cover": None if cover is None else round(cover, 1),
                     "below_safety": below_safety, "status": status, "lines": cat_lines.get(r["material_category"], []),
                     "next_arrival": {"id": n["id"], "eta_days": eta_days, "qty": n["qty"], "supplier_id": n["supplier_id"],
                                      "late": t0 > n["t_promised"]} if n else None,
                     "runs_out_before_next_arrival": runs_out_first,
                     "open_prod_orders": o.get("prod_orders", 0), "required_qty": round(float(o.get("req_qty") or 0)), "customer_orders": o.get("customer_orders", 0),
                     "value_inr": round(usable * float(r["standard_price_inr"] or 0))})
    mats.sort(key=lambda x: (x["days_cover"] is None, x["days_cover"] if x["days_cover"] is not None else 1e9))

    line_out = []
    for l in lines:
        dep = [m for m in mats if l["line_id"] in m["lines"]]
        crit = [m for m in dep if m["status"] == "critical"]
        late_in = [m for m in dep if m["next_arrival"] and (m["next_arrival"]["late"] or m["runs_out_before_next_arrival"])]
        exposed = sorted({m["material_id"] for m in crit} | {m["material_id"] for m in late_in if m["runs_out_before_next_arrival"]})
        dt_ = downtime.get(l["line_id"], {})
        line_out.append({"line_id": l["line_id"], "line_type": l["line_type"], "capacity_per_hour": l["nominal_capacity_per_hour"],
                         "takt_time_sec": l["takt_time_sec"], "categories": l["categories"], "materials": [m["material_id"] for m in dep],
                         "status": "exposed" if exposed else "watch" if any(m["status"] == "watch" for m in dep) or late_in else "ok",
                         "exposed_materials": exposed, "min_cover_days": min((m["days_cover"] for m in dep if m["days_cover"] is not None), default=None),
                         "no_material_hours_30d": round(float(dt_.get("no_material_h") or 0), 1), "downtime_hours_30d": round(float(dt_.get("all_h") or 0), 1),
                         "open_prod_orders": max((m["open_prod_orders"] for m in dep), default=0),
                         "customer_orders": max((m["customer_orders"] for m in dep), default=0)})
    for x in inbound:                                       # lines that wait for this shipment
        m = next((m for m in mats if m["material_id"] == x["material_id"]), None)
        x["lines"] = m["lines"] if m else []
        x["material_days_cover"] = m["days_cover"] if m else None
        x["line_stop_risk"] = bool(m and m["runs_out_before_next_arrival"] and x["status"] != "ok")
    inbound.sort(key=lambda x: x["days_ahead"])
    out = {"plant": plant[0], "date": the_day.isoformat(), "snapshot_date": snap, "thresholds": cfg,
           "summary": {"materials": len(mats), "critical": sum(m["status"] == "critical" for m in mats), "watch": sum(m["status"] == "watch" for m in mats),
                       "arrivals_24h": sum(x["within_24h"] for x in inbound), "arrivals_72h": len(inbound),
                       "late_inbound": sum(x["status"] == "late" for x in inbound), "exposed_lines": sum(l["status"] == "exposed" for l in line_out),
                       "quality_hold_units": sum(float(m["quality_hold"] or 0) for m in mats),
                       "stock_value_inr": sum(m["value_inr"] for m in mats)},
           "warehouses": wh, "materials": mats, "lines": line_out, "inbound": inbound,
           "assumptions": ["Daily usage = 90-day average receipts at this plant (consumption proxy, same as the Risk Radar what-if).",
                           "Line-to-material mapping = sp.line_material_category (SMT: semiconductor, PCB, consumables; final assembly: the rest).",
                           "No dock-scheduling or incoming-inspection extract is loaded: docks show expected arrivals; inspection shows stock on quality hold.",
                           "Open production orders are not line-specific in the data: every open order uses both lines of the plant."],
           "incoming_inspection_source": "fact_incoming_inspection" if A._run(f"SELECT COUNT(*) FROM {S}.fact_incoming_inspection", {}, True) else "quality_hold (inventory snapshot)",
           "position_note": "Inbound ETAs follow the Network Tracker (estimated unless milestone / GPS data exists)."}
    if is_on("plant_twin"):
        out["run_id"] = A.log_run("plant_twin", {"plant_id": plant_id, "date": the_day.isoformat()}, mats)
    return out


RECORDS = {
    "stock": ("Inventory snapshot rows (sp.fact_inventory_daily_snapshot)",
              f"""SELECT snapshot_date, warehouse_id, lot_id, material_id, on_hand_qty, allocated_qty, blocked_qty, quality_hold_qty,
                         in_transit_qty, safety_stock_qty, inventory_status, unit_cost_inr
                  FROM {S}.fact_inventory_daily_snapshot WHERE plant_id = :p AND snapshot_date = :snap
                    AND (CAST(:m AS text) IS NULL OR material_id = :m) ORDER BY material_id, warehouse_id"""),
    "usage": ("Receipts used for daily usage (last 90 days, sp.v_po_delivery)",
              f"""SELECT po_id, po_line, supplier_id, material_id, receipt_date, received_qty FROM {S}.v_po_delivery
                  WHERE plant_id = :p AND delivery_status = 'RECEIVED' AND NOT excluded AND receipt_date > CAST(:d AS date) - 90
                    AND receipt_date <= CAST(:d AS date) AND (CAST(:m AS text) IS NULL OR material_id = :m) ORDER BY receipt_date DESC"""),
    "orders": ("Open production orders using the material (sp.fact_production_order x sp.fact_bom_component)",
               f"""SELECT o.production_order_id, o.product_id, o.customer_order_id, o.order_qty, b.material_id, b.component_qty, b.scrap_factor,
                          o.planned_start, o.due_date, o.priority, o.order_status
                   FROM {S}.fact_production_order o JOIN {S}.fact_bom_component b ON b.product_id = o.product_id
                   WHERE o.plant_id = :p AND o.order_status IN ('RELEASED', 'IN_PROGRESS') AND (CAST(:m AS text) IS NULL OR b.material_id = :m)
                   ORDER BY o.due_date"""),
    "downtime": ("Downtime events, last 30 days (sp.fact_downtime_event)",
                 f"""SELECT downtime_id, line_id, start_ts, duration_min, reason_code, impact_units FROM {S}.fact_downtime_event
                     WHERE plant_id = :p AND start_ts::date > CAST(:d AS date) - 30 AND start_ts::date <= CAST(:d AS date)
                       AND (CAST(:l AS text) IS NULL OR line_id = :l) ORDER BY start_ts DESC"""),
}


def records(plant_id: str, kind: str, date: str | None = None, material_id: str | None = None, line_id: str | None = None) -> dict:
    if kind not in RECORDS:
        raise KeyError(kind)
    snap = _snap_date(plant_id, date)
    the_day = str(date or snap or A.as_of_date())
    title, sql = RECORDS[kind]
    rows = A._run(sql, {"p": plant_id, "snap": snap, "d": the_day, "m": material_id, "l": line_id})
    return {"title": title, "rows": rows, "row_count": len(rows), "snapshot_date": snap, "date": the_day}
