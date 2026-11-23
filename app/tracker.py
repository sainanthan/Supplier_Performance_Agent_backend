"""Network Tracker: a geographic digital shadow of supplier -> plant shipments.

Every PO line is one shipment. Its timeline comes from the data:
  po_date -> (in production at supplier) -> depart_date -> (in transit along the lane) -> receipt_date (= ETA)
depart_date = receipt_date - lane transit days (never before po_date + 1).
Lines received after the as-of date are forecasts; a twin scenario (delay / outage for a scope of suppliers) shifts them.

Supplier sites are ASSUMED: the data only gives a country, so each supplier is placed at a representative
industrial city for its country and category (edit sp.dim_location to real addresses). Plants and ports are fixed.
"""
from __future__ import annotations

import datetime as dt
import math

from . import analytics as A

S = A.S

# Representative sites (assumption: the data only provides a country per supplier).
SUPPLIER_SITES = {
    "SUP-001": ("Shenzhen", 22.54, 114.06, "PORT-YANTIAN"),
    "SUP-002": ("Icheon", 37.27, 127.44, "PORT-BUSAN"),
    "SUP-003": ("Taoyuan", 24.99, 121.30, "PORT-KAOHSIUNG"),
    "SUP-004": ("Oragadam, Chennai", 12.84, 79.95, None),
    "SUP-005": ("Pune", 18.52, 73.86, None),
    "SUP-006": ("Bac Ninh", 21.18, 106.07, "PORT-HAIPHONG"),
    "SUP-007": ("Hosur", 12.74, 77.83, None),
    "SUP-008": ("Coimbatore", 11.02, 76.96, None),
    "SUP-009": ("Yokohama", 35.44, 139.64, "PORT-YOKOHAMA"),
    "SUP-010": ("Jurong, Singapore", 1.33, 103.70, "PORT-SINGAPORE"),
}
PLANTS = {"IN-TN-01": ("Chennai Electronics Plant (Sriperumbudur)", 12.96, 79.95),
          "IN-KA-01": ("Bengaluru Electronics Plant", 12.97, 77.59)}
# Container-terminal berths (on the quay line, not the city centre), so the map pin, the port model and the sea lane meet
# at the waterfront. Hai Phong = Lach Huyen deep-sea terminal (the river port cannot take mainline vessels);
# Busan = Busan New Port (the main container hub); Singapore = Pasir Panjang; Yokohama = Honmoku.
PORTS = {"PORT-YANTIAN": ("Yantian port", 22.5755, 114.2730), "PORT-BUSAN": ("Busan port", 35.0745, 128.8300),
         "PORT-KAOHSIUNG": ("Kaohsiung port", 22.5660, 120.3050), "PORT-HAIPHONG": ("Hai Phong port", 20.8070, 106.9930),
         "PORT-YOKOHAMA": ("Yokohama port", 35.4310, 139.6720), "PORT-SINGAPORE": ("Singapore port", 1.2720, 103.7620),
         "PORT-CHENNAI": ("Chennai port", 13.0960, 80.2960)}
# Indian entry ports (T1). Real port locations; which supplier uses which port is an ASSUMPTION stored in
# public.network_sites.entry_port (editable). West-coast ports are available for configuration.
INDIA_PORTS = {"PORT-CHENNAI": ("Chennai port", 13.0960, 80.2960), "PORT-KATTUPALLI": ("Kattupalli port", 13.3070, 80.3470),
               "PORT-KAMARAJAR": ("Kamarajar port (Ennore)", 13.2570, 80.3320), "PORT-COCHIN": ("Cochin port", 9.9870, 76.2560),
               "PORT-NMPT": ("New Mangalore port", 12.9270, 74.8090), "PORT-TUTICORIN": ("V.O. Chidambaranar port (Tuticorin)", 8.7600, 78.2050)}
# Port facts for the port digital twin: country, main container terminal, UN/LOCODE and the bearing (degrees from north)
# the quay faces towards open water (the map turns the port model so its cranes reach over the water).
PORT_INFO = {
    "PORT-YANTIAN": ("China", "Yantian International Container Terminals", "CNYTN", 165),
    "PORT-BUSAN": ("South Korea", "Busan New Port container terminals", "KRBNP", 180),
    "PORT-KAOHSIUNG": ("Taiwan", "Kaohsiung Intercontinental Container Terminal", "TWKHH", 245),
    "PORT-HAIPHONG": ("Vietnam", "Lach Huyen deep-sea terminal", "VNHPH", 160),
    "PORT-YOKOHAMA": ("Japan", "Honmoku container pier", "JPYOK", 100),
    "PORT-SINGAPORE": ("Singapore", "Pasir Panjang Terminal", "SGSIN", 200),
    "PORT-CHENNAI": ("India", "Chennai Container Terminal", "INMAA", 90),
    "PORT-KATTUPALLI": ("India", "Kattupalli Container Terminal", "INKAT", 90),
    "PORT-KAMARAJAR": ("India", "Kamarajar Port (Ennore)", "INENR", 90),
    "PORT-COCHIN": ("India", "ICTT Vallarpadam", "INCOK", 260),
    "PORT-NMPT": ("India", "New Mangalore Port", "INNML", 270),
    "PORT-TUTICORIN": ("India", "V.O. Chidambaranar Port", "INTUT", 95),
}


