"""Connector catalogue (DI). Working connectors are classes; the rest are catalogue entries marked planned / coming soon.
To add a connector: subclass BaseConnector, give it `meta`, and add it to CONNECTORS. Nothing else changes.
"""
from __future__ import annotations

import datetime as dt
import random
import time
from typing import Iterator

from .base import BaseConnector, ConnectorMeta, Field, SourceField, SourceObject, TestResult

# ------------------------------------------------------------------ shared field sets
DB_FIELDS = [Field("host", "Host", required=True, help="Server name or IP reachable from the backend"),
             Field("port", "Port", "number", True), Field("database", "Database / service name", required=True),
             Field("username", "Username", required=True, help="Use a READ-ONLY account"), Field("password", "Password", "secret", True),
             Field("ssl", "Require TLS", "boolean", default=True), Field("schema", "Schema (optional)")]


class DemoErpConnector(BaseConnector):
    """Mock connector (no external system): an SAP-like purchasing / quality source generated in memory.
    Used for demos and tests of the whole pipeline; always labelled DEMO."""
    meta = ConnectorMeta("demo_mock", "Demo source (mock SAP-like)", "Demo", "In-memory SAP-like purchasing & quality data for demos and tests. No external system.",
                         icon="flask-conical", availability="available", fields=[Field("seed", "Random seed", "number", default=7),
                                                                                 Field("api_token", "Demo token (any value)", "secret", False)])
    TABLES = {
        "EKKO": [("EBELN", "text", True), ("LIFNR", "text", False), ("BEDAT", "date", False), ("WAERS", "text", False)],
        "EKPO": [("EBELN", "text", True), ("EBELP", "integer", True), ("MATNR", "text", False), ("WERKS", "text", False),
                 ("MENGE", "numeric", False), ("NETPR", "numeric", False)],
        "EKET": [("EBELN", "text", True), ("EBELP", "integer", True), ("ETENR", "integer", True), ("EINDT", "date", False), ("MENGE", "numeric", False)],
        "LFA1": [("LIFNR", "text", True), ("NAME1", "text", False), ("LAND1", "text", False), ("TELF1", "text", False)],
    }

    def _rng(self):
        return random.Random(int(self.config.get("seed") or 7))

    def test_connection(self) -> TestResult:
        t = time.perf_counter()
        return TestResult(True, "Connected to the demo source.", None,
                          {"latency_ms": round((time.perf_counter() - t) * 1000, 2), "server": "demo-mock 1.0", "read_only": True, "demo": True})

    def discover_schema(self) -> list[SourceObject]:
        return [SourceObject(n, "table", [SourceField(c, t, not k, k, self.is_sensitive(c)) for c, t, k in cols], 250) for n, cols in self.TABLES.items()]

    def _rows(self, obj: str, n: int) -> list[dict]:
        rng, base = self._rng(), dt.date(2026, 8, 1)
        out = []
        for i in range(n):
            po = f"45000{i // 3:05d}"
            row = {"EKKO": {"EBELN": po, "LIFNR": f"10000{i % 10}", "BEDAT": (base + dt.timedelta(days=i % 90)).isoformat(), "WAERS": "INR"},
                   "EKPO": {"EBELN": po, "EBELP": (i % 3 + 1) * 10, "MATNR": f"MAT-{i % 18:03d}", "WERKS": rng.choice(["IN01", "IN02"]),
                            "MENGE": rng.randint(500, 9000), "NETPR": round(rng.uniform(5, 4200), 2)},
                   "EKET": {"EBELN": po, "EBELP": (i % 3 + 1) * 10, "ETENR": 1, "EINDT": (base + dt.timedelta(days=i % 90 + 20)).isoformat(),
                            "MENGE": rng.randint(500, 9000)},
                   "LFA1": {"LIFNR": f"10000{i}", "NAME1": f"Demo Supplier {i}", "LAND1": rng.choice(["IN", "CN", "KR"]), "TELF1": "+91 44 0000 0000"}}[obj]
            out.append(row)
        return out

    def _preview(self, obj: str, limit: int) -> list[dict]:
        if obj not in self.TABLES:
            raise ValueError(f"Unknown object {obj}")
        return self._rows(obj, limit)

    def extract(self, obj: str, watermark_column=None, since=None, batch_size: int = 5000) -> Iterator[list[dict]]:
        rows = self._rows(obj, 250)
        if watermark_column and since is not None:
            rows = [r for r in rows if str(r.get(watermark_column)) >= str(since)]
        for i in range(0, len(rows), batch_size):
            yield rows[i:i + batch_size]


# ------------------------------------------------------------------ catalogue entries without a class yet (planned / coming soon)
def _m(key, name, cat, desc, icon, availability="coming_soon", phase=None, fields=None, objects="tables", licence=None):
    return ConnectorMeta(key, name, cat, desc, icon, availability, phase, True, fields or [], objects, licence)


