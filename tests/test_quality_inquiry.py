"""UX3 supplier quality inquiry: draft -> send (e-mail + portal) -> supplier reply (link / portal) -> close."""
import pytest
from sqlalchemy import text

from app import auth as AU
from app import quality_inquiry as QI
from app.db import engine


@pytest.fixture
def inquiry():
    d = QI.draft("SUP-003", None, "pytest")
    yield d
    with engine.begin() as c:                                   # leave nothing behind (incl. the portal request)
        c.execute(text("DELETE FROM public.notifications WHERE body LIKE :b OR subject LIKE :s"),
                  {"b": f"%(#{d['id']})%", "s": "Supplier replied:%" + d["subject"][:40] + "%"})
        c.execute(text("DELETE FROM public.quality_inquiries WHERE id = :i"), {"i": d["id"]})


def test_draft_carries_the_defect_evidence(inquiry):
    d = inquiry
    assert d["status"] == "DRAFT" and d["supplier_id"] == "SUP-003"
    assert "reason / root cause" in d["message"] and "containment" in d["message"]
    assert d["due_date"] is not None
    with pytest.raises(KeyError):
        QI.draft("SUP-999")


def test_full_cycle_without_smtp(inquiry, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    sent = QI.send(inquiry["id"], "quality@example.com", "pytest")
    assert sent["status"] == "SENT" and sent["email_status"] == "NOT_CONFIGURED"   # honest: not e-mailed without SMTP
    assert "/api/quality-reply/" in sent["reply_link"]
    with pytest.raises(ValueError):
        QI.send(inquiry["id"], None, "pytest")                                       # cannot send twice
    token = sent["reply_link"].rsplit("/", 1)[1]
    assert QI.read_token(token) == inquiry["id"]
    assert QI.read_token(token[:-2] + "xx") is None                                   # tampered link refused
    r = QI.respond(inquiry["id"], "Solder paste batch out of spec", "Lots blocked", "Supplier changed paste lot control", None,
                   "EMAIL_LINK", "SUP-003 (e-mail link)")
    assert r["status"] == "RESPONDED" and r["responded_via"] == "EMAIL_LINK"
    c = QI.close(inquiry["id"], "pytest", "Accepted")
    assert c["status"] == "CLOSED"
    with pytest.raises(ValueError):
        QI.respond(inquiry["id"], "late answer", "", "", None, "PORTAL", "x")         # closed: no more replies


def test_access_rules():
    assert AU.allowed("GET", "/api/quality-reply/abc.def", None)                       # e-mail link works without login
    assert AU.allowed("POST", "/api/quality-reply/abc.def", None)
    for role in ("ADMIN", "QUALITY"):
        assert AU.allowed("POST", "/api/quality-inquiries/1/send", role)
    for role in ("PROCUREMENT", "VIEWER", "SUPPLIER"):
        assert not AU.allowed("POST", "/api/quality-inquiries/1/send", role)
    assert AU.allowed("POST", "/api/portal/quality-inquiries/1/respond", "SUPPLIER")
    assert not AU.allowed("GET", "/api/quality-inquiries", "SUPPLIER")
