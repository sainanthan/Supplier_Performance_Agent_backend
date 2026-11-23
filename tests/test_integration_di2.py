"""Data Integration Phase 2 tests: database connectors (PostgreSQL live against the generated demo_erp source; Oracle / SQL Server
through their failure paths and units), SELECT-only guard, TLS policy, SSH host-key pinning, write-privilege warning, plain-language
errors, custom-query API with RBAC + audit, per-feature flag."""
import pytest

from app import auth as AU
from app.integration import registry as REG
from app.integration.db_connectors import (OracleConnector, PostgresConnector, SqlServerConnector, _odbc_value, explain)
from app.integration.sqlsafe import UnsafeQuery, validate_select, wrap_limit

ALLOWED = [("postgresql", "SELECT * FROM demo_erp.ekpo WHERE netpr > 10"),
           ("postgresql", "WITH a AS (SELECT 1 x) SELECT * FROM a UNION SELECT 2"),
           ("postgresql", "select 'DROP TABLE x' as s;"),
           ("oracle", "SELECT * FROM ekpo WHERE ROWNUM < 5"),
           ("sqlserver", "SELECT TOP 10 * FROM dbo.ekpo ORDER BY netpr")]
BLOCKED = [("postgresql", "DELETE FROM ekpo"), ("postgresql", "SELECT 1; DROP TABLE ekpo"),
           ("postgresql", "WITH d AS (DELETE FROM ekpo RETURNING *) SELECT * FROM d"), ("postgresql", "SELECT * INTO t2 FROM ekpo"),
           ("postgresql", "SELECT * FROM ekpo FOR UPDATE"), ("postgresql", "SELECT pg_sleep(100)"),
           ("postgresql", "SELECT pg_read_file('/etc/passwd')"), ("postgresql", "SELECT set_config('a','b',false)"),
           ("postgresql", "SELECT * FROM dblink('h','delete from x') AS t(a int)"), ("postgresql", "COPY ekpo TO '/tmp/x'"),
           ("postgresql", "UPDATE ekpo SET netpr = 0"), ("oracle", "SELECT dbms_lock.sleep(10) FROM dual"),
           ("oracle", "SELECT utl_http.request('http://x') FROM dual"), ("oracle", "BEGIN EXECUTE IMMEDIATE 'drop table x'; END;"),
           ("sqlserver", "EXEC xp_cmdshell 'dir'"), ("sqlserver", "SELECT * FROM OPENROWSET('SQLNCLI','x','select 1')"),
           ("sqlserver", "WAITFOR DELAY '00:00:10'"), ("sqlserver", "SELECT * INTO #t FROM ekpo"),
           ("sqlserver", "MERGE INTO a USING b ON a.x = b.x WHEN MATCHED THEN DELETE;"), ("postgresql", ""), ("demo_mock", "SELECT 1")]


# ---------------------------------------------------------------- units
@pytest.mark.parametrize("key,sql", ALLOWED)
def test_select_guard_allows(key, sql):
    assert validate_select(sql, key)


@pytest.mark.parametrize("key,sql", BLOCKED)
def test_select_guard_blocks(key, sql):
    with pytest.raises(UnsafeQuery):
        validate_select(sql, key)


def test_row_cap_wrapping():
    assert wrap_limit("SELECT 1", "postgresql", 5000).endswith("LIMIT 1000")          # hard cap 1000
    assert "ROWNUM <= 50" in wrap_limit("SELECT 1 FROM dual", "oracle", 50)


def test_catalogue_db_connectors_available_with_secure_fields():
    meta = {m["key"]: m for m in REG.catalogue()}
    for k in ("postgresql", "oracle", "sqlserver"):
        m = meta[k]
        assert m["availability"] == "available" and m["read_only"]
        f = {x["name"]: x for x in m["fields"]}
        assert f["password"]["secret"] and f["ssh_private_key"]["secret"] and f["ssh_key_passphrase"]["secret"]
        assert f["ssl"]["default"] is True and f["tls_verify"]["default"] is True and f["ssh_host_fingerprint"]["group"] == "ssh"


def test_tls_and_ssh_policy():
    base = {"host": "h", "port": 5432, "database": "d", "username": "u"}
    assert PostgresConnector.policy_errors({**base, "ssl": False}, {}, "DEV") == []               # DEV may relax
    assert PostgresConnector.policy_errors({**base, "ssl": False}, {}, "TEST")                    # TEST / PROD: TLS required
    assert OracleConnector.policy_errors({**base, "tls_verify": False}, {}, "PROD")              # ... with verification
    assert SqlServerConnector.policy_errors({**base, "ssh_host": "b"}, {}, "DEV")                 # SSH: fingerprint + key required
    assert not PostgresConnector.policy_errors({**base, "ssh_host": "b", "ssh_user": "x", "ssh_host_fingerprint": "SHA256:a"},
                                               {"ssh_private_key": "k"}, "PROD")


