"""Database connectors: PostgreSQL, Oracle, Microsoft SQL Server (DI Phase 2).

Security model (every connector):
- READ-ONLY: every operation runs in ONE transaction that is read-only where the database supports it (PostgreSQL BEGIN READ ONLY,
  Oracle SET TRANSACTION READ ONLY, SQL Server ApplicationIntent=ReadOnly), with autocommit OFF and an unconditional ROLLBACK.
  Object and column names come from the source catalogue and are quoted; values are bound parameters (no SQL injection).
  Custom queries pass the parsed SELECT-only guard (sqlsafe) and run with a server-side timeout and a row cap.
- WRITE-PRIVILEGE DETECTION: test_connection() inspects the account's privileges and WARNS when it could write
  (the connector still never writes, but a read-only account is required by policy).
- TLS: on by default with certificate verification; may only be relaxed in DEV (policy_errors).
- SSH tunnel (pinned host key) for databases that are not internet-accessible.
- Plain-language errors with a fix hint (DNS, refused, timeout, TLS, login, missing database, missing grants, missing driver).
"""
from __future__ import annotations

import abc
import datetime as dt
import decimal
import re
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from .base import PREVIEW_LIMIT, BaseConnector, ConnectorMeta, Field, SourceField, SourceObject, TestResult
from .sqlsafe import MAX_ROWS, tainted_outputs, validate_select, wrap_limit
from .tunnel import SshTunnel

CONNECT_TIMEOUT = 10
DEFAULT_QUERY_TIMEOUT = 30


def _b(v, default=False) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _clean(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(v))} bytes>"
    if isinstance(v, uuid.UUID):
        return str(v)
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


# ------------------------------------------------------------------ plain-language errors
_HINTS = [
    ("DRIVER", r"IM002|data source name not found|can't open lib|no default driver|ODBC Driver .* not installed",
     "The Microsoft ODBC Driver 18 for SQL Server is not installed on the backend server. Install it (free, from Microsoft) and restart the backend."),
    ("SSH", r"ssh host key|SSH|paramiko|Authentication failed\.|bastion",
     "The SSH tunnel could not be opened. Check the bastion host, user, key and the pinned host fingerprint with IT."),
    ("DNS", r"could not translate host|getaddrinfo|name or service not known|nodename nor servname|11001|unknown host|DPY-6005.*(resolve|getaddrinfo)|No such host",
     "The server name could not be found. Check the spelling. If the database is only reachable inside your network, use an SSH tunnel (or the on-prem agent, coming later)."),
    ("TLS", r"ssl|tls|certificate|SSL Provider|encrypt|DPY-6004|handshake",
     "The secure (TLS) connection failed. Check that TLS is enabled on the server and that its certificate is valid for this host name. Only in DEV may you switch off certificate verification."),
    ("AUTH", r"password authentication failed|login failed|ORA-01017|invalid username|28P01|28000|18456|authentication failed|no pg_hba",
     "The username or password was rejected. Check the credentials of the read-only account (the password is stored in the vault - re-enter it with Edit)."),
    ("DATABASE", r"database .* does not exist|ORA-12514|DPY-6001|cannot open database|4060|3D000|service .* not known|ORA-12505",
     "The database / service name was not found on that server. Check the database name (Oracle: the service name, not the SID)."),
    ("REFUSED", r"connection refused|actively refused|10061|ORA-12541|DPY-6005|no listener",
     "The server refused the connection. Check the port and that the database is running; a firewall may be blocking the backend."),
    ("TIMEOUT", r"timed out|timeout expired|10060|ORA-12170|HYT00|HYT01|DPY-4024|canceling statement due to statement timeout|DPI-1067",
     "No answer in time. Check the host and port; a firewall or VPN is probably blocking the backend. If a query timed out, raise the query timeout or add filters."),
    ("GRANTS", r"permission denied|insufficient privileges|ORA-01031|ORA-00942|42501|\b229\b|SELECT permission was denied",
     "Connected, but this account cannot read the requested object. Ask the DBA to grant SELECT (read) on it - nothing more."),
    ("READONLY", r"read-only transaction|cannot execute .* in a read-only|ORA-01456|25006",
     "Blocked: the connector only reads. Writes are never allowed."),
]


def explain(e: Exception) -> tuple[str, str, str]:
    raw = " ".join(str(e).split())[:400]
    for code, pat, hint in _HINTS:
        if re.search(pat, raw, re.I):
            return code, raw, hint
    return "OTHER", raw, "Check the host, port, database name and credentials. The technical message below helps your DBA."