def _port_node(k: str, v: tuple, india: bool | None = None) -> dict:
    info = PORT_INFO.get(k, ("", "", "", 90))
    n = {"id": k, "kind": "port", "name": v[0], "lat": v[1], "lon": v[2], "country": info[0], "terminal": info[1],
         "locode": info[2], "sea_bearing": info[3]}
    if india is not None:
        n["india"] = india
    return n
DEFAULT_ENTRY = {"SUP-001": "PORT-CHENNAI", "SUP-002": "PORT-KATTUPALLI", "SUP-003": "PORT-CHENNAI",
                 "SUP-006": "PORT-KAMARAJAR", "SUP-009": "PORT-KATTUPALLI", "SUP-010": "PORT-CHENNAI"}
EAST_COAST = {"PORT-CHENNAI", "PORT-KATTUPALLI", "PORT-KAMARAJAR"}
MALACCA_EXIT = [(1.25, 104.0), (2.5, 101.5), (5.9, 97.5), (6.6, 94.5)]          # shared corridor (real lane)


def sea_approach(entry: str) -> list[tuple]:
    """Bay of Bengal / Arabian Sea approach from the Malacca exit to an Indian entry port."""
    lat, lon = INDIA_PORTS[entry][1], INDIA_PORTS[entry][2]
    if entry in EAST_COAST:
        return [(9.5, 87.5), (lat, 81.2), (lat, lon)]
    if entry == "PORT-TUTICORIN":
        return [(6.5, 86.0), (5.8, 81.0), (7.3, 78.9), (lat, lon)]
    return [(6.5, 86.0), (5.6, 80.6), (6.8, 77.6), (lat - 0.4, lon - 0.4), (lat, lon)]   # round Sri Lanka, west coast
# Sea legs to the Strait of Malacca, then across the Bay of Bengal to Chennai.
SEA_TO_MALACCA = {
    "PORT-YANTIAN": [(19.5, 114.8), (14.0, 112.5), (7.5, 107.8), (2.5, 105.0)],
    "PORT-BUSAN": [(32.5, 127.5), (27.0, 124.0), (21.5, 120.8), (14.0, 113.5), (7.5, 107.8), (2.5, 105.0)],
    "PORT-KAOHSIUNG": [(20.0, 119.0), (14.0, 113.5), (7.5, 107.8), (2.5, 105.0)],
    "PORT-HAIPHONG": [(19.0, 108.2), (16.5, 109.5), (11.0, 109.8), (7.5, 107.8), (2.5, 105.0)],
    "PORT-YOKOHAMA": [(33.5, 137.5), (30.5, 131.0), (26.5, 125.0), (21.5, 120.8), (14.0, 113.5), (7.5, 107.8), (2.5, 105.0)],
    "PORT-SINGAPORE": [],
}
MALACCA_TO_CHENNAI = [(1.25, 104.0), (2.5, 101.5), (5.9, 97.5), (6.6, 94.5), (9.5, 87.5), (13.10, 80.30)]
ROAD_VIA = {  # (from, to) -> intermediate road waypoints
    ("PORT-CHENNAI", "IN-KA-01"): [(12.92, 79.13), (12.83, 78.40)],
    ("PORT-NMPT", "IN-KA-01"): [(12.87, 75.75), (12.95, 76.40)],
    ("PORT-COCHIN", "IN-KA-01"): [(10.80, 76.65), (11.65, 78.15), (12.13, 78.16)],
    ("PORT-COCHIN", "IN-TN-01"): [(10.80, 76.65), (11.65, 78.15), (12.25, 79.07)],
    ("PORT-TUTICORIN", "IN-TN-01"): [(9.92, 78.12), (10.79, 78.70), (11.94, 79.49)],
    ("PORT-TUTICORIN", "IN-KA-01"): [(9.92, 78.12), (11.65, 78.15), (12.13, 78.16)],
    ("SUP-004", "IN-KA-01"): [(12.92, 79.13), (12.83, 78.40)],
    ("SUP-005", "IN-KA-01"): [(16.70, 74.24), (15.36, 75.12), (13.34, 77.10)],
    ("SUP-005", "IN-TN-01"): [(17.40, 78.40), (14.45, 79.98)],
    ("SUP-007", "IN-TN-01"): [(12.92, 79.13)],
    ("SUP-008", "IN-KA-01"): [(11.65, 78.15), (12.13, 78.16)],
    ("SUP-008", "IN-TN-01"): [(11.65, 78.15), (12.25, 79.07)],
}
SEA_KM_PER_DAY, ROAD_KM_PER_DAY, PORT_DAYS = 520.0, 450.0, 2.0


