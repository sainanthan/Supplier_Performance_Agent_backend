"""Demo registrations for the GPS Tracking page: three simulated trucks on real road lanes, each on a trip carrying the PO lines
of its lane, with a 3-hour position history up to a few minutes ago (so they show as Live), plus one idle truck.

Everything is labelled: device_type = SIMULATOR (the twin always shows "Simulated demo data"), registrations start with DEMO-,
carrier "Demo transporter". Positions go through the normal validation (app.gps.ingest).

    python -m scripts.seed_gps_demo            # register the demo fleet (skips if it already exists)
    python -m scripts.seed_gps_demo --refresh  # add fresh positions so the demo trucks are Live again
    python -m scripts.seed_gps_demo --clear    # end the demo trips and deactivate the demo vehicles
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import text  # noqa: E402

from app import gps as G  # noqa: E402
from app import tracker as TR  # noqa: E402
from app.db import engine  # noqa: E402

FLEET = [("DEMO-TN-01-AX-4412", "Demo transporter · Chennai"), ("DEMO-KA-05-MN-2207", "Demo transporter · Bengaluru"),
         ("DEMO-TN-22-BK-7781", "Demo transporter · Hosur")]
IDLE = ("DEMO-TN-09-CC-1150", "Demo transporter · standby")
HOURS, EVERY_MIN, SPEED = 3, 6, 42                       # history length, minutes between positions, average km/h


def _km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _at(path, cum, km):
    k = next((i for i in range(1, len(cum)) if cum[i] >= km), len(cum) - 1)
    f = 0 if cum[k] == cum[k - 1] else (km - cum[k - 1]) / (cum[k] - cum[k - 1])
    return path[k - 1][0] + f * (path[k][0] - path[k - 1][0]), path[k - 1][1] + f * (path[k][1] - path[k - 1][1])


def _demo_vehicles():
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text("SELECT vehicle_id, registration, active FROM public.vehicles WHERE registration LIKE 'DEMO-%'")).mappings()]


def _positions(v: dict, path: list, start_frac: float) -> int:
    """Positions every EVERY_MIN minutes over the last HOURS hours, ending ~2 minutes ago, moving along the lane."""
    cum = [0.0]
    for k in range(1, len(path)):
        cum.append(cum[-1] + _km(path[k - 1], path[k]))
    now = dt.datetime.now(dt.timezone.utc)
    n = HOURS * 60 // EVERY_MIN
    km0 = min(cum[-1] * start_frac, max(0.0, cum[-1] - SPEED * HOURS))
    pings = []
    for i in range(n + 1):
        ts = now - dt.timedelta(minutes=2 + (n - i) * EVERY_MIN)
        lat, lon = _at(path, cum, min(cum[-1], km0 + SPEED * i * EVERY_MIN / 60))
        pings.append({"ts": ts.isoformat(), "lat": round(lat, 5), "lon": round(lon, 5),
                      "speed_kmh": SPEED + (i % 5) - 2, "accuracy_m": 8, "note": "demo"})
    return G.ingest({**v, "kind": "TRUCK", "device_type": "SIMULATOR"}, pings, reported_by="seed_gps_demo")["accepted"]


def _lanes():
    net = TR.tracker()
    start = dt.datetime.fromisoformat(net["start"]).replace(tzinfo=dt.timezone.utc)
    t_now = (dt.datetime.now(dt.timezone.utc) - start).total_seconds() / 86400
    lanes = {l["id"]: l for l in net["lanes"] if l["mode"] == "ROAD" and len(l.get("path") or []) > 1}
    cands = sorted((s for s in net["shipments"] if s["lane"] in lanes and s["t_eta"] >= t_now - 1), key=lambda s: abs(s["t_depart"] - t_now))
    picked, used = [], set()
    for s in cands:
        if s["lane"] not in used:
            used.add(s["lane"])
            same = [x["id"] for x in net["shipments"] if x["lane"] == s["lane"] and abs(x["t_eta"] - s["t_eta"]) < 0.6][:4]
            picked.append((lanes[s["lane"]], same))
        if len(picked) >= len(FLEET):
            break
    return picked


def seed() -> None:
    have = {v["registration"]: v for v in _demo_vehicles() if v["active"]}
    if have:
        print(f"demo fleet already registered ({len(have)} vehicles); use --refresh for fresh positions or --clear to remove it")
        return
    picked = _lanes()
    if not picked:
        raise SystemExit("no road lanes with shipments found in the tracker; is the tracker data loaded?")
    for (reg, carrier), (lane, ids), frac in zip(FLEET, picked, (0.25, 0.45, 0.6)):
        v = G.create_vehicle("TRUCK", reg, carrier, "SIMULATOR", "seed_gps_demo")
        G.start_trip(v["vehicle_id"], ids, "seed_gps_demo")
        with engine.begin() as c:                 # the trip began HOURS + 15 min ago, so the position history fits inside it
            c.execute(text("UPDATE public.vehicle_trips SET started_at = now() - make_interval(mins => :m) WHERE vehicle_id = :v AND ended_at IS NULL"),
                      {"m": HOURS * 60 + 15, "v": v["vehicle_id"]})
        n = _positions(v, lane["path"], frac)
        print(f"{v['vehicle_id']} {reg}: trip on lane {lane['id']} with {len(ids)} PO lines, {n} positions")
    v = G.create_vehicle("TRUCK", IDLE[0], IDLE[1], "SIMULATOR", "seed_gps_demo")
    print(f"{v['vehicle_id']} {IDLE[0]}: registered, idle (no trip)")


def refresh() -> None:
    lanes = {tuple(ids): lane for lane, ids in _lanes()}
    fracs = iter((0.15, 0.3, 0.45, 0.2, 0.35))               # place each demo truck mid-route, not at the plant
    for v in _demo_vehicles():
        trip = G.active_trip(v["vehicle_id"])
        if not trip or not v["active"]:
            continue
        lane = next((l for ids, l in lanes.items() if set(ids) & set(trip["shipment_ids"])), None) or next(iter(lanes.values()), None)
        if lane:
            with engine.begin() as c:
                c.execute(text("UPDATE public.vehicle_trips SET started_at = LEAST(started_at, now() - make_interval(mins => :m)) WHERE trip_id = :t"),
                          {"m": HOURS * 60 + 15, "t": trip["trip_id"]})
                # demo trips only: replace the old history, so the trail is one continuous drive (no overlapping replays)
                c.execute(text("DELETE FROM public.gps_pings WHERE trip_id = :t"), {"t": trip["trip_id"]})
            print(f"{v['vehicle_id']}: {_positions(v, lane['path'], next(fracs, 0.3))} fresh positions")


def clear() -> None:
    with engine.begin() as c:
        ids = [r[0] for r in c.execute(text("SELECT vehicle_id FROM public.vehicles WHERE registration LIKE 'DEMO-%'"))]
        if ids:
            c.execute(text("UPDATE public.vehicle_trips SET ended_at = now() WHERE vehicle_id = ANY(:i) AND ended_at IS NULL"), {"i": ids})
            c.execute(text("UPDATE public.vehicles SET active = FALSE WHERE vehicle_id = ANY(:i)"), {"i": ids})
    print(f"ended the demo trips and deactivated {len(ids)} demo vehicles")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--clear", action="store_true")
    a = ap.parse_args()
    clear() if a.clear else refresh() if a.refresh else seed()