class SourceError(Exception):
    def __init__(self, e: Exception):
        self.code, self.raw, self.hint = explain(e)
        super().__init__(f"{self.hint} (technical: {self.raw})")


# ------------------------------------------------------------------ fields
def db_fields(default_port: int, database_label: str = "Database", extra: list[Field] | None = None) -> list[Field]:
    return [Field("host", "Host", required=True, help="Server name or IP reachable from the backend (or from the SSH bastion)"),
            Field("port", "Port", "number", True, default_port),
            Field("database", database_label, required=True),
            Field("username", "Username", required=True, help="Use a READ-ONLY account (SELECT only) - the test warns if it can write"),
            Field("password", "Password", "secret", True),
            Field("schema", "Schema (optional)", help="Limit discovery to one schema, e.g. demo_erp / SAPSR3 / dbo"),
            Field("ssl", "Require TLS (encrypted connection)", "boolean", default=True, group="tls", help="Required outside DEV"),
            Field("tls_verify", "Verify server certificate", "boolean", default=True, group="tls", help="Required outside DEV"),
            *(extra or []),
            Field("query_timeout_s", "Query timeout (seconds)", "number", default=DEFAULT_QUERY_TIMEOUT, group="advanced"),
            Field("ssh_host", "SSH bastion host", group="ssh", help="Only for databases not reachable from the internet"),
            Field("ssh_port", "SSH port", "number", default=22, group="ssh"),
            Field("ssh_user", "SSH user", group="ssh"),
            Field("ssh_host_fingerprint", "Bastion host-key fingerprint", group="ssh", help="SHA256:... from IT - the tunnel refuses any other key"),
            Field("ssh_private_key", "SSH private key", "secret_text", group="ssh", help="OpenSSH / PEM; stored in the vault"),
            Field("ssh_key_passphrase", "Key passphrase", "secret", group="ssh"),
            Field("ssh_password", "SSH password (if no key)", "secret", group="ssh")]


