"""Data Integration Phase 5 tests: data-quality rules (validation, every kind, ERROR -> quarantine, WARN -> counted, reference
checks set-based, results mirrored to public.dq_results), quarantine work-queue (grouping, bulk ignore, auto-resolve), health
(stale / failing / once-per-change alerts), retention, lineage, RBAC + flag."""
import datetime as dt
import json

import pytest

from app import auth as AU
from app.integration import quality as QL


# ---------------------------------------------------------------- units
def test_rule_validation():
    assert QL.validate_rule("po_line", "range", {"field": "ordered_qty", "min": 0}, "ERROR") == []
    assert any("not a field" in e for e in QL.validate_rule("po_line", "not_null", {"field": "nope"}, "ERROR"))
    assert any("unknown rule kind" in e for e in QL.validate_rule("po_line", "sql", {"q": "DROP TABLE x"}, "ERROR"))     # no SQL rules
    assert any("min and/or max" in e for e in QL.validate_rule("po_line", "range", {"field": "ordered_qty"}, "ERROR"))
    assert any("invalid pattern" in e for e in QL.validate_rule("supplier", "pattern", {"field": "supplier_id", "regex": "("}, "WARN"))
    assert any("does not exist" in e for e in QL.validate_rule("po_line", "reference", {"field": "supplier_id", "entity": "x", "ref_field": "y"}, "ERROR"))
    assert any("severity" in e for e in QL.validate_rule("po_line", "not_null", {"field": "po_id"}, "FATAL"))


@pytest.mark.parametrize("kind,params,rec,fails", [
    ("not_null", {"field": "a"}, {"a": None}, True), ("not_null", {"field": "a"}, {"a": 0}, False),
    ("conditional_not_null", {"field": "d", "when_field": "r", "when_value": "FAIL"}, {"r": "FAIL", "d": ""}, True),
    ("conditional_not_null", {"field": "d", "when_field": "r", "when_value": "FAIL"}, {"r": "PASS", "d": None}, False),
    ("range", {"field": "q", "min": 0.000001}, {"q": 0}, True), ("range", {"field": "q", "max": 10}, {"q": 5}, False),
    ("range", {"field": "q", "min": 0}, {"q": None}, False),
    ("allowed_values", {"field": "s", "values": ["OPEN", "CLOSED"]}, {"s": "WEIRD"}, True),
    ("pattern", {"field": "id", "regex": r"SUP-\d{3}"}, {"id": "SUP-01"}, True), ("pattern", {"field": "id", "regex": r"SUP-\d{3}"}, {"id": "SUP-001"}, False),
    ("date_order", {"first": "a", "second": "b"}, {"a": "2026-10-02", "b": "2026-10-01"}, True),
    ("date_order", {"first": "a", "second": "b"}, {"a": "2026-10-01T10:00:00", "b": "2026-10-01"}, False),
    ("ratio_max", {"field": "r", "of": "o", "max": 1.5}, {"r": 160, "o": 100}, True), ("ratio_max", {"field": "r", "of": "o", "max": 1.5}, {"r": 150, "o": 100}, False)])
def test_rule_kinds(kind, params, rec, fails):
    assert (QL.check_row({"kind": kind, "params": params, "name": "t", "severity": "ERROR"}, rec) is not None) == fails


def test_policy_rbac():
    assert AU.allowed("GET", "/api/integration/health", "VIEWER") and AU.allowed("GET", "/api/integration/lineage", "QUALITY")
    assert AU.allowed("GET", "/api/integration/dq-rules", "PROCUREMENT")
    assert not AU.allowed("POST", "/api/integration/dq-rules", "PROCUREMENT")
    assert not AU.allowed("GET", "/api/integration/quarantine-queue", "QUALITY")                 # holds source values
    assert not AU.allowed("PUT", "/api/integration/settings", "QUALITY")


# ---------------------------------------------------------------- live
def _db():
    try:
        from app import analytics as A
        A._run("SELECT 1", {}, True)
        return True
    except Exception:
        return False


