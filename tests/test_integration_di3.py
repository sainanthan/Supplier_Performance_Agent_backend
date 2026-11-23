"""Data Integration Phase 3 tests: transforms / casting, schedules, rule-based suggestions, mapping validation (sensitive-column
policy), exclusion / pseudonymisation before storage, reconciliation of demo_erp -> sp (0 differences), four-eyes publish,
idempotent incremental upserts with lineage (into a SCRATCH copy of the KPI table - sp is never written by tests), quarantine
threshold, RBAC."""
import datetime as dt

import pytest

from app import auth as AU
from app.integration import mapping as MP


# ---------------------------------------------------------------- units
@pytest.mark.parametrize("v,spec,out", [
    ("0000100001", {"transform": "strip_zeros"}, "100001"), ("A0001", {"transform": "strip_zeros"}, "A0001"),
    ("20261031", {"transform": "to_date"}, "2026-10-31"), ("31.10.2026", {"transform": "to_date"}, "2026-10-31"),
    ("00000000", {"transform": "to_date"}, None), ("2026-10-31T10:00:00+05:30", {"transform": "to_date"}, "2026-10-31"),
    ("1,234.50", {"transform": "to_number"}, 1234.5), ("12.50-", {"transform": "to_number"}, -12.5),
    (30, {"transform": "divide", "arg": 10}, 3.0), ("X", {"transform": "map", "arg": {"X": "CLOSED"}}, "CLOSED"),
    ("Y", {"transform": "map", "arg": {"X": "CLOSED"}}, "Y"), (None, {"transform": "constant", "arg": "INR"}, "INR"),
    (" a ", {"transform": "trim"}, "a")])
def test_transforms(v, spec, out):
    assert MP.apply_transform(v, spec) == out


def test_casting_to_real_target_types():
    assert MP.cast("12", "integer") == 12 and MP.cast(6451.0, "integer") == 6451 and MP.cast("X", "boolean") is True
    assert MP.cast("2026-10-31", "date") == "2026-10-31" and MP.cast("", "integer") is None
    for v, t in (("12.5", "integer"), ("abc", "double precision"), ("31/31/2026", "date"), ("maybe", "boolean")):
        with pytest.raises(MP.CastError):
            MP.cast(v, t)


def test_schedules():
    assert MP.next_run("manual") is None
    t0 = dt.datetime(2026, 11, 13, 10, 0, tzinfo=dt.timezone.utc)
    assert MP.next_run("every:15", t0) == t0 + dt.timedelta(minutes=15) and MP.next_run("hourly", t0) == t0 + dt.timedelta(hours=1)
    d = MP.next_run("daily:02:00", t0)                                   # 02:00 IST next day = 20:30 UTC
    assert d > t0 and d.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30))).strftime("%H:%M") == "02:00"
    for bad in ("every:1", "daily:25:00", "weekly"):
        with pytest.raises(ValueError):
            MP.parse_schedule(bad)


SAP_COLS = [{"name": n, "type": "", "sensitive": n in ("netpr",), "key": False} for n in
            ("ebeln", "ebelp", "lifnr", "matnr", "werks", "bedat", "eindt", "gr_date", "menge", "wemng", "netpr", "waers", "zzpostat", "inco1", "aedat")]


def _db():
    try:
        from app import analytics as A
        A._run("SELECT 1", {}, True)
        return True
    except Exception:
        return False


db = pytest.mark.skipif(not _db(), reason="database not reachable")


@db
def test_rule_based_suggestions_sap():
    s = MP.suggest("po_line", SAP_COLS)
    fm = s["field_map"]
    assert fm["po_id"]["source"] == "ebeln" and fm["unit_price_inr"]["source"] == "netpr" and fm["promised_date"]["source"] == "eindt"
    assert fm["po_line"]["transform"] == "divide" and fm["po_line"]["arg"] == 10                        # SAP items step by 10
    assert s["unmapped_required"] == [] and s["watermark_candidates"] == ["aedat"] and s["sensitive_columns"] == ["netpr"]


@db
def test_validation_sensitive_policy_required_and_keys():
    fm = MP.suggest("po_line", SAP_COLS)["field_map"]
    base = {"entity": "po_line", "field_map": fm, "mode": "STAGING", "schedule": "manual", "record_id_columns": ["ebeln", "ebelp"]}
    errs, _ = MP.validate({**base, "column_policy": {}}, SAP_COLS)
    assert any("netpr looks sensitive" in e for e in errs)                                              # must decide keep / hash / exclude
    errs, _ = MP.validate({**base, "column_policy": {"netpr": "exclude"}}, SAP_COLS)
    assert any("EXCLUDED" in e for e in errs)                                                           # required price cannot be excluded
    errs, _ = MP.validate({**base, "column_policy": {"netpr": "hash"}}, SAP_COLS)
    assert any("only text fields" in e for e in errs)                                                   # a hash cannot go into a number
    assert MP.validate({**base, "column_policy": {"netpr": "keep"}}, SAP_COLS)[0] == []
    no_key = {**base, "column_policy": {"netpr": "keep"}, "field_map": {k: v for k, v in fm.items() if k != "po_id"}}
    assert any("key field 'po_id'" in e for e in MP.validate(no_key, SAP_COLS)[0])


