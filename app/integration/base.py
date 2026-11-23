"""Connector interface (DI Phase 1). Adding a source = adding ONE subclass and registering it; the page never changes.

Every connector:
  meta                 key, name, category, description, icon, availability, read_only, config fields (secret fields flagged)
  test_connection()    -> TestResult(ok, message, hint, details)   plain-language errors with a fix hint
  discover_schema()    -> [SourceObject(name, kind, fields[...], row_estimate)]
  preview_data()       -> first <=100 rows, sensitive columns masked BEFORE leaving the connector
  extract()            -> iterator of row batches; incremental when a watermark is given (idempotent upserts downstream)
  health()             -> lightweight liveness / freshness probe
Connectors are READ-ONLY by contract: no method may write to the source.
"""
from __future__ import annotations

import abc
import re
from dataclasses import asdict, dataclass, field
from typing import Iterator

PREVIEW_LIMIT = 100
# Business names + SAP technical names (net price NETPR, net value NETWR, gross BRTWR, condition KBETR, amounts DMBTR / WRBTR,
# standard / moving price STPRS / VERPR, phone TELF*, e-mail SMTP_ADDR, bank BANKN / BANKL, tax numbers STCD* / STCEG).
SENSITIVE_DEFAULT = (r"(price|amount|value|cost|salary|wage|email|phone|mobile|iban|account|bank|tax|pan|aadhaar|passport|birth|address"
                     r"|^netpr$|^netwr$|^brtwr$|^kbetr$|^dmbtr$|^wrbtr$|^stprs$|^verpr$|^telf|^smtp|^bankn$|^bankl$|^stcd|^stceg$)")


@dataclass
class Field:
    name: str
    label: str
    type: str = "text"            # text | number | select | secret | secret_text | boolean | textarea
    required: bool = False
    default: object = None
    help: str = ""
    options: list | None = None
    group: str = ""               # "" = main form; "tls" / "ssh" / "advanced" = collapsible sections in the wizard

    @property
    def secret(self) -> bool:
        return self.type in ("secret", "secret_text")


@dataclass
class ConnectorMeta:
    key: str
    name: str
    category: str
    description: str
    icon: str = "database"                 # lucide icon name used by the gallery
    availability: str = "coming_soon"      # available | planned | coming_soon
    phase: str | None = None               # delivery phase for planned connectors
    read_only: bool = True
    fields: list[Field] = field(default_factory=list)
    objects_label: str = "tables"          # tables | endpoints | files | tags
    licence_note: str | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["fields"] = [{**asdict(f), "secret": f.secret} for f in self.fields]
        return d


@dataclass
class TestResult:
    ok: bool
    message: str
    hint: str | None = None
    details: dict = field(default_factory=dict)


@dataclass
class SourceField:
    name: str
    type: str
    nullable: bool = True
    key: bool = False
    sensitive: bool = False


@dataclass
class SourceObject:
    name: str
    kind: str = "table"
    fields: list[SourceField] = field(default_factory=list)
    row_estimate: int | None = None


class BaseConnector(abc.ABC):
    meta: ConnectorMeta
    environment: str = "DEV"                 # set by registry.instance from the connection
    connection_id: str | None = None

    def __init__(self, config: dict, secrets: dict | None = None, sensitive_pattern: str = SENSITIVE_DEFAULT):
        self.config = config or {}
        self.secrets = secrets or {}
        self._sensitive = re.compile(sensitive_pattern, re.I)

    # ------------------------------------------------------------------ contract
    @abc.abstractmethod
    def test_connection(self) -> TestResult: ...

    @abc.abstractmethod
    def discover_schema(self) -> list[SourceObject]: ...

    @abc.abstractmethod
    def _preview(self, obj: str, limit: int) -> list[dict]: ...

    @abc.abstractmethod
    def extract(self, obj: str, watermark_column: str | None = None, since=None, batch_size: int = 5000) -> Iterator[list[dict]]: ...

    def health(self) -> dict:
        r = self.test_connection()
        return {"ok": r.ok, "message": r.message}

    # ------------------------------------------------------------------ shared behaviour
    def is_sensitive(self, column: str) -> bool:
        return bool(self._sensitive.search(column))

    def mask_rows(self, rows: list[dict], cols: list[str] | None = None, unmask: set | None = None) -> tuple[list[str], list[str]]:
        cols = cols if cols is not None else (list(rows[0].keys()) if rows else [])
        masked = [c for c in cols if self.is_sensitive(c) and c not in (unmask or set())]
        for r in rows:
            for c in masked:
                if r.get(c) is not None:
                    r[c] = "•••"
        return cols, masked

    def preview_data(self, obj: str, limit: int = PREVIEW_LIMIT, unmask: set | None = None) -> dict:
        """First rows with sensitive columns masked (unless explicitly unmasked by an admin)."""
        rows = self._preview(obj, min(int(limit), PREVIEW_LIMIT))
        cols, masked = self.mask_rows(rows, unmask=unmask)
        return {"object": obj, "columns": cols, "masked_columns": masked, "rows": rows, "row_count": len(rows)}

    def run_query(self, sql: str, limit: int = PREVIEW_LIMIT) -> dict:
        """Custom SELECT (database connectors only); see sqlsafe."""
        raise NotImplementedError("custom queries are only supported for database connectors")

    @classmethod
    def policy_errors(cls, config: dict, secrets: dict, environment: str) -> list[str]:
        """Security policy checks at create / edit time (e.g. TLS required outside DEV)."""
        return []

    @classmethod
    def validate_config(cls, config: dict, secrets: dict) -> list[str]:
        errs = []
        for f in cls.meta.fields:
            v = (secrets if f.secret else config).get(f.name)
            if f.required and v in (None, ""):
                errs.append(f"{f.label} is required")
        return errs
