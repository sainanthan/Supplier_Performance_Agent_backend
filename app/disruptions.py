"""Disruption overlays (T3) with pluggable providers.

Each provider returns normalised events: id, provider, type, severity (LOW | MEDIUM | HIGH | CRITICAL), title, lat, lon,
radius_km, starts, ends, url. Events are stored in public.disruption_events with the provider's refresh time; every
refresh is logged in public.job_runs (job = "feed:disruptions"). The map always shows the SOURCE and REFRESH TIME.

Free providers included:
  - GDACS   (EU JRC Global Disaster Alert & Coordination System): cyclones, floods, earthquakes, volcanoes, drought.
            Nothing about the network is sent (a bounding-box query only).
  - OPENMETEO (Open-Meteo forecast API): heavy rain / high wind at the network's ports and plants for the next 3 days.
            Only node coordinates are sent (approved, like routing).
  - MANUAL  (entered by procurement / admin): strikes, port congestion, road closures - no free reliable feed exists.
Paid candidates (pluggable, not enabled): port congestion - Portcast, MarineTraffic/Kpler; risk intelligence -
Everstream, Resilinc, Interos, Riskmethods; news - GDELT (free, noisy). Add a class with fetch() and register it in PROVIDERS.
"""
from __future__ import annotations

import datetime as dt
import json
import math

import httpx
from sqlalchemy import text

from .db import engine

BBOX = (60.0, -12.0, 150.0, 46.0)        # lon_min, lat_min, lon_max, lat_max - Asia network extent
SEV_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


class GDACS:
    name, label, url = "GDACS", "GDACS (EU JRC) disaster alerts", "https://www.gdacs.org"
    TYPES = {"TC": "Cyclone", "FL": "Flood", "EQ": "Earthquake", "VO": "Volcano", "DR": "Drought", "WF": "Wildfire"}

    def fetch(self, nodes: list[dict]) -> list[dict]:
        today = dt.date.today()
        r = httpx.get("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH", timeout=40, params={
            "eventlist": "TC;FL;EQ;VO;DR;WF", "fromDate": (today - dt.timedelta(days=21)).isoformat(), "toDate": today.isoformat(),
            "alertlevel": "Green;Orange;Red"}, headers={"Accept": "application/json"})
        r.raise_for_status()
        out = []
        for f in r.json().get("features", []):
            p, g = f.get("properties", {}), f.get("geometry", {})
            if g.get("type") != "Point":
                continue
            lon, lat = g["coordinates"][:2]
            if not (BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]):
                continue
            lvl = (p.get("alertlevel") or "Green").lower()
            sev = {"red": "CRITICAL", "orange": "HIGH"}.get(lvl, "LOW")          # GDACS green = low humanitarian impact
            et = p.get("eventtype", "")
            radius = {"TC": 300, "FL": 150, "EQ": 200, "VO": 60, "DR": 400, "WF": 50}.get(et, 100) * (1.5 if sev == "CRITICAL" else 1)
            out.append({"ext_id": f"{et}-{p.get('eventid')}-{p.get('episodeid')}", "type": self.TYPES.get(et, et), "severity": sev,
                        "title": p.get("name") or p.get("description") or self.TYPES.get(et, "Event"), "lat": lat, "lon": lon, "radius_km": radius,
                        "starts": p.get("fromdate"), "ends": p.get("todate"),
                        "url": (p.get("url") or {}).get("report") if isinstance(p.get("url"), dict) else self.url})
        return out


class OpenMeteo:
    name, label, url = "OPENMETEO", "Open-Meteo weather forecast (3 days)", "https://open-meteo.com"
    RAIN_MM, GUST_KMH = 50.0, 62.0       # thresholds for "heavy rain" / "high wind" (IMD heavy-rain ~64.5 mm/day)

    def fetch(self, nodes: list[dict]) -> list[dict]:
        pts = [n for n in nodes if n["kind"] in ("port", "plant")]
        if not pts:
            return []
        r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=30, params={
            "latitude": ",".join(str(round(n["lat"], 2)) for n in pts), "longitude": ",".join(str(round(n["lon"], 2)) for n in pts),
            "daily": "precipitation_sum,wind_gusts_10m_max", "forecast_days": 3, "timezone": "UTC"})
        r.raise_for_status()
        data = r.json()
        data = data if isinstance(data, list) else [data]
        out = []
        for n, d in zip(pts, data):
            daily = d.get("daily", {})
            for day, rain, gust in zip(daily.get("time", []), daily.get("precipitation_sum", []), daily.get("wind_gusts_10m_max", [])):
                rain, gust = rain or 0, gust or 0
                if rain < self.RAIN_MM and gust < self.GUST_KMH:
                    continue
                sev = "HIGH" if rain >= 115 or gust >= 90 else "MEDIUM"
                kind = "Heavy rain" if rain >= self.RAIN_MM else "High wind"
                out.append({"ext_id": f"{n['id']}-{day}-{kind}", "type": kind, "severity": sev,
                            "title": f"{kind} forecast at {n['name']} ({rain:.0f} mm, gusts {gust:.0f} km/h)", "lat": n["lat"], "lon": n["lon"],
                            "radius_km": 60, "starts": day, "ends": day, "url": self.url})
        return out