@db
def test_policy_applied_before_storage():
    cols = [{"name": "lifnr", "sensitive": False}, {"name": "name1", "sensitive": False}, {"name": "land1", "sensitive": False},
            {"name": "smtp_addr", "sensitive": True}, {"name": "telf1", "sensitive": True}]
    m = {"entity": "supplier", "field_map": {"supplier_id": {"source": "lifnr"}, "supplier_name": {"source": "name1"}, "country": {"source": "land1"},
                                             "quality_certification": {"source": "smtp_addr"}},
         "column_policy": {"smtp_addr": "hash"}, "record_id_columns": ["lifnr"]}
    good, bad = MP.transform_rows(m, [{"lifnr": "S1", "name1": "N", "land1": "IN", "smtp_addr": "a@b.c", "telf1": "+91 1"}], cols)
    rec = good[0][2]
    assert rec["quality_certification"].startswith("h:") and "a@b.c" not in str(good)                   # pseudonymised before staging
    assert "telf1" not in str(good) and "+91" not in str(good)                                           # unmapped columns never kept
    m["column_policy"] = {"smtp_addr": "exclude"}
    assert MP.transform_rows(m, [{"lifnr": "S1", "name1": "N", "land1": "IN", "smtp_addr": "a@b.c"}], cols)[0][0][2]["quality_certification"] is None
    bad_rows = MP.transform_rows({**m, "field_map": {**m["field_map"], "standard_lead_time_days": {"source": "name1"}}},
                                 [{"lifnr": "S2", "name1": "x", "land1": "IN", "smtp_addr": "q@w.e"}], cols)[1]
    assert bad_rows and "not a number" in bad_rows[0][2][0] and bad_rows[0][1]["smtp_addr"] is None    # excluded value never shown


def test_policy_rbac():
    assert AU.allowed("GET", "/api/integration/mappings", "VIEWER") and AU.allowed("GET", "/api/integration/runs", "QUALITY")
    assert not AU.allowed("GET", "/api/integration/quarantine", "QUALITY")                             # holds source values
    assert not AU.allowed("POST", "/api/integration/mappings", "PROCUREMENT")
    assert not AU.allowed("POST", "/api/integration/mappings/MAP-0001/run", "QUALITY")


# ---------------------------------------------------------------- live (demo_erp in Neon)
Q = """SELECT p.ebeln, p.ebelp, k.lifnr, p.matnr, p.werks, k.bedat, {eindt} AS eindt, g.gr_date, p.menge, e.wemng, p.netpr, k.waers, p.zzpostat, k.inco1,
       GREATEST(k.aedat, p.aedat, e.aedat) AS changed_at
FROM demo_erp.ekpo p JOIN demo_erp.ekko k ON k.ebeln = p.ebeln JOIN demo_erp.eket e ON e.ebeln = p.ebeln AND e.ebelp = p.ebelp AND e.etenr = 1
LEFT JOIN (SELECT ebeln, ebelp, max(budat) AS gr_date FROM demo_erp.ekbe WHERE vgabe = '1' GROUP BY ebeln, ebelp) g ON g.ebeln = p.ebeln AND g.ebelp = p.ebelp"""
SCRATCH = "pytest_di3"


