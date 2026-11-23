"""OPC UA connector - STRICTLY READ-ONLY (DI Phase 4).

Only browse and read services are used: Browse, Read (current values) and HistoryRead (raw history). This module never calls a
write / call / add / delete service - a unit test scans this file to keep it that way.
Security: SignAndEncrypt (Basic256Sha256 by default) with a client certificate + key from the vault (written to a private temp
folder only for the session) and a PINNED server certificate (SHA-256). Security mode None is allowed in DEV only.
Objects: "snapshot" (current value of every variable under the root node, depth / count capped) and "history" (raw values since
the watermark `source_time`, for nodes the server historises). Fields: node_id, browse_path, data_type, value, status,
source_time, server_time.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from urllib.parse import urlparse

from .base import BaseConnector, ConnectorMeta, Field, SourceField, SourceObject, TestResult

FIELDS = [("node_id", "text", True), ("browse_path", "text", False), ("data_type", "text", False), ("value", "text", False),
          ("status", "text", False), ("source_time", "timestamp", False), ("server_time", "timestamp", False)]


def _val(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _ts(t):
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.isoformat()


class OpcUaConnector(BaseConnector):
    meta = ConnectorMeta("opcua", "OPC UA", "MES / shop floor",
                         "Read-only browse of tags + current values and history (no writes, no method calls). SignAndEncrypt with a pinned server certificate.",
                         "cpu", "available", "4", True,
                         [Field("endpoint_url", "Endpoint URL", required=True, help="opc.tcp://host:4840/path"),
                          Field("security_mode", "Security mode", "select", True, "SignAndEncrypt", options=["SignAndEncrypt", "Sign", "None"],
                                help="None is allowed in DEV only"),
                          Field("security_policy", "Security policy", "select", default="Basic256Sha256",
                                options=["Basic256Sha256", "Aes128_Sha256_RsaOaep", "Aes256_Sha256_RsaPss"]),
                          Field("auth", "User authentication", "select", default="anonymous", options=["anonymous", "username"]),
                          Field("username", "Username"), Field("password", "Password", "secret"),
                          Field("client_certificate", "Client certificate (PEM)", "secret_text", group="tls"),
                          Field("client_private_key", "Client private key (PEM)", "secret_text", group="tls"),
                          Field("server_cert_sha256", "Server certificate SHA-256 (pin)", group="tls", help="The connection is refused if the server presents another certificate"),
                          Field("root_node", "Root node", default="i=85", group="advanced", help="Default: the Objects folder"),
                          Field("max_depth", "Browse depth", "number", default=4, group="advanced"),
                          Field("max_nodes", "Max variables", "number", default=500, group="advanced"),
                          Field("internal_network", "Server is on the internal network", "boolean", default=True, group="advanced")], "tags")
    environment = "DEV"

    @classmethod
    def policy_errors(cls, config, secrets, environment):
        errs = []
        u = urlparse(config.get("endpoint_url") or "")
        if u.scheme != "opc.tcp" or not u.hostname:
            errs.append("the endpoint must look like opc.tcp://host:4840")
        mode = config.get("security_mode") or "SignAndEncrypt"
        if mode == "None" and environment != "DEV":
            errs.append("security mode None is only allowed in DEV - use SignAndEncrypt")
        if mode != "None":
            if not secrets.get("client_certificate") or not secrets.get("client_private_key"):
                errs.append("a client certificate and private key are required for Sign / SignAndEncrypt")
            if not config.get("server_cert_sha256"):
                errs.append("pin the server certificate (SHA-256) - no trust-on-first-use")
        if (config.get("auth") or "anonymous") == "username" and (not config.get("username") or not secrets.get("password")):
            errs.append("username and password are required for username authentication")
        return errs

    @contextmanager
    def _client(self):
        from asyncua import sync
        from .http_base import check_host
        url = self.config.get("endpoint_url")
        check_host(urlparse(url).hostname, bool(self.config.get("internal_network", True)), self.environment)
        tmp = None
        c = sync.Client(url, timeout=15)
        try:
            mode = self.config.get("security_mode") or "SignAndEncrypt"
            if mode != "None":
                tmp = tempfile.mkdtemp(prefix="opcua-")
                os.chmod(tmp, 0o700)
                cert, key = os.path.join(tmp, "client.pem"), os.path.join(tmp, "client.key")
                open(cert, "w").write(self.secrets["client_certificate"])
                open(key, "w").write(self.secrets["client_private_key"])
                c.set_security_string(f"{self.config.get('security_policy') or 'Basic256Sha256'},{mode},{cert},{key}")
            if (self.config.get("auth") or "anonymous") == "username":
                c.set_user(self.config.get("username"))
                c.set_password(self.secrets.get("password"))
            c.connect()
            try:
                if mode != "None":
                    peer = getattr(getattr(c.aio_obj, "security_policy", None), "peer_certificate", None)
                    got = hashlib.sha256(peer).hexdigest() if peer else ""
                    pin = (self.config.get("server_cert_sha256") or "").replace(":", "").lower()
                    if got != pin:
                        raise PermissionError(f"server certificate mismatch: pinned {pin[:16]}..., server presented {got[:16]}... - do not connect until confirmed")
                yield c
            finally:
                c.disconnect()
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)

    def _variables(self, c) -> list[tuple]:
        from asyncua import ua
        root = c.get_node(self.config.get("root_node") or "i=85")
        depth, cap = int(self.config.get("max_depth") or 4), int(self.config.get("max_nodes") or 500)
        out, frontier = [], [(root, "")]
        for _ in range(max(1, depth)):
            nxt = []
            for node, path in frontier:
                for ch in node.get_children():
                    if ch.nodeid.NamespaceIndex == 0:             # standard OPC UA infrastructure (Server, Aliases, Locations ...)
                        continue
                    name = ch.read_browse_name().Name
                    p = f"{path}/{name}" if path else name
                    nc = ch.read_node_class()
                    if nc == ua.NodeClass.Variable:
                        out.append((ch, p))
                        if len(out) >= cap:
                            return out
                    elif nc == ua.NodeClass.Object:
                        nxt.append((ch, p))
            frontier = nxt
            if not frontier:
                break
        return out

    def _snapshot(self, c, limit: int | None = None) -> list[dict]:
        from asyncua import ua
        vs = self._variables(c)[: limit or None]
        if not vs:
            return []
        dvs = c.read_attributes([n for n, _ in vs], ua.AttributeIds.Value)
        rows = []
        for (n, p), dv in zip(vs, dvs):
            v = dv.Value.Value if dv.Value is not None else None
            rows.append({"node_id": n.nodeid.to_string(), "browse_path": p, "data_type": dv.Value.VariantType.name if dv.Value is not None else None,
                         "value": _val(v), "status": dv.StatusCode.name if dv.StatusCode else "Good",
                         "source_time": _ts(dv.SourceTimestamp), "server_time": _ts(dv.ServerTimestamp)})
        return rows

    def test_connection(self) -> TestResult:
        t0 = time.perf_counter()
        try:
            with self._client() as c:
                n = len(self._variables(c))
        except Exception as e:  # noqa: BLE001
            from .db_connectors import explain
            code, raw, hint = explain(e)
            return TestResult(False, f"Could not connect ({code.lower()}).", hint, {"category": code, "error": raw})
        mode = self.config.get("security_mode") or "SignAndEncrypt"
        return TestResult(True, f"Connected - {n} variable(s) under {self.config.get('root_node') or 'Objects'}.", None,
                          {"latency_ms": round((time.perf_counter() - t0) * 1000), "variables": n, "security": mode, "tls": mode != "None",
                           "session": "browse + read only (no writes, no method calls)",
                           "warnings": ["Unsecured OPC UA session (DEV only)."] if mode == "None" else []})

    def discover_schema(self) -> list[SourceObject]:
        f = [SourceField(n, t, not k, k, False) for n, t, k in FIELDS]
        with self._client() as c:
            n = len(self._variables(c))
        return [SourceObject("snapshot", "current values", f, n), SourceObject("history", "historical values", f, None)]

    def _preview(self, obj: str, limit: int) -> list[dict]:
        if obj not in ("snapshot", "history"):
            raise ValueError("objects are 'snapshot' and 'history'")
        if obj == "history":
            return next(self.extract("history", "source_time", None, limit), [])[:limit]
        with self._client() as c:
            return self._snapshot(c, limit)

    def extract(self, obj, watermark_column=None, since=None, batch_size: int = 5000):
        if obj == "snapshot":
            with self._client() as c:
                rows = self._snapshot(c)
            for i in range(0, len(rows), batch_size):
                yield rows[i:i + batch_size]
            return
        if obj != "history":
            raise ValueError("objects are 'snapshot' and 'history'")
        start = dt.datetime.fromisoformat(str(since)) if since else dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        end = dt.datetime.now(dt.timezone.utc)
        from asyncua import ua
        rows, supported = [], 0
        with self._client() as c:
            for n, p in self._variables(c):
                try:
                    if not n.read_attribute(ua.AttributeIds.Historizing).Value.Value:   # only nodes the server historises
                        continue
                    hist = n.read_raw_history(start, end, numvalues=10000)
                except Exception:  # noqa: BLE001 - node not historised
                    continue
                supported += 1
                for dv in hist:
                    rows.append({"node_id": n.nodeid.to_string(), "browse_path": p,
                                 "data_type": dv.Value.VariantType.name if dv.Value is not None else None,
                                 "value": _val(dv.Value.Value if dv.Value is not None else None), "status": dv.StatusCode.name if dv.StatusCode else "Good",
                                 "source_time": _ts(dv.SourceTimestamp), "server_time": _ts(dv.ServerTimestamp)})
        if not supported:
            raise ValueError("the server provides no history for these nodes - use the 'snapshot' object on a schedule instead")
        rows.sort(key=lambda r: r["source_time"] or "")
        for i in range(0, len(rows), batch_size):
            yield rows[i:i + batch_size]


OPCUA_CONNECTORS = {"opcua": OpcUaConnector}