# ------------------------------------------------------------------ base
class SqlConnector(BaseConnector, abc.ABC):
    key: str
    default_port: int
    ph = "%s"                                  # bind placeholder

    # ---- dialect hooks
    @abc.abstractmethod
    def _connect(self, host: str, port: int, via_tunnel: bool): ...

    def _begin(self, conn, cur) -> None: ...

    @abc.abstractmethod
    def _q(self, ident: str) -> str: ...

    @abc.abstractmethod
    def _limit(self, select_from: str, n: int) -> str: ...

    @abc.abstractmethod
    def _server_info(self, conn, cur) -> dict: ...

    @abc.abstractmethod
    def _objects(self, cur, schema: str | None, name: str | None = None) -> list[tuple]: ...      # (schema, name, kind, est)

    @abc.abstractmethod
    def _columns(self, cur, schemas: list[str], table: str | None = None) -> list[tuple]: ...      # (schema, table, col, type, nullable)

    @abc.abstractmethod
    def _pkeys(self, cur, schemas: list[str]) -> set[tuple]: ...                                  # {(schema, table, col)}

    @abc.abstractmethod
    def _privileges(self, cur) -> dict: ...

    # ---- policy
    @classmethod
    def policy_errors(cls, config: dict, secrets: dict, environment: str) -> list[str]:
        errs = []
        if environment != "DEV" and not _b(config.get("ssl"), True):
            errs.append("TLS is required for TEST and PROD connections")
        if environment != "DEV" and not _b(config.get("tls_verify"), True):
            errs.append("Server-certificate verification can only be switched off in DEV")
        if config.get("ssh_host"):
            if not config.get("ssh_user") or not config.get("ssh_host_fingerprint"):
                errs.append("SSH tunnel: user and the bastion host-key fingerprint are required")
            if not (secrets.get("ssh_private_key") or secrets.get("ssh_password")):
                errs.append("SSH tunnel: a private key or password is required")
        try:
            int(config.get("port") or cls.default_port)
        except (TypeError, ValueError):
            errs.append("Port must be a number")
        return errs

    # ---- session
    @property
    def timeout_s(self) -> int:
        try:
            return max(1, min(int(self.config.get("query_timeout_s") or DEFAULT_QUERY_TIMEOUT), 600))
        except (TypeError, ValueError):
            return DEFAULT_QUERY_TIMEOUT

    @contextmanager
    def _session(self):
        """One read-only transaction; ALWAYS rolled back and closed. Opens / closes the SSH tunnel when configured."""
        tun, conn = None, None
        host, port = self.config.get("host"), int(self.config.get("port") or self.default_port)
        try:
            if self.config.get("ssh_host"):
                tun = SshTunnel(self.config["ssh_host"], self.config.get("ssh_port") or 22, self.config.get("ssh_user"),
                                self.config.get("ssh_host_fingerprint"), host, port, self.secrets.get("ssh_private_key"),
                                self.secrets.get("ssh_key_passphrase"), self.secrets.get("ssh_password"), CONNECT_TIMEOUT).__enter__()
                port = tun.local_port
            conn = self._connect(host, port, bool(tun))
            cur = conn.cursor()
            self._begin(conn, cur)
            yield conn, cur
        except (SourceError, ValueError):
            raise
        except Exception as e:  # noqa: BLE001
            raise SourceError(e) from None
        finally:
            if conn is not None:
                for f in (conn.rollback, conn.close):
                    try:
                        f()
                    except Exception:  # noqa: BLE001
                        pass
            if tun:
                tun.__exit__(None, None, None)

    def _split(self, obj: str) -> tuple[str, str]:
        if "." not in obj:
            raise ValueError("object must be schema.table")
        s, t = obj.split(".", 1)
        return s, t

    def _resolve(self, cur, obj: str) -> tuple[str, str, list[tuple]]:
        """Only objects that exist in the source catalogue (and are readable) can be queried: names are never taken raw."""
        s, t = self._split(obj)
        if not self._objects(cur, s, t):
            raise ValueError(f"{obj} was not found or this account cannot read it")
        return s, t, self._columns(cur, [s], t)

    def _fetch(self, cur, n: int) -> tuple[list[str], list[dict]]:
        cols = [d[0] for d in cur.description or []]
        return cols, [{c: _clean(v) for c, v in zip(cols, r)} for r in cur.fetchmany(n)]

    # ---- contract
    def test_connection(self) -> TestResult:
        t0 = time.perf_counter()
        try:
            with self._session() as (conn, cur):
                info = self._server_info(conn, cur)
                priv = self._privileges(cur)
                readable = len(self._objects(cur, self.config.get("schema") or None))
        except SourceError as e:
            return TestResult(False, f"Could not connect ({e.code.lower()}).", e.hint, {"error": e.raw, "category": e.code})
        details = {"latency_ms": round((time.perf_counter() - t0) * 1000), **info, "via_ssh_tunnel": bool(self.config.get("ssh_host")),
                   "readable_objects": readable, "read_only_account": not priv["can_write"], "session": "read-only, rolled back"}
        warnings = []
        if priv["can_write"]:
            warnings.append("This account has WRITE privileges (" + "; ".join(priv["findings"][:4]) + "). Policy requires a read-only account - "
                            "ask the DBA for one with SELECT only. The connector itself never writes (read-only session, always rolled back).")
        if not info.get("tls"):
            warnings.append("The connection is NOT encrypted (TLS off). Allowed in DEV only.")
        if readable == 0:
            warnings.append("Connected, but this account cannot read any table" + (f" in schema {self.config.get('schema')}" if self.config.get("schema") else "")
                            + ". Ask the DBA to grant SELECT.")
        details["privileges"] = priv
        details["warnings"] = warnings
        msg = f"Connected to {info.get('server', 'the database')}" + (" · WARNING: account can write" if priv["can_write"] else " · read-only account")
        return TestResult(True, msg, warnings[0] if warnings else None, details)

    def discover_schema(self) -> list[SourceObject]:
        with self._session() as (_, cur):
            objs = self._objects(cur, self.config.get("schema") or None)
            schemas = sorted({o[0] for o in objs})
            cols = self._columns(cur, schemas) if schemas else []
            pks = self._pkeys(cur, schemas) if schemas else set()
        by: dict[tuple, list] = {}
        for s, t, c, ty, nul in cols:
            by.setdefault((s, t), []).append(SourceField(c, str(ty), bool(nul), (s, t, c) in pks, self.is_sensitive(c)))
        return [SourceObject(f"{s}.{t}", kind, by.get((s, t), []), int(est) if est is not None and est >= 0 else None) for s, t, kind, est in objs]

    def _preview(self, obj: str, limit: int) -> list[dict]:
        with self._session() as (_, cur):
            s, t, _ = self._resolve(cur, obj)
            cur.execute(self._limit(f"{self._q(s)}.{self._q(t)}", min(limit, PREVIEW_LIMIT)))
            return self._fetch(cur, PREVIEW_LIMIT)[1]

    def extract(self, obj: str, watermark_column: str | None = None, since=None, batch_size: int = 5000) -> Iterator[list[dict]]:
        with self._session() as (_, cur):
            s, t, cols = self._resolve(cur, obj)
            sql, params = f"SELECT * FROM {self._q(s)}.{self._q(t)}", []
            if watermark_column:
                if watermark_column not in {c[2] for c in cols}:
                    raise ValueError(f"watermark column {watermark_column} is not a column of {obj}")
                w = self._q(watermark_column)
                if since is not None:                                   # >= : boundary rows re-read, upserts are idempotent
                    sql += f" WHERE {w} >= {self.ph}"
                    params.append(since)
                sql += f" ORDER BY {w}"
            cur.execute(sql, params) if params else cur.execute(sql)
            cnames = [d[0] for d in cur.description]
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                yield [{c: _clean(v) for c, v in zip(cnames, r)} for r in rows]

    def extract_query(self, sql: str, watermark_column: str | None = None, since=None, batch_size: int = 5000) -> Iterator[list[dict]]:
        """Batched extraction from a validated SELECT (joins / aggregations, e.g. EKKO+EKPO+EKET). Same read-only session."""
        q = validate_select(sql, self.key)
        outer, params = f"SELECT * FROM ({q}) src", []
        if watermark_column:
            w = f"src.{self._q(watermark_column)}"
            if since is not None:
                outer += f" WHERE {w} >= {self.ph}"
                params.append(since)
            outer += f" ORDER BY {w}"
        with self._session() as (_, cur):
            cur.execute(outer, params) if params else cur.execute(outer)
            cnames = [d[0] for d in cur.description]
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                yield [{c: _clean(v) for c, v in zip(cnames, r)} for r in rows]

    def query_columns(self, sql: str) -> list[str]:
        """Result column names of a validated SELECT (no rows fetched)."""
        q = validate_select(sql, self.key)
        with self._session() as (_, cur):
            cur.execute(wrap_limit(q, self.key, 1) if self.key != "sqlserver" else q)
            return [d[0] for d in cur.description]

    def run_query(self, sql: str, limit: int = PREVIEW_LIMIT) -> dict:
        q = validate_select(sql, self.key)
        n = max(1, min(int(limit or PREVIEW_LIMIT), MAX_ROWS))
        t0 = time.perf_counter()
        with self._session() as (_, cur):
            cur.execute(wrap_limit(q, self.key, n, probe=1))
            cols, rows = self._fetch(cur, n + 1)
            more = len(rows) > n
            rows = rows[:n]
        # mask by derivation, not just by output name: `SELECT netpr AS p`, sum(netwr), CTE aliases, row_to_json(t) are all masked
        taint = tainted_outputs(q, self.key, self.is_sensitive)
        masked = [c for i, c in enumerate(cols) if self.is_sensitive(c) or c.lower() in taint or f"#{i}" in taint]
        for r in rows:
            for c in masked:
                if r.get(c) is not None:
                    r[c] = "•••"
        return {"columns": cols, "masked_columns": masked, "rows": rows, "row_count": len(rows), "truncated": more,
                "limit": n, "elapsed_ms": round((time.perf_counter() - t0) * 1000), "timeout_s": self.timeout_s}


