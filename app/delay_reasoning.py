"""Why is this late? (flag delay_reasoning)

For one PO line: where the time was lost (each stage of the journey, planned vs actual), the root cause and contributing
factors with their evidence, the impact on our lines, and what to do. For a supplier or the portfolio: a Pareto of delay
causes. Sources, in order of trust:
  - sp.fact_delay_event  - stage plan vs actual + reason (carrier / broker / supplier feeds; demo rows are source=SYNTHETIC)
  - public.promise_changes - the supplier's own reason for a new date
  - public.disruption_events - weather / disaster events near the route while the cargo was moving
  - sp.v_supply_disruption - our production lines that stopped for lack of this supplier's material
  - predictive layer        - for lines not yet due: chance of lateness and its drivers
Only facts known at the data cut-off are used; a stage after the cut-off shows as pending (reported holds excepted).
Each result carries a ready-made `brief` (markdown) so the agent and the UI tell the same, cleanly structured story.
"""
from __future__ import annotations

import datetime as dt
import math

from . import analytics as A
from . import tracker as TR

S = A.S
STAGE = {"CONFIRMED": "Order confirmed", "READY": "Goods ready at supplier", "DISPATCHED": "Picked up from supplier",
         "GATE_IN": "Delivered to origin port", "DEPARTED": "Vessel departed", "ARRIVED": "Vessel arrived in India",
         "CUSTOMS": "Customs cleared", "IN_TRANSIT": "Highway checkpoint", "DELIVERED": "Delivered to plant"}
