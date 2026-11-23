"""Data Integration Phase 1 tests: catalogue / interface, masking, secrets never in plain text, four-eyes approval,
audit trail, canonical model matches the real tables, access policy."""
import pytest

from app import auth as AU
from app.integration import registry as REG
from app.integration.base import BaseConnector


# ---------------------------------------------------------------- unit
def test_catalogue_covers_all_categories_and_priorities():
    cats = {m["category"] for m in REG.catalogue()}
    for c in ("Databases", "ERP", "MES / shop floor", "QMS", "WMS / TMS / logistics", "IoT / OT (read-only)", "Supplier portal & EDI",
              "Files & storage", "Generic", "Data platforms"):
        assert c in cats
    meta = {m["key"]: m for m in REG.catalogue()}
    for k in ("oracle", "postgresql", "sqlserver", "sap_odata", "rest", "csv_excel", "sftp", "mes_db_rest", "opcua"):
        assert meta[k]["availability"] in ("planned", "available")
    assert all(m["read_only"] for m in meta.values())                                  # every connector is read-only by contract
    assert all(f["secret"] for f in meta["postgresql"]["fields"] if f["name"] == "password")


def test_interface_enforced_and_masking():
    with pytest.raises(TypeError):
        BaseConnector({})                                                              # abstract: must implement the contract
    con = REG.instance("demo_mock", {"seed": 3}, {})
    p = con.preview_data("EKPO")
    assert "NETPR" in p["masked_columns"] and all(r["NETPR"] == "•••" for r in p["rows"])
    assert p["row_count"] == 100 and "EBELN" not in p["masked_columns"]
    assert "TELF1" in con.preview_data("LFA1")["masked_columns"]                      # personal data masked
    assert len(sum(con.extract("EKKO", "BEDAT", "2026-10-01"), [])) < 250              # incremental watermark filter


def test_policy_di():
    assert AU.allowed("GET", "/api/integration/catalog", "VIEWER")
    assert not AU.allowed("POST", "/api/integration/connections", "PROCUREMENT")
    assert AU.allowed("POST", "/api/integration/connections", "ADMIN")
    assert not AU.allowed("GET", "/api/integration/connections/CONN-0001/preview", "QUALITY")   # source data: ADMIN only
    assert not AU.allowed("GET", "/api/integration/catalog", "SUPPLIER")


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
def test_canonical_model_matches_real_tables():
    from app import analytics as A
    from app.db import init_db
    from app.integration.canonical import CANONICAL, LINEAGE_COLS
    init_db()
    for ent in CANONICAL.values():
        cols = {r["column_name"] for r in A._run("SELECT column_name FROM information_schema.columns WHERE table_schema='sp' AND table_name=:t", {"t": ent["table"]})}
        missing = (set(ent["fields"]) | set(ent["key"]) | {c for c, _ in LINEAGE_COLS}) - cols
        assert not missing, (ent["table"], missing)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.features import all_flags, set_flag
    from app.main import app
    before = all_flags(force=True).get("data_integration", False)
    set_flag("data_integration", True, "pytest")
    yield TestClient(app)
    set_flag("data_integration", before, "pytest")


def H(u, r="ADMIN"):
    return {"Authorization": "Bearer " + AU.issue_token(AU.UserCtx(u, r))}


@db
def test_secrets_never_plaintext_and_audit(client):
    from sqlalchemy import text
    from app.db import engine
    secret = "S3cret-pytest-Value-42"
    r = client.post("/api/integration/connections", headers=H("pytest-admin-a"), json={"connector_key": "demo_mock", "name": "pytest demo",
                    "environment": "DEV", "config": {"seed": 5, "api_token": "leaked-via-config?"}, "secrets": {"api_token": secret}})
    assert r.status_code == 200, r.text
    c = r.json()
    try:
        assert secret not in r.text and "api_token" in c["secrets_masked"] and "api_token" not in c["config"]
        with engine.connect() as x:
            row = x.execute(text("SELECT config::text, secret_ref FROM integration.connections WHERE id = :i"), {"i": c["id"]}).fetchone()
            ct = x.execute(text("SELECT ciphertext FROM integration.secrets WHERE ref = :r"), {"r": row[1]}).scalar()
        assert secret not in row[0] and secret not in ct                                   # encrypted at rest, not in the config row
        from app.integration.secrets import vault
        assert vault().get(row[1])["api_token"] == secret                                 # ... but resolvable by the vault
        t = client.post(f"/api/integration/connections/{c['id']}/test", headers=H("pytest-admin-a")).json()
        assert t["ok"] is True
        assert client.get(f"/api/integration/connections/{c['id']}/preview", headers=H("pytest-admin-a"), params={"object": "EKPO"}).json()["masked_columns"]
        assert client.get(f"/api/integration/connections/{c['id']}/preview", headers=H("q", "QUALITY"), params={"object": "EKPO"}).status_code == 403
        assert client.post("/api/integration/connections", headers=H("p", "PROCUREMENT"), json={"connector_key": "demo_mock", "name": "nope"}).status_code == 403
        log = client.get("/api/integration/audit", headers=H("v", "VIEWER"), params={"connection_id": c["id"]}).json()
        assert {"create", "test", "preview"} <= {e["action"] for e in log} and secret not in str(log)
    finally:
        assert client.delete(f"/api/integration/connections/{c['id']}", headers=H("pytest-admin-a")).status_code == 200
    with engine.connect() as x:
        assert x.execute(text("SELECT COUNT(*) FROM integration.secrets WHERE ref = :r"), {"r": row[1]}).scalar() == 0   # secret removed


