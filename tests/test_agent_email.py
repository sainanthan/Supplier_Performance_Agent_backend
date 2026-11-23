"""Agent Architecture layer 6: email trigger."""
from sqlalchemy import text

from app import agent_email as AE
from app.db import engine


def test_markdown_email_body():
    h = AE.md_to_html("## Title\n**Bold** <x>\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n- one\n1. first")
    assert "<table" in h and "<b>Bold</b>" in h and "&lt;x&gt;" in h and "<ul" in h and "<ol" in h   # escaped, formatted


def test_send_without_smtp_is_honest_and_delivers_in_app(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    r = AE.send("q?", "## Answer\nSUP-001 OTIF 30%", "BOTH", "pytest", {"grounded": True, "checked": 1})
    try:
        assert {x["role"] for x in r["recipients"]} == {"PROCUREMENT", "QUALITY"}
        assert all(x["status"] == "NOT_CONFIGURED" and x["notification_id"] for x in r["recipients"])
        assert "1 figure(s) verified" in r["html"]
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM public.notifications WHERE id = ANY(:i)"), {"i": [x["notification_id"] for x in r["recipients"]]})