def _km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _path_km(pts) -> float:
    return sum(_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _lane(sid: str, plant: str) -> dict:
    name, lat, lon, port = SUPPLIER_SITES[sid]
    plat, plon = PLANTS[plant][1], PLANTS[plant][2]
    stops = [{"name": name, "kind": "supplier", "lat": lat, "lon": lon}]
    if port:
        pn, pla, plo = PORTS[port]
        sea = [(pla, plo)] + SEA_TO_MALACCA[port] + MALACCA_TO_CHENNAI
        road_first = [(lat, lon), (pla, plo)]
        road_last = [(13.10, 80.30)] + ROAD_VIA.get(("PORT-CHENNAI", plant), []) + [(plat, plon)]
        path = road_first + sea[1:] + road_last[1:]
        stops += [{"name": pn, "kind": "port", "lat": pla, "lon": plo},
                  {"name": "Chennai port", "kind": "port", "lat": 13.10, "lon": 80.30}]
        sea_km, road_km = _path_km(sea), _path_km(road_first) + _path_km(road_last)
        transit = round(sea_km / SEA_KM_PER_DAY + road_km / ROAD_KM_PER_DAY + 2 * PORT_DAYS, 1)
        mode = "SEA"
    else:
        path = [(lat, lon)] + ROAD_VIA.get((sid, plant), []) + [(plat, plon)]
        sea_km, road_km = 0.0, _path_km(path)
        transit = round(max(0.5, road_km / ROAD_KM_PER_DAY + 0.5), 1)
        mode = "ROAD"
    stops.append({"name": PLANTS[plant][0], "kind": "plant", "lat": plat, "lon": plon})
    return {"id": f"{sid}>{plant}", "supplier_id": sid, "plant_id": plant, "mode": mode, "transit_days": transit,
            "distance_km": round(sea_km + road_km), "path": [[round(a, 4), round(b, 4)] for a, b in path], "stops": stops}


def _sites() -> dict:
    """Editable network sites (public.network_sites); seeded once from the constants above, all marked assumed."""
    try:
        rows = A._run("SELECT * FROM public.network_sites", {})
        if not rows:
            from sqlalchemy import text
            from .db import engine
            with engine.begin() as c:
                for sid, (name, lat, lon, port) in SUPPLIER_SITES.items():
                    c.execute(text("""INSERT INTO public.network_sites (node_id, kind, name, lat, lon, origin_port, entry_port, assumed, source)
                                      VALUES (:i, 'supplier', :n, :la, :lo, :op, :ep, TRUE, 'Representative city for the supplier country (no address in data)')
                                      ON CONFLICT DO NOTHING"""),
                              {"i": sid, "n": name, "la": lat, "lo": lon, "op": port, "ep": DEFAULT_ENTRY.get(sid) if port else None})
                for pid, (name, lat, lon) in {**PORTS, **INDIA_PORTS}.items():
                    c.execute(text("""INSERT INTO public.network_sites (node_id, kind, name, lat, lon, assumed, source)
                                      VALUES (:i, 'port', :n, :la, :lo, FALSE, 'Port location') ON CONFLICT DO NOTHING"""),
                              {"i": pid, "n": name, "la": lat, "lo": lon})
            rows = A._run("SELECT * FROM public.network_sites", {})
        return {r["node_id"]: r for r in rows}
    except Exception:
        return {}


def _lane_v2(sid: str, plant: str, sites: dict) -> dict:
    """T1 lane: origin port -> own East-Asia leg -> shared Malacca corridor -> own Indian entry port -> road to plant.
    Indices tell the client which part is road, the lane's own sea leg, and the shared corridor (drawn once)."""
    site = sites.get(sid) or {}
    name, lat, lon, port = SUPPLIER_SITES[sid]
    name, lat, lon = site.get("name", name), site.get("lat", lat), site.get("lon", lon)
    port = site.get("origin_port", port) if site else port
    plat, plon = PLANTS[plant][1], PLANTS[plant][2]
    stops = [{"name": name, "kind": "supplier", "lat": lat, "lon": lon}]
    if port:
        entry = (site.get("entry_port") if site else None) or DEFAULT_ENTRY.get(sid, "PORT-CHENNAI")
        en, ela, elo = INDIA_PORTS[entry]
        pn, pla, plo = PORTS[port]
        own = [(pla, plo)] + SEA_TO_MALACCA.get(port, [])
        shared = MALACCA_EXIT
        approach = sea_approach(entry)
        via = ROAD_VIA.get((entry, plant)) or (ROAD_VIA.get(("PORT-CHENNAI", plant), []) if entry in EAST_COAST else [])
        path = [(lat, lon)] + own + shared + approach + via + [(plat, plon)]
        sea_start, shared_from = 1, 1 + len(own)
        sea_end = shared_from + len(shared) + len(approach) - 1
        stops += [{"name": pn, "kind": "port", "lat": pla, "lon": plo}, {"name": en, "kind": "port", "lat": ela, "lon": elo}]
        sea_km = _path_km(path[sea_start:sea_end + 1])
        road_km = _path_km(path[:sea_start + 1]) + _path_km(path[sea_end:])
        transit = round(sea_km / SEA_KM_PER_DAY + road_km / ROAD_KM_PER_DAY + 2 * PORT_DAYS, 1)
        mode, extra = "SEA", {"sea_start": sea_start, "shared_from": shared_from, "sea_end": sea_end, "entry_port": entry,
                              "origin_port": port, "corridor": f"MALACCA>{entry}"}
    else:
        path = [(lat, lon)] + ROAD_VIA.get((sid, plant), []) + [(plat, plon)]
        sea_km, road_km = 0.0, _path_km(path)
        transit = round(max(0.5, road_km / ROAD_KM_PER_DAY + 0.5), 1)
        mode, extra = "ROAD", {}
    stops.append({"name": PLANTS[plant][0], "kind": "plant", "lat": plat, "lon": plon})
    return {"id": f"{sid}>{plant}", "supplier_id": sid, "plant_id": plant, "mode": mode, "transit_days": transit,
            "distance_km": round(sea_km + road_km), "path": [[round(a, 4), round(b, 4)] for a, b in path], "stops": stops,
            "assumed_route": True, **extra}


# ================================================================== T2: real routes + milestone positions
ROAD_DRIVE_H_PER_DAY = 10.0      # trucking hours per day (driver rules + loading)
HANDLING_DAYS = 0.5              # loading / unloading at each end of a road leg
MILESTONE_FRAC = {"DEPARTED_SUPPLIER": "start", "GATE_IN_ORIGIN_PORT": "sea_start", "VESSEL_DEPARTED": "sea_start",
                  "VESSEL_ARRIVED": "sea_end", "CUSTOMS_CLEARED": "sea_end", "OUT_FOR_DELIVERY": "sea_end", "DELIVERED": "end"}


def _site_of(sid: str, sites: dict) -> tuple:
    site = sites.get(sid) or {}
    name, lat, lon, port = SUPPLIER_SITES[sid]
    return site.get("name", name), site.get("lat", lat), site.get("lon", lon), (site.get("origin_port", port) if site else port), site


def lane_legs(sid: str, plant: str, sites: dict) -> list[tuple]:
    """The route legs a lane needs: [(mode, (lat, lon), (lat, lon))]."""
    name, lat, lon, port, site = _site_of(sid, sites)
    plat, plon = PLANTS[plant][1], PLANTS[plant][2]
    if not port:
        return [("ROAD", (lat, lon), (plat, plon))]
    entry = (site.get("entry_port") if site else None) or DEFAULT_ENTRY.get(sid, "PORT-CHENNAI")
    o, e = PORTS[port], INDIA_PORTS[entry]
    return [("ROAD", (lat, lon), (o[1], o[2])), ("SEA", (o[1], o[2]), (e[1], e[2])), ("ROAD", (e[1], e[2]), (plat, plon))]


def all_legs() -> list[tuple]:
    sites = _sites()
    pairs = A._run(f"SELECT DISTINCT supplier_id, plant_id FROM {S}.v_po_delivery", {})
    return [leg for p in pairs for leg in lane_legs(p["supplier_id"], p["plant_id"], sites)]


def _lane_v3(sid: str, plant: str, sites: dict) -> dict:
    """Lane from cached real routes; any leg not yet routed falls back to the T1 waypoint path (quality APPROXIMATE)."""
    from . import routing as R
    base = _lane_v2(sid, plant, sites)                 # T1 geometry = fallback + stops/ports metadata
    legs = lane_legs(sid, plant, sites)
    segs, path = [], []
    for i, (mode, a, b) in enumerate(legs):
        r = R.cached(mode, a, b)
        if r is None:                                  # fallback: the matching slice of the T1 path
            if base["mode"] == "ROAD":
                coords = base["path"]
            elif i == 0:
                coords = base["path"][: base["sea_start"] + 1]
            elif i == 1:
                coords = base["path"][base["sea_start"]: base["sea_end"] + 1]
            else:
                coords = base["path"][base["sea_end"]:]
            km = _path_km([tuple(c) for c in coords])
            r = {"coords": coords, "km": round(km, 1), "hours": None, "provider": "waypoints", "quality": "APPROXIMATE"}
        hours = r["hours"] if r["hours"] is not None else (r["km"] / 60.0 if mode == "ROAD" else None)
        days = (hours / ROAD_DRIVE_H_PER_DAY + HANDLING_DAYS) if mode == "ROAD" else r["km"] / SEA_KM_PER_DAY + PORT_DAYS
        segs.append({"mode": mode, "km": r["km"], "days": round(days, 2), "provider": r["provider"], "quality": r["quality"],
                     "from_index": max(0, len(path) - 1)})
        coords = r["coords"] if not path else r["coords"][1:] if r["coords"][0] == path[-1] else r["coords"]
        path += [list(c) for c in coords]
        segs[-1]["to_index"] = len(path) - 1
    out = {**base, "path": path, "segments": segs, "distance_km": round(sum(s["km"] for s in segs)),
           "transit_days": round(max(0.5, sum(s["days"] for s in segs)), 1),
           "route_quality": "ROUTED" if all(s["quality"] == "ROUTED" for s in segs) else "APPROXIMATE", "assumed_route": False}
    if base["mode"] == "SEA":
        sea = segs[1]
        out.update(sea_start=sea["from_index"], sea_end=sea["to_index"], shared_from=sea["to_index"], corridor=None)
    return out


def _snap_frac(path: list, cum: list, lat: float, lon: float) -> float:
    i = min(range(len(path)), key=lambda k: (path[k][0] - lat) ** 2 + (path[k][1] - lon) ** 2)
    return cum[i] / (cum[-1] or 1)


def apply_gps(net: dict) -> None:
    """LIVE positions from GPS pings of the vehicle trip carrying each shipment (T3). Raw pings are sent to the client
    (true position); a snapped track keeps the estimate moving along the route after the last ping."""
    from . import gps as G
    try:
        g = G.shipment_gps({s["id"] for s in net["shipments"]})
    except Exception:
        return
    if not g:
        return
    start = dt.datetime.combine(_d(net["start"]), dt.time(), tzinfo=dt.timezone.utc)
    lanes = {l["id"]: l for l in net["lanes"]}
    for s in net["shipments"]:
        v = g.get(s["id"])
        if not v:
            continue
        lane = lanes[s["lane"]]
        cum = [0.0]
        for k in range(1, len(lane["path"])):
            cum.append(cum[-1] + _km(lane["path"][k - 1], lane["path"][k]))
        pts = [[round((ts - start).total_seconds() / 86400, 4), round(lon, 5), round(lat, 5), sp] for ts, lat, lon, sp in v["pings"]]
        s["gps"] = pts
        track = [[p[0], round(_snap_frac(lane["path"], cum, p[2], p[1]), 4)] for p in pts]
        for k in range(1, len(track)):
            track[k][1] = max(track[k][1], track[k - 1][1])
        last = track[-1][0]
        s["t_eta"] = max(s["t_eta"], round(last + 0.05, 3))       # a vehicle still reporting has not arrived yet
        if track[-1][1] < 1:
            track.append([s["t_eta"], 1.0])
        s["track"] = track
        s["t_depart"] = min(s["t_depart"], pts[0][0])
        s["pos_source"] = "LIVE"
        s["vehicle"] = {k: v[k] for k in ("vehicle_id", "registration", "kind", "carrier", "device_type", "trip_id", "active")} | {"last_ping_t": last}
        if v["device_type"] == "SIMULATOR":
            s["demo"] = True
    src = {}
    for s in net["shipments"]:
        src[s.get("pos_source", "ESTIMATED")] = src.get(s.get("pos_source", "ESTIMATED"), 0) + 1
    net["position_sources"] = src
    net["demo_data"] = any(s.get("demo") for s in net["shipments"])
    now = dt.datetime.now(dt.timezone.utc)
    net["t_now"] = round((now - start).total_seconds() / 86400, 3)


def apply_milestones(net: dict) -> None:
    """Position source per shipment: LIVE (GPS pings) > MILESTONE (ERP/TMS events) > ESTIMATED (interpolated dates).
    Tracked shipments get `track` = [[t_days, path_fraction], ...] that the client interpolates."""
    try:
        rows = A._run(f"""SELECT po_id, po_line, milestone, milestone_ts, lat, lon, COALESCE(source, 'ERP') AS source
                          FROM {S}.fact_inbound_milestone WHERE milestone_ts IS NOT NULL ORDER BY milestone_ts""", {})
    except Exception:
        rows = []
    for s in net["shipments"]:
        s["pos_source"] = "ESTIMATED"
    if not rows:
        net["position_sources"] = {"ESTIMATED": len(net["shipments"])}
        return
    start = _d(net["start"])
    lanes = {l["id"]: l for l in net["lanes"]}
    cums = {}
    for l in lanes.values():
        c = [0.0]
        for k in range(1, len(l["path"])):
            c.append(c[-1] + _km(l["path"][k - 1], l["path"][k]))
        cums[l["id"]] = c
    by = {}
    for r in rows:
        by.setdefault(f"{r['po_id']}-{r['po_line']}", []).append(r)
    for s in net["shipments"]:
        ms = by.get(s["id"])
        if not ms:
            continue
        lane, cum = lanes[s["lane"]], cums[s["lane"]]
        total = cum[-1] or 1
        frac_of = {"start": 0.0, "end": 1.0, "sea_start": cum[lane.get("sea_start", 0)] / total, "sea_end": cum[lane.get("sea_end", 0)] / total}
        track = []
        for m in ms:
            ts = dt.datetime.fromisoformat(str(m["milestone_ts"]))
            t = (ts - dt.datetime.combine(start, dt.time())).total_seconds() / 86400
            if m["lat"] is not None and m["lon"] is not None:
                f = _snap_frac(lane["path"], cum, m["lat"], m["lon"])
            elif m["milestone"] in MILESTONE_FRAC:
                f = frac_of[MILESTONE_FRAC[m["milestone"]]]
            else:
                continue
            track.append([round(t, 3), round(f, 4)])
        if not track:
            continue
        track.sort()
        for k in range(1, len(track)):                       # progress never goes backwards
            track[k][1] = max(track[k][1], track[k - 1][1])
        if track[-1][1] < 1 and s["t_eta"] > track[-1][0]:
            track.append([s["t_eta"], 1.0])                  # to the real receipt date
        s["track"] = track
        s["t_depart"] = min(s["t_depart"], track[0][0])
        s["pos_source"] = "LIVE" if any(m["milestone"] == "GPS" for m in ms) else "MILESTONE"
        if any(m["source"] == "SYNTHETIC" for m in ms):
            s["demo"] = True
    src = {}
    for s in net["shipments"]:
        src[s["pos_source"]] = src.get(s["pos_source"], 0) + 1
    net["position_sources"] = src
    net["demo_data"] = any(s.get("demo") for s in net["shipments"])


def movements(net: dict) -> list[dict]:
    """Consolidate shipments into the vehicles that would carry them (T1): sea = one vessel per lane per arrival week,
    road = one truck run per lane per arrival day. Each movement keeps its member shipments (exact dates) so the client can
    show the live count of shipments aboard and drill down to every PO line."""
    mode = {l["id"]: l["mode"] for l in net["lanes"]}
    groups: dict = {}
    for s in net["shipments"]:
        if s.get("pos_source", "ESTIMATED") != "ESTIMATED":
            continue                                   # tracked shipments are drawn individually from their own data
        b = int(s["t_eta"] // 7) if mode[s["lane"]] == "SEA" else int(round(s["t_eta"]))
        groups.setdefault((s["lane"], mode[s["lane"]], b), []).append(s)
    out = []
    for (lane, m, b), ss in groups.items():
        out.append({"id": f"M-{lane}-{m[0]}{b}", "lane": lane, "mode": m, "supplier_id": ss[0]["supplier_id"], "plant_id": ss[0]["plant_id"],
                    "t_depart": min(x["t_depart"] for x in ss), "t_eta": max(x["t_eta"] for x in ss),
                    "t_promised": min(x["t_promised"] for x in ss), "t_po": min(x["t_po"] for x in ss),
                    "disrupted": any(x.get("disrupted") for x in ss), "count": len(ss),
                    "qty": sum(x["qty"] or 0 for x in ss), "value_inr": sum(x["value_inr"] or 0 for x in ss),
                    "material_ids": sorted({x["material_id"] for x in ss}), "shipment_ids": [x["id"] for x in ss],
                    # compact member timeline: [t_depart, t_eta, t_promised, disrupted]
                    "m": [[x["t_depart"], x["t_eta"], x["t_promised"], 1 if x.get("disrupted") else 0] for x in ss],
                    "position": "ESTIMATED"})
    return out


def _d(x) -> dt.date:
    return dt.date.fromisoformat(str(x)[:10])


def _network(v2: bool = False, v3: bool = False) -> dict:
    sup = {s["supplier_id"]: s for s in A.suppliers()}
    v2 = v2 or v3
    sites = _sites() if v2 else {}
    lines = A.query(f"""
      SELECT po_id, po_line, supplier_id, material_id, material_name, plant_id, po_date, promised_date, receipt_date,
             ordered_qty, received_qty, receipt_value_inr, on_time, otif, delivery_status, incoterm
      FROM {S}.v_po_delivery WHERE NOT excluded ORDER BY receipt_date""")
    as_of = _d(A.as_of_date())
    lanes = {}
    shipments = []
    for r in lines:
        lane_id = f"{r['supplier_id']}>{r['plant_id']}"
        if lane_id not in lanes:
            lanes[lane_id] = (_lane_v3(r["supplier_id"], r["plant_id"], sites) if v3 else
                              _lane_v2(r["supplier_id"], r["plant_id"], sites) if v2 else _lane(r["supplier_id"], r["plant_id"]))
        lane = lanes[lane_id]
        po, rec, prom = _d(r["po_date"]), _d(r["receipt_date"]), _d(r["promised_date"])
        span = (rec - po).days
        transit = min(lane["transit_days"], max(0.5, span - 1))
        depart = rec - dt.timedelta(days=transit)
        shipments.append({
            "id": f"{r['po_id']}-{r['po_line']}", "po_id": r["po_id"], "supplier_id": r["supplier_id"],
            "plant_id": r["plant_id"], "lane": lane_id, "material_id": r["material_id"], "material_name": r["material_name"],
            "qty": r["ordered_qty"], "received_qty": r["received_qty"], "value_inr": round(r["receipt_value_inr"] or 0),
            "po_date": po.isoformat(), "promised": prom.isoformat(), "eta": rec.isoformat(),
            "depart": depart.isoformat(timespec="hours") if isinstance(depart, dt.datetime) else depart.isoformat(),
            "transit_days": transit, "on_time": bool(r["on_time"]), "otif": bool(r["otif"]),
            "forecast": rec > as_of, "incoterm": r["incoterm"],
        })
    # depart may be fractional days: express everything as day offsets from the timeline start for the client
    start = min(_d(s["po_date"]) for s in shipments)
    for s in shipments:
        s["t_po"] = (_d(s["po_date"]) - start).days
        s["t_eta"] = (_d(s["eta"]) - start).days
        s["t_depart"] = round(s["t_eta"] - s["transit_days"], 2)
        s["t_promised"] = (_d(s["promised"]) - start).days
        for k in ("depart", "po_date", "promised", "eta", "material_name"):   # the client rebuilds dates from start + t_*
            s.pop(k, None)
    for lane in lanes.values():
        ls = [s for s in shipments if s["lane"] == lane["id"] and not s["forecast"]]
        lane["shipments"] = len([s for s in shipments if s["lane"] == lane["id"]])
        lane["on_time_pct"] = round(100 * sum(s["on_time"] for s in ls) / len(ls), 1) if ls else None
        lane["supplier_name"] = sup[lane["supplier_id"]]["supplier_name"]
        lane["country"] = sup[lane["supplier_id"]]["country"]
    nodes = ([{"id": k, "kind": "supplier", "name": sup[k]["supplier_name"], "site": sites.get(k, {}).get("name", v[0]),
               "lat": sites.get(k, {}).get("lat", v[1]), "lon": sites.get(k, {}).get("lon", v[2]),
               "country": sup[k]["country"], "risk_tier": sup[k]["risk_tier"], "assumed_site": sites.get(k, {}).get("assumed", True)}
              for k, v in SUPPLIER_SITES.items() if k in sup]
             + [{"id": k, "kind": "plant", "name": v[0], "lat": v[1], "lon": v[2]} for k, v in PLANTS.items()])
    if v2:      # only ports some lane actually uses (origin + Indian entry ports)
        used = {l.get("origin_port") for l in lanes.values()} | {l.get("entry_port") for l in lanes.values()}
        nodes += [_port_node(k, v, k in INDIA_PORTS)
                  for k, v in {**PORTS, **INDIA_PORTS}.items() if k in used]
    else:
        nodes += [_port_node(k, v) for k, v in PORTS.items()]
    end = start + dt.timedelta(days=max(s["t_eta"] for s in shipments))
    materials = {r["material_id"]: r["material_name"] for r in lines}
    return {"start": start.isoformat(), "end": end.isoformat(), "as_of": as_of.isoformat(), "materials": materials,
            "t_as_of": (as_of - start).days, "days": (end - start).days + 1,
            "nodes": nodes, "lanes": list(lanes.values()), "shipments": shipments,
            "assumptions": ["Supplier sites are representative cities for each supplier's country (the data has no "
                            "addresses); edit SUPPLIER_SITES in app/tracker.py.",
                            "Transit time = sea distance / 520 km per day + road distance / 450 km per day + 2 port days "
                            "per port; departure = receipt date - transit time.",
                            "Shipments received after the as-of date are forecasts and move with twin scenarios."]
            + (["Entry port per supplier is an assumption (public.network_sites.entry_port, editable).",
                "Vehicles are consolidated: one vessel per lane per arrival week, one truck run per lane per arrival day; "
                "every vehicle lists its PO lines.",
                "Positions are ESTIMATED (interpolated between dates) - no GPS / ASN / milestone feed is connected."] if v2 else []),
            "version": 3 if v3 else 2 if v2 else 1}


network = A._cached(_network)


def tracker(scope: str | None = None, delay_days: int = 0, outage_days: int = 0) -> dict:
    """Network + shipments, with an optional twin disruption applied to forecast shipments."""
    from .features import is_on
    v3 = is_on("network_twin_v2")
    v2 = is_on("tracker_cleanup") or v3
    net = network(v2, v3)
    # wall-clock "now" on the timeline (the clock opens here); the data cut-off (as_of) stays the KPI reference date
    _now = dt.datetime.now(dt.timezone.utc)
    net["today"] = _now.date().isoformat()
    net["t_now"] = round((_now - dt.datetime.combine(_d(net["start"]), dt.time(), tzinfo=dt.timezone.utc)).total_seconds() / 86400, 3)
    if v3:
        apply_milestones(net)
        if is_on("gps_tracking"):
            apply_gps(net)
        cfg = A.get_scorecard_config()
        net["lane_buffer_days"] = cfg.get("lane_at_risk_buffer_days", 2)
        if is_on("twin_risk_encoding"):            # node encodings from the existing scorecard + approved bands
            cards = {c["supplier_id"]: c for c in A.scorecard()}
            for n in net["nodes"]:
                c = cards.get(n["id"])
                if c:
                    n["score"] = {k: c.get(k) for k in ("risk_score", "health", "grade", "composite_score", "otif_pct", "incoming_ppm",
                                                         "ppm_method", "disruption_hours", "past_due_lines", "overdue_capa", "run_id")}
            net["risk_bands"] = cfg.get("health_bands")
        if any(l.get("route_quality") == "APPROXIMATE" for l in net["lanes"]):
            ensure_routes()                            # background fill; this response uses fallbacks (labelled)
    if not scope or (not delay_days and not outage_days):
        if v2:
            net["movements"] = movements(net)
        return net
    sup = {n["id"]: n for n in net["nodes"] if n["kind"] == "supplier"}
    if scope == "ALL":
        aff = set(sup)
    elif scope == "OVERSEAS":
        aff = {k for k, v in sup.items() if v["country"] != "India"}
    else:
        aff = {scope}
    t0 = net["t_as_of"]
    for s in net["shipments"]:
        if s["supplier_id"] not in aff or not s["forecast"] or s.get("track"):
            continue
        shift = 0
        if outage_days and s["t_depart"] >= t0 and s["t_depart"] < t0 + outage_days:
            shift = (t0 + outage_days) - s["t_depart"]       # cannot ship until the outage ends
        shift += delay_days
        if shift:
            s["t_depart"] = round(s["t_depart"] + shift, 2)
            s["t_eta"] = round(s["t_eta"] + shift, 2)
            s["disrupted"] = True
            s["on_time"] = s["t_eta"] <= s["t_promised"]
    net["days"] = int(max(net["days"], max(s["t_eta"] for s in net["shipments"]) + 1))
    net["scenario"] = {"scope": scope, "delay_days": delay_days, "outage_days": outage_days, "affected": sorted(aff)}
    if v2:
        net["movements"] = movements(net)          # consolidated after the scenario shift
    return net


def ensure_routes(force: bool = False, trigger: str = "auto") -> None:
    """Fill the route cache in the background, then drop the cached tracker payload so the next request uses real routes."""
    from . import routing as R

    def done():
        A.clear_cache()
    R.refresh_async(all_legs(), force=force, trigger=trigger, after=done)


# ---------------------------------------------------------------- supplier digital twin (3D view)
def supplier_twin(sid: str) -> dict:
    """Everything the 3D supplier view shows: company profile and owners, KPIs, materials supplied and the delivery history."""
    from sqlalchemy import text as _t
    from .db import read_engine
    prof = A._run(f"SELECT * FROM {A.S}.dim_supplier WHERE supplier_id = :s", {"s": sid})
    if not prof:
        raise KeyError(f"Unknown supplier {sid}")
    card = next((c for c in A.scorecard() if c["supplier_id"] == sid), {})
    with read_engine.connect() as c:
        contact = c.execute(_t("SELECT contact_name, email FROM public.supplier_contacts WHERE supplier_id = :s"), {"s": sid}).mappings().fetchone() \
            if c.execute(_t("SELECT to_regclass('public.supplier_contacts')")).scalar() else None
        portal = [dict(r) for r in c.execute(_t("SELECT display_name, username FROM public.app_users WHERE role = 'SUPPLIER' AND supplier_id = :s AND active"), {"s": sid}).mappings()]
        owners = [dict(r) for r in c.execute(_t("""SELECT display_name, role FROM public.app_users WHERE role IN ('PROCUREMENT', 'QUALITY') AND active
                                                   ORDER BY role, display_name""")).mappings()]
    monthly = A._run(f"""
      SELECT to_char(date_trunc('month', receipt_date), 'YYYY-MM') AS month, material_id, MIN(material_name) AS material_name,
             SUM(received_qty) AS qty, SUM(receipt_value_inr) AS value_inr, COUNT(*) AS lines,
             ROUND(100.0 * AVG(otif::int) FILTER (WHERE measurable), 1) AS otif_pct
      FROM {A.S}.v_po_delivery WHERE supplier_id = :s AND delivery_status = 'RECEIVED' AND receipt_date IS NOT NULL
      GROUP BY 1, 2 ORDER BY 1, 2""", {"s": sid})
    mats = A._run(f"""
      SELECT d.material_id, MIN(d.material_name) AS material_name, MIN(d.material_category) AS category,
             SUM(d.received_qty) FILTER (WHERE d.delivery_status = 'RECEIVED') AS qty_received,
             SUM(d.receipt_value_inr) AS value_inr, COUNT(*) AS lines,
             COUNT(*) FILTER (WHERE d.delivery_status = 'PAST_DUE') AS past_due,
             ROUND(100.0 * AVG(d.otif::int) FILTER (WHERE d.measurable), 1) AS otif_pct,
             (SELECT ROUND(SUM(a.attribution_weight)::numeric, 1) FROM {A.S}.v_defect_attribution a
               WHERE a.supplier_id = :s AND a.material_id = d.material_id) AS defects
      FROM {A.S}.v_po_delivery d WHERE d.supplier_id = :s GROUP BY d.material_id ORDER BY SUM(d.receipt_value_inr) DESC NULLS LAST""", {"s": sid})
    recent = A._run(f"""
      SELECT po_id, po_line, material_id, material_name, plant_id, promised_date, receipt_date, received_qty, receipt_value_inr,
             otif, delay_days, delivery_status
      FROM {A.S}.v_po_delivery WHERE supplier_id = :s AND (delivery_status = 'RECEIVED' OR delivery_status = 'PAST_DUE')
      ORDER BY COALESCE(receipt_date, promised_date) DESC LIMIT 20""", {"s": sid})
    keep = ("grade", "composite_score", "performance_rank", "risk_rank", "otif_pct", "on_time_pct", "in_full_pct", "incoming_ppm",
            "avg_lead_time_variance_days", "open_ncr", "overdue_capa", "past_due_lines", "spend_inr", "health", "disruption_hours", "scores")
    site = SUPPLIER_SITES.get(sid)
    return {"profile": {k: v for k, v in prof[0].items() if not k.startswith("_")},
            "site": {"city": site[0], "lat": site[1], "lon": site[2]} if site else None,
            "contact": dict(contact) if contact else None, "portal_users": portal, "owners": owners,
            "scorecard": {k: card.get(k) for k in keep}, "materials": mats, "monthly": monthly, "recent": recent,
            "as_of_date": A.as_of_date()}
