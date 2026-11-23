"""UX4: supplier digital twin data behind the 3D view."""
import pytest

from app import auth as AU
from app import tracker as T


def test_supplier_twin_has_profile_owners_materials_and_history():
    d = T.supplier_twin("SUP-008")
    assert d["profile"]["supplier_name"] == "Global Pack Solutions" and not any(k.startswith("_") for k in d["profile"])
    assert d["site"]["city"] and d["owners"] and all(o["role"] in ("PROCUREMENT", "QUALITY") for o in d["owners"])
    assert d["materials"] and d["monthly"] and d["recent"]
    assert sum(m["lines"] for m in d["materials"]) >= len(d["recent"])
    with pytest.raises(KeyError):
        T.supplier_twin("SUP-999")


def test_supplier_twin_is_internal_only():
    assert AU.allowed("GET", "/api/tracker/suppliers/SUP-001/twin", "VIEWER")
    assert not AU.allowed("GET", "/api/tracker/suppliers/SUP-001/twin", "SUPPLIER")
