"""Data Integration Phase 6: cross-cutting security regression suite.

- RBAC on EVERY /api/integration route (from the live route table): suppliers never; writes ADMIN-only; source-data reads ADMIN-only
- page flag: every list endpoint is off when data_integration is off
- no secret value (decrypted from the vault in memory) appears in audit log, connection config, runs, job logs or quarantine
- injection payloads against query / lineage / mapping / rule APIs are refused or treated as data
- master-key rotation (temp key file, one test secret - real secrets untouched)
"""
import json
import re
from pathlib import Path

import pytest

from app import auth as AU

SOURCE_DATA_GET = re.compile(r"/(preview|schema|quarantine|quarantine-queue)$")


def _routes():
    from app.main import app
    out = []
    for r in app.routes:
        p = getattr(r, "path", "")
        if p.startswith("/api/integration"):
            for m in sorted(getattr(r, "methods", []) or []):
                if m in ("HEAD", "OPTIONS"):
                    continue
                out.append((m, re.sub(r"\{[^}]+\}", "X1", p)))
    return out


def test_every_integration_route_has_the_right_policy():
    routes = _routes()
    assert len(routes) >= 40, len(routes)                                                  # guard: the route table was read
    for m, p in routes:
        assert AU.allowed(m, p, "ADMIN"), (m, p)
        assert not AU.allowed(m, p, "SUPPLIER"), (m, p)                                   # suppliers: never
        if m != "GET":
            for role in ("VIEWER", "PROCUREMENT", "QUALITY"):
                assert not AU.allowed(m, p, role), (m, p, role)                          # every change is ADMIN-only
        elif SOURCE_DATA_GET.search(p):
            for role in ("VIEWER", "PROCUREMENT", "QUALITY"):
                assert not AU.allowed(m, p, role), (m, p, role)                          # source data: ADMIN-only


def _db():
    try:
        from app import analytics as A
        A._run("SELECT 1", {}, True)
        return True
    except Exception:
        return False


db = pytest.mark.skipif(not _db(), reason="database not reachable")


def H(u="pytest-admin-a", r="ADMIN"):
    return {"Authorization": "Bearer " + AU.issue_token(AU.UserCtx(u, r))}


@db
def test_page_flag_switches_everything_off():
    from fastapi.testclient import TestClient
    from app.features import all_flags, set_flag
    from app.main import app
    cl = TestClient(app)
    before = all_flags(force=True).get("data_integration", False)
    set_flag("data_integration", False, "pytest")
    try:
        lists = [p for m, p in _routes() if m == "GET" and "X1" not in p and "lineage" not in p]
        assert len(lists) >= 10
        for p in lists:
            assert cl.get(p, headers=H()).status_code != 200, p
    finally:
        set_flag("data_integration", before, "pytest")


@db
def test_no_secret_value_anywhere():
    from sqlalchemy import text
    from app.integration.dbx import engine
    from app.integration.secrets import vault
    with engine.connect() as c:
        refs = [r[0] for r in c.execute(text("SELECT ref FROM integration.secrets"))]
        blobs = [str(r[0]) for q in ("SELECT detail::text FROM integration.audit_log", "SELECT config::text FROM integration.connections",
                                     "SELECT coalesce(message, '') FROM integration.runs", "SELECT coalesce(source_values::text, '') FROM integration.quarantine",
                                     "SELECT coalesce(detail::text, '') || coalesce(error, '') FROM public.job_runs",
                                     "SELECT coalesce(body, '') FROM public.notifications")
                 for r in c.execute(text(q))]
    hay = "\n".join(blobs)
    values = [v for ref in refs for v in vault().get(ref).values() if isinstance(v, str) and len(v) >= 8]
    assert values, "expected at least one stored secret to check"
    for v in values:
        assert v not in hay                                                             # never logged, never stored in plain text


