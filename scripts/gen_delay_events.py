"""Demo logistics journey per PO line, for "Why is this late?" (flag delay_reasoning).

The data has PO, promise and receipt dates but no carrier / TMS / supplier milestone feed, so it cannot say WHERE a late
delivery lost its time or WHY. This script writes a plausible stage-by-stage journey for every PO line into
sp.fact_delay_event, every row tagged source='SYNTHETIC':

  - planned dates are worked back from the promise date along the line's real route (road / sea legs, origin and Indian ports);
  - actual dates add the line's REAL total delay (receipt - promise, from the data) at the stages where it was lost;
  - each slip gets a reason from the supplier's delay profile (e.g. SolderPro: capacity, Global Pack: truck availability),
    so the per-supplier cause pattern is meaningful; a supplier's own reason from an approved promise change wins.
Deterministic (seeded by PO) - rerunning gives the same story. KPIs, the map and the tracker never read this table.

    python -m scripts.gen_delay_events --load     # (re)write all SYNTHETIC rows
    python -m scripts.gen_delay_events --clear    # remove them
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import text  # noqa: E402

from app import analytics as A  # noqa: E402
from app import tracker as TR  # noqa: E402
from app.db import engine  # noqa: E402

# reason catalogue: stage it hits, who is responsible, which modes it applies to
REASONS = {
    "SUPPLIER_CAPACITY":     ("READY", "SUPPLIER", "SEA ROAD"),
    "RAW_MATERIAL_SHORTAGE": ("READY", "SUPPLIER", "SEA ROAD"),
    "QUALITY_HOLD":          ("READY", "SUPPLIER", "SEA ROAD"),
    "LATE_DOCUMENTS":        ("GATE_IN", "SUPPLIER", "SEA"),
    "MISSED_VESSEL_CUTOFF":  ("DEPARTED", "SUPPLIER", "SEA"),
    "PORT_CONGESTION":       ("ARRIVED", "LOGISTICS", "SEA"),
    "TRANSSHIPMENT_DELAY":   ("ARRIVED", "LOGISTICS", "SEA"),
    "WEATHER":               ("ARRIVED", "EXTERNAL", "SEA"),
    "CUSTOMS_HOLD":          ("CUSTOMS", "CUSTOMS", "SEA"),
    "TRUCK_UNAVAILABLE":     ("DISPATCHED", "LOGISTICS", "SEA ROAD"),
    "ROAD_DISRUPTION":       ("IN_TRANSIT", "EXTERNAL", "ROAD"),
}
# each supplier's typical causes (weights); filtered by the lane's mode
PROFILE = {
    "SUP-001": {"RAW_MATERIAL_SHORTAGE": 4, "PORT_CONGESTION": 2, "CUSTOMS_HOLD": 2, "SUPPLIER_CAPACITY": 1, "TRUCK_UNAVAILABLE": 1},
    "SUP-002": {"SUPPLIER_CAPACITY": 3, "MISSED_VESSEL_CUTOFF": 3, "CUSTOMS_HOLD": 2, "WEATHER": 1},
    "SUP-003": {"QUALITY_HOLD": 3, "TRANSSHIPMENT_DELAY": 2, "CUSTOMS_HOLD": 2, "LATE_DOCUMENTS": 1},
    "SUP-004": {"SUPPLIER_CAPACITY": 3, "TRUCK_UNAVAILABLE": 3, "ROAD_DISRUPTION": 2, "QUALITY_HOLD": 1},
    "SUP-005": {"QUALITY_HOLD": 4, "LATE_DOCUMENTS": 3, "CUSTOMS_HOLD": 1, "TRUCK_UNAVAILABLE": 1},
    "SUP-006": {"RAW_MATERIAL_SHORTAGE": 4, "PORT_CONGESTION": 2, "MISSED_VESSEL_CUTOFF": 1, "CUSTOMS_HOLD": 1},
    "SUP-007": {"SUPPLIER_CAPACITY": 4, "QUALITY_HOLD": 2, "TRUCK_UNAVAILABLE": 1, "TRANSSHIPMENT_DELAY": 1},
    "SUP-008": {"TRUCK_UNAVAILABLE": 4, "LATE_DOCUMENTS": 2, "SUPPLIER_CAPACITY": 2, "ROAD_DISRUPTION": 1},
    "SUP-009": {"MISSED_VESSEL_CUTOFF": 3, "WEATHER": 2, "PORT_CONGESTION": 2, "CUSTOMS_HOLD": 1},
    "SUP-010": {"SUPPLIER_CAPACITY": 5, "RAW_MATERIAL_SHORTAGE": 2, "CUSTOMS_HOLD": 1, "TRUCK_UNAVAILABLE": 1},
}
TEXT = {
    "SUPPLIER_CAPACITY": "Production finished {d} days late: the supplier's line was booked with other customers' orders",
    "RAW_MATERIAL_SHORTAGE": "Production started {d} days late: a sub-supplier could not deliver raw material on time",
    "QUALITY_HOLD": "Lot failed the supplier's outgoing inspection; reworked and re-tested for {d} days",
    "LATE_DOCUMENTS": "Shipping documents (invoice, packing list, certificate of origin) were issued {d} days late at {loc}",
    "MISSED_VESSEL_CUTOFF": "Cargo missed the vessel cut-off at {loc} and was rolled to the next sailing ({d} days later)",
    "PORT_CONGESTION": "Berth congestion at {loc}: the vessel waited {d} days to unload",
    "TRANSSHIPMENT_DELAY": "Connecting vessel at the transshipment hub was late; cargo waited {d} days before {loc}",
    "WEATHER": "Rough weather on the sea route delayed the voyage into {loc} by {d} days",
    "CUSTOMS_HOLD": "Customs at {loc} held the shipment {d} days: invoice value / HS-code mismatch needed correction",
    "TRUCK_UNAVAILABLE": "No truck available at {loc}; pickup slipped {d} days",
    "ROAD_DISRUPTION": "Road closure on the highway near {loc} delayed the truck {d} days",
}
REPORTER = {"SUPPLIER": "SUPPLIER", "LOGISTICS": "CARRIER", "CUSTOMS": "BROKER", "EXTERNAL": "CARRIER"}


def _d(v):
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])


def journey(r: dict, lane: dict) -> list[dict]:
    """Planned stages (seq, key, label, location, planned date) worked back from the promise date along the route."""
    p, po = _d(r["promised_date"]), _d(r["po_date"])
    segs, stops = lane.get("segments") or [], lane.get("stops") or []
    name = {s["kind"]: s["name"] for s in stops}
    sup_site, plant = stops[0]["name"] if stops else "Supplier", stops[-1]["name"] if stops else r["plant_id"]
    sea = lane.get("mode") == "SEA" and len(segs) == 3
    if sea:
        ports = [s["name"] for s in stops if s["kind"] == "port"]
        o_port, e_port = (ports + ["Origin port", "Entry port"])[:2] if len(ports) >= 2 else ("Origin port", "Entry port")
        r1, sd, r2 = segs[0]["days"], segs[1]["days"], segs[2]["days"]
        delivered = p
        customs = p - dt.timedelta(days=round(r2 + 0.5))
        arrived = customs - dt.timedelta(days=2)
        departed = arrived - dt.timedelta(days=round(sd))
        gate = departed - dt.timedelta(days=2)
        dispatched = gate - dt.timedelta(days=max(1, round(r1)))
        ready = dispatched - dt.timedelta(days=1)
        st = [("READY", "Goods ready at supplier", sup_site, ready), ("DISPATCHED", "Picked up from supplier", sup_site, dispatched),
              ("GATE_IN", "Delivered to origin port", o_port, gate), ("DEPARTED", "Vessel departed", o_port, departed),
              ("ARRIVED", "Vessel arrived in India", e_port, arrived), ("CUSTOMS", "Customs cleared", e_port, customs),
              ("DELIVERED", "Delivered to plant (GRN)", plant, delivered)]
    else:
        days = sum(s["days"] for s in segs) if segs else 2
        st = [("READY", "Goods ready at supplier", sup_site, p - dt.timedelta(days=round(days) + 1)),
              ("DISPATCHED", "Picked up from supplier", sup_site, p - dt.timedelta(days=round(days))),
              ("IN_TRANSIT", "Highway checkpoint", f"{sup_site} → {plant}", p - dt.timedelta(days=max(1, round(days / 2)))),
              ("DELIVERED", "Delivered to plant (GRN)", plant, p)]
    out = [{"seq": 1, "key": "CONFIRMED", "label": "Order confirmed", "loc": sup_site, "planned": po + dt.timedelta(days=1)}]
    for i, (k, lbl, loc, d) in enumerate(st, start=2):
        out.append({"seq": i, "key": k, "label": lbl, "loc": loc, "planned": max(d, po + dt.timedelta(days=1))})
    return out


def build(rows: list[dict], lanes: dict, stated: dict) -> list[dict]:
    out = []
    for r in rows:
        lane = lanes.get(f"{r['supplier_id']}>{r['plant_id']}", {})
        mode = "SEA" if lane.get("mode") == "SEA" and len(lane.get("segments") or []) == 3 else "ROAD"
        stages = journey(r, lane)
        rec = _d(r["receipt_date"]) if r["receipt_date"] else None
        total = (rec - _d(r["promised_date"])).days if rec else 0
        rnd = random.Random(f"{r['po_id']}/{r['po_line']}")
        add = {}                                                     # stage key -> (days, reason)
        if total > 0:
            prof = {k: w for k, w in PROFILE.get(r["supplier_id"], {"SUPPLIER_CAPACITY": 1}).items() if mode in REASONS[k][2]} or {"SUPPLIER_CAPACITY": 1}
            said = stated.get((r["po_id"], r["po_line"]))
            if said:                                                  # the supplier said why: that is the main cause
                primary = _code_of(said)
            else:
                primary = rnd.choices(list(prof), weights=list(prof.values()))[0]
            rest = [k for k in prof if k != primary and REASONS[k][0] != REASONS[primary][0]]
            if rest and total >= 3 and rnd.random() < 0.45:
                second = rnd.choice(rest); d2 = max(1, round(total * rnd.uniform(0.2, 0.35)))
                add[REASONS[second][0]] = (d2, second, None); total_p = total - d2
            else:
                total_p = total
            k1 = REASONS[primary][0]
            add[k1] = (add.get(k1, (0,))[0] + total_p, primary, f"Supplier stated: {said}" if said else None)
        cum = 0
        for s in stages:
            days, reason, override = (add.get(s["key"]) or (0, None, None)) + (None,) * (3 - len(add.get(s["key"]) or (0, None, None)))
            cum += days
            actual = s["planned"] + dt.timedelta(days=cum)
            if s["key"] == "DELIVERED" and rec:
                actual = rec
            elif total <= 0 and rec:                                   # on time / early: never later than the receipt
                actual = min(actual, rec) if s["key"] != "CONFIRMED" else actual
            txt = (override or TEXT[reason].format(d=days, loc=s["loc"])) if reason else None
            out.append({"po": r["po_id"], "ln": r["po_line"], "s": r["supplier_id"], "seq": s["seq"], "st": s["key"], "loc": s["loc"][:120],
                        "pl": s["planned"].isoformat(), "ac": actual.isoformat(), "dd": days,
                        "rc": reason, "rt": txt, "rb": REPORTER[REASONS[reason][1]] if reason else None})
    return out


def _code_of(said: str) -> str:
    s = said.lower()
    for words, code in ((("material", "sub-tier", "component shortage"), "RAW_MATERIAL_SHORTAGE"), (("capacity", "backlog", "line"), "SUPPLIER_CAPACITY"),
                        (("quality", "rework", "inspection"), "QUALITY_HOLD"), (("customs",), "CUSTOMS_HOLD"), (("vessel", "sailing", "cut-off"), "MISSED_VESSEL_CUTOFF"),
                        (("congestion", "port"), "PORT_CONGESTION"), (("truck", "transport"), "TRUCK_UNAVAILABLE"), (("weather", "cyclone", "storm"), "WEATHER"),
                        (("document",), "LATE_DOCUMENTS")):
        if any(w in s for w in words):
            return code
    return "SUPPLIER_CAPACITY"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true"); ap.add_argument("--clear", action="store_true")
    a = ap.parse_args()
    with engine.begin() as c:
        c.execute(text("DELETE FROM sp.fact_delay_event WHERE source = 'SYNTHETIC'"))
    if a.clear:
        print("cleared"); return
    rows = A.query(f"""SELECT po_id, po_line, supplier_id, plant_id, po_date, promised_date, receipt_date
                       FROM {A.S}.v_po_delivery WHERE NOT excluded ORDER BY po_id, po_line""")
    lanes = {l["id"]: l for l in TR.network(True, True)["lanes"]}
    stated = {(r["po_id"], r["po_line"]): r["reason"] for r in A.query("SELECT po_id, po_line, reason FROM public.promise_changes WHERE reason IS NOT NULL")}
    data = build(rows, lanes, stated)
    with engine.begin() as c:
        for i in range(0, len(data), 2500):
            c.execute(text("""INSERT INTO sp.fact_delay_event (po_id, po_line, supplier_id, stage_seq, stage, location, planned_ts, actual_ts,
                                added_delay_days, reason_code, reason_text, reported_by, source)
                              SELECT x.po, x.ln, x.s, x.seq, x.st, x.loc, CAST(x.pl AS date), CAST(x.ac AS date), x.dd, x.rc, x.rt, x.rb, 'SYNTHETIC'
                              FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS x(po text, ln int, s text, seq int, st text, loc text, pl text, ac text,
                                   dd numeric, rc text, rt text, rb text)"""), {"rows": json.dumps(data[i:i + 2500])})
    late = len({(d["po"], d["ln"]) for d in data if d["rc"]})
    print(f"wrote {len(data)} stage rows for {len(rows)} PO lines ({late} with a delay reason)")


if __name__ == "__main__":
    main()
