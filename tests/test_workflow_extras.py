"""UX2 workflow extras: 8D / CAPA draft, weekly digest, dispute evidence pack."""
import re

import pytest

from app import auth as AU
from app import workflow_extras as WX


def test_capa_draft_uses_the_suppliers_own_figures():
    d = WX.capa_draft("SUP-008")
    assert d["supplier_id"] == "SUP-008" and d["title"].startswith("8D request")
    text = d["description"]
    for section in ("D0", "D2", "D3", "D4-D5", "D8"):
        assert section in text
    assert "Global Pack Solutions" in text and "supplier portal" in text
    assert d["owner_role"] in ("PROCUREMENT", "QUALITY") and d["priority"] in ("CRITICAL", "HIGH", "MEDIUM")
    assert re.search(r"open NCRs: \d+ · overdue CAPAs: \d+\n", text)          # counts print as whole numbers
    with pytest.raises(KeyError):
        WX.capa_draft("SUP-999")


@pytest.mark.parametrize("role", ["PROCUREMENT", "QUALITY"])
def test_digest_per_owner(role):
    d = WX.digest(role)
    assert d["role"] == role and "Weekly supplier digest" in d["subject"]
    assert "Your queue" in d["body"] and "Top risks" in d["body"]


def test_evidence_pack_and_pdf():
    ev = WX.evidence("POC-000001", 1)
    assert ev["po"]["po_id"] == "POC-000001" and "promise_changes" in ev and "exclusions" in ev
    pdf = WX.evidence_pdf(ev, "pytest")
    assert pdf[:4] == b"%PDF" and len(pdf) > 2000
    with pytest.raises(KeyError):
        WX.evidence("NOPE", 1)


def test_endpoints_are_internal_only():
    for path in ("/api/suppliers/SUP-001/capa-draft", "/api/digest/procurement", "/api/po-evidence/POC-000001/1",
                 "/api/po-evidence/POC-000001/1/pdf"):
        assert AU.allowed("GET", path, "PROCUREMENT") and AU.allowed("GET", path, "QUALITY")
        assert not AU.allowed("GET", path, "SUPPLIER") and not AU.allowed("GET", path, None)