@db
def test_injection_payloads_refused_or_inert():
    from fastapi.testclient import TestClient
    from app.features import all_flags, set_flag
    from app.integration import service as S
    from app.main import app
    keys = ("data_integration", "di_database_connectors", "di_mapping_loads", "di_quality_health")
    before = {k: all_flags(force=True).get(k, False) for k in keys}
    for k in keys:
        set_flag(k, True, "pytest")
    try:
        from app.integration.sqlsafe import validate_select
        cl = TestClient(app)
        payloads = ("SELECT 1; DROP TABLE demo_erp.ekpo", "SELECT pg_sleep(30)", "WITH x AS (DELETE FROM demo_erp.ekpo RETURNING 1) SELECT * FROM x",
                    "SELECT * FROM demo_erp.ekpo FOR UPDATE", "COPY demo_erp.ekpo TO PROGRAM 'id'")
        for sql in payloads:                                                            # the guard itself, always
            with pytest.raises(Exception):
                validate_select(sql, "postgresql")
        # through the API as well when a PostgreSQL source is connected (the demo may run with only the primary Neon database)
        pg = [x for x in S.list_connections() if x["connector_key"] == "postgresql"]
        cid = pg[0]["id"] if pg else None
        if cid:
            for sql in payloads:
                assert cl.post(f"/api/integration/connections/{cid}/query", headers=H(), json={"sql": sql}).status_code == 422, sql
            r = cl.get("/api/integration/connections/" + cid + "/preview", headers=H(), params={"object": 'demo_erp.ekpo"; DROP TABLE x; --'})
            assert r.status_code in (404, 422)
        r = cl.get("/api/integration/lineage", headers=H(), params={"entity": "po_line", "key": json.dumps({"po_id": "x' OR '1'='1", "po_line": "1"})})
        assert r.status_code == 404                                                     # bound parameter: no match, no injection
        r = cl.post("/api/integration/dq-rules", headers=H(), json={"entity": "po_line", "name": "evil", "kind": "range",
                                                                     "params": {"field": "ordered_qty) OR 1=1 --", "min": 0}})
        assert r.status_code == 422                                                     # rule fields must be canonical fields
        r = cl.post("/api/integration/mappings/suggest", headers=H(), json={"connection_id": cid or "CONN-NONE", "entity": "po_line", "source_kind": "object",
                                                                             "source_object": "demo_erp.ekpo; DROP TABLE x"})
        assert r.status_code in (404, 422)
        with pytest.raises(Exception):
            from app.integration import pipeline as PL
            PL.run("MAP-0001", "manual", "pytest", target_schema="sp; DROP TABLE x")    # schema name is whitelisted by pattern
    finally:
        for k, v in before.items():
            set_flag(k, v, "pytest")


@db
def test_master_key_rotation_on_temp_key_file(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from sqlalchemy import text
    from app.integration import secrets as SEC
    from app.integration.dbx import engine
    from scripts.rotate_integration_key import rotate_key_file
    monkeypatch.delenv("INTEGRATION_MASTER_KEYS", raising=False)
    kf = tmp_path / ".integration_key"
    old = Fernet.generate_key().decode()
    kf.write_text(old, encoding="utf-8")
    monkeypatch.setattr(SEC, "_KEY_FILE", kf)
    with engine.connect() as c:
        real_before = {r[0]: r[1] for r in c.execute(text("SELECT ref, ciphertext FROM integration.secrets"))}
    ref = SEC.LocalEncryptedVault().put({"password": "rotate-me-please"}, "pytest")
    try:
        out = rotate_key_file(kf, refs=[ref])
        assert out["reencrypted"] == 1 and out["verified"] == 1 and out["retired"] == [SEC.LocalEncryptedVault._fp(old)]
        assert kf.read_text(encoding="utf-8").strip() != old and "," not in kf.read_text(encoding="utf-8")   # old key retired
        assert SEC.LocalEncryptedVault().get(ref)["password"] == "rotate-me-please"                         # readable with the new key only
        assert Path(out["backup"]).read_text(encoding="utf-8").strip() == old                               # backup kept
        with engine.connect() as c:
            after = {r[0]: r[1] for r in c.execute(text("SELECT ref, ciphertext FROM integration.secrets WHERE ref <> :r"), {"r": ref})}
        assert after == real_before                                                                        # real secrets untouched
    finally:
        SEC.LocalEncryptedVault().delete(ref)


def test_no_llm_or_external_ai_in_data_integration():
    """Rule: no source data to external services / LLMs without explicit approval. Data Integration has no LLM path at all:
    mapping suggestions are rule-based on column names. This keeps it that way."""
    banned = re.compile(r"^\s*(from|import)\s+(\.\.domo_llm|app\.domo_llm|\.\.agent|langchain\w*|openai|anthropic)\b", re.M)
    for f in Path("app/integration").glob("*.py"):
        assert not banned.search(f.read_text(encoding="utf-8")), f.name