# ------------------------------------------------------------------ PostgreSQL
class PostgresConnector(SqlConnector):
    key, default_port, ph = "postgresql", 5432, "%s"
    meta = ConnectorMeta("postgresql", "PostgreSQL", "Databases", "PostgreSQL 11+ (incl. managed: Azure, AWS RDS, Neon). Read-only sessions, TLS verified.",
                         "database", "available", "2", True, db_fields(5432), licence_note="psycopg: LGPL-3.0")

    def _connect(self, host, port, via_tunnel):
        import certifi
        import psycopg
        kw = dict(host=host, port=port, dbname=self.config.get("database"), user=self.config.get("username"), password=self.secrets.get("password"),
                  connect_timeout=CONNECT_TIMEOUT, application_name="spa-data-integration")
        if via_tunnel:
            kw["hostaddr"] = "127.0.0.1"                    # TCP via the tunnel; `host` stays the real name for TLS verification / SNI
        if _b(self.config.get("ssl"), True):
            verify = _b(self.config.get("tls_verify"), True)
            kw["sslmode"] = "verify-full" if verify else "require"
            if verify:
                kw["sslrootcert"] = certifi.where()
        else:
            kw["sslmode"] = "prefer"
        conn = psycopg.connect(**kw)
        conn.read_only = True                               # every transaction: BEGIN READ ONLY
        return conn

    def _begin(self, conn, cur):
        cur.execute(f"SET LOCAL statement_timeout = {self.timeout_s * 1000}")
        cur.execute("SET LOCAL lock_timeout = 5000")

    def _q(self, ident):
        return '"' + ident.replace('"', '""') + '"'

    def _limit(self, src, n):
        return f"SELECT * FROM {src} LIMIT {int(n)}"

    def _server_info(self, conn, cur):
        cur.execute("SELECT current_setting('server_version'), current_user, current_database()")
        v, u, d = cur.fetchone()
        # Client-side libpq state is authoritative for OUR link (managed services such as Neon terminate TLS at a proxy,
        # so the server's pg_stat_ssl would show the proxy hop, not ours).
        tls = bool(conn.pgconn.ssl_in_use)
        verified = tls and _b(self.config.get("tls_verify"), True)
        return {"server": f"PostgreSQL {v}", "user": u, "database": d, "tls": tls,
                "tls_mode": "verify-full (certificate + host name checked)" if verified else ("encrypted, certificate NOT verified" if tls else "none"),
                "certificate_verified": verified}

    _SYS = "n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg\\_%%'"

    def _objects(self, cur, schema, name=None):
        sql = (f"SELECT n.nspname, c.relname, CASE WHEN c.relkind IN ('v','m') THEN 'view' ELSE 'table' END, c.reltuples::bigint "
               f"FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r','p','v','m') AND {self._SYS} "
               f"AND has_table_privilege(c.oid, 'SELECT')")
        p = []
        if schema:
            sql += " AND n.nspname = %s"
            p.append(schema)
        if name:
            sql += " AND c.relname = %s"
            p.append(name)
        cur.execute(sql + " ORDER BY 1, 2 LIMIT 500", p)
        return cur.fetchall()

    def _columns(self, cur, schemas, table=None):
        sql = ("SELECT table_schema, table_name, column_name, data_type, is_nullable = 'YES' FROM information_schema.columns "
               "WHERE table_schema = ANY(%s)")
        p = [list(schemas)]
        if table:
            sql += " AND table_name = %s"
            p.append(table)
        cur.execute(sql + " ORDER BY table_schema, table_name, ordinal_position", p)
        return cur.fetchall()

    def _pkeys(self, cur, schemas):
        cur.execute("""SELECT n.nspname, c.relname, a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid
                       JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
                       WHERE i.indisprimary AND n.nspname = ANY(%s)""", [list(schemas)])
        return {tuple(r) for r in cur.fetchall()}

    def _privileges(self, cur):
        cur.execute("""SELECT r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolbypassrls, has_database_privilege(current_database(), 'CREATE'),
                              current_setting('default_transaction_read_only') = 'on', pg_is_in_recovery()
                       FROM pg_roles r WHERE r.rolname = current_user""")
        sup, crole, cdb, bypass, dbcreate, ro_default, replica = cur.fetchone()
        cur.execute(f"""SELECT n.nspname || '.' || c.relname, count(*) OVER () FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE c.relkind IN ('r','p') AND {self._SYS} AND (has_table_privilege(c.oid,'INSERT') OR has_table_privilege(c.oid,'UPDATE')
                        OR has_table_privilege(c.oid,'DELETE') OR has_table_privilege(c.oid,'TRUNCATE')) ORDER BY 1 LIMIT 10""")
        w = cur.fetchall()
        cur.execute(f"SELECT n.nspname FROM pg_namespace n WHERE has_schema_privilege(n.oid, 'CREATE') AND {self._SYS} ORDER BY 1")
        cs = [r[0] for r in cur.fetchall()]
        f = []
        if sup:
            f.append("superuser")
        if crole or cdb:
            f.append("can create roles / databases")
        if w:
            f.append(f"INSERT/UPDATE/DELETE on {w[0][1]} table(s), e.g. {', '.join(r[0] for r in w[:3])}")
        if cs:
            f.append(f"CREATE in schema(s) {', '.join(cs[:5])}")
        if dbcreate:
            f.append("CREATE on the database")
        return {"can_write": bool(f), "admin": bool(sup), "findings": f, "writable_tables": int(w[0][1]) if w else 0,
                "mitigations": [m for m, on in (("database default is read-only", ro_default), ("server is a read replica", replica)) if on],
                "bypass_rls": bool(bypass)}