CATALOGUE: list[ConnectorMeta] = [
    # Databases
    _m("oracle", "Oracle Database", "Databases", "Oracle 12c+ via python-oracledb thin mode (no Oracle client needed).", "database", "planned", "2", DB_FIELDS, licence="python-oracledb: UPL / Apache 2.0"),
    _m("postgresql", "PostgreSQL", "Databases", "PostgreSQL 11+ (incl. managed: Azure, AWS RDS, Neon).", "database", "planned", "2", DB_FIELDS, licence="psycopg: LGPL"),
    _m("sqlserver", "Microsoft SQL Server", "Databases", "SQL Server 2016+ / Azure SQL via ODBC Driver 18.", "database", "planned", "2", DB_FIELDS, licence="Microsoft ODBC Driver 18: free"),
    _m("mysql", "MySQL / MariaDB", "Databases", "MySQL 5.7+ / MariaDB 10+.", "database"),
    _m("hana", "SAP HANA", "Databases", "HANA SQL via hdbcli.", "database", licence="hdbcli: SAP licence (free download)"),
    _m("db2", "IBM Db2", "Databases", "Db2 LUW / z/OS via ibm_db.", "database"),
    # ERP
    _m("sap_odata", "SAP S/4HANA (OData)", "ERP", "Purchase orders, schedule lines, GRNs, suppliers via standard S/4 OData APIs.", "building-2", "planned", "4",
       [Field("base_url", "OData base URL", required=True, help="https://<host>/sap/opu/odata/sap"), Field("client", "SAP client", default="100"),
        Field("username", "Username", required=True, help="Read-only communication user"), Field("password", "Password", "secret", True)], "endpoints",
       "Check SAP digital-access / indirect-use licensing with your SAP account team"),
    _m("sap_tables", "SAP ECC / S4 tables (EKKO/EKPO/EKET/EKBE/MSEG/LFA1)", "ERP",
       "Direct RFC table extraction. Available today instead: SAP S/4HANA (OData), or a read-only database replica of the SAP tables via the "
       "Oracle / SQL Server / PostgreSQL connectors (HANA connector coming soon).", "building-2",
       licence="RFC needs the SAP NW RFC SDK (S-user download, SAP licence)"),
    _m("oracle_ebs", "Oracle E-Business Suite / Fusion", "ERP", "PO, receiving and supplier data (REST / DB).", "building-2"),
    _m("dynamics365", "Microsoft Dynamics 365", "ERP", "Supply Chain Management OData entities.", "building-2"),
    _m("infor", "Infor", "ERP", "Infor CloudSuite / LN via ION API.", "building-2"),
    _m("odoo", "Odoo", "ERP", "Purchase, inventory and quality via JSON-RPC.", "building-2"),
    # MES
    _m("mes_db_rest", "MES (generic DB / REST)", "MES / shop floor", "Any MES through its database or REST API (read-only).", "factory", "planned", "4"),
    _m("opcua", "OPC UA", "MES / shop floor", "Read-only subscription to OPC UA nodes (asyncua).", "cpu", "planned", "4", objects="tags"),
    _m("opcenter", "Siemens Opcenter", "MES / shop floor", "Opcenter Execution via REST / DB.", "factory"),
    _m("factorytalk", "Rockwell FactoryTalk", "MES / shop floor", "FactoryTalk ProductionCentre / Historian.", "factory"),
    _m("aveva_mes", "AVEVA MES", "MES / shop floor", "AVEVA MES database views.", "factory"),
    _m("plex", "Plex", "MES / shop floor", "Plex Smart Manufacturing APIs.", "factory"),
    _m("tulip", "Tulip", "MES / shop floor", "Tulip Tables API.", "factory"),
    _m("ignition", "Ignition", "MES / shop floor", "Ignition gateway REST / database.", "factory"),
    # QMS
    _m("sap_qm", "SAP QM (QALS / QAVE)", "QMS", "Inspection lots and usage decisions.", "shield-check"),
    _m("etq", "ETQ Reliance", "QMS", "NCR / CAPA records via REST.", "shield-check"),
    _m("mastercontrol", "MasterControl", "QMS", "Quality events via API.", "shield-check"),
    _m("qualio", "Qualio", "QMS", "Events and CAPA via API.", "shield-check"),
    # Logistics
    _m("wms_db", "WMS database", "WMS / TMS / logistics", "Stock and receipts from a WMS database.", "warehouse"),
    _m("project44", "project44", "WMS / TMS / logistics", "Shipment visibility milestones and ETAs.", "truck", licence="Commercial contract + API key"),
    _m("fourkites", "FourKites", "WMS / TMS / logistics", "Shipment visibility milestones and ETAs.", "truck", licence="Commercial contract + API key"),
    _m("ais", "AIS ship tracking", "WMS / TMS / logistics", "Vessel positions (e.g. aisstream.io).", "ship", licence="API key"),
    _m("telematics", "GPS / telematics", "WMS / TMS / logistics", "Already live: POST /api/gps/pings with a device key.", "map-pin", "available"),
    _m("asn_feed", "ASN / milestone feed", "WMS / TMS / logistics", "Carrier and 3PL milestone files or APIs.", "truck"),
    # IoT
    _m("mqtt", "MQTT", "IoT / OT (read-only)", "Subscribe to topics (paho-mqtt), read-only.", "radio"),
    _m("modbus", "Modbus gateway", "IoT / OT (read-only)", "Via an edge gateway, read-only.", "cpu"),
    _m("pi", "OSIsoft PI / AVEVA PI", "IoT / OT (read-only)", "PI Web API, read-only.", "activity"),
    # EDI / portal
    _m("edi", "EDI 850 / 856 / 810 (AS2 / SFTP)", "Supplier portal & EDI", "POs, ASNs and invoices from EDI exchanges.", "file-text"),
    _m("portal_api", "Supplier portal API", "Supplier portal & EDI", "Already live: built-in supplier portal (NCR responses, promise changes).", "users", "available"),
    # Files
    _m("csv_excel", "CSV / Excel upload", "Files & storage", "Upload or drop CSV / XLSX files.", "file-spreadsheet", "planned", "4", objects="files"),
    _m("sftp", "SFTP / FTP", "Files & storage", "Pick up files from an SFTP server.", "folder-sync", "planned", "4", objects="files"),
    _m("azure_blob", "Azure Blob Storage", "Files & storage", "Files from a blob container.", "cloud"),
    _m("s3", "AWS S3", "Files & storage", "Files from an S3 bucket.", "cloud"),
    _m("sharepoint", "SharePoint / OneDrive", "Files & storage", "Files from a SharePoint library.", "cloud"),
    _m("network_share", "Network folder", "Files & storage", "Shared network folder (via the on-prem agent).", "folder"),
    # Generic
    _m("rest", "REST API", "Generic", "Any JSON REST API with paging and auth.", "globe", "planned", "4", objects="endpoints"),
    _m("soap", "SOAP", "Generic", "SOAP / WSDL services.", "globe"),
    _m("graphql", "GraphQL", "Generic", "GraphQL queries.", "globe"),
    _m("webhook", "Webhook (inbound)", "Generic", "Receive pushed JSON.", "webhook"),
    _m("odbc", "JDBC / ODBC", "Generic", "Any ODBC data source (via the on-prem agent).", "database"),
    _m("email", "Email attachments", "Generic", "Ingest attachments from a mailbox.", "mail"),
    # Platforms
    _m("snowflake", "Snowflake", "Data platforms", "Read tables / views.", "snowflake"),
    _m("bigquery", "BigQuery", "Data platforms", "Read tables / views.", "database"),
    _m("databricks", "Databricks", "Data platforms", "SQL warehouses.", "database"),
    _m("synapse", "Azure Synapse", "Data platforms", "Dedicated / serverless SQL.", "database"),
]

