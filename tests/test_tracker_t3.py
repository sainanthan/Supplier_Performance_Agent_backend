"""Network twin T3 tests: GPS tracking, disruption providers, source-record endpoints, saved views, access policy."""
import datetime as dt

import pytest

from app import auth as AU
from app import disruptions as D


# ---------------------------------------------------------------- unit (no database)
def test_policy_t3():
    assert AU.allowed("POST", "/api/gps/pings", None)               # device-key auth inside the endpoint
    assert AU.allowed("GET", "/api/gps/trip", None)
    assert not AU.allowed("POST", "/api/vehicles", "QUALITY")
    assert AU.allowed("POST", "/api/vehicles", "PROCUREMENT")
    assert not AU.allowed("POST", "/api/disruptions/refresh", "PROCUREMENT")
    assert AU.allowed("POST", "/api/disruptions", "PROCUREMENT") and not AU.allowed("POST", "/api/disruptions", "QUALITY")
    assert AU.allowed("POST", "/api/saved-views", "VIEWER") and not AU.allowed("GET", "/api/saved-views", "SUPPLIER")


def test_gdacs_normalisation(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self):
            return {"features": [
                {"geometry": {"type": "Point", "coordinates": [121.0, 14.5]}, "properties": {"eventtype": "TC", "eventid": 1, "episodeid": 2, "alertlevel": "Red", "name": "Cyclone X"}},
                {"geometry": {"type": "Point", "coordinates": [-75.0, 40.0]}, "properties": {"eventtype": "EQ", "eventid": 3, "episodeid": 1, "alertlevel": "Orange"}},   # outside bbox
                {"geometry": {"type": "Point", "coordinates": [80.3, 13.1]}, "properties": {"eventtype": "FL", "eventid": 4, "episodeid": 1, "alertlevel": "Green"}}]}
    monkeypatch.setattr(D.httpx, "get", lambda *a, **k: R())
    ev = D.GDACS().fetch([])
    assert [e["type"] for e in ev] == ["Cyclone", "Flood"]                 # US event filtered out by the network bbox
    assert ev[0]["severity"] == "CRITICAL" and ev[0]["radius_km"] == 450   # red cyclone: 300 km x 1.5
    assert ev[1]["severity"] == "LOW"                                      # GDACS green = low impact


def test_openmeteo_thresholds(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self):
            return [{"daily": {"time": ["2026-11-13", "2026-11-14"], "precipitation_sum": [12, 130], "wind_gusts_10m_max": [30, 40]}}]
    monkeypatch.setattr(D.httpx, "get", lambda *a, **k: R())
    ev = D.OpenMeteo().fetch([{"id": "PORT-CHENNAI", "kind": "port", "name": "Chennai port", "lat": 13.1, "lon": 80.3}])
    assert len(ev) == 1 and ev[0]["type"] == "Heavy rain" and ev[0]["severity"] == "HIGH"


# ---------------------------------------------------------------- integration (real database)
def _db():
    try:
        from app import analytics as A
        A._run("SELECT 1", {}, True)
        return True
    except Exception:
        return False


db = pytest.mark.skipif(not _db(), reason="database not reachable")


@pytest.fixture
def flags():
    from app.db import init_db
    from app.features import all_flags, set_flag
    init_db()
    keys = ("gps_tracking", "twin_side_panel", "twin_filters", "twin_disruptions", "network_twin_v2")
    before = {k: all_flags(force=True).get(k, False) for k in keys}
    for k in keys:
        set_flag(k, True, "pytest")
    yield
    for k, v in before.items():
        set_flag(k, v, "pytest")