# ------------------------------------------------------------------ Oracle
class OracleConnector(SqlConnector):
    key, default_port, ph = "oracle", 1521, ":1"
    meta = ConnectorMeta("oracle", "Oracle Database", "Databases", "Oracle 12c+ via python-oracledb thin mode (no Oracle client needed). Read-only transactions, TCPS.",
                         "database", "available", "2", True,
                         db_fields(2484, "Service name", [Field("ssl_server_cert_dn", "Server certificate DN (optional)", group="tls",
                                                               help="Needed when TLS goes through an SSH tunnel")]),
                         licence_note="python-oracledb: Apache-2.0 / UPL")

    def _connect(self, host, port, via_tunnel):
        import oracledb
        kw = dict(user=self.config.get("username"), password=self.secrets.get("password"), host=host, port=port,
                  service_name=self.config.get("database"), tcp_connect_timeout=CONNECT_TIMEOUT, retry_count=0)
        if _b(self.config.get("ssl"), True):
            kw.update(protocol="tcps", ssl_server_dn_match=_b(self.config.get("tls_verify"), True) and not (via_tunnel and not self.config.get("ssl_server_cert_dn")))
            if self.config.get("ssl_server_cert_dn"):
                kw["ssl_server_cert_dn"] = self.config["ssl_server_cert_dn"]
        conn = oracledb.connect(**kw)
        conn.call_timeout = self.timeout_s * 1000
        conn.autocommit = False
        return conn

    def _begin(self, conn, cur):
        cur.execute("SET TRANSACTION READ ONLY")

    def _q(self, ident):
        return '"' + ident.replace('"', '""') + '"'

    def _limit(self, src, n):
        return f"SELECT * FROM {src} FETCH FIRST {int(n)} ROWS ONLY"

    def _server_info(self, conn, cur):
        cur.execute("SELECT USER, SYS_CONTEXT('USERENV','DB_NAME'), SYS_CONTEXT('USERENV','NETWORK_PROTOCOL') FROM dual")
        u, d, proto = cur.fetchone()
        return {"server": f"Oracle {conn.version}", "user": u, "database": d, "tls": str(proto or "").lower() == "tcps",
                "certificate_verified": str(proto or "").lower() == "tcps" and _b(self.config.get("tls_verify"), True)}

    def _schema(self, cur, schema):
        if schema:
            return schema.upper() if schema == schema.lower() else schema
        cur.execute("SELECT SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM dual")
        return cur.fetchone()[0]

    def _objects(self, cur, schema, name=None):
        s = self._schema(cur, schema)
        sql = ("SELECT owner, table_name, 'table', num_rows FROM all_tables WHERE owner = :s" + (" AND table_name = :n" if name else "")
               + " UNION ALL SELECT owner, view_name, 'view', NULL FROM all_views WHERE owner = :s" + (" AND view_name = :n" if name else ""))
        cur.execute(f"SELECT * FROM ({sql}) ORDER BY 1, 2 FETCH FIRST 500 ROWS ONLY", {"s": s, "n": name} if name else {"s": s})
        return cur.fetchall()

    def _columns(self, cur, schemas, table=None):
        out = []
        for s in schemas:
            cur.execute("SELECT owner, table_name, column_name, data_type, CASE nullable WHEN 'Y' THEN 1 ELSE 0 END FROM all_tab_columns WHERE owner = :1"
                        + (" AND table_name = :2" if table else "") + " ORDER BY table_name, column_id", [s, table] if table else [s])
            out += cur.fetchall()
        return out

    def _pkeys(self, cur, schemas):
        out = set()
        for s in schemas:
            cur.execute("""SELECT c.owner, c.table_name, cc.column_name FROM all_constraints c JOIN all_cons_columns cc
                           ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name WHERE c.constraint_type = 'P' AND c.owner = :1""", [s])
            out |= {tuple(r) for r in cur.fetchall()}
        return out

    def _privileges(self, cur):
        cur.execute("""SELECT privilege FROM session_privs WHERE privilege LIKE 'INSERT ANY%' OR privilege LIKE 'UPDATE ANY%' OR privilege LIKE 'DELETE ANY%'
                       OR privilege LIKE 'DROP ANY%' OR privilege LIKE 'ALTER ANY%' OR privilege LIKE 'CREATE ANY%'
                       OR privilege IN ('CREATE TABLE', 'CREATE PROCEDURE', 'EXECUTE ANY PROCEDURE', 'SYSDBA', 'GRANT ANY PRIVILEGE')""")
        sp = [r[0] for r in cur.fetchall()]
        cur.execute("""SELECT table_schema || '.' || table_name, privilege FROM all_tab_privs WHERE privilege IN ('INSERT','UPDATE','DELETE','ALTER')
                       AND grantee IN (SELECT USER FROM dual UNION SELECT role FROM session_roles) FETCH FIRST 10 ROWS ONLY""")
        tp = cur.fetchall()
        cur.execute("SELECT role FROM session_roles WHERE role IN ('DBA','RESOURCE','IMP_FULL_DATABASE','DATAPUMP_IMP_FULL_DATABASE')")
        roles = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) FROM user_tables")
        owns = cur.fetchone()[0]
        f = []
        if roles:
            f.append("role(s) " + ", ".join(roles))
        if sp:
            f.append("system privileges " + ", ".join(sp[:5]))
        if tp:
            f.append("object grants e.g. " + ", ".join(f"{p} on {t}" for t, p in tp[:3]))
        if owns:
            f.append(f"owns {owns} table(s) (an owner can always write them)")
        return {"can_write": bool(f), "admin": "DBA" in roles, "findings": f, "writable_tables": len(tp) + owns,
                "mitigations": ["every transaction is SET TRANSACTION READ ONLY"]}


