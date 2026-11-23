"""GPS SIMULATOR for demos (T3). Drives the REAL ingestion API end to end - nothing is written to the database directly.

It registers simulated vehicles (device_type = SIMULATOR, so the twin always labels them "Simulated demo data"), assigns
each one shipments that are in transit NOW on a road lane, and posts a GPS ping every --every seconds while moving along
the real routed path at --speed km/h (time compressed by --accel).

    python -m scripts.gps_simulator --vehicles 3 --minutes 10          # needs the backend running and flag gps_tracking on
    python -m scripts.gps_simulator --stop                             # end all SIMULATOR trips and deactivate them

Credentials: an ADMIN or PROCUREMENT user (SIM_USER / SIM_PASSWORD env vars).
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
B = os.environ.get("API_URL", "http://127.0.0.1:8000")


def _km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicles", type=int, default=3)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--every", type=float, default=5, help="seconds between pings")
    ap.add_argument("--speed", type=float, default=55, help="truck speed km/h")
    ap.add_argument("--accel", type=float, default=60, help="simulated seconds per real second")
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args()
    user, pw = os.environ.get("SIM_USER", "admin"), os.environ.get("SIM_PASSWORD")
    if not pw:
        raise SystemExit("set SIM_USER / SIM_PASSWORD (an ADMIN or PROCUREMENT account)")
    c = httpx.Client(timeout=120)
    H = {"Authorization": "Bearer " + c.post(f"{B}/api/auth/login", json={"username": user, "password": pw}).raise_for_status().json()["token"]}

    if a.stop:
        from sqlalchemy import text
        from app.db import engine
        with engine.begin() as x:
            n = x.execute(text("""UPDATE public.vehicle_trips SET ended_at = now() WHERE ended_at IS NULL AND vehicle_id IN
                                  (SELECT vehicle_id FROM public.vehicles WHERE device_type = 'SIMULATOR')""")).rowcount
            x.execute(text("UPDATE public.vehicles SET active = FALSE WHERE device_type = 'SIMULATOR'"))
        c.post(f"{B}/api/cache/clear", headers=H)
        print(f"ended {n} simulator trips; simulator vehicles deactivated")
        return

    net = c.get(f"{B}/api/tracker", headers=H).raise_for_status().json()
    start = dt.datetime.fromisoformat(net["start"]).replace(tzinfo=dt.timezone.utc)
    t_now = (dt.datetime.now(dt.timezone.utc) - start).total_seconds() / 86400
    lanes = {l["id"]: l for l in net["lanes"]}
    road = [l for l in net["lanes"] if l["mode"] == "ROAD"]
    # shipments that would be on the road now (or the nearest upcoming ones) - grouped per lane = one truck per lane
    cands = sorted((s for s in net["shipments"] if s["lane"] in {l["id"] for l in road} and s["t_eta"] >= t_now - 1),
                   key=lambda s: abs(s["t_depart"] - t_now))
    picked, used = [], set()
    for s in cands:
        if s["lane"] not in used:
            used.add(s["lane"]); picked.append(s)
        if len(picked) >= a.vehicles:
            break
    sims = []
    for i, s in enumerate(picked):
        v = c.post(f"{B}/api/vehicles", headers=H, json={"kind": "TRUCK", "registration": f"SIM-TN-{i + 1:02d}", "carrier": "Simulated carrier",
                                                         "device_type": "SIMULATOR"}).raise_for_status().json()
        same = [x["id"] for x in net["shipments"] if x["lane"] == s["lane"] and abs(x["t_eta"] - s["t_eta"]) < 0.6][:6]
        c.post(f"{B}/api/vehicles/{v['vehicle_id']}/trip", headers=H, json={"shipment_ids": same}).raise_for_status()
        path = lanes[s["lane"]]["path"]
        cum = [0.0]
        for k in range(1, len(path)):
            cum.append(cum[-1] + _km(path[k - 1], path[k]))
        sims.append({"v": v, "path": path, "cum": cum, "km": 0.0, "lane": s["lane"], "n": len(same)})
        print(f"{v['vehicle_id']} SIM truck on {s['lane']} carrying {len(same)} PO lines ({cum[-1]:.0f} km)")
    t_end = time.time() + a.minutes * 60
    while time.time() < t_end and any(x["km"] < x["cum"][-1] for x in sims):
        for x in sims:
            if x["km"] >= x["cum"][-1]:
                continue
            x["km"] = min(x["cum"][-1], x["km"] + a.speed * a.every * a.accel / 3600)
            k = next(i for i in range(1, len(x["cum"])) if x["cum"][i] >= x["km"] or i == len(x["cum"]) - 1)
            f = (x["km"] - x["cum"][k - 1]) / ((x["cum"][k] - x["cum"][k - 1]) or 1)
            lat = x["path"][k - 1][0] + f * (x["path"][k][0] - x["path"][k - 1][0])
            lon = x["path"][k - 1][1] + f * (x["path"][k][1] - x["path"][k - 1][1])
            r = c.post(f"{B}/api/gps/pings", headers={"X-Device-Key": x["v"]["device_key"]}, json={"pings": [
                {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "lat": round(lat, 5), "lon": round(lon, 5), "speed_kmh": a.speed, "accuracy_m": 8}]})
            print(f"  {x['v']['vehicle_id']} {x['km']:6.1f}/{x['cum'][-1]:.0f} km -> {r.json()}")
        time.sleep(a.every)
    print("done (trips stay active; run with --stop to end them)")


if __name__ == "__main__":
    main()
