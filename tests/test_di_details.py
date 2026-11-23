"""Data Integration details: the built-in Neon primary warehouse and per-connection details."""
import json
from urllib.parse import urlparse

import pytest

from app.config import settings
from app.integration import health as H


def test_primary_lists_every_table_with_rows_and_never_the_password():
    d = H.primary_details()
    assert d["connector_key"] == "neon" and d["schema"] == "sp" and d["tls"] is True
    tables = {t["table"]: t for t in d["tables"]}
    po = tables["sp.fact_purchase_order_receipt"]
    assert po["rows"] > 0 and "v_po_delivery" in po["used_by"] and po["source"].startswith("CSV:")
    assert d["totals"]["tables"] == len(d["tables"]) and d["totals"]["rows"] >= po["rows"]
    assert all(t["rows"] and (t["used_by"] or t["features"]) for t in d["tables"])          # only used tables with records
    pw = urlparse(settings.database_url).password
    assert pw and pw not in json.dumps(d, default=str)                          # host only, credentials never leave the server


def test_unknown_connection():
    with pytest.raises(LookupError):
        H.connection_details("CONN-9999")
