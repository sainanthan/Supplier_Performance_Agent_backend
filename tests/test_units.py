"""Fast unit tests (no database): auth, grounding validator, significance test, access policy.

    cd backend && .venv/Scripts/python -m pytest -q
"""
from app import auth as AU
from app.analytics import significant_change
from app.grounding import check


def test_password_hash_roundtrip():
    h = AU.hash_password("s3cret-pass")
    assert AU.verify_password("s3cret-pass", h)
    assert not AU.verify_password("wrong", h)


def test_token_roundtrip_and_tamper():
    tok = AU.issue_token(AU.UserCtx("quality", "QUALITY"))
    assert AU.read_token(tok).role == "QUALITY"
    body, sig = tok.split(".")
    assert AU.read_token(body + "." + sig[:-2] + "xx") is None


def test_policy():
    assert AU.allowed("GET", "/api/health", None)
    assert not AU.allowed("GET", "/api/overview", None)
    assert AU.allowed("GET", "/api/overview", "VIEWER")
    assert not AU.allowed("POST", "/api/actions", "VIEWER")
    assert AU.allowed("POST", "/api/actions", "QUALITY")
    assert not AU.allowed("GET", "/api/compare", "QUALITY")
    assert AU.allowed("GET", "/api/compare", "PROCUREMENT")
    assert not AU.allowed("PUT", "/api/config/scorecard", "PROCUREMENT")
    assert AU.allowed("PUT", "/api/config/scorecard", "ADMIN")
    assert not AU.allowed("GET", "/api/overview", "SUPPLIER")
    assert AU.allowed("GET", "/api/portal/me", "SUPPLIER")
    assert not AU.allowed("GET", "/api/portal/me", "QUALITY")


def test_grounding_accepts_source_figures_and_units():
    src = ['{"otif_pct": 24.3, "revenue_at_risk_inr": 184912345.0, "recent": 30.0, "prior": 41.2}']
    assert check("OTIF is 24.3% and revenue at risk is ₹18.49 Cr.", src)["grounded"]
    assert check("OTIF fell 11.2 pts (41.2% to 30%).", src)["grounded"]          # difference of two source figures
    assert check("Top 3 suppliers in 2026 on 2026-10-31.", src)["grounded"]       # small ints, years, dates ignored


def test_grounding_rejects_wrong_scale():
    src = ['{"revenue_at_risk_inr": 184912345.0}']
    r = check("Revenue at risk is ₹184.9 Cr.", src)                                # 10x error seen before
    assert not r["grounded"] and r["unsupported"]


def test_significance():
    assert not significant_change(0.40, 10, 0.30, 10)                             # small sample: noise
    assert significant_change(0.40, 400, 0.30, 400)                               # large sample: real
