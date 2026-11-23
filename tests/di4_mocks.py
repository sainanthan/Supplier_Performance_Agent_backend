"""Local mock sources for DI Phase 4 tests: SAP OData v2/v4 + REST (HTTP), SFTP (paramiko), OPC UA (asyncua).
All bind to 127.0.0.1 on a free port; nothing leaves the machine."""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
POS = [{"PurchaseOrder": f"45000{i:05d}", "Supplier": f"SUP-{i % 10 + 1:03d}", "date": BASE + dt.timedelta(days=i % 60),
        "changed": BASE + dt.timedelta(days=i % 75, hours=i % 24), "DocumentCurrency": "INR"} for i in range(250)]
ITEMS = [{"PurchaseOrder": p["PurchaseOrder"], "PurchaseOrderItem": "10", "Material": f"MAT-{i % 18:03d}", "Plant": "IN01",
          "OrderQuantity": f"{100 + i}.000", "NetPriceAmount": f"{10 + i % 50}.25"} for i, p in enumerate(POS)]
SUPPLIERS = [{"id": f"S{i:03d}", "name": f"Supplier {i}", "contact": {"email": f"s{i}@example.com", "phone": "+91 1"},
              "updated_at": (BASE + dt.timedelta(days=i)).isoformat()} for i in range(23)]
EVENTS = [{"event_id": i, "line": "SMT-L1", "reason": "NO_MATERIAL" if i % 3 == 0 else "CHANGEOVER", "minutes": 5 + i} for i in range(12)]


def _md(version: str) -> str:
    if version == "v2":
        return """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
 <edmx:DataServices m:DataServiceVersion="2.0"><Schema Namespace="API_PURCHASEORDER_PROCESS_SRV" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
  <EntityType Name="A_PurchaseOrderType"><Key><PropertyRef Name="PurchaseOrder"/></Key>
   <Property Name="PurchaseOrder" Type="Edm.String" Nullable="false"/><Property Name="Supplier" Type="Edm.String"/>
   <Property Name="PurchaseOrderDate" Type="Edm.DateTime"/><Property Name="DocumentCurrency" Type="Edm.String"/>
   <Property Name="LastChangeDateTime" Type="Edm.DateTimeOffset"/></EntityType>
  <EntityType Name="A_PurchaseOrderItemType"><Key><PropertyRef Name="PurchaseOrder"/><PropertyRef Name="PurchaseOrderItem"/></Key>
   <Property Name="PurchaseOrder" Type="Edm.String" Nullable="false"/><Property Name="PurchaseOrderItem" Type="Edm.String" Nullable="false"/>
   <Property Name="Material" Type="Edm.String"/><Property Name="Plant" Type="Edm.String"/><Property Name="OrderQuantity" Type="Edm.Decimal"/>
   <Property Name="NetPriceAmount" Type="Edm.Decimal"/></EntityType>
  <EntityContainer Name="C" m:IsDefaultEntityContainer="true">
   <EntitySet Name="A_PurchaseOrder" EntityType="API_PURCHASEORDER_PROCESS_SRV.A_PurchaseOrderType"/>
   <EntitySet Name="A_PurchaseOrderItem" EntityType="API_PURCHASEORDER_PROCESS_SRV.A_PurchaseOrderItemType"/></EntityContainer>
 </Schema></edmx:DataServices></edmx:Edmx>"""
    return """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>
 <Schema Namespace="po" xmlns="http://docs.oasis-open.org/odata/ns/edm">
  <EntityType Name="PurchaseOrderType"><Key><PropertyRef Name="PurchaseOrder"/></Key>
   <Property Name="PurchaseOrder" Type="Edm.String" Nullable="false"/><Property Name="Supplier" Type="Edm.String"/>
   <Property Name="LastChangeDateTime" Type="Edm.DateTimeOffset"/></EntityType>
  <EntityContainer Name="C"><EntitySet Name="PurchaseOrder" EntityType="po.PurchaseOrderType"/></EntityContainer>
 </Schema></edmx:DataServices></edmx:Edmx>"""


