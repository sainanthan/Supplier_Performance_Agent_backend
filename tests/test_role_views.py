"""Role-based views: each role gets its own workspace (frontend/src/roles.js) and the backend enforces the admin-only parts."""
import json
import re
from pathlib import Path

from app import auth as AU

ROLES_JS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "roles.js"


def _views() -> dict:
    """Parse the role table from roles.js (keys + pages/settings/landing) so UI and backend rules cannot drift apart."""
    src = ROLES_JS.read_text(encoding="utf-8")
    out = {}
    for role, body in re.findall(r"\n  (ADMIN|PROCUREMENT|QUALITY|VIEWER): \{(.*?)\n  \},", src, re.S):
        g = lambda k: json.loads(re.search(rf"{k}: (\[[^\]]*\])", body).group(1).replace('"', '"'))  # noqa: E731
        out[role] = {"pages": g("pages"), "settings": g("settings"), "landing": re.search(r'landing: "(\w+)"', body).group(1)}
    return out


def test_each_role_has_its_own_workspace():
    v = _views()
    assert set(v) == {"ADMIN", "PROCUREMENT", "QUALITY", "VIEWER"}
    assert "integration" in v["ADMIN"]["pages"] and all("integration" not in v[r]["pages"] for r in ("PROCUREMENT", "QUALITY", "VIEWER"))
    assert "tracker" not in v["QUALITY"]["pages"]
    assert all(v[r]["landing"] == "home" for r in v)                                   # Home: the presentation command page
    assert all(v[r]["pages"][:2] == ["home", "command"] for r in v)                     # Home first, then Overview
    removed = {"story", "scorecards", "compare", "actions"}                              # merged into Overview or retired
    assert all("architecture" in v[r]["pages"] and not removed & set(v[r]["pages"]) for r in v)
    admin_only = {"flags", "gov", "jobs", "sys", "users"}
    assert admin_only <= set(v["ADMIN"]["settings"])
    for r in ("PROCUREMENT", "QUALITY", "VIEWER"):
        assert not admin_only & set(v[r]["settings"]), r                                     # no admin settings for other roles
        assert v[r]["landing"] in v[r]["pages"]
    assert len({(tuple(v[r]["pages"]), tuple(v[r]["settings"])) for r in v}) == 4          # four genuinely different workspaces


def test_backend_enforces_admin_only_governance():
    for path in ("/api/runs", "/api/runs/abc/snapshot", "/api/dq", "/api/jobs", "/api/recon", "/api/ai-usage", "/api/users", "/api/data-readiness"):
        assert AU.allowed("GET", path, "ADMIN"), path
        for role in ("PROCUREMENT", "QUALITY", "VIEWER", "SUPPLIER"):
            assert not AU.allowed("GET", path, role), (path, role)
    for path in ("/api/config/scorecard", "/api/exclusions", "/api/features"):                # read-outs every internal role needs
        for role in ("PROCUREMENT", "QUALITY", "VIEWER"):
            assert AU.allowed("GET", path, role), (path, role)
    assert not AU.allowed("PUT", "/api/features/x", "PROCUREMENT") and not AU.allowed("PUT", "/api/config/scorecard", "QUALITY")
    assert AU.allowed("GET", "/api/compare", "PROCUREMENT") and not AU.allowed("GET", "/api/compare", "QUALITY")


def test_notifications_are_role_scoped():
    """Each role sees only its own inbox; 'mark all read' cannot touch other roles' notifications. ADMIN sees everything."""
    import pytest
    try:
        from app import analytics as A
        A._run("SELECT 1", {}, True)
    except Exception:
        pytest.skip("database not reachable")
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from app.db import engine
    from app.main import app
    cl = TestClient(app)
    H = lambda u, r: {"Authorization": "Bearer " + AU.issue_token(AU.UserCtx(u, r))}  # noqa: E731
    with engine.begin() as c:
        ids = {r: c.execute(text("""INSERT INTO public.notifications (recipient_role, severity, subject, body, is_read, created_at)
                                    VALUES (:r, 'LOW', :s, 'pytest', FALSE, now()) RETURNING id"""), {"r": r, "s": f"pytest-role-{r}"}).scalar()
               for r in ("ADMIN", "PROCUREMENT", "QUALITY")}
        before = {r[0]: r[1] for r in c.execute(text("SELECT id, is_read FROM public.notifications"))}
    try:
        seen = lambda u, r: {n["subject"] for n in cl.get("/api/notifications", headers=H(u, r)).json() if n["subject"].startswith("pytest-role-")}  # noqa: E731
        assert seen("akash", "PROCUREMENT") == {"pytest-role-PROCUREMENT"}
        assert seen("rakesh", "QUALITY") == {"pytest-role-QUALITY"}
        assert seen("sainanthan", "ADMIN") == {"pytest-role-ADMIN", "pytest-role-PROCUREMENT", "pytest-role-QUALITY"}
        assert cl.post("/api/notifications/read-all", headers=H("akash", "PROCUREMENT")).status_code == 200
        with engine.connect() as c:
            st = {r[0]: r[1] for r in c.execute(text("SELECT id, is_read FROM public.notifications WHERE id = ANY(:i)"), {"i": list(ids.values())})}
        assert st[ids["PROCUREMENT"]] is True and st[ids["QUALITY"]] is False and st[ids["ADMIN"]] is False
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM public.notifications WHERE id = ANY(:i)"), {"i": list(ids.values())})
            for nid, was in before.items():                                                   # restore read flags exactly
                c.execute(text("UPDATE public.notifications SET is_read = :w WHERE id = :i AND is_read IS DISTINCT FROM :w"), {"w": was, "i": nid})