# ------------------------------------------------------------------ SQL Server
def _odbc_value(v) -> str:
    return "{" + str(v if v is not None else "").replace("}", "}}") + "}"


class SqlServerConnector(SqlConnector):
    key, default_port, ph = "sqlserver", 1433, "?"
    meta = ConnectorMeta("sqlserver", "Microsoft SQL Server", "Databases", "SQL Server 2016+ / Azure SQL via ODBC Driver 18 (Encrypt=yes). Source only.",
                         "database", "available", "2", True, db_fields(1433), licence_note="pyodbc: MIT · Microsoft ODBC Driver 18: free")

    @staticmethod
    def driver() -> str | None:
        import pyodbc
        ds = [d for d in pyodbc.drivers() if re.match(r"ODBC Driver \d+ for SQL Server", d)]
        return max(ds, key=lambda d: int(re.search(r"\d+", d).group())) if ds else None

    def _connect(self, host, port, via_tunnel):
        import pyodbc
        drv = self.driver()
        if not drv:
            raise RuntimeError("IM002 ODBC Driver 18 for SQL Server not installed")
        ssl, verify = _b(self.config.get("ssl"), True), _b(self.config.get("tls_verify"), True)
        server = f"tcp:127.0.0.1,{port}" if via_tunnel else f"tcp:{host},{port}"
        cs = (f"DRIVER={{{drv}}};SERVER={server};DATABASE={_odbc_value(self.config.get('database'))};UID={_odbc_value(self.config.get('username'))};"
              f"PWD={_odbc_value(self.secrets.get('password'))};Encrypt={'yes' if ssl else 'no'};TrustServerCertificate={'no' if verify else 'yes'};"
              f"ApplicationIntent=ReadOnly;Connection Timeout={CONNECT_TIMEOUT};APP=spa-data-integration")
        if via_tunnel and ssl:
            cs += f"HostNameInCertificate={host};"
        conn = pyodbc.connect(cs, autocommit=False, timeout=CONNECT_TIMEOUT)
        conn.timeout = self.timeout_s
        return conn

    def _begin(self, conn, cur):
        cur.execute("SET LOCK_TIMEOUT 5000")

    def _q(self, ident):
        return "[" + ident.replace("]", "]]") + "]"

    def _limit(self, src, n):
        return f"SELECT TOP ({int(n)}) * FROM {src}"

    def _server_info(self, conn, cur):
        cur.execute("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50)), SUSER_SNAME(), DB_NAME(), "
                    "(SELECT encrypt_option FROM sys.dm_exec_connections WHERE session_id = @@SPID), DATABASEPROPERTYEX(DB_NAME(), 'Updateability')")
        v, u, d, enc, upd = cur.fetchone()
        return {"server": f"SQL Server {v}", "user": u, "database": d, "tls": str(enc or "").upper() == "TRUE",
                "certificate_verified": str(enc or "").upper() == "TRUE" and _b(self.config.get("tls_verify"), True), "database_updateability": upd}

    def _objects(self, cur, schema, name=None):
        sql = ("SELECT s.name, o.name, CASE o.type WHEN 'V' THEN 'view' ELSE 'table' END, "
               "(SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id = o.object_id AND p.index_id IN (0, 1)) "
               "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0 "
               "AND HAS_PERMS_BY_NAME(QUOTENAME(s.name) + '.' + QUOTENAME(o.name), 'OBJECT', 'SELECT') = 1")
        p = []
        if schema:
            sql += " AND s.name = ?"
            p.append(schema)
        if name:
            sql += " AND o.name = ?"
            p.append(name)
        cur.execute("SELECT TOP 500 * FROM (" + sql + ") x ORDER BY 1, 2", p)
        return [tuple(r) for r in cur.fetchall()]

    def _columns(self, cur, schemas, table=None):
        marks = ",".join("?" * len(schemas))
        sql = (f"SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, CASE IS_NULLABLE WHEN 'YES' THEN 1 ELSE 0 END FROM INFORMATION_SCHEMA.COLUMNS "
               f"WHERE TABLE_SCHEMA IN ({marks})" + (" AND TABLE_NAME = ?" if table else "") + " ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION")
        cur.execute(sql, [*schemas, table] if table else list(schemas))
        return [tuple(r) for r in cur.fetchall()]

    def _pkeys(self, cur, schemas):
        marks = ",".join("?" * len(schemas))
        cur.execute(f"""SELECT k.TABLE_SCHEMA, k.TABLE_NAME, k.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS t
                        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k ON k.CONSTRAINT_NAME = t.CONSTRAINT_NAME AND k.TABLE_SCHEMA = t.TABLE_SCHEMA
                        WHERE t.CONSTRAINT_TYPE = 'PRIMARY KEY' AND t.TABLE_SCHEMA IN ({marks})""", list(schemas))
        return {tuple(r) for r in cur.fetchall()}

    def _privileges(self, cur):
        cur.execute("SELECT IS_SRVROLEMEMBER('sysadmin'), IS_ROLEMEMBER('db_owner'), IS_ROLEMEMBER('db_datawriter'), IS_ROLEMEMBER('db_ddladmin'), "
                    "DATABASEPROPERTYEX(DB_NAME(), 'Updateability')")
        sa, owner, writer, ddl, upd = cur.fetchone()
        cur.execute("SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE') WHERE permission_name IN "
                    "('INSERT','UPDATE','DELETE','ALTER','CONTROL','CREATE TABLE','ALTER ANY SCHEMA')")
        perms = sorted({r[0] for r in cur.fetchall()})
        cur.execute("""SELECT TOP 10 s.name + '.' + o.name FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE o.type = 'U'
                       AND (HAS_PERMS_BY_NAME(QUOTENAME(s.name) + '.' + QUOTENAME(o.name), 'OBJECT', 'INSERT') = 1
                         OR HAS_PERMS_BY_NAME(QUOTENAME(s.name) + '.' + QUOTENAME(o.name), 'OBJECT', 'UPDATE') = 1
                         OR HAS_PERMS_BY_NAME(QUOTENAME(s.name) + '.' + QUOTENAME(o.name), 'OBJECT', 'DELETE') = 1)""")
        tw = [r[0] for r in cur.fetchall()]
        f = [n for n, on in (("sysadmin", sa == 1), ("db_owner", owner == 1), ("db_datawriter", writer == 1), ("db_ddladmin", ddl == 1)) if on]
        if perms:
            f.append("database permissions " + ", ".join(perms))
        if tw:
            f.append("write on e.g. " + ", ".join(tw[:3]))
        return {"can_write": bool(f), "admin": sa == 1 or owner == 1, "findings": f, "writable_tables": len(tw),
                "mitigations": ["database is READ_ONLY"] if str(upd).upper() == "READ_ONLY" else []}


DB_CONNECTORS = {c.key: c for c in (PostgresConnector, OracleConnector, SqlServerConnector)}