REASON = {  # code -> (short label, responsible party, category)
    "SUPPLIER_CAPACITY": ("Supplier capacity shortfall", "supplier", "SUPPLIER"),
    "RAW_MATERIAL_SHORTAGE": ("Raw-material shortage at the supplier", "supplier", "SUPPLIER"),
    "QUALITY_HOLD": ("Quality hold / rework at the supplier", "supplier", "SUPPLIER"),
    "LATE_DOCUMENTS": ("Late shipping documents", "supplier", "SUPPLIER"),
    "MISSED_VESSEL_CUTOFF": ("Missed vessel cut-off", "supplier", "SUPPLIER"),
    "PORT_CONGESTION": ("Port congestion", "carrier / forwarder", "LOGISTICS"),
    "TRANSSHIPMENT_DELAY": ("Transshipment delay", "carrier / forwarder", "LOGISTICS"),
    "WEATHER": ("Weather on the sea route", "external", "EXTERNAL"),
    "CUSTOMS_HOLD": ("Customs hold", "customs broker", "CUSTOMS"),
    "TRUCK_UNAVAILABLE": ("No truck available", "transporter", "LOGISTICS"),
    "ROAD_DISRUPTION": ("Road disruption", "external", "EXTERNAL"),
}
CATEGORY = {"SUPPLIER": "Supplier-side", "LOGISTICS": "Logistics", "CUSTOMS": "Customs", "EXTERNAL": "External (weather / road)"}
ACTIONS = {
    "SUPPLIER_CAPACITY": ["Ask {sup} for a capacity recovery plan and weekly confirmation of promise dates",
                          "Reserve capacity with a rolling forecast or blanket order", "Qualify a second source for {mat} if the delays continue"],
    "RAW_MATERIAL_SHORTAGE": ["Ask {sup} for its sub-supplier status and a recovery date", "Raise safety stock of {mat} until supply is stable",
                              "Approve an alternate raw-material source with the supplier"],
    "QUALITY_HOLD": ["Request an 8D on the outgoing-inspection failure", "Agree pre-shipment inspection for the next lots of {mat}"],
    "LATE_DOCUMENTS": ["Agree that documents reach the forwarder 3 working days before the vessel cut-off",
                       "Share a document checklist with {sup} and the forwarder"],
    "MISSED_VESSEL_CUTOFF": ["Share the sailing and cut-off calendar with {sup}", "Book the next sailing in advance and ask the forwarder for priority loading"],
    "PORT_CONGESTION": ["Add buffer days to this lane's planned lead time", "Ask the forwarder about an alternate port or a direct service"],
    "TRANSSHIPMENT_DELAY": ["Ask the forwarder for a direct service or a more reliable hub", "Add buffer days on this lane"],
    "WEATHER": ["Add seasonal buffer days on this lane in monsoon / cyclone season", "Pre-build stock of {mat} before the season"],
    "CUSTOMS_HOLD": ["Have the customs broker pre-check invoice value and HS codes before arrival",
                     "Ask {sup} to correct its commercial-invoice template", "Use pre-arrival filing to speed up clearance"],
    "TRUCK_UNAVAILABLE": ["Book trucks 48 hours ahead with the transporter", "Add a second transporter on this lane"],
    "ROAD_DISRUPTION": ["Plan an alternate road route", "Add one buffer day on this lane"],
}
# wording for a delay still in progress at the cut-off (no final duration is known yet)
ONGOING = {
    "SUPPLIER_CAPACITY": "Production is running late at {loc}: the supplier's line is booked with other customers' orders",
    "RAW_MATERIAL_SHORTAGE": "Production has not started at {loc}: a sub-supplier has not delivered raw material",
    "QUALITY_HOLD": "The lot failed the supplier's outgoing inspection and is being reworked and re-tested",
    "LATE_DOCUMENTS": "Shipping documents (invoice, packing list, certificate of origin) have not been issued yet",
    "MISSED_VESSEL_CUTOFF": "The cargo missed the vessel cut-off at {loc} and is waiting for the next sailing",
    "PORT_CONGESTION": "Berth congestion at {loc}: the vessel is waiting to unload",
    "TRANSSHIPMENT_DELAY": "The connecting vessel at the transshipment hub is late and the cargo is waiting",
    "WEATHER": "Rough weather on the sea route is delaying the voyage into {loc}",
    "CUSTOMS_HOLD": "Customs at {loc} is holding the shipment: an invoice value / HS-code mismatch needs correction",
    "TRUCK_UNAVAILABLE": "No truck is available at {loc} yet",
    "ROAD_DISRUPTION": "A road closure near {loc} is delaying the truck",
}
WHERE = {"CONFIRMED": "at order confirmation", "READY": "in production at the supplier", "DISPATCHED": "at pickup from the supplier",
         "GATE_IN": "at the origin port gate", "DEPARTED": "at vessel departure", "ARRIVED": "on the voyage to India", "CUSTOMS": "in customs",
         "IN_TRANSIT": "on the road", "DELIVERED": "on the last mile"}
NOTE = "Journey events are demo logistics data (synthetic) until carrier, customs and supplier feeds are connected."


def _d(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])


def _days(n) -> str:
    n = float(n); return f"{n:g} day" + ("" if n == 1 else "s")