from .db_connectors import DB_CONNECTORS  # noqa: E402  (Phase 2: PostgreSQL, Oracle, SQL Server)

from .api_connectors import API_CONNECTORS  # noqa: E402  (Phase 4: SAP OData, REST, MES REST)
from .file_connectors import FILE_CONNECTORS  # noqa: E402  (Phase 4: CSV / Excel upload, SFTP)
from .opcua_connector import OPCUA_CONNECTORS  # noqa: E402  (Phase 4: OPC UA, read-only)

CONNECTORS: dict[str, type[BaseConnector]] = {"demo_mock": DemoErpConnector, **DB_CONNECTORS, **API_CONNECTORS, **FILE_CONNECTORS, **OPCUA_CONNECTORS}


FLAG_FOR = {**{k: "di_database_connectors" for k in DB_CONNECTORS},
            **{k: "di_files_apis" for k in (*API_CONNECTORS, *FILE_CONNECTORS, *OPCUA_CONNECTORS)}}


def enabled(key: str) -> bool:
    """Per-feature flag for connector families (the whole page is behind `data_integration`)."""
    f = FLAG_FOR.get(key)
    if not f:
        return True
    from ..features import is_on
    return is_on(f)


def catalogue() -> list[dict]:
    items = [c.meta.as_dict() for c in CONNECTORS.values()]
    have = {i["key"] for i in items}
    items += [m.as_dict() for m in CATALOGUE if m.key not in have]
    return items


def meta_for(key: str) -> ConnectorMeta | None:
    if key in CONNECTORS:
        return CONNECTORS[key].meta
    return next((m for m in CATALOGUE if m.key == key), None)


def instance(key: str, config: dict, secrets: dict, environment: str = "DEV", connection_id: str | None = None) -> BaseConnector:
    if key not in CONNECTORS:
        raise NotImplementedError("This connector is not available yet")
    con = CONNECTORS[key](config, secrets)
    con.environment, con.connection_id = environment, connection_id       # SSRF / TLS rules depend on the environment
    return con