class _H(BaseHTTPRequestHandler):
    calls: dict = {}
    sap_user, sap_pw, api_key, bearer = "sapreader", "s3cret!", "k-123", "tok-xyz"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        b = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):                                       # any write attempt is recorded - connectors must never do this
        _H.calls["writes"] = _H.calls.get("writes", 0) + 1
        self._send(405, {"error": "read-only mock"})

    do_PUT = do_PATCH = do_DELETE = do_POST

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        _H.calls[u.path] = _H.calls.get(u.path, 0) + 1
        host = f"http://{self.headers.get('Host')}"
        if u.path.startswith("/odata/v2/"):
            exp = "Basic " + base64.b64encode(f"{self.sap_user}:{self.sap_pw}".encode()).decode()
            if self.headers.get("Authorization") != exp:
                return self._send(401, {"error": "unauthorised"})
            if u.path.endswith("$metadata"):
                return self._send(200, _md("v2"), "application/xml")
            name = u.path.rsplit("/", 1)[-1]
            rows = POS if name == "A_PurchaseOrder" else ITEMS if name == "A_PurchaseOrderItem" else None
            if rows is None:
                return self._send(404, {"error": "no such set"})
            m = re.search(r"LastChangeDateTime ge datetimeoffset'([^']+)'", q.get("$filter", ""))
            if m:
                since = dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                rows = [r for r in rows if r["changed"] >= since]
            if q.get("$orderby") == "LastChangeDateTime":
                rows = sorted(rows, key=lambda r: r["changed"])
            skip, top = int(q.get("$skip", 0)), int(q.get("$top", 1000))
            out = []
            for r in rows[skip:skip + top]:
                d = {k: v for k, v in r.items() if k not in ("date", "changed")}
                if "date" in r:
                    d["PurchaseOrderDate"] = f"/Date({int(r['date'].timestamp() * 1000)})/"
                    d["LastChangeDateTime"] = f"/Date({int(r['changed'].timestamp() * 1000)}+0000)/"
                d["__metadata"] = {"uri": "x", "type": "t"}
                d["to_Items"] = {"__deferred": {"uri": "x"}}
                out.append(d)
            return self._send(200, {"d": {"results": out}})
        if u.path.startswith("/odata/v4/"):
            if self.headers.get("APIKey") != self.api_key:
                return self._send(401, {"error": "bad key"})
            if u.path.endswith("$metadata"):
                return self._send(200, _md("v4"), "application/xml")
            skip = int(q.get("$skiptoken", 0))
            rows = [{"PurchaseOrder": r["PurchaseOrder"], "Supplier": r["Supplier"], "LastChangeDateTime": r["changed"].isoformat()} for r in POS]
            page = rows[skip:skip + 100]
            body = {"value": page}
            if skip + 100 < len(rows):
                body["@odata.nextLink"] = f"{host}/odata/v4/PurchaseOrder?$skiptoken={skip + 100}"
            return self._send(200, body)
        if u.path.startswith("/api/v1/"):
            if self.headers.get("Authorization") != f"Bearer {self.bearer}":
                return self._send(401, {"error": "unauthorised"})
            ep = u.path.rsplit("/", 1)[-1]
            if ep == "suppliers":
                rows = [s for s in SUPPLIERS if not q.get("updated_since") or s["updated_at"] >= q["updated_since"]]
                p, n = int(q.get("page", 1)), int(q.get("limit", 10))
                return self._send(200, {"data": {"items": rows[(p - 1) * n:p * n]}, "meta": {"page": p}})
            if ep == "events":
                c = int(q.get("cursor", 0))
                nxt = c + 5 if c + 5 < len(EVENTS) else None
                return self._send(200, {"items": EVENTS[c:c + 5], "next_cursor": str(nxt) if nxt else None})
            if ep == "busy":
                _H.calls["busy_n"] = _H.calls.get("busy_n", 0) + 1
                if _H.calls["busy_n"] % 2 == 1:
                    return self._send(429, {"error": "slow down"}, headers={"Retry-After": "1"})
                return self._send(200, {"items": [{"ok": 1}]})
            if ep == "evil":
                return self._send(200, {"items": [{"a": 1}], "next": "http://169.254.169.254/latest/meta-data/"})
            if ep == "redirect":
                return self._send(302, {}, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        return self._send(404, {"error": "not found"})


def start_http() -> tuple[ThreadingHTTPServer, str]:
    _H.calls = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ------------------------------------------------------------------ SFTP (paramiko)
def start_sftp(root: str):
    import paramiko
    host_key = paramiko.RSAKey.generate(2048)
    writes: list = []

    class Srv(paramiko.ServerInterface):
        def check_auth_password(self, u, p):
            return paramiko.AUTH_SUCCESSFUL if (u, p) == ("reader", "pw-1") else paramiko.AUTH_FAILED

        def get_allowed_auths(self, u):
            return "password"

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    class Fs(paramiko.SFTPServerInterface):
        def _real(self, path):
            return os.path.join(root, os.path.normpath(path).replace("\\", "/").lstrip("/"))

        def list_folder(self, path):
            p = self._real(path)
            return [paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(p, f)), f) for f in os.listdir(p)]

        def stat(self, path):
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))

        lstat = stat

        def open(self, path, flags, attr):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
                writes.append(("open-write", path))
                return paramiko.SFTP_PERMISSION_DENIED
            h = paramiko.SFTPHandle(flags)
            h.filename, h.readfile = self._real(path), open(self._real(path), "rb")
            return h

        def remove(self, path):
            writes.append(("remove", path))
            return paramiko.SFTP_PERMISSION_DENIED

        def rename(self, a, b):
            writes.append(("rename", a))
            return paramiko.SFTP_PERMISSION_DENIED

        def mkdir(self, path, attr):
            writes.append(("mkdir", path))
            return paramiko.SFTP_PERMISSION_DENIED

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    stop = threading.Event()

    def serve():
        sock.settimeout(0.5)
        while not stop.is_set():
            try:
                c, _ = sock.accept()
            except (socket.timeout, OSError):
                continue
            t = paramiko.Transport(c)
            t.add_server_key(host_key)
            t.set_subsystem_handler("sftp", paramiko.SFTPServer, Fs)
            try:
                t.start_server(server=Srv())
            except Exception:  # noqa: BLE001
                continue

    threading.Thread(target=serve, daemon=True).start()
    from app.integration.tunnel import fingerprint
    return {"port": sock.getsockname()[1], "fingerprint": fingerprint(host_key), "writes": writes, "stop": lambda: (stop.set(), sock.close())}


# ------------------------------------------------------------------ OPC UA (asyncua)
def start_opcua():
    from asyncua import sync
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = sync.Server()
    url = f"opc.tcp://127.0.0.1:{port}/spa/"
    srv.set_endpoint(url)
    idx = srv.register_namespace("urn:spa:test")
    line = srv.nodes.objects.add_object(idx, "SMT-L1")
    state = line.add_variable(idx, "State", "RUNNING")
    good = line.add_variable(idx, "GoodCount", 1200)
    line.add_variable(idx, "Temperature", 41.5)
    srv.start()
    return {"url": url, "server": srv, "nodes": {"state": state, "good": good}}
