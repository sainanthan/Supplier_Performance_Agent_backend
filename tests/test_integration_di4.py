"""Data Integration Phase 4 tests: SAP OData v2/v4, REST, MES REST, CSV/Excel upload (encrypted at rest), SFTP and OPC UA -
all against LOCAL mock servers (tests/di4_mocks.py). Security: SSRF guard (metadata / private / loopback / redirects / cross-host
paging), GET-only, pinned SFTP host key, OPC UA read-only (static scan + server write log), sensitive masking."""
import datetime as dt
import io
import json
import re
from pathlib import Path

import pytest

from app import auth as AU
from app.integration import registry as REG
from app.integration.api_connectors import RestConnector, SapODataConnector
from app.integration.file_connectors import SftpConnector, parse_file
from app.integration.http_base import BlockedTarget, check_host
from app.integration.opcua_connector import OpcUaConnector

from tests import di4_mocks as MK


def inst(key, cfg, sec=None, env="DEV"):
    return REG.instance(key, cfg, sec or {}, env, "CONN-TEST")


# ---------------------------------------------------------------- units: security
def test_ssrf_guard():
    with pytest.raises(BlockedTarget):
        check_host("169.254.169.254", True, "DEV")                         # cloud metadata: always blocked
    with pytest.raises(BlockedTarget):
        check_host("127.0.0.1", False, "DEV")                              # loopback needs an explicit internal flag ...
    check_host("127.0.0.1", True, "DEV")
    with pytest.raises(BlockedTarget):
        check_host("127.0.0.1", True, "PROD")                              # ... and only in DEV
    with pytest.raises(BlockedTarget):
        check_host("10.20.30.40", False, "PROD")                           # private network needs the ADMIN's internal flag
    check_host("10.20.30.40", True, "PROD")
    with pytest.raises(BlockedTarget):
        check_host("no-such-host.invalid", True, "DEV")


def test_policies():
    sap = SapODataConnector.policy_errors
    assert any("https" in e for e in sap({"service_url": "http://sap.example.com/x", "auth_type": "basic", "username": "u"}, {"password": "p"}, "TEST"))
    assert any("password" in e for e in sap({"service_url": "https://sap.example.com/x", "auth_type": "basic", "username": "u"}, {}, "DEV"))
    assert any("api key" in e for e in sap({"service_url": "https://sandbox.api.sap.com/x", "auth_type": "api_key"}, {}, "DEV"))
    assert not sap({"service_url": "https://sandbox.api.sap.com/x", "auth_type": "api_key"}, {"api_key": "k"}, "PROD")
    rest = RestConnector.policy_errors
    assert any("JSON" in e for e in rest({"base_url": "https://a.b", "auth_type": "none", "endpoints": "[{bad"}, {}, "DEV"))
    assert any("relative" in e for e in rest({"base_url": "https://a.b", "auth_type": "none",
                                               "endpoints": json.dumps([{"name": "x", "path": "https://evil/x"}])}, {}, "DEV"))
    assert any("None is only allowed in DEV" in e for e in OpcUaConnector.policy_errors({"endpoint_url": "opc.tcp://h:4840", "security_mode": "None"}, {}, "PROD"))
    assert any("pin the server" in e for e in OpcUaConnector.policy_errors({"endpoint_url": "opc.tcp://h:4840"}, {"client_certificate": "c", "client_private_key": "k"}, "DEV"))
    assert any("fingerprint" in e for e in SftpConnector.policy_errors({"host": "h", "folder": "/x"}, {"password": "p"}, "DEV"))


def test_read_only_by_construction():
    """Static guard: the OPC UA and SFTP connectors contain no write / call / delete operations."""
    src = Path("app/integration/opcua_connector.py").read_text(encoding="utf-8")
    code = src.split('"""', 2)[2]                                          # skip the module docstring
    assert not re.search(r"\.(write_\w+|set_value|call_method|add_\w+|delete_\w+|set_writable)\(", code)
    fsrc = Path("app/integration/file_connectors.py").read_text(encoding="utf-8")
    assert not re.search(r"sftp\.(remove|rename|mkdir|rmdir|put|chmod|symlink|truncate|utime)\(", fsrc)
    assert not re.search(r"sftp\.open\([^)]*['\"](w|a|r\+)", fsrc)
    hsrc = Path("app/integration/http_base.py").read_text(encoding="utf-8")
    assert len(re.findall(r"self\.client\.(post|put|patch|delete)\(", hsrc)) == 1 and "token_url" in hsrc   # only the OAuth2 token POST


