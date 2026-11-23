"""Plant twin T4 tests against the loaded data: every figure re-derived from its source rows, rule consistency,
historical dates, endpoint gating / validation, GPS retention job."""
import pytest

from app import plant as PL


def _db():
    try:
        from app import analytics as A
        A._run("SELECT 1", {}, True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db(), reason="database not reachable")


@pytest.fixture(scope="module")
def v():
    from app.db import init_db
    init_db()
    return PL.view("IN-TN-01")


def test_status_rules_follow_approved_thresholds(v):
    th = v["thresholds"]
    for m in v["materials"]:
        c = m["days_cover"]
        expect = "critical" if (c is not None and c < th["critical"]) or m["below_safety"] else "watch" if c is not None and c < th["watch"] else "ok"
        assert m["status"] == expect, m["material_id"]
        assert m["below_safety"] == (m["safety"] is not None and m["usable"] < m["safety"])


def test_figures_trace_to_source_rows(v):
    m = v["materials"][0]
    stock = PL.records("IN-TN-01", "stock", v["date"], m["material_id"])["rows"]
    assert abs(sum(r["on_hand_qty"] - r["allocated_qty"] - r["blocked_qty"] - r["quality_hold_qty"] for r in stock) - m["usable"]) < 1e-6
    usage = PL.records("IN-TN-01", "usage", v["date"], m["material_id"])["rows"]
    assert abs(sum(r["received_qty"] for r in usage) / 90 - m["daily_usage"]) < 0.06
    orders = PL.records("IN-TN-01", "orders", v["date"], m["material_id"])["rows"]
    assert len({r["production_order_id"] for r in orders}) == m["open_prod_orders"]


def test_inbound_window_and_line_exposure(v):
    for i in v["inbound"]:
        assert 0 < i["days_ahead"] <= 4 and i["status"] in ("ok", "risk", "late")
        assert i["within_24h"] == (i["days_ahead"] < 2)            # ETA tomorrow = within the next 24 h after the selected day
    mats = {m["material_id"]: m for m in v["materials"]}
    for l in v["lines"]:
        for mid in l["exposed_materials"]:
            assert l["line_id"] in mats[mid]["lines"]
            assert mats[mid]["status"] == "critical" or mats[mid]["runs_out_before_next_arrival"]
        assert (l["status"] == "exposed") == bool(l["exposed_materials"])
    assert v["summary"]["exposed_lines"] == sum(l["status"] == "exposed" for l in v["lines"])


def test_historical_date_uses_that_snapshot():
    h = PL.view("IN-KA-01", "2026-09-15")
    assert h["snapshot_date"] <= "2026-09-15" and h["date"] == "2026-09-15"


def test_endpoint_gating_and_config_validation():
    from fastapi.testclient import TestClient
    from app import auth as AU
    from app.features import all_flags, set_flag
    from app.main import app
    c = TestClient(app)
    adm = {"Authorization": "Bearer " + AU.issue_token(AU.UserCtx("pytest-admin", "ADMIN"))}
    sup = {"Authorization": "Bearer " + AU.issue_token(AU.UserCtx("supplier.sup001", "SUPPLIER", "SUP-001"))}
    before = all_flags(force=True).get("plant_twin", False)
    try:
        set_flag("plant_twin", False, "pytest")
        assert c.get("/api/plants/IN-TN-01/twin", headers=adm).status_code == 404
        set_flag("plant_twin", True, "pytest")
        assert c.get("/api/plants/IN-TN-01/twin", headers=sup).status_code == 403          # suppliers never see plant internals
        assert c.get("/api/plants/IN-TN-01/twin/records", headers=adm, params={"kind": "salary"}).status_code == 422
        assert c.get("/api/plants/XX-00/twin", headers=adm).status_code == 404
    finally:
        set_flag("plant_twin", before, "pytest")
    cfg = c.get("/api/config/scorecard", headers=adm).json()
    bad = {"weights": cfg["weights"], "plant_cover_critical_days": 5, "plant_cover_watch_days": 3, "note": "pytest invalid"}
    assert c.put("/api/config/scorecard", headers=adm, json=bad).status_code == 422          # watch must exceed critical


def test_gps_retention_job_deletes_only_old_pings():
    from sqlalchemy import text
    from app.db import engine
    from app.jobs import job_gps_retention
    with engine.begin() as c:
        c.execute(text("""INSERT INTO public.gps_pings (vehicle_id, trip_id, ts, lat, lon, source) VALUES
                          ('PYTEST-RET', -1, now() - interval '400 days', 1, 1, 'SIMULATOR'), ('PYTEST-RET', -1, now(), 1, 1, 'SIMULATOR')"""))
    try:
        assert job_gps_retention()["deleted_pings"] >= 1
        with engine.connect() as c:
            left = c.execute(text("SELECT COUNT(*) FROM public.gps_pings WHERE vehicle_id = 'PYTEST-RET'")).scalar()
        assert left == 1
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM public.gps_pings WHERE vehicle_id = 'PYTEST-RET'"))