def test_identifier_quoting_and_odbc_escaping():
    pg, ms, ora = PostgresConnector({}), SqlServerConnector({}), OracleConnector({})
    assert pg._q('a"b') == '"a""b"' and ora._q('X"Y') == '"X""Y"' and ms._q("a]b") == "[a]]b]"
    assert _odbc_value("p}w;d=x") == "{p}}w;d=x}"                                              # no connection-string injection


@pytest.mark.parametrize("msg,code", [("could not translate host name \"x\" to address", "DNS"),
                                      ("FATAL: password authentication failed for user \"u\"", "AUTH"),
                                      ("ORA-01017: invalid username/password; logon denied", "AUTH"),
                                      ("[IM002] Data source name not found and no default driver specified", "DRIVER"),
                                      ("SSL error: certificate verify failed", "TLS"),
                                      ("permission denied for table ekpo", "GRANTS"),
                                      ("connection timeout expired", "TIMEOUT"),
                                      ("database \"nope\" does not exist", "DATABASE")])
def test_plain_language_errors(msg, code):
    c, _, hint = explain(Exception(msg))
    assert c == code and hint and "technical" not in hint


def test_ssh_host_key_pinning():
    import paramiko
    from app.integration.tunnel import _PinnedHostKey, fingerprint
    key = paramiko.RSAKey.generate(1024)
    fp = fingerprint(key)
    _PinnedHostKey(fp).missing_host_key(None, "bastion", key)                                   # matches: accepted
    for pinned in ("SHA256:wrong", ""):                                                          # mismatch or not pinned: refused
        with pytest.raises(paramiko.SSHException):
            _PinnedHostKey(pinned).missing_host_key(None, "bastion", key)


def test_oracle_and_sqlserver_failure_paths_are_plain_language():
    o = OracleConnector({"host": "127.0.0.1", "port": 1, "database": "FREEPDB1", "username": "u", "ssl": False}, {"password": "x"}).test_connection()
    assert o.ok is False and o.hint and o.details["category"] in ("REFUSED", "TIMEOUT")
    s = SqlServerConnector({"host": "127.0.0.1", "port": 1, "database": "d", "username": "u"}, {"password": "x"})
    r = s.test_connection()
    assert r.ok is False and r.hint
    if not SqlServerConnector.driver():
        assert r.details["category"] == "DRIVER" and "ODBC Driver 18" in r.hint


# ---------------------------------------------------------------- live (Neon demo_erp)
def _db():
    try:
        from app import analytics as A
        A._run("SELECT 1", {}, True)
        return True
    except Exception:
        return False


db = pytest.mark.skipif(not _db(), reason="database not reachable")


def _demo():
    from app.integration import service as S
    from scripts.gen_demo_erp import CONN_NAME
    c = [x for x in S.list_connections() if x["name"] == CONN_NAME]
    if not c:
        pytest.skip("demo_erp not generated (python -m scripts.gen_demo_erp)")
    return c[0]


@pytest.fixture
def flags():
    from app.features import all_flags, set_flag
    before = {k: all_flags(force=True).get(k, False) for k in ("data_integration", "di_database_connectors")}
    for k in before:
        set_flag(k, True, "pytest")
    yield set_flag
    for k, v in before.items():
        set_flag(k, v, "pytest")


@db
def test_postgres_live_read_only_tls_discovery_preview_extract(flags):
    from app.integration import service as S
    _, con = S.connector_for(_demo()["id"])
    t = con.test_connection()
    assert t.ok and t.details["read_only_account"] and t.details["tls"] and t.details["certificate_verified"], t.details
    objs = {o.name: o for o in con.discover_schema()}
    ekpo = objs["demo_erp.ekpo"]
    assert {f.name for f in ekpo.fields if f.key} == {"ebeln", "ebelp"} and {"netpr", "netwr"} <= {f.name for f in ekpo.fields if f.sensitive}
    assert ekpo.row_estimate and ekpo.row_estimate > 1000
    p = con.preview_data("demo_erp.lfa1")
    assert {"telf1", "smtp_addr", "stcd3"} <= set(p["masked_columns"]) and all(r["telf1"] == "•••" for r in p["rows"])
    full = sum(len(b) for b in con.extract("demo_erp.ekbe", "cpudt", None, 500))
    inc = sum(len(b) for b in con.extract("demo_erp.ekbe", "cpudt", "2026-10-25"))
    assert full >= 1000 and 0 < inc < full                                                      # incremental by watermark
    with pytest.raises(ValueError):
        list(con.extract("demo_erp.ekbe", "cpudt; drop table x", "2026-01-01"))                 # watermark must be a real column
    with pytest.raises(ValueError):
        con.preview_data('demo_erp.ekpo"; DROP TABLE x; --')                                    # object must exist in the catalogue