@pytest.fixture
def env():
    from sqlalchemy import text
    from app.db import engine
    from app.features import all_flags, set_flag
    from app.integration import service as S
    from scripts.gen_demo_erp import CONN_NAME
    c = [x for x in S.list_connections() if x["name"] == CONN_NAME]
    if not c:
        pytest.skip("demo_erp not generated")
    keys = ("data_integration", "di_database_connectors", "di_mapping_loads")
    before = {k: all_flags(force=True).get(k, False) for k in keys}
    for k in keys:
        set_flag(k, True, "pytest")
    with engine.begin() as x:                                            # scratch copy of the KPI table: 100 rows missing, 50 prices changed
        x.execute(text("SET LOCAL lock_timeout = '60s'"))
        x.execute(text(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE"))
        x.execute(text(f"CREATE SCHEMA {SCRATCH}"))
        x.execute(text(f"CREATE TABLE {SCRATCH}.fact_purchase_order_receipt (LIKE sp.fact_purchase_order_receipt INCLUDING ALL)"))
        x.execute(text(f"INSERT INTO {SCRATCH}.fact_purchase_order_receipt SELECT * FROM sp.fact_purchase_order_receipt ORDER BY po_id OFFSET 100"))
        x.execute(text(f"""UPDATE {SCRATCH}.fact_purchase_order_receipt SET unit_price_inr = unit_price_inr + 1
                           WHERE po_id IN (SELECT po_id FROM {SCRATCH}.fact_purchase_order_receipt ORDER BY po_id LIMIT 50)"""))
    made = []
    yield c[0]["id"], made
    for mid in made:
        try:
            MP.delete_mapping(mid, "pytest")
        except Exception:
            pass
    with engine.begin() as x:
        x.execute(text("SET LOCAL lock_timeout = '60s'"))
        x.execute(text(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE"))
    MP._TARGET_CACHE.clear()
    for k, v in before.items():
        set_flag(k, v, "pytest")


def _mapping(cid, mode="STAGING", eindt="e.eindt", **kw):
    return {"name": f"pytest {mode}", "connection_id": cid, "entity": "po_line", "source_kind": "query", "source_query": Q.format(eindt=eindt),
            "field_map": MP.suggest("po_line", SAP_COLS)["field_map"], "record_id_columns": ["ebeln", "ebelp"], "watermark_column": "changed_at",
            "column_policy": {"netpr": "keep"}, "mode": mode, "schedule": "manual", **kw}


@db
def test_reconcile_demo_erp_reproduces_kpi_data(env):
    """The SAP-like source mapped through the canonical model gives exactly today's KPI input: 0 inserts, 0 changes."""
    from app.integration import pipeline as PL
    cid, made = env
    m = MP.create_mapping(_mapping(cid), "pytest-admin-a")
    made.append(m["id"])
    r = PL.run(m["id"], "manual", "pytest-admin-a")
    assert r["status"] == "SUCCESS" and r["extracted"] == 1300 and r["staged"] == 1300, r
    assert r["inserted"] == 0 and r["updated"] == 0 and r["unchanged"] == 1300 and r["field_changes"] == {}
    assert r["dry_run"] and MP.get_mapping(m["id"])["last_watermark"] is None                          # staging never advances the watermark


@db
def test_publish_four_eyes_idempotent_incremental_lineage(env):
    from sqlalchemy import text
    from app.db import engine
    from app.integration import pipeline as PL
    cid, made = env
    m = MP.create_mapping(_mapping(cid, "PUBLISH"), "pytest-admin-a")
    made.append(m["id"])
    assert m["approval_status"] == "PENDING"
    with pytest.raises(PermissionError):
        PL.run(m["id"], "manual", "pytest-admin-a", target_schema=SCRATCH)                              # not approved yet
    with pytest.raises(PermissionError):
        MP.decide_mapping(m["id"], "APPROVED", "pytest-admin-a")                                        # requester cannot approve
    MP.decide_mapping(m["id"], "APPROVED", "pytest-admin-b")
    r1 = PL.run(m["id"], "manual", "pytest-admin-a", target_schema=SCRATCH)
    assert r1["status"] == "SUCCESS" and r1["inserted"] == 100 and r1["updated"] == 50 and r1["restamped"] == 1150, r1
    assert r1["field_changes"] == {"unit_price_inr": 50} and r1["samples"][0]["field"] == "unit_price_inr"
    with engine.connect() as x:
        row = x.execute(text(f"""SELECT count(*), count(*) FILTER (WHERE _connection_id = :c AND _run_id = :r AND _source_record_id LIKE 'POC-%|%0'),
                                 (SELECT count(*) FROM sp.fact_purchase_order_receipt t JOIN {SCRATCH}.fact_purchase_order_receipt s USING (po_id)
                                  WHERE t.unit_price_inr IS DISTINCT FROM s.unit_price_inr OR t.promised_date IS DISTINCT FROM s.promised_date)
                                 FROM {SCRATCH}.fact_purchase_order_receipt"""), {"c": cid, "r": r1["id"]}).fetchone()
    assert row[0] == 1300 and row[1] == 1300 and row[2] == 0                                            # complete, lineage on every row, equal to sp
    r2 = PL.run(m["id"], "manual", "pytest-admin-a", full=True, target_schema=SCRATCH)
    assert (r2["inserted"], r2["updated"], r2["restamped"], r2["unchanged"]) == (0, 0, 0, 1300)        # idempotent
    r3 = PL.run(m["id"], "manual", "pytest-admin-a", target_schema=SCRATCH)
    assert 0 < r3["extracted"] < 1300 and r3["watermark_from"] == r1["watermark_to"]                    # incremental from the watermark
    ed = MP.update_mapping(m["id"], {"column_policy": {"netpr": "keep"}, "watermark_column": "changed_at", "record_id_columns": ["ebeln"]}, "pytest-admin-a")
    assert ed["approval_status"] == "PENDING"                                                           # changing what is loaded resets approval
    with engine.connect() as x:
        assert x.execute(text("SELECT count(*) FROM integration.table_ownership WHERE canonical_table = 'fact_purchase_order_receipt'")).scalar() == 0


@db
def test_quarantine_threshold_blocks_publish(env):
    from sqlalchemy import text
    from app.db import engine
    from app.integration import pipeline as PL
    cid, made = env
    bad = "CASE WHEN p.ebeln LIKE '%7' THEN 'not-a-date' ELSE e.eindt::text END"                       # ~10 % unparseable dates
    m = MP.create_mapping(_mapping(cid, "PUBLISH", bad), "pytest-admin-a")
    made.append(m["id"])
    MP.decide_mapping(m["id"], "APPROVED", "pytest-admin-b")
    with engine.connect() as x:
        before = x.execute(text(f"SELECT count(*) FROM {SCRATCH}.fact_purchase_order_receipt")).scalar()
    r = PL.run(m["id"], "manual", "pytest-admin-a", target_schema=SCRATCH)
    assert r["status"] == "FAILED" and r["quarantined"] > 65 and "nothing published" in r["message"]
    with engine.connect() as x:
        assert x.execute(text(f"SELECT count(*) FROM {SCRATCH}.fact_purchase_order_receipt")).scalar() == before
    q = PL.quarantine(run_id=r["id"])
    assert q and "not a date" in q[0]["errors"][0] and q[0]["source_values"]["netpr"] == "•••"        # sensitive values masked in quarantine
    MP.update_mapping(m["id"], {"max_quarantine_pct": 20}, "pytest-admin-a")
    r2 = PL.run(m["id"], "manual", "pytest-admin-a", target_schema=SCRATCH)
    assert r2["status"] == "PARTIAL" and r2["inserted"] > 0                                             # under the limit: good rows load
    assert PL.resolve_quarantine(q[0]["id"], "IGNORED", "pytest-admin-a")["status"] == "IGNORED"


@db
def test_api_rbac_and_flag(env):
    from fastapi.testclient import TestClient
    from app.features import set_flag
    from app.main import app
    cid, made = env
    cl = TestClient(app)
    H = lambda u, r="ADMIN": {"Authorization": "Bearer " + AU.issue_token(AU.UserCtx(u, r))}  # noqa: E731
    s = cl.post("/api/integration/mappings/suggest", headers=H("pytest-admin-a"),
                json={"connection_id": cid, "entity": "po_line", "source_kind": "object", "source_object": "demo_erp.ekpo"})
    assert s.status_code == 200 and s.json()["field_map"]["po_id"]["source"] == "ebeln"
    p = cl.post("/api/integration/mappings/preview", headers=H("pytest-admin-a"), json=_mapping(cid))
    assert p.status_code == 200 and not p.json()["errors"] and p.json()["rows"][0]["unit_price_inr"] == "•••"
    c = cl.post("/api/integration/mappings", headers=H("pytest-admin-a"), json=_mapping(cid))
    assert c.status_code == 200, c.text
    made.append(c.json()["id"])
    assert cl.post(f"/api/integration/mappings/{c.json()['id']}/run", headers=H("q", "QUALITY"), json={}).status_code == 403
    assert cl.get("/api/integration/quarantine", headers=H("q", "QUALITY")).status_code == 403
    assert cl.get("/api/integration/mappings", headers=H("v", "VIEWER")).status_code == 200
    set_flag("di_mapping_loads", False, "pytest")
    assert cl.get("/api/integration/mappings", headers=H("v", "VIEWER")).status_code != 200


@db
def test_run_lease_blocks_concurrent_runs_and_expires(env):
    """One run per mapping, safe behind Neon's transaction pooler (row lease, not session advisory locks)."""
    from sqlalchemy import text
    from app.integration import pipeline as PL
    from app.integration.dbx import engine
    cid, made = env
    m = MP.create_mapping(_mapping(cid), "pytest-admin-a")
    made.append(m["id"])
    with engine.begin() as x:
        x.execute(text("UPDATE integration.mappings SET running_run_id = 'RUN-other', running_since = now() WHERE id = :i"), {"i": m["id"]})
    with pytest.raises(PermissionError):
        PL.run(m["id"], "manual", "pytest-admin-a")                                        # another run holds the lease
    with engine.begin() as x:                                                               # crashed run: lease expired
        x.execute(text("UPDATE integration.mappings SET running_since = now() - interval '3 hours' WHERE id = :i"), {"i": m["id"]})
    assert PL.run(m["id"], "manual", "pytest-admin-a")["status"] == "SUCCESS"
    assert MP.get_mapping(m["id"])["running_run_id"] is None                               # released after the run