def test_file_parsing():
    h, rows = parse_file("a.csv", "id;name;price\n1;Bolt;2,50\n\n2;Nut;1\n".encode("utf-8-sig"))
    assert h == ["id", "name", "price"] and len(rows) == 2 and rows[0]["name"] == "Bolt"     # BOM + ';' sniffed, blank row skipped
    h, rows = parse_file("b.csv", "Lieferant,Größe,Größe\nA,1,2\n".encode("cp1252"))
    assert h == ["Lieferant", "Größe", "Größe_2"]                                        # encoding detected, duplicate header made unique
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append([None])
    ws.append(["po", "date", "qty"])
    ws.append(["P1", dt.datetime(2026, 10, 1), 5])
    bio = io.BytesIO()
    wb.save(bio)
    h, rows = parse_file("c.xlsx", bio.getvalue())
    assert h == ["po", "date", "qty"] and rows[0]["date"].startswith("2026-10-01") and rows[0]["qty"] == 5
    for bad in (("x.exe", b"MZ"), ("y.xlsx", b"not a zip")):
        with pytest.raises(ValueError):
            parse_file(*bad)


# ---------------------------------------------------------------- mock HTTP sources
@pytest.fixture(scope="module")
def http():
    srv, base = MK.start_http()
    yield base
    srv.shutdown()


def _sap_v2(base, **kw):
    return inst("sap_odata", {"service_url": f"{base}/odata/v2/API_PURCHASEORDER_PROCESS_SRV", "auth_type": "basic", "username": "sapreader",
                              "internal_network": True, "page_size": 40, **kw}, {"password": "s3cret!"})


def test_sap_odata_v2(http):
    con = _sap_v2(http)
    t = con.test_connection()
    assert t.ok and t.details["odata_version"] == "v2" and t.details["entity_sets"] == 2
    objs = {o.name: o for o in con.discover_schema()}
    item = objs["A_PurchaseOrderItem"]
    assert {f.name for f in item.fields if f.key} == {"PurchaseOrder", "PurchaseOrderItem"}
    assert [f.name for f in item.fields if f.sensitive] == ["NetPriceAmount"]
    p = con.preview_data("A_PurchaseOrderItem")
    assert p["row_count"] == 100 and all(r["NetPriceAmount"] == "•••" for r in p["rows"])          # masked in preview
    allr = sum(con.extract("A_PurchaseOrder", "LastChangeDateTime", None), [])
    assert len(allr) == 250 and "__metadata" not in allr[0] and "to_Items" not in allr[0]          # $skip paging, nav props dropped
    assert allr[0]["PurchaseOrderDate"].startswith("2026-") and allr[0]["LastChangeDateTime"] <= allr[-1]["LastChangeDateTime"]
    since = "2026-11-01T00:00:00+00:00"
    inc = sum(con.extract("A_PurchaseOrder", "LastChangeDateTime", since), [])
    assert 0 < len(inc) < 250 and all(r["LastChangeDateTime"] >= since for r in inc)              # typed $filter on the server
    bad = inst("sap_odata", {"service_url": f"{http}/odata/v2/API_PURCHASEORDER_PROCESS_SRV", "auth_type": "basic", "username": "sapreader",
                             "internal_network": True}, {"password": "wrong"}).test_connection()
    assert not bad.ok and "credentials" in (bad.hint or "")
    assert MK._H.calls.get("writes", 0) == 0                                                        # GET only


def test_sap_odata_v4_api_key_and_next_link(http):
    con = inst("sap_odata", {"service_url": f"{http}/odata/v4", "auth_type": "api_key", "api_key_header": "APIKey", "internal_network": True},
               {"api_key": "k-123"})
    assert con.test_connection().details["odata_version"] == "v4"
    assert len(sum(con.extract("PurchaseOrder"), [])) == 250                                     # @odata.nextLink paging


def test_sap_blocked_without_internal_flag(http):
    t = _sap_v2(http, internal_network=False).test_connection()
    assert not t.ok and t.details["category"] == "BLOCKED"