def _poss(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def _fd(d) -> str:
    return d.strftime("%d %b") if d else "–"


def _inr(v: float) -> str:
    v = float(v or 0)
    return f"₹{v / 1e7:.2f} Cr" if v >= 1e7 else f"₹{v / 1e5:.1f} L" if v >= 1e5 else f"₹{v:,.0f}"


def _km(a, b) -> float:
    r = math.pi / 180
    return 2 * 6371 * math.asin(math.sqrt(math.sin((b[0] - a[0]) * r / 2) ** 2 + math.cos(a[0] * r) * math.cos(b[0] * r) * math.sin((b[1] - a[1]) * r / 2) ** 2))


def _lane(sid: str, plant: str) -> dict:
    return next((l for l in TR.network(True, True)["lanes"] if l["id"] == f"{sid}>{plant}"), {})


# ---------------------------------------------------------------- one PO line
def explain(po_id: str, po_line: int | None = None) -> dict:
    po_id = po_id.strip().upper()
    rows = A.query(f"""SELECT po_id, po_line, supplier_id, supplier_name, material_id, material_name, plant_id, po_date, promised_date,
                              receipt_date, delivery_status, ordered_qty, unit_price_inr, incoterm,
                              COALESCE(standard_lead_time_days, promised_lead_time_days) AS std_lt, promised_lead_time_days
                       FROM {S}.v_po_delivery WHERE po_id = :p {"AND po_line = :l" if po_line else ""} ORDER BY po_line""",
                   p=po_id, **({"l": int(po_line)} if po_line else {}))
    if not rows:
        raise KeyError(f"PO {po_id}{'/' + str(po_line) if po_line else ''} not found")
    r = rows[0]
    as_of = _d(A.as_of_date())
    prom, rec, po_d = _d(r["promised_date"]), _d(r["receipt_date"]), _d(r["po_date"])
    received = rec is not None and rec <= as_of
    ev = A.query(f"""SELECT stage_seq, stage, location, planned_ts, actual_ts, added_delay_days, reason_code, reason_text, reported_by, source
                     FROM {S}.fact_delay_event WHERE po_id = :p AND po_line = :l ORDER BY stage_seq""", p=r["po_id"], l=r["po_line"])
    plant = next((n["name"] for n in TR.network(True, True)["nodes"] if n["id"] == r["plant_id"]), r["plant_id"]).split(" (")[0]
    sup, mat = r["supplier_name"], r["material_name"]
    value = float(r["ordered_qty"] or 0) * float(r["unit_price_inr"] or 0)

    # ---- status
    pred = None
    if not received:
        try:
            from .features import is_on
            if is_on("predictive_layer"):
                from . import predictive as PR
                pred = next((i for i in PR.score()["items"] if i["po_id"] == r["po_id"] and i["po_line"] == r["po_line"]), None)
        except Exception:
            pred = None
    if received:
        status, days_late = ("DELIVERED_LATE" if rec > prom else "ON_TIME"), (rec - prom).days
    elif prom < as_of:
        status, days_late = "OVERDUE", (as_of - prom).days
    else:
        status, days_late = "OPEN", None

    # ---- timeline (only what is known at the cut-off; a hold already reported by the carrier / broker is known)
    timeline, lost = [], []
    for e in ev:
        pl, ac = _d(e["planned_ts"]), _d(e["actual_ts"])
        known = ac is not None and ac <= as_of
        add = float(e["added_delay_days"] or 0)
        why = e["reason_text"]
        if e["reason_code"] and not known and not (why or "").startswith("Supplier stated"):
            why = ONGOING.get(e["reason_code"], why or "").format(loc=e["location"]) + (f" (reported by the {str(e['reported_by']).lower()})" if e["reported_by"] else "")
        row = {"stage": e["stage"], "label": STAGE.get(e["stage"], e["stage"]), "location": e["location"], "planned": pl and pl.isoformat(),
               "actual": ac.isoformat() if known else None, "slip_days": (ac - pl).days if known else None, "added_days": add if known else None,
               "reason_code": e["reason_code"], "reason": why, "reported_by": e["reported_by"], "pending": not known}
        if not known and pl and pl < as_of and status in ("OVERDUE", "OPEN"):
            row["waiting_days"] = (as_of - pl).days                    # should have happened already
        timeline.append(row)
        if e["reason_code"] and (known or (pl and pl <= as_of)):
            lost.append({"stage": row["label"], "stage_key": e["stage"], "location": e["location"], "days": add if known else row.get("waiting_days") or add,
                         "code": e["reason_code"], "reason": why, "reported_by": e["reported_by"], "ongoing": not known})
    lost.sort(key=lambda x: -x["days"])
    total_lost = sum(x["days"] for x in lost) or 0
    for x in lost:
        x["share_pct"] = round(100 * x["days"] / total_lost) if total_lost else None

    # ---- root cause + contributing factors
    root = None
    if lost:
        top = lost[0]; lbl, who, cat = REASON.get(top["code"], (top["code"], "", ""))
        root = {"code": top["code"], "label": lbl, "responsible": who, "category": CATEGORY.get(cat, cat), "stage": top["stage"], "where": WHERE.get(top["stage_key"], ""),
                "location": top["location"], "days": top["days"], "share_pct": top["share_pct"], "evidence": top["reason"],
                "reported_by": top["reported_by"], "ongoing": top["ongoing"]}
    factors = []
    for x in lost[1:3]:
        lbl, who, _ = REASON.get(x["code"], (x["code"], "", ""))
        factors.append({"type": "SECONDARY", "title": lbl, "responsible": who,
                        "detail": f"{x['reason']} ({'+' + _days(x['days']) if not x['ongoing'] else _days(x['days']) + ' so far'}, {WHERE.get(x['stage_key'], '')})"})
    lane = _lane(r["supplier_id"], r["plant_id"])
    route_days = round(sum(s.get("days") or 0 for s in lane.get("segments") or []) + (5 if lane.get("mode") == "SEA" else 1))
    given = (prom - po_d).days if prom and po_d else None
    if given is not None and route_days and given < route_days:
        factors.append({"type": "PLAN", "title": "The promise was not achievable",
                        "detail": f"The route alone needs about {route_days} days (transport plus port and customs time), but the promise allowed {given} days from the order date"})
    elif r["std_lt"] and given is not None and given < float(r["std_lt"]):
        factors.append({"type": "PLAN", "title": "Promise tighter than standard",
                        "detail": f"Promised in {given} days against the supplier's standard lead time of {int(r['std_lt'])} days"})
    # disruptions near the route while the cargo was moving
    pts = [(s["lat"], s["lon"]) for s in lane.get("stops") or []]
    start = _d(ev[0]["planned_ts"]) if ev else po_d
    end = rec if received else as_of
    near = []
    for d in A.query("SELECT type, title, severity, lat, lon, radius_km, starts, ends FROM public.disruption_events"):
        st, en = _d(d["starts"]), _d(d["ends"]) or as_of
        if (st and st > end) or (en and en < start):
            continue
        dist = min((_km((d["lat"], d["lon"]), p) for p in pts), default=1e9)
        if dist <= float(d["radius_km"] or 0) + 150 and str(d["severity"]).upper() in ("CRITICAL", "HIGH", "MEDIUM"):   # close and material only
            near.append({"type": d["type"], "title": d["title"], "severity": d["severity"], "km": round(dist)})
    if near:
        n0 = sorted(near, key=lambda x: x["km"])[0]
        factors.append({"type": "DISRUPTION", "title": f"{n0['type'].title()} near the route",
                        "detail": f"{n0['title'] or n0['type']} ({str(n0['severity']).lower()}), {n0['km']} km from the route while the cargo was moving"})
    # supplier's own reason
    said = A.query("SELECT reason, new_promise, status FROM public.promise_changes WHERE po_id = :p AND po_line = :l ORDER BY id DESC LIMIT 1",
                   p=r["po_id"], l=r["po_line"])
    if said:
        factors.append({"type": "SUPPLIER_STATED", "title": "Supplier's own explanation",
                        "detail": f"\"{said[0]['reason']}\" (new date {_fd(_d(said[0]['new_promise']))}, {str(said[0]['status']).lower()})"})
    # repeat pattern
    pattern = None
    if root:
        hist = A.query(f"""WITH late AS (
              SELECT e.po_id, e.po_line, (array_agg(e.reason_code ORDER BY e.added_delay_days DESC))[1] AS code
              FROM {S}.fact_delay_event e JOIN {S}.v_po_delivery v USING (po_id, po_line)
              WHERE e.supplier_id = :s AND e.reason_code IS NOT NULL AND v.promised_date BETWEEN CAST(:a AS date) - 120 AND CAST(:a AS date)
              GROUP BY e.po_id, e.po_line)
            SELECT code, count(*) n FROM late GROUP BY code ORDER BY n DESC""", s=r["supplier_id"], a=as_of.isoformat())
        n_late = sum(h["n"] for h in hist); same = next((h["n"] for h in hist if h["code"] == root["code"]), 0)
        if n_late and same >= 3 and same / n_late >= 0.2:
            pattern = {"same": same, "late": n_late, "code": root["code"]}
            factors.append({"type": "PATTERN", "title": "Repeat pattern",
                            "detail": f"{same} of {_poss(sup)} {n_late} late deliveries in the last 120 days had the same cause ({root['label'].lower()})"})
        elif n_late >= 5 and hist[0]["code"] != root["code"]:
            dom = REASON.get(hist[0]["code"], (hist[0]["code"],))[0]
            pattern = {"same": hist[0]["n"], "late": n_late, "code": hist[0]["code"]}
            factors.append({"type": "PATTERN", "title": "Unusual for this supplier",
                            "detail": f"{_poss(sup)} delays usually come from {dom.lower()} ({hist[0]['n']} of {n_late} late deliveries in 120 days); this one is different"})
    if status == "OPEN" and pred:
        has_plan = any(f["type"] == "PLAN" for f in factors)
        for dv in pred.get("drivers", [])[:3]:
            if dv["effect"] == "raises" and not (has_plan and dv.get("factor") == "lead_time"):   # already covered by the plan factor
                factors.append({"type": "PREDICTION", "title": "Risk signal", "detail": dv["text"][:1].upper() + dv["text"][1:]})

    # ---- impact: our lines that stopped for lack of this supplier's material while it was late
    impact = {"value_inr": round(value), "downtime_hours": 0.0, "lines": []}
    if status in ("DELIVERED_LATE", "OVERDUE"):
        dt_rows = A.query(f"""SELECT line_id, ROUND(SUM(duration_min * attribution_weight)::numeric / 60, 1) h
                              FROM {S}.v_supply_disruption WHERE supplier_id = :s AND plant_id = :pl
                                AND start_ts::date BETWEEN CAST(:a AS date) AND CAST(:b AS date) GROUP BY 1 ORDER BY 2 DESC""",
                          s=r["supplier_id"], pl=r["plant_id"], a=prom.isoformat(), b=(rec if received else as_of).isoformat())
        impact["downtime_hours"] = float(sum(float(x["h"] or 0) for x in dt_rows))
        impact["lines"] = [x["line_id"] for x in dt_rows if float(x["h"] or 0) > 0]

    # ---- actions
    acts = []
    if status == "OVERDUE":
        acts.append(f"Expedite now: confirm a firm delivery date with {sup} and check line coverage for {mat}")
    if root:
        acts += [a.format(sup=sup, mat=mat) for a in ACTIONS.get(root["code"], [])]
    if any(f["type"] == "PLAN" for f in factors):
        acts.append("Set future promise dates from the route's real transit time (plus buffer), not the requested date")
    if status == "OPEN" and not root:
        acts.append(f"Ask {sup} to confirm the {_fd(prom)} date now and flag any risk early")

    out = {"po_id": r["po_id"], "po_line": r["po_line"], "supplier_id": r["supplier_id"], "supplier_name": sup, "material_id": r["material_id"],
           "material_name": mat, "plant_id": r["plant_id"], "plant": plant, "mode": lane.get("mode"), "as_of": as_of.isoformat(),
           "status": status, "promised": prom.isoformat(), "received": rec.isoformat() if received else None, "days_late": days_late,
           "prediction": {k: pred[k] for k in ("p_late", "expected_delay_days", "predicted_arrival", "risk_band")} if pred else None,
           "timeline": timeline, "where_lost": lost, "root_cause": root, "factors": factors, "pattern": pattern, "impact": impact,
           "actions": acts[:5], "source": "SYNTHETIC" if any(e["source"] == "SYNTHETIC" for e in ev) else ("FEED" if ev else "NONE"),
           "note": NOTE if any(e["source"] == "SYNTHETIC" for e in ev) else None}
    out["brief"] = _brief(out)
    return out


def _brief(x: dict) -> str:
    """The explanation as clean markdown: headline, verdict, where the time was lost, why, impact, what to do."""
    L = [f"**PO {x['po_id']}/{x['po_line']} · {x['material_name']} · {x['supplier_name']} → {x['plant']}**", ""]
    rc = x["root_cause"]
    if x["status"] == "DELIVERED_LATE":
        v = f"Delivered **{x['days_late']} days late** (promised {_fd(_d(x['promised']))}, received {_fd(_d(x['received']))})."
    elif x["status"] == "OVERDUE":
        v = f"**{x['days_late']} days overdue** and not yet received (promised {_fd(_d(x['promised']))})."
        if x["prediction"]:
            v += f" Expected about {x['prediction']['expected_delay_days']:g} days late in total."
    elif x["status"] == "ON_TIME":
        v = f"Delivered **on time** (promised {_fd(_d(x['promised']))}, received {_fd(_d(x['received']))}). There is no delay to explain."
    else:
        p = x["prediction"]
        v = f"Not due yet (promised {_fd(_d(x['promised']))})." + (f" **{round(p['p_late'] * 100)}% chance of arriving late**, about {p['expected_delay_days']:g} days." if p else "")
    if rc:
        v += f" Main cause: **{rc['label'].lower()}** {rc['where']}" + (f" ({rc['share_pct']}% of the time lost)." if rc["share_pct"] is not None else ".")
    L += [f"**Verdict:** {v}", ""]
    if x["where_lost"]:
        L += ["#### Where the time was lost", "| Step | Location | Planned | Actual | Time lost |", "|---|---|---|---|---|"]
        for t in x["timeline"]:
            if t["reason_code"] or t["stage"] == "DELIVERED":
                act = _fd(_d(t["actual"])) if t["actual"] else ("waiting" if t.get("waiting_days") else "pending")
                lost = f"+{_days(t['added_days'])}" if t["added_days"] else (f"{_days(t['waiting_days'])} so far" if t.get("waiting_days") and t["reason_code"] else "–")
                L.append(f"| {t['label']} | {t['location']} | {_fd(_d(t['planned']))} | {act} | {lost} |")
        L.append("")
    if rc or x["factors"]:
        L.append("#### Why it happened")
        n = 1
        if rc:
            L.append(f"{n}. **Root cause: {rc['label']}** ({rc['responsible']}). {rc['evidence']}" + (f", {_days(rc['days'])} so far. *Still ongoing.*" if rc["ongoing"] else "."))
            n += 1
        for f in x["factors"]:
            L.append(f"{n}. **{f['title']}.** {f['detail']}."); n += 1
        L.append("")
    imp = x["impact"]
    if x["status"] != "ON_TIME":
        L.append("#### Impact")
        L.append(f"- Order value: {_inr(imp['value_inr'])}")
        if imp["downtime_hours"]:
            L.append(f"- Our production stopped **{imp['downtime_hours']:g} hours** waiting for {_poss(x['supplier_name'])} material during the delay (lines {', '.join(imp['lines'])})")
        else:
            L.append("- No production stoppage recorded for this supplier during the delay")
        L.append("")
    if x["actions"]:
        L.append("#### What to do")
        L += [f"{i}. {a}" for i, a in enumerate(x["actions"], 1)]
        L.append("")
    if x["note"]:
        L.append(f"*{x['note']}*")
    return "\n".join(L).strip()


# ---------------------------------------------------------------- causes across lines (Pareto)
def causes(supplier_id: str | None = None, days: int = 120) -> dict:
    """Root cause of every late line (largest slip) for a supplier or the whole portfolio, as a Pareto."""
    as_of = _d(A.as_of_date())
    rows = A.query(f"""WITH late AS (
          SELECT e.po_id, e.po_line, e.supplier_id,
                 (array_agg(e.reason_code ORDER BY e.added_delay_days DESC))[1] AS code, SUM(e.added_delay_days) AS days,
                 (array_agg(e.stage ORDER BY e.added_delay_days DESC))[1] AS stage
          FROM {S}.fact_delay_event e JOIN {S}.v_po_delivery v USING (po_id, po_line)
          WHERE e.reason_code IS NOT NULL AND v.promised_date BETWEEN CAST(:a AS date) - :d AND CAST(:a AS date)
            {"AND e.supplier_id = :s" if supplier_id else ""}
          GROUP BY e.po_id, e.po_line, e.supplier_id)
        SELECT l.code, count(*) lines, ROUND(SUM(l.days), 0) delay_days, l.stage, s.supplier_name
        FROM late l JOIN {S}.dim_supplier s USING (supplier_id) GROUP BY l.code, l.stage, s.supplier_name""",
                   a=as_of.isoformat(), d=days, **({"s": supplier_id} if supplier_id else {}))
    by, tot_l, tot_d = {}, 0, 0.0
    for r in rows:
        b = by.setdefault(r["code"], {"code": r["code"], "lines": 0, "delay_days": 0.0, "suppliers": {}})
        b["lines"] += int(r["lines"]); b["delay_days"] += float(r["delay_days"] or 0)
        b["suppliers"][r["supplier_name"]] = b["suppliers"].get(r["supplier_name"], 0) + int(r["lines"])
        tot_l += int(r["lines"]); tot_d += float(r["delay_days"] or 0)
    items, cum = [], 0
    for b in sorted(by.values(), key=lambda x: -x["delay_days"]):
        lbl, who, cat = REASON.get(b["code"], (b["code"], "", ""))
        cum += b["delay_days"]
        top = max(b["suppliers"].items(), key=lambda kv: kv[1])[0] if b["suppliers"] else None
        items.append({"code": b["code"], "label": lbl, "responsible": who, "category": CATEGORY.get(cat, cat), "lines": b["lines"],
                      "delay_days": round(b["delay_days"]), "share_pct": round(100 * b["delay_days"] / tot_d) if tot_d else 0,
                      "cum_pct": round(100 * cum / tot_d) if tot_d else 0, "top_supplier": top})
    cats = {}
    for i in items:
        cats[i["category"]] = cats.get(i["category"], 0) + i["delay_days"]
    split = [{"category": k, "delay_days": round(v), "share_pct": round(100 * v / tot_d) if tot_d else 0} for k, v in sorted(cats.items(), key=lambda kv: -kv[1])]
    name = A.query(f"SELECT supplier_name FROM {S}.dim_supplier WHERE supplier_id = :s", s=supplier_id)[0]["supplier_name"] if supplier_id else "all suppliers"
    out = {"scope": name, "supplier_id": supplier_id, "days": days, "as_of": as_of.isoformat(), "late_lines": tot_l, "delay_days": round(tot_d),
           "causes": items, "by_category": split, "note": NOTE if rows else None}
    out["brief"] = _causes_brief(out)
    return out


def _causes_brief(c: dict) -> str:
    if not c["causes"]:
        return f"No late deliveries with a recorded cause for {c['scope']} in the last {c['days']} days."
    top = c["causes"][0]
    L = [f"**Why deliveries from {c['scope']} are late (last {c['days']} days)**", "",
         f"**Verdict:** {c['late_lines']} late PO lines lost {c['delay_days']} days in total. The biggest cause is **{top['label'].lower()}** "
         f"({top['share_pct']}% of the time lost, {top['responsible']}).", "",
         "#### Causes, largest first", "| Cause | Responsible | Late lines | Days lost | Share |", "|---|---|---|---|---|"]
    for i in c["causes"][:8]:
        L.append(f"| {i['label']} | {i['responsible']} | {i['lines']} | {i['delay_days']} | {i['share_pct']}% |")
    L += ["", "#### Who owns the delay"] + [f"- {s['category']}: **{s['share_pct']}%** of days lost" for s in c["by_category"]]
    acts = ACTIONS.get(top["code"], [])[:2]
    if acts:
        L += ["", "#### What to do first"] + [f"{k}. {a.format(sup='the supplier' if not c['supplier_id'] else c['scope'], mat='the affected materials')}" for k, a in enumerate(acts, 1)]
    if c["note"]:
        L += ["", f"*{c['note']}*"]
    return "\n".join(L)