db = pytest.mark.skipif(not _db(), reason="database not reachable")
BAD_Q = """SELECT p.ebeln, p.ebelp,
  CASE WHEN p.ebeln LIKE '%11' THEN 'SUP-GHOST' ELSE k.lifnr END AS lifnr,
  p.matnr, p.werks, k.bedat,
  CASE WHEN p.ebeln LIKE '%22' THEN k.bedat - 5 ELSE e.eindt END AS eindt,
  g.gr_date, p.menge,
  CASE WHEN p.ebeln LIKE '%33' THEN e.wemng * 3 ELSE e.wemng END AS wemng,
  p.netpr, k.waers, p.zzpostat, k.inco1, GREATEST(k.aedat, p.aedat, e.aedat) AS changed_at
FROM demo_erp.ekpo p JOIN demo_erp.ekko k ON k.ebeln = p.ebeln JOIN demo_erp.eket e ON e.ebeln = p.ebeln AND e.ebelp = p.ebelp AND e.etenr = 1
LEFT JOIN (SELECT ebeln, ebelp, max(budat) AS gr_date FROM demo_erp.ekbe WHERE vgabe = '1' GROUP BY ebeln, ebelp) g ON g.ebeln = p.ebeln AND g.ebelp = p.ebelp"""


@pytest.fixture
def env():
    from app.features import all_flags, set_flag
    from app.integration import service as S
    from scripts.gen_demo_erp import CONN_NAME
    c = [x for x in S.list_connections() if x["name"] == CONN_NAME]
    if not c:
        pytest.skip("demo_erp not generated")
    keys = ("data_integration", "di_database_connectors", "di_mapping_loads", "di_quality_health")
    before = {k: all_flags(force=True).get(k, False) for k in keys}
    for k in keys:
        set_flag(k, True, "pytest")
    made = []
    yield c[0]["id"], made, set_flag
    from app.integration import mapping as MP
    for mid in made:
        try:
            MP.delete_mapping(mid, "pytest")
        except Exception:
            pass
    for k, v in before.items():
        set_flag(k, v, "pytest")


def _mapping(cid, q=BAD_Q, **kw):
    from app.integration import mapping as MP
    from tests.test_integration_di3 import SAP_COLS
    return {"name": "pytest dq", "connection_id": cid, "entity": "po_line", "source_kind": "query", "source_query": q,
            "field_map": MP.suggest("po_line", SAP_COLS)["field_map"], "record_id_columns": ["ebeln", "ebelp"], "watermark_column": "changed_at",
            "column_policy": {"netpr": "keep"}, "mode": "STAGING", "schedule": "manual", "max_quarantine_pct": 50, **kw}


@db
def test_dq_quarantine_warn_queue_autoresolve(env):
    from app import analytics as A
    from app.integration import health as HL, mapping as MP, pipeline as PL
    cid, made, _ = env
    n = {s: A._run(f"SELECT count(*) n FROM demo_erp.ekpo WHERE ebeln LIKE '%{s}'", {})[0]["n"] for s in ("11", "22", "33")}
    m = MP.create_mapping(_mapping(cid), "pytest-admin-a")
    made.append(m["id"])
    r = PL.run(m["id"], "manual", "pytest-admin-a")
    dq = r["dq_summary"]
    assert dq["supplier_known"]["failed"] == n["11"] and dq["promise_after_po_date"]["failed"] == n["22"], dq   # ERROR rules
    assert dq["over_receipt_50pct"]["failed"] == n["33"] and dq["over_receipt_50pct"]["severity"] == "WARN"     # WARN: counted, still loaded
    assert r["quarantined"] == n["11"] + n["22"] and r["staged"] == 1300 - n["11"] - n["22"], r
    assert r["status"] == "PARTIAL"
    res = A._run("SELECT rule, severity, failed_rows FROM public.dq_results WHERE load_id = :l", {"l": r["id"]})
    assert {x["rule"]: x["failed_rows"] for x in res}["supplier_known"] == n["11"]                              # one DQ history for CSV + loads
    q = HL.queue(m["id"])
    reasons = {g["reason"]: g["count"] for g in q["groups"]}
    assert reasons == {"DQ supplier_known": n["11"], "DQ promise_after_po_date": n["22"]}
    assert all(x["source_values"].get("unit_price_inr") in (None, "•••") for x in q["rows"])                   # sensitive kept value masked
    assert HL.bulk_resolve("IGNORED", "pytest-admin-a", mapping_id=m["id"], reason="DQ promise_after_po_date")["updated"] == n["22"]
    MP.update_mapping(m["id"], {"source_query": BAD_Q.replace("'SUP-GHOST'", "k.lifnr")}, "pytest-admin-a")   # source "fixed"
    r2 = PL.run(m["id"], "manual", "pytest-admin-a")
    assert r2["quarantined"] == n["22"]
    left = HL.queue(m["id"], "OPEN")["groups"]
    assert {g["reason"] for g in left} == {"DQ promise_after_po_date"} and all(g["count"] == n["22"] for g in left)   # supplier rows auto-resolved
    assert HL.queue(m["id"], "RESOLVED")["total"] == n["11"]


