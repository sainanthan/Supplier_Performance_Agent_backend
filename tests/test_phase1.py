"""Phase 1 tests: safe query layer, dashboards, actionable alerts, review-pack export, feature flags, migrations.

Unit tests need no database; the integration tests run against the loaded Neon data (skipped if unreachable).
    cd backend && .venv/Scripts/python -m pytest -q tests/test_phase1.py
"""
import time

import pytest

from app import alert_events as AE
from app import auth as AU
from app import metrics as MX
from app.migrations import FLAGS, MIGRATIONS


# ---------------------------------------------------------------- unit
def test_every_migration_has_a_rollback():
    assert all(down for _id, _up, down in MIGRATIONS)
    assert len({m[0] for m in MIGRATIONS}) == len(MIGRATIONS)


def test_phase1_flags_declared():
    assert {"safe_query_layer", "chat_charts", "role_dashboards", "actionable_alerts", "review_pack_export"} <= {f[0] for f in FLAGS}


@pytest.mark.parametrize("metric,group_by,filters", [
    ("otif_pct", ["supplier; DROP TABLE x"], {}),
    ("not_a_metric", [], {}),
    ("otif_pct", [], {"supplier_id": "SUP-001' OR '1'='1"}),
    ("otif_pct", [], {"start": "yesterday"}),
    ("otif_pct", [], {"raw_sql": "select 1"}),
    ("disruption_hours", ["material"], {}),
    ("ppm", ["defect"], {}),
    ("otif_pct", ["supplier", "plant"], {}),
])
def test_metric_layer_rejects_unsafe_input(metric, group_by, filters):
    with pytest.raises(MX.MetricError):
        MX._validate(metric, group_by, filters)


def test_metric_sql_uses_bound_parameters_only():
    m, src, dims, grain, f = MX._validate("otif_pct", ["month", "supplier"], {"supplier_id": "SUP-001", "start": "2026-08-01"})
    sql, params = MX._sql("otif_pct", m, src, dims, grain, f)
    assert "SUP-001" not in sql and params["supplier_id"] == "SUP-001"
    assert "sp.v_po_delivery" in sql and "fact_" not in sql


def test_alert_key_is_stable():
    a = {"type": "REPEATED_DEFECT", "supplier_id": "SUP-001", "metric": {"defect_code": "D008"}, "title": "x"}
    b = dict(a, title="y", severity="HIGH")              # title / severity changes must not create a new event
    assert AE.alert_key(a) == AE.alert_key(b) == "REPEATED_DEFECT|SUP-001|D008"


def test_signed_alert_link_roundtrip_and_tamper():
    url = AE.make_link(42, "ack", "QUALITY")
    tok = url.rsplit("/", 1)[1]
    d = AE.read_link(tok)
    assert d["e"] == 42 and d["a"] == "ack"
    body, sig = tok.split(".")
    assert AE.read_link(body + "." + sig[:-3] + "abc") is None
    assert AE.read_link(AE.make_link(42, "close", "QUALITY").rsplit("/", 1)[1]) is None   # close is not allowed via link


def test_policy_for_new_endpoints():
    assert AU.allowed("GET", "/api/alert-links/abc.def", None)       # token-authorised
    assert not AU.allowed("PUT", "/api/features/chat_charts", "PROCUREMENT")
    assert AU.allowed("PUT", "/api/features/chat_charts", "ADMIN")
    assert AU.allowed("GET", "/api/metrics/otif_pct/records", "VIEWER")
    assert not AU.allowed("GET", "/api/metrics/otif_pct/records", "SUPPLIER")
    assert not AU.allowed("POST", "/api/alert-events/1/action", "VIEWER")
    assert AU.allowed("POST", "/api/alert-events/1/action", "QUALITY")


# ---------------------------------------------------------------- integration (real data)
def _db():
    try:
        from app import analytics as A
        A._run("SELECT 1", {}, True)
        return True
    except Exception:
        return False


db = pytest.mark.skipif(not _db(), reason="database not reachable")


@db
def test_migrations_applied_and_flags_present():
    from app import analytics as A
    from app.db import init_db
    init_db()
    done = {r["id"] for r in A._run("SELECT id FROM public.schema_migrations", {})}
    assert {m[0] for m in MIGRATIONS} <= done
    keys = {r["key"] for r in A._run("SELECT key FROM public.feature_flags", {})}
    assert {f[0] for f in FLAGS} <= keys


@db
def test_metric_layer_matches_existing_kpis():
    from app import analytics as A
    A.clear_cache()
    ov = A.overview()
    assert abs(MX.query("otif_pct", log=False)["rows"][0]["value"] - ov["otif_pct"]) < 0.06
    assert abs(MX.query("ppm", log=False)["rows"][0]["value"] - ov["incoming_ppm"]) < 0.06
    by_sup = {r["supplier"]: r["value"] for r in MX.query("otif_pct", ["supplier"], log=False)["rows"]}
    for c in A.scorecard():
        assert abs(by_sup[c["supplier_id"]] - c["otif_pct"]) < 0.06


@db
def test_records_match_metric_count():
    q = MX.query("past_due_lines", [], {"supplier_id": "SUP-001"}, log=False)["rows"][0]
    rec = MX.records("past_due_lines", {"supplier_id": "SUP-001"})
    assert rec["row_count"] == q["value"] == q["n"]


@db
def test_compare_periods_directions():
    r = MX.compare_periods("otif_pct", "month", "supplier")
    assert r["current_period"] > r["previous_period"]
    for row in r["rows"]:
        assert round(row["current"] - row["previous"], 2) == row["change"]
        assert row["direction"] == ("worse" if row["change"] < 0 else "better" if row["change"] > 0 else "flat")


@db
def test_safe_role_blocks_raw_tables():
    from sqlalchemy import text
    from app.db import ro_engine
    with ro_engine.connect() as c:
        c.execute(text("SET LOCAL ROLE agent_safe"))
        assert c.execute(text("SELECT COUNT(*) FROM sp.v_po_delivery")).scalar() > 0
    with ro_engine.connect() as c:
        c.execute(text("SET LOCAL ROLE agent_safe"))
        with pytest.raises(Exception):
            c.execute(text("SELECT COUNT(*) FROM sp.fact_purchase_order_receipt"))


@db
@pytest.mark.parametrize("kind", ["procurement", "quality", "management"])
def test_dashboards_build(kind):
    from app import dashboards as DB
    d = DB.build(kind)
    assert d["tiles"] and all("source" in t for t in d["tiles"])


@db
def test_alert_sync_is_idempotent_and_flow_enforced():
    first = AE.sync(force=True)
    second = AE.sync(force=True)
    assert second["new"] == [] and second["alerts"] == first["alerts"]
    ev = AE.list_events()
    assert len(ev) == first["alerts"]
    closed = next((e for e in ev if e["status"] == "CLOSED"), None)
    if closed:
        with pytest.raises(PermissionError):
            AE.act(closed["id"], "ack", "pytest")


@db
@pytest.mark.parametrize("fmt,magic", [("pdf", b"%PDF"), ("pptx", b"PK")])
def test_review_pack_export(fmt, magic):
    from app import exports as EX
    from app.main import review_pack
    pack = review_pack("SUP-001")
    data = EX.pdf(pack) if fmt == "pdf" else EX.pptx(pack)
    assert data[:len(magic)] == magic and len(data) > 5000