EPS = [{"name": "suppliers", "path": "/api/v1/suppliers", "records": "data.items", "pagination": "page", "page_param": "page",
        "size_param": "limit", "page_size": 10, "watermark_param": "updated_since"},
       {"name": "events", "path": "/api/v1/events", "records": "items", "pagination": "cursor", "cursor_field": "next_cursor"},
       {"name": "busy", "path": "/api/v1/busy", "records": "items"},
       {"name": "evil", "path": "/api/v1/evil", "records": "items", "pagination": "next_link", "next_field": "next"},
       {"name": "redirect", "path": "/api/v1/redirect", "records": "items"}]


def _rest(base, key="rest"):
    return inst(key, {"base_url": base, "auth_type": "bearer", "endpoints": json.dumps(EPS), "internal_network": True}, {"token": "tok-xyz"})


def test_rest_paging_flatten_masking_retry(http):
    con = _rest(http)
    assert con.test_connection().ok
    sup = sum(con.extract("suppliers"), [])
    assert len(sup) == 23 and sup[0]["contact.email"] == "s0@example.com"                       # page paging, nested flattened
    inc = sum(con.extract("suppliers", "updated_at", (MK.BASE + dt.timedelta(days=20)).isoformat()), [])
    assert len(inc) == 3                                                                        # watermark passed to the API
    assert {"contact.email", "contact.phone"} <= set(con.preview_data("suppliers")["masked_columns"])
    assert len(sum(con.extract("events"), [])) == 12                                            # cursor paging
    assert sum(con.extract("busy"), [])[0]["ok"] == 1                                           # 429 + Retry-After honoured


def test_rest_ssrf_redirect_and_cross_host_paging_blocked(http):
    con = _rest(http)
    with pytest.raises(BlockedTarget):
        list(con.extract("redirect"))
    with pytest.raises(BlockedTarget):
        list(con.extract("evil"))
    mes = _rest(http, "mes_db_rest")
    assert mes.meta.category == "MES / shop floor" and len(sum(mes.extract("events"), [])) == 12


# ---------------------------------------------------------------- SFTP
@pytest.fixture(scope="module")
def sftp(tmp_path_factory):
    root = tmp_path_factory.mktemp("sftp")
    (root / "outbound").mkdir()
    (root / "outbound" / "po_1.csv").write_text("po,qty,price\nP1,5,2.5\nP2,7,3\n", encoding="utf-8")
    (root / "outbound" / "po_2.csv").write_text("po,qty,price\nP3,9,1\n", encoding="utf-8")
    (root / "outbound" / "readme.txt").write_text("not data", encoding="utf-8")
    s = MK.start_sftp(str(root))
    yield s
    s["stop"]()


def _sftp(s, fp=None):
    return inst("sftp", {"host": "127.0.0.1", "port": s["port"], "username": "reader", "host_fingerprint": fp or s["fingerprint"],
                         "folder": "/outbound", "pattern": "po_*.csv", "internal_network": True}, {"password": "pw-1"})


def test_sftp_read_only_pinned(sftp):
    con = _sftp(sftp)
    t = con.test_connection()
    assert t.ok and t.details["files"] == 2, t
    rows = sum(con.extract("po_*.csv", "_file_time", None), [])
    assert len(rows) == 3 and {r["_file"] for r in rows} == {"po_1.csv", "po_2.csv"} and rows[0]["_file_time"]
    assert con.preview_data("po_*.csv")["masked_columns"] == ["price"]
    assert sum(con.extract("po_*.csv", "_file_time", "2999-01-01"), []) == []                  # incremental by file time
    bad = _sftp(sftp, "SHA256:AAAAwrongfingerprint").test_connection()
    assert not bad.ok and bad.details["category"] == "SSH"                                     # pinned host key enforced
    assert sftp["writes"] == []                                                                # never wrote / renamed / deleted


# ---------------------------------------------------------------- OPC UA
@pytest.fixture(scope="module")
def opc():
    s = MK.start_opcua()
    yield s
    s["server"].stop()


def test_opcua_read_only_snapshot(opc):
    con = inst("opcua", {"endpoint_url": opc["url"], "security_mode": "None", "internal_network": True})
    t = con.test_connection()
    assert t.ok and t.details["variables"] == 3 and t.details["warnings"], t                  # unsecured flagged (DEV only)
    rows = {r["browse_path"]: r for r in sum(con.extract("snapshot"), [])}
    assert rows["SMT-L1/State"]["value"] == "RUNNING" and rows["SMT-L1/GoodCount"]["value"] == 1200
    assert rows["SMT-L1/Temperature"]["status"] == "Good"
    with pytest.raises(ValueError):
        list(con.extract("history", "source_time", None))                                      # mock does not historise: plain message
    assert opc["nodes"]["good"].read_value() == 1200                                           # untouched
    prod = inst("opcua", {"endpoint_url": opc["url"], "security_mode": "None", "internal_network": True}, env="PROD")
    assert not prod.test_connection().ok                                                       # loopback refused outside DEV