@db
def test_health_states_alert_once_and_retention(env):
    from sqlalchemy import text
    from app.integration import health as HL, mapping as MP
    from app.integration.dbx import engine
    cid, made, _ = env
    q = "SELECT p.ebeln, p.ebelp, k.lifnr, p.matnr, p.werks, k.bedat, e.eindt, NULL::date AS gr_date, p.menge, e.wemng, p.netpr, k.waers, p.zzpostat, k.inco1, p.aedat AS changed_at FROM demo_erp.ekpo p JOIN demo_erp.ekko k ON k.ebeln = p.ebeln JOIN demo_erp.eket e ON e.ebeln = p.ebeln AND e.ebelp = p.ebelp"
    m = MP.create_mapping(_mapping(cid, q, schedule="hourly"), "pytest-admin-a")
    made.append(m["id"])
    st = lambda: next(x for x in HL.health()["mappings"] if x["mapping_id"] == m["id"])  # noqa: E731
    assert st()["status"] == "NEVER_RUN" and st()["expected_hours"] == 3
    HL.check_and_notify()
    with engine.begin() as c:
        c.execute(text("""INSERT INTO integration.runs (id, mapping_id, status, started_at, finished_at, extracted) VALUES
                          ('RUN-PYTEST-OLD-' || :s, :m, 'SUCCESS', now() - interval '10 hours', now() - interval '10 hours', 5)"""), {"m": m["id"], "s": m["id"][-4:]})
    h = st()
    assert h["status"] == "STALE" and h["freshness_hours"] >= 9.9
    first = HL.check_and_notify()["notified"]
    assert {"mapping_id": m["id"], "status": "STALE"} in first
    assert all(x["mapping_id"] != m["id"] for x in HL.check_and_notify()["notified"])                       # once per change
    with engine.begin() as c:
        c.execute(text("""INSERT INTO integration.runs (id, mapping_id, status, started_at, finished_at) VALUES
                          ('RUN-PYTEST-FAIL-' || :s, :m, 'FAILED', now(), now())"""), {"m": m["id"], "s": m["id"][-4:]})
    assert st()["status"] == "FAILING" and st()["failures_7d"] >= 1
    assert {"mapping_id": m["id"], "status": "FAILING"} in HL.check_and_notify()["notified"]
    # retention: an old file and an old closed quarantine row are purged, recent ones kept
    with engine.begin() as c:
        old = c.execute(text("""INSERT INTO integration.files (connection_id, name, size_bytes, sha256, content_enc, uploaded_at)
                                VALUES ('CONN-PYTEST', 'old.csv', 1, 'x', 'abc', now() - interval '400 days') RETURNING id""")).scalar()
        new = c.execute(text("""INSERT INTO integration.files (connection_id, name, size_bytes, sha256, content_enc)
                                VALUES ('CONN-PYTEST', 'new.csv', 1, 'y', 'abc') RETURNING id""")).scalar()
        qid = c.execute(text("""INSERT INTO integration.quarantine (run_id, mapping_id, errors, status, resolved_at)
                                VALUES ('RUN-PYTEST', :m, '["x"]', 'IGNORED', now() - interval '400 days') RETURNING id"""), {"m": m["id"]}).scalar()
    try:
        out = HL.housekeeping("pytest")
        assert out["files_purged"] >= 1 and out["quarantine_deleted"] >= 1
        with engine.connect() as c:
            assert c.execute(text("SELECT content_enc IS NULL AND NOT active FROM integration.files WHERE id = :i"), {"i": old}).scalar()
            assert c.execute(text("SELECT active FROM integration.files WHERE id = :i"), {"i": new}).scalar()
            assert c.execute(text("SELECT count(*) FROM integration.quarantine WHERE id = :i"), {"i": qid}).scalar() == 0
        with pytest.raises(ValueError):
            HL.set_settings({"file_retention_days": 1}, "pytest")
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM integration.files WHERE connection_id = 'CONN-PYTEST'"))
            c.execute(text("DELETE FROM integration.runs WHERE id LIKE 'RUN-PYTEST-%'"))
            c.execute(text("DELETE FROM integration.health_state WHERE mapping_id = :m"), {"m": m["id"]})
            c.execute(text("DELETE FROM public.notifications WHERE subject LIKE :s"), {"s": f"Data load {m['id']} %"})   # test alerts only