PROVIDERS = {"GDACS": GDACS(), "OPENMETEO": OpenMeteo()}


def refresh(nodes: list[dict], trigger: str = "auto") -> dict:
    """Fetch every provider, replace its active events, log one job run with per-provider results."""
    from .db import JobRun, SessionLocal, now
    res = {}
    for key, prov in PROVIDERS.items():
        try:
            ev = prov.fetch(nodes)
            with engine.begin() as c:
                c.execute(text("UPDATE public.disruption_events SET active = FALSE WHERE provider = :p"), {"p": key})
                for e in ev:
                    c.execute(text("""
                      INSERT INTO public.disruption_events (event_key, provider, type, severity, title, lat, lon, radius_km, starts, ends, url, active, fetched_at)
                      VALUES (:k, :p, :t, :s, :ti, :la, :lo, :r, :st, :en, :u, TRUE, now())
                      ON CONFLICT (event_key) DO UPDATE SET severity = EXCLUDED.severity, title = EXCLUDED.title, lat = EXCLUDED.lat,
                        lon = EXCLUDED.lon, radius_km = EXCLUDED.radius_km, ends = EXCLUDED.ends, active = TRUE, fetched_at = now()"""),
                              {"k": f"{key}:{e['ext_id']}", "p": key, "t": e["type"], "s": e["severity"], "ti": str(e["title"])[:300],
                               "la": e["lat"], "lo": e["lon"], "r": e["radius_km"], "st": str(e.get("starts") or "")[:40],
                               "en": str(e.get("ends") or "")[:40], "u": e.get("url")})
            res[key] = {"status": "OK", "events": len(ev)}
        except Exception as e:  # noqa: BLE001
            res[key] = {"status": "FAILED", "error": str(e)[:200]}
    with SessionLocal() as s:
        st = "OK" if all(v["status"] == "OK" for v in res.values()) else "PARTIAL" if any(v["status"] == "OK" for v in res.values()) else "FAILED"
        s.add(JobRun(job="feed:disruptions", status=st, detail={"trigger": trigger, "providers": res}, started_at=now(), finished_at=now()))
        s.commit()
    return res


def _km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def active(lanes: list[dict] | None = None, nodes: list[dict] | None = None) -> dict:
    """Active events (+ which lanes / nodes each touches) and the last refresh time per provider."""
    with engine.connect() as c:
        ev = [dict(r) for r in c.execute(text("SELECT * FROM public.disruption_events WHERE active ORDER BY fetched_at DESC")).mappings()]
        last = {r[0]: r[1] for r in c.execute(text("SELECT provider, MAX(fetched_at) FROM public.disruption_events GROUP BY provider"))}
        runs = c.execute(text("SELECT detail, started_at, status FROM public.job_runs WHERE job = 'feed:disruptions' ORDER BY id DESC LIMIT 1")).fetchone()
    for e in ev:
        e["lanes"] = sorted({l["id"] for l in lanes or [] if any(_km((e["lat"], e["lon"]), p) <= e["radius_km"] for p in l["path"][::3] + [l["path"][-1]])})
        e["nodes"] = sorted({n["id"] for n in nodes or [] if _km((e["lat"], e["lon"]), (n["lat"], n["lon"])) <= e["radius_km"]})
    provs = [{"provider": k, "label": p.label, "url": p.url, "last_event_at": last.get(k)} for k, p in PROVIDERS.items()]
    provs.append({"provider": "MANUAL", "label": "Manual (procurement / admin)", "url": None, "last_event_at": last.get("MANUAL")})
    return {"events": ev, "providers": provs,
            "last_refresh": {"at": runs[1], "status": runs[2], "detail": runs[0]} if runs else None}


def add_manual(type_: str, severity: str, title: str, lat: float, lon: float, radius_km: float, ends: str | None, actor: str) -> int:
    if severity not in SEV_ORDER:
        raise ValueError("severity must be LOW, MEDIUM, HIGH or CRITICAL")
    with engine.begin() as c:
        return c.execute(text("""INSERT INTO public.disruption_events (event_key, provider, type, severity, title, lat, lon, radius_km, starts, ends, active, fetched_at, created_by)
                                 VALUES (:k, 'MANUAL', :t, :s, :ti, :la, :lo, :r, :st, :en, TRUE, now(), :a) RETURNING id"""),
                         {"k": f"MANUAL:{dt.datetime.now().timestamp()}", "t": type_, "s": severity, "ti": title[:300], "la": lat, "lo": lon,
                          "r": radius_km, "st": dt.date.today().isoformat(), "en": ends or "", "a": actor}).scalar()


def deactivate(event_id: int) -> None:
    with engine.begin() as c:
        c.execute(text("UPDATE public.disruption_events SET active = FALSE WHERE id = :i"), {"i": event_id})


_ = json  # (kept for providers that return JSON strings)
