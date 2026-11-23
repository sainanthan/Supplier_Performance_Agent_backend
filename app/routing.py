"""Real route geometry for the network twin (T2), pluggable and cached.

Road:  OSRM (default: public demo server, override OSRM_URL for a self-hosted instance) or OpenRouteService when
       ORS_API_KEY is set. Only COORDINATES are sent (approved): no names, PO data or quantities.
Sea:   `searoute` (open-source, offline; real shipping-lane network) - nothing leaves the server.

Every route is cached in public.route_cache by its rounded endpoints and is recomputed only when an endpoint changes
(or on an explicit refresh). Requests never wait for the network: the tracker reads the cache and falls back to the
approximate waypoint path (labelled quality=APPROXIMATE) while a background refresh fills it. Each refresh is logged in
public.job_runs (job = "feed:routes").
"""
from __future__ import annotations

import json
import math
import os
import threading
import time

import httpx
from sqlalchemy import text

from .db import engine

OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
ORS_KEY = os.environ.get("ORS_API_KEY")
_lock = threading.Lock()
_running = [False]


def _key(mode: str, a: tuple, b: tuple) -> str:
    return f"{mode}|{a[0]:.4f},{a[1]:.4f}|{b[0]:.4f},{b[1]:.4f}"


def _rdp(pts: list, eps: float) -> list:
    """Ramer-Douglas-Peucker simplification on (lat, lon) points (eps in degrees)."""
    if len(pts) < 3:
        return pts
    (ax, ay), (bx, by) = pts[0], pts[-1]
    dx, dy = bx - ax, by - ay
    n = math.hypot(dx, dy) or 1e-12
    dmax, idx = 0.0, 0
    for i in range(1, len(pts) - 1):
        d = abs(dy * (pts[i][0] - ax) - dx * (pts[i][1] - ay)) / n
        if d > dmax:
            dmax, idx = d, i
    if dmax <= eps:
        return [pts[0], pts[-1]]
    return _rdp(pts[: idx + 1], eps)[:-1] + _rdp(pts[idx:], eps)


def cached(mode: str, a: tuple, b: tuple) -> dict | None:
    with engine.connect() as c:
        r = c.execute(text("SELECT * FROM public.route_cache WHERE key = :k"), {"k": _key(mode, a, b)}).mappings().fetchone()
    if not r:
        return None
    geo = r["geometry"] if isinstance(r["geometry"], list) else json.loads(r["geometry"])
    return {"coords": geo, "km": r["distance_km"], "hours": r["duration_h"], "provider": r["provider"], "quality": "ROUTED"}


def _store(mode: str, a: tuple, b: tuple, coords: list, km: float, hours: float | None, provider: str) -> None:
    with engine.begin() as c:
        c.execute(text("""INSERT INTO public.route_cache (key, mode, provider, geometry, distance_km, duration_h, fetched_at)
                          VALUES (:k, :m, :p, CAST(:g AS jsonb), :d, :h, now())
                          ON CONFLICT (key) DO UPDATE SET provider = EXCLUDED.provider, geometry = EXCLUDED.geometry,
                            distance_km = EXCLUDED.distance_km, duration_h = EXCLUDED.duration_h, fetched_at = now()"""),
                  {"k": _key(mode, a, b), "m": mode, "p": provider, "g": json.dumps(coords), "d": km, "h": hours})


def fetch_road(a: tuple, b: tuple) -> dict:
    """Driving route a -> b. Coordinates only are sent to the provider."""
    if ORS_KEY:
        r = httpx.post("https://api.openrouteservice.org/v2/directions/driving-hgv/geojson", timeout=30,
                       headers={"Authorization": ORS_KEY}, json={"coordinates": [[a[1], a[0]], [b[1], b[0]]]})
        r.raise_for_status()
        f = r.json()["features"][0]
        coords, km, hours, prov = f["geometry"]["coordinates"], f["properties"]["summary"]["distance"] / 1000, \
            f["properties"]["summary"]["duration"] / 3600, "openrouteservice"
    else:
        r = httpx.get(f"{OSRM_URL}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}",
                      params={"overview": "full", "geometries": "geojson"}, timeout=30,
                      headers={"User-Agent": "supplier-performance-agent/2.0"})
        r.raise_for_status()
        d = r.json()
        if d.get("code") != "Ok":
            raise RuntimeError(d.get("code"))
        rt = d["routes"][0]
        coords, km, hours, prov = rt["geometry"]["coordinates"], rt["distance"] / 1000, rt["duration"] / 3600, "osrm"
    pts = _rdp([(round(lat, 5), round(lon, 5)) for lon, lat in coords], 0.003)       # ~300 m tolerance
    return {"coords": [list(p) for p in pts], "km": round(km, 1), "hours": round(hours, 2), "provider": prov}


def fetch_sea(a: tuple, b: tuple) -> dict:
    import searoute as sr
    f = sr.searoute([a[1], a[0]], [b[1], b[0]], units="km")
    pts = [(round(lat, 4), round(lon, 4)) for lon, lat in f.geometry["coordinates"]]
    return {"coords": [list(p) for p in _rdp(pts, 0.05)], "km": round(f.properties["length"], 1), "hours": None, "provider": "searoute"}


def refresh(legs: list[tuple], force: bool = False, trigger: str = "auto") -> dict:
    """Fetch every missing (or, with force, every) leg [(mode, a, b)] and log the run. Runs one at a time."""
    from .db import JobRun, SessionLocal, now
    with _lock:
        if _running[0]:
            return {"status": "already running"}
        _running[0] = True
    stats = {"fetched": 0, "cached": 0, "failed": 0, "errors": [], "trigger": trigger,
             "provider": "openrouteservice" if ORS_KEY else f"osrm ({OSRM_URL})", "sea_provider": "searoute (offline)"}
    with SessionLocal() as s:
        jr = JobRun(job="feed:routes", status="RUNNING", started_at=now(), detail=stats)
        s.add(jr); s.commit(); jid = jr.id
    try:
        for mode, a, b in dict.fromkeys(legs):                  # de-duplicated, order kept
            if not force and cached(mode, a, b):
                stats["cached"] += 1
                continue
            try:
                r = fetch_sea(a, b) if mode == "SEA" else fetch_road(a, b)
                _store(mode, a, b, r["coords"], r["km"], r["hours"], r["provider"])
                stats["fetched"] += 1
                if mode == "ROAD" and not ORS_KEY:
                    time.sleep(1.1)                             # public OSRM demo: be polite (about 1 request/s)
            except Exception as e:  # noqa: BLE001
                stats["failed"] += 1
                stats["errors"].append(f"{mode} {a}->{b}: {str(e)[:120]}")
    finally:
        with SessionLocal() as s:
            jr = s.get(JobRun, jid)
            jr.status = "OK" if not stats["failed"] else ("PARTIAL" if stats["fetched"] or stats["cached"] else "FAILED")
            jr.detail, jr.finished_at = {**stats, "errors": stats["errors"][:10]}, now()
            s.commit()
        _running[0] = False
    return stats


def refresh_async(legs: list[tuple], force: bool = False, trigger: str = "auto", after=None) -> None:
    def run():
        refresh(legs, force, trigger)
        if after:
            after()
    threading.Thread(target=run, daemon=True, name="route-refresh").start()
