"""Network twin T2 tests: route helpers (no DB) + real-route lanes and milestone positions (DB)."""
import pytest

from app import routing as R
from app import tracker as TR


# ---------------------------------------------------------------- unit
def test_rdp_keeps_endpoints_and_drops_collinear_points():
    pts = [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (1.0, 2.0)]
    out = R._rdp(pts, 0.01)
    assert out[0] == pts[0] and out[-1] == pts[-1] and (0.5, 0.5) not in out and (1.0, 1.0) in out


def test_cache_key_rounds_coordinates():
    assert R._key("ROAD", (12.840001, 79.95), (12.97, 77.59)) == R._key("ROAD", (12.84, 79.950004), (12.97, 77.59))
    assert R._key("ROAD", (1, 2), (3, 4)) != R._key("SEA", (1, 2), (3, 4))


def test_lane_legs_shape():
    sea = TR.lane_legs("SUP-001", "IN-TN-01", {})
    assert [m for m, _a, _b in sea] == ["ROAD", "SEA", "ROAD"]
    assert sea[0][2] == sea[1][1] and sea[1][2] == sea[2][1]          # legs chain end-to-start
    assert [m for m, _a, _b in TR.lane_legs("SUP-004", "IN-KA-01", {})] == ["ROAD"]


# ---------------------------------------------------------------- integration
def _db():
    try:
        from app import analytics as A
        A._run("SELECT 1", {}, True)
        return True
    except Exception:
        return False


db = pytest.mark.skipif(not _db(), reason="database not reachable")


@db
def test_v3_lanes_from_routes_with_fallback_labelled():
    from app.db import init_db
    init_db()
    n = TR._network(True, True)
    assert n["version"] == 3
    for l in n["lanes"]:
        assert l["route_quality"] in ("ROUTED", "APPROXIMATE")
        assert all(s["quality"] in ("ROUTED", "APPROXIMATE") for s in l["segments"])
        assert abs(sum(s["km"] for s in l["segments"]) - l["distance_km"]) <= 1
        if l["mode"] == "SEA":
            assert 0 < l["sea_start"] < l["sea_end"] < len(l["path"]) - 1
            assert [s["mode"] for s in l["segments"]] == ["ROAD", "SEA", "ROAD"]
        if l["route_quality"] == "ROUTED":
            assert {s["provider"] for s in l["segments"]} <= {"osrm", "openrouteservice", "searoute"}


@db
def test_milestones_drive_position_source_and_movements_exclude_tracked():
    from sqlalchemy import text
    from app.db import engine
    n = TR._network(True, True)
    s = next(x for x in n["shipments"] if n["t_as_of"] - 3 < x["t_depart"] < n["t_as_of"] and x["t_eta"] > n["t_as_of"])
    po, line = s["id"].rsplit("-", 1)
    with engine.begin() as c:
        c.execute(text("DELETE FROM sp.fact_inbound_milestone WHERE source = 'PYTEST'"))
        c.execute(text("""INSERT INTO sp.fact_inbound_milestone (shipment_ref, po_id, po_line, milestone, milestone_ts, source)
                          VALUES (:r, :p, :l, 'DEPARTED_SUPPLIER', CAST(:ts AS timestamp), 'PYTEST')"""),
                  {"r": s["id"], "p": po, "l": int(line), "ts": f"{n['start']} 00:00"})
    try:
        n2 = TR._network(True, True)
        TR.apply_milestones(n2)
        t = next(x for x in n2["shipments"] if x["id"] == s["id"])
        assert t["pos_source"] == "MILESTONE" and t["track"][0][1] == 0.0 and t["track"][-1] == [t["t_eta"], 1.0]
        assert not t.get("demo")                                        # only SYNTHETIC rows are flagged as demo
        mv = TR.movements(n2)
        assert s["id"] not in {i for m in mv for i in m["shipment_ids"]}  # tracked shipments are drawn individually
        assert sum(m["count"] for m in mv) == n2["position_sources"]["ESTIMATED"]
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM sp.fact_inbound_milestone WHERE source = 'PYTEST'"))


@db
def test_route_refresh_is_logged():
    from app import analytics as A
    runs = A._run("SELECT status, detail FROM public.job_runs WHERE job = 'feed:routes' ORDER BY id DESC LIMIT 1", {})
    assert runs and runs[0]["status"] in ("OK", "PARTIAL", "FAILED")
