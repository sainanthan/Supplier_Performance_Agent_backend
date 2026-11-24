"""Synthetic logistics milestones + GPS pings for DEMOS of the network twin (T2).

Every row is tagged source='SYNTHETIC'. When any are loaded, the twin shows a "Simulated demo data" banner and marks
each affected object as demo - simulated data is never presented as real. KPIs do not read this table.

    python -m scripts.gen_milestones                 # write data/sample_synthetic/SYNTHETIC_fact_inbound_milestone.csv
    python -m scripts.gen_milestones --load          # ... and load it into sp.fact_inbound_milestone (source=SYNTHETIC)
    python -m scripts.gen_milestones --clear         # remove all SYNTHETIC rows (back to purely estimated positions)

Selection: shipments in transit within +-10 days of the as-of date; --share of them get milestones, --gps of those also
get GPS pings every 3 h on road legs (=> "Live"). Event times follow the real route segments with random delays.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import text  # noqa: E402

from app import tracker as TR  # noqa: E402
from app.db import engine, init_db  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "data" / "sample_synthetic" / "SYNTHETIC_fact_inbound_milestone.csv"
COLS = ["shipment_ref", "po_id", "po_line", "milestone", "milestone_ts", "location", "lat", "lon", "source"]


def _interp(path: list, f: float) -> tuple:
    import math
    cum = [0.0]
    for k in range(1, len(path)):
        cum.append(cum[-1] + math.dist(path[k - 1], path[k]))
    target = f * cum[-1]
    for k in range(1, len(path)):
        if cum[k] >= target:
            r = (target - cum[k - 1]) / ((cum[k] - cum[k - 1]) or 1)
            return (path[k - 1][0] + r * (path[k][0] - path[k - 1][0]), path[k - 1][1] + r * (path[k][1] - path[k - 1][1]))
    return tuple(path[-1])


def generate(share: float, gps: float, seed: int) -> list[list]:
    rnd = random.Random(seed)
    init_db()
    net = TR._network(True, True)
    start = dt.datetime.combine(dt.date.fromisoformat(net["start"]), dt.time())
    as_of = net["t_as_of"]
    lanes = {l["id"]: l for l in net["lanes"]}
    pool = [s for s in net["shipments"] if s["t_depart"] <= as_of + 10 and s["t_eta"] >= as_of - 10]
    rows = []
    for s in rnd.sample(pool, int(len(pool) * share)):
        lane = lanes[s["lane"]]
        segs = lane["segments"]
        tot_days = sum(x["days"] for x in segs) or 1
        span = s["t_eta"] - s["t_depart"]
        t = s["t_depart"] + rnd.uniform(-0.3, 0.3)
        ts = lambda d: (start + dt.timedelta(days=d)).isoformat(timespec="minutes")
        ev = [("DEPARTED_SUPPLIER", t, lane["path"][0])]
        live = rnd.random() < gps
        for k, seg in enumerate(segs):
            d = span * seg["days"] / tot_days * rnd.uniform(0.9, 1.25)          # some legs run slow
            a, b = seg["from_index"], seg["to_index"]
            if seg["mode"] == "SEA":
                ev += [("GATE_IN_ORIGIN_PORT", t, lane["path"][a]), ("VESSEL_DEPARTED", t + 0.8, lane["path"][a]),
                       ("VESSEL_ARRIVED", t + d, lane["path"][b]), ("CUSTOMS_CLEARED", t + d + rnd.uniform(0.5, 2.0), lane["path"][b])]
                t = ev[-1][1]
                ev.append(("OUT_FOR_DELIVERY", t + 0.1, lane["path"][b]))
            elif live:                                                           # GPS ping every 3 h
                sub = lane["path"][a:b + 1]
                n = max(1, int(d * 8))
                ev += [("GPS", t + d * i / n, _interp(sub, i / n)) for i in range(1, n)]
                t += d
            else:
                t += d
        rows += [[s["id"], s["po_id"], int(s["id"].rsplit("-", 1)[1]), m, ts(tt), m.replace("_", " ").title(), round(p[0], 5), round(p[1], 5), "SYNTHETIC"]
                 for m, tt, p in ev if tt <= as_of + 0.5]                         # only events that have "happened"
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--share", type=float, default=0.35)
    ap.add_argument("--gps", type=float, default=0.4)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--clear", action="store_true")
    a = ap.parse_args()
    if a.clear:
        with engine.begin() as c:
            n = c.execute(text("DELETE FROM sp.fact_inbound_milestone WHERE source = 'SYNTHETIC'")).rowcount
        print(f"removed {n} SYNTHETIC milestone rows")
        return
    rows = generate(a.share, a.gps, a.seed)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(COLS); w.writerows(rows)
    print(f"{len(rows)} SYNTHETIC milestone rows for {len({r[0] for r in rows})} shipments -> {OUT}")
    if a.load:
        with engine.begin() as c:
            c.execute(text("DELETE FROM sp.fact_inbound_milestone WHERE source = 'SYNTHETIC'"))
            # lineage: label the file and stamp the load time, like every other loader
            c.execute(text(f"INSERT INTO sp.fact_inbound_milestone ({', '.join(COLS)}, _source_system, _loaded_at) "
                           f"VALUES ({', '.join(':' + x for x in COLS)}, :_src, now())"),
                      [{**dict(zip(COLS, r)), "_src": f"SYNTHETIC:{OUT.name}"} for r in rows])
        print("loaded into sp.fact_inbound_milestone (source = SYNTHETIC); the twin will show 'Simulated demo data'")


if __name__ == "__main__":
    main()
