"""API connectors: SAP S/4HANA OData (v2 + v4), generic REST, MES (REST) - DI Phase 4. All read-only (GET only), see http_base.

SAP OData
- discover: $metadata (parsed with defusedxml - no XML entity / XXE attacks) -> entity sets, properties, keys, Edm types
- preview: $top=100 ; extract: server paging (__next / @odata.nextLink, same host only) or $top/$skip, ordered by the watermark
- incremental: $filter=<watermark> ge <typed literal> (Edm.DateTime -> datetime'..', Edm.DateTimeOffset -> datetimeoffset'..' in v2;
  plain ISO in v4). v2 /Date(ms)/ values become ISO dates. sap-client is sent when configured.
- Works with SAP API Business Hub sandbox (API key header 'APIKey') and on-prem S/4 (basic / OAuth2, read-only communication user).
REST
- endpoints are configured as JSON: path, where the records are, paging style (none / page / offset / cursor / next_link),
  optional server-side watermark parameter. Nested objects are flattened one level (a.b) - deeper values kept as JSON text.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from typing import Iterator

from defusedxml import ElementTree as ET

from .base import BaseConnector, ConnectorMeta, Field, SourceField, SourceObject, TestResult
from .http_base import HttpSource, auth_fields, http_error, http_policy_errors, status_hint

_DATE_RE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def _v2_value(v):
    if isinstance(v, str):
        m = _DATE_RE.fullmatch(v)
        if m:
            return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, dt.timezone.utc).isoformat()
    return v


def _clean_record(r: dict) -> dict:
    out = {}
    for k, v in r.items():
        if k.startswith("__") or k.startswith("@odata"):
            continue
        if isinstance(v, dict):
            if "__deferred" in v or "results" in v:          # navigation properties are not followed
                continue
            for k2, v2 in v.items():
                if not k2.startswith("__"):
                    out[f"{k}.{k2}"] = json.dumps(v2) if isinstance(v2, (dict, list)) else _v2_value(v2)
            continue
        out[k] = json.dumps(v) if isinstance(v, list) else _v2_value(v)
    return out


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


class _HttpConnector(BaseConnector):
    url_field = "base_url"
    environment = "DEV"

    @classmethod
    def policy_errors(cls, config, secrets, environment):
        return http_policy_errors(cls.url_field, config, secrets, environment)

    def http(self) -> HttpSource:
        return HttpSource(self.config.get(self.url_field) or "", self.config, self.secrets, getattr(self, "environment", "DEV"))

    @property
    def page_size(self) -> int:
        try:
            return max(1, min(int(self.config.get("page_size") or 1000), 5000))
        except (TypeError, ValueError):
            return 1000


# ------------------------------------------------------------------ SAP OData
class SapODataConnector(_HttpConnector):
    url_field = "service_url"
    meta = ConnectorMeta(
        "sap_odata", "SAP S/4HANA (OData)", "ERP",
        "Purchase orders, schedule lines, goods receipts and suppliers via standard S/4 OData APIs (v2 / v4). Read-only (GET).",
        "building-2", "available", "4", True,
        [Field("service_url", "OData service URL", required=True,
               help="e.g. https://<host>/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV  or the API Business Hub sandbox URL"),
         Field("odata_version", "OData version", "select", default="auto", options=["auto", "v2", "v4"]),
         Field("sap_client", "SAP client (sap-client)", help="Optional, e.g. 100"),
         *auth_fields()], "entity sets",
        "Check SAP digital-access / indirect-use licensing with your SAP account team")

    _meta_cache: dict | None = None

    def _metadata(self, h: HttpSource) -> dict:
        if self._meta_cache is not None:
            return self._meta_cache
        r = h.get("$metadata", self._params(), accept="application/xml")
        if r.status_code != 200:
            raise PermissionError(f"$metadata: {status_hint(r.status_code)}")
        root = ET.fromstring(r.content)
        version = "v4" if root.get("Version", "").startswith("4") else "v2"
        if self.config.get("odata_version") in ("v2", "v4"):
            version = self.config["odata_version"]
        types, sets = {}, {}
        for el in root.iter():
            tag = _local(el.tag)
            if tag == "Schema":
                ns = el.get("Namespace")
                for et in el:
                    if _local(et.tag) != "EntityType":
                        continue
                    keys = [pr.get("Name") for k in et if _local(k.tag) == "Key" for pr in k]
                    props = [(p.get("Name"), p.get("Type"), p.get("Nullable", "true") != "false") for p in et if _local(p.tag) == "Property"]
                    types[f"{ns}.{et.get('Name')}"] = (keys, props)
            elif tag == "EntitySet":
                sets[el.get("Name")] = el.get("EntityType")
        out = {"version": version, "sets": {}}
        for name, t in sets.items():
            keys, props = types.get(t, ([], []))
            out["sets"][name] = {"keys": keys, "props": props}
        self._meta_cache = out
        return out

    def _params(self, extra: dict | None = None) -> dict:
        p = {"sap-client": self.config["sap_client"]} if self.config.get("sap_client") else {}
        return {**p, **(extra or {})}

    def test_connection(self) -> TestResult:
        t0 = time.perf_counter()
        h = self.http()
        try:
            md = self._metadata(h)
        except Exception as e:  # noqa: BLE001
            return http_error(e)
        finally:
            h.close()
        return TestResult(True, f"Connected - OData {md['version']}, {len(md['sets'])} entity set(s).",
                          "Write privileges cannot be detected over OData: use a communication user with display (read) authorisations only. "
                          "The connector only sends GET requests.",
                          {"latency_ms": round((time.perf_counter() - t0) * 1000), "odata_version": md["version"], "entity_sets": len(md["sets"]),
                           "tls": self.config.get(self.url_field, "").startswith("https"), "session": "GET only (read-only)",
                           "warnings": [] if self.config.get(self.url_field, "").startswith("https") else ["Not encrypted (http) - DEV only."]})

    def discover_schema(self) -> list[SourceObject]:
        h = self.http()
        try:
            md = self._metadata(h)
        finally:
            h.close()
        return [SourceObject(n, "entity set", [SourceField(p, t.replace("Edm.", ""), nul, p in s["keys"], self.is_sensitive(p)) for p, t, nul in s["props"]])
                for n, s in sorted(md["sets"].items())]

    def _literal(self, md: dict, obj: str, col: str, since) -> str:
        typ = next((t for p, t, _ in md["sets"][obj]["props"] if p == col), "Edm.String")
        s = str(since)
        if md["version"] == "v4":
            return s if typ in ("Edm.DateTimeOffset", "Edm.Date", "Edm.Decimal", "Edm.Int32", "Edm.Int64") else "'" + s.replace("'", "''") + "'"
        if typ == "Edm.DateTime":
            return "datetime'" + s[:19] + "'"
        if typ == "Edm.DateTimeOffset":
            return "datetimeoffset'" + (s if ("+" in s[10:] or s.endswith("Z")) else s[:19] + "Z") + "'"
        if typ in ("Edm.Decimal", "Edm.Int32", "Edm.Int64", "Edm.Double"):
            return s
        return "'" + s.replace("'", "''") + "'"

    def _pages(self, obj: str, watermark_column=None, since=None, limit: int | None = None) -> Iterator[list[dict]]:
        h = self.http()
        try:
            md = self._metadata(h)
            if obj not in md["sets"]:
                raise ValueError(f"entity set {obj} does not exist in this service")
            names = {p for p, _, _ in md["sets"][obj]["props"]}
            if watermark_column and watermark_column not in names:
                raise ValueError(f"watermark {watermark_column} is not a property of {obj}")
            size = min(limit or self.page_size, self.page_size)
            q = {"$top": size, "$format": "json"}
            if watermark_column:
                q["$orderby"] = watermark_column
                if since is not None:
                    q["$filter"] = f"{watermark_column} ge {self._literal(md, obj, watermark_column, since)}"
            if md["version"] == "v2":
                q["$inlinecount"] = "none"
            url, params, skip, got = obj, self._params(q), 0, 0
            while True:
                r = h.get(url, params)
                if r.status_code != 200:
                    raise PermissionError(f"{obj}: {status_hint(r.status_code)}")
                j = r.json()
                body = j.get("d", j)
                rows = body.get("results", body) if isinstance(body, dict) else body
                if isinstance(rows, dict):
                    rows = rows.get("value", [])
                rows = [_clean_record(x) for x in (rows or [])]
                if rows:
                    yield rows
                got += len(rows)
                nxt = (body.get("__next") if isinstance(body, dict) else None) or j.get("@odata.nextLink")
                if (limit and got >= limit) or not rows:
                    return
                if nxt:
                    url, params = nxt, None                     # server-driven paging (same host enforced)
                elif len(rows) < size:
                    return
                else:
                    skip += len(rows)
                    url, params = obj, self._params({**q, "$skip": skip})
        finally:
            h.close()

    def _preview(self, obj: str, limit: int) -> list[dict]:
        rows = []
        for b in self._pages(obj, limit=limit):                         # keep paging when the page size is below the preview size
            rows += b
            if len(rows) >= limit:
                break
        return rows[:limit]

    def extract(self, obj, watermark_column=None, since=None, batch_size: int = 5000):
        yield from self._pages(obj, watermark_column, since)


# ------------------------------------------------------------------ generic REST
ENDPOINTS_HELP = ('JSON list, e.g. [{"name": "suppliers", "path": "/v1/suppliers", "records": "data.items", "pagination": "page", '
                  '"page_param": "page", "size_param": "limit", "watermark_param": "updated_since"}]. pagination: none | page | offset | cursor | next_link')


def _dig(j, path: str | None):
    if not path:
        return j
    for part in path.split("."):
        if isinstance(j, dict):
            j = j.get(part)
        else:
            return None
    return j


def _flatten(r: dict) -> dict:
    out = {}
    for k, v in r.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                out[f"{k}.{k2}"] = json.dumps(v2) if isinstance(v2, (dict, list)) else v2
        elif isinstance(v, list):
            out[k] = json.dumps(v)
        else:
            out[k] = v
    return out


class RestConnector(_HttpConnector):
    meta = ConnectorMeta("rest", "REST API", "Generic", "Any JSON REST API with paging and authentication. Read-only (GET).", "globe", "available", "4", True,
                         [Field("base_url", "Base URL", required=True, help="https://api.example.com"),
                          Field("endpoints", "Endpoints (JSON)", "textarea", True, help=ENDPOINTS_HELP), *auth_fields()], "endpoints")

    @classmethod
    def policy_errors(cls, config, secrets, environment):
        errs = super().policy_errors(config, secrets, environment)
        try:
            eps = cls._parse_endpoints(config.get("endpoints"))
            if not eps:
                errs.append("configure at least one endpoint")
        except ValueError as e:
            errs.append(str(e))
        return errs

    @staticmethod
    def _parse_endpoints(raw) -> list[dict]:
        try:
            eps = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except json.JSONDecodeError as e:
            raise ValueError(f"endpoints: not valid JSON ({e.msg})") from None
        if not isinstance(eps, list):
            raise ValueError("endpoints must be a JSON list")
        out = []
        for e in eps:
            if not isinstance(e, dict) or not e.get("name") or not e.get("path"):
                raise ValueError("each endpoint needs a name and a path")
            if str(e["path"]).startswith(("http://", "https://", "//")):
                raise ValueError("endpoint paths must be relative to the base URL")
            if e.get("pagination", "none") not in ("none", "page", "offset", "cursor", "next_link"):
                raise ValueError(f"{e['name']}: unknown pagination '{e.get('pagination')}'")
            out.append(e)
        return out

    def endpoints(self) -> dict:
        return {e["name"]: e for e in self._parse_endpoints(self.config.get("endpoints"))}

    def _pages(self, obj: str, watermark_column=None, since=None, limit: int | None = None) -> Iterator[list[dict]]:
        ep = self.endpoints().get(obj)
        if not ep:
            raise ValueError(f"endpoint {obj} is not configured")
        h = self.http()
        try:
            mode, size = ep.get("pagination", "none"), int(ep.get("page_size") or min(self.page_size, limit or self.page_size))
            params = dict(ep.get("params") or {})
            if since is not None and ep.get("watermark_param"):
                params[ep["watermark_param"]] = str(since)
            page, offset, cursor, url, got = int(ep.get("first_page", 1)), 0, None, ep["path"], 0
            while True:
                p = dict(params)
                if mode == "page":
                    p[ep.get("page_param", "page")], p[ep.get("size_param", "limit")] = page, size
                elif mode == "offset":
                    p[ep.get("offset_param", "offset")], p[ep.get("size_param", "limit")] = offset, size
                elif mode == "cursor" and cursor:
                    p[ep.get("cursor_param", "cursor")] = cursor
                r = h.get(url, p if url == ep["path"] else None)
                if r.status_code != 200:
                    raise PermissionError(f"{obj}: {status_hint(r.status_code)}")
                j = r.json()
                rows = _dig(j, ep.get("records"))
                if not isinstance(rows, list):
                    raise ValueError(f"{obj}: no list found at '{ep.get('records') or '(root)'}' in the response")
                rows = [_flatten(x) for x in rows if isinstance(x, dict)]
                if watermark_column and since is not None:            # client-side safety net (server filter may be coarse)
                    rows = [x for x in rows if x.get(watermark_column) is None or str(x[watermark_column]) >= str(since)]
                if rows:
                    yield rows
                got += len(rows)
                raw_n = len(_dig(j, ep.get("records")) or [])
                if mode == "none" or raw_n == 0 or (limit and got >= limit):
                    return
                if mode == "page":
                    if raw_n < size:
                        return
                    page += 1
                elif mode == "offset":
                    if raw_n < size:
                        return
                    offset += raw_n
                elif mode == "cursor":
                    cursor = _dig(j, ep.get("cursor_field", "next_cursor"))
                    if not cursor:
                        return
                elif mode == "next_link":
                    nxt = _dig(j, ep.get("next_field", "next"))
                    if not nxt:
                        return
                    url = nxt                                          # same-host check in HttpSource.url
        finally:
            h.close()

    def test_connection(self) -> TestResult:
        t0 = time.perf_counter()
        try:
            eps = self.endpoints()
            first = next(iter(eps))
            rows = next(self._pages(first, limit=1), [])
        except Exception as e:  # noqa: BLE001
            return http_error(e)
        return TestResult(True, f"Connected - {len(eps)} endpoint(s); '{first}' answered.", "The connector only sends GET requests (read-only).",
                          {"latency_ms": round((time.perf_counter() - t0) * 1000), "endpoints": len(eps), "sample_fields": len(rows[0]) if rows else 0,
                           "tls": str(self.config.get("base_url", "")).startswith("https"), "session": "GET only (read-only)",
                           "warnings": [] if str(self.config.get("base_url", "")).startswith("https") else ["Not encrypted (http) - DEV only."]})

    def discover_schema(self) -> list[SourceObject]:
        out = []
        for name in self.endpoints():
            rows = next(self._pages(name, limit=50), [])
            cols: dict[str, str] = {}
            for r in rows:
                for k, v in r.items():
                    cols.setdefault(k, type(v).__name__ if v is not None else "text")
            out.append(SourceObject(name, "endpoint", [SourceField(c, t, True, c.lower() in ("id", "key"), self.is_sensitive(c)) for c, t in cols.items()]))
        return out

    def _preview(self, obj: str, limit: int) -> list[dict]:
        rows = []
        for b in self._pages(obj, limit=limit):
            rows += b
            if len(rows) >= limit:
                break
        return rows[:limit]

    def extract(self, obj, watermark_column=None, since=None, batch_size: int = 5000):
        yield from self._pages(obj, watermark_column, since)


class MesRestConnector(RestConnector):
    meta = ConnectorMeta("mes_db_rest", "MES (REST API)", "MES / shop floor",
                         "Any MES through its REST API (work orders, downtime, inspections). Read-only. For an MES database use the "
                         "PostgreSQL / SQL Server / Oracle connectors.", "factory", "available", "4", True,
                         [Field("base_url", "MES API base URL", required=True),
                          Field("endpoints", "Endpoints (JSON)", "textarea", True, help=ENDPOINTS_HELP), *auth_fields()], "endpoints")


API_CONNECTORS = {c.meta.key: c for c in (SapODataConnector, RestConnector, MesRestConnector)}