@db
def test_gps_trip_lifecycle_and_live_position(flags):
    from sqlalchemy import text
    from app import gps as G
    from app import tracker as TR
    from app.db import engine
    v = G.create_vehicle("TRUCK", "PYTEST-001", "pytest", "SIMULATOR", "pytest")
    try:
        veh = G.vehicle_for_key(v["device_key"])
        assert veh and veh["vehicle_id"] == v["vehicle_id"] and G.vehicle_for_key("wrong") is None
        assert G.ingest(veh, [{"lat": 12.9, "lon": 79.9}])["accepted"] == 0           # no trip -> rejected
        n = TR._network(True, True)
        s = next(x for x in n["shipments"] if x["lane"].endswith("IN-TN-01"))
        G.start_trip(v["vehicle_id"], [s["id"]], "pytest")
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        r = G.ingest(veh, [{"ts": now, "lat": 12.9, "lon": 79.9, "speed_kmh": 50}, {"ts": now, "lat": 99, "lon": 1},
                           {"ts": now, "lat": 12.9, "lon": 79.9, "speed_kmh": 400}])
        assert r["accepted"] == 1 and r["rejected"] == 2
        n2 = TR._network(True, True)
        TR.apply_gps(n2)
        t = next(x for x in n2["shipments"] if x["id"] == s["id"])
        assert t["pos_source"] == "LIVE" and t["demo"] is True and t["gps"][0][1:3] == [79.9, 12.9]
        assert t["vehicle"]["vehicle_id"] == v["vehicle_id"] and n2["demo_data"] and "t_now" in n2
        G.end_trip(v["vehicle_id"])
        assert G.ingest(veh, [{"lat": 12.9, "lon": 79.9}])["accepted"] == 0           # trip ended -> rejected
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM public.gps_pings WHERE vehicle_id = :v"), {"v": v["vehicle_id"]})
            c.execute(text("DELETE FROM public.vehicle_trips WHERE vehicle_id = :v"), {"v": v["vehicle_id"]})
            c.execute(text("DELETE FROM public.vehicles WHERE vehicle_id = :v"), {"v": v["vehicle_id"]})


@db
def test_manual_disruption_impact():
    from app import tracker as TR
    n = TR._network(True, True)
    eid = D.add_manual("Port congestion", "HIGH", "pytest congestion", 13.1, 80.3, 30, None, "pytest")
    try:
        e = next(x for x in D.active(n["lanes"], n["nodes"])["events"] if x["id"] == eid)
        assert "PORT-CHENNAI" in e["nodes"] and e["lanes"] and e["provider"] == "MANUAL" and e["fetched_at"]
    finally:
        D.deactivate(eid)
    assert not any(x["id"] == eid for x in D.active()["events"])


@db
def test_source_endpoints_and_saved_views(flags):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    H = lambda u, r: {"Authorization": "Bearer " + AU.issue_token(AU.UserCtx(u, r))}
    q, p = H("quality", "QUALITY"), H("procurement", "PROCUREMENT")
    d = c.get("/api/tracker", headers=q).json()
    s = d["shipments"][0]
    src = c.get(f"/api/tracker/shipments/{s['po_id']}/{s['id'].rsplit('-', 1)[1]}/source", headers=q).json()
    assert src["po_line"]["po_id"] == s["po_id"] and src["erp_record"]["po_id"] == s["po_id"]
    lane = next(l for l in d["lanes"] if l["id"] == s["lane"])
    lr = c.get(f"/api/tracker/lanes/{lane['supplier_id']}/{lane['plant_id']}/records", headers=q).json()
    assert lr["on_time_pct"] == lane["on_time_pct"] and lr["row_count"] == lane["shipments"]
    vid = c.post("/api/saved-views", headers=q, json={"page": "tracker", "name": "pytest view", "payload": {"filters": {"mode": "SEA"}}}).json()["id"]
    assert any(v["id"] == vid for v in c.get("/api/saved-views", headers=q).json())
    assert not any(v["id"] == vid for v in c.get("/api/saved-views", headers=p).json())     # private
    assert c.delete(f"/api/saved-views/{vid}", headers=p).status_code == 404                  # not the owner
    assert c.delete(f"/api/saved-views/{vid}", headers=q).status_code == 200