@db
def test_lineage_and_api(env):
    from fastapi.testclient import TestClient
    from app.integration import health as HL
    from app.main import app
    cid, made, set_flag = env
    L = HL.lineage("po_line", {"po_id": "POC-000001", "po_line": 1})
    assert L["origin"] in ("csv", "connector") and L["lineage"]["_source_record_id"] == "POC-000001|1"
    assert L["kpi"]["view"] == "sp.v_po_delivery" and L["kpi"]["row"]["po_id"] == "POC-000001" and "OTIF" in L["feeds"]
    with pytest.raises(ValueError):
        HL.lineage("po_line", {"po_id": "POC-000001"})                                                     # full key required
    with pytest.raises(LookupError):
        HL.lineage("po_line", {"po_id": "NOPE", "po_line": 1})
    cl = TestClient(app)
    H = lambda u, r="ADMIN": {"Authorization": "Bearer " + AU.issue_token(AU.UserCtx(u, r))}  # noqa: E731
    assert cl.get("/api/integration/health", headers=H("v", "VIEWER")).status_code == 200
    assert cl.get("/api/integration/lineage", headers=H("q", "QUALITY"), params={"entity": "po_line", "key": json.dumps({"po_id": "POC-000001", "po_line": 1})}).status_code == 200
    assert cl.get("/api/integration/quarantine-queue", headers=H("q", "QUALITY")).status_code == 403
    assert cl.post("/api/integration/dq-rules", headers=H("p", "PROCUREMENT"), json={"entity": "po_line"}).status_code == 403
    bad = cl.post("/api/integration/dq-rules", headers=H("pytest-admin-a"), json={"entity": "po_line", "name": "x", "kind": "sql", "params": {}})
    assert bad.status_code == 422
    ok = cl.post("/api/integration/dq-rules", headers=H("pytest-admin-a"), json={"entity": "supplier", "name": "pytest id format", "kind": "pattern",
                                                                                  "params": {"field": "supplier_id", "regex": "SUP-[0-9]{3}"}, "severity": "WARN"})
    assert ok.status_code == 200, ok.text
    assert cl.patch(f"/api/integration/dq-rules/{ok.json()['id']}", headers=H("pytest-admin-a"), json={"enabled": False}).json()["enabled"] is False
    assert cl.delete(f"/api/integration/dq-rules/{ok.json()['id']}", headers=H("pytest-admin-a")).status_code == 200
    set_flag("di_quality_health", False, "pytest")
    assert cl.get("/api/integration/health", headers=H("v", "VIEWER")).status_code != 200