@db
def test_four_eyes_for_production(client):
    r = client.post("/api/integration/connections", headers=H("pytest-admin-a"), json={"connector_key": "demo_mock", "name": "pytest prod", "environment": "PROD"})
    c = r.json()
    try:
        assert c["approval_status"] == "PENDING" and c["status"] == "PENDING_APPROVAL"
        assert client.post(f"/api/integration/connections/{c['id']}/test", headers=H("pytest-admin-a")).status_code == 409   # blocked until approved
        assert client.post(f"/api/integration/connections/{c['id']}/approve", headers=H("pytest-admin-a"), json={"decision": "APPROVED"}).status_code == 409
        ok = client.post(f"/api/integration/connections/{c['id']}/approve", headers=H("pytest-admin-b"), json={"decision": "APPROVED"}).json()
        assert ok["approval_status"] == "APPROVED" and ok["approved_by"] == "pytest-admin-b"
        assert client.post(f"/api/integration/connections/{c['id']}/test", headers=H("pytest-admin-a")).json()["ok"]
        ed = client.patch(f"/api/integration/connections/{c['id']}", headers=H("pytest-admin-a"), json={"config": {"seed": 9}}).json()
        assert ed["approval_status"] == "PENDING"                                           # editing PROD re-requires approval
    finally:
        client.delete(f"/api/integration/connections/{c['id']}", headers=H("pytest-admin-a"))


@db
def test_coming_soon_and_unknown_rejected(client):
    assert client.post("/api/integration/connections", headers=H("pytest-admin-a"), json={"connector_key": "snowflake", "name": "x snowflake"}).status_code == 422
    assert client.post("/api/integration/connections", headers=H("pytest-admin-a"), json={"connector_key": "nope", "name": "x nope"}).status_code == 422
    assert client.post("/api/integration/connections", headers=H("pytest-admin-a"), json={"connector_key": "oracle", "name": "x oracle"}).status_code == 422   # in development


@db
def test_vault_rotation():
    """Rotation of ONE test secret (never the whole store): new key in front, old key still readable, then old key retired."""
    import os
    from cryptography.fernet import Fernet
    from app.integration import secrets as S
    old_env = os.environ.pop("INTEGRATION_MASTER_KEYS", None)
    v1 = S.LocalEncryptedVault()
    ref = v1.put({"password": "rotate-me"}, "pytest")
    new_key = Fernet.generate_key().decode()
    try:
        os.environ["INTEGRATION_MASTER_KEYS"] = f"{new_key},{v1._keys[0]}"
        v2 = S.LocalEncryptedVault()
        assert v2.get(ref)["password"] == "rotate-me" and v2.rotate([ref]) == 1
        os.environ["INTEGRATION_MASTER_KEYS"] = new_key                       # old key retired
        assert S.LocalEncryptedVault().get(ref)["password"] == "rotate-me"
        bad = S.LocalEncryptedVault()
        os.environ["INTEGRATION_MASTER_KEYS"] = Fernet.generate_key().decode()   # a key that cannot read the secret
        with pytest.raises(RuntimeError):
            S.LocalEncryptedVault().rotate([ref])                              # all-or-nothing: aborts, nothing changed
        assert bad.get(ref)["password"] == "rotate-me"
    finally:
        os.environ.pop("INTEGRATION_MASTER_KEYS", None)
        if old_env:
            os.environ["INTEGRATION_MASTER_KEYS"] = old_env
        v1.delete(ref)