# ---------------------------------------------------------------- CSV / Excel upload (Neon)
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
    from app.features import all_flags, set_flag
    keys = ("data_integration", "di_files_apis", "di_mapping_loads", "di_database_connectors")
    before = {k: all_flags(force=True).get(k, False) for k in keys}
    for k in keys:
        set_flag(k, True, "pytest")
    yield set_flag
    for k, v in before.items():
        set_flag(k, v, "pytest")


@db
def test_upload_encrypted_mapping_and_rbac(flags):
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from app import analytics as A
    from app.integration import mapping as MP, pipeline as PL
    from app.integration.dbx import engine
    from app.main import app
    cl = TestClient(app)
    H = lambda u, r="ADMIN": {"Authorization": "Bearer " + AU.issue_token(AU.UserCtx(u, r))}  # noqa: E731
    c = cl.post("/api/integration/connections", headers=H("pytest-admin-a"), json={"connector_key": "csv_excel", "name": "pytest uploads"}).json()
    cid = c["id"]
    try:
        sup = A._run("SELECT supplier_id, supplier_name, country FROM sp.dim_supplier ORDER BY supplier_id", {})
        sup[0]["supplier_name"] += " (renamed)"
        lines = ["supplier_id,supplier_name,country,contact_email"] + [f"{s['supplier_id']},{s['supplier_name']},{s['country']},x{i}@example.com" for i, s in enumerate(sup)]
        lines.append("SUP-NEW,New Supplier Ltd,India,new@example.com")
        data = ("\n".join(lines) + "\n").encode()
        r = cl.post(f"/api/integration/connections/{cid}/files", headers=H("pytest-admin-a"), files={"file": ("suppliers.csv", data, "text/csv")})
        assert r.status_code == 200 and r.json()["rows"] == len(sup) + 1, r.text
        assert cl.post(f"/api/integration/connections/{cid}/files", headers=H("pytest-admin-a"),
                       files={"file": ("suppliers.csv", data, "text/csv")}).status_code == 422                  # same content refused
        assert cl.post(f"/api/integration/connections/{cid}/files", headers=H("q", "QUALITY"),
                       files={"file": ("s.csv", data, "text/csv")}).status_code == 403
        assert cl.post(f"/api/integration/connections/{cid}/files", headers=H("pytest-admin-a"),
                       files={"file": ("evil.exe", b"MZ", "application/octet-stream")}).status_code == 422
        with engine.connect() as x:
            blob = bytes(x.execute(text("SELECT content_enc FROM integration.files WHERE connection_id = :c"), {"c": cid}).scalar())
        assert b"example.com" not in blob and b"supplier_id" not in blob                                     # encrypted at rest
        assert cl.get(f"/api/integration/connections/{cid}/files", headers=H("v", "VIEWER")).json()[0]["name"] == "suppliers.csv"
        m = MP.create_mapping({"connection_id": cid, "entity": "supplier", "source_kind": "object", "source_object": "suppliers.csv",
                               "field_map": {"supplier_id": {"source": "supplier_id"}, "supplier_name": {"source": "supplier_name"},
                                             "country": {"source": "country"}},
                               "record_id_columns": ["_file", "supplier_id"], "watermark_column": "_file_time", "column_policy": {},
                               "mode": "STAGING"}, "pytest-admin-a")
        run = PL.run(m["id"], "manual", "pytest-admin-a")
        assert (run["inserted"], run["updated"], run["field_changes"]) == (1, 1, {"supplier_name": 1}), run   # reconciles vs sp.dim_supplier
        MP.delete_mapping(m["id"], "pytest")
        flags("di_files_apis", False, "pytest")
        assert cl.get(f"/api/integration/connections/{cid}/files", headers=H("v", "VIEWER")).status_code != 200
    finally:
        cl.delete(f"/api/integration/connections/{cid}", headers=H("pytest-admin-a"))
        with engine.begin() as x:
            x.execute(text("DELETE FROM integration.files WHERE connection_id = :c"), {"c": cid})
