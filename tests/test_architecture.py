"""UX1: Agent Architecture catalogue."""
from app import architecture as ARCH
from app import auth as AU
from app.tools import TOOLS

PAGES = {"command", "trends", "risk", "tracker", "review", "settings", None}   # pages that still exist


def test_every_agent_tool_is_catalogued():
    names = {t.name for t in TOOLS}
    assert names == set(ARCH.TOOLS), names ^ set(ARCH.TOOLS)          # a new tool must say what it reads and where it shows
    for name, t in ARCH.TOOLS.items():
        assert t["agent"] in ARCH.AGENTS, name
        assert t["datasets"] and t["backend"] and t["label"], name
        assert t["page"] in PAGES, name
    assert [p["id"] for p in ARCH.PIPELINE] == ["receive", "plan", "tools", "ground", "answer"]


def test_summarize_tool_output():
    assert ARCH.summarize('[{"a":1},{"a":2}]') == "2 rows"
    assert ARCH.summarize('{"count":3,"alerts":[1,2,3]}') == "3 alerts"
    assert ARCH.summarize("ERROR: boom").startswith("error")
    assert ARCH.summarize("No rows.") == "no rows"
    assert ARCH.summarize('{"count":31,"alerts":[{"a":1}..."(truncated, 9000 chars total)"').startswith("31 records")


def test_new_endpoints_are_internal_only():
    for path in ("/api/architecture",):
        for role in ("ADMIN", "PROCUREMENT", "QUALITY", "VIEWER"):
            assert AU.allowed("GET", path, role), (path, role)
        assert not AU.allowed("GET", path, "SUPPLIER") and not AU.allowed("GET", path, None)