@db
def test_postgres_cannot_write_even_past_the_guard(flags):
    from app.integration import service as S
    from app.integration.db_connectors import SourceError
    _, con = S.connector_for(_demo()["id"])
    with pytest.raises(SourceError) as e:
        with con._session() as (_, cur):
            cur.execute("CREATE TABLE demo_erp.pytest_should_not_exist (a int)")
    assert e.value.code == "READONLY"
    with pytest.raises(SourceError):
        con.run_query("SELECT * FROM sp.dim_supplier")                                         # reader has no grant outside demo_erp


@db
def test_write_privilege_warning_for_owner_account():
    from urllib.parse import urlparse
    from app.config import settings
    u = urlparse(settings.database_url)
    t = PostgresConnector({"host": u.hostname, "port": u.port or 5432, "database": u.path.lstrip("/"), "username": u.username, "schema": "demo_erp"},
                          {"password": u.password}).test_connection()                                # NOT stored anywhere
    assert t.ok and t.details["privileges"]["can_write"] and any("WRITE privileges" in w for w in t.details["warnings"])


@db
def test_query_api_rbac_masking_audit_and_policy(flags):
    from fastapi.testclient import TestClient
    from app.main import app
    cl = TestClient(app)
    H = lambda u, r="ADMIN": {"Authorization": "Bearer " + AU.issue_token(AU.UserCtx(u, r))}  # noqa: E731
    cid = _demo()["id"]
    r = cl.post(f"/api/integration/connections/{cid}/query", headers=H("pytest-admin-a"),
                json={"sql": "SELECT ebeln, ebelp, netpr FROM demo_erp.ekpo ORDER BY ebeln", "limit": 7})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["row_count"] == 7 and d["truncated"] and d["masked_columns"] == ["netpr"]
    assert cl.post(f"/api/integration/connections/{cid}/query", headers=H("q", "QUALITY"), json={"sql": "SELECT 1"}).status_code == 403
    b = cl.post(f"/api/integration/connections/{cid}/query", headers=H("pytest-admin-a"), json={"sql": "DELETE FROM demo_erp.ekpo"})
    assert b.status_code == 422 and "blocked" in b.json()["detail"].lower()
    log = cl.get("/api/integration/audit", headers=H("v", "VIEWER"), params={"connection_id": cid, "limit": 20}).json()
    assert {"query", "query:blocked"} <= {e["action"] for e in log}
    t = cl.post("/api/integration/connections", headers=H("pytest-admin-a"), json={"connector_key": "postgresql", "name": "pytest no tls", "environment": "TEST",
                "config": {"host": "h", "port": 5432, "database": "d", "username": "u", "ssl": False}, "secrets": {"password": "p"}})
    assert t.status_code == 422 and "TLS" in t.json()["detail"]
    flags("di_database_connectors", False, "pytest")                                            # per-feature flag
    assert cl.post("/api/integration/connections", headers=H("pytest-admin-a"), json={"connector_key": "postgresql", "name": "pytest flag off",
                   "config": {"host": "h", "port": 5432, "database": "d", "username": "u"}, "secrets": {"password": "p"}}).status_code == 422
    cat = {m["key"]: m for m in cl.get("/api/integration/catalog", headers=H("v", "VIEWER")).json()}
    assert cat["oracle"]["flag_off"] and cat["oracle"]["availability"] == "planned"
    assert cl.post(f"/api/integration/connections/{cid}/test", headers=H("pytest-admin-a")).status_code == 409


MASK_BYPASS = {"SELECT ebeln, netpr AS p FROM demo_erp.ekpo": "p",
               "SELECT ebeln, netpr * 2 AS dbl FROM demo_erp.ekpo": "dbl",
               "SELECT lifnr, sum(netwr) AS total FROM demo_erp.ekko JOIN demo_erp.ekpo USING (ebeln) GROUP BY 1": "total",
               "WITH x AS (SELECT ebeln, netpr AS q FROM demo_erp.ekpo) SELECT ebeln, q FROM x": "q",
               "SELECT y.* FROM (SELECT ebeln, netpr AS z FROM demo_erp.ekpo) y": "z",
               "SELECT row_to_json(e) AS j FROM demo_erp.ekpo e": "j",
               "SELECT e FROM demo_erp.ekpo e": "e",
               "SELECT json_build_object('a', netpr) AS o FROM demo_erp.ekpo": "o",
               "SELECT ebeln, netpr + 0 FROM demo_erp.ekpo": "?column?",
               "SELECT ebeln, 1 AS a FROM demo_erp.ekko UNION ALL SELECT ebeln, netpr FROM demo_erp.ekpo": "a"}


@db
def test_custom_query_masking_cannot_be_bypassed(flags):
    """Masking follows derivation (aliases, expressions, CTEs, sub-queries, whole-row refs, unaliased, UNION), not just names."""
    from app.integration import service as S
    _, con = S.connector_for(_demo()["id"])
    for q, col in MASK_BYPASS.items():
        r = con.run_query(q, 3)
        assert col in r["masked_columns"] and all(row[col] == "•••" for row in r["rows"]), (q, r["masked_columns"])
        assert "ebeln" not in r["masked_columns"] and "lifnr" not in r["masked_columns"]
